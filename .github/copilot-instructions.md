# PiVision AI Coding Agent Instructions

## Project Context

**PiVision** is a Raspberry Pi–hosted event detection gateway for a food stand. It receives JPEG frames from an ESP32 camera, detects interactions/inventory changes, stores event frames for 7 days, and provides a local admin dashboard.

**Key constraint:** MVP runs on minimal Pi hardware with stdlib-only Python backend (no heavy dependencies in core server).

## Architecture

### Three main components:

1. **Backend (`backend/server.py`)**: Stateless ThreadingHTTPServer (stdlib only)
   - `POST /api/v1/ingest/frame` — accepts JPEG + metadata from ESP32, writes staging file, enqueues job
   - `/api/v1/admin/*` — read-only admin APIs (events, devices, metrics)
   - `/api/v1/health` — system health + disk/db/directory status
   - Uses `sqlite3.Row` factories for dict-like db results; all paths relative to `DATA_DIR`

2. **Worker (`backend/worker.py`)**: DB-backed job processor
   - Polls `jobs` table for queued captures (2-second loop)
   - Calls `process_capture()` → creates event record → promotes staging frame to events dir
   - Logs job success/failure to `system_health` table for health dashboard

3. **Dashboard (`dashboard/app.js`)**: Fetch-based JS UI
   - Refreshes every 5 seconds from `http://localhost:8080/api/v1/*`
   - Shows ingest/queue/system metrics + event timeline

### Data directories:
- `DATA_DIR/staging/<device_id>/<YYYY-MM-DD>/<capture_id>.jpg` — ephemeral frames
- `DATA_DIR/events/<device_id>/<YYYY-MM-DD>/<event_id>/pre.jpg` — retained event frames
- `pivision.db` — SQLite with captures, jobs, events, devices, ingest_audit, system_health tables

## Critical patterns

### Time handling:
- Always use `UTC` timezone, ISO 8601 format (`datetime.now(UTC).isoformat()`)
- `_parse_iso_ts()` converts ISO strings to aware datetimes; validate with it

### DB access:
- Use `connect_db()` context manager (auto-commits, handles errors gracefully)
- Queries return `sqlite3.Row` objects → access as dicts (`row["column_name"]`)
- Use positional `?` placeholders in prepared statements for security
- Call `init_db()` once per process from `schema.sql`

### File paths:
- Use `Path` objects; always `Path(__file__).resolve().parent / "relative/path"`
- `storage_uri` field is a file path string (not URI despite name)
- Security: validate `file_path` is within `DATA_DIR` before serving (`str(file_path).startswith(str(DATA_DIR))`)

### Error handling:
- Record errors to `system_health` table via `record_system_health()` for visibility
- Jobs can fail gracefully: store `last_error`, mark status `'failed'` (retry later in Phase 2)
- Admin APIs return `{"ok": bool, ...}` JSON response structure

### Event model:
- Single event type for MVP: `interaction_detected`
- Event record references a capture; can include `note` (e.g., "auto interaction_detected seq=X")
- Phase 2: confidence scores + richer tuning metadata in `details` JSONB field

## Developer workflows

### Setup and run:
```bash
make setup           # Creates venv + installs requirements.txt (pillow, requests, opencv)
make run-server      # Starts backend (port 8080)
make run-worker      # Starts job processor (polls DB)
make run-retention   # Cleanup job (Phase 2)
make check           # Backend health check
```

### Key test file:
- `backend/tests/test_server.py` — HTTP handler tests + ingest flow

### Adding a new admin endpoint:
1. Add `_handle_<name>()` method to `PiVisionHandler`
2. Add route mapping in `do_GET()` path dispatch
3. Return `self._json(HTTPStatus.OK, {"ok": True, ...})`
4. Update dashboard `app.js` fetch + UI render

### Adding a new job type (Phase 2):
1. Define new `EVENT_TYPE_*` constant + processing logic in `worker.py`
2. Extend `process_capture()` to choose logic based on capture metadata
3. Add job retry/backoff to claim loop (currently just `POLL_S=2`)

## Known design stubs (Phase 2+)

- **Auth**: Currently basic device-key check; HMAC/replay protection deferred
- **Queue retry**: Fixed polling; no exponential backoff yet
- **Event dedup**: Single job per capture; merge windows TODO
- **Storage**: Filesystem-only; S3-like abstraction deferred
- **Retention cleanup**: `retention.py` stub exists; full idempotent sweep not yet implemented

## Common pit falls

- **Don't hardcode paths**: Use `Path(__file__).resolve().parent / DATA_DIR`
- **Don't forget UTC**: Naive datetimes become platform-dependent; always use `UTC`
- **Don't skip DB commits**: Context managers auto-commit; direct `conn.execute()` outside them may not persist
- **Don't assume files exist**: Always check `path.exists()` before `open()` or `move()`
- **Dashboard 5s refresh lag**: Changes to DB take up to 5 seconds to appear in UI

## Reference implementations in codebase

- **Health checks**: `backend/server.py` lines ~630 — disk/db/directory validation pattern
- **Metrics aggregation**: `backend/server.py` lines ~136 — ingest metric bucketing (60m window, 5m buckets)
- **Job processing**: `backend/worker.py` lines ~62 — claim-process-record pattern
- **Static file serving**: `backend/server.py` lines ~665 — path traversal validation + MIME type selection
