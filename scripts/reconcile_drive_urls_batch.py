#!/usr/bin/env python3
"""reconcile_drive_urls_batch.py — sheet-side write of already-uploaded Drive URLs.

State of Drive URL backfill (Jim OOB 2026-08-14):
  Total scan_log entries: 75
    - 64 already have drive_image_url (uploaded earlier, never written to sheet)
    - 5 pending uploads, but ALL 5 image files are missing from disk (unrecoverable)
    - 6 with no image_path (legacy imports)
  Sheet rows missing col K: 435
    - 64 are scan-pipeline entries whose Drive URL is sitting in scan_log
    - ~371 are legacy bulk-imports from "Jim Meals Log legacy" with no image

This script does only the sheet-side write — it never uploads. For each
scan_log entry with a drive_image_url, it finds the matching sheet row
(by date+name, fallback by date+time) and writes the URL to col K.

Run with --chunk to process a slice of the scan_log; the orchestrator
launches parallel agents with non-overlapping ranges so the whole 64 is
done in a few seconds.

Usage:
  python3 scripts/reconcile_drive_urls_batch.py --chunk 0 16
  python3 scripts/reconcile_drive_urls_batch.py --chunk 16 32
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "migrations"))
from sheet_cleanup_v3_2_7_47 import refresh_access_token  # noqa: E402

SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
SHEET_TAB = "Nutrition"
SCAN_LOG_PATH = Path("/home/work/.hermes/food_scan_log.json")


def fetch_sheet_rows(access: str) -> list:
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
           f"{SHEET_TAB}?valueRenderOption=FORMATTED_VALUE")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read()).get("values", [])


def put_cell(access: str, a1_range: str, value: str) -> bool:
    """Write one cell with USER_ENTERED. Returns True on success."""
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
           f"{a1_range}?valueInputOption=USER_ENTERED")
    body = {"values": [[value]]}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="PUT",
        headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        print(f"  ! PUT {a1_range} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def batch_put(access: str, edits: list) -> int:
    """Batch-update cells. Returns number successfully written."""
    if not edits:
        return 0
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
           f"/values:batchUpdate")
    body = {"valueInputOption": "USER_ENTERED", "data": edits}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=60).read()
        return len(edits)
    except Exception as e:
        print(f"  ! batchUpdate failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def build_sheet_index(sheet_rows: list) -> tuple:
    """Build lookup tables: (date, name_first_8_lower) and (date, HH:MM)."""
    by_name, by_time = {}, {}
    for i, row in enumerate(sheet_rows[1:], start=2):
        if len(row) < 2 or not row[0]:
            continue
        date = row[0]
        name = (row[3] if len(row) > 3 else "").strip()
        time_part = row[1] if len(row) > 1 else ""
        if name:
            by_name[(date, name[:8].lower())] = i
        if time_part:
            by_time[(date, time_part[:5])] = i
    return by_name, by_time


def find_sheet_row(entry: dict, by_name: dict, by_time: dict):
    """Locate the sheet row for a scan_log entry. Returns row_num or None."""
    date = entry.get("date") or ""
    if not date and entry.get("timestamp_iso"):
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(entry["timestamp_iso"].replace("Z", "+00:00"))
            date = dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    time_short = (entry.get("time") or "")[:5]
    name_norm = ((entry.get("name") or "").strip()[:8]).lower()
    if date and name_norm and (date, name_norm) in by_name:
        return by_name[(date, name_norm)]
    if date and time_short and (date, time_short) in by_time:
        return by_time[(date, time_short)]
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--chunk", nargs=2, type=int, required=True,
                   help="start end indices into scan_log (e.g. 0 16)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    start, end = args.chunk

    log = json.loads(SCAN_LOG_PATH.read_text())
    eligible = [(i, e) for i, e in enumerate(log) if e.get("drive_image_url") and not e.get("_sheet_synced")]
    slice_ = eligible[start:end]
    print(f"[chunk {start}:{end}] eligible={len(eligible)}, processing {len(slice_)}")

    if args.dry_run:
        for i, e in slice_:
            print(f"  would sync row for entry[{i}]: {e.get('name', '?')[:40]}")
        return

    access = refresh_access_token()
    sheet_rows = fetch_sheet_rows(access)
    by_name, by_time = build_sheet_index(sheet_rows)
    print(f"Sheet index: {len(by_name)} by name, {len(by_time)} by time")

    edits = []
    n_matched = 0
    for i, e in slice_:
        row = find_sheet_row(e, by_name, by_time)
        if row:
            edits.append({"range": f"{SHEET_TAB}!K{row}",
                          "values": [[e["drive_image_url"]]]})
            n_matched += 1
            log[i]["_sheet_synced"] = True
            print(f"  entry[{i}] → sheet K{row}: {e.get('name', '?')[:40]}")
        else:
            print(f"  entry[{i}] NO MATCH: {e.get('date')} {e.get('time')} {e.get('name', '?')[:40]!r}")

    n_done = batch_put(access, edits)
    print(f"[chunk {start}:{end}] matched={n_matched} wrote={n_done}")

    # Save log with sync flags
    SCAN_LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2))
    print(f"Saved log with _sheet_synced flags")


if __name__ == "__main__":
    main()
