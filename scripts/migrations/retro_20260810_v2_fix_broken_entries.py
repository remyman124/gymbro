"""Retroactively fix the remaining broken entries discovered after
removing the 6 test artifacts (retro_20260810_remove_test_entries.py).

Three categories of fixes:

1. **Fix-name**: nutrition_log[475] soda water — image is a real Chang
   soda water bottle (scan_log #46 already correctly says 蘇打水). The
   nutrition_log entry has the AI's generic-reject placeholder
   '無法提供小票或其他餐點資訊' as the name. Update to 蘇打水 with
   the same nutrition as scan_log #46 (0 kcal, 5mg sodium).

2. **Fix-name**: scan_log #48 + nutrition_log[477] — image shows a real
   Hainan chicken rice (the rice portion of the meal that scan_log #47
   at 21:17:05 already correctly labeled as 海南雞). The empty-name
   entries are the auto-commit artifacts from v3.2.7.10-era code that
   committed before v3.2.7.19's narration guard. Fill in 海南雞飯
   with reasonable rice-side nutrition (~350 kcal, 6g protein, 75g
   carbs, 2g fat, 400mg sodium).

3. **Fix-name**: nutrition_log[467] (2026-08-09 20:26:54) — image shows
   a real bowl of white rice with tofu puffs and Chinese greens in
   brown sauce. Empty name. Fill in 米飯配豆腐青菜 with reasonable
   nutrition (~460 kcal, 18g protein, 70g carbs, 12g fat, 700mg sodium).

4. **Remove-test-artifacts**: nutrition_log entries [445-459] —
   15 entries from 2026-08-09 18:26-18:35 all have empty names and
   kcal=0. Images are black/empty (vision API test artifacts) except
   for the 18:33 paper cup. The user (Jim) confirmed these are test
   artifacts from a vision API stress test, not real meals.

Run as a dry-run first (no args), then with `--apply` to write.
"""
import argparse
import json
from pathlib import Path

SCAN_LOG = Path("/home/work/.hermes/food_scan_log.json")
NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")


# ── Test image fragments for the 18:26-18:35 test session ────────────────
# Empty/black images or paper cup — all kcal=0, all empty names. These
# came from a vision API stress test where the user (Jim) was checking
# how the AI responded to non-food images.
TEST_ARTIFACT_FRAGMENTS_20260809_18XX = (
    "scan_20260809_182622", "scan_20260809_182641",
    "scan_20260809_182650", "scan_20260809_182658",
    "scan_20260809_182713", "scan_20260809_183206",
    "scan_20260809_183215", "scan_20260809_183225",
    "scan_20260809_183232", "scan_20260809_183246",
    "scan_20260809_183300", "scan_20260809_183435",
    "scan_20260809_183444", "scan_20260809_183451",
    "scan_20260809_183457",
)


def is_test_artifact_18xx(entry):
    """True if the entry is a vision-API test artifact from 2026-08-09
    18:26-18:35. The whole window is one stress-test session where the
    user (Jim) was checking how the AI responded to non-food images.
    All 15 entries have kcal=0 (nothing was actually eaten). Some have
    empty names, some have AI narration leaked in ('係一張純色圖片',
    '無法提供更詳細資料', '對不起', '0'). All are test artifacts."""
    img = entry.get("image_saved_to", "") or entry.get("image_path", "") or entry.get("image_url", "")
    if not any(f in img for f in TEST_ARTIFACT_FRAGMENTS_20260809_18XX):
        return False
    kcal = entry.get("calories", 0) or 0
    return kcal == 0


def fix_soda_water(meals):
    """nutrition_log[475]: name '無法提供小票...' → '蘇打水' (match scan_log #46)."""
    for m in meals:
        if "scan_20260810_145524" in m.get("image_saved_to", ""):
            m["name"] = "蘇打水"
            m["calories"] = 0
            m["protein"] = 0
            m["carbs"] = 0
            m["fat"] = 0
            m["fiber"] = 0
            m["sugar"] = 0
            m["sodium"] = 5
            m["sat_fat"] = 0
            m["trans_fat"] = 0
            m["vit_c"] = 0
            m["iron"] = 0
            m["calcium"] = 0
            m["coach_comment"] = {
                "grade": "A",
                "comment": "蘇打水零卡零糖，極低鈉，係補水嘅好選擇",
                "suggestions": ["運動後飲可以補水", "冇糖配方唔會影響減重"],
                "rationale": "soda water — 0 kcal, 0g 糖, 極低鈉. 飲品熱量零，係最健康嘅補水選項",
            }
            m["retro_fixed"] = True
            m["retro_fix_note"] = "v3.2.7.19: retro-fit name from scan_log (was '無法提供小票或其他餐點資訊' generic-reject)"
            return [m]
    return []


def fix_hainan_chicken_rice(scan_log, meals):
    """scan_log #48 + nutrition_log[477]: empty name → '海南雞飯' (the
    rice portion of the meal at #47 which was correctly labeled 海南雞).
    The image clearly shows Hainan chicken on the left and rice on the
    right in a takeout box. This is the rice-side portion of the meal.
    """
    fixed = []
    for e in scan_log:
        if e.get("scan_index") == 48:
            e["name"] = "海南雞飯"
            e["calories"] = 350
            e["protein"] = 6
            e["carbs"] = 75
            e["fat"] = 2
            e["fiber"] = 1
            e["sugar"] = 0
            e["sodium"] = 400
            e["sat_fat"] = 0
            e["trans_fat"] = 0
            e["vit_c"] = 0
            e["iron"] = 1
            e["calcium"] = 15
            e["vision_short"] = "海南雞飯 (米飯部分) — 配海南雞嘅油飯"
            e["restaurant_chain"] = "Hainanese chicken rice shop"
            e["coach_comment"] = {
                "grade": "B",
                "comment": "海南雞飯嘅米飯部分，油飯配海南雞好味但熱量唔低",
                "suggestions": ["如果想減碳水可以減半飯量", "配多啲菜平衡一下"],
                "rationale": "海南雞飯米飯部分 — ~350 kcal, 6g 蛋白質, 75g 碳水. 米飯用雞油煮會高少少脂肪",
            }
            e["retro_fixed"] = True
            e["retro_fix_note"] = "v3.2.7.19: retro-fit name from image (was empty, auto-commit from v3.2.7.10-era)"
            fixed.append(e)
            break
    for m in meals:
        if "scan_20260810_211714" in m.get("image_saved_to", ""):
            m["name"] = "海南雞飯"
            m["calories"] = 350
            m["protein"] = 6
            m["carbs"] = 75
            m["fat"] = 2
            m["fiber"] = 1
            m["sugar"] = 0
            m["sodium"] = 400
            m["sat_fat"] = 0
            m["trans_fat"] = 0
            m["vit_c"] = 0
            m["iron"] = 1
            m["calcium"] = 15
            m["coach_comment"] = {
                "grade": "B",
                "comment": "海南雞飯嘅米飯部分，油飯配海南雞好味但熱量唔低",
                "suggestions": ["如果想減碳水可以減半飯量", "配多啲菜平衡一下"],
                "rationale": "海南雞飯米飯部分 — ~350 kcal, 6g 蛋白質, 75g 碳水. 米飯用雞油煮會高少少脂肪",
            }
            m["retro_fixed"] = True
            m["retro_fix_note"] = "v3.2.7.19: retro-fit name from image (was empty, auto-commit from v3.2.7.10-era)"
            fixed.append(m)
            break
    return fixed


def fix_rice_tofu(meals):
    """nutrition_log[467] 2026-08-09 20:26:54: empty name → '米飯配豆腐青菜'.
    Image shows white rice in a bowl + tofu puffs + Chinese greens in
    brown sauce on a takeout plate. Typical HK-style 一飯一餸 meal.
    """
    for m in meals:
        if "scan_20260809_202654" in m.get("image_saved_to", ""):
            m["name"] = "米飯配豆腐青菜"
            m["calories"] = 460
            m["protein"] = 18
            m["carbs"] = 70
            m["fat"] = 12
            m["fiber"] = 4
            m["sugar"] = 3
            m["sodium"] = 700
            m["sat_fat"] = 2
            m["trans_fat"] = 0
            m["vit_c"] = 25
            m["iron"] = 3
            m["calcium"] = 150
            m["restaurant_chain"] = "HK 一飯一餸"
            m["coach_comment"] = {
                "grade": "A",
                "comment": "一飯一餸嘅均衡晚餐，豆腐有蛋白質，青菜有纖維，米飯提供能量",
                "suggestions": ["已經好均衡，繼續保持", "如果想多啲蛋白質可以加多件豆腐"],
                "rationale": "米飯配豆腐青菜 — ~460 kcal, 18g 蛋白質, 70g 碳水, 12g 脂肪. 蛋白質主要嚟自豆腐，蔬菜提供纖維同維他命",
            }
            m["retro_fixed"] = True
            m["retro_fix_note"] = "v3.2.7.19: retro-fit name from image (was empty, v2.2-scan)"
            return [m]
    return []


def remove_test_artifacts(meals):
    """Remove the 15 nutrition_log entries from 2026-08-09 18:26-18:35
    that are all kcal=0 + empty name + vision-API test images."""
    removed = []
    kept = []
    for m in meals:
        if is_test_artifact_18xx(m):
            removed.append(m)
        else:
            kept.append(m)
    return removed, kept


def main(apply=False):
    scan_log = json.loads(SCAN_LOG.read_text())
    nl = json.loads(NUTRITION_LOG.read_text())
    meals = nl.get("meals", [])
    before_scan = len(scan_log)
    before_meal = len(meals)

    # 1. Fix soda water
    soda_fixed = fix_soda_water(meals)
    # 2. Fix Hainan chicken rice
    hainan_fixed = fix_hainan_chicken_rice(scan_log, meals)
    # 3. Fix rice+tofu
    rice_fixed = fix_rice_tofu(meals)
    # 4. Remove test artifacts
    removed, kept = remove_test_artifacts(meals)
    meals = kept

    print(f"=== Before ===")
    print(f"  scan_log: {before_scan}")
    print(f"  nutrition_log meals: {before_meal}")
    print()
    print(f"=== Fixes ===")
    print(f"  Soda water fixed: {len(soda_fixed)} entry")
    print(f"  Hainan chicken rice fixed: {len(hainan_fixed)} entries (scan_log + nutrition_log)")
    print(f"  Rice+tofu fixed: {len(rice_fixed)} entry")
    print(f"  Test artifacts removed: {len(removed)} entries")
    print()
    print(f"=== After ===")
    print(f"  scan_log: {len(scan_log)} (no change)")
    print(f"  nutrition_log meals: {len(meals)} ({before_meal} → {len(meals)})")

    if removed:
        print()
        print("=== Removed test artifacts ===")
        for m in removed:
            print(f"  {m.get('timestamp_iso','')[:19]} img={m.get('image_saved_to','')[-35:]} name={m.get('name')!r}")

    if not apply:
        print()
        print("DRY RUN — pass --apply to write changes")
        return 0

    nl["meals"] = meals
    NUTRITION_LOG.write_text(json.dumps(nl, ensure_ascii=False, indent=2))
    SCAN_LOG.write_text(json.dumps(scan_log, ensure_ascii=False, indent=2))
    print()
    print("Wrote scan_log + nutrition_log")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = ap.parse_args()
    raise SystemExit(main(apply=args.apply))
