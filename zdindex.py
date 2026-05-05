#!/usr/bin/env python3
"""
zdindex — build a SQLite + FTS5 index from the local Zendesk export.

Reads the same files as zdsearch.py (./data) and writes ./data/zdsearch.sqlite.
Designed to be invoked once after a fresh export; the GUI uses the resulting
index for sub-second live search.

Stdlib only. Resumable via per-source mtime fingerprints in the meta table:
re-running with no source changes is a no-op. --rebuild forces a clean rebuild.

Progress reporting is emitted on stdout as `key=value` lines for the GUI:
  phase=start
  phase=users count=…
  phase=orgs count=…
  phase=groups count=…
  phase=tickets count=… total=…
  phase=comments count=… total=…
  phase=optimize
  phase=done seconds=… size_bytes=…
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA_VERSION = "1"
DEFAULT_DATA = Path(__file__).resolve().parent / "data"


# --- helpers ----------------------------------------------------------------

def _iter_ndjson(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _parse_ts(s: str | None) -> int | None:
    """Zendesk ISO-8601 → unix epoch seconds (UTC)."""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def _file_fingerprints(data_dir: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    for name in (
        "tickets.ndjson",
        "users.ndjson",
        "organizations.ndjson",
        "groups.json",
        "ticket_fields.json",
        "comments_all.ndjson",
    ):
        p = data_dir / name
        if p.exists():
            out[name] = p.stat().st_mtime
    cdir = data_dir / "comments"
    if cdir.exists():
        # mtime of dir + count is good enough as a fingerprint
        out["comments/"] = cdir.stat().st_mtime
        out["comments_count"] = len(list(cdir.iterdir()))
    return out


def emit(progress: bool, **kv) -> None:
    if not progress:
        return
    parts = [f"{k}={v}" for k, v in kv.items()]
    print(" ".join(parts), flush=True)


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    with path.open("rb") as f:
        for _ in f:
            n += 1
    return n


# --- schema -----------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name TEXT, email TEXT, role TEXT,
  organization_id INTEGER, phone TEXT, active INTEGER
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_org ON users(organization_id);

CREATE TABLE IF NOT EXISTS organizations (
  id INTEGER PRIMARY KEY,
  name TEXT, domain_names_json TEXT
);

CREATE TABLE IF NOT EXISTS groups_ (
  id INTEGER PRIMARY KEY,
  name TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY,
  subject TEXT, description TEXT,
  status TEXT, priority TEXT, type TEXT,
  requester_id INTEGER, assignee_id INTEGER, submitter_id INTEGER,
  organization_id INTEGER, group_id INTEGER,
  created_at TEXT, updated_at TEXT,
  created_ts INTEGER, updated_ts INTEGER,
  tags_json TEXT, custom_fields_json TEXT,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_requester ON tickets(requester_id);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee  ON tickets(assignee_id);
CREATE INDEX IF NOT EXISTS idx_tickets_org       ON tickets(organization_id);
CREATE INDEX IF NOT EXISTS idx_tickets_group     ON tickets(group_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status    ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_priority  ON tickets(priority);
CREATE INDEX IF NOT EXISTS idx_tickets_type      ON tickets(type);
CREATE INDEX IF NOT EXISTS idx_tickets_updated   ON tickets(updated_ts);
CREATE INDEX IF NOT EXISTS idx_tickets_created   ON tickets(created_ts);

CREATE TABLE IF NOT EXISTS ticket_tags (
  ticket_id INTEGER NOT NULL,
  tag TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticket_tags_tag    ON ticket_tags(tag);
CREATE INDEX IF NOT EXISTS idx_ticket_tags_ticket ON ticket_tags(ticket_id);

CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY,
  ticket_id INTEGER, author_id INTEGER, public INTEGER,
  created_at TEXT, created_ts INTEGER,
  body TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_ticket ON comments(ticket_id);
CREATE INDEX IF NOT EXISTS idx_comments_author ON comments(author_id);
CREATE INDEX IF NOT EXISTS idx_comments_created ON comments(created_ts);

CREATE VIRTUAL TABLE IF NOT EXISTS tickets_fts USING fts5(
  subject, description, tags, custom_fields,
  tokenize='unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS comments_fts USING fts5(
  body,
  tokenize='unicode61 remove_diacritics 2'
);
"""


def _set_build_pragmas(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA journal_mode = OFF")
    con.execute("PRAGMA synchronous = OFF")
    con.execute("PRAGMA temp_store = MEMORY")
    con.execute("PRAGMA cache_size = -200000")  # 200 MB
    con.execute("PRAGMA locking_mode = EXCLUSIVE")


def _set_runtime_pragmas(con: sqlite3.Connection) -> None:
    # Checkpoint and drop WAL so the resulting file has no -wal/-shm
    # sidecars. The web UI opens this file from a different connection
    # right after build; on macOS, a lingering -wal can cause the next
    # open to fail with "unable to open database file".
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA journal_mode = DELETE")
    con.execute("PRAGMA synchronous = NORMAL")


# --- ingest -----------------------------------------------------------------

def ingest_users(con: sqlite3.Connection, path: Path, progress: bool) -> int:
    rows = []
    n = 0
    for u in _iter_ndjson(path):
        uid = u.get("id")
        if uid is None:
            continue
        rows.append((
            uid,
            u.get("name") or "",
            u.get("email") or "",
            u.get("role") or "",
            u.get("organization_id"),
            u.get("phone") or "",
            1 if u.get("active", True) else 0,
        ))
        if len(rows) >= 5000:
            con.executemany(
                "INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?)", rows
            )
            n += len(rows)
            rows.clear()
            emit(progress, phase="users", count=n)
    if rows:
        con.executemany("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?)", rows)
        n += len(rows)
    emit(progress, phase="users", count=n)
    return n


def ingest_orgs(con: sqlite3.Connection, path: Path, progress: bool) -> int:
    rows = []
    n = 0
    for o in _iter_ndjson(path):
        oid = o.get("id")
        if oid is None:
            continue
        rows.append((oid, o.get("name") or "", json.dumps(o.get("domain_names") or [])))
        if len(rows) >= 5000:
            con.executemany("INSERT OR REPLACE INTO organizations VALUES (?,?,?)", rows)
            n += len(rows)
            rows.clear()
    if rows:
        con.executemany("INSERT OR REPLACE INTO organizations VALUES (?,?,?)", rows)
        n += len(rows)
    emit(progress, phase="orgs", count=n)
    return n


def ingest_groups(con: sqlite3.Connection, path: Path, progress: bool) -> int:
    if not path.exists():
        emit(progress, phase="groups", count=0)
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        emit(progress, phase="groups", count=0)
        return 0
    rows = [
        (g["id"], g.get("name") or "")
        for g in data.get("groups", [])
        if "id" in g
    ]
    if rows:
        con.executemany("INSERT OR REPLACE INTO groups_ VALUES (?,?)", rows)
    emit(progress, phase="groups", count=len(rows))
    return len(rows)


def ingest_tickets(con: sqlite3.Connection, path: Path, progress: bool) -> int:
    """Stream tickets.ndjson into tickets + ticket_tags + tickets_fts."""
    total_est = _count_lines(path)
    n = 0
    rows: list[tuple] = []
    fts_rows: list[tuple] = []
    tag_rows: list[tuple] = []

    INS_TICKET = (
        "INSERT OR REPLACE INTO tickets "
        "(id, subject, description, status, priority, type, "
        " requester_id, assignee_id, submitter_id, organization_id, group_id, "
        " created_at, updated_at, created_ts, updated_ts, "
        " tags_json, custom_fields_json, raw_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    INS_FTS = (
        "INSERT INTO tickets_fts(rowid, subject, description, tags, custom_fields) "
        "VALUES (?,?,?,?,?)"
    )
    INS_TAG = "INSERT INTO ticket_tags(ticket_id, tag) VALUES (?,?)"

    BATCH = 5000

    def flush():
        nonlocal rows, fts_rows, tag_rows
        if rows:
            con.executemany(INS_TICKET, rows)
            con.executemany(INS_FTS, fts_rows)
            if tag_rows:
                con.executemany(INS_TAG, tag_rows)
            rows = []
            fts_rows = []
            tag_rows = []

    for t in _iter_ndjson(path):
        tid = t.get("id")
        if tid is None:
            continue
        tags = t.get("tags") or []
        cf = t.get("custom_fields") or []

        cf_text_parts: list[str] = []
        for c in cf:
            v = c.get("value")
            if v in (None, "", []):
                continue
            if isinstance(v, list):
                cf_text_parts.extend(str(x) for x in v)
            else:
                cf_text_parts.append(str(v))
        cf_text = " ".join(cf_text_parts)
        tags_text = " ".join(str(x) for x in tags)

        rows.append((
            tid,
            t.get("subject") or "",
            t.get("description") or "",
            (t.get("status") or "").lower(),
            (t.get("priority") or "").lower(),
            (t.get("type") or "").lower(),
            t.get("requester_id"),
            t.get("assignee_id"),
            t.get("submitter_id"),
            t.get("organization_id"),
            t.get("group_id"),
            t.get("created_at"),
            t.get("updated_at"),
            _parse_ts(t.get("created_at")),
            _parse_ts(t.get("updated_at")),
            json.dumps(tags),
            json.dumps(cf),
            json.dumps(t, ensure_ascii=False),
        ))
        fts_rows.append((tid, t.get("subject") or "", t.get("description") or "", tags_text, cf_text))
        for tag in tags:
            tag_rows.append((tid, str(tag).lower()))

        if len(rows) >= BATCH:
            flush()
            n += BATCH
            emit(progress, phase="tickets", count=n, total=total_est)

    if rows:
        m = len(rows)
        flush()
        n += m
    emit(progress, phase="tickets", count=n, total=total_est)
    return n


def _iter_comment_records(data_dir: Path) -> Iterator[tuple[int, list[dict]]]:
    """Yield (ticket_id, comments_list). Prefers comments_all.ndjson when present,
    otherwise walks comments/<id>.json."""
    consolidated = data_dir / "comments_all.ndjson"
    if consolidated.exists():
        for rec in _iter_ndjson(consolidated):
            tid = rec.get("ticket_id")
            if tid is None:
                continue
            yield tid, list(rec.get("comments") or [])
        return
    cdir = data_dir / "comments"
    if not cdir.exists():
        return
    for entry in sorted(cdir.iterdir()):
        if not entry.name.endswith(".json"):
            continue
        try:
            tid = int(entry.name.split(".")[0])
        except ValueError:
            continue
        try:
            d = json.loads(entry.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        yield tid, list(d.get("comments") or [])


def ingest_comments(con: sqlite3.Connection, data_dir: Path, progress: bool) -> int:
    INS_C = (
        "INSERT OR REPLACE INTO comments "
        "(id, ticket_id, author_id, public, created_at, created_ts, body) "
        "VALUES (?,?,?,?,?,?,?)"
    )
    INS_FTS = "INSERT INTO comments_fts(rowid, body) VALUES (?,?)"
    BATCH = 5000

    rows: list[tuple] = []
    fts_rows: list[tuple] = []
    n = 0
    tickets_seen = 0

    consolidated = data_dir / "comments_all.ndjson"
    cdir = data_dir / "comments"
    if consolidated.exists():
        total_tickets = _count_lines(consolidated)
    elif cdir.exists():
        total_tickets = sum(1 for _ in cdir.iterdir())
    else:
        total_tickets = 0

    for tid, comments in _iter_comment_records(data_dir):
        tickets_seen += 1
        for c in comments:
            cid = c.get("id")
            if cid is None:
                continue
            body = c.get("plain_body") or c.get("body") or c.get("html_body") or ""
            rows.append((
                cid,
                tid,
                c.get("author_id"),
                1 if c.get("public", True) else 0,
                c.get("created_at"),
                _parse_ts(c.get("created_at")),
                body,
            ))
            fts_rows.append((cid, body))

        if len(rows) >= BATCH:
            con.executemany(INS_C, rows)
            con.executemany(INS_FTS, fts_rows)
            n += len(rows)
            rows = []
            fts_rows = []
            emit(progress, phase="comments", count=n, total=total_tickets, tickets=tickets_seen)

    if rows:
        con.executemany(INS_C, rows)
        con.executemany(INS_FTS, fts_rows)
        n += len(rows)

    emit(progress, phase="comments", count=n, total=total_tickets, tickets=tickets_seen)
    return n


# --- main -------------------------------------------------------------------

def build(data_dir: Path, db_path: Path, rebuild: bool, progress: bool) -> None:
    t0 = time.time()
    emit(progress, phase="start")

    if rebuild and db_path.exists():
        db_path.unlink()
        for ext in ("-wal", "-shm"):
            p = db_path.with_name(db_path.name + ext)
            if p.exists():
                p.unlink()

    fresh = not db_path.exists()
    con = sqlite3.connect(str(db_path))
    try:
        _set_build_pragmas(con)
        con.executescript(SCHEMA_SQL)
        con.commit()

        # Skip if nothing has changed since last build.
        prints = _file_fingerprints(data_dir)
        prev_raw = con.execute(
            "SELECT value FROM meta WHERE key='source_mtimes'"
        ).fetchone()
        prev_ver = con.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if (
            not fresh
            and not rebuild
            and prev_raw
            and prev_ver
            and prev_ver[0] == SCHEMA_VERSION
            and json.loads(prev_raw[0]) == prints
        ):
            emit(progress, phase="up_to_date")
            emit(progress, phase="done", seconds=round(time.time() - t0, 2),
                 size_bytes=db_path.stat().st_size if db_path.exists() else 0)
            return

        # Wipe content tables (keep schema). FTS shadow tables are reset implicitly.
        con.execute("BEGIN")
        for tbl in (
            "users", "organizations", "groups_",
            "tickets", "ticket_tags", "comments",
            "tickets_fts", "comments_fts",
        ):
            con.execute(f"DELETE FROM {tbl}")
        con.commit()

        con.execute("BEGIN")
        ingest_users(con, data_dir / "users.ndjson", progress)
        ingest_orgs(con, data_dir / "organizations.ndjson", progress)
        ingest_groups(con, data_dir / "groups.json", progress)
        ingest_tickets(con, data_dir / "tickets.ndjson", progress)
        ingest_comments(con, data_dir, progress)
        con.commit()

        emit(progress, phase="optimize")
        con.execute("INSERT INTO tickets_fts(tickets_fts) VALUES('optimize')")
        con.execute("INSERT INTO comments_fts(comments_fts) VALUES('optimize')")
        con.execute("ANALYZE")

        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,))
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('built_at', ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
        )
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('source_mtimes', ?)",
            (json.dumps(prints),),
        )
        con.commit()

        _set_runtime_pragmas(con)
    finally:
        con.close()

    size = db_path.stat().st_size if db_path.exists() else 0
    emit(progress, phase="done", seconds=round(time.time() - t0, 2), size_bytes=size)


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="zdindex",
        description="Build a SQLite/FTS5 index over the local Zendesk export.",
    )
    p.add_argument("--data-dir", default=str(DEFAULT_DATA),
                   help=f"path to ./data (default {DEFAULT_DATA})")
    p.add_argument("--db", default=None,
                   help="output sqlite path (default: <data-dir>/zdsearch.sqlite)")
    p.add_argument("--rebuild", action="store_true",
                   help="discard existing index and rebuild from scratch")
    p.add_argument("--progress", action="store_true",
                   help="emit machine-readable progress on stdout")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    data_dir = Path(args.data_dir).expanduser().resolve()
    if not data_dir.exists():
        print(f"error: data directory not found: {data_dir}", file=sys.stderr)
        return 2
    db_path = Path(args.db).expanduser().resolve() if args.db else (data_dir / "zdsearch.sqlite")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    build(data_dir, db_path, rebuild=args.rebuild, progress=args.progress)
    print(f"index: {db_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
