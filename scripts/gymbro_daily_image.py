#!/usr/bin/env python3
"""
gymbro_daily_image.py — generate today's motivation image for the gymbro app.

Calls the MiniMax image-01 model, downloads the result, and writes it to
the Flask app's static image cache so it appears at /img/<filename>.

Output: /home/work/.hermes/image_cache/gymbro_<YYYY-MM-DD>.png

Run once per morning (cron @ 06:00 HKT recommended).

Pre-flight: run scripts/probe_image_endpoint.py first to confirm API
behaviour — model name "image-01" is the only accepted value (verified
2026-07-04 EU Day 3).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------- Config ----------
HKT = timezone(timedelta(hours=8))
IMAGE_CACHE = Path("/home/work/.hermes/image_cache")
ENDPOINT = "https://api.minimax.io/v1/image_generation"
MODEL = "image-01"  # canonical — verified 2026-07-04
WIDTH = 1024
HEIGHT = 1024

# Prompt is intentionally minimal and gym-flavoured. Edit freely.
PROMPT = (
    "cinematic gym motivation poster, dramatic side-lighting on a single barbell "
    "and chalked hands, dark moody background, low-angle hero shot, "
    "ultra-detailed, photorealistic, 35mm film grain, no text"
)


def today_hkt_iso() -> str:
    return datetime.now(HKT).strftime("%Y-%m-%d")


def cache_path_for(date_iso: str) -> Path:
    IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
    return IMAGE_CACHE / f"gymbro_{date_iso}.png"


def fetch_image_url(api_key: str, prompt: str) -> str:
    payload = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "width": WIDTH,
        "height": HEIGHT,
        "response_format": "url",
    }).encode("utf-8")

    req = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    if body.get("base_resp", {}).get("status_code", -1) != 0:
        raise RuntimeError(f"image-01 API error: {body}")

    urls = body.get("data", {}).get("image_urls") or []
    if not urls:
        raise RuntimeError(f"no image_urls in response: {body}")
    return urls[0]


def download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as resp:
        dest.write_bytes(resp.read())


def main() -> int:
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        print("ERROR: MINIMAX_API_KEY env var not set", file=sys.stderr)
        return 2

    date_iso = today_hkt_iso()
    dest = cache_path_for(date_iso)

    if dest.exists() and dest.stat().st_size > 0 and "--force" not in sys.argv:
        print(f"✓ cache hit: {dest} ({dest.stat().st_size} bytes) — skipping gen")
        return 0

    print(f"→ generating image for {date_iso}")
    try:
        url = fetch_image_url(api_key, PROMPT)
    except urllib.error.HTTPError as e:
        print(f"ERROR: HTTP {e.code} from {ENDPOINT}: {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: fetch failed: {e}", file=sys.stderr)
        return 1

    print(f"→ downloading {url}")
    try:
        download(url, dest)
    except Exception as e:
        print(f"ERROR: download failed: {e}", file=sys.stderr)
        return 1

    size = dest.stat().st_size
    print(f"✓ wrote {dest} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())