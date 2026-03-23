#!/usr/bin/env python3
"""
Seed the PiVision database with realistic FreeStand activity data.

Models 14 days of usage based on actual stand patterns:
  - ~100 interactions/day, 7 days/week
  - Peak 1: 12am–3am  (night crowd)
  - Peak 2: 7am–10am  (morning)
  - Peak 3: 4pm–6pm   (afternoon)
  - Steady low traffic the rest of the day

Also inserts stock_changed events (restocks and empties) and a device.
"""

import random
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "backend" / "data" / "pivision.db"

DEVICE_ID = "freestand-cam-01"
DEVICE_KEY = "dev-key"

# Hourly weight table — relative probability of an interaction each hour.
# Peaks: 12am-3am, 7am-10am, 4pm-6pm
HOURLY_WEIGHTS = {
    0:  8,   # 12am
    1:  9,   # 1am
    2:  8,   # 2am
    3:  3,   # 3am
    4:  2,   # 4am
    5:  2,   # 5am
    6:  3,   # 6am
    7: 10,   # 7am  ← morning peak
    8: 14,   # 8am
    9: 11,   # 9am
    10: 5,   # 10am
    11: 4,   # 11am
    12: 4,   # 12pm
    13: 4,   # 1pm
    14: 4,   # 2pm
    15: 3,   # 3pm
    16: 9,   # 4pm  ← afternoon peak
    17: 8,   # 5pm
    18: 4,   # 6pm
    19: 3,   # 7pm
    20: 3,   # 8pm
    21: 3,   # 9pm
    22: 4,   # 10pm
    23: 6,   # 11pm
}

RESTOCK_NOTES = [
    "Restocked — added bread and canned goods",
    "Restocked — filled with produce",
    "Restocked — added hygiene items and snacks",
    "Refilled stand",
    "Restocked by volunteer",
]

EMPTY_NOTES = [
    "Stand appears empty",
    "Low inventory detected",
    "Stand nearly empty",
]


def weighted_minute(hour: int) -> int:
    """Random minute within an hour, slightly biased toward the top of the hour."""
    return random.randint(0, 59)


def generate_timestamps(day: datetime, target_count: int) -> list[datetime]:
    """Generate target_count timestamps for a given day using hourly weights."""
    hours = list(HOURLY_WEIGHTS.keys())
    weights = list(HOURLY_WEIGHTS.values())
    timestamps = []
    for _ in range(target_count):
        hour = random.choices(hours, weights=weights)[0]
        minute = weighted_minute(hour)
        second = random.randint(0, 59)
        ts = day.replace(hour=hour, minute=minute, second=second, microsecond=0, tzinfo=UTC)
        timestamps.append(ts)
    return sorted(timestamps)


def seed(days: int = 14, interactions_per_day: int = 100) -> None:
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}")
        print("Run the backend first to initialise the DB, then re-run this script.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Insert device
    now_iso = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO devices (device_id, device_key, last_seen, fw_version)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET last_seen=excluded.last_seen
        """,
        (DEVICE_ID, DEVICE_KEY, now_iso, "seed-v1.0"),
    )

    seq = 1
    total_interactions = 0
    total_stock_events = 0

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    for day_offset in range(days - 1, -1, -1):
        day = today - timedelta(days=day_offset)

        # Vary count slightly day to day (±15%)
        jitter = random.randint(-15, 15)
        count = max(60, interactions_per_day + jitter)
        timestamps = generate_timestamps(day, count)

        for ts in timestamps:
            ts_iso = ts.isoformat()
            conn.execute(
                """
                INSERT INTO captures
                  (device_id, capture_ts, received_ts, seq, width, height, jpeg_quality, processing_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'done')
                """,
                (DEVICE_ID, ts_iso, ts_iso, seq, 640, 480, 85),
            )
            capture_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            confidence = round(random.uniform(0.65, 0.99), 2)
            conn.execute(
                """
                INSERT INTO events (capture_id, device_id, event_type, event_ts, confidence, note)
                VALUES (?, ?, 'interaction_detected', ?, ?, ?)
                """,
                (capture_id, DEVICE_ID, ts_iso, confidence, "Person visited the stand"),
            )

            conn.execute(
                """
                INSERT INTO jobs (capture_id, status, attempts, created_ts, updated_ts)
                VALUES (?, 'done', 1, ?, ?)
                """,
                (capture_id, ts_iso, ts_iso),
            )

            seq += 1
            total_interactions += 1

        # Add stock_changed events:
        # - Morning restock ~8am most days
        # - Occasional empty detection mid-morning or afternoon
        restock_ts = day.replace(hour=random.randint(7, 9), minute=random.randint(0, 30), tzinfo=UTC)
        conn.execute(
            """
            INSERT INTO captures
              (device_id, capture_ts, received_ts, seq, width, height, jpeg_quality, processing_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'done')
            """,
            (DEVICE_ID, restock_ts.isoformat(), restock_ts.isoformat(), seq, 640, 480, 85),
        )
        restock_capture_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """
            INSERT INTO events (capture_id, device_id, event_type, event_ts, confidence, note)
            VALUES (?, ?, 'stock_changed', ?, ?, ?)
            """,
            (restock_capture_id, DEVICE_ID, restock_ts.isoformat(), 0.92, random.choice(RESTOCK_NOTES)),
        )
        seq += 1
        total_stock_events += 1

        # ~40% chance of an empty detection later in the day
        if random.random() < 0.4:
            empty_ts = day.replace(hour=random.randint(11, 15), minute=random.randint(0, 59), tzinfo=UTC)
            conn.execute(
                """
                INSERT INTO captures
                  (device_id, capture_ts, received_ts, seq, width, height, jpeg_quality, processing_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'done')
                """,
                (DEVICE_ID, empty_ts.isoformat(), empty_ts.isoformat(), seq, 640, 480, 85),
            )
            empty_capture_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """
                INSERT INTO events (capture_id, device_id, event_type, event_ts, confidence, note)
                VALUES (?, ?, 'stock_changed', ?, ?, ?)
                """,
                (empty_capture_id, DEVICE_ID, empty_ts.isoformat(), 0.81, random.choice(EMPTY_NOTES)),
            )
            seq += 1
            total_stock_events += 1

    conn.commit()
    conn.close()
    print(f"Seeded {days} days of data:")
    print(f"  {total_interactions} interaction events")
    print(f"  {total_stock_events} stock change events")
    print(f"  {seq - 1} total captures")
    print(f"  Device: {DEVICE_ID}")


if __name__ == "__main__":
    random.seed(42)  # reproducible seed
    seed()
