# scripts/migrations — one-shot data fixes

Each script in this directory is a **one-time** data migration that was run on
2026-08-08/09 to fix issues in `.whoop_workout_log.json`,
`.hermes/nutrition_log.json`, and `.hermes/food_scan_log.json`.

They live here for historical reference and in case a similar issue needs to be
re-run against a fresh dataset. **They are not idempotent** — re-running may
either no-op (by length-comparison) or overwrite corrected data.

| Script | Purpose |
|---|---|
| `retro_0808_override.py` | Override 8/8 entry with actual vision content (eggs + toast) |
| `retro_comprehensive_comments.py` | Re-derive 4-section `coach_comment` on all entries |
| `retro_scan_log_micros.py` | Backfill micros + coach_comment into scan_log from nutrition_log |
| `retro_today_data.py` | Run `_extract_dish_name + _coach_comment` on today's entries |
| `retro_today8_data.py` | Clean up today's 8/8 entry — drop rating, fix restaurant_chain |

All scripts depend on importing `gym_web.py` via `importlib` to call internal
helpers (`_extract_dish_name`, `_coach_comment`). They expect to be run from
the project root:

```bash
cd /home/work/projects/gymbro
python3 scripts/migrations/retro_today_data.py
```