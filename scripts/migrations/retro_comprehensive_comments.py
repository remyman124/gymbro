#!/usr/bin/env python3
"""Retro-fill all existing entries with new comprehensive 4-section coach_comment."""
import json, importlib.util

spec = importlib.util.spec_from_file_location("gym_web", "/home/work/projects/gymbro/gym_web.py")
gw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gw)

nutr_log_path = "/home/work/.hermes/nutrition_log.json"
scan_log_path = "/home/work/.hermes/food_scan_log.json"

with open(nutr_log_path) as f:
    nl = json.load(f)
with open(scan_log_path) as f:
    sl = json.load(f)

fixed = 0
# Update nutrition_log entries
for top_key, entries in nl.items():
    if not isinstance(entries, list):
        continue
    for e in entries:
        if not isinstance(e, dict):
            continue
        name = e.get("name", "")
        cal = e.get("calories") or e.get("kcal") or 0
        if not name or cal <= 0:
            continue
        p = e.get("protein") or e.get("protein_g") or 0
        c = e.get("carbs") or e.get("carbs_g") or 0
        f_ = e.get("fat") or e.get("fat_g") or 0
        rest = e.get("restaurant_chain") or e.get("chain") or ""
        new_cc = gw._coach_comment(name, cal, p, c, f_, rest)
        old_cc = e.get("coach_comment", {})
        # Only update if the new comment is more comprehensive (longer)
        if len(new_cc.get("comment", "")) > len(old_cc.get("comment", "")):
            e["coach_comment"] = new_cc
            fixed += 1

# Update scan_log entries similarly
for e in sl:
    if not isinstance(e, dict):
        continue
    name = e.get("name", "")
    cal = e.get("calories", 0) or 0
    if not name or cal <= 0:
        continue
    p = e.get("protein", 0) or 0
    c = e.get("carbs", 0) or 0
    f_ = e.get("fat", 0) or 0
    rest = e.get("restaurant_chain", "")
    new_cc = gw._coach_comment(name, cal, p, c, f_, rest)
    old_cc = e.get("coach_comment", {})
    if len(new_cc.get("comment", "")) > len(old_cc.get("comment", "")):
        e["coach_comment"] = new_cc
        fixed += 1

with open(nutr_log_path, "w") as f:
    json.dump(nl, f, ensure_ascii=False, indent=2)
with open(scan_log_path, "w") as f:
    json.dump(sl, f, ensure_ascii=False, indent=2)
print(f"✓ Retro-filled {fixed} entries with 4-section comprehensive comment")
