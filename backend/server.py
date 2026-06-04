#!/usr/bin/env python3
"""PiVision MVP backend scaffold.

This is intentionally lightweight and uses only the Python standard library so it can
run directly on a Raspberry Pi without extra setup during early development.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT.parent / "dashboard"
DATA_DIR = ROOT / "data"
STAGING_DIR = DATA_DIR / "staging"
EVENTS_DIR = DATA_DIR / "events"
DB_PATH = DATA_DIR / "pivision.db"
SCHEMA_PATH = ROOT / "schema.sql"
DEFAULT_DEVICE_KEY = os.getenv("PIVISION_DEVICE_KEY", "dev-key")

_INGEST_WINDOW_MINUTES = 60
_INGEST_BUCKET_COUNT = 12
_INGEST_BUCKET_MINUTES = 5



def _parse_iso_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_uptime(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total = int(seconds)
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _read_uptime_seconds() -> float | None:
    uptime_path = Path("/proc/uptime")
    if not uptime_path.exists():
        return None
    try:
        with uptime_path.open() as fh:
            parts = fh.readline().split()
        return float(parts[0]) if parts else None
    except (ValueError, OSError):
        return None


def _read_memory_percent() -> float | None:
    meminfo_path = Path("/proc/meminfo")
    if not meminfo_path.exists():
        return None
    info: dict[str, int] = {}
    try:
        for line in meminfo_path.read_text().splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            info[key.strip()] = int(value.strip().split()[0])
    except ValueError:
        return None

    total = info.get("MemTotal")
    available = info.get("MemAvailable")
    if not total or not available:
        return None
    used = total - available
    return round((used / total) * 100, 1)


def _read_cpu_percent() -> float | None:
    if not hasattr(os, "getloadavg"):
        return None
    try:
        load = os.getloadavg()[0]
    except OSError:
        return None
    cpus = os.cpu_count() or 1
    return round(min(100.0, (load / cpus) * 100.0), 1)


def _read_temp_c() -> float | None:
    for zone in ("thermal_zone0", "thermal_zone1"):
        path = Path(f"/sys/class/thermal/{zone}/temp")
        if not path.exists():
            continue
        try:
            raw = int(path.read_text().strip())
        except ValueError:
            continue
        if raw > 1_000:
            return round(raw / 1_000, 1)
        return float(raw)
    return None


def _system_metrics() -> dict:
    disk_target = DATA_DIR if DATA_DIR.exists() else Path("/")
    try:
        disk_usage = shutil.disk_usage(disk_target)
        disk_remaining_gb = round(disk_usage.free / 1_073_741_824, 1)
    except OSError:
        disk_remaining_gb = 0.0

    return {
        "cpu": _read_cpu_percent(),
        "memory": _read_memory_percent(),
        "diskRemainingGb": disk_remaining_gb,
        "tempC": _read_temp_c(),
        "uptime": _format_uptime(_read_uptime_seconds()),
    }


def _collect_ingest_metrics(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT ok, latency_ms, request_ts FROM ingest_audit").fetchall()
    now = datetime.now(UTC)
    window_start = now - timedelta(minutes=_INGEST_WINDOW_MINUTES)
    bucket_start = now - timedelta(minutes=_INGEST_BUCKET_MINUTES * _INGEST_BUCKET_COUNT)
    series = [0] * _INGEST_BUCKET_COUNT

    success_total = failure_total = 0
    success_60m = failure_60m = 0
    latency_samples: list[int] = []
    for row in rows:
        ts = _parse_iso_ts(row["request_ts"])
        if not ts:
            continue

        if row["ok"]:
            success_total += 1
        else:
            failure_total += 1

        if ts >= window_start:
            if row["ok"]:
                success_60m += 1
            else:
                failure_60m += 1
            latency_samples.append(row["latency_ms"])

        if ts >= bucket_start:
            bucket_index = int((ts - bucket_start).total_seconds() // (_INGEST_BUCKET_MINUTES * 60))
            if 0 <= bucket_index < len(series):
                series[bucket_index] += 1

    avg_latency = round(sum(latency_samples) / len(latency_samples), 1) if latency_samples else 0
    return {
        "success_total": success_total,
        "failure_total": failure_total,
        "success_60m": success_60m,
        "failure_60m": failure_60m,
        "avg_latency_ms": avg_latency,
        "series": series,
    }


_TABLE_LAST_ACTIVITY_COLUMNS: dict[str, str] = {
    "captures": "MAX(received_ts)",
    "events": "MAX(event_ts)",
    "jobs": "MAX(updated_ts)",
    "devices": "MAX(last_seen)",
    "ingest_audit": "MAX(request_ts)",
}


def _table_last_activity(conn: sqlite3.Connection, table: str) -> str | None:
    column = _TABLE_LAST_ACTIVITY_COLUMNS.get(table)
    if not column:
        return None
    query = f"SELECT {column} as ts FROM {table}"
    row = conn.execute(query).fetchone()
    return row["ts"] if row and row["ts"] else None


def _collect_database_metrics(conn: sqlite3.Connection) -> dict:
    tables = ["captures", "events", "jobs", "devices", "ingest_audit"]
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()["cnt"]

    total_rows = sum(counts.values())
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    approx_per_row = (db_size / max(total_rows, 1)) if total_rows else 0

    table_details: list[dict[str, str | int]] = []
    for table in tables:
        last_write = _table_last_activity(conn, table)
        size_mb = round((approx_per_row * counts[table]) / 1_048_576, 2) if approx_per_row else 0
        table_details.append(
            {
                "name": table,
                "rows": counts[table],
                "lastWrite": last_write or "N/A",
                "size": f"{size_mb:.2f} MB",
            }
        )

    version = conn.execute("SELECT sqlite_version() as version").fetchone()["version"]
    return {
        "connected": True,
        "version": version,
        "dbSizeMb": round(db_size / 1_048_576, 2),
        "captures": counts["captures"],
        "events": counts["events"],
        "jobs": counts["jobs"],
        "devices": counts["devices"],
        "ingestAudit": counts["ingest_audit"],
        "tables": table_details,
    }


def _collect_stand_metrics(conn: sqlite3.Connection) -> dict:
    import zoneinfo
    eastern = zoneinfo.ZoneInfo("America/New_York")
    now_eastern = datetime.now(eastern)
    utc_offset_hours = int(now_eastern.utcoffset().total_seconds() / 3600)
    offset_str = f"{utc_offset_hours:+d} hours"

    today = now_eastern.date().isoformat()
    week_start = (now_eastern.date() - timedelta(days=6)).isoformat()

    interactions_today = conn.execute(
        "SELECT COUNT(*) as cnt FROM events WHERE event_type='interaction_detected' AND DATE(datetime(event_ts, ?))==?",
        (offset_str, today),
    ).fetchone()["cnt"]

    interactions_week = conn.execute(
        "SELECT COUNT(*) as cnt FROM events WHERE event_type='interaction_detected' AND DATE(datetime(event_ts, ?))>=?",
        (offset_str, week_start),
    ).fetchone()["cnt"]

    interactions_total = conn.execute(
        "SELECT COUNT(*) as cnt FROM events WHERE event_type='interaction_detected'",
    ).fetchone()["cnt"]

    last_week_end = (now_eastern.date() - timedelta(days=7)).isoformat()
    last_week_start = (now_eastern.date() - timedelta(days=13)).isoformat()
    interactions_last_week = conn.execute(
        "SELECT COUNT(*) as cnt FROM events WHERE event_type='interaction_detected' AND DATE(datetime(event_ts, ?)) BETWEEN ? AND ?",
        (offset_str, last_week_start, last_week_end),
    ).fetchone()["cnt"]

    last_stock = conn.execute(
        "SELECT event_ts, note FROM events WHERE event_type='stock_changed' AND device_id='freestand-cam' ORDER BY event_ts DESC LIMIT 1",
    ).fetchone()

    last_interaction = conn.execute(
        "SELECT event_ts FROM events WHERE event_type='interaction_detected' ORDER BY event_ts DESC LIMIT 1",
    ).fetchone()

    daily_rows = conn.execute(
        """
        SELECT
            DATE(datetime(event_ts, ?)) as day,
            SUM(CASE WHEN CAST(strftime('%H', datetime(event_ts, ?)) AS INTEGER) < 6 THEN 1 ELSE 0 END) as night,
            SUM(CASE WHEN CAST(strftime('%H', datetime(event_ts, ?)) AS INTEGER) BETWEEN 6 AND 11 THEN 1 ELSE 0 END) as morning,
            SUM(CASE WHEN CAST(strftime('%H', datetime(event_ts, ?)) AS INTEGER) BETWEEN 12 AND 17 THEN 1 ELSE 0 END) as afternoon,
            SUM(CASE WHEN CAST(strftime('%H', datetime(event_ts, ?)) AS INTEGER) >= 18 THEN 1 ELSE 0 END) as evening,
            COUNT(*) as count
        FROM events
        WHERE event_type='interaction_detected' AND DATE(datetime(event_ts, ?)) >= ?
        GROUP BY day ORDER BY day ASC
        """,
        (offset_str, offset_str, offset_str, offset_str, offset_str, offset_str, week_start),
    ).fetchall()

    hourly_rows = conn.execute(
        """
        SELECT CAST(strftime('%H', datetime(event_ts, ?)) AS INTEGER) as hour, COUNT(*) as count
        FROM events
        WHERE event_type='interaction_detected' AND DATE(datetime(event_ts, ?))=?
        GROUP BY hour ORDER BY hour ASC
        """,
        (offset_str, offset_str, today),
    ).fetchall()
    hourly = {row["hour"]: row["count"] for row in hourly_rows}
    hourly_series = [{"hour": h, "count": hourly.get(h, 0)} for h in range(24)]

    return {
        "interactions_today": interactions_today,
        "interactions_week": interactions_week,
        "interactions_last_week": interactions_last_week,
        "interactions_total": interactions_total,
        "last_stock_change": dict(last_stock) if last_stock else None,
        "last_interaction": last_interaction["event_ts"] if last_interaction else None,
        "daily_interactions": [{"day": r["day"], "count": r["count"], "night": r["night"], "morning": r["morning"], "afternoon": r["afternoon"], "evening": r["evening"]} for r in daily_rows],
        "hourly_today": hourly_series,
    }


def _collect_captures_daily(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    rows = conn.execute(
        """
        SELECT DATE(received_ts) as day, COUNT(*) as count
        FROM captures
        WHERE received_ts >= DATE('now', ?)
        GROUP BY day
        ORDER BY day ASC
        """,
        (f"-{days} days",),
    ).fetchall()
    return [{"day": row["day"], "count": row["count"]} for row in rows]


def _directory_status(name: str, path: Path) -> dict[str, str | bool]:
    exists = path.exists()
    writable = exists and os.access(path, os.W_OK)
    return {"name": name, "path": str(path), "exists": exists, "writable": writable}


def _collect_system_health_records(conn: sqlite3.Connection) -> list[dict[str, str | dict | None]]:
    rows = conn.execute("SELECT name, last_success, last_error, details FROM system_health").fetchall()
    records: list[dict[str, str | dict | None]] = []
    for row in rows:
        details_value = row["details"]
        parsed_details = None
        if details_value:
            try:
                parsed_details = json.loads(details_value)
            except json.JSONDecodeError:
                parsed_details = details_value
        records.append(
            {
                "name": row["name"],
                "lastSuccess": row["last_success"],
                "lastError": row["last_error"],
                "details": parsed_details,
            }
        )
    return records


def record_system_health(
    name: str, success: bool, details: dict | None = None, error: str | None = None
) -> None:
    timestamp = now_iso()
    detail_payload = json.dumps(details) if details is not None else None
    success_ts = timestamp if success else None
    error_text = f"{timestamp} {error}" if error else None
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO system_health (name, last_success, last_error, details)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              last_success = COALESCE(excluded.last_success, system_health.last_success),
              last_error = CASE
                WHEN excluded.last_error IS NOT NULL THEN excluded.last_error
                ELSE system_health.last_error
              END,
              details = COALESCE(excluded.details, system_health.details)
            """,
            (name, success_ts, error_text, detail_payload),
        )

def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as conn:
        conn.executescript(SCHEMA_PATH.read_text())


def require_fields(payload: dict, fields: list[str]) -> tuple[bool, str]:
    for field in fields:
        if field not in payload:
            return False, f"missing required field: {field}"
    return True, ""


def parse_int_field(payload: dict, field: str) -> tuple[bool, int | None, str]:
    try:
        return True, int(payload[field]), ""
    except (TypeError, ValueError):
        return False, None, f"invalid integer field: {field}"


class PiVisionHandler(BaseHTTPRequestHandler):
    server_version = "PiVisionHTTP/0.1"

    def log_message(self, fmt: str, *args) -> None:
        # keep default behavior but with compact tag
        super().log_message(f"[pivision] {fmt}", *args)

    def _set_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-DEVICE-KEY")

    def _json(self, code: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self._set_cors_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _record_ingest_audit(self, endpoint: str, ok: bool, latency_ms: int) -> None:
        with connect_db() as conn:
            conn.execute(
                "INSERT INTO ingest_audit (request_ts, endpoint, ok, latency_ms) VALUES (?, ?, ?, ?)",
                (now_iso(), endpoint, int(ok), latency_ms),
            )

    def _assert_device_key(self) -> tuple[bool, str]:
        key = self.headers.get("X-DEVICE-KEY", "")
        if key != DEFAULT_DEVICE_KEY:
            return False, "invalid device key"
        return True, ""


    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._handle_health()
            return
        if parsed.path == "/api/v1/device/config":
            self._handle_device_config(parsed)
            return
        if parsed.path == "/api/v1/admin/events":
            self._handle_admin_events(parsed)
            return
        if parsed.path == "/api/v1/admin/captures":
            self._handle_admin_captures(parsed)
            return
        if parsed.path == "/api/v1/admin/devices":
            self._handle_admin_devices()
            return
        if parsed.path.startswith("/api/v1/admin/metrics/"):
            self._handle_admin_metrics(parsed.path)
            return
        if parsed.path.startswith("/static/"):
            self._handle_static(parsed.path)
            return
        if parsed.path in ("/", "/app.js", "/styles.css"):
            self._handle_dashboard(parsed.path)
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/v1/ingest/frame":
            self._handle_ingest_frame(datetime.now(UTC))
            return
        if parsed.path == "/api/v1/ingest/heartbeat":
            self._handle_heartbeat()
            return
        if parsed.path == "/api/v1/ingest/event":
            self._handle_ingest_event()
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._set_cors_headers()
        self.end_headers()
        return

    def _handle_ingest_frame(self, started: datetime) -> None:
        def fail(code: HTTPStatus, error: str) -> None:
            latency_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
            self._record_ingest_audit("/api/v1/ingest/frame", False, latency_ms)
            self._json(code, {"ok": False, "error": error})

        authed, msg = self._assert_device_key()
        if not authed:
            fail(HTTPStatus.UNAUTHORIZED, msg)
            return

        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            fail(HTTPStatus.BAD_REQUEST, "invalid json")
            return

        required = ["device_id", "capture_ts", "seq", "width", "height", "jpeg_quality"]
        valid, error = require_fields(payload, required)
        if not valid:
            fail(HTTPStatus.BAD_REQUEST, error)
            return

        device_id = payload["device_id"]
        capture_ts = payload["capture_ts"]
        ok, seq, error = parse_int_field(payload, "seq")
        if not ok:
            fail(HTTPStatus.BAD_REQUEST, error)
            return

        parsed_fields: dict[str, int] = {}
        for field in ["width", "height", "jpeg_quality"]:
            ok, value, error = parse_int_field(payload, field)
            if not ok:
                fail(HTTPStatus.BAD_REQUEST, error)
                return
            parsed_fields[field] = value

        image_b64 = payload.get("image_b64")
        image_path = None

        if image_b64:
            try:
                image_bytes = base64.b64decode(image_b64, validate=True)
            except (binascii.Error, ValueError):
                fail(HTTPStatus.BAD_REQUEST, "invalid image_b64")
                return
            image_name = f"{device_id}-{seq}.jpg"
            image_path = STAGING_DIR / image_name
            image_path.write_bytes(image_bytes)

        received_ts = now_iso()
        duplicate_seq = False
        capture_id = None
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO devices (device_id, device_key, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET last_seen=excluded.last_seen
                """,
                (device_id, DEFAULT_DEVICE_KEY, received_ts),
            )
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO captures (device_id, capture_ts, received_ts, seq, width, height, jpeg_quality, storage_uri)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device_id,
                        capture_ts,
                        received_ts,
                        seq,
                        parsed_fields["width"],
                        parsed_fields["height"],
                        parsed_fields["jpeg_quality"],
                        str(image_path) if image_path else None,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                duplicate_seq = True
            else:
                capture_id = int(cursor.lastrowid)
                conn.execute(
                    "INSERT INTO jobs (capture_id, status, created_ts, updated_ts) VALUES (?, 'queued', ?, ?)",
                    (capture_id, received_ts, received_ts),
                )

        if duplicate_seq:
            fail(HTTPStatus.CONFLICT, "duplicate device seq")
            return

        latency_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        self._record_ingest_audit("/api/v1/ingest/frame", True, latency_ms)
        self._json(HTTPStatus.OK, {"ok": True, "frame_id": capture_id, "received_ts": received_ts})

    def _handle_heartbeat(self) -> None:
        authed, msg = self._assert_device_key()
        if not authed:
            self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": msg})
            return

        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid json"})
            return

        valid, error = require_fields(payload, ["device_id"])
        if not valid:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
            return

        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO devices (device_id, device_key, last_seen, rssi, battery_mv, fw_version)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                  last_seen=excluded.last_seen,
                  rssi=excluded.rssi,
                  battery_mv=excluded.battery_mv,
                  fw_version=excluded.fw_version
                """,
                (
                    payload["device_id"],
                    DEFAULT_DEVICE_KEY,
                    now_iso(),
                    payload.get("rssi"),
                    payload.get("battery_mv"),
                    payload.get("fw_version"),
                ),
            )

        self._json(HTTPStatus.OK, {"ok": True, "last_seen": now_iso()})

    def _handle_ingest_event(self) -> None:
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid json"})
            return

        required = ["device_id", "event_type", "event_ts"]
        valid, error = require_fields(payload, required)
        if not valid:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
            return

        with connect_db() as conn:
            cursor = conn.execute(
                "INSERT INTO events (device_id, event_type, event_ts, confidence, note) VALUES (?, ?, ?, ?, ?)",
                (
                    payload["device_id"],
                    payload["event_type"],
                    payload["event_ts"],
                    payload.get("confidence"),
                    payload.get("note"),
                ),
            )
            event_id = cursor.lastrowid

        self._json(HTTPStatus.OK, {"ok": True, "event_id": event_id})

    def _handle_device_config(self, parsed) -> None:
        params = parse_qs(parsed.query)
        device_id = params.get("device_id", [None])[0]
        if not device_id:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "device_id query param required"})
            return

        with connect_db() as conn:
            row = conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()

        config = {
            "capture_interval_s": row["capture_interval_s"] if row else 30,
            "burst_fps": row["burst_fps"] if row else 2,
            "burst_duration_s": row["burst_duration_s"] if row else 15,
            "burst_cooldown_s": row["burst_cooldown_s"] if row else 60,
            "interaction_threshold": row["interaction_threshold"] if row else 0.3,
            "interaction_min_frames": row["interaction_min_frames"] if row else 3,
            "interaction_end_timeout_s": row["interaction_end_timeout_s"] if row else 3,
        }
        self._json(HTTPStatus.OK, {"ok": True, "device_id": device_id, "config": config})

    def _handle_admin_events(self, parsed) -> None:
        params = parse_qs(parsed.query)
        try:
            limit = int(params.get("limit", [20])[0])
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "limit must be an integer"})
            return

        if limit < 1:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "limit must be positive"})
            return

        device_id = params.get("device_id", [None])[0]
        event_type = params.get("event_type", [None])[0]

        filters = []
        args = []
        if device_id:
            filters.append("e.device_id = ?")
            args.append(device_id)
        if event_type:
            filters.append("e.event_type = ?")
            args.append(event_type)
        where = ("WHERE " + " AND ".join(filters)) if filters else ""

        with connect_db() as conn:
            rows = conn.execute(
                f"""
                SELECT e.id, e.device_id, e.event_type, e.event_ts, e.note, e.confidence,
                       c.storage_uri, c.width, c.height, c.capture_ts
                FROM events e
                LEFT JOIN captures c ON c.id = e.capture_id
                {where}
                ORDER BY e.event_ts DESC
                LIMIT ?
                """,
                (*args, limit),
            ).fetchall()

        events = []
        for row in rows:
            event_dict = dict(row)
            # Add derived fields for better dashboard display
            event_dict["has_image"] = bool(event_dict["storage_uri"])
            event_dict["resolution"] = f"{event_dict['width']}x{event_dict['height']}" if event_dict['width'] and event_dict['height'] else None
            event_dict["age_minutes"] = _calculate_minutes_since(event_dict["event_ts"])
            events.append(event_dict)
        
        # Debug logging
        print(f"Events API: Returning {len(events)} events, limit={limit}")
        if events:
            print(f"Latest event: {events[0]['event_ts']} - {events[0]['event_type']}")
        
        self._json(HTTPStatus.OK, {"ok": True, "events": events})

    def _handle_admin_captures(self, parsed) -> None:
        params = parse_qs(parsed.query)
        try:
            limit = int(params.get("limit", [5])[0])
        except ValueError:
            limit = 5
        device_id = params.get("device_id", [None])[0]
        with connect_db() as conn:
            if device_id:
                rows = conn.execute(
                    """
                    SELECT id, device_id, capture_ts, received_ts, width, height, storage_uri
                    FROM captures
                    WHERE storage_uri IS NOT NULL AND device_id = ?
                    ORDER BY received_ts DESC
                    LIMIT ?
                    """,
                    (device_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, device_id, capture_ts, received_ts, width, height, storage_uri
                    FROM captures
                    WHERE storage_uri IS NOT NULL
                    ORDER BY received_ts DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        captures = []
        for row in rows:
            cap = dict(row)
            if cap["storage_uri"]:
                try:
                    rel = Path(cap["storage_uri"]).relative_to(DATA_DIR)
                    cap["static_url"] = f"/static/{rel}"
                except ValueError:
                    cap["static_url"] = None
            captures.append(cap)
        self._json(HTTPStatus.OK, {"ok": True, "captures": captures})

    def _handle_admin_devices(self) -> None:
        with connect_db() as conn:
            rows = conn.execute(
                "SELECT device_id, last_seen, rssi, battery_mv, fw_version FROM devices ORDER BY device_id"
            ).fetchall()
        self._json(HTTPStatus.OK, {"ok": True, "devices": [dict(row) for row in rows]})

    def _handle_admin_metrics(self, path: str) -> None:
        metric_type = path.split("/")[-1]
        with connect_db() as conn:
            if metric_type == "ingest":
                ingest_data = _collect_ingest_metrics(conn)
                self._json(HTTPStatus.OK, {"ok": True, **ingest_data})
                return

            if metric_type == "queue":
                status_rows = conn.execute("SELECT status, COUNT(*) count FROM jobs GROUP BY status").fetchall()
                metrics = {row["status"]: row["count"] for row in status_rows}
                depth = sum(metrics.get(status, 0) for status in ("queued", "running", "failed", "dead"))
                self._json(HTTPStatus.OK, {"ok": True, "queue": metrics, "depth": depth})
                return

            if metric_type == "database":
                db_metrics = _collect_database_metrics(conn)
                self._json(HTTPStatus.OK, {"ok": True, **db_metrics})
                return

            if metric_type == "system":
                self._json(HTTPStatus.OK, {"ok": True, **_system_metrics()})
                return

            if metric_type == "captures_daily":
                daily = _collect_captures_daily(conn)
                self._json(HTTPStatus.OK, {"ok": True, "days": daily})
                return

            if metric_type == "stand":
                self._json(HTTPStatus.OK, {"ok": True, **_collect_stand_metrics(conn)})
                return

        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "unknown metrics group"})

    def _handle_health(self) -> None:
        directories = [
            _directory_status("data", DATA_DIR),
            _directory_status("staging", STAGING_DIR),
            _directory_status("events", EVENTS_DIR),
            _directory_status("db", DB_PATH),
        ]
        db_ok = True
        db_error = None
        records: list[dict[str, str | dict | None]] = []
        try:
            with connect_db() as conn:
                conn.execute("SELECT 1")
                records = _collect_system_health_records(conn)
        except sqlite3.Error as exc:
            db_ok = False
            db_error = str(exc)

        system_metrics = _system_metrics()
        ok = db_ok and all(directory["exists"] and directory["writable"] for directory in directories)
        response = {
            "ok": ok,
            "timestamp": now_iso(),
            "db": {"connected": db_ok, "path": str(DB_PATH), "error": db_error},
            "directories": directories,
            "system": system_metrics,
            "background": records,
        }
        status = HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE
        self._json(status, response)

    def _handle_static(self, path: str) -> None:
        try:
            # Remove /static/ prefix and make it relative to DATA_DIR
            relative_path = path[8:]  # Remove "/static/" prefix
            file_path = DATA_DIR / relative_path
            
            # Security check - ensure path is within DATA_DIR
            if not str(file_path).startswith(str(DATA_DIR)):
                self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "invalid path"})
                return
                
            if not file_path.exists() or not file_path.is_file():
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "file not found"})
                return
                
            # Serve the file
            with open(file_path, "rb") as f:
                content = f.read()
                
            content_type = "image/jpeg" if file_path.suffix.lower() == ".jpg" else "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"static serve error: {exc}"})


    def _handle_dashboard(self, path: str) -> None:
        filename_map = {
            "/": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
        }
        content_type_map = {
            "index.html": "text/html; charset=utf-8",
            "app.js": "application/javascript; charset=utf-8",
            "styles.css": "text/css; charset=utf-8",
        }
        filename = filename_map[path]
        file_path = DASHBOARD_DIR / filename
        if not file_path.exists():
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "dashboard file not found"})
            return
        content = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type_map[filename])
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


def _calculate_minutes_since(iso_timestamp: str) -> int:
    try:
        ts = _parse_iso_ts(iso_timestamp)
        if ts:
            delta = datetime.now(UTC) - ts
            return int(delta.total_seconds() / 60)
    except Exception:
        pass
    return 0

def serve(port: int = 8080) -> None:
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", port), PiVisionHandler)
    print(f"PiVision backend listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    serve(int(os.getenv("PORT", "8080")))
