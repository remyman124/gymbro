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


def whoop_get_cached():
    """Read last successful Whoop V2 pull (cycles, recovery, sleep, workouts)."""
    if not WHOOP_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(WHOOP_CACHE_PATH.read_text())
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
    """Read refresh token from token file."""
    if not WHOOP_TOKEN_PATH.exists():
        return None
    try:
        tokens = json.loads(WHOOP_TOKEN_PATH.read_text())
        return tokens.get("refresh_token")
    except Exception:
        return None


__all__ = [
    "WHOOP_TOKEN_PATH", "WHOOP_CODE_VERIFIER_PATH", "WHOOP_CACHE_PATH",
    "WHOOP_USER_AGENT", "WHOOP_BASE_URL", "WHOOP_V2_BASE",
    "whoop_is_authenticated", "whoop_get_cached", "whoop_get_token", "whoop_get_refresh_token",
]
