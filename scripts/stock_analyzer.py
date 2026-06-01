#!/usr/bin/env python3
"""Analyze Pi camera frames with Gemini Vision to detect food stock levels."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from google import genai
from google.genai import types
from PIL import Image

PROMPT = """You are monitoring a food storage area. Analyze this image and respond ONLY with a JSON object in this exact format:
{
  "food_present": true or false,
  "stock_level": "empty" or "low" or "half" or "full",
  "food_items": ["item1", "item2"],
  "summary": "one sentence description"
}

Guidelines:
- food_present: true if any food is visible
- stock_level: empty=nothing there, low=less than 25% full, half=25-75% full, full=over 75% full
- food_items: list what types of food you can see (e.g. "milk", "eggs", "butter", "bread", "canned goods")
- summary: brief human-readable description for a dashboard

Respond with JSON only, no other text."""


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_latest_image(staging_dir: Path, device_id: str) -> Path | None:
    images = sorted(staging_dir.glob(f"{device_id}-*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    return images[0] if images else None


def analyze_image(client, image_path: Path) -> dict:
    img = Image.open(image_path)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[PROMPT, img],
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def post_stock_event(session: requests.Session, api_base: str, device_id: str, analysis: dict) -> None:
    stock_level = analysis.get("stock_level", "unknown")
    food_items = analysis.get("food_items", [])
    summary = analysis.get("summary", "")

    items_str = ", ".join(food_items) if food_items else "nothing visible"
    note = f"Stock: {stock_level} — {summary} (items: {items_str})"

    payload = {
        "device_id": device_id,
        "event_type": "stock_changed",
        "event_ts": iso_now(),
        "confidence": 0.85,
        "note": note,
    }
    resp = session.post(f"{api_base}/ingest/event", json=payload, timeout=10)
    resp.raise_for_status()
    print(f"[analyzer] Posted stock event: {stock_level} — {items_str}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze food stand stock with Gemini Vision.")
    parser.add_argument("--api-key", required=True, help="Google AI API key")
    parser.add_argument("--api-base", default="http://localhost:8080/api/v1")
    parser.add_argument("--device-id", default="pi-camera")
    parser.add_argument("--staging-dir", type=Path, default=Path("backend/data/staging"))
    parser.add_argument("--interval", type=int, default=300, help="Seconds between analyses")
    args = parser.parse_args()

    client = genai.Client(api_key=args.api_key)
    session = requests.Session()

    print(f"[analyzer] Starting stock analyzer, interval={args.interval}s")

    last_stock_level = None
    last_event_time = 0.0
    FORCE_EVENT_INTERVAL = 1800  # always post at least every 30 min

    while True:
        try:
            image_path = get_latest_image(args.staging_dir, args.device_id)
            if not image_path:
                print("[analyzer] No images found in staging dir")
            else:
                print(f"[analyzer] Analyzing {image_path.name}")
                analysis = analyze_image(client, image_path)
                print(f"[analyzer] Result: {analysis}")

                current_level = analysis.get("stock_level")
                time_since_event = time.time() - last_event_time
                level_changed = current_level != last_stock_level
                overdue = time_since_event >= FORCE_EVENT_INTERVAL

                if level_changed or overdue:
                    reason = "level changed" if level_changed else "periodic update"
                    print(f"[analyzer] Posting event ({reason})")
                    post_stock_event(session, args.api_base, args.device_id, analysis)
                    last_stock_level = current_level
                    last_event_time = time.time()
                else:
                    print(f"[analyzer] Stock level unchanged ({current_level}), skipping event")

        except Exception as exc:
            print(f"[analyzer] Error: {exc}")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
