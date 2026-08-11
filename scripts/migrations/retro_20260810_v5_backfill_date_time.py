"""Backfill missing `date`/`time` on nutrition_log meals from timestamp_iso.

Jim OOB 2026-08-10 'why the 海南雞 disappeared for yesterday' — the
2026-08-10 21:17 海南雞飯 entry (and 10 others) were written by the
v2.2-scan / auto-commit path with `timestamp_iso` only. Every date-filtered
view (`_load_today_nutrition`, sheet sync) keys off `date`, so those meals
were silently invisible even though they were in the log.

The program fix is in gym_web.py `_load_today_nutrition` (falls back to
timestamp_iso). This script normalises the existing rows so all consumers
agree.
"""
import argparse
import json
from pathlib import Path

NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")


def main(apply=False):
    nl = json.loads(NUTRITION_LOG.read_text())
    meals = nl.get("meals", [])
    fixed = []
    for i, m in enumerate(meals):
        if not isinstance(m, dict) or m.get("date"):
            continue
        ts = m.get("timestamp_iso") or m.get("timestamp") or m.get("logged_at") or ""
        if not isinstance(ts, str) or len(ts) < 16:
            continue
        m["date"] = ts[:10]
        m.setdefault("time", ts[11:16])
        fixed.append((i, m["date"], m["time"], m.get("name"), m.get("calories")))

    for i, d, t, name, kcal in fixed:
        print(f"  [{i}] {d} {t}  {name!r}  {kcal} kcal")
    print(f"\n{len(fixed)} of {len(meals)} meals backfilled")

    if not apply:
        print("\nDRY RUN — pass --apply to write changes")
        return 0

    NUTRITION_LOG.write_text(json.dumps(nl, ensure_ascii=False, indent=2))
    print("\nWrote nutrition_log")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    raise SystemExit(main(apply=ap.parse_args().apply))
