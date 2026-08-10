"""Retroactively fix tonight's 2026-08-10 23:xx broken entries.

Jim OOB 2026-08-10 'today food that I logged tonight' — three issues:

1. **23:19 總結** — auto-commit's `_extract_dish_name` fell back to the
   literal word '總結' (summary) from the vision output instead of the
   dish name. The actual image is a Nestea lemon tea can (330ml).
   Rename to '檸檬茶 (Nestea)' with proper nutrition from pplx
   enrichment (160 kcal, 0g P, 40g carbs, 0g fat, 38g sugar, 10mg Na).

2. **23:30 duplicate** — auto-commit wrote 海南雞 680 kcal at 23:29:46,
   then Jim tapped confirm 16s later and scan_commit wrote it again
   at 23:30:02 with the same image. Remove the second one (the
   earlier auto-commit is the canonical entry).

3. **scan_log #50** — also remove from scan_log (matches #464).

The PROGRAM bug (auto-commit + manual confirm double-write) is fixed
in templates/index.html onScanPhotosPicked() — when
data.auto_committed is true, the queue item is marked as 'committed'
immediately and the confirm button is disabled.
"""
import argparse
import json
from pathlib import Path

SCAN_LOG = Path("/home/work/.hermes/food_scan_log.json")
NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")


def main(apply=False):
    nl = json.loads(NUTRITION_LOG.read_text())
    meals = nl.get("meals", [])
    sl = json.loads(SCAN_LOG.read_text())
    before_meal = len(meals)
    before_scan = len(sl)

    renamed = []
    removed_meals = []
    removed_scans = []

    # 1. Fix the 23:19 總結 entry (matches image scan_20260810_231925.jpg)
    for m in meals:
        if "scan_20260810_231925" in m.get("image_saved_to", ""):
            old = m.get("name")
            m["name"] = "檸檬茶 (Nestea)"
            m["calories"] = 160
            m["protein"] = 0
            m["carbs"] = 40
            m["fat"] = 0
            m["fiber"] = 0
            m["sugar"] = 38
            m["sodium"] = 10
            m["sat_fat"] = 0
            m["trans_fat"] = 0
            m["vit_c"] = 0
            m["iron"] = 0
            m["calcium"] = 0
            m["restaurant_chain"] = "Nestea"
            m["coach_comment"] = {
                "grade": "C",
                "comment": "檸檬茶含糖量高 (38g ≈ 9 茶匙糖)，建議改無糖版本",
                "suggestions": ["下次揀無糖檸檬茶或梳打水", "運動後當補水唔理想"],
                "rationale": "Nestea 檸檬茶 330ml — 160 kcal 主要嚟自精製糖, 蛋白質零, 維他命零",
            }
            m["retro_fixed"] = True
            m["retro_fix_note"] = "v3.2.7.21: retro-fit name from image (was AI fallback '總結')"
            renamed.append(("23:19 總結→檸檬茶", old, m["name"], m.get("image_saved_to", "")[-35:]))
            break

    # 2. Remove the 23:30:02 duplicate (matches image scan_20260810_233002.jpg)
    kept_meals = []
    for m in meals:
        if "scan_20260810_233002" in m.get("image_saved_to", ""):
            removed_meals.append(("23:30:02 duplicate", m.get("name"), m.get("calories"), m.get("image_saved_to", "")[-35:]))
            continue
        kept_meals.append(m)

    # 3. Remove the matching scan_log entry
    kept_scan = []
    for e in sl:
        if "scan_20260810_233002" in (e.get("image_path", "") or e.get("image_url", "")):
            removed_scans.append((e.get("scan_index"), e.get("timestamp_iso", "")[:19], e.get("name"), e.get("calories")))
            continue
        kept_scan.append(e)

    print(f"=== Before ===")
    print(f"  scan_log: {before_scan}")
    print(f"  nutrition_log meals: {before_meal}")
    print()
    print(f"=== Renamed ===")
    for label, old, new, img in renamed:
        print(f"  {label}: {old!r} → {new!r}  ({img})")
    print()
    print(f"=== Removed (nutrition_log) ===")
    for label, name, kcal, img in removed_meals:
        print(f"  {label}: name={name!r} kcal={kcal}  ({img})")
    print()
    print(f"=== Removed (scan_log) ===")
    for idx, ts, name, kcal in removed_scans:
        print(f"  #{idx} {ts}: name={name!r} kcal={kcal}")
    print()
    print(f"=== After ===")
    print(f"  scan_log: {len(kept_scan)} (was {before_scan})")
    print(f"  nutrition_log meals: {len(kept_meals)} (was {before_meal})")

    if not apply:
        print()
        print("DRY RUN — pass --apply to write changes")
        return 0

    nl["meals"] = kept_meals
    NUTRITION_LOG.write_text(json.dumps(nl, ensure_ascii=False, indent=2))
    SCAN_LOG.write_text(json.dumps(kept_scan, ensure_ascii=False, indent=2))
    print()
    print("Wrote scan_log + nutrition_log")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    raise SystemExit(main(apply=args.apply))
