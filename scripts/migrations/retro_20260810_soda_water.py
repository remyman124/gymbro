"""Retroactively fix the soda water scan entry that got a generic-reject name.

Background: On 2026-08-10 14:55, a scan of a Chang soda water bottle
landed in scan_log with:
  - name='無法提供小票或其他餐點資訊'  (generic reject)
  - calories/protein/carbs/fat = 0  (model refused to commit)
  - vision_short='這張相顯示一支蘇打水樽，品牌為 "Chang"...'  (narration
    in the description — this is the bug the user complained about)

The vision_short clearly identifies the dish as 蘇打水 (soda water).
Standard nutrition for a 330ml bottle of unflavoured soda water is
~0 kcal, 0g everything. We retroactively:
  1. Set name='蘇打水'
  2. Set reasonable nutrition (all 0 except sodium ~5mg from mineral content)
  3. Clean vision_short to just the dish + brand (no narration)
  4. Re-grade the coach comment
"""
import json
from pathlib import Path

SCAN_LOG = Path("/home/work/.hermes/food_scan_log.json")

# Target entry: scan_index 46
TARGET_INDEX = 46

# Clean name from vision_short
NEW_NAME = "蘇打水"

# Standard nutrition for 330ml unflavoured soda water (Chang brand)
NEW_NUTRITION = {
    "calories": 0,
    "protein": 0,
    "carbs": 0,
    "fat": 0,
    "fiber": 0,
    "sugar": 0,
    "sodium": 5,       # ~5mg per 330ml from mineral content
    "sat_fat": 0,
    "trans_fat": 0,
    "vit_c": 0,
    "iron": 0,
    "calcium": 0,
}

NEW_VISION_SHORT = "蘇打水 (Chang 330ml) — 透明樽裝，無糖無卡路里"

NEW_COACH_COMMENT = {
    "grade": "A",
    "comment": "蘇打水零卡零糖，極低鈉，係補水嘅好選擇",
    "suggestions": ["運動後飲可以補水", "冇糖配方唔會影響減重"],
    "rationale": "soda water — 0 kcal, 0g 糖, 極低鈉. 飲品熱量零，係最健康嘅補水選項",
}


def main():
    data = json.loads(SCAN_LOG.read_text())
    target = next((e for e in data if e.get("scan_index") == TARGET_INDEX), None)
    if not target:
        print(f"ERROR: no entry with scan_index={TARGET_INDEX}")
        return 1

    if target.get("name") == NEW_NAME:
        print(f"Already fixed: scan_index={TARGET_INDEX}")
        return 0

    print(f"Before:")
    print(f"  name: {target.get('name')!r}")
    print(f"  calories: {target.get('calories')}")
    print(f"  vision_short: {target.get('vision_short', '')[:80]!r}...")

    # Apply retro fix
    target["name"] = NEW_NAME
    for k, v in NEW_NUTRITION.items():
        target[k] = v
    target["vision_short"] = NEW_VISION_SHORT
    target["coach_comment"] = NEW_COACH_COMMENT
    target["retro_fixed"] = True
    target["retro_fix_note"] = "v3.2.7.17: retro-fit name from vision_short (was '無法提供小票或其他餐點資訊' generic-reject)"

    SCAN_LOG.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print()
    print(f"After:")
    print(f"  name: {target['name']!r}")
    print(f"  calories: {target['calories']}, sodium: {target['sodium']}mg")
    print(f"  vision_short: {target['vision_short']!r}")
    print(f"  grade: {target['coach_comment']['grade']}")
    print()
    print(f"OK: scan_log retro-fixed at {SCAN_LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
