# Agent instructions

**Read `STRUCTURE.md` first.** It maps every module, table, route, page and data path, so you don't need to
crawl the repo. When you add, move or rename any of those, update `STRUCTURE.md` in the same change.

Hard rules:
- Never print, log, return to the UI, or persist anything from `.env.live` (private key, builder secrets)
  or the `POLY_API_*` values in `.env`. Only `backend/trading/gateway.py` reads them, via `config.live_env()`.
- Never place a live order, arm trading, or cancel live orders unless the user explicitly asks in this session.
- Paths and URLs come from `backend/config.py`; schema changes go in `backend/db.py:SCHEMA`.
- Windows Application Control blocks unsigned native binaries on this PC: no `pyarrow.dataset`, and the UI
  builds with esbuild-wasm. Test-import any new native dependency before relying on it (STRUCTURE.md §2).
- Rebuild the UI after frontend edits: `cd frontend && ..\.tools\node\npm.cmd run build`.
- `archive/` holds the old v1 app and raw files. Treat it as read-only history.
