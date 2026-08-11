"""Re-extract dish names + nutrition for junk-named scans from 2026-08-10.

Jim OOB 2026-08-11 'i think you should fix the filter or data such that
this can be visible' — the late-night scans fell back to garbage names
('總結', '**', '無小票或其他菜名', '這罐檸檬茶') because the vision
output was junk or empty. The images still exist on disk, so re-run the
vision + dish-name extraction and patch both logs in place.

Notes:
- The 23:19 entry was already renamed in nutrition_log by retro_v4
  ('檸檬茶 (Nestea)' + 160 kcal), but scan_log #48 still shows '總結'.
  We patch scan_log #48 with the same corrected name.
- For the 3 remaining junk entries, we re-run vision → dish extraction →
  pplx+apiyi nutrition merge. The narration AI check is skipped because
  these entries already had real kcal logged (so vision clearly identified
  food) and the classifier has been observed to flag legitimate names
  ('罐 330ml 嘅檸檬茶') as narration.
- Matching is by image basename to avoid substring false positives.
"""
import argparse
import base64
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/work/projects/gymbro")
spec = importlib.util.spec_from_file_location("gym_web", "/home/work/projects/gymbro/gym_web.py")
gw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gw)

NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")
SCAN_LOG = Path("/home/work/.hermes/food_scan_log.json")

JUNK_SUBSTRINGS = ("總結", "**", "小票", "呢張", "呢罐", "這罐", "呢支",
                   "這支", "呢個", "這個")

VISION_PROMPT = (
    "呢張相入面有咩食物或飲品？用一句繁體中文廣東話描述。簡短，唔好長篇"
    "大論，唔好寫'呢張相'呢啲開場白。例：'一支 330ml 嘅可口可樂罐裝'。"
    "只係描述最清楚嗰樣嘢嘅名 + 大小。"
)


def is_junk(name: str) -> bool:
    return isinstance(name, str) and any(b in name for b in JUNK_SUBSTRINGS)


def basename(path: str) -> str:
    return Path(path).name if path else ""


def re_extract(image_path: str, fallback_kcal: float):
    """Re-run vision + dish + nutrition on a saved scan image.

    Returns (name, calories, protein, carbs, fat, status).
    """
    try:
        img_b64 = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    except Exception as e:
        return None, 0, 0, 0, 0, f"read fail: {e}"

    vision_a = gw._minimax_vision(img_b64, VISION_PROMPT) or ""
    vision_b = gw._apiyi_vision_analyze(img_b64, VISION_PROMPT) or ""
    if not vision_a and not vision_b:
        return None, 0, 0, 0, 0, "vision both failed"

    vision_merged = vision_a or vision_b
    dish = gw._extract_dish_name(vision_merged, vision_b, fallback="")
    if not dish or is_junk(dish):
        return None, 0, 0, 0, 0, f"dish name still junk: {dish!r}"

    pplx = gw._pplx_enrich(dish) or ""
    apiyi = gw._apiyi_nutrition_enrich(dish) or ""
    pplx_parsed = gw._parse_nutrition_block(pplx)
    try:
        apiyi_parsed = json.loads(apiyi)
    except Exception:
        apiyi_parsed = gw._parse_nutrition_block(apiyi)
    merged = gw._merge_nutrition_estimates([pplx_parsed, apiyi_parsed])
    cal = float(merged.get("calories", {}).get("value") or 0)
    p = float(merged.get("protein", {}).get("value") or 0)
    c = float(merged.get("carbs", {}).get("value") or 0)
    f = float(merged.get("fat", {}).get("value") or 0)
    if cal == 0 and fallback_kcal:
        cal = fallback_kcal  # don't zero out a meal that had real kcal
    return dish, cal, p, c, f, "ok"


def main(apply=False):
    nl = json.loads(NUTRITION_LOG.read_text())
    meals = nl.get("meals", [])
    sl = json.loads(SCAN_LOG.read_text())

    actions = []

    # 1. Patch scan_log #48 (23:19 '總結' → '檸檬茶 (Nestea)' to match retro_v4)
    for e in sl:
        if e.get("scan_index") == 48 and e.get("name") == "總結":
            actions.append(("scan_log", "scan_20260810_231925.jpg",
                            "總結", "檸檬茶 (Nestea)", e.get("calories") or 0,
                            "rename to match retro_v4"))
            e["name"] = "檸檬茶 (Nestea)"
            e["retro_fixed"] = True
            e["retro_fix_note"] = "v3.2.7.26: rename to match retro_v4 (was '總結')"
            break

    # 2. Re-extract the remaining junk scans (every junk entry, even if scan_index dupes)
    junk_scans = []
    for e in sl:
        nm = e.get("name", "")
        if not is_junk(nm):
            continue
        if e.get("scan_index") == 48 and nm == "總結":
            continue  # handled by #1
        junk_scans.append(e)

    for e in junk_scans:
        img = e.get("image_path") or ""
        if not img:
            continue
        old_name = e["name"]
        old_kcal = e.get("calories") or 0
        dish, cal, p, c, f, status = re_extract(img, old_kcal)
        if dish:
            actions.append(("scan_log", basename(img), old_name, dish, cal, status))
            e["name"] = dish
            e["calories"] = cal
            e["protein"] = p
            e["carbs"] = c
            e["fat"] = f
            e["retro_fixed"] = True
            e["retro_fix_note"] = f"v3.2.7.26: re-extracted from image ({status})"
        else:
            actions.append(("scan_log", basename(img), old_name, f"FAILED: {status}",
                            old_kcal, status))

    # 3. Patch nutrition_log meals by image basename (safer than substring)
    for row in actions:
        kind, img_base, old, new, kcal, status = row
        if "FAILED" in str(new) or kind != "scan_log":
            continue
        for m in meals:
            if basename(m.get("image_saved_to", "")) != img_base:
                continue
            old_mname = m.get("name")
            if old_mname == new:
                continue
            actions.append(("nutrition_log", img_base, old_mname, new, kcal, status))
            m["name"] = new
            m["calories"] = kcal
            m["retro_fixed"] = True
            m["retro_fix_note"] = f"v3.2.7.26: re-extracted from image (was {old_mname!r})"
            break

    for row in actions:
        kind, ref, old, new, kcal, status = row
        print(f"  [{kind:14}] {ref:30} {old!r:30} → {new!r:30} ({kcal} kcal) [{status}]")

    if not apply:
        print("\nDRY RUN — pass --apply to write changes")
        return 0

    NUTRITION_LOG.write_text(json.dumps(nl, ensure_ascii=False, indent=2))
    SCAN_LOG.write_text(json.dumps(sl, ensure_ascii=False, indent=2))
    print("\nWrote nutrition_log + scan_log")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    raise SystemExit(main(apply=ap.parse_args().apply))