"""gymbro Whoop integration (v3.1.0 modular refactor).

Whoop V2 API client + caching. New code lives here; legacy `/api/whoop_*`
routes in gym_web.py will migrate to a Blueprint in v3.2.0.

Key facts:
- Token at /home/work/.whoop_tokens.json
- PKCE verifier at /home/work/.whoop_code_verifier.json
- User-Agent MUST be Mozilla/5.0 (compatible; CheerBot/1.0; ...)
  or Cloudflare WAF returns 1010 IP block
- V2 endpoints: /developer/v2/{cycle,recovery,activity/sleep,activity/workout}
- expires_in (seconds), NOT expires_at
- Refresh via Google Identity oauth2.googleapis.com/token
"""

from pathlib import Path
import json

WHOOP_TOKEN_PATH = Path("/home/work/.whoop_tokens.json")
WHOOP_CODE_VERIFIER_PATH = Path("/home/work/.whoop_code_verifier.json")
WHOOP_CACHE_PATH = Path("/home/work/.whoop_data_latest.json")
WHOOP_USER_AGENT = "Mozilla/5.0 (compatible; CheerBot/1.0; +https://alonso.local/whoop-pull)"
WHOOP_BASE_URL = "https://api.prod.whoop.com"
WHOOP_V2_BASE = WHOOP_BASE_URL + "/developer/v2"


def whoop_is_authenticated():
    """Check if Whoop tokens are present and refreshable."""
    if not WHOOP_TOKEN_PATH.exists():
        return False
    try:
        tokens = json.loads(WHOOP_TOKEN_PATH.read_text())
        return bool(tokens.get("refresh_token"))
    except Exception:
        return False


def whoop_get_cached(cache_path=None):
    """Read last successful Whoop V2 pull (cycles, recovery, sleep, workouts).

    `cache_path` lets the caller (gym_web.py / the MCP server) point at its
    own WHOOP_CACHE constant, which is what the test-suite monkeypatches to
    isolate from production state.
    """
    path = Path(cache_path) if cache_path else WHOOP_CACHE_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def whoop_get_token():
    """Read current access token from token file (does NOT refresh)."""
    if not WHOOP_TOKEN_PATH.exists():
        return None
    try:
        tokens = json.loads(WHOOP_TOKEN_PATH.read_text())
        return tokens.get("access_token")
    except Exception:
        return None


def whoop_get_refresh_token():
    """Read refresh token from token file (does NOT refresh)."""
    if not WHOOP_TOKEN_PATH.exists():
        return None
    try:
        tokens = json.loads(WHOOP_TOKEN_PATH.read_text())
        return tokens.get("refresh_token")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# v3.3.25 (Jim OOB 2026-10-20 「Pls use whoop step from now on」)
# WHOOP step extraction from the V2 cycle cache.
#
# Why: `/developer/v2/cycle` returns a top-level `step_count` on every cycle
# record. It has been in the cache since the V2 pipeline was built, but nothing
# ever read it — so every step number in gymbro came from Withings, which
# frequently has no same-day record (-> steps: null) even though Whoop had the
# number all along.
#
# Semantics:
#   - The cycle array is NEWEST-FIRST and cycles[0] is the IN-PROGRESS cycle
#     (`end` is null) => cycles[0].step_count is TODAY's running total.
#   - cycles[1..7] are the last 7 COMPLETE days => 7d average = their mean.
#   - `start`/`end`/`updated_at` are UTC (the payload's separate
#     `timezone_offset` field describes the user's zone, it is NOT a
#     conversion). Everything below converts to HKT before display.
#
# Truthfulness (mirrors the Withings v2.7.25 rule, and adds the device caveat):
#   - Only the in-progress cycle counts as "today". NEVER fall back to a
#     previous day's number.
#   - `updated_at` is Whoop's own last-write stamp on the cycle record. The
#     in-progress cycle only advances while the band is worn, so an old
#     `updated_at` means "band off-body", NOT "API broken". We surface the age
#     and set `band_off` so the UI can honestly say "last known" instead of
#     implying a live running total.
# ---------------------------------------------------------------------------

WHOOP_STALE_MIN = 30       # >30 min since the record was written -> "stale" (UI amber)
WHOOP_BAND_OFF_HOURS = 6   # >6h -> the band was almost certainly off-body


def whoop_get_steps(now=None, cache_path=None):
    """Today's + 7-day step counts from the Whoop V2 cycle cache.

    Returns {} when the cache is missing/unreadable/has no cycles.
    Otherwise a dict with:
      steps, date (HKT), data_as_of (HKT iso), age_min, stale, band_off,
      in_progress, steps_7d_avg, steps_7d_samples, series, _source
    """
    from datetime import datetime, timezone, timedelta

    HKT = timezone(timedelta(hours=8))  # HK has no DST — fixed offset is safe
    cache = whoop_get_cached(cache_path)
    if not isinstance(cache, dict):
        return {}

    cycles = cache.get("cycles")
    if not isinstance(cycles, list):
        # Shape A: nested {"cycle": {"records": [...]}} — gym_web.py's
        # _whoop_strain() already tolerates both shapes, so we must too.
        cycle_root = cache.get("cycle")
        if isinstance(cycle_root, dict):
            cycles = cycle_root.get("records") or []
        else:
            cycles = []
    if not isinstance(cycles, list) or not cycles:
        return {}
    cycles = [c for c in cycles if isinstance(c, dict)]
    if not cycles:
        return {}

    now = now or datetime.now(timezone.utc)

    def _parse(v):
        if not v:
            return None
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    def _as_int(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    # --- today's (in-progress) cycle ------------------------------------
    cur = cycles[0]
    steps_today = _as_int(cur.get("step_count"))
    cur_start = _parse(cur.get("start"))
    cur_updated = _parse(cur.get("updated_at"))
    in_progress = cur.get("end") is None

    date_hkt = cur_start.astimezone(HKT).strftime("%Y-%m-%d") if cur_start else None

    # v3.3.31 (2026-10-21, Jim OOB「the as of time is incorrect — this is
    # showing 08:14 but should be at 11am」):
    #
    # WHOOP's cycle `updated_at` does NOT track the in-progress cycle's
    # step_count. PROVEN 2026-10-21 with three live pulls:
    #     11:22 -> step_count 1042, updated_at 08:14:53
    #     11:45 -> step_count 1159, updated_at 08:14:53
    #     11:46 -> step_count 1342, updated_at 08:14:53
    # The number was advancing in real time while `updated_at` stayed
    # frozen — and recovery/sleep carried the SAME frozen stamp, so it is
    # a record-level stamp, not a "band last wrote" stamp.
    #
    # Using it as the steps freshness marker made the app claim the count
    # was 3h old when it was seconds old — exactly the wrongness Jim
    # reported. The truthful stamp for a live running total is WHEN WE
    # READ THE NUMBER = the cache file's mtime (gymbro's last successful
    # pull). The source stamp is kept as `source_updated_at` and still
    # drives the band-off heuristic.
    _cache_path = Path(cache_path) if cache_path else WHOOP_CACHE_PATH
    try:
        pulled_at = datetime.fromtimestamp(_cache_path.stat().st_mtime, HKT)
    except Exception:
        pulled_at = None

    source_age_min = None
    if cur_updated:
        source_age_min = max(0, int((now - cur_updated).total_seconds() // 60))

    # age = how long ago gymbro last pulled WHOOP (i.e. how fresh the
    # number we are showing actually is).
    age_min = None
    if pulled_at:
        age_min = max(0, int((now - pulled_at).total_seconds() // 60))
    elif cur_updated:
        age_min = source_age_min
    stale = bool(age_min is not None and age_min > WHOOP_STALE_MIN)
    # band_off still uses the SOURCE stamp: if WHOOP itself has not written
    # the record for >6h the band was almost certainly off-body.
    band_off = bool(source_age_min is not None
                    and source_age_min > WHOOP_BAND_OFF_HOURS * 60)

    # --- 7d average over the last 7 COMPLETE cycles ----------------------
    complete = [c for c in cycles[1:8] if c.get("end") is not None]
    vals = [_as_int(c.get("step_count")) for c in complete]
    vals = [v for v in vals if v is not None]
    avg = int(round(sum(vals) / len(vals))) if vals else None

    # --- yesterday = the most recent COMPLETE cycle ----------------------
    # v3.3.25: Withings was fully removed as a step source, so the paired
    # "yesterday" figure now comes from Whoop's own previous cycle.
    steps_yesterday = None
    yesterday_date = None
    if complete:
        _y = complete[0]
        steps_yesterday = _as_int(_y.get("step_count"))
        _ys = _parse(_y.get("start"))
        yesterday_date = _ys.astimezone(HKT).strftime("%Y-%m-%d") if _ys else None

    # --- per-day series (newest first) for cross-checking ----------------
    series = []
    for c in cycles[:8]:
        st = _parse(c.get("start"))
        v = _as_int(c.get("step_count"))
        if st is None or v is None:
            continue
        series.append({
            "date": st.astimezone(HKT).strftime("%Y-%m-%d"),
            "steps": v,
            "in_progress": c.get("end") is None,
        })

    return {
        "steps": steps_today,
        "date": date_hkt,
        # v3.3.31: the as-of stamp is when WE read the number (cache mtime),
        # not WHOOP's frozen `updated_at`. See the long comment above.
        "data_as_of": (pulled_at or cur_updated).astimezone(HKT).isoformat()
                      if (pulled_at or cur_updated) else None,
        "source_updated_at": cur_updated.astimezone(HKT).isoformat() if cur_updated else None,
        "source_age_min": source_age_min,
        "age_min": age_min,
        "stale": stale,
        "band_off": band_off,
        "in_progress": in_progress,
        "steps_yesterday": steps_yesterday,
        "steps_yesterday_date": yesterday_date,
        "steps_7d_avg": avg,
        "steps_7d_samples": len(vals),
        "series": series,
        "_source": "whoop_cycle",
    }


def whoop_get_body(cache_path=None):
    """WHOOP body measurements (weight / height / BMI) from the V2 cache.

    v3.3.25 (Jim OOB 2026-10-20「I mean weight. If whoop has weight data.
    Remove all withings」): WHOOP's body endpoint is
    `/developer/v2/user/measurement/body` (OAuth scope `read:body_measurement`).
    It returns a single object:
        {"height_meter": 1.778, "weight_kilogram": 78.107, "max_heart_rate": 182}

    NOTE: the older pipeline note "`/developer/v2/activity/body_measurement` 404"
    was true but tested the WRONG path — the real one is under /user/measurement/.

    Returns {} when the cache is missing/unreadable/has no body record.
    """
    cache = whoop_get_cached(cache_path)
    if not isinstance(cache, dict):
        return {}
    bm = cache.get("body_measurement")
    if not isinstance(bm, dict) or bm.get("_error"):
        return {}

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    weight = _f(bm.get("weight_kilogram"))
    height = _f(bm.get("height_meter"))
    bmi = None
    if weight and height:
        try:
            bmi = round(weight / (height ** 2), 1)
        except (TypeError, ValueError, ZeroDivisionError):
            bmi = None
    return {
        "weight_kg": weight,
        "height_m": height,
        "bmi": bmi,
        "max_heart_rate": bm.get("max_heart_rate"),
        # WHOOP has NO body-composition sensors (no fat%, muscle, bone or
        # hydration — those need a bioimpedance scale). These stay None so the
        # UI hides them instead of showing a fabricated 0.
        "fat_pct": None,
        "muscle_kg": None,
        "bone_kg": None,
        "hydration_pct": None,
        "_source": "whoop_body",
    }


__all__ = [
    "WHOOP_TOKEN_PATH", "WHOOP_CODE_VERIFIER_PATH", "WHOOP_CACHE_PATH",
    "WHOOP_USER_AGENT", "WHOOP_BASE_URL", "WHOOP_V2_BASE",
    "WHOOP_STALE_MIN", "WHOOP_BAND_OFF_HOURS",
    "whoop_is_authenticated", "whoop_get_cached", "whoop_get_token",
    "whoop_get_refresh_token", "whoop_get_steps", "whoop_get_body",
]
