"""gymbro v3.x core constants + helpers.

Shared utilities used across modules. v3.1.0 first release; legacy code
in gym_web.py still re-implements some of these inline until migrated.
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path
import json

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
VERSION = "3.2.7.22"
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
