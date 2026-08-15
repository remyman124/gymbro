#!/usr/bin/env python3
"""backfill_drive_uploads_v2.py — match Sheet rows to scan_cache/ files by date+time.

v3.3.2 companion script: with food_scan_log.json gone, the only bridge
between Sheet rows and local scan_cache/ files is the file's
encoded date+time (scan_YYYYMMDD_HHMMSS.jpg). This script:

  1. Reads the Sheet rows with empty column K (no Drive URL).
  2. For each, looks up scan_cache/ for a file whose encoded
     YYYYMMDD_HHMMSS matches the row's date+time within ±5 min.
  3. Uploads the JPEG to Google Drive (best-effort, logs failures).
  4. Updates column K of that Sheet row with the new Drive URL.

Idempotent — re-runs skip rows that already have a URL.

Usage:
  python3 scripts/backfill_drive_uploads_v2.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path("/home/work/.hermes")
SCAN_CACHE = HOME / "scan_cache"
GOOGLE_TOKEN_PATH = HOME / "google_token.json"
SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
SHEET_TAB = "Nutrition"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
SHEET_UPDATE_URL = (
    f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate"
)


def refresh_access_token() -> str:
    tok = json.loads(GOOGLE_TOKEN_PATH.read_text())
    data = urllib.parse.urlencode({
        "client_id": tok["client_id"],
        "client_secret": tok["client_secret"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    tok["token"] = resp["access_token"]
    GOOGLE_TOKEN_PATH.write_text(json.dumps(tok, indent=2))
    return resp["access_token"]


def fetch_sheet_rows() -> list[list[str]]:
    access = refresh_access_token()
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
        f"{SHEET_TAB}!A1:K1000"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read()).get("values", [])


def upload_to_drive(local_path: Path) -> str:
    """Upload JPEG to Drive, make public-readable, return public view URL."""
    access = refresh_access_token()
    # Multipart upload: metadata + media in one request
    boundary = "----gymbrobackfill"
    filename = local_path.name
    body = []
    body.append(f"--{boundary}".encode())
    body.append(b"Content-Type: application/json; charset=UTF-8")
    body.append(b"")
    body.append(json.dumps({"name": filename, "mimeType": "image/jpeg"}).encode())
    body.append(f"--{boundary}".encode())
    body.append(b"Content-Type: image/jpeg")
    body.append(b"")
    body.append(local_path.read_bytes())
    body.append(f"--{boundary}--".encode())
    body.append(b"")
    payload = b"\r\n".join(body)
    req = urllib.request.Request(
        f"{DRIVE_UPLOAD_URL}?uploadType=multipart",
        data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {access}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    file_id = resp["id"]
    # Make public
    perm_req = urllib.request.Request(
        f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
        data=json.dumps({"type": "anyone", "role": "reader"}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(perm_req, timeout=15).read()
    return f"https://drive.google.com/uc?export=view&id={file_id}"


def update_sheet_cell(row_idx: int, col_letter: str, value: str) -> None:
    access = refresh_access_token()
    body = {
        "valueInputOption": "USER_ENTERED",
        "data": [{"range": f"{SHEET_TAB}!{col_letter}{row_idx}", "values": [[value]]}],
    }
    req = urllib.request.Request(
        SHEET_UPDATE_URL, data=json.dumps(body).encode(), method="POST",
        headers={
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(req, timeout=15).read()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--rate", type=float, default=1.0,
                   help="seconds between Drive uploads")
    args = p.parse_args()

    print(f"Sheet: {SHEET_ID} (tab {SHEET_TAB})")
    print(f"scan_cache: {SCAN_CACHE}")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print(f"Rate limit: {args.rate}s between uploads")
    print()

    # Index scan_cache by date -> [(time_minutes, filename), ...]
    files_by_date: dict[str, list[tuple[int, str]]] = collections.defaultdict(list)
    for f in sorted(os.listdir(SCAN_CACHE)):
        if not f.endswith(".jpg"):
            continue
        m = re.match(r"(?:scan|preview)_(\d{8})_(\d{6})", f)
        if not m:
            continue
        d = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}"
        t = int(m.group(2)[:2]) * 60 + int(m.group(2)[2:4])
        files_by_date[d].append((t, f))

    print(f"Indexed {sum(len(v) for v in files_by_date.values())} files across "
          f"{len(files_by_date)} dates")

    # Read Sheet rows
    rows = fetch_sheet_rows()
    print(f"Sheet rows: {len(rows)} (incl. header)")

    matched = []
    for i, r in enumerate(rows):
        if i == 0:
            continue
        if len(r) < 6:
            continue
        date = r[0].strip()
        time_short = r[1].strip()[:5]
        if not date or not time_short:
            continue
        has_drive = len(r) > 10 and bool(r[10].strip())
        if has_drive:
            continue
        if date not in files_by_date:
            continue
        try:
            h, m = time_short.split(":")
            row_minutes = int(h) * 60 + int(m)
        except Exception:
            continue
        # Find file within ±5 min
        candidates = files_by_date[date]
        best = None
        best_diff = 99
        for file_min, fname in candidates:
            diff = abs(file_min - row_minutes)
            if diff <= 5 and diff < best_diff:
                best = fname
                best_diff = diff
        if best is None:
            continue
        matched.append((i + 1, date, time_short, r[3][:30] if len(r) > 3 else "",
                        best, best_diff))

    print(f"Matched {len(matched)} Sheet rows to scan_cache/ files")
    print()

    if args.limit:
        matched = matched[:args.limit]

    uploaded = 0
    failed = 0
    skipped = 0
    for sheet_row, date, time_short, name, fname, diff in matched:
        local = SCAN_CACHE / fname
        if not local.exists():
            skipped += 1
            continue
        print(f"  row {sheet_row:4d} | {date} {time_short} | {name:30.30s} | "
              f"→ {fname} (Δ{diff}min)", end="")
        if args.dry_run:
            print(" | DRY-RUN skip")
            continue
        try:
            url = upload_to_drive(local)
            update_sheet_cell(sheet_row, "K", url)
            print(f" | OK {url[:50]}")
            uploaded += 1
            time.sleep(args.rate)
        except Exception as e:
            print(f" | FAIL {type(e).__name__}: {e}")
            failed += 1

    print()
    print(f"Done: uploaded={uploaded} failed={failed} skipped={skipped}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())