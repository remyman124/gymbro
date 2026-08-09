#!/usr/bin/env python3
"""Retro-fix today's data using the new pipeline (_extract_dish_name + _coach_comment)."""
import json, sys, importlib.util

# Load gym_web.py to access the new functions
spec = importlib.util.spec_from_file_location("gym_web", "/home/work/projects/gymbro/gym_web.py")
gw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gw)

scan_log_path = "/home/work/.hermes/food_scan_log.json"
nutr_log_path = "/home/work/.hermes/nutrition_log.json"

with open(scan_log_path) as f:
    sl = json.load(f)  # list of entries
with open(nutr_log_path) as f:
    nl = json.load(f)  # dict[date_key -> list]

# Bad dish-name patterns (prose instead of dish name)
bad_patterns = [
    r"^相顯示", r"^相中", r"^可見", r"^睇到", r"^睇見",
    r"^圖片顯示", r"^圖顯示", r"^碗", r"^盒", r"^相片",
]

def is_bad(name):
    if not name:
        return True
    if any(__import__("re").match(p, name) for p in bad_patterns):
        return True
    if len(name) > 30:
        return True
    return False

# === Fix food_scan_log.json (list of entries) ===
print("=== Fixing food_scan_log.json ===")
fixed_scan = 0
for e in sl:
    old_name = e.get("name", "")
    old_grade = (e.get("coach_comment") or {}).get("grade")
    if is_bad(old_name) or not old_grade:
        new_name = gw._extract_dish_name(old_name)
        coach = gw._coach_comment(
            new_name,
            e.get("kcal") or e.get("calories", 0),
            e.get("protein_g") or e.get("protein", 0),
            e.get("carbs_g") or e.get("carbs", 0),
            e.get("fat_g") or e.get("fat", 0),
        )
        e["name"] = new_name
        e["coach_comment"] = coach
        ts = e.get("timestamp_hkt") or e.get("time") or "?"
        print(f"  {ts[:16]} | {old_name[:40]:40s} → {new_name:25s} | grade={coach.get('grade')}")
        fixed_scan += 1

print(f"Fixed scan entries: {fixed_scan}")

# === Fix nutrition_log.json ===
print("\n=== Fixing nutrition_log.json ===")
fixed_nutr = 0
for date_key, entries in nl.items():
    for e in entries:
        if not isinstance(e, dict):
            continue
        old_name = e.get("name", "")
        old_grade = (e.get("coach_comment") or {}).get("grade")
        # Skip zero-cal entries (Mounjaro injection, Soda Water only, DIAG, system events)
        cal = e.get("kcal") or e.get("calories", 0) or 0
        if cal <= 0:
            continue
        if is_bad(old_name) or not old_grade:
            new_name = gw._extract_dish_name(old_name)
            coach = gw._coach_comment(
                new_name,
                cal,
                e.get("protein_g") or e.get("protein", 0),
                e.get("carbs_g") or e.get("carbs", 0),
                e.get("fat_g") or e.get("fat", 0),
            )
            e["name"] = new_name
            e["coach_comment"] = coach
            print(f"  {date_key} | {old_name[:40]:40s} → {new_name:25s} | grade={coach.get('grade')}")
            fixed_nutr += 1

print(f"Fixed nutrition entries: {fixed_nutr}")

# Write back
with open(scan_log_path, "w") as f:
    json.dump(sl, f, ensure_ascii=False, indent=2)
with open(nutr_log_path, "w") as f:
    json.dump(nl, f, ensure_ascii=False, indent=2)

print(f"\n✓ Saved scan_log ({fixed_scan} fixed) + nutrition_log ({fixed_nutr} fixed)")
