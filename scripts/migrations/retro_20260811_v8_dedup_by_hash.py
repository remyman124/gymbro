"""Deduplicate scan_log + nutrition_log entries by image md5.

Jim OOB 2026-08-11 'now there are 3 Nestea' — same photo scanned
multiple times when vision AI fails and user retries, producing 2-3
identical entries with junk names.

For each md5 group:
- Keep the EARLIEST entry (the original scan attempt — usually has a
  bad name + 0 kcal because vision failed).
- If a later entry has a real name + kcal > 0, MERGE that data into
  the kept entry (better name + nutrition wins).
- Drop all other duplicate entries from scan_log + nutrition_log.

3 sets of duplicates found by md5:
- 2026-07-23 22:38 MiniMax vision failure (scan#0, #1)
- 2026-07-31 18:01 生椰拿鐵 (scan#4, #15)
- 2026-08-10 23:54 罐 330ml 嘅檸檬茶 (scan#50, #51)
"""
import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

SCAN_LOG = Path("/home/work/.hermes/food_scan_log.json")
NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")


def md5_of(path: str) -> str | None:
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except Exception:
        return None


def main(apply=False):
    sl = json.loads(SCAN_LOG.read_text())
    nl = json.loads(NUTRITION_LOG.read_text())
    meals = nl.get("meals", [])

    # Group scan_log entries by md5
    by_hash = defaultdict(list)
    no_hash = []
    for i, e in enumerate(sl):
        img = e.get("image_path") or ""
        h = md5_of(img) if img else None
        if h:
            by_hash[h].append(i)
        else:
            no_hash.append(i)

    # Plan: for each md5 group with >1 entry, keep the earliest index,
    # merge kcal/name from the best later entry, and remove the others.
    scan_log_removed = []
    scan_log_merged = []
    for h, indices in by_hash.items():
        if len(indices) <= 1:
            continue
        # sort by timestamp_iso to find earliest
        indices_sorted = sorted(indices, key=lambda i: sl[i].get("timestamp_iso", ""))
        keep_idx = indices_sorted[0]
        keep = sl[keep_idx]
        # find the BEST later entry (most kcal, then earliest timestamp)
        best = keep
        for idx in indices_sorted[1:]:
            cand = sl[idx]
            if (cand.get("calories") or 0) > (best.get("calories") or 0):
                best = cand
        # merge best fields into keep (only if keep lacks them)
        merged = False
        for k in ("name", "calories", "protein", "carbs", "fat", "fiber",
                  "sugar", "sodium", "sat_fat", "trans_fat", "vit_c",
                  "iron", "calcium", "restaurant_chain", "vision_short"):
            if (not keep.get(k) or keep.get(k) == 0 or keep.get(k) == "scan") \
               and best.get(k) not in (None, 0, "", "scan"):
                keep[k] = best[k]
                merged = True
        if merged:
            keep["retro_fixed"] = True
            keep["retro_fix_note"] = "v3.2.7.30: merged better nutrition from later dup scan"
            scan_log_merged.append((keep_idx, sl[keep_idx].get("name"), best.get("name")))

        # mark the later indices for removal
        for idx in indices_sorted[1:]:
            scan_log_removed.append((idx, sl[idx].get("timestamp_iso", "")[:19],
                                     sl[idx].get("name"), sl[idx].get("calories")))

    kept_scan = [e for i, e in enumerate(sl)
                 if i not in {idx for idx, *_ in scan_log_removed}]

    # Now find nutrition_log meals referencing the same image filenames
    # and drop them.
    removed_image_names = set()
    for idx, *_ in scan_log_removed:
        img = sl[idx].get("image_path") or ""
        if img:
            removed_image_names.add(Path(img).name)

    kept_meals = []
    removed_meals = []
    for m in meals:
        img = m.get("image_saved_to") or ""
        bn = Path(img).name if img else ""
        if bn and bn in removed_image_names:
            removed_meals.append((m.get("time", ""), m.get("name"),
                                  m.get("calories"), bn))
            continue
        kept_meals.append(m)

    print(f"=== scan_log ===")
    print(f"  md5 groups with dupes: {sum(1 for v in by_hash.values() if len(v) > 1)}")
    print(f"  removed: {len(scan_log_removed)}")
    for idx, ts, name, kcal in scan_log_removed:
        print(f"    [{idx}] {ts} {name!r} {kcal} kcal")
    print(f"  merged: {len(scan_log_merged)}")
    for idx, before, after in scan_log_merged:
        print(f"    [{idx}] {before!r} ← {after!r}")
    print(f"  {len(sl)} → {len(kept_scan)}")
    print()
    print(f"=== nutrition_log ===")
    print(f"  removed: {len(removed_meals)}")
    for t, name, kcal, img in removed_meals:
        print(f"    {t} {name!r} {kcal} kcal  ({img})")
    print(f"  {len(meals)} → {len(kept_meals)}")

    if not apply:
        print("\nDRY RUN — pass --apply to write changes")
        return 0

    SCAN_LOG.write_text(json.dumps(kept_scan, ensure_ascii=False, indent=2))
    NUTRITION_LOG.write_text(json.dumps({"meals": kept_meals}, ensure_ascii=False, indent=2))
    print("\nWrote scan_log + nutrition_log")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    raise SystemExit(main(apply=ap.parse_args().apply))