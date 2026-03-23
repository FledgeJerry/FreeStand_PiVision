PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY,
  device_key TEXT,
  last_seen TEXT,
  rssi INTEGER,
  battery_mv INTEGER,
  fw_version TEXT,
  capture_interval_s INTEGER DEFAULT 30,
  burst_fps INTEGER DEFAULT 2,
  burst_duration_s INTEGER DEFAULT 15,
  burst_cooldown_s INTEGER DEFAULT 60,
  interaction_threshold REAL DEFAULT 0.3,
  interaction_min_frames INTEGER DEFAULT 3,
  interaction_end_timeout_s INTEGER DEFAULT 3
);

CREATE TABLE IF NOT EXISTS captures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  capture_ts TEXT NOT NULL,
  received_ts TEXT NOT NULL,
  seq INTEGER NOT NULL,
  width INTEGER,
  height INTEGER,
  jpeg_quality INTEGER,
  storage_uri TEXT,
  processing_status TEXT NOT NULL DEFAULT 'queued',
  FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_captures_device_seq ON captures(device_id, seq);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  capture_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_ts TEXT NOT NULL,
  updated_ts TEXT NOT NULL,
  FOREIGN KEY (capture_id) REFERENCES captures(id)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  capture_id INTEGER,
  device_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_ts TEXT NOT NULL,
  confidence REAL,
  note TEXT,
  FOREIGN KEY (capture_id) REFERENCES captures(id),
  FOREIGN KEY (device_id) REFERENCES devices(device_id)
);

CREATE TABLE IF NOT EXISTS ingest_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_ts TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  ok INTEGER NOT NULL,
  latency_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS system_health (
  name TEXT PRIMARY KEY,
  last_success TEXT,
  last_error TEXT,
  details TEXT
);
