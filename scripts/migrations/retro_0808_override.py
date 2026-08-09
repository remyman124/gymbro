#!/usr/bin/env python3
"""Override 8/8 entry with actual vision content: 煎蛋 + 烤麵包."""
import json, sys, importlib.util

# Load gym_web.py for _coach_comment
spec = importlib.util.spec_from_file_location("gym_web", "/home/work/projects/gymbro/gym_web.py")
gw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gw)

nutr_log_path = "/home/work/.hermes/nutrition_log.json"
with open(nutr_log_path) as f:
    nl = json.load(f)

# Vision actual: 煎蛋 × 2 + 烤麵包 × 1
new_name = "煎蛋、烤麵包"
new_vision_desc = "煎蛋兩隻加一片烤麵包，份量適中"
macros = {"calories": 192.5, "protein": 9, "carbs": 20, "fat": 10}  # keep existing

fixed = 0
for date_key, entries in nl.items():
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("date") != "2026-08-08":
            continue
        old_name = e.get("name", "")
        # Update fields
        e["name"] = new_name
        e["vision_desc"] = new_vision_desc
        e["user_correction"] = "煎蛋 + 烤麵包 (Jim OOB 2026-08-08 10:30 HKT 'should recognise as 雞蛋、麵包')"
        e["user_hints"] = ["煎蛋", "烤麵包"]
        # Re-derive coach_comment
        e["coach_comment"] = gw._coach_comment(
            new_name,
            macros["calories"],
            macros["protein"],
            macros["carbs"],
            macros["fat"],
        )
        print(f"  Override 8/8 {e.get('time', '?')}: '{old_name}' → '{new_name}'")
        print(f"     vision_desc: {new_vision_desc}")
        print(f"     grade: {e['coach_comment'].get('grade')} — {e['coach_comment'].get('comment')}")
        print(f"     rationale: {e['coach_comment'].get('rationale')}")
        fixed += 1

with open(nutr_log_path, "w") as f:
    json.dump(nl, f, ensure_ascii=False, indent=2)
print(f"\n✓ Overrode {fixed} 8/8 entry(ies)")
