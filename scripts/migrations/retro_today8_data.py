#!/usr/bin/env python3
"""Clean up today's 8/8 entry: drop rating, fix restaurant_chain."""
import json

nutr_log_path = "/home/work/.hermes/nutrition_log.json"
with open(nutr_log_path) as f:
    nl = json.load(f)

fixed = 0
for date_key, entries in nl.items():
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("date") != "2026-08-08":
            continue
        # Drop legacy rating
        if "rating" in e:
            del e["rating"]
        # Clean up chatty restaurant_chain
        if e.get("restaurant_chain") == "無法準確識別餐廳":
            e["restaurant_chain"] = ""
        # Source label
        if e.get("source", "").startswith("v2.2"):
            e["source"] = e["source"].replace("v2.2-scan", "v3.2.7.4-scan")
        print(f"  Fixed 8/8 {e.get('time', '?')} {e.get('name', '?')}: rating dropped, restaurant_chain cleaned, source updated")
        fixed += 1

with open(nutr_log_path, "w") as f:
    json.dump(nl, f, ensure_ascii=False, indent=2)
print(f"\n✓ Cleaned {fixed} 8/8 entry(ies)")
