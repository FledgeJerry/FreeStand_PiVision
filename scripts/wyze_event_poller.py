#!/usr/bin/env python3
"""Poll Wyze API for person detection events and post them to PiVision backend."""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

import requests
from wyze_sdk import Client
from wyze_sdk.models.events import EventAlarmType, AiEventType

FREESTAND_MAC = "2CAA8E005A25"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def get_wyze_client(email: str, password: str, key_id: str, api_key: str) -> Client:
    return Client(email=email, password=password, key_id=key_id, api_key=api_key)


def fetch_person_events(client: Client, mac: str, since_ms: int) -> list:
    events = client.events.list(device_ids=[mac], limit=20)
    person_events = []
    for e in events:
        if e.time <= since_ms:
            continue
        tags = getattr(e, "_tags", [])
        is_person = any(getattr(t, "name", "") == "PERSON" for t in tags)
        if is_person:
            person_events.append(e)
    return person_events


def post_interaction(session: requests.Session, api_base: str, event, device_id: str) -> None:
    tags = getattr(event, "_tags", [])
    tag_names = [getattr(t, "name", str(t)) for t in tags]
    files = getattr(event, "_files", [])
    image_url = files[0].url if files else None

    payload = {
        "device_id": device_id,
        "event_ts": ms_to_iso(event.time),
        "event_type": "interaction_detected",
        "confidence": 0.9,
        "note": f"Wyze person detection ({', '.join(tag_names)})" + (f" — {image_url}" if image_url else ""),
    }
    resp = session.post(f"{api_base}/ingest/event", json=payload, timeout=5)
    resp.raise_for_status()
    print(f"[poller] Posted interaction at {ms_to_iso(event.time)} tags={tag_names}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll Wyze for person events and push to PiVision.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--api-base", default="http://192.168.1.153:8080/api/v1")
    parser.add_argument("--device-id", default="wyze-freestand2")
    parser.add_argument("--poll-interval", type=int, default=120, help="Seconds between polls")
    parser.add_argument("--lookback", type=int, default=600, help="Seconds to look back on first poll")
    parser.add_argument("--mac", default=FREESTAND_MAC)
    args = parser.parse_args()

    print(f"[poller] Starting Wyze event poller for {args.mac}")
    print(f"[poller] Posting to {args.api_base} every {args.poll_interval}s")

    client = get_wyze_client(args.email, args.password, args.key_id, args.api_key)
    session = requests.Session()
    last_seen_ms = int(time.time() * 1000) - (args.lookback * 1000)

    while True:
        try:
            events = fetch_person_events(client, args.mac, last_seen_ms)
            if events:
                print(f"[poller] Found {len(events)} new person event(s)")
                for e in events:
                    try:
                        post_interaction(session, args.api_base, e, args.device_id)
                        last_seen_ms = max(last_seen_ms, e.time)
                    except requests.RequestException as exc:
                        print(f"[poller] Failed to post event: {exc}")
            else:
                print(f"[poller] No new person events since {ms_to_iso(last_seen_ms)}")
        except Exception as exc:
            print(f"[poller] Error fetching events: {exc}")
            if "access token" in str(exc).lower() or "2001" in str(exc):
                print("[poller] Token expired — re-authenticating")
                try:
                    client = get_wyze_client(args.email, args.password, args.key_id, args.api_key)
                    print("[poller] Re-authenticated successfully")
                except Exception as auth_exc:
                    print(f"[poller] Re-auth failed: {auth_exc}")

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
