"""WHOOP step_count tests — v3.3.25 (Jim OOB 2026-10-20「Steps is whoop!」).

`/developer/v2/cycle` has always carried a top-level `step_count` on every
cycle record; nothing read it until v3.3.25, so every step number came from
Withings (which frequently has no same-day record). These tests pin:

  - the extraction semantics (cycles[0] = in-progress = today; cycles[1..7]
    = the 7d average)
  - the truthfulness rules (never fall back to a previous day; surface the
    staleness so the UI can say "band off" instead of implying "live")
  - WHOOP as the PRIMARY source in /api/health_overlay, Withings as fallback
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

HKT = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def _clear_overlay_cache():
    """Drop the shared `overlay:health` Redis entry around every test.

    /api/health_overlay caches its merged payload in Redis for 60s, and that
    cache is a process-wide singleton (namespace "gymbro") shared with
    production traffic. Without this, one test's payload leaks into the next
    and the step assertions read the wrong source's numbers.
    """
    from gym_web.redis_cache import get_redis_cache
    cache = get_redis_cache()
    cache.delete("overlay:health")
    yield
    cache.delete("overlay:health")


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _cycle(start, end, steps, updated_at):
    c = {
        "id": 1,
        "start": _iso(start),
        "end": _iso(end) if end else None,
        "timezone_offset": "+08:00",
        "score_state": "SCORED",
        "score": {"strain": 8.0, "kilojoule": 3000.0},
    }
    if steps is not None:
        c["step_count"] = steps
    if updated_at is not None:
        c["updated_at"] = _iso(updated_at)
    return c


def _seed_whoop(path, cycles):
    path.write_text(json.dumps({"cycles": cycles, "recovery": [], "sleep": [], "workouts": []}))


def _now():
    """Real 'now' — the endpoint/overlay compute staleness against the wall
    clock, so the fixtures must be anchored to the wall clock too."""
    return datetime.now(timezone.utc)


# ── unit: gym_web.whoop.whoop_get_steps ──────────────────────────────────

def test_get_steps_reads_in_progress_cycle_as_today(isolated_files):
    """cycles[0] has end=None -> its step_count is TODAY's running total."""
    from gym_web.whoop import whoop_get_steps

    now = _now()
    cycles = [
        _cycle(now - timedelta(hours=6), None, 2286, now - timedelta(minutes=5)),
        _cycle(now - timedelta(days=1, hours=6), now - timedelta(hours=6), 6647, now - timedelta(hours=6)),
        _cycle(now - timedelta(days=2, hours=6), now - timedelta(days=1, hours=6), 31147, now - timedelta(days=1, hours=6)),
    ]
    _seed_whoop(isolated_files["WHOOP_CACHE"], cycles)

    r = whoop_get_steps(now=now, cache_path=isolated_files["WHOOP_CACHE"])
    assert r["steps"] == 2286
    assert r["in_progress"] is True
    assert r["stale"] is False
    assert r["band_off"] is False
    assert r["steps_7d_samples"] == 2
    assert r["steps_7d_avg"] == 18897          # (6647 + 31147) / 2
    assert r["date"] == (now - timedelta(hours=6)).astimezone(HKT).strftime("%Y-%m-%d")
    assert len(r["series"]) == 3
    assert r["_source"] == "whoop_cycle"


def test_get_steps_7d_avg_excludes_in_progress_cycle(isolated_files):
    """The running cycle must not pollute the 7-day average."""
    from gym_web.whoop import whoop_get_steps

    now = _now()
    cycles = [_cycle(now - timedelta(hours=6), None, 999999, now - timedelta(minutes=1))]
    for i in range(1, 8):
        cycles.append(_cycle(
            now - timedelta(days=i, hours=6), now - timedelta(days=i - 1, hours=6),
            1000 * i, now - timedelta(days=i - 1, hours=6),
        ))
    _seed_whoop(isolated_files["WHOOP_CACHE"], cycles)

    r = whoop_get_steps(now=now, cache_path=isolated_files["WHOOP_CACHE"])
    assert r["steps"] == 999999
    assert r["steps_7d_samples"] == 7
    # mean of 1000..7000 = 4000
    assert r["steps_7d_avg"] == 4000


def test_get_steps_flags_band_off_when_stale(isolated_files):
    """A cycle that hasn't been written for >6h means the band was off-body."""
    from gym_web.whoop import whoop_get_steps

    now = _now()
    cycles = [_cycle(now - timedelta(hours=10), None, 2286, now - timedelta(hours=9, minutes=54))]
    _seed_whoop(isolated_files["WHOOP_CACHE"], cycles)

    r = whoop_get_steps(now=now, cache_path=isolated_files["WHOOP_CACHE"])
    assert r["steps"] == 2286
    assert r["stale"] is True
    assert r["band_off"] is True
    assert r["age_min"] == 594


def test_get_steps_empty_cache_returns_empty(isolated_files):
    from gym_web.whoop import whoop_get_steps
    assert whoop_get_steps(cache_path=isolated_files["WHOOP_CACHE"]) == {}


def test_get_steps_no_step_count_is_none_not_zero(isolated_files):
    """Missing step_count must be None (unknown), never 0 (a real zero-step day)."""
    from gym_web.whoop import whoop_get_steps

    now = _now()
    cycles = [_cycle(now - timedelta(hours=6), None, None, now)]
    _seed_whoop(isolated_files["WHOOP_CACHE"], cycles)

    r = whoop_get_steps(now=now, cache_path=isolated_files["WHOOP_CACHE"])
    assert r["steps"] is None
    assert r["steps_7d_avg"] is None


def test_get_steps_supports_nested_cache_shape(isolated_files):
    """Shape A: {"cycle": {"records": [...]}} — gym_web.py tolerates both."""
    from gym_web.whoop import whoop_get_steps

    now = _now()
    cycles = [_cycle(now - timedelta(hours=6), None, 4242, now - timedelta(minutes=2))]
    isolated_files["WHOOP_CACHE"].write_text(json.dumps({"cycle": {"records": cycles}}))

    r = whoop_get_steps(now=now, cache_path=isolated_files["WHOOP_CACHE"])
    assert r["steps"] == 4242


# ── API: /api/whoop_steps ────────────────────────────────────────────────

def test_whoop_steps_endpoint_shape(client, isolated_files):
    now = _now()
    cycles = [_cycle(now - timedelta(hours=6), None, 2286, now - timedelta(minutes=5))]
    _seed_whoop(isolated_files["WHOOP_CACHE"], cycles)

    r = client.get("/api/whoop_steps")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert d["steps"] == 2286
    assert d["in_progress"] is True
    assert d["stale"] is False
    assert d["band_off"] is False
    assert d["_source"] == "whoop_cycle"
    # Withings-compatible aliases so the front-end can consume either source
    assert "syncing" in d and "calories" in d and "distance_km" in d


def test_whoop_steps_endpoint_empty_cache_is_ok_false(client, isolated_files):
    r = client.get("/api/whoop_steps")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is False
    assert d["steps"] is None


# ── API: /api/health_overlay uses WHOOP as PRIMARY ──────────────────────

def test_health_overlay_uses_whoop_steps_as_primary(client, isolated_files):
    now = _now()
    cycles = [
        _cycle(now - timedelta(hours=6), None, 2286, now - timedelta(minutes=5)),
        _cycle(now - timedelta(days=1, hours=6), now - timedelta(hours=6), 6647, now - timedelta(hours=6)),
        _cycle(now - timedelta(days=2, hours=6), now - timedelta(days=1, hours=6), 31147, now - timedelta(days=1, hours=6)),
    ]
    _seed_whoop(isolated_files["WHOOP_CACHE"], cycles)

    d = client.get("/api/health_overlay").get_json()
    assert d["steps_today"] == 2286          # WHOOP wins
    assert d["steps_source"] == "whoop"
    assert d["steps_date"] is not None
    assert d["steps_syncing"] is False
    assert d["whoop_steps_7d_avg"] == 18897


def test_health_overlay_whoop_only_no_withings_fallback(client, isolated_files):
    """v3.3.25: Withings is FULLY removed as a step source.

    When Whoop has no step_count, the overlay reports steps_today=None and
    steps_syncing=True — it does NOT silently fall back to a Withings number
    (and it certainly does not show a previous day's number as today's).
    """
    now = _now()
    cycles = [_cycle(now - timedelta(hours=6), None, None, now)]
    _seed_whoop(isolated_files["WHOOP_CACHE"], cycles)
    # v3.3.25: Withings is gone entirely — there is no WITHINGS_CACHE to
    # seed any more, so this test now simply asserts that a Whoop cycle
    # WITHOUT a step_count reports "no steps" instead of silently showing
    # some other source's number.

    d = client.get("/api/health_overlay").get_json()
    assert d["steps_today"] is None
    assert d["steps_syncing"] is True
    assert d["steps_source"] == "whoop"
    # The Withings step keys are gone from the payload entirely.
    for gone in ("distance_km_today", "calories_today",
                 "distance_km_yesterday", "calories_yesterday"):
        assert gone not in d


def test_health_overlay_yesterday_comes_from_whoop(client, isolated_files):
    now = _now()
    cycles = [
        _cycle(now - timedelta(hours=6), None, 2286, now - timedelta(minutes=5)),
        _cycle(now - timedelta(days=1, hours=6), now - timedelta(hours=6), 6647, now - timedelta(hours=6)),
    ]
    _seed_whoop(isolated_files["WHOOP_CACHE"], cycles)

    d = client.get("/api/health_overlay").get_json()
    assert d["steps_today"] == 2286
    assert d["steps_yesterday"] == 6647
    assert d["steps_in_progress"] is True


def test_health_overlay_surfaces_band_off_flag(client, isolated_files):
    now = _now()
    cycles = [_cycle(now - timedelta(hours=10), None, 2286, now - timedelta(hours=9, minutes=54))]
    _seed_whoop(isolated_files["WHOOP_CACHE"], cycles)

    d = client.get("/api/health_overlay").get_json()
    assert d["steps_today"] == 2286
    assert d["steps_band_off"] is True
    assert d["steps_stale"] is True
    assert d["steps_as_of"] is not None


# ── the Withings step surface is GONE ────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/withings_steps_today",
    "/api/withings_steps_7d_avg",
    "/api/withings_refresh",
])
def test_withings_step_endpoints_are_removed(client, path):
    """v3.3.25: 'Fully remove withings' as a step source."""
    r = client.get(path)
    assert r.status_code == 404
