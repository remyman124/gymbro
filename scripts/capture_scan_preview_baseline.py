#!/usr/bin/env python3
"""Capture /api/scan_preview response + recursive key structure for regression.

Usage:
  PYTHONPATH="" /usr/bin/python3 scripts/capture_scan_preview_baseline.py \
      --base-url http://127.0.0.1:7000 \
      --image /home/work/.hermes/scan_cache/scan_20261020_221827.jpg \
      --out /home/work/projects/gymbro/scan_preview_baseline.json
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def key_paths(obj, prefix=""):
    """Recursively collect every key path in a JSON structure."""
    out = []
    if isinstance(obj, dict):
        for k in obj.keys():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.append(p)
            out.extend(key_paths(obj[k], p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(key_paths(v, f"{prefix}[]"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:7000")
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    img = Path(args.image)
    if not img.exists():
        print(f"FATAL: image not found: {img}", file=sys.stderr)
        return 2

    boundary = f"----gymbrobaseline{uuid.uuid4().hex}"
    payload = img.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{img.name}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{args.base_url}/api/scan_preview",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        status = e.code
    except Exception as e:
        print(f"FATAL: request failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"FATAL: response not JSON: {e}\n{raw[:500]}", file=sys.stderr)
        return 4

    top_keys = sorted(parsed.keys())
    all_keys = sorted(set(key_paths(parsed)))
    payload_out = {
        "captured_from": args.base_url,
        "endpoint": "POST /api/scan_preview",
        "http_status": status,
        "image_used": str(img),
        "top_level_keys": top_keys,
        "all_key_paths": all_keys,
        "response": parsed,
    }
    outp = Path(args.out)
    outp.write_text(json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK status={status} top_keys={len(top_keys)} all_key_paths={len(all_keys)} -> {outp}")
    print("TOP_KEYS:", json.dumps(top_keys, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
