Drop your Zendesk export files here.

Expected files (NDJSON exported from Zendesk):
  - tickets.ndjson
  - users.ndjson
  - organizations.ndjson
  - groups.json
  - comments_all.ndjson    (or a comments/ subdirectory of per-ticket JSON files)

The first time you run the app, an FTS5 search index (zdsearch.sqlite) will be
built from these files. That index is rebuilt automatically when the source
files change.
