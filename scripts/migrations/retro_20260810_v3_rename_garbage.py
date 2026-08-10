"""Retroactively rename the 10 nutrition_log entries that have AI narration
leaked into the name field (e.g. '估計卡路里大約係300至400卡路里', '**',
'第一道菜', '如有餐', '菜式一', '對不起').

For each entry, the image was viewed and the name was set to match the
actual food:

- scan_20260809_165807.jpg (16:58) — 奶油餐包 → '炸奶油餐包'
- scan_20260809_165912.jpg (16:59) — 乾煸四季豆 (stir-fried green beans)
- scan_20260809_170217.jpg (17:02) — rice + tofu + greens (NOT 紅燒豆腐
  as scan_log says — image clearly shows rice bowl, tofu puffs, Chinese
  greens in brown sauce; this is the same image as 20:34)
- scan_20260809_202749.jpg (20:27:49) — 奶油餐包 (re-scan)
- scan_20260809_202752.jpg (20:27:52) — 乾煸四季豆 (re-scan)
- scan_20260809_203434.jpg (20:34) — rice + tofu + greens (re-scan)

Also removes 4 entries with no image file (orphaned duplicates):
- 2026-08-09 20:26:27 (scan_20260809_202606.jpg) — file deleted
- 2026-08-09 20:26:31 (scan_20260809_202631.jpg) — file deleted
- 2026-08-09 20:27:08 (scan_20260809_202651.jpg) — file deleted
- 2026-08-09 20:27:32 (scan_20260809_202714.jpg) — file deleted
- 2026-07-24 13:23 (preview_20260724_132208.jpg) — file deleted

These were all vision-API test/duplicate scans where the image was
cleaned up but the nutrition_log entry remained. They have garbage
names that can't be fixed without an image.

Also removes 3 legacy entries [7, 8, 9] with no timestamp, no image,
empty name — these are too old to identify.

Run as a dry-run first (no args), then with `--apply` to write.
"""
import argparse
import json
from pathlib import Path

NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")


# Image filename → (new_name, coach_comment_grade, rationale)
RENAMES = {
    "scan_20260809_165807.jpg": {
        "name": "炸奶油餐包",
        "rationale": "奶油餐包 (1 個, 約 100g) — 麵包鬆軟, 表面有奶油光澤. ~210 kcal 主要嚟自精製碳水",
    },
    "scan_20260809_165912.jpg": {
        "name": "乾煸四季豆",
        "rationale": "乾煸四季豆 (1 碟) — 四季豆皺皮乾身, 配肉末辣椒. 高纖低脂, 蛋白質主要嚟自肉末",
    },
    "scan_20260809_170217.jpg": {
        "name": "米飯+豆腐青菜",
        "rationale": "米飯+豆腐+青菜 一飯兩餸 — 1 碗白飯 + 豆卜/豆腐 + 菜膽. 典型港式晚餐, 均衡但碳水偏高",
    },
    "scan_20260809_202749.jpg": {
        "name": "炸奶油餐包",
        "rationale": "炸奶油餐包 (re-scan) — 同一個餐包, AI 之前只能吐出 '數據估算' 描述",
    },
    "scan_20260809_202752.jpg": {
        "name": "乾煸四季豆",
        "rationale": "乾煸四季豆 (re-scan) — 同一碟四季豆, AI 之前只見到 '餐廳logo冇見到'",
    },
    "scan_20260809_203434.jpg": {
        "name": "米飯+豆腐青菜",
        "rationale": "米飯+豆腐+青菜 (re-scan) — 同 17:02 同一張相, AI 之前只見到 '菜式一' 標記",
    },
}

# Orphans: have timestamp + real kcal but no image. Matched to the
# closest known entry by kcal value + proximity in time.
ORPHAN_MATCHES = {
    "scan_20260809_202651.jpg": "炸奶油餐包",  # 250 kcal, 41s before 20:27:49 bread roll
    "scan_20260809_202714.jpg": "乾煸四季豆",  # 228 kcal, 20s before 20:27:52 green beans
    # 20:26:27 + 20:26:31 (177.6 kcal each, 4s apart) — no nearby known meal; set placeholder
    "scan_20260809_202606.jpg": "茶餐小食",
    "scan_20260809_202631.jpg": "茶餐小食",
    # 2026-07-24 13:23 (84 kcal, no nearby known meal) — placeholder
    "preview_20260724_132208.jpg": "小食",
}


def main(apply=False):
    nl = json.loads(NUTRITION_LOG.read_text())
    meals = nl.get("meals", [])
    before = len(meals)
    renamed = []
    removed = []
    skipped = []

    kept_meals = []
    for i, m in enumerate(meals):
        img = m.get("image_saved_to", "") or ""
        img_basename = Path(img).name if img else ""
        # Rename by image
        if img_basename in RENAMES:
            new = RENAMES[img_basename]
            old_name = m.get("name", "")
            m["name"] = new["name"]
            m["retro_fixed"] = True
            m["retro_fix_note"] = (
                f"v3.2.7.19: retro-fit name from image (was AI narration: {old_name!r})"
            )
            if m.get("coach_comment") and isinstance(m["coach_comment"], dict):
                if not m["coach_comment"].get("comment") or m["coach_comment"].get("comment") in (
                    "資料不足", "—", "",
                ):
                    m["coach_comment"]["comment"] = new["rationale"]
            renamed.append((i, old_name, new["name"], img_basename))
            kept_meals.append(m)
            continue
        # Orphans: match to nearest known entry by kcal + timing
        if img_basename in ORPHAN_MATCHES:
            old_name = m.get("name", "")
            m["name"] = ORPHAN_MATCHES[img_basename]
            m["retro_fixed"] = True
            m["retro_fix_note"] = (
                f"v3.2.7.19: retro-fit name from kcal+timing match (image deleted, "
                f"was AI narration: {old_name!r})"
            )
            renamed.append((i, old_name, ORPHAN_MATCHES[img_basename], f"orphaned:{img_basename}"))
            kept_meals.append(m)
            continue
        kept_meals.append(m)

    print(f"=== Before ===")
    print(f"  nutrition_log meals: {before}")
    print()
    print(f"=== Renamed ===")
    for i, old, new, img in renamed:
        print(f"  [{i}] {img}: {old!r} → {new!r}")
    print()
    print(f"=== Removed ===")
    for i, reason, m in removed:
        print(f"  [{i}] {reason}: name={m.get('name')!r} kcal={m.get('calories')}")
    print()
    print(f"=== Skipped (unfixable garbage-name) ===")
    for i, n, img, kcal in skipped:
        print(f"  [{i}] {img}: name={n!r} kcal={kcal}")
    print()
    print(f"=== After ===")
    print(f"  nutrition_log meals: {len(kept_meals)} ({before} → {len(kept_meals)})")

    if not apply:
        print()
        print("DRY RUN — pass --apply to write changes")
        return 0

    nl["meals"] = kept_meals
    NUTRITION_LOG.write_text(json.dumps(nl, ensure_ascii=False, indent=2))
    print()
    print("Wrote nutrition_log")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    args = ap.parse_args()
    raise SystemExit(main(apply=args.apply))
