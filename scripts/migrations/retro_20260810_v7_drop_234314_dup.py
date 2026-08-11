"""Drop the 23:43 海南雞 duplicate (same image as 21:17).

Jim OOB 2026-08-11 'i can see the chicken rice but duplicated' — both
21:17 and 23:43 entries point to the same photo (scan_20260810_211714.jpg
== scan_20260810_234314.jpg). The user ate 海南雞 once at 21:17; the
23:43 entry is an accidental re-scan (probably tapping confirm twice).

Action:
- Keep 21:17 as the canonical meal (correct timestamp).
- Update 21:17's nutrition from retro_v2's rice-portion estimate
  (350 kcal / 6g P / 75g C / 2g F) to retro_v6's full-meal estimate
  (750 kcal / 38g P / 80g C / 25g F) — the 350 kcal was a mistake,
  the image clearly shows the whole 海南雞 rice box.
- Remove the 23:43 duplicate from scan_log + nutrition_log.
"""
import argparse
import json
from pathlib import Path

SCAN_LOG = Path("/home/work/.hermes/food_scan_log.json")
NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")

DUP_IMAGE = "scan_20260810_234314.jpg"
KEEP_IMAGE = "scan_20260810_211714.jpg"


def main(apply=False):
    sl = json.loads(SCAN_LOG.read_text())
    nl = json.loads(NUTRITION_LOG.read_text())

    before_scan = len(sl)
    before_meals = len(nl.get("meals", []))

    # 1. Update the 21:17 scan_log entry to use full-meal nutrition
    keep_scan_updated = False
    for e in sl:
        if KEEP_IMAGE in (e.get("image_path") or ""):
            old = (e.get("calories"), e.get("protein"), e.get("carbs"), e.get("fat"))
            e["calories"] = 750
            e["protein"] = 38
            e["carbs"] = 80
            e["fat"] = 25
            e["vision_short"] = "海南雞飯（成盒飯 — 白飯 + 海南雞 + 青瓜）"
            e["retro_fixed"] = True
            e["retro_fix_note"] = "v3.2.7.30: full-meal estimate (was rice-portion only); dropped 23:43 duplicate"
            keep_scan_updated = True
            print(f"  scan_log[{e.get('scan_index')}] 21:17: kcal {old[0]} → {e['calories']}, "
                  f"P {old[1]} → {e['protein']}, C {old[2]} → {e['carbs']}, F {old[3]} → {e['fat']}")
            break

    # 2. Update the 21:17 nutrition_log meal to match
    keep_meal_updated = False
    meals = nl.get("meals", [])
    for m in meals:
        if KEEP_IMAGE in (m.get("image_saved_to") or ""):
            old = (m.get("calories"), m.get("protein"), m.get("carbs"), m.get("fat"))
            m["calories"] = 750
            m["protein"] = 38
            m["carbs"] = 80
            m["fat"] = 25
            m["retro_fixed"] = True
            m["retro_fix_note"] = "v3.2.7.30: full-meal estimate (was rice-portion only); dropped 23:43 duplicate"
            keep_meal_updated = True
            print(f"  nutrition_log[{m.get('time')}] 21:17: kcal {old[0]} → {m['calories']}, "
                  f"P {old[1]} → {m['protein']}, C {old[2]} → {m['carbs']}, F {old[3]} → {m['fat']}")
            break

    # 3. Drop the 23:43 duplicate from scan_log
    kept_scan = []
    removed_scan = []
    for e in sl:
        if DUP_IMAGE in (e.get("image_path") or ""):
            removed_scan.append(e)
        else:
            kept_scan.append(e)
    print(f"  scan_log: dropped {len(removed_scan)} duplicate(s) (image {DUP_IMAGE})")

    # 4. Drop the 23:43 duplicate from nutrition_log
    kept_meals = []
    removed_meal = []
    for m in meals:
        if DUP_IMAGE in (m.get("image_saved_to") or ""):
            removed_meal.append(m)
        else:
            kept_meals.append(m)
    print(f"  nutrition_log: dropped {len(removed_meal)} duplicate(s) (image {DUP_IMAGE})")

    print()
    print(f"  scan_log: {before_scan} → {len(kept_scan)}")
    print(f"  nutrition_log meals: {before_meals} → {len(kept_meals)}")

    if not apply:
        print("\nDRY RUN — pass --apply to write changes")
        return 0

    NUTRITION_LOG.write_text(json.dumps({"meals": kept_meals}, ensure_ascii=False, indent=2))
    SCAN_LOG.write_text(json.dumps(kept_scan, ensure_ascii=False, indent=2))
    print("\nWrote scan_log + nutrition_log")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    raise SystemExit(main(apply=ap.parse_args().apply))