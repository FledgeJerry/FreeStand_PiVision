#!/usr/bin/env python3
"""Capture frames via libcamera-still and push them to PiVision ingest."""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
import tempfile

import requests

try:
    import shutil
except ImportError:
    shutil = None  # type: ignore[assignment]

LIBCAMERA_STILL = None
for candidate in ["rpicam-still", "libcamera-still"]:
    LIBCAMERA_STILL = shutil.which(candidate) if shutil is not None else None
    if LIBCAMERA_STILL:
        break
if not LIBCAMERA_STILL:
    for candidate in ["/usr/bin/rpicam-still", "/usr/local/bin/libcamera-still"]:
        if os.path.exists(candidate):
            LIBCAMERA_STILL = candidate
            break

if not LIBCAMERA_STILL:
    raise SystemExit("rpicam-still not found; install libcamera-apps (apt install libcamera-apps).")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_seq(path: Path) -> int:
    if not path.exists():
        return 1
    try:
        return int(path.read_text().strip())
    except ValueError:
        return 1


def persist_seq(path: Path, seq: int) -> None:
    path.write_text(str(seq))


def capture_jpeg(temp_path: Path, width: int, height: int) -> bytes:
    cmd = [
        LIBCAMERA_STILL,
        "--nopreview",
        "--timeout",
        "1",
        "--width",
        str(width),
        "--height",
        str(height),
        "-o",
        str(temp_path),
    ]
    subprocess.run(cmd, check=True)
    return temp_path.read_bytes()


def build_payload(device_id: str, seq: int, width: int, height: int, quality: int, image_b64: str) -> dict:
    return {
        "device_id": device_id,
        "capture_ts": iso_now(),
        "seq": seq,
        "width": width,
        "height": height,
        "jpeg_quality": quality,
        "image_b64": image_b64,
    }


def encode(image_bytes: bytes, quality: int) -> str:
    return base64.b64encode(image_bytes).decode("ascii")


def send_frame(session: requests.Session, base_url: str, device_key: str, payload: dict) -> dict:
    headers = {"X-DEVICE-KEY": device_key, "Content-Type": "application/json"}
    resp = session.post(f"{base_url}/ingest/frame", headers=headers, json=payload, timeout=5)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="PiVision ingest client using libcamera-still.")
    parser.add_argument("--device-id", default="pi-camera", help="Device id for ingest payload.")
    parser.add_argument("--api-base", default="http://localhost:8080/api/v1", help="Ingest API base URL.")
    parser.add_argument("--device-key", default="dev-key", help="X-DEVICE-KEY header value.")
    parser.add_argument("--capture-interval", type=float, default=1.0, help="Seconds between frames.")
    parser.add_argument("--seq-file", type=Path, default=Path("backend/data/pi-camera.seq"), help="Sequence file path.")
    parser.add_argument("--resolution", type=str, default="640x480", help="Resolution WIDTHxHEIGHT.")
    parser.add_argument("--jpeg-quality", type=int, default=70, help="JPEG quality (1-100).")
    parser.add_argument("--max-frames", type=int, default=0, help="Stop after N frames (0 = forever).")
    args = parser.parse_args()

    WIDTH, HEIGHT = map(int, args.resolution.split("x"))
    seq_path = args.seq_file
    seq_path.parent.mkdir(parents=True, exist_ok=True)
    seq = load_seq(seq_path)
    session = requests.Session()

    print(
        f"PiVision libcamera ingest device={args.device_id} interval={args.capture_interval}s resolution={WIDTH}x{HEIGHT}"
    )
    frame_count = 0
    while True:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            image_bytes = capture_jpeg(temp_path, WIDTH, HEIGHT)
            image_b64 = encode(image_bytes, args.jpeg_quality)
            payload = build_payload(args.device_id, seq, WIDTH, HEIGHT, args.jpeg_quality, image_b64)
            try:
                response = send_frame(session, args.api_base, args.device_key, payload)
                print(f"[ingest] seq={seq} frame_id={response.get('frame_id')} ok")
            except requests.RequestException as exc:
                print(f"[ingest] seq={seq} failed: {exc}")
        finally:
            temp_path.unlink(missing_ok=True)

        seq += 1
        persist_seq(seq_path, seq)
        frame_count += 1
        if args.max_frames and frame_count >= args.max_frames:
            break
        time.sleep(args.capture_interval)


if __name__ == "__main__":
    main()
