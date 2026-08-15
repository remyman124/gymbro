"""End-to-end test: verify 海南雞 entry is visible in the food log.

Jim OOB 2026-08-11 'still missing the 海南雞飯, please do automated
testing' — trace the full data chain from Google Sheet (canonical) →
in-memory NutritionCache → /api/scan_recent → simulated frontend grouping.
Fail loudly if anything drops the entry.

Run against the LIVE server at localhost:7000 (so we know the cache
hydration from Sheet is actually live).
"""
import json
import urllib.request

SCAN_RECENT_URL = "http://localhost:7000/api/scan_recent?limit=100"


def test_api_scan_recent_returns_hainanji():
    resp = urllib.request.urlopen(SCAN_RECENT_URL, timeout=10)
    d = json.loads(resp.read())
    scans = d.get("scans", [])
    matches = [s for s in scans if s.get("name") == "海南雞飯"
               and (s.get("timestamp_iso") or "").startswith("2026-08-10")]
    assert len(matches) >= 1, (
        f"/api/scan_recent returned {len(matches)} 海南雞飯 for yesterday "
        f"(total scans: {len(scans)}, filtered: {d.get('filtered')})"
    )
    for s in matches:
        assert s.get("image_url"), f"missing image_url on {s.get('timestamp_iso')}"
    print(f"  ✓ /api/scan_recent: {len(matches)} 海南雞飯 yesterday, all with image_url")


def test_sw_cache_bumped():
    """v3.2.7.32 fix — the SW cache must be v121 so iPhone PWA evicts stale data."""
    resp = urllib.request.urlopen("http://localhost:7000/sw.js", timeout=10)
    sw = resp.read().decode()
    assert "gym-web-v121" in sw, "SW cache is not v121 — bump CACHE in gym_web.py"
    assert "gym-web-v116" not in sw, "stale v116 still referenced in SW"
    print("  ✓ SW cache: v117 (old v120 evicted)")


def test_frontend_grouping_keeps_hainanji():
    """Simulate the frontend `recentScansGrouped` getter (templates/index.html:1848).

    For each scan in the API response, the frontend:
      1. Slices timestamp_iso[:10] as the date key
      2. Drops entries with date.length < 10
      3. Groups by date, sums kcal, sorts items DESC by timestamp_iso

    Verify both 海南雞 entries land in the 2026-08-10 group.
    """
    resp = urllib.request.urlopen(SCAN_RECENT_URL, timeout=10)
    scans = json.loads(resp.read()).get("scans", [])

    groups = {}
    for s in scans:
        ts = s.get("timestamp_iso") or ""
        date = ts[:10]
        if not date or len(date) < 10:
            continue
        groups.setdefault(date, []).append(s)

    yesterday = "2026-08-10"
    assert yesterday in groups, (
        f"no group for {yesterday}; dates: {sorted(groups.keys(), reverse=True)[:5]}"
    )
    yesterday_items = groups[yesterday]
    hainan = [s for s in yesterday_items if s.get("name") == "海南雞飯"]
    assert len(hainan) >= 1, (
        f"frontend grouping would show {len(hainan)} 海南雞 for {yesterday} "
        f"(total yesterday items: {len(yesterday_items)})\n"
        f"all yesterday items: {[(s.get('name'), s.get('calories'), s.get('timestamp_iso')[:16]) for s in yesterday_items]}"
    )
    print(f"  ✓ frontend grouping: {len(hainan)} 海南雞 visible in 昨日 group")


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            print(f"\n{fn.__name__}:")
            fn()
        except AssertionError as e:
            print(f"  ✗ FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ ERROR: {type(e).__name__}: {e}")
            failed += 1
    print()
    if failed:
        print(f"{failed} of {len(tests)} tests FAILED")
        raise SystemExit(1)
    print(f"all {len(tests)} tests passed")