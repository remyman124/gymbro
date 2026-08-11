"""Fix today's two bad-name scan entries.

Jim OOB 2026-08-11 'why today recognized food as 菜式 as food name!!!
are you using AI to find the food???? don't use rule/regex' — root
cause was a 4-part bug:
  1. Vision prompts asked for structured markdown output (「菜式：」,
     「份量：」 headers) and the AI returned those headers, leaking
     the section label into the candidate name.
  2. The dish-name AI was asked to extract a name but treated the
     literal section header 「菜式：」 as the dish name. Vision_desc
     was the markdown block; answer was 「菜式：」 (literally).
  3. _extract_dish_name had a regex fallback (`菜式[：:]\s*xxx`)
     that ALSO returned 「菜式」 when vision only had headers.
  4. MiniMax model `MiniMax-M3-highspeed` returns 400 (unknown
     model) — the API never had that name. So vision was returning
     `unable to identify` and the regex extractor got '菜式' instead
     of a real name.

Today two entries got committed before the fix:

- scan #47 (image scan_20260811_152839.jpg, 15:29:08) — name
  was literally '菜式：', 260 kcal, 43g P (chicken breast+sides).
  The vision description matched 雞胸肉 + 青瓜 + 紅蘿蔔 + 芝麻醬,
  so the dish is most likely a sesame chicken salad. Name it
  「芝麻雞胸沙律」 and keep nutrition as-is.

- scan #48 (image scan_20260811_152916.jpg, 15:29:41) — name was
  the full vision_prose '- 馬黛茶（未見到有沖水...）。', 0 kcal.
  Unsweetened yerba mate = 0 kcal is plausible. Name it simply
  「馬黛茶」 and keep 0 kcal.

Fix v3.2.7.32 lands the actual fix (no regex cascade, plaintext
vision prompts, model name corrected to MiniMax-M3). This retro
just cleans up today's two specific entries.
"""
import argparse
import json
from pathlib import Path

SCAN_LOG = Path("/home/work/.hermes/food_scan_log.json")
NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")

# scan#47 — sesame chicken salad (chicken breast + cucumber + carrot shreds + sesame sauce)
SCAN47_IMG = "scan_20260811_152839.jpg"
SCAN47_NEW_NAME = "芝麻雞胸沙律"
SCAN47_BACKFILL = {
    # nutrition_merged holds the 12-field schema; backfill a reasonable
    # breakfast/lunch snapshot. Most fields stay at 0 since we don't
    # have raw API output — kcal/P/C/F on the entry are already correct.
    "vision_short": "雞胸肉切片配青瓜絲、紅蘿蔔絲,淋上芝麻醬,似涼拌雞絲沙律。",
}

# scan#48 — yerba mate tea
SCAN48_IMG = "scan_20260811_152916.jpg"
SCAN48_NEW_NAME = "馬黛茶"
SCAN48_BACKFILL = {
    "vision_short": "一包即沖嘅馬黛茶 (yerba mate),未見到有沖水,可能係準備沖泡或已經飲用過。零糖零奶。",
}


def main(apply=False):
    sl = json.loads(SCAN_LOG.read_text())
    nl = json.loads(NUTRITION_LOG.read_text())

    def fix_entry(entries, img, new_name, backfill, source):
        updated = []
        for e in entries:
            if img in (e.get("image_path") or "") or img in (e.get("image_saved_to") or ""):
                old_name = e.get("name")
                e["name"] = new_name
                for k, v in backfill.items():
                    e[k] = v
                e["retro_fixed"] = True
                e["retro_fix_note"] = (
                    f"v3.2.7.32: renamed {old_name!r} → {new_name!r} "
                    f"({source}); vision-extraction (菜式/Markdown/regex) "
                    f"was returning label words. Kept existing nutrition."
                )
                updated.append((e.get("timestamp_iso", "")[:19], old_name, new_name))
            else:
                pass
        return updated

    print("=== scan_log ===")
    fix47 = fix_entry(sl, SCAN47_IMG, SCAN47_NEW_NAME, SCAN47_BACKFILL, "scan_log")
    for ts, old, new in fix47:
        print(f"  {ts} {old!r}  → {new!r}")

    print("=== scan_log (scan#48) ===")
    fix48 = fix_entry(sl, SCAN48_IMG, SCAN48_NEW_NAME, SCAN48_BACKFILL, "scan_log")
    for ts, old, new in fix48:
        print(f"  {ts} {old!r}  → {new!r}")

    print()
    print("=== nutrition_log ===")
    fix47n = fix_entry(nl.get("meals", []), SCAN47_IMG, SCAN47_NEW_NAME, SCAN47_BACKFILL, "nutrition_log")
    for ts, old, new in fix47n:
        print(f"  {ts} {old!r}  → {new!r}")
    print("=== nutrition_log (scan#48) ===")
    fix48n = fix_entry(nl.get("meals", []), SCAN48_IMG, SCAN48_NEW_NAME, SCAN48_BACKFILL, "nutrition_log")
    for ts, old, new in fix48n:
        print(f"  {ts} {old!r}  → {new!r}")

    if not apply:
        print("\nDRY RUN — pass --apply to write changes")
        return 0

    SCAN_LOG.write_text(json.dumps(sl, ensure_ascii=False, indent=2))
    NUTRITION_LOG.write_text(json.dumps(nl, ensure_ascii=False, indent=2))
    print("\nWrote scan_log + nutrition_log")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    raise SystemExit(main(apply=ap.parse_args().apply))
