#!/usr/bin/env python3
"""backfill_drive_uploads.py — fill Drive Image URL column K for historical scans.

Jim OOB 2026-08-14: "the food log sheet should be the golden copy — both name
and image attached". Prior to v3.2.7.45, only the dish name was mirrored to
the Google Sheet — the image stayed local-only in /home/work/.hermes/scan_cache/.

Schema note (v3.2.7.48+): the Nutrition sheet is 11 columns A-K. Drive Image
URL is at column K (index 10). Pre-v3.2.7.48 it was at column N (index 13);
this script writes to K.

This script:
  1. Reads /home/work/.hermes/food_scan_log.json (line 5656+ of gym_web.py).
  2. For each entry with image_path but no drive_image_url:
     a. Upload the JPEG to Drive (best-effort, logs to /tmp/drive_upload_errors.log).
     b. Update the entry's drive_image_url field in food_scan_log.json.
     c. Find the corresponding row in the Google Sheet (by date+time match).
     d. Update column K of that row with the Drive URL.
  3. Idempotent — re-running skips entries that already have drive_image_url.

Usage:
  python3 scripts/backfill_drive_uploads.py [--dry-run] [--limit N]

Options:
  --dry-run    Print what would be done without uploading or modifying anything.
  --limit N    Process at most N entries (for testing).
  --rate N     Seconds between uploads (default 1.0, increase if you hit quota).
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import uuid
from pathlib import Path

# Constants — mirror gym_web.py
HOME = Path("/home/work/.hermes")
SCAN_LOG_PATH = HOME / "food_scan_log.json"
GOOGLE_TOKEN_PATH = HOME / "google_token.json"
SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
SHEET_TAB = "Nutrition"


def refresh_access_token() -> str:
    """Refresh OAuth access token; returns "" on failure."""
    try:
        tok = json.loads(GOOGLE_TOKEN_PATH.read_text())
        if not tok.get("refresh_token"):
            return ""
        data = urllib.parse.urlencode({
            "client_id": tok["client_id"],
            "client_secret": tok["client_secret"],
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        access = resp["access_token"]
        tok["token"] = access
        GOOGLE_TOKEN_PATH.write_text(json.dumps(tok, indent=2))
        return access
    except Exception:
        return ""


def upload_to_drive(local_path: Path, access_token: str) -> str:
    """Upload JPEG to Drive, return public URL or "" on failure."""
    if not local_path.exists():
        return ""
    try:
        boundary = uuid.uuid4().hex
        file_data = local_path.read_bytes()
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps({'name': local_path.name, 'mimeType': 'image/jpeg'})}\r\n"
            f"--{boundary}\r\n"
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode() + file_data + (f"\r\n--{boundary}--").encode()
        req = urllib.request.Request(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
            data=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            file_id = json.loads(r.read()).get("id")
        if not file_id:
            return ""
        # Make public-readable
        try:
            perm_req = urllib.request.Request(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
                data=json.dumps({"role": "reader", "type": "anyone"}).encode(),
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(perm_req, timeout=10).read()
        except Exception:
            pass
        return f"https://drive.google.com/uc?export=view&id={file_id}"
    except Exception as e:
        try:
            with open("/tmp/drive_upload_errors.log", "a") as f:
                f.write(f"{time.time():.0f} | backfill | {type(e).__name__}: {e} | path={local_path}\n")
        except Exception:
            pass
        return ""


def fetch_sheet_rows(access_token: str) -> list:
    """Fetch all rows from Nutrition tab. Returns list of row dicts."""
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{SHEET_TAB}?valueRenderOption=FORMATTED_VALUE"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return data.get("values", [])


def update_sheet_column_k(row_index: int, drive_url: str, access_token: str) -> bool:
    """Update Sheet row's column K (Drive Image URL, v3.2.7.48+).
    Row 1 is header; data rows start at 2."""
    try:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{SHEET_TAB}!K{row_index}?valueInputOption=USER_ENTERED"
        body = {"values": [[drive_url]]}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="PUT",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:
        return False


def ensure_header_k(access_token: str) -> bool:
    """Set K1 = 'Drive Image URL' if not already set."""
    try:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{SHEET_TAB}!K1"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        existing = (data.get("values") or [[""]])[0][0]
        if existing:
            return True  # already set
        url_put = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{SHEET_TAB}!K1?valueInputOption=USER_ENTERED"
        body = {"values": [["Drive Image URL"]]}
        req_put = urllib.request.Request(
            url_put, data=json.dumps(body).encode(), method="PUT",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        )
        urllib.request.urlopen(req_put, timeout=10).read()
        return True
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Backfill Drive URLs for historical food scans")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without modifying anything")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N entries (0 = all)")
    parser.add_argument("--rate", type=float, default=1.0, help="Seconds between uploads (default 1.0)")
    args = parser.parse_args()

    print(f"=== Backfill Drive URLs ===")
    print(f"Scan log: {SCAN_LOG_PATH}")
    print(f"Sheet: {SHEET_ID} (tab {SHEET_TAB})")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print(f"Rate limit: {args.rate}s between uploads")
    print()

    if not SCAN_LOG_PATH.exists():
        print(f"ERROR: {SCAN_LOG_PATH} not found")
        sys.exit(1)

    log = json.loads(SCAN_LOG_PATH.read_text())
    pending = [e for e in log if e.get("image_path") and not e.get("drive_image_url")]
    print(f"Total entries: {len(log)}")
    print(f"With image_path: {sum(1 for e in log if e.get('image_path'))}")
    print(f"Already have drive_image_url: {sum(1 for e in log if e.get('drive_image_url'))}")
    print(f"Pending uploads: {len(pending)}")
    print()

    if args.limit > 0:
        pending = pending[:args.limit]
        print(f"Limited to: {len(pending)} entries")
        print()

    if not pending:
        print("Nothing to do.")
        return

    if args.dry_run:
        print("Dry-run: would upload the following:")
        for i, e in enumerate(pending[:10]):
            print(f"  {i+1}. {e.get('name', '?')[:40]:40} | {e.get('image_path')}")
        if len(pending) > 10:
            print(f"  ... and {len(pending) - 10} more")
        return

    # Refresh access token once + fetch sheet rows for matching
    print("Refreshing access token...")
    access = refresh_access_token()
    if not access:
        print("ERROR: failed to refresh access token")
        sys.exit(1)
    print(f"Got access token (len={len(access)})")

    # Build (date, time) → row_index map for the sheet
    # v3.2.7.45: Use fuzzy matching. Sheet's column B has time rounded to whole
    # minute (e.g. "14:14:00") while scan_log timestamp_iso has full precision
    # (e.g. "14:14:09.008416"). Match by date + name (col D) since exact time
    # match fails when seconds differ.
    print("Fetching sheet rows for matching...")
    sheet_rows = fetch_sheet_rows(access)
    if not sheet_rows:
        print("ERROR: sheet is empty")
        sys.exit(1)
    # Build lookup tables
    # 1. (date, name_normalized) → row_index (most reliable)
    # 2. (date, time) → row_index (fallback for exact-second matches)
    sheet_by_name = {}  # (date, name_lower) → row_index
    sheet_by_time = {}  # (date, HH:MM) → row_index
    for i, row in enumerate(sheet_rows[1:], start=2):
        if len(row) < 2:
            continue
        date = row[0]
        dt_full = row[1]
        name = row[3] if len(row) > 3 else ""
        if not date or not dt_full:
            continue
        time_part = dt_full[11:16] if "T" in dt_full else dt_full[:5]
        if time_part:
            sheet_by_time[(date, time_part)] = i
        if name and date:
            # Normalize: strip whitespace, lowercase, take first 8 chars
            name_norm = name.strip()[:8].lower()
            sheet_by_name[(date, name_norm)] = i
    print(f"Sheet rows: {len(sheet_rows)} ({len(sheet_by_time)} by time, {len(sheet_by_name)} by name)")
    print()

    # Bootstrap column K header
    if not ensure_header_k(access):
        print("WARNING: failed to set K1 header (will continue)")
    else:
        print("✓ Set K1 = 'Drive Image URL'")

    # Process each pending entry
    n_ok = 0
    n_sheet_ok = 0
    n_fail = 0
    for i, entry in enumerate(pending):
        ts = entry.get("timestamp_iso", "")
        name = entry.get("name", "?")[:50]
        img_path = Path(entry.get("image_path", ""))
        if not img_path.exists():
            print(f"[{i+1}/{len(pending)}] {ts} | {name} | SKIP (file missing)")
            n_fail += 1
            continue

        print(f"[{i+1}/{len(pending)}] {ts} | {name} | uploading...", end=" ", flush=True)
        drive_url = upload_to_drive(img_path, access)
        if not drive_url:
            print("FAILED")
            n_fail += 1
            continue
        print(f"OK ({drive_url[-20:]})")

        # Update entry in scan_log
        entry["drive_image_url"] = drive_url
        n_ok += 1

        # Find matching sheet row
        date = entry.get("date", "")
        time_short = (entry.get("time", "") or "")[:5]
        if not date or not time_short:
            # Reconstruct from timestamp_iso
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                date = dt.strftime("%Y-%m-%d")
                time_short = dt.strftime("%H:%M")
            except Exception:
                pass
        # Try matching by name first (most reliable for dish matching)
        # then by date+time (fallback for exact-second matches)
        entry_name = (entry.get("name") or "").strip()
        name_norm = entry_name[:8].lower()
        sheet_row = sheet_by_name.get((date, name_norm)) or sheet_by_time.get((date, time_short))
        if sheet_row:
            if update_sheet_column_k(sheet_row, drive_url, access):
                n_sheet_ok += 1
                print(f"         → sheet K{sheet_row} updated (match: {entry_name[:20]})")
            else:
                print(f"         → sheet K{sheet_row} FAILED")
        else:
            print(f"         → no matching sheet row ({date} {time_short} {entry_name[:20]})")

        # Save log every 10 entries (atomic write)
        if (i + 1) % 10 == 0:
            tmp = SCAN_LOG_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(log, ensure_ascii=False, indent=2))
            tmp.replace(SCAN_LOG_PATH)
            print(f"         (saved log checkpoint at {i+1})")

        time.sleep(args.rate)

    # Final save
    tmp = SCAN_LOG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(log, ensure_ascii=False, indent=2))
    tmp.replace(SCAN_LOG_PATH)

    print()
    print(f"=== Done ===")
    print(f"Uploaded: {n_ok}")
    print(f"Sheet column K updated: {n_sheet_ok}")
    print(f"Failed: {n_fail}")


if __name__ == "__main__":
    main()
