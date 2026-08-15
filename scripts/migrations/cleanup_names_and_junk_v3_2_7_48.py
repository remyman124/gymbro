#!/usr/bin/env python3
"""cleanup_names_and_junk_v3_2_7_48.py — fix col D names, drop junk rows.

Jim OOB 2026-08-14:
  "please clean up duplicated and error column - such as （APiyi gpt-4o vision
   失敗). moreover, 這張相顯示一支蘇打水樽 should be recognized as 蘇打水"

Two defects, both fallout from the 2026-08-10/11 auto-commit storm and from
older vision paths that wrote the raw description straight into column D:

  1. AI NARRATION IN COLUMN D (28 rows). The vision model's prose description
     was stored as the dish name, then truncated at 120 chars — e.g.
     "這張相顯示一支蘇打水樽" instead of "蘇打水".
  2. JUNK ROWS. The storm wrote paired rows per scan: one with narration and
     one totally empty, plus rows whose name is the literal API failure string
     "（APiyi gpt-4o vision 失敗".

Re-extraction is AI-ONLY. Jim's standing rule (OOB 2026-08-08, restated
2026-08-11): "No regex. Use ai" / "don't use rule/regex". So every name is
re-derived via _extract_dish_name_ai, and when the AI returns nothing the
name becomes 未識別菜式 — never a fabricated or regex-sliced guess.

Deletion is deliberately conservative: a row is only dropped when it has
kcal == 0 AND no usable name, or when it is an exact duplicate of an earlier
row. Rows carrying real macros (e.g. the kcal=412 row whose name is the API
error string) are RELABELLED, never deleted, so no nutrition data is lost.

Usage:
  python3 scripts/migrations/cleanup_names_and_junk_v3_2_7_48.py [--dry-run] [--yes]
"""

import argparse
import importlib.util
import json
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sheet_cleanup_v3_2_7_47 import (  # noqa: E402
    SHEET_ID, SHEET_TAB, refresh_access_token, fetch_sheet, batch_update, delete_rows,
)

NARRATION_PREFIXES = ("呢張相", "呢張圖", "這張相", "這張圖", "呢個相",
                      "相顯示", "圖顯示", "圖片顯示", "相片顯示")
ERROR_MARKERS = ("APiyi", "vision 失敗", "gpt-4o vision")
PLACEHOLDERS = ("scan", "Unknown", "unknown", "食物", "未識別菜式")
UNKNOWN = "未識別菜式"


def load_gym_web():
    """Load gym_web.py as a module so the AI extractor can be reused."""
    spec = importlib.util.spec_from_file_location("gwmod", REPO / "gym_web.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gwmod"] = mod
    spec.loader.exec_module(mod)
    return mod


def cell(r, i):
    return (r[i] if len(r) > i else "") or ""


def is_narration(d: str) -> bool:
    return any(d.startswith(p) for p in NARRATION_PREFIXES) or "顯示" in d[:12]


def is_error_name(d: str) -> bool:
    return d.startswith("（") or any(m in d for m in ERROR_MARKERS)


def classify(rows: list):
    """Split data rows into: rename candidates, junk, exact duplicates."""
    rename, junk, dupes = [], [], []
    seen = {}
    for i, r in enumerate(rows[1:], start=2):
        d = cell(r, 3).strip()
        try:
            kcal = int(float(cell(r, 5) or 0))
        except ValueError:
            kcal = 0

        sig = tuple(cell(r, c).strip() for c in range(11))  # A-K (v3.2.7.48+)
        if any(sig) and sig in seen:
            dupes.append((i, seen[sig]))
            continue
        seen[sig] = i

        no_usable_name = (not d) or d in PLACEHOLDERS or (is_error_name(d) and not is_narration(d))
        if kcal == 0 and no_usable_name:
            junk.append((i, d, kcal))
        elif is_narration(d) or is_error_name(d) or d in PLACEHOLDERS or not d:
            # Carries real macros (or a real but polluted name) -> re-extract,
            # never delete.
            rename.append((i, d, kcal))
    return rename, junk, dupes


def main():
    ap = argparse.ArgumentParser(description="Clean col D names + drop junk rows")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    print(f"=== Name cleanup + junk removal ({'DRY-RUN' if args.dry_run else 'LIVE'}) ===")
    access = refresh_access_token()
    rows = fetch_sheet(access)
    print(f"Loaded {len(rows)} rows (header + {len(rows) - 1} data)\n")

    rename, junk, dupes = classify(rows)
    print(f"To re-extract via AI : {len(rename)}")
    print(f"Junk (kcal=0, no name): {len(junk)}  rows={[i for i, _, _ in junk]}")
    print(f"Exact duplicates      : {len(dupes)}  rows={[i for i, _ in dupes]}\n")

    gw = load_gym_web()
    edits, resolved = [], []
    for i, d, kcal in rename:
        name = ""
        try:
            name = (gw._extract_dish_name_ai(d) or "").strip()
        except Exception as e:
            print(f"  ! row {i}: AI error {type(e).__name__}: {e}")
        # Never fabricate: an empty AI result becomes the explicit unknown
        # label. Also reject a name carrying the U+FFFD replacement char —
        # the source text was truncated mid-codepoint at 120 chars, so the
        # AI can echo a broken glyph (e.g. "燒肉、�頭" for 饅頭).
        if not name or "�" in name:
            name = ""
        final = name or UNKNOWN
        resolved.append((i, d, final, kcal))
        edits.append({"range": f"{SHEET_TAB}!D{i}", "values": [[final]]})
        print(f"  row {i:>4} kcal={kcal:>5} | {d[:38]!r} -> {final!r}")

    n_named = sum(1 for _, _, f, _ in resolved if f != UNKNOWN)
    print(f"\nAI resolved {n_named}/{len(resolved)} names; "
          f"{len(resolved) - n_named} fell back to {UNKNOWN}")

    delete_idx = [i - 1 for i, _, _ in junk] + [i - 1 for i, _ in dupes]

    if args.dry_run:
        print(f"\n[DRY-RUN] would rewrite {len(edits)} names and delete "
              f"{len(delete_idx)} rows")
        return

    if not args.yes:
        if input(f"\nRewrite {len(edits)} names, delete {len(delete_idx)} rows? [y/N] ").lower() != "y":
            print("Aborted.")
            sys.exit(0)

    if edits:
        batch_update(access, edits)
        print(f"  ✓ Rewrote {len(edits)} dish names")
    if delete_idx:
        delete_rows(access, delete_idx)
        print(f"  ✓ Deleted {len(delete_idx)} junk/duplicate rows")

    after = fetch_sheet(access)
    print(f"\nAfter: {len(after) - 1} data rows (was {len(rows) - 1})")


if __name__ == "__main__":
    main()
