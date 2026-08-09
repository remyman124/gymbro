#!/usr/bin/env python3
"""Retro-fill scan_log micros + coach_comment from nutrition_log.json.

Match by (date, time) primary, (date, name) fallback.
"""
import json, re

scan_log_path = "/home/work/.hermes/food_scan_log.json"
nutr_log_path = "/home/work/.hermes/nutrition_log.json"

with open(scan_log_path) as f:
    sl = json.load(f)
with open(nutr_log_path) as f:
    nl = json.load(f)

# Build index by (date, time) and (date, name)
# nutrition_log schema: {"meals": [...], "workouts": [...], ...}
# Each meal entry has a "date" field.
by_dt = {}
by_dn = {}
for top_key, entries in nl.items():
    if not isinstance(entries, list):
        continue
    for e in entries:
        if not isinstance(e, dict):
            continue
        date_key = e.get("date", "")
        time_hm = e.get("time", "")[:5]
        name = e.get("name", "")
        if date_key and time_hm:
            by_dt[(date_key, time_hm)] = e
        if date_key and name:
            by_dn[(date_key, name)] = e

MICRO_FIELDS = ["carbs", "fat", "fiber", "sugar", "sodium", "sat_fat",
                "trans_fat", "vit_c", "iron", "calcium"]

fixed_micros = 0
fixed_grade = 0
unmatched = []

for entry in sl:
    if not isinstance(entry, dict):
        continue
    ts = entry.get("timestamp_iso", "")
    if not ts:
        continue
    date_part = ts[:10]
    time_part = ts[11:16]
    name = entry.get("name", "")

    # Find target — try (date,time) then (date,name) then (date, ±5min)
    target = by_dt.get((date_part, time_part))
    if not target:
        target = by_dn.get((date_part, name))
    if not target and time_part:
        # Try ±5 minutes match
        h, m = time_part.split(":")
        for delta in range(-5, 6):
            try:
                m_new = int(m) + delta
                h_new = int(h)
                if m_new < 0:
                    m_new += 60
                    h_new -= 1
                if m_new >= 60:
                    m_new -= 60
                    h_new += 1
                alt_time = f"{h_new:02d}:{m_new:02d}"
                cand = by_dt.get((date_part, alt_time))
                if cand and cand.get("name") == name:
                    target = cand
                    break
            except Exception:
                continue
    if not target:
        unmatched.append(f"{date_part} {time_part} {name}")
        continue

    # Copy micros if missing
    for f in MICRO_FIELDS:
        if (entry.get(f) in (0, None) or entry.get(f) == "—") and target.get(f, 0):
            entry[f] = target[f]
            fixed_micros += 1
    # Copy coach_comment if missing
    if not entry.get("coach_comment") and target.get("coach_comment"):
        entry["coach_comment"] = target["coach_comment"]
        fixed_grade += 1
    # Clean restaurant_chain
    rc = entry.get("restaurant_chain", "")
    if rc.startswith("無法") or rc.startswith("呢碗") or rc.startswith("係喺") or rc.startswith("頭都係"):
        if target.get("restaurant_chain"):
            entry["restaurant_chain"] = target["restaurant_chain"]
    # Source label
    src = entry.get("source", "")
    if src.startswith("v2.2"):
        entry["source"] = src.replace("v2.2-scan", "v3.2.7.7-scan")

print(f"Fixed {fixed_micros} micro fields + {fixed_grade} coach_comments")
if unmatched:
    print(f"Unmatched {len(unmatched)}: {unmatched[:5]}")

with open(scan_log_path, "w") as f:
    json.dump(sl, f, ensure_ascii=False, indent=2)
print(f"✓ Saved {scan_log_path}")
