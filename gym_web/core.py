"""gymbro v3.x core constants + helpers.

Shared utilities used across modules. v3.1.0 first release; legacy code
in gym_web.py still re-implements some of these inline until migrated.
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import json
import threading
import urllib.parse
import urllib.request

# ---------- Constants ----------
HKT = timezone(timedelta(hours=8))
PORT = 7000
HOST = "0.0.0.0"

# File paths
WORKOUT_LOG = Path("/home/work/.whoop_workout_log.json")
NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")
FOOD_SCAN_LOG = Path("/home/work/.hermes/food_scan_log.json")
NUTRITION_SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
NUTRITION_TAB_NAME = "Nutrition"

# v3.x version — keep in sync with __version__ in gym_web.py
# v3.3.2: removed food_scan_log.json + nutrition_log.json + _save_scan_log +
# _load_scan_log + _append_to_nutrition_log + _load_today_nutrition.
# Sheet + Drive are the only persistent store; cache hydrates from Sheet
# on boot and is wiped on restart.
VERSION = "3.3.2"
GIT_COMMIT = "pre-release"  # updated on tag


# ---------- Time helpers ----------
def now_hkt():
    return datetime.now(HKT)


def today_iso():
    return now_hkt().strftime("%Y-%m-%d")


def now_iso():
    return now_hkt().isoformat()


# ---------- File IO helpers ----------
def safe_read_json(path: Path, default=None):
    """Read JSON safely — never raises to UI."""
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}


def safe_write_json(path: Path, data) -> bool:
    """Write JSON atomically — returns True on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
        return True
    except Exception:
        return False


# ---------- Workout helpers ----------
def detect_intensity(set_n: int, working_target: int = 4) -> str:
    """Auto-detect intensity based on set position in pyramid."""
    if set_n == 1:
        return "warm-up"
    if set_n == 2:
        return "warm-up"
    if set_n <= working_target:
        return "working"
    return "burn-out"


def default_reps() -> int:
    """Jim 7/18 OOB: default reps = 10."""
    return 10


# ---------- v3.x: AI persona constants (P3 unification) ----------
# Single source of truth for coach voice. Used across PWA cheer text,
# Telegram bot replies, MCP tool responses, and future native app.
PERSONA = {
    "name": "Alonso",
    "role": "Jim's fitness coach + butler",
    "language": "繁體中文廣東話",
    "tone": "warm, direct, no corporate speak, no fabrication",
    "particle_examples": ["嘅", "啦", "咗", "嗰", "咁", "嘢", "嚟", "吓", "吖", "喇"],
    "forbidden_words": ["artifact", "endpoint", "pipeline", "cache", "trigger",
                         "process", "deploy", "commit", "backend", "frontend"],
}

# v3.x: Gym focus mode (UX overhaul) — gym tab supports landscape with
# large display + audio player
GYM_FOCUS_DEFAULTS = {
    "auto_landscape": True,  # request landscape on gym tab
    "voice_coach_tips": True,  # TTS coach tip after each set
    "background_audio_allowed": True,  # user can play song + hear tips
    "set_rest_seconds": 90,  # default rest timer
}

# v3.x: Food history landscape grid (UX overhaul) — auto-detect orientation
FOOD_HISTORY_LAYOUT = {
    "portrait": "list",  # vertical cards
    "landscape": "grid-2col",  # 2-column grid for compact timeline
    "switch_breakpoint": "orientation: landscape",  # CSS media query
}


# v3.3.0: Helper for image_proxy lazy backfill. Pulls column K (Drive URL)
# for a single Sheet row. Returns the URL string or None if the row is
# empty / not found. Imported lazily by gym_web.image_proxy to avoid
# pulling the Sheet reader at module-load time.
# v3.3.38: access token 記憶體快取(避免每次 Drive 查詢都做一次 OAuth refresh)
#   之前 get_google_access_token() 每次都 refresh → 餐牌 JPG 拉 7 張縮圖
#   = 7 次 token refresh(每次 ~300ms)+ 7 次 Drive metadata call → 慢 3-4 秒。
#   Google access token 有效期 1 鐘頭,快取 45 分鐘好安全。
_ACCESS_TOKEN_CACHE: dict = {"token": None, "expires_at": 0.0}
_ACCESS_TOKEN_TTL_S = 45 * 60
_ACCESS_TOKEN_LOCK = threading.Lock()


def get_google_access_token(force_refresh: bool = False) -> Optional[str]:
    """Return a Google access token via the refresh-token flow (cached 45 min).

    v3.3.38: extracted out of get_row_drive_url so gym_web.image_proxy can
    reuse it without a circular import (core never imports image_proxy).
    Token 快取避免每次 Drive metadata 查詢都做 OAuth refresh。

    Returns the access_token string, or None when the token file is
    missing/incomplete or the refresh call fails.
    """
    if not force_refresh:
        import time as _time
        with _ACCESS_TOKEN_LOCK:
            cached = _ACCESS_TOKEN_CACHE.get("token")
            if cached and _time.time() < _ACCESS_TOKEN_CACHE.get("expires_at", 0):
                return cached
    token_path = Path("/home/work/.hermes/google_token.json")
    try:
        tok = json.loads(token_path.read_text())
        refresh = tok.get("refresh_token")
        client_id = tok.get("client_id")
        client_secret = tok.get("client_secret")
        if not (refresh and client_id and client_secret):
            print(f"[core.get_google_access_token] token file incomplete: {token_path}", flush=True)
            return None
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        access = json.loads(urllib.request.urlopen(req, timeout=10).read())["access_token"]
        import time as _time
        with _ACCESS_TOKEN_LOCK:
            _ACCESS_TOKEN_CACHE["token"] = access
            _ACCESS_TOKEN_CACHE["expires_at"] = _time.time() + _ACCESS_TOKEN_TTL_S
        return access
    except Exception as e:
        print(f"[core.get_google_access_token] failed: {e}", flush=True)
        return None


def drive_file_is_trashed(file_id: str) -> Optional[bool]:
    """Is this Drive file in the trash? None = unknown (API error).

    v3.3.38: Drive 對住 trashed 檔會回**同一張**「已掉進垃圾桶」placeholder
    JPEG。2026-10-21 Jim 見到「昨日餐牌縮圖全部一樣」,根因正係多行同時
    指向已被 trash 嘅檔 → 縮圖 byte 级一模一樣。呢個 helper 俾
    image_proxy 喺派圖之前先查清楚,避免派錯圖。

    Args:
        file_id: bare Drive file id (no URL).

    Returns True (trashed), False (live), None (couldn't tell).
    """
    if not file_id:
        return None
    access = get_google_access_token()
    if not access:
        return None
    try:
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=id,trashed"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
        body = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return bool(body.get("trashed"))
    except Exception as e:
        print(f"[core.drive_file_is_trashed] failed for {file_id}: {e}", flush=True)
        return None


def get_row_drive_url(row_index: int) -> Optional[str]:
    """Fetch Drive Image URL (column K) for a single Sheet row.

    Used by image_proxy._resolve_drive_url when the per-row URL cache misses.
    Reads `Nutrition!A{row_index}:K{row_index}` from the configured Sheet.

    Args:
        row_index: 1-based Sheet row (1 = header). Must be >= 1.

    Returns:
        The URL string from column K, or None if the row is empty / doesn't
        exist / has no Drive URL.
    """
    if row_index < 2:  # row 1 is the header — no Drive URL there
        return None
    try:
        access = get_google_access_token()
        if not access:
            return None
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{NUTRITION_SHEET_ID}/values/"
            f"{NUTRITION_TAB_NAME}!A{row_index}:K{row_index}"
        )
        req2 = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
        body = json.loads(urllib.request.urlopen(req2, timeout=10).read())
        values = body.get("values") or []
        if not values:
            return None
        cells = values[0]
        return cells[10].strip() if len(cells) > 10 and cells[10].strip() else None
    except Exception:
        return None
