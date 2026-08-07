"""gymbro Withings integration (v3.1.0 modular).

Withings OAuth2 + body composition + steps.
Apple HealthKit is the canonical step source for Jim
(verified 2026-07-29 - modelid 1059 brand 18 is_tracker false).

Endpoints owned (planned v3.2.0 migration):
- /api/withings/steps_today (with sync-delay honesty)
- /api/withings/steps_7d_avg
- /api/withings/weight
- /api/withings/refresh
"""

from pathlib import Path
import json

WITHINGS_TOKEN_PATH = Path("/home/work/.withings_token.json")
WITHINGS_CACHE_PATH = Path("/home/work/.withings_latest_cache.json")

# Apple HealthKit is the source of truth for Jim's steps
HEALTHKIT_MODEL_ID = 1059
HEALTHKIT_BRAND_ID = 18


def withings_is_authenticated():
    """Check if Withings tokens are present."""
    if not WITHINGS_TOKEN_PATH.exists():
        return False
    try:
        tokens = json.loads(WITHINGS_TOKEN_PATH.read_text())
        return bool(tokens.get("access_token"))
    except Exception:
        return False


def withings_get_cached():
    """Read last successful Withings pull."""
    if not WITHINGS_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(WITHINGS_CACHE_PATH.read_text())
    except Exception:
        return {}


def withings_get_steps_today():
    """Return today's steps from cache. None if syncing / not yet pulled."""
    cache = withings_get_cached()
    steps = cache.get("steps", {})
    return steps.get("today")


def withings_get_steps_yesterday():
    """Return yesterday's steps from cache."""
    cache = withings_get_cached()
    steps = cache.get("steps", {})
    return steps.get("yesterday")


__all__ = [
    "WITHINGS_TOKEN_PATH", "WITHINGS_CACHE_PATH",
    "HEALTHKIT_MODEL_ID", "HEALTHKIT_BRAND_ID",
    "withings_is_authenticated", "withings_get_cached",
    "withings_get_steps_today", "withings_get_steps_yesterday",
]
