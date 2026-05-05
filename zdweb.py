#!/usr/bin/env python3
"""
zdweb — local web UI for searching the archived Zendesk export.

Run:  ./zdweb.py [--data-dir PATH] [--port 8765] [--no-open]

Starts a tiny HTTP server on localhost backed by data/zdsearch.sqlite (the
SQLite/FTS5 index produced by zdindex.py) and opens the search page in your
default browser. If the index is missing it builds one first.

Stdlib only.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
DEFAULT_DATA = THIS_DIR / "data"
DEFAULT_URL_TEMPLATE = "https://lyneshelpcenter.zendesk.com/agent/tickets/{id}"

UPDATE_REPO_URL = "https://github.com/CrissZollo/ZendeskTicket"
UPDATE_BRANCH = "main"
UPDATE_TIMEOUT_SECONDS = 10
UPDATE_SKIP_DIRS = {"data"}
AUTO_UPDATE_DISABLED_FILE = "auto_update_disabled.txt"
LAST_DATA_DIR_FILE = "last-data-dir.txt"
UPDATE_SKIP_FILES = {AUTO_UPDATE_DISABLED_FILE, LAST_DATA_DIR_FILE}


def auto_update_enabled(install_dir: Path) -> bool:
    """Auto-update is on by default. Drop a file named
    auto_update_disabled.txt next to the script to turn it off.
    """
    return not (install_dir / AUTO_UPDATE_DISABLED_FILE).exists()


def read_last_data_dir(install_dir: Path) -> Path | None:
    """Return the data folder remembered from a previous run, or None.

    The file holds the absolute path on its first non-comment line. Returns
    None when the file is missing, unreadable, empty, or points at a path
    that no longer exists or isn't a directory — callers fall through to
    the default in that case.
    """
    saved = install_dir / LAST_DATA_DIR_FILE
    if not saved.exists():
        return None
    try:
        lines = saved.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        try:
            p = Path(s).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        return p if p.is_dir() else None
    return None


def write_last_data_dir(install_dir: Path, data_dir: Path) -> None:
    """Persist the most recently chosen data folder next to the script.

    Failures are swallowed: an inability to write this file must never
    break the user-facing setup flow.
    """
    saved = install_dir / LAST_DATA_DIR_FILE
    try:
        saved.write_text(
            f"{data_dir}\n"
            "# The data folder picked via the web setup page.\n"
            "# Used automatically on next startup unless --data-dir is passed.\n"
            "# Delete this file to forget the saved location.\n",
            encoding="utf-8",
        )
    except OSError:
        pass


# --- self-update -----------------------------------------------------------

def self_update(install_dir: Path) -> int:
    """Fetch the latest version from GitHub and overwrite changed files.

    Returns the number of files written. Returns 0 on any error or when
    nothing needed updating. Anything inside a directory listed in
    UPDATE_SKIP_DIRS (e.g. user data) is left untouched.
    """
    zip_url = f"{UPDATE_REPO_URL}/archive/refs/heads/{UPDATE_BRANCH}.zip"
    print(f"[zdweb] checking for updates from {UPDATE_REPO_URL}…", flush=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = tmp_path / "update.zip"
            req = urllib.request.Request(zip_url, headers={"User-Agent": "zdweb-updater"})
            with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT_SECONDS) as resp:
                with open(zip_path, "wb") as f:
                    shutil.copyfileobj(resp, f)

            extract_root = tmp_path / "extract"
            extract_root.mkdir()
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_root)

            entries = [p for p in extract_root.iterdir() if p.is_dir()]
            if not entries:
                print("[zdweb] update archive was empty, skipping.", flush=True)
                return 0
            src_root = entries[0]

            changed = 0
            for src_file in src_root.rglob("*"):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(src_root)
                if rel.parts and rel.parts[0] in UPDATE_SKIP_DIRS:
                    continue
                if rel.name in UPDATE_SKIP_FILES:
                    continue
                dst_file = install_dir / rel
                if dst_file.exists() and _files_equal(src_file, dst_file):
                    continue
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)
                changed += 1
            return changed
    except (urllib.error.URLError, socket.timeout, OSError, zipfile.BadZipFile) as e:
        print(f"[zdweb] update check failed (continuing with local version): {e}", flush=True)
        return 0


def _files_equal(a: Path, b: Path, chunk: int = 65536) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
    except OSError:
        return False
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ba = fa.read(chunk)
                bb = fb.read(chunk)
                if ba != bb:
                    return False
                if not ba:
                    return True
    except OSError:
        return False


# --- helpers shared with the GUI/CLI ---------------------------------------

def _fts_escape(q: str) -> str:
    """Build a safe FTS5 MATCH expression from a free-text query."""
    tokens = re.findall(r'"[^"]+"|\S+', q.strip())
    out: list[str] = []
    for tok in tokens:
        if tok.startswith('"') and tok.endswith('"'):
            inner = tok[1:-1].replace('"', "")
            if inner:
                out.append(f'"{inner}"')
        else:
            inner = re.sub(r'[^A-Za-z0-9_À-￿]+', "", tok)
            if not inner:
                continue
            out.append(f'"{inner}"*')
    return " AND ".join(out)


# --- data layer -------------------------------------------------------------

class Backend:
    def __init__(self, db_path: Path):
        # Plain RW open (not URI mode=ro) so SQLite can manage the -shm/-wal
        # sidecar files when the index was just written in WAL mode. A strict
        # read-only open fails with "unable to open database file" on macOS
        # if a -wal file is present and -shm needs to be (re)created.
        self.con = sqlite3.connect(str(db_path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA query_only = 1")
        self.con.execute("PRAGMA cache_size = -100000")
        self.con.execute("PRAGMA temp_store = MEMORY")
        self._lock = threading.Lock()

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass

    def list_tags(self, limit: int = 500) -> list[str]:
        with self._lock:
            return [r[0] for r in self.con.execute(
                "SELECT tag, COUNT(*) c FROM ticket_tags GROUP BY tag ORDER BY c DESC LIMIT ?",
                (limit,),
            ).fetchall()]

    def list_statuses(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self.con.execute(
                "SELECT DISTINCT status FROM tickets WHERE status != '' ORDER BY status"
            ).fetchall()]

    def lookup_user(self, uid):
        if uid is None:
            return None
        with self._lock:
            r = self.con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None

    def lookup_org(self, oid):
        if oid is None:
            return None
        with self._lock:
            r = self.con.execute("SELECT * FROM organizations WHERE id=?", (oid,)).fetchone()
        return dict(r) if r else None

    def lookup_group(self, gid):
        if gid is None:
            return None
        with self._lock:
            r = self.con.execute("SELECT name FROM groups_ WHERE id=?", (gid,)).fetchone()
        return r[0] if r else None

    def search(self, params: dict, limit: int = 200, offset: int = 0) -> tuple[list[dict], int]:
        clauses: list[str] = []
        sql_params: list[Any] = []

        text = (params.get("q") or "").strip()
        scope_tickets = params.get("st", "1") == "1"
        scope_comments = params.get("sc", "1") == "1"

        match_expr = _fts_escape(text) if text else ""
        if text and match_expr:
            scopes = []
            if scope_tickets:
                scopes.append("SELECT rowid AS id FROM tickets_fts WHERE tickets_fts MATCH ?")
                sql_params.append(match_expr)
            if scope_comments:
                scopes.append(
                    "SELECT c.ticket_id AS id FROM comments c "
                    "JOIN comments_fts cf ON cf.rowid = c.id WHERE comments_fts MATCH ?"
                )
                sql_params.append(match_expr)
            if not scopes:
                return [], 0
            clauses.append("t.id IN (" + " UNION ".join(scopes) + ")")

        if params.get("status"):
            clauses.append("t.status = ?"); sql_params.append(params["status"].lower())
        if params.get("priority"):
            clauses.append("t.priority = ?"); sql_params.append(params["priority"].lower())
        if params.get("type"):
            clauses.append("t.type = ?"); sql_params.append(params["type"].lower())
        if params.get("tag"):
            clauses.append("t.id IN (SELECT ticket_id FROM ticket_tags WHERE tag = ?)")
            sql_params.append(params["tag"].lower())

        for field, sql_col, table, search_cols in (
            ("requester", "requester_id", "users", ("name", "email")),
            ("assignee", "assignee_id", "users", ("name", "email")),
            ("organization", "organization_id", "organizations", ("name", "domain_names_json")),
            ("group", "group_id", "groups_", ("name",)),
        ):
            needle = (params.get(field) or "").strip()
            if not needle:
                continue
            like = f"%{needle.lower()}%"
            cond = " OR ".join(f"LOWER({c}) LIKE ?" for c in search_cols)
            with self._lock:
                ids = [r[0] for r in self.con.execute(
                    f"SELECT id FROM {table} WHERE {cond}",
                    [like] * len(search_cols),
                ).fetchall()]
            if not ids:
                return [], 0
            clauses.append(f"t.{sql_col} IN ({','.join('?' * len(ids))})")
            sql_params.extend(ids)

        for date_field, op in (
            ("created_after", ">="), ("created_before", "<="),
            ("updated_after", ">="), ("updated_before", "<="),
        ):
            v = params.get(date_field)
            if not v:
                continue
            try:
                ts = _parse_date_to_ts(v, end_of_day=date_field.endswith("_before"))
            except ValueError:
                continue
            col = "created_ts" if date_field.startswith("created") else "updated_ts"
            clauses.append(f"t.{col} {op} ?")
            sql_params.append(ts)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cnt_sql = "SELECT COUNT(*) FROM tickets t" + where

        sort_cols = {
            "id":       "t.id",
            "status":   "t.status",
            "priority": "t.priority",
            "updated":  "t.updated_ts",
            "subject":  "t.subject COLLATE NOCASE",
        }
        sort_expr = sort_cols.get(params.get("sort") or "updated", sort_cols["updated"])
        order_dir = "ASC" if str(params.get("order") or "desc").lower() == "asc" else "DESC"

        sel_sql = (
            "SELECT t.id, t.subject, t.status, t.priority, t.type, t.created_at, t.updated_at, "
            "       t.requester_id, t.assignee_id, t.organization_id, t.group_id, t.tags_json "
            "FROM tickets t" + where +
            f" ORDER BY {sort_expr} {order_dir}, t.id DESC LIMIT ? OFFSET ?"
        )

        with self._lock:
            total = self.con.execute(cnt_sql, sql_params).fetchone()[0]
            rows = [dict(r) for r in self.con.execute(sel_sql, sql_params + [limit, offset]).fetchall()]

        # Enrich rows with names so the UI can render without N round-trips
        for r in rows:
            u = self.lookup_user(r["requester_id"]) or {}
            r["requester_name"] = u.get("name") or u.get("email") or ""
            r["requester_email"] = u.get("email") or ""
            a = self.lookup_user(r["assignee_id"]) or {}
            r["assignee_name"] = a.get("name") or a.get("email") or ""
            o = self.lookup_org(r["organization_id"]) or {}
            r["organization_name"] = o.get("name") or ""
            try:
                r["tags"] = json.loads(r.pop("tags_json") or "[]")
            except json.JSONDecodeError:
                r["tags"] = []
        return rows, total

    def ticket_with_thread(self, tid: int) -> dict | None:
        with self._lock:
            t = self.con.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if not t:
            return None
        t = dict(t)
        try:
            t["tags"] = json.loads(t.get("tags_json") or "[]")
        except json.JSONDecodeError:
            t["tags"] = []
        try:
            t["custom_fields"] = json.loads(t.get("custom_fields_json") or "[]")
        except json.JSONDecodeError:
            t["custom_fields"] = []
        for k in ("tags_json", "custom_fields_json", "raw_json"):
            t.pop(k, None)

        with self._lock:
            comments = [dict(r) for r in self.con.execute(
                "SELECT id, ticket_id, author_id, public, created_at, body "
                "FROM comments WHERE ticket_id=? ORDER BY created_ts, id",
                (tid,),
            ).fetchall()]

        # Resolve author names
        for c in comments:
            au = self.lookup_user(c["author_id"]) or {}
            c["author_name"] = au.get("name") or ""
            c["author_email"] = au.get("email") or ""

        t["comments"] = comments
        t["requester"] = self.lookup_user(t.get("requester_id"))
        t["assignee"] = self.lookup_user(t.get("assignee_id"))
        t["organization"] = self.lookup_org(t.get("organization_id"))
        t["group_name"] = self.lookup_group(t.get("group_id"))
        return t

    def search_users(self, needle: str, limit: int = 50) -> list[dict]:
        if not needle:
            return []
        like = f"%{needle.lower()}%"
        with self._lock:
            rows = self.con.execute(
                "SELECT u.*, o.name AS organization_name "
                "FROM users u LEFT JOIN organizations o ON o.id = u.organization_id "
                "WHERE LOWER(u.name) LIKE ? OR LOWER(u.email) LIKE ? OR LOWER(u.phone) LIKE ? "
                "LIMIT ?",
                (like, like, like, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_orgs(self, needle: str, limit: int = 50) -> list[dict]:
        if not needle:
            return []
        like = f"%{needle.lower()}%"
        with self._lock:
            rows = self.con.execute(
                "SELECT * FROM organizations WHERE LOWER(name) LIKE ? OR LOWER(domain_names_json) LIKE ? LIMIT ?",
                (like, like, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["domain_names"] = json.loads(d.pop("domain_names_json") or "[]")
            except json.JSONDecodeError:
                d["domain_names"] = []
            out.append(d)
        return out


def _parse_date_to_ts(s: str, end_of_day: bool = False) -> int:
    from datetime import datetime, timezone
    d = datetime.strptime(s, "%Y-%m-%d")
    if end_of_day:
        d = d.replace(hour=23, minute=59, second=59)
    return int(d.replace(tzinfo=timezone.utc).timestamp())


# --- HTTP layer -------------------------------------------------------------

class AppState:
    """Mutable state shared across handler threads while we wait for the user
    to point us at a data folder via the web setup page."""
    def __init__(self, data_dir: Path, db_path: Path | None):
        self.data_dir = data_dir
        self.db_path = db_path  # None until configured
        self.backend: Backend | None = None
        self.lock = threading.Lock()
        self.setup_in_progress = False

    def is_ready(self) -> bool:
        return self.backend is not None


class Handler(http.server.BaseHTTPRequestHandler):
    state: "AppState" = None  # set by main()
    url_template: str = DEFAULT_URL_TEMPLATE

    @property
    def backend(self) -> Backend:
        return self.state.backend

    def log_message(self, fmt, *args):
        # Quieter access log
        sys.stderr.write(f"[zdweb] {self.address_string()} {fmt % args}\n")

    def _json(self, code: int, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str):
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _fs_list(self, params: dict):
        """List subdirectories of a folder, for the web folder picker."""
        target = params.get("path") or str(Path.home())
        try:
            p = Path(target).expanduser().resolve()
        except (OSError, RuntimeError) as e:
            return self._json(400, {"error": f"bad path: {e}"})
        if not p.exists():
            return self._json(404, {"error": f"not found: {p}"})
        if not p.is_dir():
            return self._json(400, {"error": f"not a directory: {p}"})
        entries = []
        try:
            it = os.scandir(p)
        except PermissionError:
            return self._json(403, {"error": f"permission denied: {p}"})
        try:
            while True:
                try:
                    e = next(it)
                except StopIteration:
                    break
                except OSError:
                    # Unreadable entry; skip it and keep listing the rest.
                    continue
                if e.name.startswith("."):
                    continue
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                sub = Path(e.path)
                try:
                    is_export = _has_export_data(sub)
                except OSError:
                    is_export = False
                entries.append({
                    "name": e.name,
                    "path": str(sub),
                    "is_export": is_export,
                })
        finally:
            it.close()
        entries.sort(key=lambda x: x["name"].lower())
        parent = str(p.parent) if p.parent != p else None
        return self._json(200, {
            "path": str(p),
            "parent": parent,
            "is_export": _has_export_data(p),
            "entries": entries,
            "sep": os.sep,
        })

    def do_GET(self):
        try:
            url = urllib.parse.urlsplit(self.path)
            path = url.path
            qs = urllib.parse.parse_qs(url.query)
            params = {k: v[0] for k, v in qs.items()}

            # Setup-mode routes: only the setup page, the fs picker, and the
            # status endpoint work until a backend is configured.
            if not self.state.is_ready():
                if path == "/setup":
                    return self._html(_render_setup_html(str(self.state.data_dir)))
                if path == "/api/setup/status":
                    return self._json(200, {
                        "ready": False,
                        "data_dir": str(self.state.data_dir),
                        "in_progress": self.state.setup_in_progress,
                    })
                if path == "/api/fs/list":
                    return self._fs_list(params)
                if path == "/api/fs/roots":
                    return self._json(200, _fs_roots())
                if path == "/":
                    return self._redirect("/setup")
                return self._json(503, {"error": "setup required", "setup_url": "/setup"})

            if path == "/setup":
                # Configured already, but the user may want to switch folders.
                return self._html(_render_setup_html(str(self.state.data_dir)))
            if path == "/api/setup/status":
                return self._json(200, {"ready": True, "data_dir": str(self.state.data_dir)})
            if path == "/api/fs/list":
                return self._fs_list(params)
            if path == "/api/fs/roots":
                return self._json(200, _fs_roots())

            if path == "/":
                return self._html(_render_index_html(self.url_template))
            if path == "/api/meta":
                return self._json(200, {
                    "tags": self.backend.list_tags(500),
                    "statuses": self.backend.list_statuses(),
                    "url_template": self.url_template,
                })
            if path == "/api/search":
                limit = max(1, min(int(params.get("limit", 200)), 1000))
                offset = max(0, int(params.get("offset", 0)))
                rows, total = self.backend.search(params, limit=limit, offset=offset)
                return self._json(200, {"rows": rows, "total": total})
            if path.startswith("/api/ticket/"):
                try:
                    tid = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    return self._json(400, {"error": "bad ticket id"})
                t = self.backend.ticket_with_thread(tid)
                if not t:
                    return self._json(404, {"error": "not found"})
                return self._json(200, t)
            if path == "/api/users":
                return self._json(200, {
                    "users": self.backend.search_users(params.get("q", ""), 50),
                    "orgs": self.backend.search_orgs(params.get("q", ""), 50),
                })
            self.send_error(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json(500, {"error": str(e)})
            except Exception:
                pass

    def do_POST(self):
        try:
            url = urllib.parse.urlsplit(self.path)
            path = url.path
            if path != "/api/setup":
                return self._json(404, {"error": "not found"})

            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid JSON body"})

            chosen = (payload.get("path") or "").strip()
            if not chosen:
                return self._json(400, {"error": "missing 'path'"})

            try:
                picked = Path(chosen).expanduser().resolve()
            except (OSError, RuntimeError) as e:
                return self._json(400, {"error": f"invalid path: {e}"})

            if not picked.exists() or not picked.is_dir():
                return self._json(400, {"error": f"folder not found: {picked}"})
            if not _has_export_data(picked):
                return self._json(400, {
                    "error": (f"no Zendesk export files in {picked}. "
                              f"Expected one of: {', '.join(_EXPECTED_EXPORT_FILES)}"),
                })

            with self.state.lock:
                if self.state.setup_in_progress:
                    return self._json(409, {"error": "setup already in progress"})
                self.state.setup_in_progress = True

            old_backend = self.state.backend
            try:
                # When switching folders, derive a fresh db_path from the chosen
                # folder unless --db was pinned at startup.
                db_path = (picked / "zdsearch.sqlite")
                _build_index_if_missing(picked, db_path)
                backend = Backend(db_path)
                self.state.data_dir = picked
                self.state.db_path = db_path
                self.state.backend = backend
                if old_backend is not None and old_backend is not backend:
                    try:
                        old_backend.close()
                    except Exception:
                        pass
                write_last_data_dir(THIS_DIR, picked)
                print(f"[zdweb] setup complete — using {picked}", flush=True)
                return self._json(200, {"ok": True, "data_dir": str(picked)})
            except Exception as e:
                return self._json(500, {"error": f"failed to build index: {e}"})
            finally:
                self.state.setup_in_progress = False
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json(500, {"error": str(e)})
            except Exception:
                pass


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _fs_roots() -> dict:
    """Common starting locations to seed the folder picker."""
    home = Path.home()
    roots = [{"name": "Home", "path": str(home)}]
    if sys.platform == "win32":
        # Enumerate mounted drive letters.
        import string
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:/")
            if drive.exists():
                roots.append({"name": f"{letter}:\\", "path": str(drive)})
    else:
        roots.append({"name": "Root", "path": "/"})
        for extra in ("/Volumes", "/mnt", "/media"):
            p = Path(extra)
            if p.is_dir():
                roots.append({"name": extra, "path": extra})
    return {"roots": roots, "home": str(home), "sep": os.sep}


def _render_setup_html(default_path: str) -> str:
    return _SETUP_HTML.replace("__DEFAULT_PATH__", json.dumps(default_path))


_SETUP_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Zendesk Search — Setup</title>
<style>
  :root { color-scheme: light dark; --accent:#2563eb; --border:#9994; --hover:#7771; --sub:#888; }
  body { font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         max-width: 720px; margin: 3rem auto; padding: 0 1.25rem; }
  h1 { margin: 0 0 .5rem; font-size: 1.4rem; }
  p  { margin: .5rem 0; }
  code { background: rgba(127,127,127,.15); padding: 1px 5px; border-radius: 4px; }
  .crumbs { display:flex; align-items:center; gap:6px; flex-wrap:wrap;
            background: rgba(127,127,127,.08); padding:.5rem .7rem; border-radius:6px;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px;
            word-break: break-all; }
  .crumbs button { background:none; border:0; color:var(--accent); cursor:pointer;
                   font:inherit; padding:0 2px; }
  .crumbs button:hover { text-decoration: underline; }
  .crumbs .sep { color: var(--sub); }
  .roots { display:flex; gap:6px; flex-wrap:wrap; margin: .5rem 0 .75rem; }
  .roots button { padding: .25rem .6rem; font:inherit; font-size:13px;
                  border:1px solid var(--border); border-radius:6px;
                  background: transparent; color: inherit; cursor:pointer; }
  .roots button:hover { background: var(--hover); }
  .listing { border:1px solid var(--border); border-radius:8px; max-height: 320px;
             overflow:auto; margin-top:.5rem; }
  .row { display:flex; align-items:center; gap:8px; padding:.45rem .7rem;
         border-bottom:1px solid var(--border); cursor:pointer;
         font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:13px; }
  .row:last-child { border-bottom:0; }
  .row:hover { background: var(--hover); }
  .row .icon { width:1.1em; text-align:center; }
  .row.up { color: var(--sub); }
  .row .badge { margin-left:auto; font-size:11px; padding:1px 6px; border-radius:99px;
                background: rgba(34,197,94,.15); color:#15803d; }
  .row.empty { cursor:default; color:var(--sub); justify-content:center; }
  .actions { display:flex; align-items:center; gap:.75rem; margin-top:1rem; flex-wrap:wrap; }
  button.primary {
    padding: .55rem 1.1rem; font: inherit; font-weight: 600;
    border: 0; border-radius: 6px; background: var(--accent); color: white;
    cursor: pointer;
  }
  button.primary:disabled { opacity: .6; cursor: progress; }
  .link { color: var(--sub); cursor:pointer; background:none; border:0; font:inherit; }
  .link:hover { color: var(--fg); text-decoration: underline; }
  details { margin-top: 1rem; }
  summary { cursor: pointer; color: var(--accent); }
  .note { color: var(--sub); font-size: .9em; }
  .err  { color: #c00; margin-top: .75rem; min-height: 1.4em; white-space: pre-wrap; }
  .ok   { color: #060; margin-top: .75rem; }
  input[type=text] {
    width: 100%; box-sizing: border-box; padding: .55rem .7rem;
    font: inherit; border: 1px solid var(--border); border-radius: 6px;
    background: Field; color: FieldText; margin-top:.4rem;
  }
</style>
</head>
<body>
<h1 id="title">Choose your Zendesk data folder</h1>
<p id="lede">Browse to the folder that contains your Zendesk export
(<code>tickets.ndjson</code> and friends), then click <b>Use this folder</b>.</p>

<div class="roots" id="roots"></div>
<div class="crumbs" id="crumbs"></div>
<div class="listing" id="listing"><div class="row empty">loading…</div></div>

<div class="actions">
  <button id="go" class="primary">Use this folder</button>
  <span id="here-badge" class="note"></span>
  <button id="toggle-manual" class="link" type="button">Type a path manually</button>
  <a id="cancel" href="/" style="display:none; color:var(--sub);">Cancel</a>
</div>
<div id="manual-wrap" style="display:none;">
  <input id="manual" type="text" autocomplete="off" spellcheck="false"
         placeholder="e.g. /Users/you/Zendesk/data">
  <p class="note">Press Enter to jump to that folder.</p>
</div>
<div id="msg" class="err"></div>

<details>
  <summary>What files am I looking for?</summary>
  <p>A folder counts as a Zendesk export if it contains any of:
  <code>tickets.ndjson</code>, <code>users.ndjson</code>,
  <code>organizations.ndjson</code>, <code>groups.json</code>,
  <code>comments_all.ndjson</code>, or a <code>comments/</code> subfolder of
  per-ticket JSON. Folders that look like exports are tagged
  <span class="row" style="display:inline-flex; padding:0; border:0; cursor:default;"><span class="badge">export</span></span>
  in the listing.</p>
</details>

<script>
  const DEFAULT_PATH = __DEFAULT_PATH__;
  const $ = s => document.querySelector(s);
  const esc = s => s.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

  let cwd = DEFAULT_PATH;
  let sep = "/";

  function renderCrumbs(p) {
    const parts = p.split(/[\\/]+/).filter(Boolean);
    const isWin = sep === "\\";
    const html = [];
    let acc = isWin ? "" : "";
    if (!isWin) {
      html.push(`<button data-path="/">/</button>`);
      acc = "";
    }
    parts.forEach((part, i) => {
      acc = (isWin && i === 0) ? part + sep : acc + (acc.endsWith(sep) || acc === "" ? "" : sep) + part;
      const full = isWin ? acc : "/" + parts.slice(0, i + 1).join("/");
      html.push(`<span class="sep">${esc(sep)}</span>`);
      html.push(`<button data-path="${esc(full)}">${esc(part)}</button>`);
    });
    $("#crumbs").innerHTML = html.join("");
    $("#crumbs").querySelectorAll("button").forEach(b => {
      b.addEventListener("click", () => navigate(b.dataset.path));
    });
  }

  function renderListing(data) {
    const list = $("#listing");
    list.innerHTML = "";
    if (data.parent) {
      const up = document.createElement("div");
      up.className = "row up";
      up.innerHTML = `<span class="icon">⬆</span><span>.. (parent folder)</span>`;
      up.addEventListener("click", () => navigate(data.parent));
      list.appendChild(up);
    }
    if (!data.entries.length) {
      const e = document.createElement("div");
      e.className = "row empty";
      e.textContent = "no subfolders";
      list.appendChild(e);
    }
    data.entries.forEach(en => {
      const r = document.createElement("div");
      r.className = "row";
      r.innerHTML = `<span class="icon">📁</span><span>${esc(en.name)}</span>` +
        (en.is_export ? `<span class="badge">export</span>` : "");
      r.addEventListener("click", () => navigate(en.path));
      list.appendChild(r);
    });
    $("#here-badge").innerHTML = data.is_export
      ? `<span style="color:#15803d;">✓ this folder looks like a Zendesk export</span>`
      : `<span>using current folder: <code>${esc(data.path)}</code></span>`;
  }

  async function navigate(target) {
    $("#listing").innerHTML = `<div class="row empty">loading…</div>`;
    $("#msg").textContent = "";
    try {
      const r = await fetch("/api/fs/list?path=" + encodeURIComponent(target));
      const data = await r.json();
      if (!r.ok) {
        $("#msg").className = "err";
        $("#msg").textContent = data.error || ("HTTP " + r.status);
        return;
      }
      cwd = data.path;
      sep = data.sep || sep;
      renderCrumbs(cwd);
      renderListing(data);
    } catch (e) {
      $("#msg").className = "err";
      $("#msg").textContent = "request failed: " + e;
    }
  }

  async function loadRoots() {
    try {
      const r = await fetch("/api/fs/roots");
      const data = await r.json();
      sep = data.sep || sep;
      const wrap = $("#roots");
      wrap.innerHTML = "";
      data.roots.forEach(root => {
        const b = document.createElement("button");
        b.textContent = root.name;
        b.title = root.path;
        b.addEventListener("click", () => navigate(root.path));
        wrap.appendChild(b);
      });
    } catch (_) { /* non-fatal */ }
  }

  async function submit() {
    const msg = $("#msg");
    msg.className = "err"; msg.textContent = "";
    const btn = $("#go");
    btn.disabled = true; btn.textContent = "Building index…";
    try {
      const r = await fetch("/api/setup", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({path: cwd}),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        msg.textContent = data.error || ("HTTP " + r.status);
        btn.disabled = false; btn.textContent = "Use this folder";
        return;
      }
      msg.className = "ok";
      msg.textContent = "Done. Loading the search UI…";
      setTimeout(() => { window.location.href = "/"; }, 400);
    } catch (e) {
      msg.textContent = "request failed: " + e;
      btn.disabled = false; btn.textContent = "Use this folder";
    }
  }

  $("#go").addEventListener("click", submit);

  $("#toggle-manual").addEventListener("click", () => {
    const w = $("#manual-wrap");
    const open = w.style.display === "none";
    w.style.display = open ? "block" : "none";
    if (open) { $("#manual").value = cwd; $("#manual").focus(); $("#manual").select(); }
  });
  $("#manual").addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); navigate($("#manual").value.trim()); }
  });

  fetch("/api/setup/status").then(r => r.json()).then(s => {
    if (s.ready) {
      $("#title").textContent = "Change data folder";
      $("#lede").innerHTML = "Currently using <code>" + esc(s.data_dir) +
        "</code>. Browse to a different folder to switch.";
      $("#go").textContent = "Switch to this folder";
      $("#cancel").style.display = "inline";
    }
  }).catch(() => {});

  loadRoots();
  navigate(DEFAULT_PATH);
</script>
</body>
</html>
"""


# --- frontend (single-file HTML/CSS/JS) -------------------------------------

def _render_index_html(url_template: str) -> str:
    # All assets inlined. No external CDN. Pure CommonJS-free vanilla JS.
    return _INDEX_HTML.replace("__URL_TEMPLATE__", json.dumps(url_template))


_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Zendesk Search</title>
<style>
:root {
  --bg:#fff; --fg:#202124; --sub:#5f6368; --accent:#3b82f6; --border:#dadce0;
  --hover:#f1f3f4; --selected:#e8f0fe; --selected-fg:#1a73e8;
  --pill-bg:#eef0f3; --pill-fg:#3c4043;
  --status-new:#3b82f6; --status-open:#ef4444; --status-pending:#f59e0b;
  --status-hold:#a855f7; --status-solved:#22c55e; --status-closed:#6b7280;
}
html.dark {
  --bg:#1b1d1f; --fg:#e8eaed; --sub:#9aa0a6; --accent:#3b82f6; --border:#3a3a3a;
  --hover:#26282c; --selected:#0d2c54; --selected-fg:#8ab4f8;
  --pill-bg:#2b2d31; --pill-fg:#e8eaed;
}
* { box-sizing: border-box; }
html, body { margin:0; padding:0; height:100%; background:var(--bg); color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; font-size:13px; }
body { display:flex; flex-direction:column; overflow:hidden; }
header { flex:none; display:flex; gap:8px; padding:8px; border-bottom:1px solid var(--border); align-items:center; }
header input[type=search] { flex:1; padding:8px 10px; font-size:14px; border:1px solid var(--border);
  border-radius:6px; background:var(--bg); color:var(--fg); }
header input[type=search]:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 2px rgba(59,130,246,.2); }
header label { user-select:none; color:var(--sub); display:flex; align-items:center; gap:4px; }
header a#change-folder { color:var(--sub); text-decoration:none; padding:6px 10px; border:1px solid var(--border);
  border-radius:6px; font-size:13px; }
header a#change-folder:hover { background:var(--hover); color:var(--fg); }
header button { padding:6px 10px; border:1px solid var(--border); background:var(--bg); color:var(--fg);
  border-radius:6px; cursor:pointer; }
header button:hover { background:var(--hover); }

main { flex:1; min-height:0; display:grid; grid-template-columns: 260px 1fr 1fr; overflow:hidden; }
main > * { min-height:0; min-width:0; }
#filters { padding:10px; border-right:1px solid var(--border); overflow:auto; }
#filters h3 { font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:var(--sub); margin:14px 0 6px; }
#filters h3:first-child { margin-top:0; }
#filters label { display:block; font-size:12px; color:var(--sub); margin:8px 0 2px; }
#filters select, #filters input { width:100%; padding:5px 6px; border:1px solid var(--border); border-radius:4px;
  background:var(--bg); color:var(--fg); font-size:12px; }
#filters .row2 { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
#filters .clear { margin-top:14px; width:100%; padding:6px; border:1px solid var(--border); border-radius:4px;
  background:var(--bg); color:var(--fg); cursor:pointer; }
#filters .clear:hover { background:var(--hover); }

#results { display:flex; flex-direction:column; border-right:1px solid var(--border); overflow:hidden; }
#status { flex:none; padding:6px 10px; color:var(--sub); font-size:12px; border-bottom:1px solid var(--border); }
#table-wrap { flex:1; min-height:0; overflow:auto; }
table { border-collapse:collapse; width:100%; font-size:12px; }
th { position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--border); text-align:left;
  padding:6px 8px; font-weight:600; color:var(--sub); cursor:default; user-select:none; z-index:1;
  white-space:nowrap; }
th.sortable { cursor:pointer; }
th.sortable:hover { color:var(--fg); }
th.sortable.active { color:var(--fg); }
.sort-ind { display:inline; margin-left:4px; opacity:0.7; pointer-events:none; }
td { padding:5px 8px; border-bottom:1px solid var(--border); white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; max-width:300px; }
tr.row { cursor:pointer; }
tr.row:hover td { background:var(--hover); }
tr.row.sel td { background:var(--selected); color:var(--selected-fg); }
.id { text-align:right; font-variant-numeric:tabular-nums; }
.pill { display:inline-block; padding:1px 7px; border-radius:10px; font-size:11px;
  background:var(--pill-bg); color:var(--pill-fg); }
.pill.s-new{background:var(--status-new);color:#fff;}
.pill.s-open{background:var(--status-open);color:#fff;}
.pill.s-pending{background:var(--status-pending);color:#fff;}
.pill.s-hold{background:var(--status-hold);color:#fff;}
.pill.s-solved{background:var(--status-solved);color:#fff;}
.pill.s-closed{background:var(--status-closed);color:#fff;}
mark { background: rgba(245,158,11,0.4); color:inherit; padding:0 2px; border-radius:2px;}

#detail { padding:14px 18px; overflow:auto; min-width:0; }
#detail .empty { color:var(--sub); padding:30px; text-align:center; }
#detail h1 { font-size:18px; margin:0 0 6px 0; word-break:break-word; }
#detail .meta { color:var(--sub); font-size:12px; margin-bottom:10px; }
#detail .meta a { color:var(--accent); text-decoration:none; }
#detail .meta a:hover { text-decoration:underline; }
#detail .chip { display:inline-block; background:var(--pill-bg); color:var(--pill-fg); padding:1px 7px;
  border-radius:10px; margin-right:4px; font-size:11px; }
#detail table.kv { border-collapse:collapse; margin:8px 0 14px 0; font-size:12px; }
#detail table.kv td { padding:2px 10px 2px 0; vertical-align:top; }
#detail table.kv td.k { color:var(--sub); white-space:nowrap; }
#detail .comment { border-top:1px solid var(--border); padding:10px 0; }
#detail .comment .head { color:var(--sub); font-size:11px; margin-bottom:6px; }
#detail .comment.internal { background:rgba(245,158,11,0.08); padding-left:10px; border-left:3px solid var(--status-pending); }
#detail .comment .body { white-space:pre-wrap; word-break:break-word; }
#detail .comment .body-html { word-break:break-word; }
#detail .actions { margin-top:10px; display:flex; gap:8px; }
#detail .actions button { padding:6px 10px; border:1px solid var(--border); background:var(--bg);
  color:var(--fg); border-radius:6px; cursor:pointer; }
#detail .actions button:hover { background:var(--hover); }

.kbd { font-family:ui-monospace,monospace; background:var(--pill-bg); padding:0 4px; border-radius:3px; font-size:11px; }
</style>
</head>
<body>
<header>
  <input id="q" type="search" placeholder="Search subjects, descriptions, comments…  (press / to focus)" autocomplete="off" autofocus>
  <label><input type="checkbox" id="sc-tickets" checked> tickets</label>
  <label><input type="checkbox" id="sc-comments" checked> comments</label>
  <a id="change-folder" href="/setup" title="Switch to a different data folder">Change data folder</a>
  <button id="dark">Dark</button>
</header>
<main>
  <aside id="filters">
    <h3>Filters</h3>
    <label>Status</label><select id="f-status"><option value=""></option></select>
    <label>Priority</label>
    <select id="f-priority">
      <option value=""></option><option>low</option><option>normal</option><option>high</option><option>urgent</option>
    </select>
    <label>Type</label>
    <select id="f-type">
      <option value=""></option><option>question</option><option>incident</option><option>problem</option><option>task</option>
    </select>
    <label>Tag</label>
    <input id="f-tag" list="tags-list" placeholder="any">
    <datalist id="tags-list"></datalist>
    <label>Requester (name/email)</label><input id="f-requester">
    <label>Assignee (name/email)</label><input id="f-assignee">
    <label>Organization</label><input id="f-org">
    <label>Group</label><input id="f-group">

    <h3>Created</h3>
    <div class="row2">
      <input id="f-created-after"  type="date">
      <input id="f-created-before" type="date">
    </div>
    <h3>Updated</h3>
    <div class="row2">
      <input id="f-updated-after"  type="date">
      <input id="f-updated-before" type="date">
    </div>

    <button class="clear" id="clear-filters">Clear filters</button>
    <p style="margin-top:18px;color:var(--sub);font-size:11px;line-height:1.5;">
      Shortcuts: <span class="kbd">/</span> focus search · <span class="kbd">Esc</span> clear ·
      <span class="kbd">↑/↓</span> rows · <span class="kbd">Enter</span> open
    </p>
  </aside>

  <section id="results">
    <div id="status">Ready.</div>
    <div id="table-wrap">
      <table>
        <thead>
          <tr>
            <th class="id sortable" data-sort="id">#<span class="sort-ind"></span></th>
            <th class="sortable" data-sort="status">Status<span class="sort-ind"></span></th>
            <th class="sortable" data-sort="priority">Pri<span class="sort-ind"></span></th>
            <th class="sortable" data-sort="updated">Updated<span class="sort-ind"></span></th>
            <th>Requester</th><th>Assignee</th><th>Org</th>
            <th class="sortable" data-sort="subject">Subject<span class="sort-ind"></span></th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
  </section>

  <section id="detail"><div class="empty">Select a ticket to see the full thread.</div></section>
</main>

<script>
const URL_TEMPLATE = __URL_TEMPLATE__;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const state = {
  rows: [],
  total: 0,
  selected: null,
  highlight: [],
  sort: "updated",
  order: "desc",
};

function debounce(fn, ms) {
  let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;")
    .replaceAll('"',"&quot;").replaceAll("'","&#39;");
}

function highlight(text, terms) {
  if (!terms.length) return escapeHtml(text);
  let out = escapeHtml(text);
  for (const term of terms) {
    if (!term) continue;
    const re = new RegExp("(" + term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
    out = out.replace(re, "<mark>$1</mark>");
  }
  return out;
}

function statusPill(s) {
  const slug = (s || "").toLowerCase();
  return `<span class="pill s-${slug}">${escapeHtml(s || "")}</span>`;
}

function fmtDate(s) {
  if (!s) return "";
  return s.length >= 10 ? s.slice(0, 10) : s;
}

function buildSearchParams() {
  const p = new URLSearchParams();
  p.set("q", $("#q").value.trim());
  p.set("st", $("#sc-tickets").checked ? "1" : "0");
  p.set("sc", $("#sc-comments").checked ? "1" : "0");
  for (const id of ["status","priority","type","tag","requester","assignee","org","group"]) {
    const el = document.getElementById("f-" + id);
    if (!el) continue;
    const v = (el.value || "").trim();
    if (v) p.set(id === "org" ? "organization" : id, v);
  }
  for (const id of ["created-after","created-before","updated-after","updated-before"]) {
    const v = document.getElementById("f-" + id).value;
    if (v) p.set(id.replace("-","_"), v);
  }
  p.set("limit", "300");
  p.set("sort", state.sort);
  p.set("order", state.order);
  return p;
}

function updateSortIndicators() {
  for (const th of document.querySelectorAll("th.sortable")) {
    const active = th.dataset.sort === state.sort;
    th.classList.toggle("active", active);
    const ind = th.querySelector(".sort-ind");
    if (ind) ind.textContent = active ? (state.order === "asc" ? "▲" : "▼") : "";
  }
}

async function runSearch() {
  $("#status").textContent = "Searching…";
  const p = buildSearchParams();
  // Compute highlight terms (free-text only, AND'd; phrases included)
  const text = p.get("q") || "";
  const terms = (text.match(/"[^"]+"|\S+/g) || [])
    .map(t => t.startsWith('"') && t.endsWith('"') ? t.slice(1,-1) : t)
    .filter(Boolean);
  state.highlight = terms;

  const res = await fetch("/api/search?" + p.toString());
  if (!res.ok) {
    $("#status").textContent = "Error: " + res.status;
    return;
  }
  const j = await res.json();
  state.rows = j.rows;
  state.total = j.total;
  renderRows();
  updateSortIndicators();
  if (state.total === 0) {
    $("#status").textContent = "0 matches.";
    state.selected = null;
    $("#detail").innerHTML = '<div class="empty">No matches.</div>';
  } else {
    $("#status").textContent =
      state.total > state.rows.length
        ? `Showing ${state.rows.length} of ${state.total} matches`
        : `${state.total} ${state.total === 1 ? "match" : "matches"}`;
    if (state.rows.length) selectRow(0);
  }
}

function renderRows() {
  const tbody = $("#rows");
  tbody.innerHTML = "";
  for (let i = 0; i < state.rows.length; i++) {
    const r = state.rows[i];
    const tr = document.createElement("tr");
    tr.className = "row";
    tr.dataset.idx = i;
    tr.innerHTML = `
      <td class="id">${r.id}</td>
      <td>${statusPill(r.status)}</td>
      <td>${escapeHtml(r.priority || "")}</td>
      <td>${escapeHtml(fmtDate(r.updated_at))}</td>
      <td title="${escapeHtml(r.requester_email)}">${highlight(r.requester_name || r.requester_email || "", state.highlight)}</td>
      <td>${highlight(r.assignee_name || "", state.highlight)}</td>
      <td>${highlight(r.organization_name || "", state.highlight)}</td>
      <td>${highlight(r.subject || "", state.highlight)}</td>`;
    tr.addEventListener("click", () => selectRow(i));
    tr.addEventListener("dblclick", () => window.open(URL_TEMPLATE.replace("{id}", r.id), "_blank"));
    tbody.appendChild(tr);
  }
}

async function selectRow(i) {
  if (i < 0 || i >= state.rows.length) return;
  state.selected = i;
  for (const tr of $$("tr.row")) tr.classList.remove("sel");
  const tr = document.querySelector(`tr.row[data-idx="${i}"]`);
  if (tr) {
    tr.classList.add("sel");
    tr.scrollIntoView({ block: "nearest" });
  }
  const tid = state.rows[i].id;
  const res = await fetch("/api/ticket/" + tid);
  if (!res.ok) {
    $("#detail").innerHTML = `<div class="empty">Failed to load #${tid}</div>`;
    return;
  }
  const t = await res.json();
  $("#detail").innerHTML = renderTicket(t);
}

function sanitizeHtml(s) {
  // Allowlist sanitizer in the browser. We strip script/style and any tag not in the allowlist.
  const allowed = new Set(["p","br","b","i","u","a","pre","code","ul","ol","li","blockquote","strong","em","div","span","hr","mark"]);
  const wrapper = document.createElement("div");
  // Manually clean by stripping disallowed tags. We use DOMParser, then walk and remove.
  const doc = new DOMParser().parseFromString("<div>" + s + "</div>", "text/html");
  const root = doc.body.firstChild;
  function walk(node) {
    const children = Array.from(node.childNodes);
    for (const c of children) {
      if (c.nodeType === 1) {
        const tag = c.tagName.toLowerCase();
        if (!allowed.has(tag)) {
          // Replace with its children (strip tag, keep text)
          while (c.firstChild) node.insertBefore(c.firstChild, c);
          node.removeChild(c);
          continue;
        }
        // Drop attributes except href on <a>
        for (const a of Array.from(c.attributes)) {
          if (tag === "a" && (a.name === "href" || a.name === "title")) {
            if (a.name === "href" && /^(javascript|data):/i.test(a.value)) {
              c.removeAttribute(a.name);
            }
            continue;
          }
          c.removeAttribute(a.name);
        }
        if (tag === "a") {
          c.setAttribute("target", "_blank");
          c.setAttribute("rel", "noopener noreferrer");
        }
        walk(c);
      } else if (c.nodeType !== 3) {
        node.removeChild(c);
      }
    }
  }
  walk(root);
  return root.innerHTML;
}

function renderTicket(t) {
  const url = URL_TEMPLATE.replace("{id}", t.id);
  const req = t.requester || {};
  const ass = t.assignee || {};
  const org = t.organization || {};
  const tags = (t.tags || []).map(x => `<span class="chip">${escapeHtml(x)}</span>`).join(" ");

  const cf = (t.custom_fields || []).filter(c => c.value !== null && c.value !== "" && !(Array.isArray(c.value) && !c.value.length));
  const cfRows = cf.length ? `<tr><td class="k">Custom fields</td><td>${cf.map(c => `id ${escapeHtml(c.id)} = ${escapeHtml(Array.isArray(c.value) ? c.value.join(", ") : c.value)}`).join("<br>")}</td></tr>` : "";

  const commentBlocks = [];
  if (t.description) {
    commentBlocks.push(`<div class="comment"><div class="head">initial description</div><div class="body">${highlight(t.description, state.highlight)}</div></div>`);
  }
  for (const c of (t.comments || [])) {
    const head = `${escapeHtml(fmtDate(c.created_at))} · ${escapeHtml(c.author_name || "")} &lt;${escapeHtml(c.author_email || "")}&gt;${c.public ? "" : " · internal"}`;
    const body = c.body || "";
    let rendered;
    if (/<[a-z][^>]*>/i.test(body)) {
      rendered = `<div class="body-html">${sanitizeHtml(highlight(body, state.highlight))}</div>`;
      // Note: highlight wraps text with <mark>; sanitizer keeps unknown tags' text but removes them.
      // To preserve <mark>, allow it: extend the allowlist locally.
    } else {
      rendered = `<div class="body">${highlight(body, state.highlight)}</div>`;
    }
    const cls = c.public ? "comment" : "comment internal";
    commentBlocks.push(`<div class="${cls}"><div class="head">${head}</div>${rendered}</div>`);
  }

  return `
    <h1>#${t.id} — ${highlight(t.subject || "", state.highlight)}</h1>
    <div class="meta">
      ${statusPill(t.status)} <span class="chip">${escapeHtml(t.priority || "")}</span>
      <span class="chip">${escapeHtml(t.type || "")}</span>
      created ${escapeHtml(fmtDate(t.created_at))} · updated ${escapeHtml(fmtDate(t.updated_at))} ·
      <a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">open in Zendesk</a>
    </div>
    <table class="kv">
      <tr><td class="k">Requester</td><td>${escapeHtml(req.name || "")} &lt;${escapeHtml(req.email || "")}&gt;</td></tr>
      <tr><td class="k">Assignee</td><td>${escapeHtml(ass.name || "")} &lt;${escapeHtml(ass.email || "")}&gt;</td></tr>
      <tr><td class="k">Organization</td><td>${escapeHtml(org.name || "")}</td></tr>
      <tr><td class="k">Group</td><td>${escapeHtml(t.group_name || "")}</td></tr>
      <tr><td class="k">Tags</td><td>${tags}</td></tr>
      ${cfRows}
    </table>
    <div class="actions">
      <button onclick="navigator.clipboard.writeText(${JSON.stringify(url)});this.textContent='Copied!';setTimeout(()=>this.textContent='Copy URL',1200);">Copy URL</button>
      <button onclick="exportTicketCSV(${t.id})">Download CSV</button>
      <button onclick="downloadTicketHTML(${t.id})">Download HTML</button>
    </div>
    ${commentBlocks.join("")}
  `;
}

function exportTicketCSV(tid) {
  const r = state.rows.find(x => x.id === tid);
  if (!r) return;
  const headers = ["id","status","priority","updated_at","requester","assignee","organization","subject","url"];
  const row = [
    r.id, r.status, r.priority, r.updated_at,
    r.requester_email || r.requester_name, r.assignee_name, r.organization_name,
    (r.subject || "").replace(/\n/g, " "),
    URL_TEMPLATE.replace("{id}", r.id),
  ];
  const csv = headers.join(",") + "\n" + row.map(v => `"${String(v ?? "").replaceAll('"','""')}"`).join(",") + "\n";
  downloadBlob(csv, `ticket_${tid}.csv`, "text/csv");
}

async function downloadTicketHTML(tid) {
  const detail = $("#detail").innerHTML;
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>Ticket #${tid}</title>
  <style>body{font-family:system-ui,sans-serif;font-size:13px;margin:24px;max-width:900px;}
  .pill{display:inline-block;padding:1px 7px;border-radius:10px;background:#eee;font-size:11px;}
  .chip{display:inline-block;background:#eee;padding:1px 7px;border-radius:10px;margin-right:4px;font-size:11px;}
  table.kv td{padding:2px 10px 2px 0;vertical-align:top;}
  table.kv td.k{color:#666;white-space:nowrap;}
  .comment{border-top:1px solid #ddd;padding:10px 0;}
  .comment .head{color:#666;font-size:11px;margin-bottom:6px;}
  .body{white-space:pre-wrap;word-break:break-word;}
  mark{background:#fef08a;}
  </style></head><body>${detail}</body></html>`;
  downloadBlob(html, `ticket_${tid}.html`, "text/html");
}

function downloadBlob(content, filename, mime) {
  const blob = new Blob([content], { type: mime });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

// Wire up events
const debouncedSearch = debounce(runSearch, 150);
$("#q").addEventListener("input", debouncedSearch);
for (const id of ["sc-tickets","sc-comments"]) document.getElementById(id).addEventListener("change", debouncedSearch);
for (const id of ["status","priority","type","tag","requester","assignee","org","group",
                  "created-after","created-before","updated-after","updated-before"]) {
  document.getElementById("f-" + id).addEventListener("input", debouncedSearch);
  document.getElementById("f-" + id).addEventListener("change", debouncedSearch);
}
$("#clear-filters").addEventListener("click", () => {
  for (const id of ["status","priority","type","tag","requester","assignee","org","group",
                    "created-after","created-before","updated-after","updated-before"]) {
    document.getElementById("f-" + id).value = "";
  }
  runSearch();
});

for (const th of document.querySelectorAll("th.sortable")) {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (state.sort === key) {
      state.order = state.order === "asc" ? "desc" : "asc";
    } else {
      state.sort = key;
      state.order = (key === "subject" || key === "status" || key === "priority") ? "asc" : "desc";
    }
    runSearch();
  });
}
updateSortIndicators();

// Keyboard
document.addEventListener("keydown", (e) => {
  const tag = (e.target && e.target.tagName) || "";
  if (e.key === "/" && tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") {
    e.preventDefault(); $("#q").focus(); return;
  }
  if (e.key === "Escape" && document.activeElement === $("#q")) {
    $("#q").value = ""; runSearch(); return;
  }
  if ((e.key === "ArrowDown" || e.key === "ArrowUp") && tag !== "INPUT") {
    e.preventDefault();
    const next = (state.selected ?? -1) + (e.key === "ArrowDown" ? 1 : -1);
    if (next >= 0 && next < state.rows.length) selectRow(next);
  }
  if (e.key === "Enter" && state.selected != null && tag !== "INPUT") {
    const r = state.rows[state.selected];
    if (r) window.open(URL_TEMPLATE.replace("{id}", r.id), "_blank");
  }
});

// Dark mode
function applyDark(on) {
  document.documentElement.classList.toggle("dark", on);
  localStorage.setItem("zdweb.dark", on ? "1" : "0");
  $("#dark").textContent = on ? "Light" : "Dark";
}
$("#dark").addEventListener("click", () => applyDark(!document.documentElement.classList.contains("dark")));
applyDark(localStorage.getItem("zdweb.dark") === "1");

// Initial load: populate filters then run an empty search (recent tickets)
(async () => {
  try {
    const r = await fetch("/api/meta");
    const m = await r.json();
    const sel = $("#f-status");
    for (const s of m.statuses) {
      const o = document.createElement("option"); o.value = s; o.textContent = s; sel.appendChild(o);
    }
    const dl = $("#tags-list");
    for (const t of m.tags) {
      const o = document.createElement("option"); o.value = t; dl.appendChild(o);
    }
  } catch (e) { console.warn(e); }
  runSearch();
})();
</script>
</body>
</html>"""


# --- bootstrap --------------------------------------------------------------

# Files we expect to find in a populated Zendesk export directory. If none of
# these are present, the directory is treated as empty and the user is asked
# to point us somewhere else.
_EXPECTED_EXPORT_FILES = (
    "tickets.ndjson",
    "users.ndjson",
    "organizations.ndjson",
    "groups.json",
    "comments_all.ndjson",
)


def _has_export_data(data_dir: Path) -> bool:
    """True if data_dir looks like it contains a Zendesk export."""
    if not data_dir.exists():
        return False
    if any((data_dir / name).exists() for name in _EXPECTED_EXPORT_FILES):
        return True
    # comments may be split across per-ticket files in a comments/ subdir
    comments_dir = data_dir / "comments"
    if comments_dir.is_dir() and any(comments_dir.glob("*.json")):
        return True
    return False


def _build_index_if_missing(data_dir: Path, db_path: Path) -> None:
    if db_path.exists():
        return
    print("[zdweb] no index found — building one (this is a one-time step)…", flush=True)
    indexer = THIS_DIR / "zdindex.py"
    if not indexer.exists():
        sys.exit(f"error: zdindex.py not found at {indexer}; cannot build index")
    # Import and call directly (faster than subprocess; same Python, same env).
    sys.path.insert(0, str(THIS_DIR))
    import zdindex  # type: ignore
    zdindex.build(data_dir, db_path, rebuild=False, progress=False)


def parse_args(argv):
    p = argparse.ArgumentParser(prog="zdweb", description="Local web UI for the Zendesk archive.")
    p.add_argument("--data-dir", default=None,
                   help=(f"path to the data directory (default: last folder "
                         f"picked via the setup page, otherwise {DEFAULT_DATA})"))
    p.add_argument("--db", default=None,
                   help="path to zdsearch.sqlite (default <data-dir>/zdsearch.sqlite)")
    p.add_argument("--port", type=int, default=8765, help="local port (default 8765)")
    p.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    p.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    p.add_argument("--no-update", action="store_true",
                   help="skip the GitHub update check at startup")
    p.add_argument("--url-template", default=DEFAULT_URL_TEMPLATE,
                   help="URL template for 'open in Zendesk' links")
    return p.parse_args(argv)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = parse_args(argv)

    if not args.no_update and not os.environ.get("ZDWEB_UPDATED"):
        if not auto_update_enabled(THIS_DIR):
            print(f"[zdweb] auto-update disabled via {AUTO_UPDATE_DISABLED_FILE}", flush=True)
        else:
            changed = self_update(THIS_DIR)
            if changed > 0:
                print(f"[zdweb] updated {changed} file(s) — restarting…", flush=True)
                env = {**os.environ, "ZDWEB_UPDATED": "1"}
                result = subprocess.run([sys.executable, sys.argv[0], *argv], env=env)
                return result.returncode

    if args.data_dir is not None:
        data_dir = Path(args.data_dir).expanduser().resolve()
        print(f"[zdweb] data folder from --data-dir: {data_dir}", flush=True)
    else:
        remembered = read_last_data_dir(THIS_DIR)
        if remembered is not None:
            data_dir = remembered
            print(f"[zdweb] data folder from {LAST_DATA_DIR_FILE}: {data_dir}", flush=True)
        else:
            data_dir = DEFAULT_DATA
    db_path = Path(args.db).expanduser().resolve() if args.db else None

    state = AppState(data_dir=data_dir, db_path=db_path)

    # If we already have an index, or a populated data dir, configure the
    # backend up front. Otherwise the server starts in setup mode and the
    # user picks the folder via /setup in the browser.
    effective_db = db_path or (data_dir / "zdsearch.sqlite")
    if effective_db.exists() or _has_export_data(data_dir):
        _build_index_if_missing(data_dir, effective_db)
        state.backend = Backend(effective_db)
        state.db_path = effective_db
    else:
        print(f"[zdweb] no Zendesk export files found in {data_dir} — "
              f"opening setup page in browser", flush=True)

    Handler.state = state
    Handler.url_template = args.url_template

    httpd = ThreadingServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    if state.is_ready():
        print(f"[zdweb] serving {state.db_path} at {url}")
    else:
        print(f"[zdweb] serving setup page at {url}setup")
    print(f"[zdweb] press Ctrl+C to stop")

    if not args.no_open:
        # Open in a separate thread so an unconfigured browser doesn't block startup.
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[zdweb] stopping.")
    finally:
        httpd.server_close()
        if state.backend is not None:
            state.backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
