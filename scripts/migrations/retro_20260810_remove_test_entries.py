"""Retroactively remove the 6 broken scan_log entries created by my
testing on 2026-08-10 21:20-21:27 (1x1 white test JPGs that the vision
API rejected, but the v3.2.7.10-era auto-commit code committed anyway).

Jim OOB 2026-08-10 'you didn't fix my data' — these 6 entries are
visible in the food log and have empty name + empty calories. Remove
them from both scan_log (entries 49-54) and nutrition_log (entries
478-483).

Does NOT touch the 2 legacy 2026-07-23 entries (scan_index 0, 1) — those
predate this session and may be real user data that the user wants to
keep.
"""
import json
from pathlib import Path

SCAN_LOG = Path("/home/work/.hermes/food_scan_log.json")
NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")

# Test image filenames — the 1x1 white JPGs created during repro testing
TEST_IMAGE_FRAGMENTS = (
    "scan_20260810_212026", "scan_20260810_212103",
    "scan_20260810_212153", "scan_20260810_212221",
    "scan_20260810_212254", "scan_20260810_212744",
)


def is_test_entry(entry):
    """True if the entry is a test artifact from 2026-08-10 21:20-21:27."""
    img = entry.get("image_path", "") or entry.get("image_url", "")
    if any(f in img for f in TEST_IMAGE_FRAGMENTS):
        return True
    # Belt-and-suspenders: also match by the broken name pattern
    name = entry.get("name", "")
    if "（APiyi gpt-4o vision 失敗" in name and "2026-08-10T21:2" in entry.get("timestamp_iso", ""):
        return True
    return False


def main():
    removed_scan, removed_meal = [], []

    # 1. scan_log
    if SCAN_LOG.exists():
        scan_log = json.loads(SCAN_LOG.read_text())
        before = len(scan_log)
        kept = [e for e in scan_log if not is_test_entry(e)]
        removed_scan = [e for e in scan_log if is_test_entry(e)]
        SCAN_LOG.write_text(json.dumps(kept, ensure_ascii=False, indent=2))
        print(f"scan_log: {before} → {len(kept)} (removed {len(removed_scan)})")
    else:
        print(f"scan_log not found: {SCAN_LOG}")

    # 2. nutrition_log
    if NUTRITION_LOG.exists():
        nl = json.loads(NUTRITION_LOG.read_text())
        meals = nl.get("meals", [])
        before = len(meals)
        kept_meals = []
        for m in meals:
            if is_test_entry(m):
                removed_meal.append(m)
            else:
                kept_meals.append(m)
        nl["meals"] = kept_meals
        NUTRITION_LOG.write_text(json.dumps(nl, ensure_ascii=False, indent=2))
        print(f"nutrition_log: {before} → {len(kept_meals)} meals (removed {len(removed_meal)})")
    else:
        print(f"nutrition_log not found: {NUTRITION_LOG}")

    print()
    print("Removed scan_log entries:")
    for e in removed_scan:
        print(f"  #{e.get('scan_index')} {e.get('timestamp_iso', '')[:16]} name={e.get('name')!r}")
    print()
    print("Removed nutrition_log entries:")
    for m in removed_meal:
        print(f"  {m.get('timestamp_iso', '')[:16]} name={m.get('name')!r} kcal={m.get('calories')} img={m.get('image_saved_to', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
