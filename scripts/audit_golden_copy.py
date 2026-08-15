#!/usr/bin/env python3
"""audit_golden_copy.py — audit & repair Sheet column K (Drive Image URL).

Jim OOB 2026-08-15 'if the golden source is broken, pls fix the golden
copy at google sheet'. v3.3.2 made Sheet + Drive canonical; v3.3.3
recovered 25 recoverable rows; v3.3.4 stopped serving placeholder URLs.
The remaining gaps:

  - rows where K is non-empty but not a URL (corrupted data → CLEAR to
    empty, no image)
  - rows where K is empty + a scan_cache/ file matches by date+time (±5
    min) → backfill via Drive upload + values:batchUpdate to K
  - rows where K is empty + no match → unfixable, leave as-is

Idempotent — re-runs are safe (skip rows where K is already a URL).

Usage:
  python3 scripts/audit_golden_copy.py [--dry-run] [--no-clear-bad]
                                       [--no-backfill] [--limit N]
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
URL_RE = re.compile(r"^https?://(lh3\.googleusercontent\.com|drive\.google\.com)/")


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
    access = refresh_access_token()
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


def update_sheet_cells(updates: list[tuple[int, str, str]]) -> int:
    """Batch update (row_idx, col_letter, value). Returns total updated."""
    if not updates:
        return 0
    access = refresh_access_token()
    body = {
        "valueInputOption": "USER_ENTERED",
        "data": [
            {"range": f"{SHEET_TAB}!{c}{r}", "values": [[v]]}
            for r, c, v in updates
        ],
    }
    req = urllib.request.Request(
        SHEET_UPDATE_URL, data=json.dumps(body).encode(), method="POST",
        headers={
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
        },
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    return resp.get("totalUpdatedCells", 0)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-clear-bad", action="store_true",
                   help="don't clear corrupted K values")
    p.add_argument("--no-backfill", action="store_true",
                   help="don't upload missing K from scan_cache")
    p.add_argument("--limit", type=int, default=0,
                   help="limit rows audited (excluding header)")
    args = p.parse_args()

    print(f"Sheet: {SHEET_ID} (tab {SHEET_TAB})")
    print(f"scan_cache: {SCAN_CACHE}")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print()

    # Index scan_cache by date -> [(time_minutes, filename)]
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

    rows = fetch_sheet_rows()
    print(f"Sheet rows: {len(rows)} (incl. header)")

    bad_k_rows: list[tuple[int, str]] = []      # row_idx, bad_value
    empty_k_unmatchable: list[tuple[int, str, str]] = []  # row_idx, date, time
    empty_k_matchable: list[tuple[int, str, str, str]] = []  # row_idx, date, time, fname
    ok_k_rows = 0

    for i, r in enumerate(rows):
        if i == 0:
            continue  # header
        row_idx = i + 1
        k_val = r[10].strip() if len(r) > 10 else ""

        if k_val and URL_RE.match(k_val):
            ok_k_rows += 1
            continue

        date = r[0].strip() if len(r) > 0 else ""
        time_short = r[1].strip()[:5] if len(r) > 1 else ""

        if k_val:
            # Non-empty but bad — corrupt data
            bad_k_rows.append((row_idx, k_val))
            continue

        if not date or not time_short:
            continue

        try:
            h, mm = time_short.split(":")
            row_min = int(h) * 60 + int(mm)
        except Exception:
            continue

        candidates = files_by_date.get(date, [])
        best, best_diff = None, 99
        for file_min, fname in candidates:
            diff = abs(file_min - row_min)
            if diff <= 5 and diff < best_diff:
                best, best_diff = fname, diff
        if best:
            empty_k_matchable.append((row_idx, date, time_short, best))
        else:
            empty_k_unmatchable.append((row_idx, date, time_short))

    print(f"  ✓ valid URL K column: {ok_k_rows}")
    print(f"  ✗ corrupted K column  : {len(bad_k_rows)} rows")
    print(f"  ⟳ empty K + recoverable local file: {len(empty_k_matchable)} rows")
    print(f"  · empty K + unfixable: {len(empty_k_unmatchable)} rows")

    if args.limit:
        bad_k_rows = bad_k_rows[:args.limit]
        empty_k_matchable = empty_k_matchable[:args.limit]
        empty_k_unmatchable = empty_k_unmatchable[:args.limit]
        print(f"  (limited to {args.limit})")

    print()
    if bad_k_rows:
        print("Corrupted K values to clear (not URLs):")
        for row_idx, val in bad_k_rows[:10]:
            print(f"  row {row_idx}: K={val!r}")
        if len(bad_k_rows) > 10:
            print(f"  ... and {len(bad_k_rows) - 10} more")

    if empty_k_matchable:
        print(f"\nEmpty K rows recoverable from scan_cache:")
        for row_idx, date, time_short, fname in empty_k_matchable[:10]:
            print(f"  row {row_idx}: {date} {time_short} → {fname}")
        if len(empty_k_matchable) > 10:
            print(f"  ... and {len(empty_k_matchable) - 10} more")

    if args.dry_run:
        print("\nDRY-RUN: no changes made")
        return 0

    # Phase 1: clear bad K values
    cleared = 0
    if bad_k_rows and not args.no_clear_bad:
        updates = [(r, "K", "") for r, _ in bad_k_rows]
        cleared = update_sheet_cells(updates)
        print(f"\n✓ cleared {cleared} corrupted K cells")

    # Phase 2: upload + populate for empty-but-recoverable rows
    uploaded = 0
    failed = 0
    if empty_k_matchable and not args.no_backfill:
        for row_idx, date, time_short, fname in empty_k_matchable:
            local = SCAN_CACHE / fname
            if not local.exists():
                failed += 1
                continue
            try:
                url = upload_to_drive(local)
                update_sheet_cells([(row_idx, "K", url)])
                uploaded += 1
                print(f"  row {row_idx}: {date} {time_short} → {url[:60]}")
                time.sleep(1.0)
            except Exception as e:
                print(f"  row {row_idx}: FAIL {type(e).__name__}: {e}")
                failed += 1

    print()
    print(f"Summary: cleared={cleared} uploaded={uploaded} failed={failed}")
    print(f"  unfixable rows left as-is: {len(empty_k_unmatchable)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
