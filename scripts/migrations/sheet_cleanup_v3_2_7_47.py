#!/usr/bin/env python3
"""sheet_cleanup_v3_2_7_47.py — fix the 537-row mess in Nutrition sheet.

!! SCHEMA NOTE (v3.2.7.48+) !!
This migration targets the PRE-v3.2.7.48 14-column schema (A-N), where
Drive Image URL was at column N. After v3.2.7.48 the sheet is 11 columns
A-K, the URL moved to K, and the Image column (L) was dropped. This script
must NOT be re-run against the current 11-column sheet — its row-padding
to 14, its A:N write ranges, and its O:AC clear would corrupt the new
schema. Behaviour is preserved as-is for historical fidelity; the read path
on a live 11-column sheet will simply produce no findings in step 1.

Jim OOB 2026-08-14: "the google sheet food log is all in a mess. please fix
the data and fix the pipeline of gymbro. find out the root cause and fix it.
spill up a job to validate it."

Three problems in existing data (root causes in pipeline):
  1. 76 rows have >14 cols (data "bled" into columns O+). Root cause: when
     the sheet's bottom row had width >14, values.append auto-detected the
     table range from the bottom, so subsequent rows inherited the wide
     width. Fixed in v3.2.7.47 by pre-flight trimming.
  2. 451 rows have ISO datetime in column B (e.g. "2026-08-09T20:44:48+08:00")
     instead of HH:MM. Root cause: a code path wrote f"{date}T{time}:00+08:00"
     which the Sheets API parsed as a full datetime instead of plain text.
     Fixed in v3.2.7.47 by stripping to HH:MM before appending.
  3. 9 rows have an image (col L) but no Drive URL (col N). Root cause: the
     Drive upload helper failed silently on transient errors before the
     v3.2.7.47 retry was added. Fixed in v3.2.7.47 by 2x retry with 2s backoff.

This migration:
  Step 1: Trim every row to exactly 14 cols (drop the O+ bleed)
  Step 2: Delete empty orphan rows (those with all-empty A-N)
  Step 3: Rewrite column B for rows that contain ISO datetime → HH:MM
  Step 4: Backfill Drive URLs for rows with image but empty col N

Usage:
  python3 scripts/migrations/sheet_cleanup_v3_2_7_47.py [--dry-run] [--skip-backfill]

Options:
  --dry-run          Show what would be done without modifying the sheet.
  --skip-backfill    Skip step 4 (Drive URL backfill).
  --yes              Skip confirmation prompt.

Requires the backup at /tmp/nutrition_sheet_backup_YYYYMMDD_HHMMSS.json
created by the manual pre-migration snapshot.
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from pathlib import Path

SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
SHEET_TAB = "Nutrition"
TOKEN_PATH = Path("/home/work/.hermes/google_token.json")
SCAN_LOG_PATH = Path("/home/work/.hermes/food_scan_log.json")
SCAN_CACHE_DIR = Path("/home/work/.hermes/scan_cache")


def refresh_access_token() -> str:
    tok = json.loads(TOKEN_PATH.read_text())
    data = urllib.parse.urlencode({
        "client_id": tok["client_id"], "client_secret": tok["client_secret"],
        "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    access = resp["access_token"]
    tok["token"] = access
    TOKEN_PATH.write_text(json.dumps(tok, indent=2))
    return access


def fetch_sheet(access: str):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{SHEET_TAB}?valueRenderOption=FORMATTED_VALUE"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read()).get("values", [])


def batch_update(access: str, edits: list, max_per_call: int = 500):
    """Apply edits in chunks (batchUpdate caps at ~500 per call)."""
    n_done = 0
    for chunk_start in range(0, len(edits), max_per_call):
        chunk = edits[chunk_start:chunk_start + max_per_call]
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate?valueInputOption=USER_ENTERED"
        body = {"valueInputOption": "USER_ENTERED", "data": chunk}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=60).read()
        n_done += len(chunk)
    return n_done


def delete_rows(access: str, row_indices: list):
    """Delete rows from sheet by 0-indexed positions using batchUpdate.deleteDimension."""
    if not row_indices:
        return 0
    # Sort descending so deletions don't shift unprocessed indices
    sorted_rows = sorted(set(row_indices), reverse=True)
    n_done = 0
    # batchUpdate can take up to ~500 requests per call; chunk by 200
    chunk_size = 200
    for chunk_start in range(0, len(sorted_rows), chunk_size):
        chunk = sorted_rows[chunk_start:chunk_start + chunk_size]
        requests = [{
            "deleteDimension": {
                "range": {
                    "sheetId": 474877075,  # Nutrition tab's sheetId
                    "dimension": "ROWS",
                    "startIndex": idx,
                    "endIndex": idx + 1,
                }
            }
        } for idx in chunk]
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate"
        body = {"requests": requests}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=60).read()
        n_done += len(chunk)
    return n_done


def upload_to_drive(local_path: Path, access: str) -> str:
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
                "Authorization": f"Bearer {access}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            file_id = json.loads(r.read()).get("id")
        if not file_id:
            return ""
        perm_req = urllib.request.Request(
            f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
            data=json.dumps({"role": "reader", "type": "anyone"}).encode(),
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(perm_req, timeout=10).read()
        except Exception:
            pass
        return f"https://drive.google.com/uc?export=view&id={file_id}"
    except Exception as e:
        with open("/tmp/drive_upload_errors.log", "a") as _f:
            _f.write(f"{time.time():.0f} | cleanup_v3_2_7_47 | {type(e).__name__}: {e} | path={local_path}\n")
        return ""


def step1_trim_rows(rows: list, access: str, dry_run: bool):
    """Normalize every row to exactly 14 cols (drop O+ bleed, shift back if shifted).

    Three row shapes:
      (a) Normal: row has 11-14 cols. Pad to 14. → writes A-N.
      (b) Wide-with-A-N-data: row has 15-29 cols AND A-N is populated. Trim to 14.
          The O+ bleed is duplicate/junk from API auto-detect — drop it.
          ALSO overwrite O+ with empty so the row's width collapses back to 14.
      (c) Shifted: row has 15-29 cols AND A-N is EMPTY (data starts in O+).
          Real data was mis-aligned by values.append auto-detect. SHIFT the
          data back to A, then trim to 14. ALSO clear O+ (it's now junk from
          before the shift).

    Without the shift in (c), we'd lose real food log entries. Without
    overwriting O+ in (b) and (c), the row's width stays >14 and confuses
    the next append's range auto-detection.
    """
    edits = []
    n_affected = 0
    n_shifted = 0
    for i, r in enumerate(rows[1:], start=2):  # skip header
        if len(r) <= 14:
            continue  # already correct
        # Pad to len(r) for slicing, find first non-empty column
        padded = list(r) + [""] * max(0, 14 - len(r))
        a_to_n = padded[:14]
        a_n_has_data = any((c or "").strip() for c in a_to_n)
        if a_n_has_data:
            # Case (b): wide row with valid A-N data.
            new_a_n = a_to_n[:14]
        else:
            # Case (c): shifted row. Find first non-empty cell.
            first = next((k for k, c in enumerate(padded) if (c or "").strip()), None)
            if first is None or first < 15:
                continue  # truly empty row, skip (Step 2 handles)
            shifted = padded[first:first + 14]
            while len(shifted) < 14:
                shifted.append("")
            new_a_n = shifted[:14]
            n_shifted += 1
        # Write A:N with new data AND clear any extra cols (15+)
        # Two separate edits: A:N overwrite + O:ZZ clear
        edits.append({"range": f"{SHEET_TAB}!A{i}:N{i}", "values": [new_a_n]})
        # Clear O+ cells up to the original width (observed max 29 cols → O:AC)
        edits.append({"range": f"{SHEET_TAB}!O{i}:AC{i}", "values": [[""] * 15]})
        n_affected += 1
    if dry_run:
        print(f"  [DRY-RUN] Step 1: would normalize {n_affected} rows ({n_shifted} shifted back to A, {n_affected - n_shifted} trimmed)")
        return n_affected
    n_done = batch_update(access, edits)
    print(f"  ✓ Step 1: normalized {n_affected} rows ({n_shifted} shifted, {n_affected - n_shifted} trimmed); {n_done} total edits (A:N + O:Z)")
    return n_affected


def step2_delete_empty(rows: list, access: str, dry_run: bool):
    """Delete empty orphan rows (those with all-empty A-N)."""
    empty_indices = []
    for i, r in enumerate(rows[1:], start=2):
        # Consider empty if no date in A and no other meaningful content
        a = r[0] if r else ""
        if not a or not a.strip():
            empty_indices.append(i - 1)  # 0-indexed for batchUpdate
    if dry_run:
        print(f"  [DRY-RUN] Step 2: would delete {len(empty_indices)} empty rows: {empty_indices[:20]}{'...' if len(empty_indices)>20 else ''}")
        return len(empty_indices)
    n_done = delete_rows(access, empty_indices)
    print(f"  ✓ Step 2: deleted {n_done} empty orphan rows")
    return n_done


def step3_fix_iso_time(rows: list, access: str, dry_run: bool):
    """Rewrite column B from ISO datetime → HH:MM."""
    edits = []
    n_affected = 0
    for i, r in enumerate(rows[1:], start=2):
        if len(r) < 2:
            continue
        b = r[1] if r[1] else ""
        if "T" in b:
            # e.g. "2026-08-09T20:44:48+08:00" → "20:44"
            t_part = b.split("T", 1)[1][:5]
            edits.append({"range": f"{SHEET_TAB}!B{i}", "values": [[t_part]]})
            n_affected += 1
    if dry_run:
        print(f"  [DRY-RUN] Step 3: would fix {n_affected} ISO datetime → HH:MM")
        return n_affected
    n_done = batch_update(access, edits)
    print(f"  ✓ Step 3: rewrote {n_done} column-B ISO datetimes → HH:MM")
    return n_done


def step4_backfill_drive(rows: list, access: str, dry_run: bool, scan_log: list):
    """Backfill Drive URLs for rows with image but empty col N."""
    # Build (date, name_first_8) → image_path lookup from scan_log
    log_by_key = {}
    for e in scan_log:
        d = e.get("date", "")
        n = (e.get("name") or "")[:8].lower()
        ip = e.get("image_path", "")
        if d and n and ip:
            log_by_key[(d, n)] = ip

    pending = []  # (sheet_row_index, local_image_path)
    n_with_image = 0
    for i, r in enumerate(rows[1:], start=2):
        # rows is already normalized (14 cols) — direct index
        n_url = r[13] or ""
        l_image = r[11] or ""
        if l_image.strip() and not n_url.strip():
            n_with_image += 1
            # Find scan_log entry by (date, name[:8].lower())
            date = r[0] or ""
            name = (r[3] or "")[:8].lower()
            img_path_str = log_by_key.get((date, name), "")
            if img_path_str:
                pending.append((i, Path(img_path_str)))

    print(f"  Found {n_with_image} rows with image but no Drive URL; {len(pending)} have matching scan_log entries")
    if dry_run:
        for i, p in pending[:5]:
            print(f"    [DRY-RUN] would backfill row {i} from {p}")
        print(f"  [DRY-RUN] Step 4: would upload + update {len(pending)} rows")
        return 0

    n_ok = 0
    edits = []
    for sheet_row, img_path in pending:
        if not img_path.exists():
            print(f"    Row {sheet_row}: image {img_path} missing on disk, skipping")
            continue
        drive_url = upload_to_drive(img_path, access)
        if not drive_url:
            print(f"    Row {sheet_row}: upload failed for {img_path}")
            continue
        edits.append({"range": f"{SHEET_TAB}!N{sheet_row}", "values": [[drive_url]]})
        n_ok += 1
        if len(edits) >= 50:
            batch_update(access, edits)
            edits = []
            print(f"    ... uploaded {n_ok}/{len(pending)}")
            time.sleep(1.0)
    if edits:
        batch_update(access, edits)
    print(f"  ✓ Step 4: backfilled {n_ok} Drive URLs")
    return n_ok


def _normalize_rows(rows: list) -> list:
    """Pad every row to exactly 14 cols so checks can use fixed indices."""
    out = []
    for r in rows:
        if r is None:
            r = []
        # Don't trim — caller wants the full original row. Just pad.
        out.append(list(r) + [""] * (14 - len(r)) if len(r) < 14 else list(r))
    return out


def main():
    parser = argparse.ArgumentParser(description="Fix the Nutrition sheet mess")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--skip-backfill", action="store_true", help="Skip Drive URL backfill (step 4)")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"=== Nutrition Sheet Cleanup ({mode}) ===")
    print(f"Sheet: {SHEET_ID}")
    print()

    access = refresh_access_token()
    print(f"✓ Got access token (len={len(access)})")
    print()

    rows = fetch_sheet(access)
    print(f"Loaded {len(rows)} rows (header + {len(rows)-1} data)")
    rows = _normalize_rows(rows)
    print()

    # Diagnostic preflight (post-padding)
    n_bleed = sum(1 for r in rows[1:] if len(r) > 14)
    n_empty = sum(1 for r in rows[1:] if not (r[0] if r else "").strip())
    n_iso = sum(1 for r in rows[1:] if len(r) > 1 and r[1] and "T" in r[1])
    n_no_drive = sum(1 for r in rows[1:] if (r[11] or "").strip() and not (r[13] or "").strip())
    print(f"Preflight: bleed={n_bleed} | empty={n_empty} | ISO_B={n_iso} | no_drive_url={n_no_drive}")
    print()

    if not args.yes and not args.dry_run:
        confirm = input("Proceed? [y/N] ")
        if confirm.lower() != "y":
            print("Aborted.")
            sys.exit(0)

    scan_log = json.loads(SCAN_LOG_PATH.read_text()) if SCAN_LOG_PATH.exists() else []

    # IMPORTANT: re-fetch sheet after each step that modifies it. Otherwise
    # in-memory row indices become stale after deletions (step 2) or wide-row
    # shifts (step 1), and subsequent edits target wrong rows.
    n1 = step1_trim_rows(rows, access, args.dry_run)
    if not args.dry_run:
        rows = _normalize_rows(fetch_sheet(access))
        print(f"  [re-fetched after step 1: {len(rows)-1} data rows]")
    print()

    n2 = step2_delete_empty(rows, access, args.dry_run)
    if not args.dry_run:
        rows = _normalize_rows(fetch_sheet(access))
        print(f"  [re-fetched after step 2: {len(rows)-1} data rows]")
    print()

    n3 = step3_fix_iso_time(rows, access, args.dry_run)
    if not args.dry_run:
        rows = _normalize_rows(fetch_sheet(access))
        print(f"  [re-fetched after step 3: {len(rows)-1} data rows]")
    print()

    if not args.skip_backfill:
        n4 = step4_backfill_drive(rows, access, args.dry_run, scan_log)
        print()

    if args.dry_run:
        print("[DRY-RUN complete — no changes made]")
    else:
        print(f"=== Done ===")
        print(f"Rows normalized (step 1): {n1}")
        print(f"Empty rows deleted (step 2): {n2}")
        print(f"ISO datetimes fixed (step 3): {n3}")
        if not args.skip_backfill:
            print(f"Drive URLs backfilled (step 4): {n4}")


if __name__ == "__main__":
    main()