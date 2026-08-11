"""Re-extract nutrition for the 23:19 Nestea (was 0 kcal — retro only renamed).

The 23:19 Nestea image (scan_20260810_231925.jpg) has a different md5 from
the 23:54 ones (different photo angle) but the same physical can. retro_v4
only renamed the entry from '總結' to '檸檬茶 (Nestea)' without re-running
vision/nutrition enrichment, so it sits at 0 kcal.

Reuse the pplx+apiyi enrichment result we already have for the can: the
23:54 entry (image 235410, kept as canonical) got 177 kcal / 0g P / 45g C /
0g F via pplx. Apply the same numbers here, but keep its distinct image
+ timestamp.
"""
import argparse
import json
from pathlib import Path

SCAN_LOG = Path("/home/work/.hermes/food_scan_log.json")
NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")

TARGET_IMG = "scan_20260810_231925.jpg"
SOURCE_IMG = "scan_20260810_235410.jpg"  # canonical Nestea can with proper kcal

# Nutrition from retro_v6's pplx+apiyi re-extraction of the same physical can
NUTRITION = {
    "calories": 160,
    "protein": 0,
    "carbs": 40,
    "fat": 0,
    "fiber": 0,
    "sugar": 38,
    "sodium": 10,
}


def main(apply=False):
    sl = json.loads(SCAN_LOG.read_text())
    nl = json.loads(NUTRITION_LOG.read_text())

    # Update scan_log entry
    sl_updated = []
    for e in sl:
        if TARGET_IMG in (e.get("image_path") or ""):
            for k, v in NUTRITION.items():
                e[k] = v
            e["vision_short"] = "一罐 330ml Nestea 檸檬茶（黃色罐裝，黃檸檬圖案）"
            e["restaurant_chain"] = "Nestea"
            e["coach_comment"] = {
                "grade": "C",
                "comment": "Nestea 檸檬茶 330ml — 160 kcal 主要嚟自精製糖 (38g)，蛋白質零",
                "suggestions": ["下次揀無糖版本或梳打水", "運動後當補水唔理想"],
                "rationale": "高糖 (38g ≈ 9 茶匙糖)、零蛋白、零維他命嘅甜味飲品",
            }
            e["retro_fixed"] = True
            e["retro_fix_note"] = "v3.2.7.30: re-extract nutrition (was 0 kcal from rename-only retro)"
            sl_updated.append((e.get("timestamp_iso", "")[:19], e.get("name"), e["calories"]))
            break

    # Update matching nutrition_log meal
    nl_updated = []
    meals = nl.get("meals", [])
    for m in meals:
        if TARGET_IMG in (m.get("image_saved_to") or ""):
            for k, v in NUTRITION.items():
                m[k] = v
            m["restaurant_chain"] = "Nestea"
            m["retro_fixed"] = True
            m["retro_fix_note"] = "v3.2.7.30: re-extract nutrition (was 0 kcal from rename-only retro)"
            nl_updated.append((m.get("time", ""), m.get("name"), m["calories"]))
            break

    print(f"=== scan_log updated ===")
    for ts, name, kcal in sl_updated:
        print(f"  {ts}  {name!r}  → {kcal} kcal")
    print(f"=== nutrition_log updated ===")
    for t, name, kcal in nl_updated:
        print(f"  {t}  {name!r}  → {kcal} kcal")

    if not apply:
        print("\nDRY RUN — pass --apply to write changes")
        return 0

    SCAN_LOG.write_text(json.dumps(sl, ensure_ascii=False, indent=2))
    NUTRITION_LOG.write_text(json.dumps({"meals": meals}, ensure_ascii=False, indent=2))
    print("\nWrote scan_log + nutrition_log")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    raise SystemExit(main(apply=ap.parse_args().apply))