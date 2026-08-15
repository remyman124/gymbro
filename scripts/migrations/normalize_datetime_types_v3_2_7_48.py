#!/usr/bin/env python3
"""normalize_datetime_types_v3_2_7_48.py — make columns A/B real date/time cells.

Jim OOB 2026-08-14: "There are inconsistency on data such as data field is not
a date field. time stampe is not aligned."

Diagnosis (UNFORMATTED_VALUE read of 476 data rows):
  Column A (日期): 447 rows are plain TEXT ("2026-07-18"), 29 rows are real
                   DATE serials (46245).
  Column B (時間): 474 rows are real TIME serials (0.875), 2 rows are TEXT.

Root cause: the emergency restore wrote the whole sheet with
valueInputOption=RAW (everything became text), then sheet_cleanup_v3_2_7_47
rewrote only the 29 wide rows with USER_ENTERED, converting just those back
into real dates. A half-text column cannot be sorted or filtered by Sheets,
and text cells left-align while date/time cells right-align — which is the
visible "not aligned" symptom.

This migration:
  Step 1: Read A/B as UNFORMATTED_VALUE and canonicalise every cell to a
          locale-independent string — A -> "YYYY-MM-DD", B -> "HH:MM".
          Serial numbers are decoded arithmetically rather than by re-parsing
          display text, so a locale-dependent format like "7/18/2026" can
          never be misread as day-first.
  Step 2: Write the canonical strings back with USER_ENTERED so Sheets parses
          every cell into a true DATE / TIME value.
  Step 3: Pin the number formats (A = yyyy-mm-dd, B = hh:mm) so the display
          stays stable and both columns align uniformly.

Usage:
  python3 scripts/migrations/normalize_datetime_types_v3_2_7_48.py [--dry-run] [--yes]

Take a backup first (see /tmp/nutrition_backup_unformatted_*.json).
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
SHEET_TAB = "Nutrition"
SHEET_GID = 474877075

# Google Sheets serial epoch. Day 1 is 1899-12-31, so day 0 is 1899-12-30.
SHEETS_EPOCH = date(1899, 12, 30)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from sheet_cleanup_v3_2_7_47 import refresh_access_token  # noqa: E402


def fetch_unformatted(access: str):
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
           f"{SHEET_TAB}!A1:B10000?valueRenderOption=UNFORMATTED_VALUE")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read()).get("values", [])


def canon_date(v):
    """Return 'YYYY-MM-DD' or None if the value cannot be understood."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        # Sheets date serial -> calendar date. int() drops any time fraction.
        try:
            return (SHEETS_EPOCH + timedelta(days=int(v))).strftime("%Y-%m-%d")
        except (OverflowError, ValueError):
            return None
    s = str(v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def canon_time(v):
    """Return 'HH:MM' or None if the value cannot be understood."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        # Sheets time serial is a fraction of a day. Round to the nearest
        # minute: 0.8576388889 is 20:35 stored with float drift, and
        # truncating instead of rounding would render it as 20:34.
        frac = float(v) % 1.0
        total_min = int(round(frac * 24 * 60))
        total_min %= 24 * 60
        return f"{total_min // 60:02d}:{total_min % 60:02d}"
    s = str(v or "").strip()
    if not s:
        return None
    if "T" in s:  # leftover ISO datetime
        s = s.split("T", 1)[1]
    parts = s.split(":")
    if len(parts) >= 2:
        try:
            h, m = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    return None


def batch_update_values(access: str, data: list, chunk: int = 400):
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
           f"/values:batchUpdate")
    n = 0
    for i in range(0, len(data), chunk):
        body = {"valueInputOption": "USER_ENTERED", "data": data[i:i + chunk]}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {access}",
                     "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=90).read()
        n += len(data[i:i + chunk])
    return n


def apply_number_formats(access: str, n_rows: int):
    """Pin A = yyyy-mm-dd (DATE) and B = hh:mm (TIME) for all data rows."""
    def fmt_req(col_start, col_end, ftype, pattern):
        return {"repeatCell": {
            "range": {"sheetId": SHEET_GID, "startRowIndex": 1,
                      "endRowIndex": n_rows, "startColumnIndex": col_start,
                      "endColumnIndex": col_end},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": ftype, "pattern": pattern}}},
            "fields": "userEnteredFormat.numberFormat"}}

    body = {"requests": [fmt_req(0, 1, "DATE", "yyyy-mm-dd"),
                         fmt_req(1, 2, "TIME", "hh:mm")]}
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {access}",
                 "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=60).read()


def main():
    ap = argparse.ArgumentParser(description="Normalize A/B to real date/time cells")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    print(f"=== Normalize date/time cell types ({'DRY-RUN' if args.dry_run else 'LIVE'}) ===")
    access = refresh_access_token()
    rows = fetch_unformatted(access)
    print(f"Loaded {len(rows)} rows (header + {len(rows) - 1} data)\n")

    n_a_text = n_a_serial = n_b_text = n_b_serial = 0
    edits, bad = [], []
    for i, r in enumerate(rows[1:], start=2):
        a_raw = r[0] if len(r) > 0 else ""
        b_raw = r[1] if len(r) > 1 else ""
        if isinstance(a_raw, (int, float)) and not isinstance(a_raw, bool):
            n_a_serial += 1
        elif str(a_raw or "").strip():
            n_a_text += 1
        if isinstance(b_raw, (int, float)) and not isinstance(b_raw, bool):
            n_b_serial += 1
        elif str(b_raw or "").strip():
            n_b_text += 1

        a_c, b_c = canon_date(a_raw), canon_time(b_raw)
        if a_c is None or b_c is None:
            bad.append((i, a_raw, b_raw))
            continue
        edits.append({"range": f"{SHEET_TAB}!A{i}:B{i}", "values": [[a_c, b_c]]})

    print(f"Before — col A: {n_a_serial} real dates, {n_a_text} text")
    print(f"Before — col B: {n_b_serial} real times, {n_b_text} text")
    print(f"Canonicalised {len(edits)} rows; {len(bad)} unparseable\n")
    for i, a_raw, b_raw in bad[:10]:
        print(f"  ! row {i}: A={a_raw!r} B={b_raw!r}")

    if args.dry_run:
        for e in edits[:5]:
            print(f"  [DRY-RUN] {e['range']} -> {e['values'][0]}")
        print(f"\n[DRY-RUN] would rewrite {len(edits)} rows and pin number formats")
        return

    if not args.yes:
        if input(f"Rewrite {len(edits)} rows? [y/N] ").lower() != "y":
            print("Aborted.")
            sys.exit(0)

    n = batch_update_values(access, edits)
    print(f"  ✓ Rewrote {n} rows as real date/time values")
    apply_number_formats(access, len(rows))
    print(f"  ✓ Pinned number formats (A=yyyy-mm-dd, B=hh:mm)")

    after = fetch_unformatted(access)
    a_ok = sum(1 for r in after[1:] if len(r) > 0 and isinstance(r[0], (int, float)))
    b_ok = sum(1 for r in after[1:] if len(r) > 1 and isinstance(r[1], (int, float)))
    print(f"\nAfter — col A: {a_ok}/{len(after) - 1} real dates")
    print(f"After — col B: {b_ok}/{len(after) - 1} real times")


if __name__ == "__main__":
    main()
