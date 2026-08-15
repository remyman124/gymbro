#!/usr/bin/env python3
"""drop_columns_klm_v3_2_7_48.py — drop 來源 (K), Image (L), User Hints (M).

Jim OOB 2026-08-14: "i don't think 來源 User Hints columns are useful.
please remove the column and remove the update from gymbro pipeline."

Pre-v3.2.7.48 the Nutrition sheet was 14 cols (A-N):
  A 日期  B 時間  C 餐別  D 餐名  E 餐廳/連鎖  F kcal  G P  H C  I F  J 備註
  K 來源  L Image  M User Hints  N Drive Image URL

Post-v3.2.7.48 it is 11 cols (A-K), Drive URL moved to K; the three
中间 columns (來源, Image, User Hints) are dead. The pipeline code was
already migrated; this script only does the live-sheet-side delete.

A forensic archive of K/L/M is written to
/tmp/nutrition_dropped_columns_KLM_<ts>.json before the deletion so
any debugging value those columns carried is preserved offline.

The 24 rows still at 14 cols (out of 459) will be compressed to 11 cols
automatically by the column delete — Sheets closes the gap. Other rows
that were 11/12/13 cols simply gain the empty trailing cols we delete.

Usage:
  python3 scripts/migrations/drop_columns_klm_v3_2_7_48.py [--dry-run] [--yes]
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sheet_cleanup_v3_2_7_47 import (  # noqa: E402
    SHEET_ID, SHEET_TAB, refresh_access_token, fetch_sheet,
)

NUTRITION_SHEET_GID = 474877075


def post_batch_update(access: str, body: dict, timeout: int = 60):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=timeout).read()


def archive_klm(rows: list) -> Path:
    """Snapshot K, L, M for every row into a JSON file for forensics.

    Rows are stored as {row_num, date, time, meal_name, K, L, M} so the
    archive is self-describing even if column ordering later changes.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(f"/tmp/nutrition_dropped_columns_KLM_{ts}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, r in enumerate(rows[1:], start=2):
        entries.append({
            "row_num": i,
            "date": (r[0] if len(r) > 0 else ""),
            "time": (r[1] if len(r) > 1 else ""),
            "meal_name": (r[3] if len(r) > 3 else ""),
            "K_source": (r[10] if len(r) > 10 else ""),
            "L_image": (r[11] if len(r) > 11 else ""),
            "M_user_hints": (r[12] if len(r) > 12 else ""),
        })
    payload = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "sheet_id": SHEET_ID,
        "tab": SHEET_TAB,
        "rows_archived": len(entries),
        "header_seen": rows[0] if rows else [],
        "entries": entries,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return out


def main():
    ap = argparse.ArgumentParser(description="Drop K/L/M columns + archive")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    print(f"=== Drop K (來源) + L (Image) + M (User Hints) "
          f"({'DRY-RUN' if args.dry_run else 'LIVE'}) ===")
    access = refresh_access_token()
    rows = fetch_sheet(access)
    print(f"Loaded {len(rows)} rows (header + {len(rows) - 1} data)")

    # 1. Archive
    archive = archive_klm(rows)
    print(f"  ✓ Archived K/L/M to {archive}")

    # 2. Count rows that will be compressed (had data in cols 11+)
    n_will_compress = sum(1 for r in rows[1:] if len(r) > 11)
    print(f"  ℹ {n_will_compress} rows > 11 cols will be auto-compressed by the delete")

    if args.dry_run:
        print("\n[DRY-RUN] would delete columns K, L, M (sheetId=474877075, "
              "startIndex=10, endIndex=13) in one batchUpdate")
        return

    if not args.yes:
        ans = input("Delete columns K, L, M from the live sheet? [y/N] ")
        if ans.lower() != "y":
            print("Aborted.")
            sys.exit(0)

    # 3. Delete columns K, L, M in one batchUpdate.
    # deleteDimension with dimension=COLUMNS, startIndex=10, endIndex=13
    # removes K, L, M atomically. Column N (Drive URL) shifts to become K.
    body = {"requests": [{
        "deleteDimension": {
            "range": {
                "sheetId": NUTRITION_SHEET_GID,
                "dimension": "COLUMNS",
                "startIndex": 10,
                "endIndex": 13,
            }
        }
    }]}
    post_batch_update(access, body)
    print("  ✓ Deleted columns K, L, M (Drive URL shifted from N → K)")

    # 4. Verify
    after = fetch_sheet(access)
    print(f"\nAfter: {len(after) - 1} data rows")
    print(f"Header: {after[0]}")
    widths = [len(r) for r in after[1:]]
    from collections import Counter
    print(f"Width distribution: {dict(sorted(Counter(widths).items()))}")
    extras = sum(1 for r in after[1:] if len(r) > 11)
    if extras:
        print(f"  ! WARNING: {extras} rows still > 11 cols — investigate")
    else:
        print("  ✓ All rows ≤ 11 cols")


if __name__ == "__main__":
    main()
