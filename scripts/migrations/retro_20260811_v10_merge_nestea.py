"""Merge the 2 Nestea entries (23:19 + 23:54) into one.

Jim OOB 2026-08-11 'there are still 2, they should be the same' — the
user drank ONE Nestea but the system has 2 entries because:
- 23:19 scan: vision returned '總結' (junk), retro renamed to
  '檸檬茶 (Nestea)' but couldn't re-extract nutrition → 0 kcal at first
- 23:54 scan: user re-photographed the same can from a different angle,
  vision returned '無小票' (junk), retro re-extracted to
  '罐 330ml 嘅檸檬茶' 177 kcal

Both photos are byte-different (different angle) so the v8 md5-dedup
didn't catch them. They are semantically the same drink.

Decision:
- Keep the 23:19 entry (timestamp = when the user drank it).
- Use 23:54's photo (235410 — front view, lemon label visible, better
  display) but rename the image file to keep filename ↔ time consistent.
  Actually no — keep 23:19's image (filename matches timestamp).
- Use 23:54's retro-extracted nutrition (177 kcal — re-extracted from
  the same physical can, more accurate than 23:19's 160 kcal estimate).
- Drop the 23:54 entry entirely (later retry, no audit value once we
  have its better nutrition).
"""
import argparse
import json
from pathlib import Path

SCAN_LOG = Path("/home/work/.hermes/food_scan_log.json")
NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")

KEEP_IMG = "scan_20260810_231925.jpg"  # 23:19 (original photo, when drank)
DROP_IMG = "scan_20260810_235410.jpg"  # 23:54 (retry photo, to remove)

# Nutrition from the 23:54 retro extraction (177 kcal — pplx re-extracted
# from the same physical can)
NUTRITION = {
    "calories": 177,
    "protein": 0,
    "carbs": 43,
    "fat": 0,
}


def main(apply=False):
    sl = json.loads(SCAN_LOG.read_text())
    nl = json.loads(NUTRITION_LOG.read_text())

    # Update 23:19 scan_log entry with 23:54's nutrition
    for e in sl:
        if KEEP_IMG in (e.get("image_path") or ""):
            old = (e.get("calories"), e.get("protein"), e.get("carbs"))
            for k, v in NUTRITION.items():
                e[k] = v
            e["retro_fixed"] = True
            e["retro_fix_note"] = "v3.2.7.31: merged 177 kcal from 23:54 retry (same physical can)"
            print(f"  scan_log 23:19: kcal {old[0]} → {e['calories']}, "
                  f"P {old[1]} → {e['protein']}, C {old[2]} → {e['carbs']}")
            break

    # Update 23:19 nutrition_log meal
    for m in nl["meals"]:
        if KEEP_IMG in (m.get("image_saved_to") or ""):
            old = (m.get("calories"), m.get("protein"), m.get("carbs"))
            for k, v in NUTRITION.items():
                m[k] = v
            m["retro_fixed"] = True
            m["retro_fix_note"] = "v3.2.7.31: merged 177 kcal from 23:54 retry (same physical can)"
            print(f"  nutrition_log 23:19: kcal {old[0]} → {m['calories']}, "
                  f"P {old[1]} → {m['protein']}, C {old[2]} → {m['carbs']}")
            break

    # Drop the 23:54 scan_log entry
    kept = [e for e in sl if DROP_IMG not in (e.get("image_path") or "")]
    removed_scan = len(sl) - len(kept)
    print(f"  scan_log: dropped {removed_scan} entry (image {DROP_IMG})")

    # Drop the 23:54 nutrition_log meal
    before = len(nl["meals"])
    nl["meals"] = [m for m in nl["meals"]
                   if DROP_IMG not in (m.get("image_saved_to") or "")]
    removed_meal = before - len(nl["meals"])
    print(f"  nutrition_log: dropped {removed_meal} meal (image {DROP_IMG})")

    print(f"\n  scan_log: {len(sl)} → {len(kept)}")
    print(f"  nutrition_log meals: {before} → {len(nl['meals'])}")

    if not apply:
        print("\nDRY RUN — pass --apply to write changes")
        return 0

    SCAN_LOG.write_text(json.dumps(kept, ensure_ascii=False, indent=2))
    NUTRITION_LOG.write_text(json.dumps(nl, ensure_ascii=False, indent=2))
    print("\nWrote scan_log + nutrition_log")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    raise SystemExit(main(apply=ap.parse_args().apply))