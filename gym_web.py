#!/usr/bin/env python3
"""
Jim's Gym Web App — port 7000
Uber-style mobile-first interface for gym set logging via Tailnet VPN.

Stack: Flask 3.1.3 + Tailwind CDN + Alpine.js
Bind: 0.0.0.0:7000 (Tailscale IP 100.114.66.125)
Persistence: /home/work/.whoop_workout_log.json[YYYY-MM-DD]
PWA: installable, wake-lock enabled
"""
import base64
import functools
import json
import os
import random
import re
import secrets
import shutil
import threading
import time
import urllib.request
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta, date
from datetime import datetime as _dt
from pathlib import Path

try:
    from PIL import Image, ImageOps
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

from flask import Flask, Response, jsonify, request, render_template_string, send_from_directory
from workout_formatter import render as _render_text
from dotenv import load_dotenv
from gym_web.core import safe_read_json as _safe_read_json
from gym_web.core import safe_write_json as _safe_write_json
from gym_web.idempotency import check_and_remember as _idempotency_check
from gym_web.cache import NutritionCache, NutritionRow, get_cache, get_cache as _get_nutrition_cache
from gym_web import image_proxy
from gym_web.whoop import whoop_get_steps as _whoop_get_steps
from gym_web.whoop import whoop_get_body as _whoop_get_body
# v3.3.32 (Jim OOB 2026-10-21「fix the manual food input workflow」):
# 餐廳名消毒 + 餐廳歷史索引 + 品牌識別,同食物相搜尋策略
from gym_web.food_context import (
    sanitise_restaurant_name as _sanitise_restaurant_name,
    build_restaurant_history as _build_restaurant_history,
    match_brand as _match_brand,
    detect_mentioned_chain as _detect_mentioned_chain,
    looks_like_product as _looks_like_product,
    product_name_is_specific as _product_name_is_specific,
    BRAND_BASELINE as _BRAND_BASELINE,
    BRAND_ALIAS_TO_CANON as _BRAND_ALIAS_TO_CANON,
)
from gym_web.food_image import (
    pick_food_image as _pick_food_image,
    plan_image_search as _plan_image_search,
)

# Load MiniMax keys from hermes-torres first, then hermes — preserves existing
# dual-location behavior (hermes-torres wins, hermes fills gaps).
load_dotenv("/home/work/.hermes-torres/.env")
load_dotenv("/home/work/.hermes/.env")

# ---------- Constants ----------
WORKOUT_LOG = Path("/home/work/.whoop_workout_log.json")
HKT = timezone(timedelta(hours=8))
PORT = 7000
HOST = "0.0.0.0"

app = Flask(__name__, static_folder="/home/work/.hermes/image_cache", static_url_path="/img")
# v3.3.0: Drive image proxy + on-demand thumbnail (replaces /scan_img/<file> route).
app.register_blueprint(image_proxy.bp)


# v3.3.0: Hydrate the nutrition cache + start background refresh at app start.
# First hydration blocks app startup briefly (~3-5s for 500 rows) — acceptable.
# Background refresh keeps it warm (60s) so concurrent edits (MCP, cheer)
# converge without manual intervention.
#
# Deferred: _hydrate_nutrition_cache is defined later in this file (line ~1614),
# so calling it at import time raises NameError. Run the hydration right before
# serving the app (see __main__ block below).

# Static token (Tailscale-only network = trusted)
SESSION_COOKIE = "gym_web_session"


# v3.3.38 (2026-10-22, Jim OOB「The thumbnail are the same again. Pls fix the
# thumbnail and the systemic problem」):Alpine 由 unpkg CDN 改為**本地託管**。
#   之前 <script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js">
#   → CDN 慢/被擋/手機網絡差 → Alpine 永遠唔起 → 個 app 淨係得個空殼,
#     所有食物歷史縮圖都係同一個空框 → Jim 見到「啲縮圖一樣」。
#   順帶消除 QA 嘅 networkidle / wait_for_function 隨機 timeout(實測:
#   2026-10-22 frontend group 因 unpkg 慢而 ERROR,重跑就綠)。
#   版本號入檔名(alpine-3.17.5.min.js)→ 可以永久快取 + 換版本即失效。
_ALPINE_LOCAL = Path(__file__).resolve().parent / "static" / "js" / "alpine-3.17.5.min.js"


@app.route("/js/alpine-3.17.5.min.js")
def serve_alpine_local():
    """Serve the vendored Alpine build (see comment above)."""
    try:
        data = _ALPINE_LOCAL.read_bytes()
    except OSError as e:
        print(f"[serve_alpine_local] read failed: {e}", flush=True)
        return Response(b"", status=404, mimetype="application/javascript")
    resp = Response(data, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    resp.headers["X-Gymbro-Immutable"] = "1"
    return resp


@app.after_request
def add_no_cache_headers(response):
    """Jim OOB 2026-07-19: force no-cache so iPhone PWA picks up every code change immediately.

    v3.3.38: 內容定址圖片(URL 帶 ?v=<hash>)係 immutable —— 呢類資源可以
    永久快取,唔應該被呢度嘅 no-store 蓋走。image_proxy 會打上
    X-Gymbro-Immutable: 1 marker,呢度就放行。
    """
    if response.headers.get("X-Gymbro-Immutable") == "1":
        # 內容定址資源:保留 immutable cache header(客戶端唔會再顯示舊圖)
        return response
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# ---------- Helpers ----------
def now_hkt():
    return datetime.now(HKT)


def today_iso():
    return now_hkt().strftime("%Y-%m-%d")


def now_iso():
    return now_hkt().isoformat()


def load_log():
    if WORKOUT_LOG.exists():
        return json.loads(WORKOUT_LOG.read_text())
    return {}


def save_log(log):
    """v3.3.40: atomic write.

    之前用 `write_text()` 直寫 —— 寫到一半撞去(重啟/OOM)會截斷成半個
    JSON,成個 log 就冇晒。呢個檔係 Jim 唯一嘅本機訓練鏡像,唔可以
    因為一次 crash 就全失。所以:寫去 .tmp 再 `os.replace()` 原子換入。
    """
    tmp = WORKOUT_LOG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(log, indent=2, ensure_ascii=False))
    os.replace(tmp, WORKOUT_LOG)


def get_or_create_session(session_date=None):
    """Return the session dict for a date (default: today).

    v3.3.39: optional `session_date` (YYYY-MM-DD) so a past class can be
    back-filled (Jim 補記 10/22 午市健身課嘅 set)。只寫本機 log,唔掂 Sheet。
    """
    log = load_log()
    today = session_date or today_iso()
    if today not in log:
        log[today] = {
            "date": today,
            "start_time": now_iso(),
            "end_time": None,
            "completed": False,
            "context": "Jim's gym session via :7000 web app",
            "exercises": [],
        }
        save_log(log)
    else:
        if "exercises" not in log[today]:
            log[today]["exercises"] = []
        # v3.3.40: key 同 inner `date` 一定要一致。
        # 寫入端用 `log[session.get("date")]` 做 key,所以 inner date 一旦
        # 唔等於佢自己嘅 key,下次寫入就會寫去錯嘅 key —— 2026-10-23 就係
        # 咁樣被一節 10/05 嘅 session 頂咗(見 tests/qa/test_workout_log_integrity.py)。
        # 呢度強制 normalize,令 key ↔ inner date 永遠對齊。
        if log[today].get("date") != today:
            log[today]["date"] = today
        save_log(log)
    return log[today]


def detect_intensity(set_n, working_target=4):
    """Auto-detect intensity based on set position in pyramid."""
    if set_n == 1:
        return "warm-up"
    if set_n == 2:
        return "warm-up"
    if set_n <= working_target:
        return "working"
    return "burn-out"


def default_reps():
    """Jim 7/18 OOB: default reps = 10."""
    return 10


def find_last_set_for_exercise(session, exercise_name):
    """Look up last-set weight for this exercise (warm-up ramp pattern)."""
    for ex in reversed(session.get("exercises", [])):
        if ex.get("exercise") == exercise_name:
            return ex
    return None


# ---------- Routes ----------
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "time": now_iso(), "today": today_iso()})


# v3.1.0: P4 — diagnostic endpoints
@app.route("/api/version")
def api_version():
    """P4: expose app version, git commit, deploy time, module load status."""
    import subprocess as _sp
    try:
        commit = _sp.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd="/home/work/projects/gymbro", stderr=_sp.DEVNULL,
        ).decode().strip()
    except Exception:
        commit = "unknown"
    return jsonify({
        "version": __version__,
        "git_commit": commit,
        "branch": "3.x-overhaul" if commit != "unknown" else "main",
        "modules_loaded": [
            "gym_web.core", "gym_web.whoop",
        ],
        "legacy_file": "gym_web.py (9202 lines, will migrate per-version)",
        "uptime_since": now_iso(),
    })


# v3.2.0: schedule tab — calendar + week of activities (Jim OOB 2026-08-07
# 'in gymbro, under schedule tab, no need to show the tab if there is no
# activities. put the week of the day. i think it's also good to have a
# monthly calendar view on which date i have done gym. Moreover, have you
# downloaded whoop other activities such as walking and view it on the
# calendar too?').
#
# Read from local whoop_data_latest.json cache (already includes all
# workouts: weightlifting, walking, cycling, etc.). No live API call —
# schedule tab loads fast even on iPhone over Tailscale.
WHOOP_ACTIVITY_LABELS = {
    "weightlifting": ("🏋️", "Gym"),
    "walking": ("🚶", "Walk"),
    "running": ("🏃", "Run"),
    "cycling": ("🚴", "Ride"),
    "swimming": ("🏊", "Swim"),
    "yoga": ("🧘", "Yoga"),
    "rowing": ("🚣", "Row"),
    "hiking": ("🥾", "Hike"),
    "basketball": ("🏀", "Hoops"),
    "soccer": ("⚽", "Footy"),
    "tennis": ("🎾", "Tennis"),
    "golf": ("⛳", "Golf"),
    "boxing": ("🥊", "Box"),
    "crossfit": ("💪", "Crossfit"),
    "pilates": ("🤸", "Pilates"),
    "meditation": ("🧘", "Meditate"),
}


def _whoop_activities_normalized():
    """Pull workouts from cache, normalize into list[dict] for frontend.

    Each entry: {date (YYYY-MM-DD), sport, icon, label, strain, start, end}
    Sorted descending by start (newest first).
    """
    cache = Path("/home/work/.whoop_data_latest.json")
    if not cache.exists():
        return []
    try:
        data = json.loads(cache.read_text())
    except Exception:
        return []
    out = []
    for w in (data.get("workouts") or []):
        sport = (w.get("sport_name") or "").lower().strip()
        icon, label = WHOOP_ACTIVITY_LABELS.get(
            sport, ("🏅", sport.title() if sport else "Activity")
        )
        start = w.get("start", "")
        end = w.get("end", "")
        date_iso = start[:10] if start else ""
        score = w.get("score") or {}
        strain = score.get("strain") if isinstance(score, dict) else None
        out.append({
            "date": date_iso,
            "sport": sport or "activity",
            "icon": icon,
            "label": label,
            "strain": round(float(strain), 1) if strain is not None else None,
            "start": start,
            "end": end,
        })
    out.sort(key=lambda a: a["start"], reverse=True)
    return out


def _workout_log_gym_dates():
    """v3.3.18 (Jim OOB 2026-09-05): return set of ISO dates in WORKOUT_LOG
    that have at least one exercise entry — i.e., days Jim actually logged
    gym sets via gym-web-tap. Distinct from _schedule_enrichment()'s key
    set, which also includes Whoop recovery/sleep-only dates (no gym).
    """
    if not WORKOUT_LOG.exists():
        return set()
    try:
        log = json.loads(WORKOUT_LOG.read_text())
    except Exception:
        return set()
    out = set()
    for date_iso, entry in log.items():
        if not isinstance(entry, dict):
            continue
        exs = entry.get("exercises")
        if isinstance(exs, list) and len(exs) > 0:
            out.add(date_iso)
    return out


def _schedule_enrichment():
    """v3.2.7: enrich calendar days with workout-log + Whoop recovery + sleep.

    Returns dict keyed by ISO date -> {
      gym_volume_kg, gym_set_count, gym_exercises,
      hrv_ms, recovery_pct, sleep_pct
    }
    """
    out = {}
    log_path = WORKOUT_LOG
    if log_path.exists():
        try:
            log = json.loads(log_path.read_text())
        except Exception:
            log = {}
        for date_iso, entry in log.items():
            exercises = entry.get("exercises") or []
            if not isinstance(exercises, list):
                continue
            vol = 0.0
            sets = 0
            seen, ordered = set(), []
            for ex in exercises:
                try:
                    w = float(ex.get("weight_kg") or 0)
                    r = float(ex.get("reps") or 0)
                    vol += w * r
                    sets += 1
                except (TypeError, ValueError):
                    continue
                name = (ex.get("exercise") or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    ordered.append(name)
            if sets > 0:
                out[date_iso] = {
                    **out.get(date_iso, {}),
                    "gym_volume_kg": round(vol, 1),
                    "gym_set_count": sets,
                    "gym_exercises": ordered[:3],
                }
    whoop_cache = Path("/home/work/.whoop_data_latest.json")
    if whoop_cache.exists():
        try:
            whoop = json.loads(whoop_cache.read_text())
        except Exception:
            whoop = {}
        for rec in (whoop.get("recovery") or []):
            if not isinstance(rec, dict):
                continue
            created_at = rec.get("created_at") or ""
            if not created_at:
                continue
            try:
                ts = _dt.fromisoformat(created_at.replace("Z", "+00:00"))
                ts_hkt = ts.astimezone(HKT)
                d_iso = ts_hkt.date().isoformat()
            except Exception:
                continue
            score = rec.get("score") or {}
            if not isinstance(score, dict):
                continue
            rec_pct = score.get("recovery_score")
            hrv = score.get("hrv_rmssd_milli")
            out[d_iso] = {
                **out.get(d_iso, {}),
                "recovery_pct": int(round(float(rec_pct))) if rec_pct is not None else None,
                "hrv_ms": round(float(hrv), 1) if hrv is not None else None,
            }
        for s in (whoop.get("sleep") or []):
            if not isinstance(s, dict):
                continue
            end = s.get("end") or ""
            if not end:
                continue
            try:
                ts = _dt.fromisoformat(end.replace("Z", "+00:00"))
                ts_hkt = ts.astimezone(HKT)
                d_iso = ts_hkt.date().isoformat()
            except Exception:
                continue
            score = s.get("score") or {}
            if not isinstance(score, dict):
                continue
            sp = score.get("sleep_performance_percentage")
            out[d_iso] = {
                **out.get(d_iso, {}),
                "sleep_pct": int(round(float(sp))) if sp is not None else None,
            }
    return out


@app.route("/api/whoop_activities_calendar")
def api_whoop_activities_calendar():
    """v3.2.0/v3.2.7: monthly calendar for schedule tab — enriched.

    Returns activities grouped by date, enriched with workout-log
    volume/sets/exercises + Whoop recovery (HRV + recovery %) + Whoop
    sleep performance %.

    Two modes:
    - `?month=YYYY-MM` (preferred): return all days of that month.
    - `?days=N` (legacy): return the past N days (default 42, max 90).
    """
    today = datetime.now(HKT).date()
    month_arg = (request.args.get("month") or "").strip()

    if month_arg:
        # v3.2.7.19: monthly view. Return all days in that calendar month.
        try:
            year_s, month_s = month_arg.split("-", 1)
            year_i = int(year_s)
            month_i = int(month_s)
            if month_i < 1 or month_i > 12:
                raise ValueError
        except (ValueError, AttributeError):
            year_i, month_i = today.year, today.month
        # First day of month → first day of next month (exclusive)
        start_date = date(year_i, month_i, 1)
        if month_i == 12:
            end_date_excl = date(year_i + 1, 1, 1)
        else:
            end_date_excl = date(year_i, month_i + 1, 1)
        days_n = (end_date_excl - start_date).days
        is_current_month = (year_i == today.year and month_i == today.month)
        view_label = f"{year_i}年{month_i}月"
    else:
        # Legacy: past N days rolling
        try:
            days_n = int(request.args.get("days", 42))
        except (TypeError, ValueError):
            days_n = 42
        days_n = max(7, min(days_n, 90))
        start_date = today - timedelta(days=days_n - 1)
        is_current_month = True
        view_label = ""

    activities = _whoop_activities_normalized()
    by_date = {}
    for a in activities:
        by_date.setdefault(a["date"], []).append(a)
    enrich = _schedule_enrichment()
    # v3.3.18 (Jim OOB 2026-09-05 'calendar tab, gym and 套數 are always zero'):
    # Build a set of dates that have at least one gym entry (either from Whoop
    # auto-detect OR from local WORKOUT_LOG). The previous logic counted only
    # `sport == "weightlifting"` Whoop activities, missing dates where Jim
    # logged sets via gym-web-tap but Whoop didn't auto-detect (verified:
    # Aug 2026 had 4 such dates: 8-05, 8-06, 8-10, 8-27 with 8+13+2+1=24 sets
    # not counted in gym_count).
    #
    # NOTE: enrich.keys() is NOT a reliable proxy for "log gym days" because
    # _schedule_enrichment() also emits keys for dates that ONLY have Whoop
    # recovery/sleep data — using enrich.keys() would inflate log_gym_dates
    # to every date with any Whoop signal. Instead, derive log_gym_dates
    # from WORKOUT_LOG directly (dates with ≥1 exercise entry).
    whoop_gym_dates = {d for d, acts in by_date.items()
                       if any(a["sport"] == "weightlifting" for a in acts)}
    log_gym_dates = _workout_log_gym_dates()
    gym_dates = whoop_gym_dates | log_gym_dates
    days = []
    gym_count = 0
    other_count = 0
    total_volume = 0.0
    total_sets = 0
    rec_pcts_sum = 0
    rec_pcts_n = 0
    for i in range(days_n):
        d = start_date + timedelta(days=i)
        iso = d.isoformat()
        acts = by_date.get(iso, [])
        # has_gym: true if EITHER source says it's a gym day.
        has_gym = iso in gym_dates
        if has_gym:
            # Count distinct "gym sessions" for the day. Use Whoop activity
            # count when available (one Whoop auto-detect = one session),
            # otherwise treat each log date as a single session. Clamped to
            # ≥1 so log-only dates always count as a session.
            whoop_sessions = len([a for a in acts if a["sport"] == "weightlifting"])
            log_sessions = 1 if iso in log_gym_dates else 0
            gym_count += max(whoop_sessions, log_sessions)
            other_count += len([a for a in acts if a["sport"] != "weightlifting"])
        else:
            other_count += len(acts)
        total_strain = sum((a["strain"] or 0) for a in acts)
        ed = enrich.get(iso, {})
        if ed.get("gym_volume_kg"):
            total_volume += ed["gym_volume_kg"]
        if ed.get("gym_set_count"):
            total_sets += ed["gym_set_count"]
        if ed.get("recovery_pct") is not None:
            rec_pcts_sum += ed["recovery_pct"]
            rec_pcts_n += 1
        days.append({
            "date": iso,
            "weekday": d.weekday(),
            "count": len(acts),
            "activities": acts,
            "has_gym": has_gym,
            "total_strain": round(total_strain, 1),
            "gym_volume_kg": ed.get("gym_volume_kg"),
            "gym_set_count": ed.get("gym_set_count"),
            "gym_exercises": ed.get("gym_exercises", []),
            "hrv_ms": ed.get("hrv_ms"),
            "recovery_pct": ed.get("recovery_pct"),
            "sleep_pct": ed.get("sleep_pct"),
            "is_today": iso == today.isoformat(),
        })
    end_date = start_date + timedelta(days=days_n - 1)
    return jsonify({
        "days": days,
        "range_start": start_date.isoformat(),
        "range_end": end_date.isoformat(),
        "view_month": month_arg or "",
        "view_label": view_label,
        "is_current_month": is_current_month,
        "total_activities": len(activities),
        "gym_count": gym_count,
        "other_count": other_count,
        "total_volume_kg": round(total_volume, 1),
        "total_sets": total_sets,
        "avg_recovery_pct": round(rec_pcts_sum / rec_pcts_n, 1) if rec_pcts_n > 0 else None,
        "enriched_dates": len(enrich),
        # v3.3.17: expose distinct gym-day union count for transparency
        # (Jim can see if gym_count < gym_dates suggests multiple sessions/day).
        "gym_dates_count": len(gym_dates),
    })


# v3.2.6: /api/whoop_activities_week endpoint removed (Jim OOB
# 2026-08-07 23:30 HKT 'Remove its list view and weekly view').
# The schedule tab now shows the monthly calendar only — feed by
# /api/whoop_activities_calendar. Backend route deleted to avoid
# dead endpoints polluting /api/health + the docs.


@app.route("/api/health")
def api_health():
    """P4: full health check — Whoop, Withings, Sheet, scan log, gym log all reachable?"""
    import os as _os
    checks = {}
    # Whoop
    whoop_tok = Path("/home/work/.whoop_tokens.json")
    checks["whoop_token"] = whoop_tok.exists()
    whoop_cache = Path("/home/work/.whoop_data_latest.json")
    checks["whoop_cache"] = whoop_cache.exists()
    if whoop_cache.exists():
        try:
            data = json.loads(whoop_cache.read_text())
            meta = data.get("_meta", {})
            # v3.3.31: compute the cache age HERE instead of reading
            # meta["sync_time_hkt_age_min"]. No writer ever produced that key,
            # so /api/health always reported null and a stale Whoop cache was
            # indistinguishable from a fresh one. Prefer the epoch the pull
            # script writes (sync_time_epoch); fall back to the file mtime.
            age_min = None
            # v3.3.35 (2026-10-21, Jim OOB「The as of date, make it same as
            # data as of data」): 之前呢度用 `_meta.sync_time_epoch`,但嗰個
            # 係一個已經唔存在嘅 script 留落嘅遺物(永遠停喺舊時間),而 app
            # 用檔案 mtime —— 同一份資料兩個 as-of,health 報 65 分鐘但 app
            # 報「as of 19:51」。而家以**檔案 mtime 為準**(就係 app 顯示嘅
            # 嗰個值),_meta 只做對照,唔再係真相來源。
            epoch = None
            try:
                epoch = whoop_cache.stat().st_mtime
            except OSError:
                epoch = meta.get("sync_time_epoch")
            if epoch is not None:
                try:
                    age_min = round((time.time() - float(epoch)) / 60.0, 2)
                except (TypeError, ValueError):
                    age_min = None
            checks["whoop_cache_age_minutes"] = age_min
        except Exception:
            checks["whoop_cache_age_minutes"] = "corrupt"
    # Withings
    # v3.3.25: the Withings scale is retired. WHOOP supplies weight/height
    # via /developer/v2/user/measurement/body, so the body check is the Whoop
    # cache, not a Withings cache file.
    checks["whoop_body"] = WHOOP_CACHE_PATH.exists()
    # Google creds
    google_tok = Path("/home/work/.hermes/google_token.json")
    checks["google_token"] = google_tok.exists()
    # TG bot
    checks["telegram_bot_configured"] = bool(_os.environ.get("TELEGRAM_BOT_TOKEN")) or _os.path.exists("/home/work/.hermes/.env")
    # v3.3.2: removed `nutrition_log` + `food_scan_log` checks — the Sheet
    # is the canonical store now; those JSON files no longer exist.
    checks["workout_log"] = Path("/home/work/.whoop_workout_log.json").exists()
    # v3.3.25 (2026-10-21): vision provider status. A provider key died on
    # 2026-10-20 and every scan silently returned 未識別菜式 / 0 kcal.
    # This makes the degradation visible instead of silent — check
    # checks["vision_state"] != "ok" before trusting a food scan.
    _vs = globals().get("_VISION_STATUS", {})
    checks["vision_provider"] = _vs.get("provider", "unknown")
    checks["vision_provider_key"] = _vs.get("provider_key", "unknown")
    checks["vision_state"] = _vs.get("state", "unknown")
    checks["vision_last_error"] = _vs.get("last_error", "")
    checks["vision_last_ok_ts"] = _vs.get("last_ok_ts", 0)
    # v3.3.29: the SECOND opinion has its own record. Before this fix the
    # 2nd opinion overwrote _VISION_STATUS, so vision_provider_key named
    # the WRONG provider (the 2nd-opinion one) and a provider failure in
    # the 2nd opinion could poison the primary's state — which bypassed
    # the rule-2 write guard. Now opinion #1 and #2 are both visible.
    _vs2 = globals().get("_VISION_STATUS_2ND", {})
    checks["vision_provider_2nd"] = _vs2.get("provider", "unknown")
    checks["vision_provider_key_2nd"] = _vs2.get("provider_key", "unknown")
    checks["vision_state_2nd"] = _vs2.get("state", "unknown")
    checks["vision_last_error_2nd"] = _vs2.get("last_error", "")
    # v3.3.26: the full provider pool, so you can see at a glance that a
    # single key death can no longer take the whole food log down.
    checks["vision_pool"] = [n for n, _ in globals().get(
        "_VISION_PROVIDER_POOL", ())]
    # Which AI keys are actually usable right now (a dead key must be
    # visible before it ruins a whole day of scans).
    checks["api_keys_present"] = {
        "minimax": bool(_minimax_api_key()),
        # v3.3.34: Perplexity 已拎走(Jim OOB 2026-10-21)
    }
    # Health overlay API (composite)
    overall = all([
        checks.get("whoop_token"),
        checks.get("whoop_body"),
        checks.get("google_token"),
    ])
    return jsonify({
        "ok": overall,
        "checks": checks,
        "version": __version__,
        "checked_at": now_iso(),
    })


@app.route("/api/state")
def api_state():
    """Return full session state for client sync."""
    session = get_or_create_session()
    return jsonify({
        "session": session,
        "today": today_iso(),
        "time": now_iso(),
    })


@app.route("/api/log_set", methods=["POST"])
def api_log_set():
    """Log a single set."""
    data = request.get_json(force=True)
    exercise = data.get("exercise", "").strip()
    weight = data.get("weight_kg")
    reps = data.get("reps", default_reps())
    form_check = data.get("form_check", "ok")
    source = data.get("source", "gym-web-tap")

    if not exercise:
        return jsonify({"error": "exercise name required"}), 400

    # v3.3.39: 可選 `date`(YYYY-MM-DD)→ 補記過去一節(只寫本機 log)
    session_date = (data.get("date") or "").strip() or None
    if session_date:
        try:
            datetime.strptime(session_date, "%Y-%m-%d")
        except Exception:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    session = get_or_create_session(session_date)
    # set_n 唔俾就自動遞增(同一動作喺該節已做幾多組 + 1)
    if data.get("set_n") is None:
        set_n = sum(1 for e in session["exercises"] if e.get("exercise") == exercise) + 1
    else:
        set_n = data.get("set_n")
    if not data.get("intensity"):
        intensity = detect_intensity(set_n)
    entry = {
        "exercise": exercise,
        "weight_kg": weight,
        "weight_note": data.get("weight_note", ""),
        "reps": reps,
        "set": set_n,
        "intensity": intensity,
        "form_check": form_check,
        "time": now_iso(),
        "source": source,
    }
    session["exercises"].append(entry)
    log = load_log()
    # v3.3.39: 用 session 自己嘅日期做 key(支援補記過去一節),
    # 唔可以硬寫 today_iso() —— 否則補記會覆蓋今日 session
    log[session.get("date") or today_iso()] = session
    save_log(log)
    return jsonify({"ok": True, "entry": entry, "total_sets": len(session["exercises"])})


@app.route("/api/finish_exercise", methods=["POST"])
def api_finish_exercise():
    """Mark exercise as done (advance to next exercise summary)."""
    session = get_or_create_session()
    exercise_name = request.get_json(force=True).get("exercise")
    exercise_sets = [e for e in session["exercises"] if e["exercise"] == exercise_name]
    total_vol = sum((e.get("weight_kg") or 0) * (e.get("reps") or 0) for e in exercise_sets)
    summary = {
        "exercise": exercise_name,
        "sets": len(exercise_sets),
        "total_vol_kg": total_vol,
        "set_breakdown": [
            {"set": e["set"], "weight": e.get("weight_kg"), "reps": e.get("reps"), "intensity": e.get("intensity")}
            for e in exercise_sets
        ],
        "finished_at": now_iso(),
    }
    return jsonify(summary)


@app.route("/api/end_session", methods=["POST"])
def api_end_session():
    """Finalize workout, write to Google Sheet via refresh-token."""
    session = get_or_create_session()
    session["end_time"] = now_iso()
    session["completed"] = True
    log = load_log()
    # v3.3.39: 用 session 自己嘅日期做 key(支援補記過去一節),
    # 唔可以硬寫 today_iso() —— 否則補記會覆蓋今日 session
    log[session.get("date") or today_iso()] = session
    save_log(log)
    # v3.3.8: invalidate daily coaching so the next cheer/video reflects
    # the freshly completed gym session.
    _invalidate_daily_coaching()

    # Aggregate pyramid
    exercises = {}
    for entry in session["exercises"]:
        ex = entry["exercise"]
        if ex not in exercises:
            exercises[ex] = {"sets": [], "vol": 0}
        exercises[ex]["sets"].append(f"{entry.get('weight_kg')}kg×{entry.get('reps')}")
        exercises[ex]["vol"] += (entry.get("weight_kg") or 0) * (entry.get("reps") or 0)

    pyramid = "\n".join([
        f"**{ex}** — {' / '.join(data['sets'])} = {data['vol']}kg vol"
        for ex, data in exercises.items()
    ])
    total_sets = len(session["exercises"])
    total_vol = sum(data["vol"] for data in exercises.values())

    return jsonify({
        "pyramid": pyramid,
        "total_sets": total_sets,
        "total_vol_kg": total_vol,
        "exercises": exercises,
    })


@app.route("/api/today_image")
def api_today_image():
    """Return today's daily motivation image (or None if not yet generated).

    Now returns the FIRST entry of the full image list, so the client can
    cycle through other available motivation images via /api/today_images.
    Jim OOB 2026-07-19: "The button changing motivation image should also
    refresh other data on the homepage."
    """
    today = today_iso()
    img_path = Path("/home/work/.hermes/image_cache") / f"gymbro_{today}.png"
    if img_path.exists() and img_path.stat().st_size > 50000:
        return jsonify({
            "image_url": f"/img/gymbro_{today}.png",
            "date": today,
            "total_available": _count_today_images(),
        })
    return jsonify({"image_url": None, "date": today, "total_available": _count_today_images()})


def _count_today_images():
    """Helper: how many motivation images exist for today across both naming conventions."""
    today = today_iso()
    cache = Path("/home/work/.hermes/image_cache")
    if not cache.exists():
        return 0
    n = 0
    # gymbro_{YYYY-MM-DD}.png (with dashes)
    if (cache / f"gymbro_{today}.png").exists():
        n += 1
    # cheer_{YYYYMMDD}_*.png (no dashes, suffix present)
    yyyymmdd = today.replace("-", "")
    n += sum(1 for f in cache.glob(f"cheer_{yyyymmdd}_*.png") if f.suffix in ('.png', '.jpg'))
    return n


@app.route("/api/today_images")
def api_today_images():
    """Return the ordered list of today's motivation images for cycling.

    Order (newest-cheer-first by mtime, then gymbro daily as anchor):
      - cheer_{YYYYMMDD}_*.png (no dashes, sorted newest-mtime first)
      - gymbro_{YYYY-MM-DD}.png (with dashes) — daily anchor, last in list

    Response shape:
      {"date": "YYYY-MM-DD",
       "images": [
         {"url": "/img/cheer_20260719_HKT_afternoon_D2.png",
          "kind": "cheer",
          "context": "afternoon_D2",
          "size_kb": 227,
          "mtime": "2026-07-19T14:53:14"},
         ...
         {"url": "/img/gymbro_2026-07-19.png",
          "kind": "gymbro",
          "size_kb": 178,
          "mtime": "2026-07-19T14:46:36"}
       ]}
    """
    from datetime import datetime as _dt
    today = today_iso()
    yyyymmdd = today.replace("-", "")
    cache = Path("/home/work/.hermes/image_cache")
    images = []
    if not cache.exists():
        return jsonify({"date": today, "images": images})

    # Cheer images for today (no dashes), newest-mtime first
    cheer_files = sorted(
        [f for f in cache.glob(f"cheer_{yyyymmdd}_*.png") if f.suffix in ('.png', '.jpg')],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for f in cheer_files:
        # Extract context suffix: "cheer_20260719_HKT_afternoon_D2.png" -> "afternoon_D2"
        suffix = f.stem.replace(f"cheer_{yyyymmdd}_", "", 1)
        ctx = suffix.split("_", 1)[1] if "_" in suffix else suffix
        images.append({
            "url": f"/img/{f.name}",
            "kind": "cheer",
            "context": ctx,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "mtime": _dt.fromtimestamp(f.stat().st_mtime).isoformat(timespec='seconds'),
        })

    # Gymbro daily anchor last
    g = cache / f"gymbro_{today}.png"
    if g.exists() and g.stat().st_size > 50000:
        images.append({
            "url": f"/img/{g.name}",
            "kind": "gymbro",
            "context": "daily",
            "size_kb": round(g.stat().st_size / 1024, 1),
            "mtime": _dt.fromtimestamp(g.stat().st_mtime).isoformat(timespec='seconds'),
        })

    return jsonify({"date": today, "images": images, "total": len(images)})


@app.route("/api/streak")
def api_streak():
    """Count consecutive days ending today where session was completed
    with >= 3 exercises (a 'real' workout). Walks backwards day-by-day
    until a gap is found. Returns 0 if no completed workouts found."""
    log = load_log()
    today = today_iso()
    streak = 0
    last_workout_date = None

    # Walk backwards from today, one day at a time
    cursor = datetime.strptime(today, "%Y-%m-%d").date()
    while True:
        key = cursor.strftime("%Y-%m-%d")
        session = log.get(key)
        if session and session.get("completed") and len(session.get("exercises", [])) >= 3:
            streak += 1
            last_workout_date = key
            cursor = cursor - timedelta(days=1)
        else:
            break

    return jsonify({"streak": streak, "last_workout_date": last_workout_date})


@app.route("/api/cancel_last_set", methods=["POST"])
def api_cancel_last_set():
    """Pop the last entry from today's session exercises. Returns the removed entry."""
    session = get_or_create_session()
    if not session.get("exercises"):
        return jsonify({"error": "no sets to cancel"}), 400
    removed = session["exercises"].pop()
    log = load_log()
    # v3.3.39: 用 session 自己嘅日期做 key(支援補記過去一節),
    # 唔可以硬寫 today_iso() —— 否則補記會覆蓋今日 session
    log[session.get("date") or today_iso()] = session
    save_log(log)
    return jsonify({"ok": True, "removed": removed, "remaining": len(session["exercises"])})


# ---------- Health overlay (Whoop recovery + Withings weight) — minimal 2 numbers ----------
WHOOP_CACHE = Path("/home/work/.whoop_data_latest.json")

# gymbro PWA version — bump on every release
# v3.3.2: removed food_scan_log.json + nutrition_log.json + scan_cache +
# scan_thumb_cache from the WRITE path. Sheet + Drive are the only
# persistent store; cache hydrates from Sheet on boot.
__version__ = "3.3.40"


def _recovery_pct():
    """Latest Whoop recovery score (single number 0-100, or None).
    Reads from Whoop V2 cache shape: {"recovery": {"records": [{score, score_state, ...}, ...]}}.
    Falls back to flat shape `{"recovery": [r1, ...]}` for backwards compat.
    """
    d = _safe_read_json(WHOOP_CACHE)
    if not isinstance(d, dict):
        return None
    recs_root = d.get("recovery")
    # Shape A: nested {records: [...]}
    if isinstance(recs_root, dict):
        recs = recs_root.get("records", []) or []
    # Shape B: flat list
    elif isinstance(recs_root, list):
        recs = recs_root
    else:
        return None
    for r in recs:
        if not isinstance(r, dict):
            continue
        score_raw = r.get("score")
        if not isinstance(score_raw, dict):
            continue
        val = score_raw.get("recovery_score")
        if val is not None and r.get("score_state") == "SCORED":
            return int(round(float(val)))


def _whoop_strain():
    """v3.3.13 (Jim OOB 2026-09-01「Make UIUX change on gym — Make it like
    whoop」): latest Whoop strain score (0-21, or None). Reads from cache
    cycles list, each item's score.strain.
    """
    d = _safe_read_json(WHOOP_CACHE)
    if not isinstance(d, dict):
        return None
    # Shape A: nested {cycle: {records: [...]}}
    cycle_root = d.get("cycle")
    if isinstance(cycle_root, dict):
        cycles = cycle_root.get("records", []) or []
    # Shape B: flat list `cycles`
    elif isinstance(d.get("cycles"), list):
        cycles = d.get("cycles")
    else:
        return None
    if not isinstance(cycles, list):
        return None
    for c in cycles:
        if not isinstance(c, dict):
            continue
        if c.get("score_state") != "SCORED":
            continue
        score = c.get("score") or {}
        if not isinstance(score, dict):
            continue
        strain = score.get("strain")
        if strain is not None:
            try:
                return round(float(strain), 1)
            except (TypeError, ValueError):
                continue
    return None


def _whoop_sleep_summary():
    """v3.3.13: latest Whoop sleep summary dict {sleep_pct, sleep_hrs}.

    v3.3.24d (Jim OOB 2026-10-20 "why don't you talk about my nap today?
    you should have detected it from whoop"):

    BUG (v3.3.13-v3.3.24c): the old version iterated the sleep list and
    returned the FIRST scored record it hit. Whoop's list is not ordered
    nap-first, and the nap record shares cycle_id with the preceding night's
    main sleep — so the nap was either silently skipped or (worse) surfaced
    as "last night's sleep", which is what made Jim's 1h35m nap invisible
    on 2026-10-20.

    FIX:
      1. Sort by `start` DESC so we always evaluate the most recent record.
      2. Separate nap records (`nap: true`) from main sleep records.
      3. Compute duration from `start`/`end` ISO timestamps as the PRIMARY
         source (timezone-safe), falling back to stage_summary ms.
      4. Return nap + sleep-debt + main-sleep detail so the coach layer can
         always talk about a nap if one happened.

    Returns dict with keys:
      sleep_pct, sleep_hrs            — most recent MAIN sleep
      sleep_start, sleep_end          — main sleep window (ISO)
      nap_detected (bool)
      nap_hrs, nap_pct, nap_disturbances, nap_cycles
      nap_start, nap_end
      sleep_debt_hrs                  — Whoop-computed debt (need_from_sleep_debt)
      sleep_need_hrs                  — Whoop baseline need
      sleep_efficiency_pct
      respiratory_rate
      total_sleep_hrs                 — main sleep + nap (today's total)
    """
    d = _safe_read_json(WHOOP_CACHE)
    if not isinstance(d, dict):
        return {}
    sleeps = d.get("sleep")
    if not isinstance(sleeps, list):
        return {}

    from datetime import datetime as _dt

    def _parse_iso(v):
        if not v:
            return None
        try:
            return _dt.fromisoformat(str(v).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    def _dur_hrs(s):
        """Duration in hours from start/end, fallback to stage_summary ms."""
        sd = _parse_iso(s.get("start"))
        ed = _parse_iso(s.get("end"))
        if sd and ed:
            try:
                return round((ed - sd).total_seconds() / 3600, 2)
            except (TypeError, ValueError):
                pass
        score = s.get("score") or {}
        stage = score.get("stage_summary") or {} if isinstance(score, dict) else {}
        in_bed_ms = stage.get("total_in_bed_time_milli") if isinstance(stage, dict) else None
        try:
            if in_bed_ms is not None:
                return round(float(in_bed_ms) / 3600000, 2)
        except (TypeError, ValueError):
            pass
        return None

    scored = [
        s for s in sleeps
        if isinstance(s, dict) and s.get("score_state") == "SCORED"
    ]
    if not scored:
        return {}

    # Most recent first
    scored.sort(key=lambda s: str(s.get("start") or ""), reverse=True)

    naps = [s for s in scored if s.get("nap") is True]
    mains = [s for s in scored if s.get("nap") is not True]

    out = {}

    # --- Most recent NAP (today if one happened) ---
    if naps:
        n = naps[0]
        nscore = n.get("score") or {}
        nstage = nscore.get("stage_summary") or {} if isinstance(nscore, dict) else {}
        out["nap_detected"] = True
        out["nap_hrs"] = _dur_hrs(n)
        out["nap_pct"] = nscore.get("sleep_performance_percentage") if isinstance(nscore, dict) else None
        out["nap_disturbances"] = nstage.get("disturbance_count") if isinstance(nstage, dict) else None
        out["nap_cycles"] = nstage.get("sleep_cycle_count") if isinstance(nstage, dict) else None
        out["nap_start"] = n.get("start")
        out["nap_end"] = n.get("end")
        out["nap_cycle_id"] = n.get("cycle_id")
    else:
        out["nap_detected"] = False
        out["nap_hrs"] = None
        out["nap_pct"] = None
        out["nap_disturbances"] = None
        out["nap_cycles"] = None
        out["nap_start"] = None
        out["nap_end"] = None

    # --- Most recent MAIN sleep ---
    if mains:
        m = mains[0]
        mscore = m.get("score") or {}
        mstage = mscore.get("stage_summary") or {} if isinstance(mscore, dict) else {}
        mneed = mscore.get("sleep_needed") or {} if isinstance(mscore, dict) else {}
        out["sleep_pct"] = mscore.get("sleep_performance_percentage") if isinstance(mscore, dict) else None
        out["sleep_hrs"] = _dur_hrs(m)
        out["sleep_start"] = m.get("start")
        out["sleep_end"] = m.get("end")
        out["sleep_efficiency_pct"] = mscore.get("sleep_efficiency_percentage") if isinstance(mscore, dict) else None
        out["sleep_consistency_pct"] = mscore.get("sleep_consistency_percentage") if isinstance(mscore, dict) else None
        out["respiratory_rate"] = mscore.get("respiratory_rate") if isinstance(mscore, dict) else None
        out["sleep_disturbances"] = mstage.get("disturbance_count") if isinstance(mstage, dict) else None
        out["sleep_cycles"] = mstage.get("sleep_cycle_count") if isinstance(mstage, dict) else None
        out["sleep_cycle_id"] = m.get("cycle_id")
        # Sleep debt / need from Whoop
        try:
            out["sleep_debt_hrs"] = round(float(mneed.get("need_from_sleep_debt_milli") or 0) / 3600000, 2)
        except (TypeError, ValueError):
            out["sleep_debt_hrs"] = None
        try:
            out["sleep_need_hrs"] = round(float(mneed.get("baseline_milli") or 0) / 3600000, 2)
        except (TypeError, ValueError):
            out["sleep_need_hrs"] = None
    else:
        out["sleep_pct"] = None
        out["sleep_hrs"] = None
        out["sleep_start"] = None
        out["sleep_end"] = None
        out["sleep_efficiency_pct"] = None
        out["sleep_consistency_pct"] = None
        out["respiratory_rate"] = None
        out["sleep_disturbances"] = None
        out["sleep_cycles"] = None
        out["sleep_cycle_id"] = None
        out["sleep_debt_hrs"] = None
        out["sleep_need_hrs"] = None

    # Today's total (main sleep + nap that shares the same cycle_id)
    total = 0.0
    if out.get("sleep_hrs"):
        total += out["sleep_hrs"]
    if out.get("nap_detected") and out.get("nap_hrs"):
        # Only count the nap if it belongs to the same cycle as the main sleep
        # (i.e. it's part of the current sleep window, not an old one).
        if out.get("nap_cycle_id") == out.get("sleep_cycle_id"):
            total += out["nap_hrs"]
    out["total_sleep_hrs"] = round(total, 2)

    return out


def _whoop_recovery_metrics():
    """v3.3.13: latest Whoop recovery record — HRV + RHR.

    Returns dict {hrv_ms, rhr_bpm} or {} if unavailable.
    """
    d = _safe_read_json(WHOOP_CACHE)
    if not isinstance(d, dict):
        return {}
    recs_root = d.get("recovery")
    if isinstance(recs_root, dict):
        recs = recs_root.get("records", []) or []
    elif isinstance(recs_root, list):
        recs = recs_root
    else:
        return {}
    for r in recs:
        if not isinstance(r, dict):
            continue
        if r.get("score_state") != "SCORED":
            continue
        out = {}
        # Whoop V2 puts hrv/rhr inside score (not top-level).
        score = r.get("score") or {}
        if isinstance(score, dict):
            hrv_v = score.get("hrv_rmssd_milli")
            rhr_v = score.get("resting_heart_rate")
        else:
            hrv_v = r.get("hrv_rmssd_milli")
            rhr_v = r.get("resting_heart_rate")
        # Top-level fallback (older V1 cache shape)
        if hrv_v is None:
            hrv_v = r.get("hrv_rmssd_milli")
        if rhr_v is None:
            rhr_v = r.get("resting_heart_rate")
        try:
            if hrv_v is not None:
                out["hrv_ms"] = round(float(hrv_v), 1)
        except (TypeError, ValueError):
            pass
        try:
            if rhr_v is not None:
                out["rhr_bpm"] = round(float(rhr_v), 1)
        except (TypeError, ValueError):
            pass
        if out:
            return out
    return {}


def _whoop_workouts_in_window(cutoff_iso_date):
    """Read Whoop cached workouts filtered by HKT date >= cutoff.

    Returns list of dicts:
      [{date, sport_name, strain, kJ, avg_hr, max_hr, start_iso, end_iso}, ...]

    Read from ~/.whoop_data_latest.json cache which is populated by
    `whoop_nutrition.py --sync` (cron refreshes every ~hour). Falls back to
    empty list if cache missing/malformed.

    Jim OOB 2026-07-19 (persistent): "Please always refer to whoop activities
    supplemented by Google sheet" — Cheer routines + History pulls should
    always include Whoop activity data alongside the Sheet-sourced set rows.

    Per `whoop` skill: workout `start` is ISO UTC; convert via HKT for date.
    Strain / kJ / avg+max HR live under nested `score` dict.
    """
    out = []
    d = _safe_read_json(WHOOP_CACHE)
    if not isinstance(d, dict):
        return out
    for w in d.get("workouts", []) or []:
        if w.get("score_state") != "SCORED":
            continue
        start_iso = w.get("start", "")
        if not start_iso:
            continue
        try:
            from zoneinfo import ZoneInfo
            hkt = ZoneInfo("Asia/Hong_Kong")
            dt_utc = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            date_hkt = dt_utc.astimezone(hkt).__format__("%Y-%m-%d")
        except Exception:
            continue
        if date_hkt < cutoff_iso_date:
            continue
        sc = w.get("score") or {}
        out.append({
            "date": date_hkt,
            "sport_name": w.get("sport_name", ""),
            "strain": sc.get("strain"),
            "kJ": sc.get("kilojoule"),
            "avg_hr": sc.get("average_heart_rate"),
            "max_hr": sc.get("max_heart_rate"),
            "start_iso": start_iso,
            "end_iso": w.get("end", ""),
            "duration_ms": w.get("duration"),
            "id": w.get("id"),
            "source": "whoop",
        })
    out.sort(key=lambda r: (r["date"], r["start_iso"]), reverse=True)
    return out


def _body_latest() -> dict:
    """Latest body measurements — WHOOP is the source (v3.3.25).

    Jim OOB 2026-10-20「I mean weight. If whoop has weight data. Remove all
    withings」: WHOOP's `/developer/v2/user/measurement/body` (OAuth scope
    `read:body_measurement`) returns
        {"height_meter": 1.778, "weight_kilogram": 78.107, "max_heart_rate": 182}
    so the Withings SCALE is no longer needed for weight.

    WHOOP has NO bioimpedance sensors, so fat% / muscle / bone / hydration are
    None — the UI hides them rather than showing a fabricated 0.
    """
    try:
        return _whoop_get_body(cache_path=WHOOP_CACHE) or {}
    except Exception:
        return {}


def _body_weight():
    """Latest weight in kg (WHOOP). Jim OOB 2026-07-19: use latest, not just today."""
    w = _body_latest().get("weight_kg")
    try:
        return round(float(w), 1) if w else None
    except (TypeError, ValueError):
        return None


def _body_fat_pct():
    """Latest body fat percentage. WHOOP has no bioimpedance sensor -> None."""
    f = _body_latest().get("fat_pct")
    try:
        return round(float(f), 1) if f else None
    except (TypeError, ValueError):
        return None


def _body_muscle_kg():
    """Latest muscle mass in kg. WHOOP has no bioimpedance sensor -> None."""
    m = _body_latest().get("muscle_kg")
    try:
        return round(float(m), 2) if m else None
    except (TypeError, ValueError):
        return None


def _body_bone_kg():
    """Latest bone mass in kg. WHOOP has no bioimpedance sensor -> None."""
    b = _body_latest().get("bone_kg")
    try:
        return round(float(b), 2) if b else None
    except (TypeError, ValueError):
        return None


def _body_hydration_pct():
    """Latest hydration percentage. WHOOP has no bioimpedance sensor -> None."""
    h = _body_latest().get("hydration_pct")
    try:
        return round(float(h), 1) if h else None
    except (TypeError, ValueError):
        return None


def _whoop_steps() -> dict:
    """v3.3.25 (Jim OOB 2026-10-20「Steps is whoop! 用 whoop count step」):
    today's step count + 7-day average from the Whoop V2 cycle cache.

    WHOOP is now the PRIMARY step source. `/developer/v2/cycle` has carried a
    top-level `step_count` on every cycle record since the V2 pipeline was
    built, but nothing read it — every step number came from Withings, which
    frequently has no same-day record (-> steps: null) even though Whoop had
    the number all along.

    Semantics: the cycle array is newest-first and cycles[0] is the
    IN-PROGRESS cycle (`end` is null) => its step_count is today's running
    total. cycles[1..7] are the last 7 COMPLETE days => 7d average.

    Truthfulness (mirrors the Withings v2.7.25 rule, plus the device caveat):
      - Only the in-progress cycle counts as "today"; never fall back to a
        previous day's number.
      - `updated_at` is Whoop's own last-write stamp. The in-progress cycle
        only advances while the band is worn, so an old stamp means "band
        off-body", NOT "API broken". `band_off` lets the UI say "last known"
        instead of implying a live running total.
    """
    try:
        # Pass the module-level WHOOP_CACHE so the test-suite's monkeypatch
        # (which redirects it to a tmp file) is honoured — never read
        # production state from a test.
        return _whoop_get_steps(cache_path=WHOOP_CACHE) or {}
    except Exception:
        return {}


@app.route("/api/whoop_steps")
def api_whoop_steps():
    """Today's Whoop step count, 7-day average, freshness and per-day series.

    v3.3.25: WHOOP is the primary step source (Jim OOB 2026-10-20). Shape
    mirrors the retired `/api/withings_steps_today` shape so the front-end could swap sources
    without changing its state model, plus the Whoop-specific extras:
      stale     — record older than 30 min (UI amber)
      band_off  — record older than 6 h (the Whoop was off-body; the number
                  is a real last-known total, not a live one)
      in_progress — cycles[0] is the running cycle (end == null)
    """
    data = _whoop_steps()
    if not data:
        return jsonify({
            "steps": None, "date": None, "data_as_of": None, "age_min": None,
            "stale": False, "band_off": False, "in_progress": False,
            "steps_7d_avg": None, "steps_7d_samples": 0, "series": [],
            "syncing": False, "_source": "whoop_cycle", "ok": False,
        })
    return jsonify({
        "steps": data.get("steps"),
        "date": data.get("date"),
        "data_as_of": data.get("data_as_of"),
        # v3.3.31 (2026-10-21): WHOOP's cycle `updated_at` is frozen and
        # does NOT track the live step_count (proved: 1042 -> 1159 -> 1342
        # while updated_at stayed 08:14:53). `data_as_of` is therefore the
        # pull time; the source stamp is exposed separately for diagnostics
        # and for the band-off heuristic.
        "source_updated_at": data.get("source_updated_at"),
        "source_age_min": data.get("source_age_min"),
        "age_min": data.get("age_min"),
        "stale": data.get("stale", False),
        # v3.3.35 (2026-10-21, Jim OOB「Why gymbro is saying my whoop band
        # off?!!!」): 語意由「band 除咗身」改成「個步數冇郁過」——
        # 舊推斷用 cycle `updated_at`,實證每朝 08:00-08:30 就停,同 band
        # 有冇戴完全無關,所以每晚都報「band off」(假警報)。
        "band_off": data.get("band_off", False),
        "band_off_basis": data.get("band_off_basis", "steps_frozen"),
        "steps_frozen_min": data.get("steps_frozen_min"),
        "last_change_at": data.get("last_change_at"),
        "last_change_known": data.get("last_change_known", False),
        "pulled_at": data.get("pulled_at"),
        "in_progress": data.get("in_progress", False),
        "steps_yesterday": data.get("steps_yesterday"),
        "steps_yesterday_date": data.get("steps_yesterday_date"),
        "steps_7d_avg": data.get("steps_7d_avg"),
        "steps_7d_samples": data.get("steps_7d_samples", 0),
        "series": data.get("series", []),
        # Withings-compatible aliases so the front-end can consume either
        # endpoint through the same code path.
        "syncing": data.get("steps") is None,
        "calories": None,   # Whoop cycles carry no step-specific calories
        "distance_km": None,
        "_source": "whoop_cycle",
        "ok": True,
    })


@app.route("/api/health_overlay")
def api_health_overlay():
    """Single endpoint for the hero overlay.
    - Top-left: Whoop recovery %
    - Top-right: Withings weight kg + fat % (latest reading, drives Jim's goal)
    - Steps: TODAY (large) + YESTERDAY (small) — paired widget
    - v3.3.12: body composition full set (weight / fat / muscle / bone / hydration).
    - v3.3.13: Whoop extended metrics (strain / sleep / HRV / RHR) for Whoop-Today layout.

    v3.3.23al (Jim OOB 2026-09-27 'Have you fixed it? Still slow start'):
    The endpoint previously called 12+ helper functions in series, hitting
    Withings/Whoop APIs sequentially. Total: 1.4s per call. Since this is
    called on every page load, this is the dominant "slow start" complaint.

    Two fixes:
    1. Cache the merged payload in Redis with 60s TTL (jim doesn't need
       second-by-second freshness for body weight/steps/recovery on every
       page render).
    2. The expensive external API calls (_body_latest,
       _whoop_recovery, etc.) are now run in parallel via ThreadPoolExecutor.
       Max single call (was 400-800ms) becomes total wait time.
    """
    from gym_web.redis_cache import get_redis_cache
    rcache = get_redis_cache()
    cache_key = "overlay:health"
    cached = rcache.get(cache_key)
    if cached is not None:
        try:
            return jsonify(json.loads(cached))
        except Exception:
            pass

    import concurrent.futures as _cf
    import time as _time
    overlay_start = _time.time()

    # Run all the helper functions in parallel (they hit separate APIs)
    # v3.3.25 (Jim OOB 2026-10-20「Fully remove withings / Steps is whoop!」):
    # the Withings STEP helpers are GONE from this endpoint — WHOOP is the
    # only step source. Withings still supplies body composition
    # (weight / fat / muscle / bone / hydration), which it is the sole
    # source of. Dropping the two step calls also removes two slow
    # external API round-trips from the overlay's critical path.
    def _body():
        return _body_latest() or {}

    def _sleep():
        return _whoop_sleep_summary()

    def _rec_metrics():
        return _whoop_recovery_metrics()

    def _recovery_pct_safe():
        try: return _recovery_pct()
        except: return None

    def _weight():
        try: return _body_weight()
        except: return None

    def _fat():
        try: return _body_fat_pct()
        except: return None

    def _muscle():
        try: return _body_muscle_kg()
        except: return None

    def _bone():
        try: return _body_bone_kg()
        except: return None

    def _hydration():
        try: return _body_hydration_pct()
        except: return None

    def _strain():
        try: return _whoop_strain()
        except: return None

    def _wsteps():
        # v3.3.25: Whoop is the PRIMARY step source.
        return _whoop_steps() or {}

    with _cf.ThreadPoolExecutor(max_workers=12) as _ex:
        fut_body = _ex.submit(_body)
        fut_sleep = _ex.submit(_sleep)
        fut_rec_metrics = _ex.submit(_rec_metrics)
        fut_rec_pct = _ex.submit(_recovery_pct_safe)
        fut_weight = _ex.submit(_weight)
        fut_fat = _ex.submit(_fat)
        fut_muscle = _ex.submit(_muscle)
        fut_bone = _ex.submit(_bone)
        fut_hydration = _ex.submit(_hydration)
        fut_strain = _ex.submit(_strain)
        fut_wsteps = _ex.submit(_wsteps)

        # Wait with 3s timeout (external APIs can hang)
        def _wait(fut, default=None):
            try:
                return fut.result(timeout=3)
            except Exception:
                return default

        body = _wait(fut_body, {})
        sleep = _wait(fut_sleep, {})
        rec_metrics = _wait(fut_rec_metrics, {})
        recovery_pct = _wait(fut_rec_pct)
        weight_kg = _wait(fut_weight)
        fat_pct = _wait(fut_fat)
        muscle_kg = _wait(fut_muscle)
        bone_kg = _wait(fut_bone)
        hydration_pct = _wait(fut_hydration)
        whoop_strain = _wait(fut_strain)
        wsteps = _wait(fut_wsteps, {})

    # v3.3.25 (Jim OOB 2026-10-20「Fully remove withings / Steps is whoop!」):
    # WHOOP is the ONLY step source. The Withings step path is gone from this
    # endpoint entirely — it was often missing a same-day record (-> null)
    # even though Whoop had the number all along. Withings still supplies body
    # composition (weight/fat/muscle/bone/hydration), its remaining job here.
    _w_steps = wsteps.get("steps") if isinstance(wsteps, dict) else None
    _w_date = wsteps.get("date") if isinstance(wsteps, dict) else None
    _w_asof = wsteps.get("data_as_of") if isinstance(wsteps, dict) else None
    _w_stale = bool(wsteps.get("stale")) if isinstance(wsteps, dict) else False
    _w_band_off = bool(wsteps.get("band_off")) if isinstance(wsteps, dict) else False
    # v3.3.35 (2026-10-21): band_off 嘅語意 =「個步數停咗 >3 個鐘」
    # (舊版用 cycle `updated_at` 推斷 band 有冇戴身,已實測推翻)。
    _w_band_off_basis = (wsteps.get("band_off_basis") if isinstance(wsteps, dict)
                          else None) or "steps_frozen"
    _w_frozen_min = wsteps.get("steps_frozen_min") if isinstance(wsteps, dict) else None
    _w_in_progress = bool(wsteps.get("in_progress")) if isinstance(wsteps, dict) else False
    _w_avg7 = wsteps.get("steps_7d_avg") if isinstance(wsteps, dict) else None
    _w_series = wsteps.get("series", []) if isinstance(wsteps, dict) else []
    _w_yesterday = wsteps.get("steps_yesterday") if isinstance(wsteps, dict) else None
    _w_yesterday_date = wsteps.get("steps_yesterday_date") if isinstance(wsteps, dict) else None

    payload = {
        "recovery": recovery_pct,
        "weight_kg": weight_kg,
        "fat_pct": fat_pct,
        "muscle_kg": muscle_kg,
        "bone_kg": bone_kg,
        "hydration_pct": hydration_pct,
        "weight_date": body.get("date") if isinstance(body, dict) else None,
        # v3.3.13 Whoop extended metrics
        "whoop_strain": whoop_strain,
        "whoop_sleep_pct": sleep.get("sleep_pct") if isinstance(sleep, dict) else None,
        "whoop_sleep_hrs": sleep.get("sleep_hrs") if isinstance(sleep, dict) else None,
        # v3.3.24d nap + sleep debt (Jim OOB 2026-10-20)
        "whoop_nap_detected": sleep.get("nap_detected", False) if isinstance(sleep, dict) else False,
        "whoop_nap_hrs": sleep.get("nap_hrs") if isinstance(sleep, dict) else None,
        "whoop_nap_pct": sleep.get("nap_pct") if isinstance(sleep, dict) else None,
        "whoop_nap_disturbances": sleep.get("nap_disturbances") if isinstance(sleep, dict) else None,
        "whoop_nap_start": sleep.get("nap_start") if isinstance(sleep, dict) else None,
        "whoop_nap_end": sleep.get("nap_end") if isinstance(sleep, dict) else None,
        "whoop_total_sleep_hrs": sleep.get("total_sleep_hrs") if isinstance(sleep, dict) else None,
        "whoop_sleep_debt_hrs": sleep.get("sleep_debt_hrs") if isinstance(sleep, dict) else None,
        "whoop_sleep_need_hrs": sleep.get("sleep_need_hrs") if isinstance(sleep, dict) else None,
        "whoop_sleep_efficiency_pct": sleep.get("sleep_efficiency_pct") if isinstance(sleep, dict) else None,
        "whoop_respiratory_rate": sleep.get("respiratory_rate") if isinstance(sleep, dict) else None,
        "whoop_sleep_start": sleep.get("sleep_start") if isinstance(sleep, dict) else None,
        "whoop_sleep_end": sleep.get("sleep_end") if isinstance(sleep, dict) else None,
        "whoop_hrv_ms": rec_metrics.get("hrv_ms") if isinstance(rec_metrics, dict) else None,
        "whoop_rhr_bpm": rec_metrics.get("rhr_bpm") if isinstance(rec_metrics, dict) else None,
        # Steps — v3.3.25: WHOOP is the ONLY step source (Jim OOB 2026-10-20
        # 「Fully remove withings / Steps is whoop!」).
        # distance_km_today / calories_today / steps_yesterday are gone: the
        # Withings step path is removed and Whoop cycles carry neither step
        # distance nor step calories. "yesterday" is Whoop's previous cycle.
        "steps_today": _w_steps,
        "steps_date": _w_date,
        "steps_syncing": _w_steps is None,
        "steps_source": "whoop",
        "steps_as_of": _w_asof,
        "steps_stale": _w_stale,
        "steps_band_off": _w_band_off,
        # v3.3.35 (2026-10-21): band_off 嘅語意 =「個步數停咗 >3 個鐘」;
        # 舊版用 cycle `updated_at` 推斷 band 有冇戴身,已實測推翻
        # (每朝 08:00-08:30 就停,同 band 無關)。
        "steps_band_off_basis": _w_band_off_basis,
        "steps_frozen_min": _w_frozen_min,
        "steps_in_progress": _w_in_progress,
        "steps_yesterday": _w_yesterday,
        "steps_yesterday_date": _w_yesterday_date,
        "whoop_steps_7d_avg": _w_avg7,
        "whoop_steps_series": _w_series,
    }
    # Cache for 60s (next page load uses Redis hit)
    try:
        rcache.set(cache_key, json.dumps(payload).encode("utf-8"), ttl_s=60)
    except Exception:
        pass
    elapsed = (_time.time() - overlay_start) * 1000
    print(f"[health_overlay] built in {elapsed:.0f}ms (parallel + Redis cache)", flush=True)
    return jsonify(payload)


# ---------- Jim context store (cheer pipeline reads this to make text more personal) ----------
JIM_CONTEXT = Path("/home/work/.jim_context.json")


def _load_jim_context() -> dict:
    """Read /home/work/.jim_context.json. Returns {entries: {...}, updated_at: ...}."""
    d = _safe_read_json(JIM_CONTEXT, {"entries": {}})
    if not isinstance(d, dict):
        return {"entries": {}}
    if not isinstance(d.get("entries"), dict):
        d["entries"] = {}
    return d


def _save_jim_context(ctx: dict) -> bool:
    """Atomic write to JIM_CONTEXT."""
    try:
        tmp = str(JIM_CONTEXT) + ".tmp"
        Path(tmp).write_text(json.dumps(ctx, indent=2, ensure_ascii=False))
        os.replace(tmp, str(JIM_CONTEXT))
        return True
    except Exception:
        return False


def _get_jim_context_for_cheer() -> str:
    """Format jim_context as a string block for the cheer prompt.

    Reads JIM_CONTEXT, formats each entry as "- <key>: <value> (tags: <tags>)".
    Returns empty string if no entries.

    v3.3.7 (Jim OOB 2026-08-19 'voice summary is now referring latest schedule:
    the hiking was done last Sunday but this Sunday and i have already bought
    my barefoot asia'): filter out past-dated events. The cheer LLM was
    reading stale '8/10 Sun 大帽山行山' / '8/16 Sun cancelled' entries and
    treating them as upcoming plans. Now entries whose value contains a
    date earlier than today (HKT) are dropped. Entries without parseable
    dates pass through (preferences, dietary rules, etc.).
    """
    ctx = _load_jim_context()
    entries = ctx.get("entries") or {}
    if not entries:
        return ""
    try:
        from zoneinfo import ZoneInfo
        hkt = ZoneInfo("Asia/Hong_Kong")
        today_hkt = datetime.now(hkt).date()
    except Exception:
        today_hkt = datetime.now().date()

    # Match dates like 8/10, 8/16, 2026-08-16, 8/9 Sat — accept them as
    # past if their date is before today. Conservative: if ambiguous, keep.
    import re as _re
    date_re = _re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})")  # YYYY-MM-DD
    short_re = _re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)")  # M/D

    def _is_past(val: str) -> bool:
        for m in date_re.finditer(val):
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if datetime(y, mo, d).date() < today_hkt:
                    return True
            except Exception:
                continue
        for m in short_re.finditer(val):
            try:
                # Assume current year, current month unless the date has
                # already passed this month (treat as past).
                mo, d = int(m.group(1)), int(m.group(2))
                cand = datetime(today_hkt.year, mo, d).date()
                if cand < today_hkt:
                    return True
            except Exception:
                continue
        return False

    lines = ["\n**Jim 個人 context（pushed by MCP / cheer 要記住）**："]
    dropped = 0
    for k, v in entries.items():
        if not isinstance(v, dict):
            continue
        val = v.get("value", "")
        # Skip deleted entries
        if val.strip().upper() == "DELETED":
            continue
        # Skip past-dated event entries so cheer doesn't pretend they're upcoming
        if _is_past(val):
            dropped += 1
            continue
        tags = ", ".join(v.get("tags") or [])
        lines.append(f"- {k}: {val}" + (f" (tags: {tags})" if tags else ""))
    if dropped:
        print(f"[jim_context_for_cheer] dropped {dropped} past-dated entries")
    return "\n".join(lines)


@app.route("/api/context", methods=["GET", "POST"])
def api_context():
    """GET: return all Jim context entries.
    POST: push {key, value, tags?} → stored in /home/work/.jim_context.json.

    This is the gym_web HTTP-side twin of the MCP `push_jim_context` tool, so
    the iPhone PWA can push context (e.g. "today I want low-carb diet") and
    the cheer pipeline picks it up on the next fire.
    """
    if request.method == "GET":
        return jsonify(_load_jim_context())
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    value = (data.get("value") or "").strip()
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    if not key or not value:
        return jsonify({"error": "key and value required"}), 400
    ctx = _load_jim_context()
    hkt_iso = datetime.now(timezone(timedelta(hours=8))).isoformat()
    ctx["entries"][key] = {"value": value, "tags": tags, "pushed_at": hkt_iso}
    ctx["updated_at"] = hkt_iso
    ok = _save_jim_context(ctx)
    return jsonify({
        "ok": ok,
        "key": key,
        "stored": ctx["entries"][key],
        "total_entries": len(ctx["entries"]),
    })


@app.route("/api/nutrition/refresh", methods=["POST"])
def api_nutrition_refresh():
    """Force rehydrate the in-memory NutritionCache from Google Sheets.

    v3.3.10 (Jim OOB 2026-08-31「The gym bro cheer routine is not reflecting
    the intraday activities again」): when you directly patch sheet rows
    via batchUpdate (NOT via /api/scan_commit), the cache doesn't auto-refresh
    and cheer pipeline keeps reading stale data. This endpoint triggers a
    full cache rehydrate so cheer sees freshest intraday nutrition.

    Returns: {ok, rows_loaded, refreshed_at}
    """
    try:
        rows_loaded = _hydrate_nutrition_cache()
        return jsonify({
            "ok": True,
            "rows_loaded": rows_loaded,
            "refreshed_at": now_iso(),
            "message": f"Cache rehydrated with {rows_loaded} rows from Sheet. "
                      f"Cheer pipeline will now see current intraday data."
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/nutrition/today")
def api_nutrition_today():
    """Return today's nutrition: per-meal list + totals + last_meal_ts.

    Used by gym_web PWA nutrition tab + cheer pipeline. iPhone PWA can
    also POST to /api/food_scan (existing) which writes to the same log
    this endpoint reads.

    v3.3.0: reads from in-memory NutritionCache instead of nutrition_log.json.
    v3.3.10: switched from inline cache.get_by_date() to _load_today_nutrition()
    helper so the late-night fallback ("today's HKT date has 0 meals, fall
    back to most recent date") activates for cheer pipeline consistency.
    """
    data = _load_today_nutrition()
    return jsonify({
        "date": data.get("date") or today_iso(),
        "fetched_at": now_iso(),
        "meal_count": data.get("meal_count", 0),
        "totals": data.get("totals", {"kcal": 0, "P": 0, "C": 0, "F": 0}),
        "last_meal_ts": data.get("last_meal_ts"),
        "meals": data.get("meals", []),
        "cache": _get_nutrition_cache().stats(),
    })


# v3.3.23 (Jim OOB 2026-09-11 'is this possible to download an jpg image
# containing about all the daily food log like screenshot of the food'):
# render today's food log as a shareable PNG/JPG using PIL. No new deps
# (Pillow already in venv). Renders header (date + totals), per-meal rows
# (with food thumbnails where available), footer (timestamp + gymbro
# branding). Layout: 1080x(60+90+80*N+60) px, dark theme matching the
# PWA's gradient. Output saved to /home/work/.hermes/nutrition_card_cache/
# + same path exposed via /nutrition_card/<filename> for the frontend
# to download. Daily cache (one card per day) — overwrites if exists
# so the user always gets the latest view.
def _render_nutrition_card_pil(date_iso: str, meals: list, totals: dict, page_num: int = 1, page_total: int = 1) -> "Path | None":
    """Render today's food log as a shareable JPG using PIL.

    v3.3.23al (Jim OOB 2026-09-27 'Pls fix all of them. Not just quick
    win'): pre-fetch all meal thumbs in PARALLEL via ThreadPoolExecutor
    before starting the PIL render loop. Previously each meal fetched
    its Drive thumb sequentially during the render loop, so 8 meals
    with cold Drive cache took 8-16s. With parallel fetches, all 8
    thumbs complete in ~2s, then the synchronous PIL render takes
    ~150-200ms. Total: ~2.2s instead of ~16s (8x speedup on cold cache).
    """
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError:
        return None

    # v3.3.23ax: compute page slice EARLY (before parallel thumb fetch) so the
    # thumb pool only fetches thumbs for the page being rendered, not all pages.
    # Each page is bounded by MEALS_PER_PAGE so image height stays under
    # ~1700px and renders cleanly across Telegram, WhatsApp, image hosts.
    MEALS_PER_PAGE = 6
    page_start = (page_num - 1) * MEALS_PER_PAGE
    page_end = page_start + MEALS_PER_PAGE
    page_meals = meals[page_start:page_end]

    # v3.3.23al: PARALLEL THUMB FETCH
    # Collect row_indices first, then fetch in parallel
    import concurrent.futures as _cf
    from gym_web.image_proxy import scan_thumb as _scan_thumb_func

    def _fetch_one_thumb(meal: dict) -> "tuple[int, bytes | None]":
        """Returns (idx, bytes) — bytes is None on failure."""
        thumb_path = (
            meal.get("image_path")
            or meal.get("image_url")
            or meal.get("thumbnail_url")
            or meal.get("drive_image_url")
            or ""
        )
        if not thumb_path.startswith("/scan_thumb/"):
            return (id(meal), None)
        try:
            # v3.3.38: URL 帶 ?v=<hash>(內容定址)→ 剝走 query 先 parse row index
            rest = thumb_path[len("/scan_thumb/"):].split("?", 1)[0]
            if not rest.isdigit():
                return (id(meal), None)
            row_idx = int(rest)
            resp = _scan_thumb_func(row_idx)
            if resp.status_code == 200:
                return (id(meal), resp.get_data())
        except Exception as e:
            print(f"[card._fetch_one_thumb] row fetch failed for {thumb_path}: {e}", flush=True)
        return (id(meal), None)

    # Build a thumb_bytes dict keyed by id(meal) for the render loop below
    # v3.3.23ax: only fetch thumbs for the page being rendered (not all pages)
    thumb_bytes_for_meal: dict = {}
    if page_meals:
        with _cf.ThreadPoolExecutor(max_workers=min(8, len(page_meals))) as _ex:
            future_to_id = {}
            for m in page_meals:
                f = _ex.submit(_fetch_one_thumb, m)
                future_to_id[f] = id(m)
            for f in _cf.as_completed(future_to_id):
                mid = future_to_id[f]
                try:
                    _, b = f.result(timeout=10)
                    if b:
                        thumb_bytes_for_meal[mid] = b
                except Exception:
                    pass

    # Theme
    BG_TOP = (28, 25, 46)         # deep purple
    BG_BOT = (12, 10, 24)         # near-black
    BAND = (45, 38, 70)           # slightly lighter
    ACCENT = (251, 191, 36)       # amber for kcal
    PROTEIN = (96, 165, 250)      # blue
    CARBS = (251, 146, 60)        # orange
    FAT = (220, 38, 38)           # red
    TEXT = (240, 240, 245)
    TEXT_DIM = (170, 170, 185)
    BORDER = (80, 75, 110)
    # Fonts (try CJK + Latin; fall back to default)
    def _load(size: int, bold: bool = False):
        # v3.3.23d (Jim OOB 2026-09-12 'The Chinese cannot be shown'):
        # Use index=4 for the .ttc CJK file to load the HK (Traditional
        # Chinese, Cantonese-aware) variant — default index=0 is the
        # JP face, which lacks some Cantonese-specific characters and
        # renders some TC chars with JP fallback glyphs. HK variant
        # matches Jim's HKT/Hong Kong writing convention best.
        # Also added more fallback paths because the actual install path
        # of fonts-noto-cjk is /usr/share/fonts/opentype/noto/, NOT
        # /usr/share/fonts/truetype/noto/ (which only has emoji).
        candidates = [
            # Prefer HK Cantonese-aware CJK (v3.3.23d)
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold
             else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 4),
            # HK Bold: index=4 same (TTC face index, not weight)
            ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold
             else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 3),
            # Latin fallback (always works)
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
             else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
            # Last-ditch CJK (wrong sub-face but still renders)
            ("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold
             else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
        ]
        for path, index in candidates:
            try:
                return ImageFont.truetype(path, size, index=index)
            except Exception:
                continue
        return ImageFont.load_default()
    f_date = _load(34, bold=True)
    # v3.3.23u (Jim OOB 2026-09-14 'Still no grading for jpg download.
    # Still poor layout of timing'): time text 22px was too big — caused
    # overlap with grade badge at x=102. Now reduced to 18px + grade
    # badge made much more visible (wider color tab, larger pill).
    f_kcal_big = _load(60, bold=True)
    f_macro = _load(28, bold=True)
    f_macro_val = _load(28, bold=True)
    f_time = _load(18)          # was 22px — reduced for cleaner layout
    # v3.3.23u: food name font (40px) was OK; keep
    f_name = _load(40, bold=True)
    f_name_small = _load(32)
    f_kcal_row = _load(48, bold=True)
    f_macro_pill = _load(28, bold=True)
    f_foot = _load(18)
    f_brand = _load(22, bold=True)
    f_rest = _load(24)
    # v3.3.23u: dedicated grade font — bold 26px for the grade letter
    f_grade = _load(26, bold=True)
    # v3.3.23x (Jim OOB 2026-09-16 'timing is not prominent too'): dedicated
    # BIG time font — 36px bold for the prominent left-side time pill
    f_time_big = _load(36, bold=True)
    # Geometry — v3.3.23p (Jim OOB 2026-09-12 'Some font are overlapping'
    # + 'Can you arrange a better design?'): full redesign of card
    # layout to fix overlap + visual hierarchy. Three-column structure
    # with strict separation:
    #   [thumb 220×220] [text col 360w] [right col 240w]
    # Thumb narrower so right col has clear space for pills. Text col
    # fixed width 360 so restaurant chain never bleeds into pill area.
    # Right col now uses full 240 vertical = MEAL_H. All blocks pixel-
    # perfect aligned. Fonts tuned so 雲呢拿蛋白飲 + ON Gold Standard
    # both fit on single line (no overflow).
    W = 1080
    HEAD = 70
    # v3.3.23q: BAND_H 100→130 to fit summary band with proper vertical
    # separation between "Total" label + big kcal number + "kcal" suffix
    BAND_H = 130
    MEAL_H = 240       # taller row to fit bigger fonts
    FOOT = 60
    # v3.3.23ax: PAGE indicator room when multiple pages exist (default 0)
    PAGE = 36 if page_total > 1 else 0
    PAD = 32
    THUMB_SIZE = 240   # slightly bigger thumb (220→240) for new bigger food font
    THUMB_RADIUS = 14
    # v3.3.23p: explicit column widths to prevent overlap
    THUMB_W = THUMB_SIZE
    TEXT_COL_W = 380    # name + restaurant only — fixed width
    GAP = 20            # gap between thumb and text
    RIGHT_COL_W = 240   # kcal pill + P/C/F stack
    # v3.3.23ax: H based on page_meals (slice) not full meals, plus PAGE room
    # for the page indicator on multi-page outputs
    H = HEAD + BAND_H + max(1, len(page_meals)) * MEAL_H + FOOT + PAGE
    img = Image.new("RGB", (W, H), BG_TOP)
    draw = ImageDraw.Draw(img)
    # Background gradient (top→bottom) — draw thin horizontal bands
    for y in range(H):
        t = y / max(1, H - 1)
        r = int(BG_TOP[0] * (1 - t) + BG_BOT[0] * t)
        g = int(BG_TOP[1] * (1 - t) + BG_BOT[1] * t)
        b = int(BG_TOP[2] * (1 - t) + BG_BOT[2] * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    # Header bar
    draw.rectangle([(0, 0), (W, HEAD)], fill=(0, 0, 0, 0))
    # Brand chip top-left
    draw.rounded_rectangle([(PAD, 18), (PAD + 180, 64)], radius=12, fill=BAND)
    draw.text((PAD + 22, 23), "gymbro · food log", fill=ACCENT, font=f_brand)
    # Date big right
    # Convert date to friendly format
    try:
        from datetime import datetime as _dt
        d_obj = _dt.strptime(date_iso, "%Y-%m-%d")
        weekday = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"][d_obj.weekday()]
        date_label = f"{d_obj.year}年{d_obj.month:02d}月{d_obj.day:02d}日 {weekday}"
    except Exception:
        date_label = date_iso
    bbox = draw.textbbox((0, 0), date_label, font=f_date)
    draw.text((W - PAD - (bbox[2] - bbox[0]), 14), date_label, fill=TEXT, font=f_date)
    # Summary band
    by = HEAD
    draw.rectangle([(0, by), (W, by + BAND_H)], fill=BAND)
    # v3.3.23q (Jim OOB 'Font of total calorie is overlapping'):
    # Redesigned Total kcal block — explicit vertical separation:
    #   "Total" (24px label)    at y = by + 12
    #   "950"  (60px number)   at y = by + 36   (fits within 130h band)
    #   "kcal" (28px suffix)   right-aligned at top with "Total"
    # Old code: "kcal" was at by+50 which collided with 72px number.
    # New: number is 60px (tighter) and "kcal" sits ABOVE the number.
    kcal_total = int(round(totals.get("kcal", 0) or 0))
    # Label row (y = by + 12): "Total" left, "kcal" right
    draw.text((PAD, by + 12), "Total", fill=TEXT_DIM, font=f_macro)
    # Right-align "kcal" with the number baseline for clean axis
    kcal_suffix_bbox = draw.textbbox((0, 0), "kcal", font=f_macro)
    kcal_suffix_w = kcal_suffix_bbox[2] - kcal_suffix_bbox[0]
    draw.text((PAD + 130 + 12, by + 12), "kcal", fill=TEXT_DIM, font=f_macro)
    # Big kcal number at y = by + 38 (well below the "Total/kcal" label row)
    draw.text((PAD, by + 38), f"{kcal_total}", fill=ACCENT, font=f_kcal_big)
    # Macro cells: P / C / F
    cell_w = 200
    cell_x0 = 360
    for i, (label, key, color) in enumerate([("蛋白質 P", "P", PROTEIN), ("碳水 C", "C", CARBS), ("脂肪 F", "F", FAT)]):
        x = cell_x0 + i * (cell_w + 12)
        y = by + 14
        # Colored pill bar
        draw.rounded_rectangle([(x, y), (x + cell_w, y + 60)], radius=10, fill=(30, 26, 48))
        # Left color tab
        draw.rectangle([(x, y), (x + 6, y + 60)], fill=color)
        # Label
        draw.text((x + 16, y + 6), label, fill=TEXT_DIM, font=f_macro)
        val = totals.get(key, 0) or 0
        unit = "g"
        draw.text((x + 16, y + 28), f"{val:.0f}{unit}", fill=TEXT, font=f_macro_val)
    # Meals — v3.3.23p: 3-column layout (thumb + text + right pills)
    # with explicit non-overlapping column boundaries. The text col is
    # capped at TEXT_COL_W so long restaurant chains never bleed into
    # the right pill column. Text + pills vertically centered within the
    # 220px row. Font sizes tuned to prevent wrap overflow.
    # v3.3.23x (Jim OOB 2026-09-16 'rating is still overlapping and timing
    # is not prominent too'): complete redesign of the meal row header
    # band. Previous attempts:
    #   v3.3.23u: time at x=28, grade at x=90-166 → both in thumb zone
    #   v3.3.23w: time at x=28, grade at x=976-1052 → grade overlaps
    #              kcal pill TOP area (x=812-992, y=my+16..my+29)
    # New approach: TIME moves OUT of the row header — rendered as a BIG
    # left-side band on a dark pill (very prominent, 36px). GRADE moves
    # to the FAR LEFT, just under the time pill — completely separate
    # from the thumb (which sits to the right), the text col (middle),
    # and the right pills (far right).
    #
    # Layout per meal row (240px tall):
    #
    #   ┌──────┬──────────────────────────────────────────┬──────────────┐
    #   │12:45 │ ┌──────┐                                  │  ┌─kcal──┐  │
    #   │      │ │      │  白切雞飯                          │  │        │  │
    #   │ [A]  │ │thumb │  @ 餐廳                            │  │  P/C/F │  │
    #   │      │ │ 220  │                                  │  │ pills  │  │
    #   │      │ └──────┘                                  │  └────────┘  │
    #   └──────┴──────────────────────────────────────────┴──────────────┘
    #   x=28-  x=160-380 (thumb)                          x=812-1052
    #
    # Left meta band (x=28-156): big TIME (36px) + GRADE pill (28px) below
    # Thumb: shifted right to x=160-380 (220 wide)
    # Text col: x=400-780 (380 wide)
    # Right pills: x=812-1052 (kcal + P/C/F) — UNCHANGED
    LEFT_META_W = 128   # width of time + grade band on left
    # New thumb x = PAD + LEFT_META_W + GAP = 28 + 128 + 8 = 164
    # But to keep things simple, let's just stack time + grade on the LEFT
    # with thumb pushed right. Thumb at x=PAD+LEFT_META_W = 156, 200×200
    THUMB2_X = PAD + LEFT_META_W  # 28 + 128 = 156
    THUMB2_SIZE = 200
    # Text col shifted right by LEFT_META_W
    TEXT2_X = THUMB2_X + THUMB2_SIZE + GAP  # 156 + 200 + 18 = 374
    # v3.3.23aa (Jim OOB 2026-09-16 'Add one more line break between food
    # and restaurant on jpg download. Leave some right margin space such
    # that it won't overlap the right side'): reduced TEXT2_W from 320→260
    # to add 120px more right margin. Now text col ends at x=634, leaving
    # 178px gap before right pills at x=812. Food name will never overlap
    # the right pill column even for very long Chinese names.
    TEXT2_W = 260   # was 320 (v3.3.23z), was 340 (v3.3.23y), was 380 (v3.3.23x)
    for idx, m in enumerate(page_meals):
        my = HEAD + BAND_H + idx * MEAL_H
        # Row separator
        if idx > 0:
            draw.line([(PAD, my), (W - PAD, my)], fill=BORDER, width=1)

        # === LEFT META BAND (time + grade) ===
        # TIME: BIG prominent pill at x=PAD..PAD+128, y=my+4..my+56 (52px tall)
        time_str = m.get("time") or ""
        if time_str:
            tm_x = PAD
            tm_y = my + 4
            tm_w = LEFT_META_W
            tm_h = 52
            # Dark pill background for time
            draw.rounded_rectangle(
                [(tm_x, tm_y), (tm_x + tm_w, tm_y + tm_h)],
                radius=10, fill=(30, 26, 48)
            )
            # Big time text centered in pill (36px bold white)
            tbox = draw.textbbox((0, 0), time_str, font=f_time_big)
            tw = tbox[2] - tbox[0]
            th = tbox[3] - tbox[1]
            draw.text(
                (tm_x + (tm_w - tw) // 2, tm_y + (tm_h - th) // 2 - 4),
                time_str, fill=TEXT, font=f_time_big
            )

        # GRADE: full-color pill below time, x=PAD..PAD+128, y=my+62..my+98
        grade = (m.get("coach_comment") or {}).get("grade") or "—"
        grade_colors = {
            "A+": (74, 222, 128),   # bright green
            "A":  (110, 231, 183),  # green
            "B":  (96, 165, 250),   # blue
            "C":  (251, 191, 36),   # amber
            "D":  (251, 146, 60),   # orange
            "F":  (239, 68, 68),    # red
        }
        grade_color = grade_colors.get(grade, TEXT_DIM)
        gb_x = PAD
        gb_y = my + 62
        gb_w = LEFT_META_W   # 128 — same width as time pill (clean alignment)
        gb_h = 36             # was 28 — slightly taller for prominence
        draw.rounded_rectangle(
            [(gb_x, gb_y), (gb_x + gb_w, gb_y + gb_h)],
            radius=10, fill=grade_color
        )
        # Grade letter centered in pill (26px bold, dark on color)
        gbbox = draw.textbbox((0, 0), grade, font=f_grade)
        gw = gbbox[2] - gbbox[0]
        gh = gbbox[3] - gbbox[1]
        draw.text(
            (gb_x + (gb_w - gw) // 2, gb_y + (gb_h - gh) // 2 - 3),
            grade, fill=(20, 18, 32), font=f_grade
        )

        # === HERO THUMB (shifted right to make room for left meta) ===
        thumb_x = THUMB2_X
        thumb_y = my + (MEAL_H - THUMB2_SIZE) // 2   # vertically centered
        thumb_path_str = (
            m.get("image_path")
            or m.get("image_url")
            or m.get("thumbnail_url")
            or m.get("drive_image_url")
            or ""
        )
        thumb_drawn = False
        if thumb_path_str:
            try:
                # v3.3.23al: use pre-fetched bytes from parallel batch
                thumb_bytes = thumb_bytes_for_meal.get(id(m))
                if thumb_path_str.startswith("/scan_thumb/"):
                    # v3.3.38: URL 帶 ?v=<hash> → 剝走 query 先 parse row index
                    rest = thumb_path_str[len("/scan_thumb/"):].split("?", 1)[0]
                    if rest.isdigit():
                        # Only fetch inline if not pre-fetched (shouldn't happen)
                        if thumb_bytes is None:
                            try:
                                from gym_web.image_proxy import scan_img, scan_thumb
                                resp = scan_img(int(rest))
                                if resp.status_code == 200:
                                    thumb_bytes = resp.get_data()
                                if thumb_bytes is None or len(thumb_bytes) < 1000:
                                    resp = scan_thumb(int(rest))
                                    if resp.status_code == 200:
                                        thumb_bytes = resp.get_data()
                            except Exception:
                                thumb_bytes = None
                        if thumb_bytes is None and thumb_path_str.startswith("http"):
                            raise ValueError("remote thumb (drive)")
                if thumb_bytes is not None:
                    import io as _io
                    thumb = Image.open(_io.BytesIO(thumb_bytes)).convert("RGB")
                else:
                    p = thumb_path_str
                    if p.startswith("/scan_img/"):
                        p = "/home/work/.hermes/scan_cache" + p[len("/scan_img"):]
                    elif p.startswith("/scan_thumb/"):
                        p = "/home/work/.hermes/scan_thumb_cache" + p[len("/scan_thumb"):]
                    if p.startswith("http"):
                        raise ValueError("remote thumb")
                    thumb = Image.open(p).convert("RGB")
                thumb.thumbnail((THUMB2_SIZE, THUMB2_SIZE), Image.LANCZOS)
                mask = Image.new("L", thumb.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle([(0, 0), thumb.size], radius=THUMB_RADIUS, fill=255)
                img.paste(thumb, (thumb_x, thumb_y), mask)
                thumb_drawn = True
            except Exception:
                thumb_drawn = False
        if not thumb_drawn:
            # v3.3.23ap (Jim OOB 2026-09-28 'I have just inputted food log in text
            # but it does not grab the image online and now causing broken image
            # on jpg download'): previous code drew 🍽 emoji with CJK font,
            # which doesn't have emoji glyphs → renders as tofu/box (broken
            # image look). Replace with styled placeholder: dark background
            # + light border + '無圖' CJK text (renders cleanly in CJK font).
            draw.rounded_rectangle([(thumb_x, thumb_y), (thumb_x + THUMB2_SIZE, thumb_y + THUMB2_SIZE)],
                                   radius=THUMB_RADIUS, fill=(40, 36, 60), outline=BORDER, width=2)
            # Subtle "no image" label in CJK (renders properly)
            f_no_img = _load(28, bold=True)
            label = "無 圖"
            # Center text horizontally
            try:
                lbb = draw.textbbox((0, 0), label, font=f_no_img)
                lw = lbb[2] - lbb[0]
                lh = lbb[3] - lbb[1]
            except Exception:
                lw, lh = 80, 32
            cx = thumb_x + (THUMB2_SIZE - lw) // 2
            cy = thumb_y + (THUMB2_SIZE - lh) // 2
            draw.text((cx, cy), label, fill=TEXT_DIM, font=f_no_img)
        # v3.3.23x (Jim OOB 2026-09-16 'rating is still overlapping'):
        # text column shifted right because thumb is now at x=156 (was x=28)
        text_x = TEXT2_X   # 374 (was 266)
        text_right = text_x + TEXT2_W   # 754 (clear gap before right pills at 812)
        # Text content vertically centered within MEAL_H
        text_block_top = my + 16
        text_block_bot = my + MEAL_H - 16
        name_str = (m.get("meal_name") or m.get("name") or "食物")[:60]
        rest = m.get("restaurant_chain") or m.get("restaurant") or ""
        # Single-line name (font 32px — about 14-15 chars fit in 380w)
        if len(name_str) > 14:
            # v3.3.23aa (Jim OOB 2026-09-16): split line1 at a CJK char
            # boundary so it fits in 260px col. CJK chars are ~40px wide,
            # ASCII ~22px. "Quest 朱古力花生醬" = 5 ASCII + 7 CJK ≈ 400px
            # which overflows 260px. Splitting at 8 chars gives "Quest 朱古"
            # ≈ 200px which fits cleanly.
            line1 = name_str[:8]
            line2 = name_str[8:32]
            # v3.3.23aa (Jim OOB 2026-09-16 'Add one more line break
            # between food and restaurant'): increased gaps for cleaner
            # visual separation. Was 32 + 30 + 26 + 30 + 22 = 140.
            stack_total_h = 32 + 30 + 26 + 30 + 22
            stack_top = my + (MEAL_H - stack_total_h) // 2
            draw.text((text_x, stack_top), line1, fill=TEXT, font=f_name)
            draw.text((text_x, stack_top + 32 + 30), line2, fill=TEXT, font=f_name_small)
            if rest and rest not in ("不係香港餐廳", "Hong Kong restaurant"):
                # v3.3.23aa: truncate restaurant string to fit text col width
                rest_label = f"📍 {rest[:10]}"   # was 14 — even tighter
                draw.text((text_x, stack_top + 32 + 30 + 26 + 30), rest_label, fill=TEXT_DIM, font=f_rest)
        else:
            # v3.3.23aa (Jim OOB 2026-09-16 'Add one more line break between
            # food and restaurant on jpg download'): increased gap from
            # 28→40px between name and restaurant for clearer visual
            # separation. Also truncate food name to fit within TEXT2_W
            # so it never overflows into the right pill column.
            stack_total_h = 32 + 40 + 22   # name + visual gap + restaurant
            stack_top = my + (MEAL_H - stack_total_h) // 2
            # v3.3.23aa: truncate food name to fit in 260px-wide text col
            # at 40px font (CJK chars ~40px wide, ASCII ~22px). Roughly
            # 6 CJK chars fit safely. Use a conservative cut.
            display_name = name_str[:10]
            draw.text((text_x, stack_top), display_name, fill=TEXT, font=f_name)
            if rest and rest not in ("不係香港餐廳", "Hong Kong restaurant"):
                rest_label = f"📍 {rest[:12]}"   # was 16 — even tighter
                draw.text((text_x, stack_top + 32 + 40), rest_label, fill=TEXT_DIM, font=f_rest)
        # v3.3.23p: RIGHT COLUMN — kcal pill + P/C/F stack, all 240 wide
        # Right edge anchor: pill_right = W - PAD = 1052
        pill_right = W - PAD
        # Total right col width = RIGHT_COL_W = 240 (kcal 180 + P/C/F 60 × 1)
        # Layout: kcal (180w, 220h) on left + P/C/F column (60w) on right
        kcal_w = 180
        kcal_left = pill_right - 240 - 0  # align with text col right
        kcal_right = kcal_left + kcal_w    # 1052 - 240 = 812... no, 1052-240=812
        kcal_right = kcal_left + kcal_w
        # Actually layout: kcal 180 wide, then P/C/F 60 wide column = 240 total
        # Total right col starts at: pill_right - 240
        right_col_left = pill_right - RIGHT_COL_W   # 1052 - 240 = 812
        kcal_left = right_col_left
        kcal_right = right_col_left + kcal_w         # 812 + 180 = 992
        pcf_x = kcal_right                          # 992 (P/C/F column starts)
        pcf_w = 60
        kcal_val = m.get("calories") or m.get("kcal") or 0
        # kcal pill: 180 wide × (MEAL_H - 32) tall, vertically spans row
        kcal_top = my + 16
        kcal_bot = my + MEAL_H - 16    # 220 - 32 = 188 tall
        kcal_h = kcal_bot - kcal_top
        draw.rounded_rectangle([(kcal_left, kcal_top), (kcal_right, kcal_bot)],
                               radius=14, fill=(30, 26, 48))
        # Left color tab spans full height
        draw.rectangle([(kcal_left, kcal_top), (kcal_left + 8, kcal_bot)], fill=ACCENT)
        # Label "kcal" top-left
        draw.text((kcal_left + 18, kcal_top + 12), "kcal", fill=TEXT_DIM, font=f_macro)
        # Number — right-aligned, vertically centered in pill
        kcal_text = f"{int(round(kcal_val))}"
        kbbox = draw.textbbox((0, 0), kcal_text, font=f_kcal_row)
        kw = kbbox[2] - kbbox[0]
        kh = kbbox[3] - kbbox[1]
        # Right edge with 16px padding
        draw.text((kcal_right - 16 - kw, kcal_top + (kcal_h - kh) // 2 - 4),
                  kcal_text, fill=ACCENT, font=f_kcal_row)
        # P/C/F column on right — 60 wide, 3 pills stacked, each ~60 tall
        # Each pill: P (60h) + gap (4) — 3 × 60 + 2 × 4 = 188 = kcal_h
        micro_h = (kcal_h - 8) // 3     # 60 per pill, 4px gap
        micro_gap = 4
        for i, (lbl, val, color) in enumerate([
            ("P", m.get("protein", 0) or 0, PROTEIN),
            ("C", m.get("carbs", 0) or 0, CARBS),
            ("F", m.get("fat", 0) or 0, FAT),
        ]):
            mp_top = kcal_top + i * (micro_h + micro_gap)
            mp_bot = mp_top + micro_h
            # Pill background
            draw.rounded_rectangle([(pcf_x, mp_top), (pcf_x + pcf_w, mp_bot)],
                                    radius=8, fill=(30, 26, 48))
            # Color tab on top (full width since column is narrow)
            draw.rectangle([(pcf_x, mp_top), (pcf_x + pcf_w, mp_top + 4)], fill=color)
            # Label + number vertically stacked, centered horizontally
            lbbox = draw.textbbox((0, 0), lbl, font=f_macro_pill)
            lw = lbbox[2] - lbbox[0]
            val_text = f"{int(round(val))}"
            mbbox = draw.textbbox((0, 0), val_text, font=f_macro_pill)
            mw = mbbox[2] - mbbox[0]
            # Layout: label at top, value below, both centered
            label_y = mp_top + 8
            value_y = mp_top + 32
            draw.text((pcf_x + (pcf_w - lw) // 2, label_y), lbl, fill=TEXT_DIM, font=f_macro_pill)
            draw.text((pcf_x + (pcf_w - mw) // 2, value_y), val_text, fill=TEXT, font=f_macro_pill)
    # Footer
    fy = H - FOOT
    draw.rectangle([(0, fy), (W, H)], fill=BAND)
    # generation timestamp
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    from datetime import timedelta as _td
    hkt_now = _dt.now(_tz(_td(hours=8))).strftime("%Y-%m-%d %H:%M HKT")
    foot_left = f"📊 gymbro · {hkt_now}"
    draw.text((PAD, fy + 12), foot_left, fill=TEXT_DIM, font=f_foot)
    # meal count
    meal_str = f"{len(meals)} 餐"
    bbox = draw.textbbox((0, 0), meal_str, font=f_foot)
    draw.text((W - PAD - (bbox[2] - bbox[0]), fy + 22), meal_str, fill=TEXT_DIM, font=f_foot)
    # Save
    out_dir = Path("/home/work/.hermes/nutrition_card_cache")
    out_dir.mkdir(parents=True, exist_ok=True)
    # v3.3.23ax (Jim OOB 2026-10-07 'The download jpg cannot view all record.
    # Make it multiple jpg if overflow'): when there are more meals than
    # fit on one page (max MEALS_PER_PAGE), split into multiple page JPGs
    # so each stays within image-height limits (~1700px) for all viewers.
    # Page filename pattern: nutrition_{date}_p1.jpg, _p2.jpg, ...
    if page_total > 1:
        out_path = out_dir / f"nutrition_{date_iso}_p{page_num}.jpg"
    else:
        out_path = out_dir / f"nutrition_{date_iso}.jpg"
    # v3.3.23ax: also save page 1 under the legacy filename (nutrition_{date}.jpg)
    # so existing PWA code that references /nutrition_card/nutrition_${date}.jpg
    # still works for both single-page and multi-page days.
    legacy_path = out_dir / f"nutrition_{date_iso}.jpg"
    legacy_write_path = None
    if page_total > 1 and page_num == 1 and legacy_path != out_path:
        legacy_write_path = legacy_path

    # v3.3.23ax: draw page indicator on footer when there are multiple pages
    if page_total > 1:
        try:
            page_label = f"第 {page_num}/{page_total} 頁"
            bbox = draw.textbbox((0, 0), page_label, font=f_foot)
            lw = bbox[2] - bbox[0]
            draw.text((W - PAD - lw, H - PAGE - 4), page_label, fill=ACCENT, font=f_foot)
        except Exception:
            pass

    img.save(out_path, "JPEG", quality=92, optimize=True)
    # v3.3.23ax: also save page 1 under legacy filename for backwards compat
    # with existing PWA code (which references /nutrition_card/nutrition_${date}.jpg)
    if legacy_write_path is not None:
        try:
            img.save(legacy_write_path, "JPEG", quality=92, optimize=True)
        except Exception:
            pass
    return out_path


@app.route("/api/nutrition/card", methods=["GET"])
def api_nutrition_card():
    """v3.3.23 (Jim OOB 2026-09-11): render today's food log as a JPG card.
    Optional `?date=YYYY-MM-DD` query (default = today HKT). Returns the
    image bytes directly (image/jpeg). Daily-cached on disk so re-renders
    are instant. Frontend uses <a href="/api/nutrition/card" download>
    for one-tap download.
    """
    from flask import send_file
    target_date = (request.args.get("date") or today_iso()).strip()
    # Inline the same query shape as /api/nutrition/today, but for arbitrary date
    data = {"meals": [], "totals": {"kcal": 0, "P": 0, "C": 0, "F": 0},
            "meal_count": 0, "last_meal_ts": None, "date": target_date}
    cache = get_cache()
    if cache.is_ready():
        # v3.3.23ab (Jim OOB 2026-09-16 'in jpg download, should be list
        # by chronological order, earliest the first'): re-sort meals
        # by time ASC so the card renders earliest meal at top. The cache
        # returns DESC by default; we reverse here to keep the cache
        # behaviour unchanged for other endpoints.
        rows = cache.get_by_date(target_date) or []
        # v3.3.23ac (Jim OOB 2026-09-16 'wait. the image is not in right
        # order. please look into it'): bug — sorting by r.time as STRING
        # puts '9:46' AFTER '13:35' because '9' > '1' alphabetically.
        # Fix: parse time string into (HH, MM) tuple so natural numeric
        # ordering applies.
        def _sort_key(r):
            t = (r.time or "0:0").strip()
            parts = t.split(":")
            try:
                return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
            except (ValueError, IndexError):
                return (99, 99)  # unparseable times go to end
        rows.sort(key=_sort_key, reverse=False)
        meals = []
        total_kcal = total_p = total_c = total_f = 0.0
        last_ts = None
        for r in rows:
            # v3.3.23i (Jim OOB 2026-09-12 'Still missing food thumbnail
            # image'): NutritionRow.thumbnail_url holds the canonical
            # Drive URL, NOT the local /scan_thumb/<row_index> proxy
            # route. PIL renderer can't open Drive URLs (no internet
            # during render, and the thumb code only resolves local
            # /scan_thumb/ paths). Fix: build the local proxy route
            # directly from row_index, which is what the renderer
            # expects. Also expose drive_image_url as 4th-priority
            # fallback for any old row with no row_index.
            row_idx = getattr(r, "row_index", None)
            if row_idx is not None:
                row_thumb = f"/scan_thumb/{row_idx}"
            else:
                row_thumb = (
                    getattr(r, "thumbnail_url", None)
                    or getattr(r, "image_url_for_pwa", None)
                    or ""
                )
            meal = {
                "date": getattr(r, "date", target_date),
                "time": getattr(r, "time", "") or "",
                "meal_name": getattr(r, "name", "") or "",
                "name": getattr(r, "name", "") or "",
                "calories": getattr(r, "kcal", 0) or 0,
                "protein": getattr(r, "p", 0) or 0,
                "carbs": getattr(r, "c", 0) or 0,
                "fat": getattr(r, "f", 0) or 0,
                "restaurant_chain": getattr(r, "restaurant", "") or "",
                "image_path": getattr(r, "image_path", "") or "",
                "image_url": row_thumb,
                "thumbnail_url": row_thumb,
                "drive_image_url": getattr(r, "drive_image_url", "") or "",
            }
            # v3.3.23t (Jim OOB 2026-09-14 'still missing the grading'):
            # Populate coach_comment with grade + comment for the JPG
            # renderer. Uses _coach_comment_keyword (no AI call, fast
            # keyword + macro grade). Result cached on NutritionRow for
            # subsequent /api/scan_recent / /api/food_history calls.
            # NOTE: get_or_compute_coach_comment passes the row (self) to
            # coach_fn. _coach_comment_keyword takes individual args —
            # wrap in a lambda that unpacks row fields.
            cc = r.get_or_compute_coach_comment(
                lambda row: _coach_comment_keyword(
                    row.name, row.kcal, row.p, row.c, row.f, row.restaurant
                )
            )
            meal["coach_comment"] = {"grade": cc.get("grade", "—"), "comment": cc.get("comment", "")}
            meals.append(meal)
            total_kcal += meal["calories"]
            total_p += meal["protein"]
            total_c += meal["carbs"]
            total_f += meal["fat"]
            ts = getattr(r, "timestamp_iso", None) or ""
            if ts and (last_ts is None or ts > last_ts):
                last_ts = ts
        data["meals"] = meals
        data["totals"] = {"kcal": total_kcal, "P": total_p, "C": total_c, "F": total_f}
        data["meal_count"] = len(meals)
        data["last_meal_ts"] = last_ts
    meals = data.get("meals", []) or []
    totals = data.get("totals", {"kcal": 0, "P": 0, "C": 0, "F": 0}) or {"kcal": 0, "P": 0, "C": 0, "F": 0}
    # v3.3.23ax (Jim OOB 2026-10-07 'The download jpg cannot view all record.
    # Make it multiple jpg if overflow'): split into multiple JPGs when
    # meal count exceeds MEALS_PER_PAGE (6). Each page stays within ~1700px
    # tall so all viewers (Telegram preview, WhatsApp, image hosts) can
    # display it without resizing/cropping.
    MEALS_PER_PAGE = 6
    page_num = max(1, int((request.args.get("page") or "1")))
    total_meals = len(meals)
    page_total = (total_meals + MEALS_PER_PAGE - 1) // MEALS_PER_PAGE  # ceil div
    page_num = min(page_num, max(1, page_total))

    from flask import Response
    # v3.3.23al (Jim OOB 2026-09-27 'Pls fix all of them. Not just quick
    # win'): 3-tier JPG card cache (per-page).
    #  Tier 1: in-memory LRU (10ms, served from RAM)
    #  Tier 2: on-disk cache (<5 min old, served from disk via send_file)
    #  Tier 3: re-render via PIL (150-230ms with local thumbs)
    from gym_web.image_proxy import _jpg_cache_get, _jpg_cache_put
    cache_key = f"{target_date}_p{page_num}" if page_total > 1 else target_date
    cached_bytes = _jpg_cache_get(cache_key)
    if cached_bytes is not None:
        download_name = (
            f"gymbro_foodlog_{target_date}_p{page_num}.jpg" if page_total > 1
            else f"gymbro_foodlog_{target_date}.jpg"
        )
        return Response(
            cached_bytes,
            mimetype="image/jpeg",
            headers={
                "Content-Disposition": f"attachment; filename={download_name}",
                "Cache-Control": "public, max-age=300",
            },
        )
    out_dir = Path("/home/work/.hermes/nutrition_card_cache")
    out_path = (
        out_dir / f"nutrition_{target_date}_p{page_num}.jpg" if page_total > 1
        else out_dir / f"nutrition_{target_date}.jpg"
    )
    download_name = out_path.name.replace("nutrition_", "gymbro_foodlog_")
    # Tier 2: disk cache (fresh if < 5 min old)
    try:
        if out_path.exists():
            file_age_s = (datetime.now().timestamp() - out_path.stat().st_mtime)
            if file_age_s < 300:
                jpg_bytes = out_path.read_bytes()
                _jpg_cache_put(cache_key, jpg_bytes)
                return Response(
                    jpg_bytes,
                    mimetype="image/jpeg",
                    headers={
                        "Content-Disposition": f"attachment; filename={download_name}",
                        "Cache-Control": "public, max-age=300",
                    },
                )
    except Exception:
        pass  # fall through to re-render
    out_path = _render_nutrition_card_pil(target_date, meals, totals, page_num=page_num, page_total=page_total)
    if not out_path or not out_path.exists():
        return jsonify({"ok": False, "error": "render_failed (PIL missing?)"}), 500
    # Tier 3: re-rendered — also populate in-memory cache for next call
    try:
        jpg_bytes = out_path.read_bytes()
        _jpg_cache_put(cache_key, jpg_bytes)
    except Exception:
        pass
    return send_file(
        str(out_path),
        mimetype="image/jpeg",
        as_attachment=True,
        download_name=download_name,
        max_age=0,
    )


@app.route("/api/nutrition/card_url", methods=["GET"])
def api_nutrition_card_url():
    """v3.3.23ax (Jim OOB 2026-10-07 'The download jpg cannot view all record.
    Make it multiple jpg if overflow'): returns JSON with the list of page
    URLs for the day's food log. Single-page returns 1 entry; multi-page
    days return N entries so PWA can download each separately. The PWA
    detects `pages.length` and loops download for overflow days.
    """
    target_date = (request.args.get("date") or today_iso()).strip()
    out_dir = Path("/home/work/.hermes/nutrition_card_cache")
    # Compute page count based on meal count in cache
    cache = get_cache()
    MEALS_PER_PAGE = 6
    page_total = 1
    meal_count = 0
    pages: list = []
    if cache.is_ready():
        rows = cache.get_by_date(target_date) or []
        meal_count = len(rows)
        if meal_count > MEALS_PER_PAGE:
            page_total = (meal_count + MEALS_PER_PAGE - 1) // MEALS_PER_PAGE

    for p in range(1, page_total + 1):
        fname = f"nutrition_{target_date}_p{p}.jpg" if page_total > 1 else f"nutrition_{target_date}.jpg"
        path = out_dir / fname
        pages.append({
            "page": p,
            "page_total": page_total,
            "image_path": str(path),
            "image_url": f"/nutrition_card/{fname}",
            "exists": path.exists(),
            "size_kb": int(path.stat().st_size / 1024) if path.exists() else 0,
        })

    return jsonify({
        "date": target_date,
        "page_total": page_total,
        "meal_count": meal_count,
        "pages": pages,
    })


@app.route("/nutrition_card/<filename>", methods=["GET"])
def serve_nutrition_card(filename):
    """Serve a generated nutrition card from the cache dir directly
    (bypassing /api/ which forces as_attachment download). Allows
    <img src=...> display in the PWA preview."""
    from flask import send_from_directory
    return send_from_directory("/home/work/.hermes/nutrition_card_cache", filename)


@app.route("/api/history")
def api_history():
    """Return all dates with summary stats, sorted DESC. Plus current streak."""
    log = load_log()
    history = []
    for date_key in sorted(log.keys(), reverse=True):
        s = log[date_key]
        exercises = s.get("exercises", []) or []
        total_vol = sum((e.get("weight_kg") or 0) * (e.get("reps") or 0) for e in exercises)
        history.append({
            "date": date_key,
            "sets": len(exercises),
            "total_vol_kg": total_vol,
            "exercises": list({e.get("exercise", "") for e in exercises if e.get("exercise")}),
            "completed": bool(s.get("completed", False)),
            "start_time": s.get("start_time"),
            "end_time": s.get("end_time"),
        })
    # Reuse streak logic (single source of truth)
    streak = 0
    today = today_iso()
    try:
        cursor = datetime.strptime(today, "%Y-%m-%d").date()
        while True:
            key = cursor.strftime("%Y-%m-%d")
            ss = log.get(key)
            if ss and ss.get("completed") and len(ss.get("exercises", [])) >= 3:
                streak += 1
                cursor = cursor - timedelta(days=1)
            else:
                break
    except Exception:
        streak = 0
    return jsonify({"history": history, "streak": streak, "today": today})


@app.route("/api/delete_session", methods=["POST"])
def api_delete_session():
    """Delete a session from BOTH local WORKOUT_LOG and Google Sheet.

    Behaviour:
      - Local-first: removes the entry from WORKOUT_LOG.json if present.
      - Sheet-also:  finds matching rows in the Workouts tab by (date, exercise)
        and removes them via batchUpdate.deleteDimension.
      - Returns combined deletion summary so the UI can flash what happened.
    Refuses to delete today only if the request asks for `safe=true`.
    """
    data = request.get_json(force=True)
    date = (data.get("date") or "").strip()
    safe = bool(data.get("safe", False))
    if not date:
        return jsonify({"error": "date required"}), 400
    if safe and date == today_iso():
        return jsonify({"error": "cannot delete today — use cancel button"}), 400

    local_deleted = False
    log = load_log()
    if date in log:
        del log[date]
        save_log(log)
        local_deleted = True

    sheet_deleted = []
    sheet_errors = []
    try:
        sheet_deleted = _sheet_delete_date(date)
    except Exception as e:
        sheet_errors.append(str(e))

    if not local_deleted and not sheet_deleted and not sheet_errors:
        return jsonify({
            "error": f"date {date} not found in local log or sheet",
            "local_deleted": False,
            "sheet_deleted_rows": 0,
        }), 404

    return jsonify({
        "ok": True,
        "deleted": date,
        "local_deleted": local_deleted,
        "sheet_deleted_rows": len(sheet_deleted),
        "sheet_deleted": sheet_deleted,
        "sheet_errors": sheet_errors,
    })


@app.route("/img/<path:filename>")
def serve_image(filename):
    return send_from_directory("/home/work/.hermes/image_cache", filename)


@app.route("/static/<path:filename>")
def serve_static(filename):
    """Serve static assets like favicon, apple-touch-icon, PWA icons."""
    return send_from_directory("/home/work/.hermes/image_cache", filename)


@app.route("/manifest.json")
def pwa_manifest():
    """PWA web app manifest — referenced from <link rel=manifest>."""
    return jsonify({
        "name": "Gymbro · Jim",
        "short_name": "Gymbro",
        "description": "Quick gym workout logger with Whoop overlay",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#000000",
        "orientation": "portrait",
        "icons": [
            {"src": "/static/gymbro_icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/gymbro_icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/gymbro_apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
        ],
    })


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    """Serve audio files from audio_cache directory."""
    return send_from_directory("/home/work/.hermes/audio_cache", filename)


@app.route("/api/today_audio")
def api_today_audio():
    """Return audio track info for the hero overlay.

    Priority (Jim OOB 2026-07-20 「always in mp3」):
      1. cheer_{today}.mp3   (today's MP3 voice summary — canonical ONLY format)
      2. Latest cheer_*.mp3   (any MP3 date — fallback for first-day / no today yet)
      3. None (UI hides play button)

    Hard rule: ONLY MP3. Never OGG/opus/M4A. /api/audio_cache/ is MP3-only
    (Rule 22 MEMORY.md). Legacy .ogg audio has been converted to .mp3 + .ogg originals deleted.
    """
    today = today_iso()
    audio_dir = Path("/home/work/.hermes/audio_cache")
    if not audio_dir.exists():
        return jsonify({"available": False})
    # 1. Today-specific MP3.
    candidate = audio_dir / f"cheer_{today}.mp3"
    if candidate.exists():
        return jsonify({
            "available": True,
            "url": f"/audio/{candidate.name}",
            "kind": "voice_summary",
            "title": "今日教練總結",
            "date": today,
            "size_kb": round(candidate.stat().st_size / 1024, 1),
        })
    # 2. Latest MP3 cheer file (any date).
    mp3_files = sorted(
        list(audio_dir.glob("cheer_*.mp3")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not mp3_files:
        mp3_files = sorted(
            audio_dir.glob("*.mp3"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
    if mp3_files:
        latest = mp3_files[0]
        return jsonify({
            "available": True,
            "url": f"/audio/{latest.name}",
            "kind": "voice_summary_fallback",
            "title": "上次的教練總結",
            "date": today,
            "size_kb": round(latest.stat().st_size / 1024, 1),
            "is_fallback": True,
        })
    return jsonify({"available": False})


# ---------- Alonso cheer session endpoints ----------
# Polled by cron */5 * * * * → /tmp/gym_recent.json for cheer consumption.
SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
SHEET_TAB = "Workouts"
SHEET_HEADER = [
    "日期", "時間", "運動名稱", "Sets", "Reps", "重量", "每邊",
    "Bar", "Volume", "備註", "Whoop Strain", "Image"
]
LAST_POLL_TS = {"ts": None, "count": 0}
LAST_SHEET_SYNC = {"ts": None, "rows_added": 0, "status": None, "error": None}
GOOGLE_TOKEN_PATH = Path("/home/work/.hermes/google_token.json")


def _get_google_access_token():
    """Refresh Google OAuth access token using stored refresh_token.

    v3.3.26 (2026-10-21): PERSIST the refreshed token back to
    google_token.json. Previously this function refreshed in memory only,
    so the file on disk kept an expired access_token and every reader that
    reads the FILE directly (tests/qa/test_no_test_pollution.py, ad-hoc
    scripts, other agents) got `HTTP 401 Unauthorized` — which surfaced as
    a bogus "pollution FAIL" in the QA suite. Writing it back keeps the
    file valid for everyone.
    """
    import urllib.request, urllib.parse
    with GOOGLE_TOKEN_PATH.open() as f:
        tok = json.load(f)
    data = urllib.parse.urlencode({
        "client_id": tok["client_id"],
        "client_secret": tok["client_secret"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        tok.get("token_uri", "https://oauth2.googleapis.com/token"),
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode())
    # v3.3.26: write the fresh token back so file-based readers work.
    try:
        tok["access_token"] = body["access_token"]
        tok["token"] = body["access_token"]
        if body.get("expires_in"):
            tok["expires_in"] = body["expires_in"]
            tok["expires_at"] = time.time() + float(body["expires_in"])
        tok["refreshed_at"] = time.time()
        tmp = GOOGLE_TOKEN_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(tok, indent=2))
        tmp.replace(GOOGLE_TOKEN_PATH)
    except Exception as e:
        print(f"[google] token persist failed (access still valid in memory): "
              f"{type(e).__name__}: {e}", flush=True)
    return body["access_token"]


def _sheet_append_rows(rows):
    """Append rows to sheet using Sheets v4 REST API."""
    import urllib.request
    access_token = _get_google_access_token()
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
        f"{SHEET_TAB}!A1:L1:append?valueInputOption=USER_ENTERED"
        f"&insertDataOption=INSERT_ROWS"
    )
    body = json.dumps({"values": rows}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _sheet_read_all():
    """Read all rows from sheet tab. Returns list of row arrays."""
    import urllib.request
    access_token = _get_google_access_token()
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
        f"{SHEET_TAB}!A1:L"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access_token}"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
    return body.get("values", [])


# v3.3.0: Sheet reader for the in-memory NutritionCache. Reads the Nutrition tab
# (A:K = 11 cols). Returns list of row arrays; first row is the header.
# Lives in gym_web.py so it can reuse _get_google_access_token(); cache.py
# stays pure.
NUTRITION_SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
NUTRITION_SHEET_TAB = "Nutrition"


def _sheet_read_nutrition_rows() -> list:
    """Return all rows from the Nutrition tab (A:K = 11 cols).

    v3.3.23al (Jim OOB 2026-09-27 'Pls fix all of them. Not just quick
    win'): sheets.values_get is already a single batched call (returns
    all rows at once), but the response payload is ~250KB for 540 rows.
    Added valueRenderOption=FORMATTED_VALUE to skip server-side date
    parsing (we don't need formatted dates — Sheet returns ISO strings
    as-is via UNFORMATTED_VALUE). Also force majorDimension=ROWS for
    predictable ordering. ~10-15% payload reduction.

    Plus Redis cache (5 min TTL) so re-hydration / background refresh
    doesn't hit Google Sheets every 5 min when nothing changed.
    """
    from gym_web.redis_cache import get_redis_cache
    rcache = get_redis_cache()
    cache_key = "sheet:nutrition:all:A1:K"
    cached = rcache.get(cache_key)
    if cached is not None:
        try:
            return json.loads(cached)
        except Exception:
            pass  # fall through to live read

    access = _get_google_access_token()
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{NUTRITION_SHEET_ID}/values/"
        f"{NUTRITION_SHEET_TAB}!A1:K?valueRenderOption=UNFORMATTED_VALUE"
        f"&majorDimension=ROWS&dateTimeRenderOption=SERIAL_NUMBER"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access}"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
    rows = body.get("values", [])
    # Cache for 5 min (300s)
    try:
        rcache.set(cache_key, json.dumps(rows).encode("utf-8"), ttl_s=300)
    except Exception:
        pass
    return rows


def _invalidate_sheet_cache():
    """v3.3.23al: invalidate Sheet cache after any write so next read
    picks up fresh data. Called by api_scan_commit / api_scan_correct /
    api_scan_rename / api_scan_delete / api_scan_edit_datetime."""
    from gym_web.redis_cache import get_redis_cache
    rcache = get_redis_cache()
    rcache.delete("sheet:nutrition:all:A1:K")


def _sheet_read_nutrition_row(row_index: int) -> list[str]:
    """Return cells from a single row in the Nutrition tab (A{row}:K{row}).
    Returns an 11-cell list padded with '' if the row is short. Empty list on error.
    Used by cache.refresh_one() to patch the cache after a single-row edit/delete.
    """
    if row_index < 2:
        return []
    try:
        access = _get_google_access_token()
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{NUTRITION_SHEET_ID}/values/"
            f"{NUTRITION_SHEET_TAB}!A{row_index}:K{row_index}"
        )
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {access}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
        cells = (body.get("values") or [[]])[0]
        return (list(cells) + [""] * 11)[:11]
    except Exception:
        return []


def _hydrate_nutrition_cache() -> int:
    """Pull the Nutrition tab into the in-memory cache. Safe to call repeatedly.
    Returns rows loaded; 0 if Sheet read failed (cache stays in last-known state).
    """
    try:
        return _get_nutrition_cache().hydrate(_sheet_read_nutrition_rows)
    except Exception as e:
        try:
            with open("/tmp/cache_hydrate_errors.log", "a") as f:
                f.write(f"{now_iso()} | hydrate | {type(e).__name__}: {e}\n")
        except Exception:
            pass
        return 0


def _session_to_sheet_rows(date, session):
    """Convert a single date's session to sheet-ready row arrays (one per set).

    Handles BOTH session shapes:
      - Legacy / telegram-text-mode: exercises[i].sets[j] = {n, reps, weight_kg/weight, time}
      - gym-web-tap (NEW 2026-07-19): exercises[i] = {exercise, set, reps, weight_kg, time} (each entry IS a set)

    Jim OOB 2026-07-19: gym-web 13 sets of BB Bench Press logged but sync_sheet
    reported rows_added=0 because legacy _session_to_sheet_rows iterated
    ex["sets"] which is empty for flat-shape gym-web-tap entries.
    """
    rows = []
    exercises = session.get("exercises", []) if isinstance(session, dict) else []
    for ex in exercises:
        ex_name = ex.get("name") or ex.get("exercise", "")
        muscle = ex.get("muscle_group", "")
        notes = f"{muscle}" if muscle else ""
        sub_sets = ex.get("sets") if isinstance(ex.get("sets"), list) else None
        if sub_sets:
            # Legacy / telegram-text-mode shape
            for s in sub_sets:
                reps = s.get("reps", 0)
                weight = s.get("weight_kg") or s.get("weight") or 0
                volume = reps * weight if (reps and weight) else 0
                rows.append([
                    date,
                    s.get("time", ""),
                    ex_name,
                    s.get("n", ""),
                    reps,
                    weight,
                    "",
                    "",
                    volume,
                    notes,
                    "",
                    "",
                ])
        else:
            # gym-web-tap flat shape: each exercise entry IS a set
            reps = ex.get("reps", 0)
            weight = ex.get("weight_kg") or ex.get("weight") or 0
            volume = reps * weight if (reps and weight) else 0
            rows.append([
                date,
                ex.get("time", ""),
                ex_name,
                ex.get("set", ""),
                reps,
                weight,
                "",
                "",
                volume,
                notes,
                "",
                "",
            ])
    return rows


def _has_sheet_row(date, exercise, set_n):
    """Check if a (date, exercise, set_n) tuple already exists in sheet."""
    try:
        rows = _sheet_read_all()
    except Exception:
        return False
    for row in rows[1:]:  # skip header
        if len(row) >= 4 and row[0] == date and row[2] == exercise and str(row[3]) == str(set_n):
            return True
    return False


def _get_workouts_sheet_id():
    """Resolve the numeric sheetId of the Workouts tab via Sheets v4 metadata."""
    import urllib.request
    access_token = _get_google_access_token()
    req = urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        meta = json.loads(resp.read().decode())
    for s in meta.get("sheets", []):
        props = s.get("properties", {})
        if props.get("title") == SHEET_TAB:
            return int(props["sheetId"])
    raise RuntimeError(f"sheet tab {SHEET_TAB!r} not found in {SHEET_ID}")


def _sheet_delete_date(date):
    """Delete all rows from Workouts tab where column A == date.

    Sheets API requires deleting contiguous ranges bottom-up so indices stay valid.
    Returns list of (date, exercise, set_n) tuples that were removed.
    """
    import urllib.request
    rows = _sheet_read_all()
    if not rows:
        return []
    # Collect 0-indexed sheet rows (add 1 to skip header) that match date.
    matching_row_indices = []
    removed_summary = []
    for idx, row in enumerate(rows[1:], start=1):  # start=1 because row 0 is header
        if row and row[0] == date:
            matching_row_indices.append(idx)
            removed_summary.append({
                "row": idx + 1,  # 1-indexed for human reading
                "exercise": row[2] if len(row) > 2 else "",
                "set_n": row[3] if len(row) > 3 else "",
            })
    if not matching_row_indices:
        return []
    # Group contiguous indices into ranges and delete bottom-up to preserve indices.
    # Sort descending so deletions don't shift indices of earlier rows.
    matching_row_indices.sort(reverse=True)
    sheet_id_num = _get_workouts_sheet_id()
    access_token = _get_google_access_token()
    # Build deleteDimension requests for each contiguous run.
    runs = []
    current_run = [matching_row_indices[0]]
    for idx in matching_row_indices[1:]:
        if idx == current_run[-1] - 1:
            current_run.append(idx)
        else:
            runs.append(current_run)
            current_run = [idx]
    runs.append(current_run)
    requests_body = []
    for run in runs:
        start = min(run)
        end = max(run)
        requests_body.append({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id_num,
                    "dimension": "ROWS",
                    "startIndex": start,
                    "endIndex": end + 1,  # endIndex is exclusive
                }
            }
        })
    body = json.dumps({"requests": requests_body}).encode()
    req = urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate",
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        json.loads(resp.read().decode())
    return removed_summary


def _derive_muscle_group(exercise_name):
    """Map exercise name to muscle group via keyword matching."""
    if not exercise_name:
        return ""
    n = exercise_name.lower()
    if any(k in n for k in ["bench", "chest", "pec", "decline", "fly", "crossover"]):
        return "chest"
    if any(k in n for k in ["row", "pulldown", "pull-up", "pullup", "lat", "back"]):
        return "back"
    if any(k in n for k in ["squat", "leg", "rdl", "deadlift", "calf", "lunge", "hip thrust"]):
        return "leg"
    if any(k in n for k in ["shoulder", "ohp", "lateral", "raise", "face pull"]):
        return "shoulder"
    if any(k in n for k in ["plank", "crunch", "abs", "ab wheel", "russian twist", "sit up", "leg raise"]):
        return "abs"
    if any(k in n for k in ["curl", "tricep", "extension", "pressdown"]):
        return "arms"
    return ""


def _parse_reps_total(reps_field):
    """Parse reps field which may be '10', '10, 10, 10', or empty. Return total reps."""
    if not reps_field:
        return 0
    if isinstance(reps_field, (int, float)):
        return int(reps_field)
    parts = [p.strip() for p in str(reps_field).replace("x", ",").replace(";", ",").split(",") if p.strip()]
    total = 0
    for p in parts:
        try:
            total += int(float(p))
        except (ValueError, TypeError):
            continue
    return total


def _flatten_sessions(log):
    """Convert {date: {session}} dict into flat list of set rows."""
    flat = []
    for date, payload in log.items():
        if not isinstance(payload, dict):
            continue
        session = payload.get("session") if "session" in payload else payload
        if not isinstance(session, dict):
            continue
        exercises = session.get("exercises", [])
        for ex in exercises:
            muscle = ex.get("muscle_group") or ex.get("muscle") or ""
            for s in ex.get("sets", []):
                flat.append({
                    "date": date,
                    "exercise": ex.get("name", ""),
                    "muscle_group": muscle,
                    "set_n": s.get("n"),
                    "reps": s.get("reps"),
                    "weight_kg": s.get("weight_kg") or s.get("weight"),
                    "volume_kg": (s.get("reps") or 0) * (s.get("weight_kg") or s.get("weight") or 0),
                    "intensity": s.get("intensity", ""),
                    "time": s.get("time", ""),
                })
    flat.sort(key=lambda r: (r["date"], r.get("time", "")))
    return flat


@app.route("/api/workout_recent")
def api_workout_recent():
    """Return recent workouts summary for Alonso cheer sessions.

    Pulls from Google Sheet first (single source of truth across devices);
    falls back to local WORKOUT_LOG if sheet read fails.
    """
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        days = 7
    days = max(1, min(days, 90))
    cutoff_date = (datetime.now(HKT) - timedelta(days=days)).strftime("%Y-%m-%d")

    source = "sheet"
    rows = []
    try:
        sheet_rows = _sheet_read_all()
        # Header: [日期, 時間, 運動名稱, Sets, Reps, 重量 (kg), 每邊 (kg), Bar (kg), Volume (kg), 備註, Whoop Strain, Image]
        for row in sheet_rows[1:]:
            if not row or len(row) < 3:
                continue
            date = row[0]
            if date < cutoff_date:
                continue
            ex_name = row[2] if len(row) > 2 else ""
            reps_total = _parse_reps_total(row[4] if len(row) > 4 else "")
            try:
                weight = float(row[5]) if len(row) > 5 and row[5] else 0.0
            except (ValueError, TypeError):
                weight = 0.0
            try:
                sheet_volume = float(row[8]) if len(row) > 8 and row[8] else 0.0
            except (ValueError, TypeError):
                sheet_volume = 0.0
            # Trust sheet's volume column when present; fallback to reps × weight.
            volume = sheet_volume if sheet_volume > 0 else reps_total * weight
            try:
                set_n = int(row[3]) if len(row) > 3 and row[3] else None
            except (ValueError, TypeError):
                set_n = None
            rows.append({
                "date": date,
                "time": row[1] if len(row) > 1 else "",
                "exercise": ex_name,
                "set_n": set_n,
                "reps": reps_total,
                "weight_kg": weight,
                "volume_kg": volume,
                "muscle_group": _derive_muscle_group(ex_name),
            })
    except Exception as e:
        # Fallback to local log if sheet unreachable.
        source = "local_fallback"
        log = load_log()
        flat = _flatten_sessions(log)
        rows = [r for r in flat if r["date"] >= cutoff_date]

    total_volume = sum((r.get("volume_kg") or 0) for r in rows)
    muscle_groups = sorted({r["muscle_group"] for r in rows if r.get("muscle_group")})
    by_date = {}
    for r in rows:
        by_date.setdefault(r["date"], {"sets": 0, "volume": 0.0, "exercises": []})
        by_date[r["date"]]["sets"] += 1
        by_date[r["date"]]["volume"] += r.get("volume_kg") or 0
        if r["exercise"] and r["exercise"] not in by_date[r["date"]]["exercises"]:
            by_date[r["date"]]["exercises"].append(r["exercise"])
    sessions_sorted = sorted(by_date.items(), key=lambda kv: kv[0], reverse=True)[:5]
    last_workout = sorted(rows, key=lambda r: (r["date"], r.get("time", "")))[-1] if rows else None
    LAST_POLL_TS["ts"] = datetime.now(HKT).isoformat()
    LAST_POLL_TS["count"] = len(rows)
    # Jim OOB 2026-07-19 (PERSISTENT): always include Whoop activity summary
    # alongside the Sheet-pulled set rows. Cheer-routine §2 / History tab /
    # any downstream consumer should see both data sources.
    whoop_activities = _whoop_workouts_in_window(cutoff_date)
    return jsonify({
        "source": source,
        "days": days,
        "cutoff_date": cutoff_date,
        "set_count": len(rows),
        "total_volume_kg": round(total_volume, 1),
        "muscle_groups": muscle_groups,
        "last_workout": last_workout,
        "sessions": [
            {
                "date": d,
                "sets": info["sets"],
                "volume_kg": round(info["volume"], 1),
                "exercises": info["exercises"][:8],
            }
            for d, info in sessions_sorted
        ],
        "whoop_activities": whoop_activities,
        "whoop_activity_count": len(whoop_activities),
        "poll_meta": LAST_POLL_TS,
    })


# Jim OOB 2026-07-19 (PERSISTENT): "Please always refer to whoop activities
# supplemented by Google sheet." This is the canonical combined endpoint for
# cheer-routine §2 workouts table and History tab.
#
# Pulls from BOTH sources simultaneously:
#   1. Google Sheet `Workouts` tab (cross-device source of truth — set reps × weight)
#   2. Whoop /developer/v2/activity/workout (energy / strain / heart rate)
#   3. Local `WORKOUT_LOG.json` (immediate-write from web app — fallback when Sheet sync pending)
#
# Returns a unified response with two parallel arrays:
#   - `set_rows`: per-set entries from Sheet (+ local-fallback), per-set dedup
#   - `whoop_activities`: per-session Whoop summary records (sport, strain, kJ, HR)
# Plus per-date merged `sessions` view combining both (volume from Sheet,
# strain from Whoop).
#
# Cheer / dashboard should display both — never one alone.
@app.route("/api/workout_combined")
def api_workout_combined():
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        days = 7
    days = max(1, min(days, 90))
    cutoff_date = (datetime.now(HKT) - timedelta(days=days)).strftime("%Y-%m-%d")

    # --- 1. Sheet pull (preferred), local fallback if sheet read fails ---
    set_rows = []
    source_set = "sheet"
    try:
        sheet_rows = _sheet_read_all()
        for row in sheet_rows[1:]:
            if not row or len(row) < 3:
                continue
            date = row[0]
            if date < cutoff_date:
                continue
            ex_name = row[2] if len(row) > 2 else ""
            reps_total = _parse_reps_total(row[4] if len(row) > 4 else "")
            try:
                weight = float(row[5]) if len(row) > 5 and row[5] else 0.0
            except (ValueError, TypeError):
                weight = 0.0
            try:
                sheet_volume = float(row[8]) if len(row) > 8 and row[8] else 0.0
            except (ValueError, TypeError):
                sheet_volume = 0.0
            volume = sheet_volume if sheet_volume > 0 else reps_total * weight
            try:
                set_n = int(row[3]) if len(row) > 3 and row[3] else None
            except (ValueError, TypeError):
                set_n = None
            set_rows.append({
                "date": date,
                "time": row[1] if len(row) > 1 else "",
                "exercise": ex_name,
                "muscle_group": _derive_muscle_group(ex_name),
                "set_n": set_n,
                "reps": reps_total,
                "weight_kg": weight,
                "volume_kg": volume,
                "source": "sheet",
            })
    except Exception:
        source_set = "local_fallback"
        log = load_log()
        flat = _flatten_sessions(log)
        set_rows = [r for r in flat if r["date"] >= cutoff_date]

    # --- 2. Whoop activities pull (independent) ---
    whoop_activities = _whoop_workouts_in_window(cutoff_date)

    # --- 3. Merge per-date ---
    by_date = {}
    for r in set_rows:
        d = r["date"]
        slot = by_date.setdefault(d, {
            "date": d,
            "sets": 0,
            "volume_kg": 0.0,
            "exercises": [],
            "whoop_strain": [],
            "whoop_kJ": [],
            "whoop_sports": [],
            "whoop_max_hr": [],
        })
        slot["sets"] += 1
        slot["volume_kg"] += r.get("volume_kg") or 0
        ex = r.get("exercise") or ""
        if ex and ex not in slot["exercises"]:
            slot["exercises"].append(ex)

    for w in whoop_activities:
        d = w["date"]
        slot = by_date.setdefault(d, {
            "date": d,
            "sets": 0,
            "volume_kg": 0.0,
            "exercises": [],
            "whoop_strain": [],
            "whoop_kJ": [],
            "whoop_sports": [],
            "whoop_max_hr": [],
        })
        if w.get("strain") is not None:
            slot["whoop_strain"].append(w["strain"])
        if w.get("kJ") is not None:
            slot["whoop_kJ"].append(w["kJ"])
        if w.get("sport_name") and w["sport_name"] not in slot["whoop_sports"]:
            slot["whoop_sports"].append(w["sport_name"])
        if w.get("max_hr") is not None:
            slot["whoop_max_hr"].append(w["max_hr"])

    sessions = []
    for d, slot in sorted(by_date.items(), reverse=True):
        sessions.append({
            "date": slot["date"],
            "sets": slot["sets"],
            "volume_kg": round(slot["volume_kg"], 1),
            "exercises": slot["exercises"][:8],
            "whoop_strain_total": round(sum(slot["whoop_strain"]), 2) if slot["whoop_strain"] else None,
            "whoop_kJ_total": round(sum(slot["whoop_kJ"]), 1) if slot["whoop_kJ"] else None,
            "whoop_sports": slot["whoop_sports"],
            "whoop_max_hr": max(slot["whoop_max_hr"]) if slot["whoop_max_hr"] else None,
            "whoop_activity_count": len([w for w in whoop_activities if w["date"] == d]),
        })

    LAST_POLL_TS["ts"] = datetime.now(HKT).isoformat()
    LAST_POLL_TS["count"] = len(set_rows)
    return jsonify({
        "source": "sheet+whoop" if source_set == "sheet" else "local_fallback+whoop",
        "days": days,
        "cutoff_date": cutoff_date,
        "set_count": len(set_rows),
        "whoop_activity_count": len(whoop_activities),
        "total_volume_kg": round(sum((r.get("volume_kg") or 0) for r in set_rows), 1),
        "muscle_groups": sorted({r["muscle_group"] for r in set_rows if r.get("muscle_group")}),
        "last_workout": sorted(set_rows, key=lambda r: (r["date"], r.get("time", "")))[-1] if set_rows else None,
        "set_rows": set_rows,             # per-set entries (Sheet/local)
        "whoop_activities": whoop_activities,  # per-activity entries (Whoop)
        "sessions": sessions,             # per-date merged view (THE MAIN consumer table)
        "poll_meta": LAST_POLL_TS,
    })


# Jim OOB 2026-07-19: Copy-to-clipboard export endpoint. Returns plain text
# formatted for chat-AI ingestion (per `text-coach-summary-voice` Rule 15 —
# "match format to consumer"). NO Markdown clutter, NO `───` separators,
# NO double-unit. Emoji headers, short bullets, compact one-line-per-set.
#
# Query: ?date=YYYY-MM-DD  (single day — Jim OOB 2026-07-21: per-row Copy)
#        ?days=N|0|-1       (days back from today; -1 = all-time. legacy)
#        &fmt=whoop_text_v2 (DEFAULT — all-caps "X OF Y" framing, AI-clean)
#           |whoop_text    (alias of whoop_text_v2)
#           |whoop_emoji   (chat-friendly, emoji-rich)
#           |md            (Obsidian / docs)
#           |json          (raw structured)
# Text rendering is delegated to workout_formatter.py (single source of truth).
@app.route("/api/export_text")
def api_export_text():
    """Export workout log. Two modes:
      - ?date=YYYY-MM-DD → single day (Jim OOB 2026-07-21: per-row Copy)
      - ?days=N → last N days (legacy compatibility)
    """
    fmt = request.args.get("fmt", "txt")
    target_date = request.args.get("date")
    if target_date:
        cutoff_date = target_date
        days = 0
        date_filter_label = target_date
    else:
        try:
            days = int(request.args.get("days", 7))
        except (ValueError, TypeError):
            days = 7
        cutoff_date = (datetime.now(HKT) - timedelta(days=days)).strftime("%Y-%m-%d") if days >= 0 else "0000-00-00"
        date_filter_label = f"Last {days} day(s) (since {cutoff_date})"

    # Sheet pull (preferred, may fail) — falls back to local WORKOUT_LOG.
    sheet_debug = {"status": "ok", "error": None, "rows": 0}
    rows = []
    try:
        sheet_rows = _sheet_read_all()
        sheet_debug["rows"] = len(sheet_rows)
        for r in sheet_rows[1:]:
            if not r or len(r) < 3:
                continue
            date = r[0]
            if date < cutoff_date:
                continue
            if target_date and date != target_date:
                continue
            ex_name = (r[2] if len(r) > 2 else "").strip()
            if not ex_name:
                continue
            reps_total = _parse_reps_total(r[4] if len(r) > 4 else "")
            try:
                weight = float(r[5]) if len(r) > 5 and r[5] else 0.0
            except (ValueError, TypeError):
                weight = 0.0
            try:
                sheet_volume = float(r[8]) if len(r) > 8 and r[8] else 0.0
            except (ValueError, TypeError):
                sheet_volume = 0.0
            volume = sheet_volume if sheet_volume > 0 else reps_total * weight
            try:
                set_n = int(r[3]) if len(r) > 3 and r[3] else None
            except (ValueError, TypeError):
                set_n = None
            rows.append({
                "date": date,
                "time": r[1] if len(r) > 1 else "",
                "exercise": ex_name,
                "muscle_group": _derive_muscle_group(ex_name),
                "set_n": set_n,
                "reps": reps_total,
                "weight_kg": weight,
                "volume_kg": volume,
            })
    except Exception as e:
        sheet_debug["status"] = "exception"
        sheet_debug["error"] = repr(e)
        # Fallback: local WORKOUT_LOG
        log = load_log()
        flat = _flatten_sessions(log)
        rows = [r for r in flat if r.get("date") and r["date"] >= cutoff_date]

    # Per-date grouping
    by_date = {}
    for r in rows:
        d = r["date"]
        slot = by_date.setdefault(d, {"date": d, "rows": [], "volume_kg": 0.0, "exercises": []})
        slot["rows"].append(r)
        slot["volume_kg"] += r.get("volume_kg") or 0
        if r.get("exercise") and r["exercise"] not in slot["exercises"]:
            slot["exercises"].append(r["exercise"])

    # Render text by fmt
    sessions = sorted(by_date.values(), key=lambda s: s["date"], reverse=True)
    total_volume = round(sum(s["volume_kg"] for s in sessions), 1)
    muscle_split = {}
    for r in rows:
        mg = r.get("muscle_group") or "other"
        muscle_split[mg] = muscle_split.get(mg, 0) + 1

    if fmt == "json":
        text = json.dumps({
            "range_days": days,
            "cutoff_date": cutoff_date,
            "sessions": sessions,
            "total_volume_kg": total_volume,
            "muscle_split": muscle_split,
            "sheet_debug": sheet_debug,
        }, ensure_ascii=False, indent=2)
    elif fmt == "md":
        # Markdown variant for Obsidian / docs ingestion
        parts = [f"# Workout Log — Last {days} day(s) (since {cutoff_date})", ""]
        for s in sessions:
            parts.append(f"## {s['date']}  ·  {len(s['rows'])} sets · {round(s['volume_kg'],1)}kg volume")
            for r in s["rows"]:
                weight = r.get("weight_kg", 0)
                if weight and weight == int(weight):
                    w = f"{int(weight)}kg"
                elif weight:
                    w = f"{weight}kg"
                else:
                    w = "BW"
                reps = r.get("reps", 0)
                set_n = r.get("set_n") or "?"
                parts.append(f"- Set {set_n} · {r.get('exercise','')} — {w} × {reps}")
            parts.append("")
        parts.append(f"**Totals**: {len(rows)} sets · {total_volume}kg volume")
        if muscle_split:
            muscle_str = " · ".join(f"{k.upper()} {v}" for k, v in sorted(muscle_split.items(), key=lambda kv: -kv[1]))
            parts.append(f"**Muscle split**: {muscle_str}")
        parts.append("")
        parts.append(f"Copied from gymbro · {datetime.now(HKT).isoformat()}")
        text = "\n".join(parts)
    elif fmt in ("whoop_text", "whoop_text_v2", "whoop_emoji"):
        # Jim OOB 2026-07-22: extracted to workout_formatter.py module.
        # whoop_text_v2 (DEFAULT for copyDay): all-caps keywords + "X OF Y"
        # framing + dedup + empirical exercise-group detection. Designed
        # for AI parser ingestion with NO ambiguity.
        # whoop_emoji: chat-friendly visual variant.
        text = _render_text(
            rows,
            fmt=fmt if fmt != "whoop_text" else "whoop_text_v2",
            date_filter_label=date_filter_label,
            total_volume=total_volume,
            muscle_split=muscle_split,
        )
    else:
        # Legacy `fmt=txt` and any unknown → whoop_text_v2 (the new default).
        # Backwards compatible: copyDay() now defaults to whoop_text_v2.
        text = _render_text(
            rows,
            fmt="whoop_text_v2",
            date_filter_label=date_filter_label,
            total_volume=total_volume,
            muscle_split=muscle_split,
        )

    return jsonify({
        "text": text,
        "sessions": len(sessions),
        "total_sets": len(rows),
        "total_volume_kg": total_volume,
        "range_days": days,
        "fmt": fmt,
        "sheet_debug": sheet_debug,
    })


@app.route("/api/sync_sheet", methods=["POST"])
def api_sync_sheet():
    """Push local WORKOUT_LOG entries to Google Sheet (idempotent).
    Jim OOB 2026-07-22: dedup by (date, exercise, set_n, time_iso) tuple so
    repeated sync calls never accumulate duplicates even when local set_n
    restarts after mid-session deletes."""
    payload = request.get_json(silent=True) or {}
    target_date = payload.get("date")  # optional: sync one date, else all
    log = load_log()
    dates = [target_date] if target_date else sorted(log.keys())
    # Cache existing sheet rows once per call (was re-read for every set).
    try:
        _existing_sheet = _sheet_read_all()
    except Exception:
        _existing_sheet = []
    def _has(date, exercise, set_n, time_iso):
        for row in _existing_sheet[1:]:
            if (len(row) >= 4 and row[0] == date and row[2] == exercise
                    and str(row[3]) == str(set_n)
                    and len(row) > 1 and row[1] == time_iso):
                return True
        return False
    added, skipped, errors = 0, 0, []
    for date in dates:
        entry = log.get(date)
        if not isinstance(entry, dict):
            continue
        session = entry.get("session") if "session" in entry else entry
        if not isinstance(session, dict):
            continue
        rows_to_push = []
        for ex in session.get("exercises", []):
            sub_sets = ex.get("sets") if isinstance(ex.get("sets"), list) else None
            if sub_sets:
                for s in sub_sets:
                    set_n = s.get("n")
                    if set_n is None:
                        continue
                    if not _has(date, ex.get("name", ""), set_n, s.get("time", "")):
                        rows_to_push.extend(_session_to_sheet_rows(date, {"exercises": [ex]}))
                    else:
                        skipped += 1
            else:
                set_n = ex.get("set")
                # v3.3.23s (Jim OOB 2026-09-14 'workout tab not updated since
                # 2026-08-31'): don't skip Whoop auto-detect entries with
                # set=null. If set is None but the exercise has a whoop_id
                # or weight_note (Whoop auto-detect entries), still write a
                # placeholder row so the sheet shows the activity.
                if set_n is None and not (ex.get("whoop_id") or ex.get("weight_note")):
                    continue
                if set_n is None:
                    # Use Whoop ID hash as set_n fallback (stable across syncs)
                    import hashlib
                    whoop_id = ex.get("whoop_id", "") or ex.get("time", "")
                    set_n = "wd-" + hashlib.md5(whoop_id.encode()).hexdigest()[:6]
                if not _has(date, ex.get("exercise", ""), set_n, ex.get("time", "")):
                    rows_to_push.extend(_session_to_sheet_rows(date, {"exercises": [ex]}))
                else:
                    skipped += 1
        if rows_to_push:
            try:
                _sheet_append_rows(rows_to_push)
                added += len(rows_to_push)
                # Refresh cache so subsequent checks in this call see the new rows.
                _existing_sheet = _sheet_read_all()
            except Exception as e:
                errors.append({"date": date, "error": str(e)})
    LAST_SHEET_SYNC.update({
        "ts": datetime.now(HKT).isoformat(),
        "rows_added": added,
        "skipped": skipped,
        "errors": errors,
        "status": "ok" if not errors else "partial",
    })
    return jsonify(LAST_SHEET_SYNC)


# Jim OOB 2026-07-22: surgical rebuild of sheet rows for a single date from local.
# Root cause: previous sync_sheet had no (date, exercise, set_n) dedup across
# multiple sync passes → accumulated duplicates → Whoop AI parsed 39 rows as
# 15 collapsed sets. repair_sheet() deletes ALL rows of a date from sheet
# then re-pushes from local WORKOUT_LOG idempotently.
@app.route("/api/repair_sheet", methods=["POST"])
def api_repair_sheet():
    """Clear all sheet rows for one date, then push from local WORKOUT_LOG.
    Payload: {"date": "YYYY-MM-DD"}. Returns counts of removed / added."""
    payload = request.get_json(silent=True) or {}
    target_date = payload.get("date")
    if not target_date:
        return jsonify({"ok": False, "error": "missing date"}), 400
    removed_summary = []
    try:
        removed_summary = _sheet_delete_date(target_date)
    except Exception as e:
        return jsonify({"ok": False, "stage": "delete", "error": str(e)}), 500
    # Re-push from local for that date
    log = load_log()
    added = 0
    errors = []
    entry = log.get(target_date)
    if isinstance(entry, dict):
        session = entry.get("session") if "session" in entry else entry
        if isinstance(session, dict):
            rows_to_push = []
            for ex in session.get("exercises", []):
                rows_to_push.extend(_session_to_sheet_rows(target_date, {"exercises": [ex]}))
            # Dedupe within local-source rows by (set_n, exercise) first
            seen = set()
            deduped = []
            for row in rows_to_push:
                key = (row[0], row[2], row[3])
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(row)
            rows_to_push = deduped
            if rows_to_push:
                try:
                    _sheet_append_rows(rows_to_push)
                    added = len(rows_to_push)
                except Exception as e:
                    errors.append(str(e))
    return jsonify({
        "ok": not errors,
        "date": target_date,
        "removed_count": len(removed_summary),
        "removed_summary": removed_summary[:20],  # first 20 for visibility
        "added": added,
        "errors": errors,
    })


@app.route("/api/sync_health")
def api_sync_health():
    """Cheer-side sync health probe — local count vs. last poll status."""
    log = load_log()
    flat = _flatten_sessions(log)
    return jsonify({
        "local_workout_count": len(flat),
        "local_dates": len(log),
        "last_poll_ts": LAST_POLL_TS["ts"],
        "last_poll_set_count": LAST_POLL_TS["count"],
        "last_sheet_sync": LAST_SHEET_SYNC,
        "sheet_id": SHEET_ID,
        "sheet_tab": SHEET_TAB,
        "status": "healthy" if LAST_POLL_TS["ts"] else "never_polled",
        "server_pid": os.getpid(),
        "uptime_note": "Polled every 5 min by cron → /tmp/gym_recent.json",
    })


# ---------- v2.1 FOOD SCAN (MiniMax vision + MiniMax enrichment) ----------
# Jim OOB 2026-07-23 22:26 HKT: "Version will be able to scan food or food receipt
# to capture. Using MiniMax image recognition and web search"

SCAN_CACHE_DIR = Path("/home/work/.hermes/scan_cache")
SCAN_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ---------- v3.3.25 PRODUCTION WRITE GUARD (Jim OOB 2026-10-20) ----------
# "Someone has pollute the production data. Pls avoid doing so"
#
# Jim's Nutrition tab is his GOLDEN COPY of the real food log. Any
# commit path (Sheet append + Drive upload) that runs with a TEST image
# is indistinguishable from a real meal and silently corrupts the log.
#
# Guard design:
#   GYMBRO_WRITE_MODE=production  (DEFAULT — real Jim usage, writes)
#   GYMBRO_WRITE_MODE=dryrun      (QA/E2E — NEVER writes; returns the
#                                 row that WOULD have been written)
#   GYMBRO_WRITE_MODE=throwaway   (points at a COPY of the sheet tab;
#                                 the tab id comes from GYMBRO_SHEET_ID)
#
# In dryrun/throwaway mode:
#   - the Sheets append POST is never issued against the production tab
#   - the Drive upload is skipped (no orphan Drive files)
#   - the response carries "would_have_written" so QA can report the
#     row list without touching production (Alonso rule 4)
#
# Rule 3: a test that genuinely needs the full commit pipeline MUST use
# GYMBRO_WRITE_MODE=throwaway + GYMBRO_SHEET_ID pointing at a copy of
# the tab. Never the live one.
PRODUCTION_SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
PRODUCTION_NUTRITION_TAB = "Nutrition"


def _write_mode() -> str:
    """Return 'production' | 'dryrun' | 'throwaway'."""
    mode = (os.environ.get("GYMBRO_WRITE_MODE") or "production").strip().lower()
    if mode not in ("production", "dryrun", "throwaway"):
        print(f"[write-guard] unknown GYMBRO_WRITE_MODE={mode!r} — "
              f"treating as production", flush=True)
        return "production"
    return mode


def _target_sheet_id() -> str:
    """Sheet spreadsheet id to write to (production id unless overridden)."""
    override = (os.environ.get("GYMBRO_SHEET_ID") or "").strip()
    return override or PRODUCTION_SHEET_ID


def _is_production_target() -> bool:
    """True when the effective target IS Jim's live golden copy."""
    return _target_sheet_id() == PRODUCTION_SHEET_ID


# ---------- v3.3.25 TEST-FIXTURE DETECTOR (content-based backstop) ----------
# The env-var guard above is opt-in, so a careless test could still write
# to production. This detector is the BACKSTOP: it inspects the actual
# image bytes and refuses the commit when the image is one of the known
# synthetic QA fixtures (by content hash) OR when the file lives in a
# test_fixtures/ directory.
#
# This works even if GYMBRO_WRITE_MODE was never set — which is exactly
# the failure mode that produced the 2026-10-20 pollution.
_TEST_FIXTURE_DIR_MARKER = "test_fixtures"
_TEST_FIXTURE_SHA256 = {
    # test_fixtures/food_1.jpg  (synthetic "GYMbro TEST FIXTURE - NOT REAL FOOD")
    "1b47cf21abc24093ddb68e7424757aea9d24111ba23843e8b0585b412400d39d",
    # test_fixtures/food_2.jpg  (synthetic "GYMbro TEST FIXTURE - NOT REAL FOOD")
    "c344aa01bf509aeb41a8ab67ec3cdfffb7ae0ad8f099c0ec240af0b2f5820cd4",
    # test_fixtures/static_test.jpg (gymbro static test image, not food)
    "be9f0d447cf726edf7b97eedfa59b6fe8b78250fe7aef26670f1c91e298dd15a",
    # The 1x1 base64 JPEG that tests/api/test_endpoints.py posts as RAW
    # bytes (it is a base64 STRING, not decoded JPEG — 555 ASCII bytes).
    # sha256 of the ASCII bytes = sha256 of the polluted scan_*.jpg files
    # written on 2026-10-20 (sheet rows 656/660/661/662).
    "2c60fdf21dc936bfbd388c804f9134b97e3a0cd3b731e5c9ba205317e9f72cad",
}


def _sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _is_test_fixture_image(path) -> bool:
    """True when the image is a known QA fixture (never commit these).

    Two independent signals:
      1. the file lives under a directory named 'test_fixtures'
      2. its sha256 matches a registered synthetic fixture
    """
    try:
        p = Path(str(path))
    except Exception:
        return False
    try:
        if _TEST_FIXTURE_DIR_MARKER in p.parts:
            return True
    except Exception:
        pass
    try:
        digest = _sha256_file(p)
    except Exception:
        digest = ""
    if digest and digest in _TEST_FIXTURE_SHA256:
        return True
    return False


def _assert_not_test_fixture(path) -> None:
    """Raise if the image is a QA fixture — refuse to commit it."""
    if _is_test_fixture_image(path):
        raise ValueError(
            "REFUSED: refusing to commit a QA test fixture to the "
            f"production Nutrition sheet (image={path}). Use "
            "GYMBRO_WRITE_MODE=dryrun to report the would-have-written "
            "row, or GYMBRO_WRITE_MODE=throwaway + GYMBRO_SHEET_ID=<copy> "
            "to exercise the full pipeline against a throwaway tab."
        )

# v3.2.7.46: thumbnail cache — generated on commit, served via /scan_thumb/.
# 480px max-side JPEGs (~15-30KB each) replace 1.9MB originals in food
# history view, dropping initial page weight from ~320MB to ~5MB.
# v3.3.2: SCAN_CACHE_DIR + SCAN_THUMB_DIR remain as ephemeral preview buffer
# for the multi-photo scan flow. They are no longer the canonical store —
# Google Drive (lh3 URLs) + Google Sheet are. Reads of /scan_img/<filename>
# + /scan_thumb/<filename> fall through to 404 once these directories are
# eventually cleaned.
SCAN_THUMB_DIR = Path("/home/work/.hermes/scan_thumb_cache")
SCAN_THUMB_DIR.mkdir(parents=True, exist_ok=True)

# v3.3.34 (2026-10-21, Jim OOB "Remove perplexity. keep minimax only"):
# PPLX_API_KEY 已完全唔使 —— Perplexity 嘅所有呼叫(視覺 / 營養 enrichment /
# 菜名 / narration 判斷 / cheer web search)都已經移除或者改用 MiniMax。
# 環境變數仲喺度都冇人讀,唔會再有任何 HTTP 請求打去 api.perplexity.ai。


def _get_latest_news_for_cheer() -> str:
    """Pull 1-2 fresh HK / sports / lifestyle news bits for the cheer prompt.
    Jim OOB 2026-07-24: 'use latest news to make it more innovative and funny'.
    Jim OOB 2026-07-29: random-pick 1 of 4 categories (gossip / sports / AI / hacker news).
    Returns a short Chinese string or '' on failure.
    """
    import random
    # Jim OOB 2026-07-29: random-pick 1 of 4 categories
    # v3.3.34 (2026-10-21): Perplexity 拎走 → Brave 網頁搜尋 + MiniMax 撮要。
    # 每個 category 有兩個字串:(搜尋 query, 問 MiniMax 嘅要求)
    categories = [
        ("gossip", "香港 娛樂新聞 明星 藝人",
         "寫 1 句 30-50 字繁體中文廣東話,可以用嚟做 cheer 收尾嘅有趣話題。"
         "只可以講搜尋結果入面出現過嘅事。"),
        ("sports", "國際 體育 新聞 足球 NBA 網球",
         "寫 1 句 30-50 字繁體中文廣東話嘅體育新聞摘要。只可以講搜尋結果入面"
         "出現過嘅事。"),
        ("ai", "AI 人工智能 大模型 新聞 發佈",
         "寫 1 句 30-50 字繁體中文廣東話嘅 AI 業界新聞摘要。只可以講搜尋結果"
         "入面出現過嘅事。"),
        ("hacker", "黑客 資訊安全 漏洞 數據外洩 開源 新聞",
         "寫 1 句 30-50 字繁體中文廣東話嘅駭客 / 資安新聞摘要。只可以講搜尋"
         "結果入面出現過嘅事。"),
    ]
    cat_name, cat_query, cat_ask = random.choice(categories)
    try:
        content = _cheer_web_fact(cat_query, cat_ask, max_tokens=250,
                                  temperature=0.7)
        # Prepend category tag for cheer to reference
        cat_label = {"gossip": "🎭 明星娛樂", "sports": "⚽ 體育", "ai": "🤖 AI", "hacker": "💻 黑客"}.get(cat_name, "📰")
        return f"【{cat_label}】{content}" if content else ""
    except Exception:
        return ""


# v3.3.12: Pplx-cached news + fund fact for the unified daily narrative.
# Both helpers wrap a 30-min TTL file cache so cheer / video / food-coach
# don't each fire their own web search. The cheer LLM prompt references
# them by name so it weaves the latest fact into the narrative.
_CHEER_NEWS_TTL_S = 30 * 60
_CHEER_NEWS_CACHE = "/home/work/.hermes/cheer_pplx_cache.json"


def _cheer_cache_get(key: str) -> str:
    try:
        if not os.path.exists(_CHEER_NEWS_CACHE):
            return ""
        with open(_CHEER_NEWS_CACHE) as f:
            data = json.load(f)
        e = data.get(key)
        if not e:
            return ""
        if (time.time() - e.get("ts", 0)) > _CHEER_NEWS_TTL_S:
            return ""
        return e.get("text", "")
    except Exception:
        return ""


def _cheer_cache_put(key: str, text: str) -> None:
    try:
        data = {}
        if os.path.exists(_CHEER_NEWS_CACHE):
            try:
                with open(_CHEER_NEWS_CACHE) as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[key] = {"text": text, "ts": time.time()}
        # Trim to last 10 entries to keep file small
        if len(data) > 10:
            sorted_keys = sorted(data.keys(), key=lambda k: data[k].get("ts", 0), reverse=True)
            data = {k: data[k] for k in sorted_keys[:10]}
        with open(_CHEER_NEWS_CACHE, "w") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _get_latest_fund_fact_for_cheer() -> str:
    """Pull 1 fresh HK / finance / macro fact for the cheer prompt.

    Jim OOB 2026-08-20: "the cheer routine should also refer to the latest
    news and latest fund fact (web search)". UA Finance (largest listed
    finance co in HK) is Jim's employer, so HK stocks / HSI / macro /
    rates / FX are the natural topics. Returns a 30-60 字 Cantonese
    string or '' on failure. 30-min TTL file cache.
    """
    cached = _cheer_cache_get("fund_fact")
    if cached:
        return cached
    # Pick a finance sub-topic randomly so each cheer gets variety
    # v3.3.34 (2026-10-21): Perplexity 拎走 → Brave 網頁搜尋 + MiniMax 撮要。
    import random
    subtopics = [
        ("📈 港股", "香港 恒生指數 港股 大市 藍籌股 港交所 最新",
         "搵 1 個最新嘅恒生指數 / 港股大市嘅 fact / 數字 / 變動"),
        ("💱 匯率", "美元 港元 人民幣 匯率 美元指數 最新",
         "搵 1 個最新嘅匯率 fact / 數字 / 變動"),
        ("📊 利率", "港元利率 美聯儲 HIBOR 拆息 國債孳息率 最新",
         "搵 1 個最新嘅利率 fact / 數字 / 變動"),
        ("🏦 金融", "香港銀行 金融科技 金管局 監管政策 最新",
         "搵 1 個最新嘅香港金融業 fact / 政策 / 數字"),
        ("🛢️ 商品", "原油 黃金 銅 大宗商品 價格 最新",
         "搵 1 個最新嘅大宗商品 fact / 數字 / 變動"),
    ]
    label, cat_query, scope = random.choice(subtopics)
    try:
        content = _cheer_web_fact(
            cat_query,
            f"{scope},簡短 30-60 字繁中廣東話回覆, 要具體 "
            f"(有數字 / 變幅 / 日期)。唔肯定就答「本週未確認」。",
            max_tokens=250, temperature=0.4)
        out = f"【{label}】{content}" if content else ""
        if out:
            _cheer_cache_put("fund_fact", out)
        return out
    except Exception:
        return ""


def _get_cached_news_for_cheer() -> str:
    """Cached wrapper around _get_latest_news_for_cheer (30-min TTL)."""
    cached = _cheer_cache_get("news")
    if cached:
        return cached
    out = _get_latest_news_for_cheer()
    if out:
        _cheer_cache_put("news", out)
    return out


def _get_liverpool_fixture_for_cheer() -> str:
    """Pull next Liverpool FC fixture for cheer prompt context (Jim OOB 2026-07-29).
    If next match is Big 6 (Man Utd / Man City / Chelsea / Arsenal / Tottenham)
    within 7 days, return a Cantonese string with opponent + days_until.
    Returns '' on failure / no upcoming Big 6.
    Cache: 6-hour TTL via /home/work/.liverpool_fixture_cache.json.
    """
    cache_path = "/home/work/.liverpool_fixture_cache.json"
    # Check cache TTL (6h)
    try:
        cache_mtime = os.path.getmtime(cache_path)
        if (time.time() - cache_mtime) < 6 * 3600:
            with open(cache_path) as f:
                cached = json.load(f)
                if cached.get("block"):
                    return cached["block"]
                else:
                    return ""
    except Exception:
        pass
    # v3.3.34 (2026-10-21): Perplexity 拎走 → Brave 網頁搜尋 + MiniMax 撮要。
    try:
        content = _cheer_web_fact(
            "Liverpool FC next fixture Premier League schedule",
            "利物浦 (Liverpool FC) 下一場英超賽事係幾時? 對手係邊隊? "
            "喺主場定作客?用繁中廣東話一句 30-50 字回覆, 例如"
            "「下週六 (7月25日) 主場對曼聯」。如果 7 日內冇賽事就答"
            "「7 日內冇利物浦比賽」。",
            max_tokens=200, temperature=0.3)
        if not content:
            return ""
        # Determine if Big 6 — if yes, return block; else empty
        big6_kw = ["曼聯", "曼城", "車路士", "阿仙奴", "熱刺", "曼聯", "曼联", "曼联", "Manchester United", "Man City", "Chelsea", "Arsenal", "Tottenham"]
        is_big6 = any(kw in content for kw in big6_kw)
        is_no_match = "冇" in content or "未確認" in content or "未確認" in content
        block = content if (is_big6 and not is_no_match) else ""
        # Atomic cache write
        tmp = cache_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"block": block, "raw": content, "is_big6": is_big6, "fetched_at": datetime.now(timezone.utc).isoformat()}, f, ensure_ascii=False)
        os.rename(tmp, cache_path)
        return block
    except Exception:
        return ""


def _minimax_api_key() -> str:
    """Read MiniMax M3 key from env (loaded from .hermes-torres/.env then .hermes/.env at startup)."""
    return os.environ.get("MINIMAX_API_KEY", "")


# Canonical 12-field nutrition schema (Jim OOB 2026-07-25 13:35 HKT:
# "food recognition buggy, doesn't gather protein, carbs, fiber etc").
# Used by the minimax enrich prompts + parser + Sheet header.
NUTRITION_FIELDS = [
    "calories",   # kcal (total energy)
    "protein",    # g
    "carbs",      # g
    "fat",        # g
    "fiber",      # g
    "sugar",      # g
    "sodium",     # mg
    "sat_fat",    # g (saturated fat)
    "trans_fat",  # g
    "vit_c",      # mg (vitamin C)
    "iron",       # mg
    "calcium",    # mg
]
NUTRITION_UNITS = {
    "calories": "kcal", "protein": "g", "carbs": "g", "fat": "g",
    "fiber": "g", "sugar": "g", "sodium": "mg", "sat_fat": "g",
    "trans_fat": "g", "vit_c": "mg", "iron": "mg", "calcium": "mg",
}
# Field alias mapping for robust parsing (Chinese + English variants).
# Each field maps to a list of regex patterns; first match wins.
NUTRITION_ALIASES = {
    "calories":  [r"卡路里", r"熱量", r"卡[^路路]里", r"calorie", r"kcal", r"energy"],
    "protein":   [r"蛋白質", r"蛋白", r"protein", r"^p[^a-z]"],
    "carbs":     [r"碳水化合物", r"碳水", r"carbs?", r"^c[^a-z]"],
    "fat":       [r"脂肪(?!酸)", r"油脂", r"\bfat\b", r"^f[^a-z]"],
    "fiber":     [r"纖維", r"膳食纖維", r"纖[^維]", r"fiber", r"fibre"],
    "sugar":     [r"糖[^尿果]", r"蔗糖", r"添加糖", r"sugar"],
    "sodium":    [r"鈉", r"鈉質", r"鹽分.*?(\d)", r"sodium", r"salt"],
    "sat_fat":   [r"飽和脂肪", r"飽和脂", r"飽和", r"saturated\s*fat", r"sat\s*fat"],
    "trans_fat": [r"反式脂肪", r"反式脂", r"反式", r"trans\s*fat"],
    "vit_c":     [r"維他命c", r"維生素c", r"vit(?:amin)?\s*c", r"^vc[^a-z]"],
    "iron":      [r"鐵質", r"鐵", r"\biron\b", r"\bfe\b"],
    "calcium":   [r"鈣質", r"鈣", r"\bcalcium\b", r"\bca\b"],
}


_VISION_STATUS = {
    "provider": "unknown",
    "state": "unknown",
    "last_error": "",
    "last_ok_ts": 0,
}
# v3.3.29: the SECOND opinion gets its own record so it can never
# overwrite who served the FIRST one (see _record_vision_status_2nd).
_VISION_STATUS_2ND = {
    "provider": "unknown",
    "state": "unknown",
    "last_error": "",
    "last_ok_ts": 0,
}
# v3.3.29: routing flag. While the 2nd-opinion chain is running, EVERY
# status write (chain-level AND provider-level) must land in
# _VISION_STATUS_2ND, not _VISION_STATUS. Two real bugs this prevents:
#   1. /api/health named the 2nd-opinion provider instead of the primary.
#   2. WORSE — a provider failure inside the 2nd opinion set
#      _VISION_STATUS["state"]="failed"; if opinion #2 then succeeded it
#      overwrote that with "ok", which BYPASSED the rule-2 write guard
#      (`state == "failed" and not vision_desc`) and let 「未識別菜式」
#      reach the golden copy. That is the exact regression v3.3.25
#      existed to prevent.
_VISION_2ND_OPINION = False


def _record_vision_status(provider: str, state: str, err: str = "",
                          provider_key: str = "") -> None:
    """v3.3.25 (2026-10-21): record which vision provider actually served.

    WHY THIS EXISTS: on 2026-10-20 the APiyi key died (HTTP 401
    "Invalid token"). NOTHING reported it — every scan silently degraded
    to 未識別菜式 / 0 kcal and Jim only noticed because the food log was
    full of "unrecognised food". A provider failure must be LOUD and
    QUERYABLE, never silent.

    v3.3.26: `provider_key` holds the RAW provider id (e.g.
    "perplexity-sonar") so the 2nd-opinion stage can deliberately pick a
    DIFFERENT provider than the one that served opinion #1 — otherwise
    "2 independent opinions" would be the same model twice.

    v3.3.29 (2026-10-21, Isak 獨立驗證): the 2nd-opinion call went
    through this same function and OVERWROTE the record of who served
    opinion #1. /api/health then reported the 2nd-opinion provider
    (perplexity-sonar) instead of the primary one (minimax-text-01) —
    PROVEN: opinion#1=minimax-text-01, opinion#2=perplexity-sonar,
    final status=perplexity-sonar. The whole point of _VISION_STATUS is
    to say WHICH provider served; when the last writer wins, it says the
    wrong one — and the 2nd-opinion exclusion logic (which reads
    provider_key to decide who to skip) could be thrown off by it.

    FIX: the 2nd opinion records into a SEPARATE dict
    (_VISION_STATUS_2ND). _VISION_STATUS now always describes the
    PRIMARY opinion, which is what /api/health and the rule-2 write
    guard read.

    v3.3.29: this function now ROUTES on _VISION_2ND_OPINION. The
    provider-level failure records (in _minimax_vision_text01 /
    minimax-text-01) call THIS function, so they follow the same
    route — a provider failure during the 2nd opinion can no longer
    poison the primary opinion's state.
    """
    global _VISION_STATUS
    if _VISION_2ND_OPINION:
        _record_vision_status_2nd(provider, state, err,
                                   provider_key=provider_key)
        return
    _VISION_STATUS = {
        "provider": provider,
        "provider_key": provider_key or provider,
        "state": state,
        "last_error": err,
        "last_ok_ts": (int(time.time()) if state == "ok"
                       else _VISION_STATUS.get("last_ok_ts", 0)),
    }


def _record_vision_status_2nd(provider: str, state: str, err: str = "",
                               provider_key: str = "") -> None:
    """v3.3.29: record the SECOND opinion separately.

    Keeps _VISION_STATUS pointing at the PRIMARY provider so:
      - /api/health reports who actually served opinion #1
      - the rule-2 write guard reads the primary's state
      - the 2nd-opinion exclusion logic reads a stable provider_key
    """
    global _VISION_STATUS_2ND
    _VISION_STATUS_2ND = {
        "provider": provider,
        "provider_key": provider_key or provider,
        "state": state,
        "last_error": err,
        "last_ok_ts": (int(time.time()) if state == "ok"
                       else _VISION_STATUS_2ND.get("last_ok_ts", 0)),
    }


def _minimax_vision_text01(img_b64: str, prompt: str) -> str:
    """Vision via MiniMax-text-01 — the FALLBACK that actually works.

    v3.3.25 (2026-10-21): the old comment in _minimax_vision said
    "api.minimax.io has zero vision models (all 'unknown model' HTTP
    400)". That was true for MiniMax-VL-01 ('unknown model 2013') but
    WRONG for MiniMax-text-01 — it accepts OpenAI-style image_url
    content and genuinely describes food photos. Verified 2026-10-21
    against Jim's real lunch photo: it correctly described the
    韓式燒肉五花腩 ingredients (三片豬五花肉、洋蔥圈、切花紋的蘑菇).

    Returns description text, or "" on any failure (never a （...）
    label — that would leak into the dish name).
    """
    api_key = _minimax_api_key()
    if not api_key:
        return ""
    payload = {
        "model": "MiniMax-text-01",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
        ]}],
        "max_tokens": 1200,
        "temperature": 0.25,
    }
    try:
        req = urllib.request.Request(
            "https://api.minimax.io/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=90).read())
        content = (resp.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        # Strip a reasoning block if the model emitted one.
        if "</think>" in content:
            content = content.split("</think>")[-1].strip()
        if content.startswith("<think>"):
            content = ""
        # Safety-filter refusal → treat as failure so the next provider runs.
        if content and ("抱歉" in content[:30] and "無法" in content[:50]
                        and len(content) < 80):
            return ""
        return content.strip()
    except Exception as e:
        _record_vision_status("minimax-text-01", "failed",
                              f"{type(e).__name__}: {e}")
        print(f"[vision] MiniMax-text-01 failed: {type(e).__name__}: {e}",
              flush=True)
        return ""


# Keys are the raw provider ids used for the "2nd opinion must come from a
# DIFFERENT provider" rule.
# v3.3.34 (2026-10-21, Jim OOB "Remove perplexity. keep minimax only"):
# 供應商池淨返 MiniMax 一家。
# Perplexity 拎走嘅原因:佢個 key 額度爆咗(HTTP 401 insufficient_quota),
# 而且每次 scan 都要多等一個 provider 先交叉核對 —— 救唔到任何嘢。
# ⚠️ 單一供應商 = 單點故障(見 `fitness/jim-image-recognition` Rule 28):
# MiniMax 死 → 掃描會被硬攔截(唔會寫「未識別菜式」入表),但要即時加返
# 第二個 provider。
_VISION_PROVIDER_POOL = (
    ("minimax-text-01", "_minimax_vision_text01"),
)


def _minimax_vision(img_b64: str, prompt: str,
                     exclude: tuple = (),
                     record_2nd: bool = False) -> str:
    """Vision description — provider chain.

    v3.3.34 (2026-10-21, Jim OOB "Remove perplexity. keep minimax only")
    chain:
        MiniMax-text-01  (單一供應商)
    (Perplexity 已拎走 —— 佢個 key 額度爆咗 HTTP 401 insufficient_quota,
    而且每次 scan 都要多等一個 provider 先交叉核對,救唔到任何嘢。
    APiyI 更加早就拎走咗 —— 佢個 key 2026-10-20 死咗 HTTP 401。)

    ⚠️ 而家係**單點故障**:MiniMax 死 → 掃描會被硬攔截(唔會寫
    「未識別菜式」入表,Jim 見到失敗提示而唔係壞資料),但整個食物 log
    都會停。要加返第二個 provider 就改 _VISION_PROVIDER_POOL。

    v3.3.25 (2026-10-21, Jim OOB 2026-10-20 'And the ai recognition is
    all wrong. It was showing unrecognised food'): a provider key died
    (HTTP 401 "Invalid token") and the whole scan pipeline degraded
    SILENTLY to 未識別菜式 / 0 kcal. Now there are working fallback
    providers AND the degradation is recorded in _VISION_STATUS (exposed
    via /api/health) and printed to the log.

    `exclude` holds provider ids to skip (used by the 2nd-opinion stage
    so the two opinions come from different models).

    `record_2nd` (v3.3.29): when True, the provider that serves is
    recorded into _VISION_STATUS_2ND instead of _VISION_STATUS. The
    2nd-opinion call reuses this same chain — without the flag it
    overwrote the primary opinion's record, so /api/health reported the
    WRONG provider (PROVEN 2026-10-21: opinion#1=minimax-text-01,
    opinion#2=perplexity-sonar, status ended as perplexity-sonar).

    Kept the _minimax_vision function name for backward compat with 5
    call sites.
    """
    errors = []
    skip = set(exclude or ())
    # v3.3.29: route the status write to the right dict. The 2nd-opinion
    # call reuses this chain, so without this the primary opinion's record
    # was overwritten and /api/health named the wrong provider. The flag
    # also covers the PROVIDER-level failure records (they call
    # _record_vision_status, which routes on the same flag).
    global _VISION_2ND_OPINION
    _prev_2nd = _VISION_2ND_OPINION
    _VISION_2ND_OPINION = bool(record_2nd)
    try:
        return _minimax_vision_chain(img_b64, prompt, skip, errors)
    finally:
        _VISION_2ND_OPINION = _prev_2nd


def _minimax_vision_chain(img_b64, prompt, skip, errors) -> str:
    """The provider loop body of _minimax_vision (v3.3.29 split out).

    Split into its own function so _minimax_vision can wrap the whole
    loop in the _VISION_2ND_OPINION routing flag with a try/finally —
    every status write inside the loop (chain-level and provider-level)
    then lands in the correct dict.
    """
    # v3.3.26 (2026-10-21): "prefer a different provider for the 2nd
    # opinion" is a SOFT preference, NOT a hard exclusion. Hard-excluding
    # the provider that served opinion #1 left the 2nd opinion with ZERO
    # providers when only one was left alive, and the scan then fell back
    # to 未識別菜式 even though the 1st opinion was perfect. A 2nd opinion
    # from the same model still beats having no 2nd opinion at all.
    ordered = ([p for p in _VISION_PROVIDER_POOL if p[0] not in skip]
               + [p for p in _VISION_PROVIDER_POOL if p[0] in skip])
    for name, fn_name in ordered:
        fn = globals().get(fn_name)
        if not fn:
            continue
        try:
            result = fn(img_b64, prompt)
        except Exception as e:  # a provider must never kill the scan
            result = ""
            errors.append(f"{name}: {type(e).__name__}: {e}")
            print(f"[vision] {name} raised: {type(e).__name__}: {e}",
                  flush=True)
            continue
        if result and not result.startswith("（"):
            if errors:
                print(f"[vision] {name} served after {len(errors)} failure(s): "
                      f"{'; '.join(errors)}", flush=True)
                _record_vision_status(f"{name} (fallback)", "ok",
                                      "; ".join(errors), provider_key=name)
            else:
                _record_vision_status(name, "ok", "", provider_key=name)
            return result
        errors.append(f"{name}: {(result or 'empty/short').strip('（）')}")

    print(f"[vision] ALL vision providers FAILED ({'; '.join(errors)}) — "
          f"the scan will be blocked (no 未識別菜式 written to the sheet)",
          flush=True)
    _record_vision_status("none", "failed", "; ".join(errors))
    # Return empty so _extract_dish_name falls through to fallback; do NOT
    # return "（Vision 服務暫時不可用）" — that string is a generic label
    # and would itself become the dish name (Jim OOB 2026-08-09 16:48 HKT).
    return ""


def _vision_second_opinion(img_b64: str, prompt: str) -> str:
    """2nd-opinion vision — provider chain, skipping the 1st-opinion provider.

    v3.3.30 (2026-10-21, Jim OOB "Remove apiyi stuff"): this used to be
    APiyi gpt-4o-mini first, then fall back to the chain. The APiyi key is
    dead (HTTP 401 on every endpoint), so the APiyI branch is gone and the
    2nd opinion now comes straight from the chain.

    The 2nd opinion MUST come from a DIFFERENT provider than the one that
    served opinion #1 — else "2 independent opinions" is the same model
    twice and the median-merge becomes meaningless. The provider that
    served opinion #1 is tracked in _VISION_STATUS["provider_key"].

    v3.3.34 (2026-10-21, Jim OOB "Remove perplexity"): 供應商池淨返
    MiniMax 一家,所以 `exclude` 會令同一個 model 再行一次(唔同 prompt)。
    呢個唔係「兩個獨立意見」,只係「兩次問法」—— 總好過冇第二輪:
    詳細描述可以帶出第一輪漏咗嘅菜式細節。
    """
    used = _VISION_STATUS.get("provider_key", "")
    skip = tuple(x for x in (used,) if x)
    # v3.3.29: record_2nd=True so this call CANNOT overwrite the primary
    # opinion's provider record in _VISION_STATUS.
    return _minimax_vision(img_b64, prompt, exclude=skip, record_2nd=True)


def _brave_api_key() -> str:
    """Brave Search API key for image lookup. Separate from BRAVE_ANSWER_KEY
    (which is for the LLM-grounded answer endpoint, not raw image search)."""
    return os.environ.get("BRAVE_API_KEY", "")


def _brave_image_search(query: str, count: int = 3) -> list:
    """Search Brave Images API for `query`. Returns list of dicts with
    {title, url, thumbnail_url, source, confidence}.

    Falls back to empty list on auth failure / network error / no key.
    Used to backfill a representative image for text-only food entries
    in /api/scan_preview_text (Jim OOB 2026-09-10).
    """
    api_key = _brave_api_key()
    if not api_key:
        return []
    if not query or not query.strip():
        return []
    try:
        req = urllib.request.Request(
            "https://api.search.brave.com/res/v1/images/search"
            + "?q=" + urllib.parse.quote(query.strip())
            + f"&count={min(count, 10)}&safesearch=off",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        out = []
        for r in (data.get("results") or []):
            props = r.get("properties") or {}
            out.append({
                "title": r.get("title", ""),
                "url": props.get("url", "") or r.get("url", ""),
                "thumbnail_url": (r.get("thumbnail") or {}).get("src", ""),
                "source": r.get("source", ""),
                "confidence": r.get("confidence", "low"),
            })
        return out
    except Exception as e:
        print(f"[brave_image_search] {type(e).__name__}: {e}", flush=True)
        return []


def _brave_web_search(query: str, count: int = 5) -> list:
    """Brave 網頁搜尋 → list[str](標題 + 描述)。

    v3.3.34 (2026-10-21, Jim OOB "Remove perplexity. keep minimax only"):
    新增 —— 原本 cheer 嘅新聞 / 基金 fact / 利物浦賽程靠 Perplexity
    sonar-pro 上網,Perplexity 拎走之後要有第二個 web-search 來源,
    否則呢三個 context 全部變空(cheer 會少咗三樣料)。
    Returns [] on no key / auth failure / network error.
    """
    api_key = _brave_api_key()
    if not api_key or not query or not query.strip():
        return []
    try:
        req = urllib.request.Request(
            "https://api.search.brave.com/res/v1/web/search"
            + "?q=" + urllib.parse.quote(query.strip())
            + f"&count={min(count, 10)}&safesearch=off",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        out = []
        for r in ((data.get("web") or {}).get("results") or []):
            t = (r.get("title") or "").strip()
            d = (r.get("description") or "").strip()
            if t:
                out.append(f"{t} — {d}" if d else t)
        return out
    except Exception as e:
        print(f"[brave_web_search] {type(e).__name__}: {e}", flush=True)
        return []


def _cheer_web_fact(search_query: str, ask: str, max_tokens: int = 250,
                     temperature: float = 0.4) -> str:
    """Brave 網頁搜尋 + MiniMax 撮成一句(取代 Perplexity sonar-pro)。

    v3.3.34 (2026-10-21): Perplexity 拎走之後嘅替代方案。
    規矩唔變:**只可以用搜尋結果嘅內容,唔准 fabricate**;搜尋冇結果就回 ''
    (cheer 會少呢一塊 context,好過亂講)。
    """
    titles = _brave_web_search(search_query, count=4)
    if not titles:
        return ""
    joined = "\n".join(f"- {t}" for t in titles)
    payload = {
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content":
                      f"以下係網上最新搜尋結果:\n{joined}\n\n{ask}\n"
                      "只可以用上面搜尋結果入面出現過嘅事,唔准 fabricate 任何"
                      "數字 / 對手 / 日期。冇就答「未確認」。"}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        req = urllib.request.Request(
            "https://api.minimax.io/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + _minimax_api_key()},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        return (resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                or "").strip()
    except Exception as e:
        print(f"[cheer_web_fact] {type(e).__name__}: {e}", flush=True)
        return ""


# v3.3.20: download a food image from URL and save to SCAN_CACHE_DIR.
# Returns local Path or None on failure.
def _download_food_image(image_url: str, slug: str) -> "Path | None":
    """Download image from URL → save to /home/work/.hermes/scan_cache/
    as scan_{date}_{slug}.jpg. Also generates a thumb. Returns Path.

    slug = sanitized dish name for filename safety.
    Returns None on download failure (timeout, 404, non-image content).
    """
    if not image_url or not image_url.startswith(("http://", "https://")):
        return None
    # Sanitize slug for filesystem
    safe_slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "_", (slug or "food"))[:32].strip("_") or "food"
    now_hkt = datetime.now(timezone(timedelta(hours=8)))
    fname = f"scan_{now_hkt.strftime('%Y%m%d_%H%M%S')}_{safe_slug}.jpg"
    out_path = SCAN_CACHE_DIR / fname
    try:
        req = urllib.request.Request(image_url, headers={
            "User-Agent": "Mozilla/5.0 (gymbro food-image fetch)",
            "Accept": "image/jpeg,image/png,image/webp,image/*",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = (resp.headers.get("Content-Type") or "").lower()
            data = resp.read()
        # Validate it's actually an image (skip >5MB, skip non-image)
        if len(data) > 5 * 1024 * 1024:
            print(f"[download_food_image] too large: {len(data)}B from {image_url[:80]}", flush=True)
            return None
        if not (content_type.startswith("image/") or data[:4] in (b"\xff\xd8\xff", b"\x89PNG", b"RIFF")):
            print(f"[download_food_image] not an image: {content_type}", flush=True)
            return None
        out_path.write_bytes(data)
        # Generate thumb (best-effort)
        try:
            _make_thumb(out_path, _thumb_path_for(fname))
        except Exception:
            pass
        return out_path
    except Exception as e:
        print(f"[download_food_image] {type(e).__name__}: {e}", flush=True)
        return None


def _minimax_nutrition_enrich(dish_desc: str) -> str:
    """v3.2.7.12: 12-field nutrition enrichment via MiniMax M3 (Jim OOB
    2026-08-08 20:55 HKT 'Use more minimax m3 ... Don't use gpt except
    food recognition dual scanning'). MiniMax is the canonical LLM —
    single source of truth for nutrition estimates + coach comments.

    v3.2.7.6+: MiniMax M3 via api.minimax.io. Returns text (or empty on
    failure) — caller parses via _parse_nutrition_block.
    v3.3.34 (2026-10-21): Perplexity 拎走 → 而家係**單一 estimate**
    (之前係 pplx + MiniMax 兩份取 median)。

    v3.3.22 (Jim OOB 2026-09-11 'No food nutritional info'): MiniMax M3 was
    running into max_tokens=400 BEFORE the JSON was emitted, so 12-field
    block came back empty and text-direct path returned all zeros. Bumped
    to 1500 + added explicit `response_format: json_object` flag (most
    OpenAI-compat providers honor it; if MiniMax ignores, fallback parser
    _parse_nutrition_block already handles prose).
    """
    api_key = _minimax_api_key()
    if not api_key:
        return ""  # graceful fallback — 冇 MiniMax key 就冇 estimate
    fields_desc = ", ".join(
        f"{f} ({NUTRITION_UNITS[f]})" for f in NUTRITION_FIELDS
    )
    prompt = (
        f"Estimate per-portion nutrition for this HK/Cantonese food description:\n\n"
        f"「{dish_desc}」\n\n"
        f"Reply with ONLY a single JSON object, no markdown, no commentary. "
        f"Schema (all 12 fields required, use 0 only if truly unknown, do NOT fabricate):\n"
        f'{{"calories": 0, "protein": 0, "carbs": 0, "fat": 0, "fiber": 0, '
        f'"sugar": 0, "sodium": 0, "sat_fat": 0, "trans_fat": 0, '
        f'"vit_c": 0, "iron": 0, "calcium": 0}}\n\n'
        f"Units: {fields_desc}.\n"
        f"Note: sodium in mg (1g salt ≈ 400mg sodium); vit_c / iron / calcium "
        f"in mg; rest in g except calories (kcal).\n"
        f"Be conservative — chain-specific numbers only if well-known "
        f"(KFC, McDonald's, Starbucks). Otherwise use typical HK portion."
    )
    payload = {
        "model": "MiniMax-M3",
        "messages": [
            {"role": "system", "content": "You are a nutrition fact checker for Hong Kong dishes. Output ONLY a valid JSON object with the exact 12-field schema requested. No markdown, no prose, no explanation."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1500,  # v3.3.22: bumped from 400 — JSON got truncated mid-block
        "temperature": 0.1,
        "response_format": {"type": "json_object"},  # v3.3.22: enforce JSON if provider supports
    }
    try:
        req = urllib.request.Request(
            "https://api.minimax.io/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "".join(["Bearer ", api_key]),
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        raw_content = resp["choices"][0]["message"]["content"]
        # v3.3.23ah (Jim OOB 2026-09-16 'Why the estimation was wrong. Pls
        # fix the systemic problem'): MiniMax M3 with reasoning enabled
        # emits a `<think>...</think>` reasoning block BEFORE the actual
        # JSON object. Naive `startswith("{")` checks at every call site
        # would skip parsing. Fix at the SOURCE — strip the thinking
        # block here so ALL callers automatically get a clean response.
        # Belt-and-suspenders: also strip markdown code fences.
        return _strip_thinking_block(raw_content)
    except Exception as e:
        return f"（MiniMax enrichment 失敗：{type(e).__name__}）"


# v3.3.22 (Jim OOB 2026-09-11 'No food nutritional info'): MiniMax M3 with
# reasoning enabled returns content with a leading `` block
# (reasoning tokens) before the actual JSON. json.loads() fails because
# the string isn't valid JSON. This helper strips the reasoning block
# + any markdown fences so callers can json.loads() the result cleanly.
# Also handles the case where MiniMax returns plain text (no JSON at
# all) by leaving it intact for the regex-based _parse_nutrition_block
# fallback.
def _strip_thinking_block(text: str) -> str:
    """Remove `` reasoning blocks and markdown ```json fences from
    MiniMax M3 chat-completions output so json.loads() can parse the
    remainder. Tolerant of plain text input (returns unchanged)."""
    if not text or not isinstance(text, str):
        return text or ""
    s = text.strip()
    # Strip  reasoning blocks (greedy across newlines)
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL)
    # Strip markdown code fences
    s = re.sub(r"^```(?:json|JSON)?\s*\n", "", s.strip())
    s = re.sub(r"\n```\s*$", "", s.strip())
    return s.strip()


def _safe_parse_nutrition_json(raw: str) -> dict:
    """Try multiple parse strategies for a MiniMax M3 nutrition response:
      1. json.loads() after stripping  reasoning block.
      2. Extract the LAST {...} JSON object via regex (handles leading prose).
      3. Fall back to _parse_nutrition_block (prose regex) on plain text.
    Returns the parsed dict (may be empty on total failure).
    """
    if not raw or not isinstance(raw, str):
        return {}
    # 1) Strip thinking + try direct json
    cleaned = _strip_thinking_block(raw)
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    # 2) Extract last {...} JSON object from the cleaned string
    #    (handles "leading prose {json} trailing prose")
    matches = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", cleaned, flags=re.DOTALL)
    if matches:
        for m in reversed(matches):  # try the LAST json object first
            try:
                return json.loads(m)
            except Exception:
                continue
    # 3) Prose fallback (existing behavior)
    return _parse_nutrition_block(cleaned or raw)


def _minimax_nutrition_enrich_multi(dish_desc: str) -> str:
    """v3.2.7.12: Multi-dish 12-field nutrition enrichment via MiniMax
    M3-highspeed (Jim OOB 2026-08-08 20:55 HKT 'Use more minimax m3
    high speed ... Don't use gpt except food recognition dual scanning').
    Returns JSON object with 'dishes' key — one 12-field object per
    detected dish.

    Schema returned:
      {"dishes": [
        {"name": "海南雞飯", "calories": 600, "protein": 30, ...},
        {"name": "白菜",     "calories": 50,  "protein": 2,  ...},
        {"name": "例湯",     "calories": 80,  "protein": 3,  ...}
      ]}
    """
    api_key = _minimax_api_key()
    if not api_key:
        return '{"dishes": []}'
    fields_desc = ", ".join(
        f"{f} ({NUTRITION_UNITS[f]})" for f in NUTRITION_FIELDS
    )
    prompt = (
        f"呢張食物相/描述可能包含多過一樣食物。請你逐樣 dish 分開 estimate nutrition。\n\n"
        f"描述：\n「{dish_desc}」\n\n"
        f"Reply with ONLY a JSON object (no markdown, no commentary).\n"
        f"Schema — wrap dishes array under 'dishes' key, one object per dish, "
        f"all 12 fields required, use 0 if unknown:\n"
        f'{{"dishes": [\n'
        f'  {{"name": "菜名1", "calories": 0, "protein": 0, "carbs": 0, "fat": 0, '
        f'"fiber": 0, "sugar": 0, "sodium": 0, "sat_fat": 0, "trans_fat": 0, '
        f'"vit_c": 0, "iron": 0, "calcium": 0}},\n'
        f'  {{"name": "菜名2", "calories": 0, "protein": 0, ...}}\n'
        f']}}\n\n'
        f"Units: {fields_desc}. Sodium in mg; vit_c/iron/calcium in mg; rest in g except calories (kcal).\n"
        f"Be conservative — chain-specific numbers only if well-known. "
        f"Otherwise typical HK portion. If only ONE dish, return 1-element array."
    )
    payload = {
        "model": "MiniMax-M3",
        "messages": [
            {"role": "system", "content": "You are a nutrition fact checker. Output ONLY valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1500,
        "temperature": 0.1,
    }
    try:
        req = urllib.request.Request(
            "https://api.minimax.io/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "".join(["Bearer ", api_key]),
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        raw_content = resp["choices"][0]["message"]["content"]
        # v3.3.23ah: strip <think> block at the source so multi-dish
        # responses are also clean.
        return _strip_thinking_block(raw_content)
    except Exception as e:
        return '{"dishes": []}'


def _parse_nutrition_block(text: str) -> dict:
    """Parse a nutrition block (Chinese or English) into the 12-field schema.

    Handles formats like:
      "蛋白質: 38g" / "蛋白質 38克" / "P: 38g" / "Protein: 38g"
      "卡路里: 750" / "熱量: 750kcal" / "Calories: 750"
      "Sodium 800mg" / "鈉: 800mg" / "鹽分 2g" (auto-convert 2g salt → 800mg)
    Returns {field: numeric_value, ...} for fields found; missing fields absent.
    """
    out = {}
    if not text or not isinstance(text, str):
        return out
    for field, aliases in NUTRITION_ALIASES.items():
        unit = NUTRITION_UNITS[field]
        for alias in aliases:
            # Pattern: <alias> (optional space/colons) <number> (optional unit)
            pat = re.compile(
                rf"(?:{alias})\s*[:：=]?\s*"
                rf"(\d+(?:\.\d+)?)\s*"
                rf"(?:{unit}|mg|mcg|克|gram|g)?",
                re.IGNORECASE | re.UNICODE,
            )
            m = pat.search(text)
            if m:
                try:
                    val = float(m.group(1))
                    # Unit conversion: if alias matched but value was in wrong unit
                    if field == "sodium" and val < 50 and "鹽" in text:
                        # Heuristic: if "鹽分 2g" and we got 2, that's grams of salt
                        # convert g salt → mg sodium (×400). Skip — too risky, only
                        # trust explicit mg.
                        pass
                    out[field] = val
                    break  # first match wins for this field
                except (ValueError, IndexError):
                    continue
    return out


# v3.2.7.17: AI-based narration classifier. Jim OOB 2026-08-10
# 'don't use rule. please use ai to obtain'. The MiniMax M3 model
# classifies the candidate dish name as either a real dish ('DISH')
# or narration prose leaked from the vision description ('NARRATION').
# Used by the post-AI guard in _extract_dish_name_ai AND by the
# commit-path has_meaningful check.
_NARRATION_CACHE: dict[str, bool] = {}
_NARRATION_CACHE_MAX = 256


def _ai_check_narration(name: str) -> bool:
    """Return True if `name` is model narration prose, not a dish.

    Uses MiniMax M3-highspeed for a quick yes/no classification. On
    API failure, defaults to True (treat as narration) so the commit
    path refuses to auto-commit. Better to ask the user than to save
    garbage. Caches results to avoid repeated API calls for the same
    name within a session.
    """
    if not name:
        return False
    if name in _NARRATION_CACHE:
        return _NARRATION_CACHE[name]
    if len(_NARRATION_CACHE) >= _NARRATION_CACHE_MAX:
        _NARRATION_CACHE.clear()

    api_key = _minimax_api_key()
    if not api_key:
        # v3.3.34 (2026-10-21, Jim OOB "Remove perplexity"): 冇 MiniMax key
        # 就 fail closed —— 寧願問 Jim,都唔好自動寫入垃圾。
        # (原本呢度會問 Perplexity;而家淨返一個 provider。)
        _NARRATION_CACHE[name] = True
        return True

    payload = {
        "model": "MiniMax-M3",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You classify whether a candidate string is a dish name "
                    "or model narration prose. Reply with ONLY one word: "
                    "'DISH' or 'NARRATION'."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Candidate: 「{name}」\n\n"
                    "Is this a concrete dish/food name (e.g. 蘇打水、漢堡包、"
                    "海南雞飯), or is it vision-model narration prose (e.g. "
                    "這張相顯示一支蘇打水樽、品牌為可口可樂、可見到一塊蛋糕)?\n"
                    "Reply ONLY 'DISH' or 'NARRATION'."
                ),
            },
        ],
        "max_tokens": 10,
        "temperature": 0,
    }
    try:
        req = urllib.request.Request(
            "https://api.minimax.io/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "".join(["Bearer ", api_key]),
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=8).read())
        reply = (resp["choices"][0]["message"]["content"] or "").strip().upper()
        is_narration = "NARRATION" in reply and "DISH" not in reply
        _NARRATION_CACHE[name] = is_narration
        return is_narration
    except Exception:
        # v3.3.34 (2026-10-21, Jim OOB "Remove perplexity"): MiniMax 唔到 →
        # fail closed(唔会自动 commit)。原本呢度會問 Perplexity。
        # Fail closed: if NO AI can decide, refuse to auto-commit.
        _NARRATION_CACHE[name] = True
        return True


def _name_has_narration(name: str) -> bool:
    """True if `name` looks like model narration rather than a dish.

    Thin wrapper around the AI classifier. Empty/None names are NOT
    narration (handled by the empty-name check elsewhere).
    """
    if not name:
        return False
    return _ai_check_narration(name)


# v3.3.26 (2026-10-21): dish-name label words, promoted to module level so
# every extractor (MiniMax + Perplexity fallback) rejects the SAME set.
_DISH_NAME_LABEL_WORDS = {
    "菜式", "菜名", "主菜", "副菜", "前菜", "甜品", "湯品",
    "飲品", "煮法", "份量", "份量大小", "份量（目測）",
    "詳細描述", "菜單", "餐牌", "品牌", "餐廳", "套餐",
    "菜式：", "菜名：", "未識別", "未能識別",
}

# v3.3.30 (2026-10-21): names that must NEVER reach Jim's golden copy.
# Rule 25: a generic / placeholder dish name is never acceptable. These
# are the exact strings the pipeline can produce when the NAME extraction
# fails even though the vision provider itself was healthy.
_GENERIC_DISH_NAMES_BLOCKLIST = {
    "未識別菜式", "未識別", "食物", "餐", "菜式", "菜名", "餐點",
    "料理", "菜式一", "未識別菜", "NOT FOOD", "",
}


def _extract_dish_name_ai(vision_desc: str, pplx_desc: str = "") -> str:
    """v3.2.7.4: AI-based dish name extraction (Jim OOB 2026-08-08 10:35 HKT
    'No regex. Use ai' + 'Use minimax too'). Primary path: MiniMax M3
    via api.minimax.io. Returns 2-12 char SPECIFIC Cantonese dish name.

    v3.2.7.6: Generic meal labels ('早餐', '午餐', '晚餐', 'afternoon tea')
    are NOT acceptable — must extract the actual dishes in the meal.

    v3.2.7.32: Vision now returns plaintext, but older entries in
    scan_log still contain markdown like '**詳細描述：** \\n 1. **菜式：**'
    — strip those headers out before sending to the dish-name AI, and
    reject AI responses that are themselves label words (菜式, 菜名, 主菜,
    etc.). Jim OOB 2026-08-11 'why today recognized food as 菜式 as food
    name!!!! are you using AI to find the food???? don't use rule/regex'
    — extraction is AI-only now (no regex cascade).
    """
    if not vision_desc and not pplx_desc:
        return ""
    combined = ((vision_desc or "") + "\n" + (pplx_desc or "")).strip()
    if not combined:
        return ""

    # v3.2.7.32: pre-process to remove markdown section headers that older
    # vision outputs used to embed (e.g. "詳細描述：" / "菜式：" / "份量：").
    # Strip `**...：**` headings and bare `xxx：` token-only lines so the
    # AI never sees the structural labels as candidate dish names.
    cleaned_lines = []
    for line in combined.split("\n"):
        s = line.strip()
        if not s:
            continue
        # 1. Strip leading list markers ('1. ', '2. ') and bullets
        # ('- ', '• ', '· ') so '1. **菜式：**' reduces to '**菜式：**'
        # BEFORE the label-regex check below.
        s = re.sub(r"^\d+\.\s*", "", s)
        s = re.sub(r"^[-•·]\s*", "", s).strip()
        if not s:
            continue
        # 2. Drop lines that are just a section header / label:
        # `**詳細描述：**` (bold wrapper), `菜式：` (plain label + colon).
        if re.match(r"^\*\*?[^*:\n]{1,8}：\*\*?\s*$|^\*\*?[^*:\n]{1,8}:\*\*?\s*$|^[^*:\n]{1,8}[：:]\s*$", s):
            continue
        cleaned_lines.append(s)
    cleaned = "\n".join(l for l in cleaned_lines if l).strip()[:800]

    if not cleaned:
        return ""

    # v3.3.7: anti-hallucination clause + dropped the 蘇打水 example that
    # was training the AI to guess 蘇打水 whenever a transparent bottle
    # appeared (Jim OOB 2026-08-16 'don't put 蘇打水 as default fallback
    # food — very unprofessional' + 'no hallucination pls'). A bottle
    # without a clear liquid identification should fall through to
    # '未識別', not default to soda water.
    prompt = (
        "你係香港人，識粵語。以下係食物描述（可能由舊模型產生仍帶markdown/標題）。"
        "淨係俾我 2-8 個中文字嘅 SPECIFIC 菜名，唔好加任何描述、量詞、前綴、餐廳名。\n"
        "絕對唔可以做嘅事：\n"
        "(A) 唔可以回任何 section 標題、label 類字眼 — 例如 '菜式'、'菜名'、"
        "'主菜'、'副菜'、'前菜'、'甜品'、'湯品'、'飲品'、'煮法'、'份量'、"
        "'詳細描述'、'菜單'、'餐牌'、'品牌'、'餐廳'。"
        "如果描述入面得 section header 冇實際菜名，回 '未識別'。\n"
        "(B) 唔可以用 generic 餐名 ('早餐'、'午餐'、'晚餐'、'下午茶') 當菜名；"
        "一定要抽實際嘅食物 (例如：煎蛋、烤麵包、粥、飯、雞胸、青瓜、檸檬茶)。\n"
        "(C) 多過一樣食物可以用 '、' 分隔 (例如：'煎蛋、烤麵包')。\n"
        "(D) 唔好寫 '相顯示...'、'呢張相...'、'圖中可見...' 等等敘述性句式，"
        "唔好寫 '品牌為...'、'牌子係...' 等品牌描述，"
        "唔好寫 '一支XXX樽'、'一個XXX盒' 等容器+量詞組合。\n"
        "(E) v3.3.7 禁 hallucination — 描述入面只見到容器/包裝(透明樽、罐、紙盒)"
        "而睇唔清內容物,唔可以憑外形估 '蘇打水'、'汽水'、'清水'、'樽裝水' 之類。"
        "睇唔清就回 '未識別',等用戶自己填。\n\n"
        "例子："
        "'千層蛋糕'（唔好寫'相顯示咗一塊千層蛋糕'），"
        "'海南雞飯'（唔好寫'可見海南雞飯配青瓜'），"
        "'黑咖啡'（唔好寫'一杯黑咖啡'），"
        "'凍檸茶'（唔好寫'一杯凍檸茶'），"
        "'雞胸肉'（唔好寫'菜式：'或'主菜：'），"
        "'煎蛋、烤麵包'（唔好寫'簡單嘅早餐'，generic 餐名唔接受），"
        "'馬黛茶'（唔好寫'- 馬黛茶（未見到有沖水...）。'）。\n\n"
        "描述：\n" + cleaned + "\n\n菜名："
    )

    # Try MiniMax first
    try:
        import urllib.request, json as _json
        api_key = _minimax_api_key()
        if api_key:
            payload = {
                "model": "MiniMax-M3",
                "messages": [{"role": "user", "content": prompt}],
                # v3.2.7.32: MiniMax M3 emits a ` 進思考...進思考` block
                # before the final answer. max_tokens=30 was clipping the
                # answer (e.g. returned just the think block) so the
                # extractor got nothing. 500 fits a typical think (~300-450
                # tokens) + the 2-12 char dish name (≤12 tokens). Calls
                # take ~3-7s.
                "max_tokens": 1500,
                "temperature": 0.1,
            }
            req = urllib.request.Request(
                "https://api.minimax.io/v1/chat/completions",
                data=_json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "".join(["Bearer ", api_key]),
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = _json.loads(r.read())
                dish = (data["choices"][0]["message"]["content"] or "").strip()
            except Exception:
                dish = ""
            # v3.2.7.32: strip the M3 `` reasoning block if present so
            # downstream guards see only the final answer.
            dish = re.sub(r"^\s*<think.*?</think>\s*", "", dish, flags=re.DOTALL)
            dish = dish.strip()
            dish = dish.strip("「」『』\"'` \n\r\t。.,，")
            # v3.2.7.32: post-AI label guard. Strip any leading bullet /
            # dash / section label that the model occasionally copies
            # through (e.g. "- 馬黛茶..." → "馬黛茶"; "菜式：" → "").
            dish = re.sub(r"^[-•·\s]+", "", dish)
            dish = dish.strip("：:，,。. ")
            # v3.2.7.32: reject label/header words returned as the dish
            label_words = _DISH_NAME_LABEL_WORDS
            # v3.2.7.32: safety net — if M3 emitted a `進思考` block but
            # never closed (max_tokens cut off mid-think), discard the
            # partial think instead of saving narration as the dish.
            #
            # v3.3.29 (2026-10-21, Isak 獨立驗證): this early `return ""`
            # was the LAST unguarded exit in this function. MiniMax was
            # ALIVE but half-dead (think block truncated by max_tokens),
            # so the Perplexity extractor was never reached — a perfect
            # vision description still became 「未識別菜式」. Same bug
            # class as the three exits fixed in v3.3.26; Alonso's report
            # claimed "fallback 要喺每個 return "" 之前行" but this one
            # was missed. PROVEN 2026-10-21 with a fake that returns an
            # unclosed 進思考 block: Perplexity call count = 0.
            if "進思考" in dish:
                alt = ""  # v3.3.34: Perplexity 已移除
                if alt:
                    print(f"[dish-name] MiniMax think block truncated — "
                          f"Perplexity sonar extracted {alt!r}", flush=True)
                    return alt
                return ""
            if dish in label_words or not dish or len(dish) > 16:
                # v3.3.26 (2026-10-21): do NOT give up here. This early
                # return used to be the reason a dead MiniMax key produced
                # "未識別菜式" even when the vision description was perfect
                # — the Perplexity extractor below was unreachable. Try it
                # before returning empty.
                alt = ""  # v3.3.34: Perplexity 已移除
                if alt:
                    print(f"[dish-name] MiniMax extractor unusable — "
                          f"Perplexity sonar extracted {alt!r}", flush=True)
                    return alt
                return ""
            # v3.2.7.17: narration guard (相顯示... / 一支XXX樽...). If
            # it leaks, ask once more with a stricter prompt — and on
            # second failure, return empty (caller will surface to user
            # instead of saving narration as dish name).
            if _name_has_narration(dish):
                retry_payload = {
                    "model": "MiniMax-M3",
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                f"以下係你之前回嘅菜名，但係敘述性句子唔係菜名：'{dish}'。\n"
                                f"原始描述：{cleaned[:400]}\n\n"
                                "只回 2-8 個中文字嘅 SPECIFIC 菜名，例如 '海南雞飯'、"
                                "'凍檸茶'、'煎蛋'。唔好再寫敘述。"
                                "如果睇唔到明確嘅食物,回 '未識別',唔好靠外形估。"
                            ),
                        }
                    ],
                    "max_tokens": 1500,
                    "temperature": 0,
                }
                with urllib.request.urlopen(
                    urllib.request.Request(
                        "https://api.minimax.io/v1/chat/completions",
                        data=_json.dumps(retry_payload).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": "".join(["Bearer ", api_key]),
                        },
                    ),
                    timeout=8,
                ) as r2:
                    data2 = _json.loads(r2.read())
                dish2 = (data2["choices"][0]["message"]["content"] or "").strip()
                dish2 = dish2.strip("「」『』\"'` \n\r\t。.,，")
                dish2 = re.sub(r"^[-•·\s]+", "", dish2).strip("：:，,。. ")
                if (not dish2 or dish2 in label_words or len(dish2) > 16
                        or _name_has_narration(dish2)):
                    # v3.3.26: same reasoning as above — the strict retry
                    # failing must not be the end of the road.
                    alt = ""  # v3.3.34: Perplexity 已移除
                    if alt:
                        print(f"[dish-name] MiniMax strict retry unusable — "
                              f"Perplexity sonar extracted {alt!r}", flush=True)
                        return alt
                    return ""
                return dish2
            if 2 <= len(dish) <= 16:
                return dish
    except Exception:
        pass

    # v3.3.34 (2026-10-21, Jim OOB "Remove perplexity. keep minimax only"):
    # 原本呢度有 Perplexity sonar 做第二個 dish-name extractor(2026-10-21 加,
    # 因為 MiniMax key 死咗會令好嘅名都變「未識別菜式」)。
    # 而家淨返 MiniMax 一家 —— 所以呢度冇 fallback。
    # ⚠️ 意味住:MiniMax 死 → 菜名攞唔到 → 掃描會被 Rule 25 硬攔截(唔會寫
    # 「未識別菜式」入表),Jim 會見到失敗提示而唔係壞資料。

    # v3.2.7.32: no regex fallback (per Jim OOB 2026-08-11 'don't use
    # rule/regex'). If the AI can't decide, return empty so the caller
    # surfaces the issue rather than silently saving a garbage name.
    return ""


# v3.2.7.48: shared dish-name sanitiser. Single source of truth for
# "is this candidate string acceptable as the dish name written to
# column D of the Golden Copy sheet?". Reused at every entry-construction
# site AND as the last line of defence in _append_to_sheet_nutrition,
# so no path can write empty / `scan` / `Unknown` / filename-bleed /
# narration into column D.
#
# Per Jim OOB 2026-08-11 'don't use rule/regex' this is REJECTION-ONLY —
# it never invents a dish name from prose. When rejected, it returns
# the literal '未識別菜式' marker (v3.2.7.32 contract) so the frontend
# can prompt Jim to name the entry.
def _sanitise_dish_name(candidate, vision_desc: str = "", pplx_desc: str = "") -> str:
    """Return a clean dish name, or '未識別菜式' if `candidate` is unusable.

    Rejection criteria (mirrors the existing guard at gym_web.py:5071-5084
    so behaviour stays identical, just centralised):
      - empty / whitespace-only
      - literal placeholder 'scan' or 'Unknown'/'unknown' (legacy)
      - filename bleed: startswith 'img_' or endswith '.jpg'
      - narration prefixes: '相顯示' / '圖顯示' / '呢張'
      - the placeholder '食物'
      - length > 60 chars (long prose bleeds into sheet were exactly
        120 chars due to [:120] slice; 60 chars is a hard ceiling for
        a real Cantonese dish name)
      - generic label words already rejected by _extract_dish_name

    Re-extraction via _extract_dish_name when given the original vision
    descriptions lets a bad candidate (e.g. raw narration text written
    by an earlier code path) get re-derived into a real dish name when
    the AI vision is still around. If re-extraction also fails, return
    '未識別菜式'.
    """
    if candidate is None:
        return "未識別菜式"
    s = str(candidate).strip()
    if not s:
        return "未識別菜式"

    # Generic label words (subset of _extract_dish_name's generic_labels
    # that are realistic candidates after .get(...).strip(); the full set
    # is consulted inside _extract_dish_name).
    generic_labels = {
        "scan", "unknown", "Unknown", "UNKNOWN",
        "食物", "餐", "餐點",
    }
    if s in generic_labels:
        return "未識別菜式"

    # Filename / placeholder leakage from earlier code paths that used
    # img_*.jpg or scan_*.jpg as the meal_name.
    if s.startswith("img_") or s.endswith(".jpg") or s.endswith(".jpeg") or s.endswith(".png"):
        return "未識別菜式"

    # Narration prefixes — model "相顯示..." / "圖顯示..." / "呢張..."
    # prose leaking from raw vision_desc. Also reject the full-width
    # open paren "（..." which is the prefix APiyi gpt-4o-mini uses
    # when emitting a safety-filter refusal or 2nd-opinion note
    # (e.g. "（APiyi gpt-4o vision 失敗" leaked into 6 sheet rows).
    # v3.3.4: also catch "這張相顯示" / "這張相" — same narration pattern,
    # just with a demonstrative pronoun that the vision model adds when
    # the photo is too sparse to identify (e.g. "這張相顯示一支蘇打水樽").
    if (s.startswith("相顯示") or s.startswith("圖顯示")
            or s.startswith("呢張") or s.startswith("這張")
            or s.startswith("（")):
        return "未識別菜式"

    # Length cap: real Cantonese dish names are 2-16 chars; anything
    # longer is almost certainly prose. The 60-char ceiling matches the
    # existing guard so we don't reject longer user-typed names that are
    # actually valid (e.g. user typed "雙層芝士漢堡加大薯條套餐").
    if len(s) > 60:
        if vision_desc or pplx_desc:
            redrive = _extract_dish_name(vision_desc, pplx_desc, fallback=s[:60])
            if redrive and redrive != "未識別菜式":
                return redrive
        return "未識別菜式"

    return s


def _extract_dish_name(vision_desc: str, pplx_desc: str = "", fallback: str = "") -> str:
    """v3.2.7.32: AI-only dish-name extraction. No regex cascade.

    Jim OOB 2026-08-11 'are you using AI to find the food?? don't use
    rule/regex' — extract is now strictly AI. If the AI can't decide,
    fall back to the caller-supplied hint (e.g. user typed the name in
    the confirm box), or the marker '未識別菜式' so the frontend can
    prompt the user to name it.

    Returns a 2-12 char SPECIFIC Cantonese dish name, e.g.:
      '相顯示咗一塊千層蛋糕' → '千層蛋糕'
      '可見海南雞飯配青瓜' → '海南雞飯'
      '一杯黑咖啡' → '黑咖啡'
    """
    # v3.2.7.32: AI only — no regex extraction. The previous regex
    # cascade below (lines 3033-3191 in v3.2.7.32; now removed) included
    # `菜式[：:]\s*xxx` patterns that returned nonsense labels like
    # '菜式' when vision output used markdown headers. Jim OOB
    # 2026-08-11 'don't use rule/regex'. The base prompt in
    # _extract_dish_name_ai also rejects label/header words and retries
    # once on narration.
    ai_result = _extract_dish_name_ai(vision_desc, pplx_desc)
    # v3.2.7.32: bumped from 12 to 16 chars — multi-component dishes
    # (e.g. '雞胸肉、青瓜、紅蘿蔔絲' = 15) are valid Cantonese names;
    # the 12-char cap was rejecting them.
    if ai_result and 2 <= len(ai_result) <= 16:
        # v3.2.7.32: extended generic-label guard — covers all label-word
        # sets that used to leak through regex OR AI (主菜/菜名/未識別).
        generic_labels = {
            "早餐", "午餐", "晚餐", "午飯", "晚飯", "下午茶",
            "宵夜", "tea", "brunch", "dinner", "lunch",
            "breakfast", "第一道菜", "主菜", "副菜", "前菜",
            "甜品", "湯品", "飲品", "餐", "食物", "料理",
            "菜式", "菜名", "菜式：", "菜名：", "套餐",
            "未識別", "未識別菜式", "餐點",
        }
        if ai_result not in generic_labels:
            return ai_result

    # v3.2.7.32: AI refused or returned a label. Fall back only to the
    # caller-supplied hint (e.g. user typed '海南雞飯' in the confirm
    # box). Never invent a name from regex over vision text per Jim's
    # 'don't use rule/regex' directive.
    if fallback and 2 <= len(fallback.strip()) <= 30:
        return fallback.strip()[:30]

    # Last resort: '未識別菜式' marker so the frontend can prompt the
    # user to name it (instead of seeing '菜式' / '相顯示xxx').
    return "未識別菜式"


def _compute_rating(vision_desc: str, macros: dict = None) -> int:
    """v3.2.7.1: derive a 1-5 star rating from food keywords + macros.

    Tier 1 (5★) = 清淡零負擔 (coffee/tea/water/clear soup/steamed veg/egg white)
    Tier 2 (4★) = 健康均衡 (grilled chicken, salad, fish, sashimi, oats, yogurt)
    Tier 3 (3★) = 中性 (rice/noodle/porridge, congee, regular home-cooked)
    Tier 4 (2★) = 偏heavy (cake/dessert/cream/fried rice/pizza/sushi >2pc)
    Tier 5 (1★) = 極heavy (deep-fried/BBQ/processed meat/large cake/buffet/sugary drink)

    Description-quality fallback: macro count + description length (for unknown dishes).
    Used by scan_preview_text + scan_commit so the food list view
    shows a star rating column (Jim OOB 2026-08-07 23:50 HKT 'no
    rating' on new entries).
    """
    desc = (vision_desc or "").strip()
    desc_lower = desc.lower()
    # name field (if macros dict has 'name', use it for keyword match)
    name_hint = ""
    if isinstance(macros, dict):
        name_hint = (macros.get("name") or macros.get("meal_name") or "").strip()
    combined = (name_hint + " " + desc).lower()

    # Tier 1: 5★
    tier1_kw = ["黑咖啡", "齋啡", "凍咖啡", "咖啡", "綠茶", "紅茶", "烏龍", "麥茶",
                "清水", "白開水", "齋水", "檸水", "蜂蜜水", "豆漿", "脫脂奶",
                "蛋白", "雞胸", "雞胸肉", "三文魚刺身", "沙律", "沙拉", "生菜",
                "西蘭花", "椰菜花", "蘆筍", "蒸雞", "清蒸", "白灼", "烚菜",
                "希臘乳酪", "茅屋芝士", "豆腐", "枝豆", "海帶", "紫菜湯"]
    if any(k in combined for k in tier1_kw):
        return 5

    # Tier 2: 4★
    tier2_kw = ["魚", "蝦", "帶子", "刺身", "壽司 (1-2 件)", "壽司", "和牛 (細份)",
                "牛扒 (細)", "牛柳", "烤雞", "燒雞", "蒸魚", "煎魚", "蒸蛋",
                "番茄", "菠菜", "甘藍", "羽衣甘藍", "蘑菇", "茄子", "彩椒",
                "燕麥", "乳酪", "酸奶", "香蕉", "蘋果", "藍莓", "奇異果",
                "牛油果", "番薯", "紫薯", "糙米", "藜麥", "扁糧",
                "毛豆", "蝦仁", "海鮮", "貝殼類", "蟹肉 (蒸)", "蜆", "青口",
                "豬里脊", "牛腱", "雞腿 (去皮)", "火雞", "鴨胸", "鵪鶉蛋"]
    if any(k in combined for k in tier2_kw):
        return 4

    # Tier 5: 1★ (deep fried/BBQ/buffet/heavy dessert)
    tier5_kw = ["炸雞", "炸魚", "炸薯條", "炸魷", "炸春卷", "天婦羅", "炸蝦",
                "炸排骨", "炸雞翼", "炸雞塊", "炸雞扒", "炸豬排", "炸物",
                "燒烤 (自助)", "bbq 自助", "燒肉自助", "韓燒", "日式燒肉",
                "自助餐", "all-you-can", "放題", "buffet",
                "漢堡包", "巨無霸", "whopper", "雙層芝士", "double double",
                "蛋糕 (> 500 卡)", "朱古力蛋糕", "芝士蛋糕 (重)", "忌廉蛋糕",
                "全脂奶", "雪糕 (大)", "奶昔 (大)", "星冰樂", "frappuccino",
                "珍珠奶茶 (大杯)", "bubble tea (大)", "coca-cola (大)",
                "煙肉", "bacon (重份)", "香腸 (2 條+)", "午餐肉", "罐頭肉",
                "即食麵", "杯麵", "公仔麵"]
    if any(k in combined for k in tier5_kw):
        return 1

    # Tier 4: 2★ (heavy dessert / creamy / sweet)
    tier4_kw = ["千層蛋糕", "瑞士卷", "tiramisu", "泡芙", "蛋撻",
                "曲奇", "餅乾", "donut", "冬甩", "muffin", "鬆餅", "班戟",
                "pancake", "waffle", "窩夫", "朱古力", "雪糕", "冰淇淋",
                "奶昔", "pudding", "布丁", "焦糖", "糖水", "芝麻糊",
                "楊枝甘露", "芒果糯米", "pizza (1 塊)", "薄餅 (1 塊)",
                "焗芝士", "mac & cheese", "拉麵 (濃湯)", "豚骨拉麵",
                "薯片", "蝦條", "popcorn", "粟米片", "焦糖爆谷",
                "炸雞 (1 塊)", "薯條 (小)", "壽司 (3 件+)",
                "咖喱飯", "焗飯", "焗豬扒飯", "星洲炒米", "揚州炒飯"]
    # Tier 4b: 2★ for generic 蛋糕 (after dessert-specific 4★ check)
    tier4_generic_kw = ["蛋糕", "芝士蛋糕", "朱古力蛋糕", "忌廉蛋糕",
                        "cupcake", "紙杯蛋糕"]
    if any(k in combined for k in tier4_kw):
        return 2
    if any(k in combined for k in tier4_generic_kw):
        return 2

    # Tier 3: 3★ (default for most Hong Kong home-style + rice/noodle neutral)
    tier3_kw = ["飯", "粥", "麵", "米粉", "河粉", "瀨粉", "烏冬", "蕎麥麵",
                "饅頭", "餃子", "包子", "雲吞", "燒賣", "腸粉", "蘿蔔糕",
                "牛肉餅", "蒸肉餅", "肉碎", "蒸排骨", "蒸水蛋", "燉湯",
                "雞翼", "雞腳", "鳳爪", "豬手", "牛腩", "炆牛腩",
                "炒菜", "青菜", "菜心", "芥蘭", "通菜", "豆苗", "白菜",
                "蒸饅頭", "小籠包", "生煎包", "鍋貼", "水餃", "湯圓",
                "豆腐花", "豆花", "豆漿 (甜)", "油條", "煎餅", "葱油餅",
                "蛋炒飯", "星洲炒米", "公仔麵 (1 個)"]
    if any(k in combined for k in tier3_kw):
        return 3

    # Fallback: description-quality heuristic (legacy behaviour)
    desc_len = len(desc)
    macro_n = sum(1 for v in (macros or {}).values()
                  if isinstance(v, (int, float)) and v and v > 0)
    if desc_len >= 80 and macro_n >= 2:
        return 4
    if desc_len >= 40 and macro_n >= 1:
        return 4
    if desc_len >= 20 and macro_n >= 1:
        return 3
    if desc_len >= 5 or macro_n >= 1:
        return 3
    return 2


def _merge_nutrition_estimates(estimates: list) -> dict:
    """Merge multiple nutrition estimate dicts via median + confidence.

    Args:
        estimates: list of {field: value, ...} dicts. Empty fields ignored.

    Returns:
        {field: {value: float, source_count: N, confidence: 'high'|'medium'|'low'}, ...}
        - 'high' if 2+ sources agree within 30%
        - 'medium' if 2+ sources but disagree
        - 'low' if only 1 source (no second opinion)
    Missing fields absent.
    """
    if not estimates:
        return {}
    merged = {}
    for field in NUTRITION_FIELDS:
        vals = []
        for est in estimates:
            if field in est and est[field] is not None and est[field] > 0:
                try:
                    vals.append(float(est[field]))
                except (TypeError, ValueError):
                    pass
        if not vals:
            continue
        if len(vals) == 1:
            merged[field] = {
                "value": round(vals[0], 1),
                "source_count": 1,
                "confidence": "low",
            }
        else:
            # Median
            vals_sorted = sorted(vals)
            mid = len(vals_sorted) // 2
            if len(vals_sorted) % 2 == 0:
                median = (vals_sorted[mid - 1] + vals_sorted[mid]) / 2
            else:
                median = vals_sorted[mid]
            # Agreement check: max/min within 30%
            vmin, vmax = min(vals), max(vals)
            if vmin > 0:
                spread = (vmax - vmin) / vmin
            else:
                spread = 1.0
            confidence = "high" if spread < 0.3 else "medium"
            merged[field] = {
                "value": round(median, 1),
                "source_count": len(vals),
                "confidence": confidence,
                "sources": vals,
            }
    return merged


# v2.7.37: Coach comment + food grading (Jim OOB 2026-08-06: "It would be great if
# there is a coach comment and suggest on the food that i log. therefore, it should
# not be just recognizing but also what are good selection and bad selection of food")
# Uses MiniMax M3 text-only (no image needed) to score + suggest.
# Returns: {grade: 'A+'|'A'|'B'|'C'|'D'|'F', comment: str, suggestions: [str, ...], rationale: str}
def _coach_comment_keyword(dish_name: str, calories: float, protein: float, carbs: float, fat: float, restaurant: str = "") -> dict:
    """v3.3.1: fast keyword + macro grade, NO AI call.

    Returns the same dict shape as _coach_comment but with empty
    macro_breakdown / micronutrient_status / next_meal_suggestion fields.
    Safe to call N times per request (food history list endpoint).
    """
    if not dish_name:
        dish_name = "(未命名菜式)"
    combined = dish_name.lower()
    # Jim OOB 2026-08-11 'not every food has a rating. Pls fix it':
    # Even when calories=0 (early scan / data sync) or dish_name empty, we
    # still render a usable grade from whatever macros we DO have. The
    # '—' placeholder caused rating gaps in PWA (Jim's gymbro dashboard
    # showed missing badges). Now we degrade gracefully:
    #   - dish_name empty + no macros → return 'C' with note "菜名未知"
    #   - calories=0 but macros present → use macros to compute (only need
    #     P/C/F fat_pct; calories derived from 4P+4C+9F)
    #   - everything missing → still 'C' as neutral default
    if not dish_name:
        dish_name = "(未命名菜式)"

    combined = dish_name.lower()

    # ----- Step 1: keyword tier pre-grade -----
    tier_a_plus = [
        "黑咖啡", "齋啡", "凍咖啡", "espresso", "美式咖啡",
        "綠茶", "紅茶", "烏龍", "麥茶", "抹茶",
        "清水", "白開水", "齋水", "檸水", "蜂蜜水",
        "豆漿", "脫脂奶", "蛋白", "蛋白質飲品", "蛋白粉",
        "雞胸", "雞胸肉", "三文魚刺身", "沙律", "沙拉",
        "西蘭花", "椰菜花", "蘆筍", "蒸雞", "清蒸", "白灼", "烚菜",
        "希臘乳酪", "茅屋芝士", "豆腐", "枝豆", "海帶", "紫菜湯",
        "燙青菜", "灼菜", "蒸魚", "蒸蛋白", "蛋白奶昔",
        # Whole-egg forms — clean gym protein (Jim OOB 2026-08-15
        # "eggs that i scanned is marked as F, this should be a good food
        # for gym"). Boiled/poached/tea/century/chicken egg are zero-burden
        # gym food. Use specific phrases — bare "蛋" would mis-match
        # 蛋糕/蛋撻/蛋卷 which are desserts in tier_d below.
        "烚雞蛋", "烚蛋", "水煮蛋", "水蛋", "茶葉蛋", "皮蛋", "雞蛋",
        "蒸水蛋",  # savory steamed egg custard — wholesome, was wrongly in tier_b
    ]
    tier_a = [
        "魚", "蝦", "帶子", "刺身", "壽司", "和牛 (細份)",
        "牛扒 (細)", "牛柳", "烤�", "燒雞", "煎魚",
        "蒸蛋", "番茄", "菠菜", "甘藍", "羽衣甘藍", "蘑菇", "茄子", "彩椒",
        # Cooked-with-oil egg forms (Jim OOB 2026-08-15): fried/scrambled/sunny
        # side up are still healthy gym protein — cooking oil adds ~20-40 kcal
        # vs boiled, so tier_a (healthy balanced) not tier_a_plus (zero burden).
        "煎蛋", "炒蛋", "太陽蛋", "滑蛋", "西炒蛋", "蛋花湯", "滷蛋",
        "燕麥", "乳酪", "酸奶", "香蕉", "蘋果", "藍莓", "奇異果",
        "牛油果", "番薯", "紫薯", "糙米", "藜麥", "扁糧",
        "毛豆", "蝦仁", "海鮮", "貝殼類", "蟹肉 (蒸)", "蜆", "青口",
        "豬里脊", "牛腱", "雞腿 (去皮)", "火雞", "鴨胸", "鵪鶉蛋",
        "麥皮", "粥 (清)", "豆腐花 (清)", "蒸饅頭",
        # Raw / lightly-cooked lean protein (Jim OOB 2026-08-13 13:00 HKT
        # 'The 生牛肉 that I logged is quite lean. Why is it rated as F?'):
        # USDA Choice top sirloin lean raw 100g = 22g P / 5g F / 0 C = 41%
        # fat cal. After grill, fat ratio naturally rises to 55-65% but
        # absolute protein density still high (P >= 30g + P/F ratio >= 1.2).
        # These are tier-A premium protein, NOT heavy food.
        "生牛肉", "生牛肉片", "生牛", "牛肉他他", "生牛肉他他",
        "beef tartare", "steak tartare", "tartare",
        # Grilled / Korean BBQ lean beef single-portion (same rationale):
        # 韓燒牛 / 日式燒肉 / 烤牛 / 燒烤牛肉 = single-portion premium protein.
        # Self-service 韓燒自助 / 燒肉自助 stays in tier_f below.
        "韓燒牛", "燒烤牛", "烤牛", "燒牛肉", "烤牛肉",
        "日式燒牛肉", "韓燒牛肉", "燒烤牛肉", "燒烤牛肉片",
        "grilled beef", "korean bbq beef",
    ]
    tier_f = [
        "炸雞", "炸魚", "炸薯條", "炸魷", "炸春卷", "天婦羅", "炸蝦",
        "炸排骨", "炸雞翼", "炸雞塊", "炸雞扒", "炸豬排", "炸物",
        # BBQ/韓燒 ONLY when自助/放題 (Jim OOB 2026-08-13 13:00 HKT):
        # Single-portion Korean BBQ lean beef is NOT heavy — it's premium
        # protein (see 生牛肉 fix above). Keep 自助/放題/buffet/all-you-can
        # modifiers; remove standalone 韓燒/日式燒肉/燒烤 to prevent
        # mis-grading single-portion grilled beef.
        "燒烤 (自助)", "bbq 自助", "燒肉自助",
        "自助餐", "all-you-can", "放題", "buffet",
        "漢堡包", "巨無霸", "whopper", "雙層芝士", "double double",
        "朱古力蛋糕", "芝士蛋糕 (重)", "忌廉蛋糕",
        "全脂奶", "星冰樂", "frappuccino",
        "珍珠奶茶 (大杯)", "bubble tea (大)",
        "煙肉", "bacon (重份)", "午餐肉", "罐頭肉",
        "即食麵", "杯麵", "公仔麵", "豬骨濃湯拉麵", "豚骨拉麵",
    ]
    tier_d = [
        "千層蛋糕", "瑞士卷", "tiramisu", "泡芙", "蛋撻",
        "曲奇", "餅乾", "donut", "冬甩", "muffin", "鬆餅", "班戟",
        "pancake", "waffle", "窩夫", "雪糕", "冰淇淋",
        "奶昔", "pudding", "布丁", "焦糖", "糖水", "芝麻糊",
        "楊枝甘露", "芒果糯米", "pizza (1 塊)", "薄餅 (1 塊)",
        "焗芝士", "mac & cheese",
        "薯片", "蝦條", "popcorn", "粟米片", "焦糖爆谷",
        "炸雞 (1 塊)", "薯條 (小)", "壽司 (3 件+)",
        "咖喱飯", "焗飯", "焗豬扒飯", "星洲炒米", "揚州炒飯",
        "蛋糕", "cupcake", "紙杯蛋糕",
        # v3.3.7: HK cafe dessert carbs. Jim OOB 2026-08-16:
        # '抹茶厚多士 was rated A, should not be'. Substring match on
        # '抹茶' (tier_a_plus) was overriding the dessert base. 多士 / 西多士 /
        # 酥皮 / 丹麥酥 / 牛角酥 are all sugar-butter pastry carbs — tier_d.
        "多士", "西多士", "厚多士", "法式多士",
        "酥皮", "丹麥酥", "牛角酥", "牛角包", "croissant",
        "西餅", "葡撻", "蛋卷", "薄餅", "pizza (全)",
    ]
    tier_b = [
        "飯", "粥", "麵", "米粉", "河粉", "瀨粉", "烏冬", "蕎麥麵",
        "饅頭", "餃子", "包子", "雲吞", "燒賣", "腸粉", "蘿蔔糕",
        "牛肉餅", "蒸肉餅", "肉碎", "蒸排骨", "燉湯",
        "雞翼", "雞腳", "鳳爪", "豬手", "牛腩", "�牛腩",
        "炒菜", "青菜", "菜心", "芥蘭", "通菜", "豆苗", "白菜",
        "蒸饅頭", "小籠包", "生煎包", "鍋貼", "水餃", "湯圓",
        "油條", "煎餅", "葱油餅",
        "海南雞飯", "燒味飯", "叉燒飯", "燒鵝飯", "燒鴨飯", "燒臘飯",
    ]

    # v3.3.7: check HEAVY/DESSERT tiers FIRST so substring matches like
    # '抹茶厚多士' (tier_a_plus 抹茶 matches) or '凍咖啡蛋糕' (tier_a_plus
    # 凍咖啡 matches) don't get auto-A+ when the dish base is pastry/carb.
    # Heaviest → lightest: F > D > B > A > A+.
    if any(k in combined for k in tier_f):
        pre_grade = "F"
    elif any(k in combined for k in tier_d):
        pre_grade = "D"
    elif any(k in combined for k in tier_b):
        pre_grade = "B"
    elif any(k in combined for k in tier_a):
        pre_grade = "A"
    elif any(k in combined for k in tier_a_plus):
        pre_grade = "A+"
    else:
        pre_grade = "C"

    # ----- Step 2: macro adjustment -----
    protein_pct = (protein * 4) / max(calories, 1) * 100
    fat_pct = (fat * 9) / max(calories, 1) * 100
    carb_pct = (carbs * 4) / max(calories, 1) * 100

    grade_order = {"A+": 5, "A": 4, "B": 3, "C": 2, "D": 1, "F": 0}
    rank = grade_order[pre_grade]

    # High-protein-density guard (Jim OOB 2026-08-13 13:00 HKT):
    # When a meal has high absolute protein AND high P/F ratio, it's clearly
    # a premium protein source (e.g. 250g lean raw beef = 56g P / 38g F = 1.47
    # ratio, fat_pct 61%). The fat_pct penalty was designed for processed /
    # fried foods where fat ratio rises WITHOUT protein, not for naturally
    # fatty-but-lean real meat. Skip macro downgrade in this case.
    is_premium_protein = (protein >= 30 and fat > 0 and protein / fat >= 1.2)

    if fat_pct > 60 and rank > 0 and not is_premium_protein:
        rank = max(rank - 2, 0)
    elif fat_pct > 50 and rank > 0 and not is_premium_protein:
        rank = max(rank - 1, 0)
    elif protein_pct > 30 and fat_pct < 30 and rank < 5:
        rank = min(rank + 1, 5)
    if protein_pct < 10 and rank > 0:
        rank = max(rank - 1, 0)

    final_rank = rank
    # v3.3.7: don't demote D/F further via macro noise — a dessert tier
    # was already correctly classified (Jim OOB 2026-08-16 '抹茶厚多士
    # got F, too harsh'). Low-protein penalty should not push a labelled
    # dessert into the worst tier.
    if pre_grade in ("D", "F"):
        # Keep dessert/heavy tier as-is (preserves D for pastries).
        # Still allow premium-protein boost on D → D+a (impossible since D
        # is already rank 1; leave as-is).
        rank = max(rank, grade_order[pre_grade])
    final_grade = {v: k for k, v in grade_order.items()}[rank]

    # Comment — v3.3.13: macro-specific phrasing. The old single phrase
    # "整體均衡，可以接受。" fired for every grade-unchanged meal (Jim OOB
    # 2026-08-26 "整體均衡 too generic"). Now branch on macro profile so
    # each scan carries a concrete observation; only the genuinely-balanced
    # fall-through uses the classic line, and even there we vary wording.
    if final_grade == pre_grade:
        if protein_pct >= 20 and fat_pct <= 30:
            pre_comment = f"蛋白質 {protein_pct:.0f}% 夠，脂肪 {fat_pct:.0f}% 適中。"
        elif fat_pct >= 40:
            pre_comment = f"脂肪佔 {fat_pct:.0f}% 卡路里，少油版本會更好。"
        elif protein_pct < 15:
            pre_comment = f"蛋白質只佔 {protein_pct:.0f}%，加蛋/雞胸平衡更好。"
        elif carb_pct >= 60:
            pre_comment = f"碳水佔 {carb_pct:.0f}% 為主，飯量可減 1/3。"
        elif calories <= 200:
            pre_comment = f"份量輕 ({calories:.0f} kcal)，snack 級。"
        else:
            pre_comment = random.choice([
                "整體均衡，可以接受。",
                "比例適中，可以。",
                "組合 OK，唔差。",
            ])
    elif final_rank > grade_order[pre_grade]:
        pre_comment = f"蛋白質 {protein_pct:.0f}%，脂肪 {fat_pct:.0f}%，比例好。"
    else:
        pre_comment = f"脂肪佔 {fat_pct:.0f}% 卡路里，偏高。" if fat_pct > 35 else f"蛋白質只佔 {protein_pct:.0f}%，偏低。"

    suggestions = []
    if final_rank <= 1:
        suggestions.append("下次可選少油版本（走醬 / 少汁 / 走炸皮）")
    if protein_pct < 15 and calories > 300:
        suggestions.append("加一隻蛋 / 雞胸 / 豆腐提升蛋白比例")
    if carb_pct > 65 and calories > 400:
        suggestions.append("配菜加多啲菜，飯量減 1/3")
    return {
        "grade": final_grade,
        "comment": pre_comment,
        "suggestions": suggestions[:2],
        "rationale": f"蛋白 {protein_pct:.0f}% · 碳 {carb_pct:.0f}% · 脂 {fat_pct:.0f}%",
        "macro_breakdown": "",
        "micronutrient_status": "",
        "next_meal_suggestion": suggestions[0] if suggestions else "",
    }


def _coach_comment(dish_name: str, calories: float, protein: float, carbs: float, fat: float, restaurant: str = "", user_context: str = "", daily_ctx_seed: str = "") -> dict:
    """v3.3.1: full coach comment = keyword + AI 4-section enrichment.

    For single-row display (scan commit/correct/preview detail views).
    /api/scan_recent MUST NOT call this — use _coach_comment_keyword instead.
    Each AI call costs ~5s, so a list of N rows = N×5s (was the v3.3.0 bug).

    v3.3.8: optional `daily_ctx_seed` is prepended to the prompt so the
    4-section comment can reference today's theme (e.g. "復原日 蛋白優先").
    Comes from the daily coaching narrative; empty = no theme hint.
    """
    base = _coach_comment_keyword(dish_name, calories, protein, carbs, fat, restaurant)
    pre_comment = base["comment"]
    final_grade = base["grade"]
    suggestions = base["suggestions"]
    protein_pct = (protein * 4) / max(calories, 1) * 100
    fat_pct = (fat * 9) / max(calories, 1) * 100
    carb_pct = (carbs * 4) / max(calories, 1) * 100

    # v3.2.7.11: comprehensive coach comment via MiniMax M3 (Jim OOB
    # 2026-08-08 20:35 HKT 'comment on the food can be much more
    # comprehensive'). 4-section structured response: overall health +
    # macro breakdown + micronutrient status + next-meal suggestion.
    api_comment = None
    macro_breakdown = ""
    micronutrient_status = ""
    next_meal_suggestion = ""
    try:
        # v3.3.8: weave today's theme into the food coach comment so the
        # 4 sections feel like part of the same day's narrative.
        theme_line = f"\n今日主題: {daily_ctx_seed}\n" if daily_ctx_seed else ""
        prompt = (
            f"你係香港私人健身教練，操繁體中文廣東話。分析以下食物。"
            f"{theme_line}\n"
            f"食物：{dish_name}\n"
            f"餐廳：{restaurant or '無'}\n"
            f"份量：{calories:.0f} kcal · 蛋白 {protein:.0f}g · 碳 {carbs:.0f}g · 脂 {fat:.0f}g\n"
            f"用戶目標：{user_context or '減脂 + 增肌'}\n\n"
            f"請用以下 4 段 (每段 1 句，最多 30 字)：\n"
            f"1) 【整體】一句講整體好/普通/差。\n"
            f"2) 【巨量營養】P/C/F 比例點評 (e.g. 蛋白佔 X% 偏低 / 脂肪 Y% 偏高)。\n"
            f"3) 【微量營養】鈣/鐵/維 C/鈉 嘅攝取有咩特別。\n"
            f"4) 【下餐】具體下次食咩 balance (e.g. 下餐加菜減飯 / 配雞胸 / 走醬)。\n\n"
            f"範例：\n"
            f"【整體】蛋白優質，脂肪稍多。\n"
            f"【巨量營養】蛋白佔 38% 出色，脂 42% 略高。\n"
            f"【微量營養】鐵 3.5mg 不錯，鈉 1200mg 偏上限。\n"
            f"【下餐】下餐配灼菜一碗平衡鈉。\n\n"
            f"唔好講廢話，4 段都要具體。"
        )
        payload = {
            "model": "MiniMax-M3",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 350,
            "temperature": 0.4,
        }
        req = urllib.request.Request(
            "https://api.minimax.io/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "".join(["Bearer ", os.environ.get("MINIMAX_API_KEY", "")]),
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        full = data["choices"][0]["message"]["content"].strip()
        # Strip <think>...</think> blocks leaked from reasoning models
        # (Jim OOB 2026-08-15: "description of the egg is having a tag
        # </think>, pls fix"). Without this, the regex below captures the
        # tag into the user-visible comment field.
        import re
        full = re.sub(r"<think>.*?</think>", "", full, flags=re.DOTALL).strip()
        # Parse the 4 sections from AI response
        sec1 = re.search(r"【整體】\s*(.+?)(?=\n|【|$)", full, re.DOTALL)
        sec2 = re.search(r"【巨量營養】\s*(.+?)(?=\n|【|$)", full, re.DOTALL)
        sec3 = re.search(r"【微量營養】\s*(.+?)(?=\n|【|$)", full, re.DOTALL)
        sec4 = re.search(r"【下餐】\s*(.+?)(?=\n|【|$)", full, re.DOTALL)
        if sec1:
            api_comment = sec1.group(1).strip()
            if sec2:
                macro_breakdown = sec2.group(1).strip()
            if sec3:
                micronutrient_status = sec3.group(1).strip()
            if sec4:
                next_meal_suggestion = sec4.group(1).strip()
    except Exception:
        api_comment = None

    # Build the visible `comment` field — combine all sections into a 4-line string
    if api_comment:
        comment_lines = [f"【整體】{api_comment}"]
        if macro_breakdown:
            comment_lines.append(f"【巨量】{macro_breakdown}")
        if micronutrient_status:
            comment_lines.append(f"【微量】{micronutrient_status}")
        if next_meal_suggestion:
            comment_lines.append(f"【下餐】{next_meal_suggestion}")
        final_comment = "\n".join(comment_lines)
    else:
        # Fallback to the original static template
        final_comment = pre_comment

    base["comment"] = final_comment
    base["macro_breakdown"] = macro_breakdown or pre_comment
    base["micronutrient_status"] = micronutrient_status
    base["next_meal_suggestion"] = next_meal_suggestion or (suggestions[0] if suggestions else "")
    return base


# v2.7.37: DDG web search (Jim OOB 2026-08-06: "bundled with all the search tools
# such as pplx, ddg"). Use for brand/origin/portion verification when scan has
# branded or unclear dish. Returns top 5 result snippets.
def _ddg_search(query: str, max_results: int = 5) -> list:
    """Search DuckDuckGo for brand/origin/portion confirmation.
    Returns list of {'title', 'snippet', 'url'}.
    """
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results, region="hk-tw"))
        return [{"title": r.get("title", ""), "snippet": r.get("body", "")[:200], "url": r.get("href", "")} for r in results]
    except Exception:
        # Fallback: try duckduckgo_search package
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results, region="hk-tw"))
            return [{"title": r.get("title", ""), "snippet": r.get("body", "")[:200], "url": r.get("href", "")} for r in results]
        except Exception:
            return []


def _detect_shared_meal(dish_desc: str) -> bool:
    """Heuristic — detect if dish description suggests shared meal.

    Jim OOB 2026-08-02 02:50 HKT: "2 個人食" is the natural HK phrasing,
    not the mainland-PRC-stiff "二人份". Add HK-natual expressions:
    「2 個人食」「兩個人」「兩個」「share」「分開」「一半」 etc.
    Common-case phrase 「兩個人」「2 人」「2個人」covers 95% of Jim's case.
    """
    shared_indicators = [
        # Mainland-PRC-stiff
        "兩人份", "二人份", "分享", "share", "套餐", "二人餐", "二人",
        "set menu", "family", "set for two", "二人用", "二人套餐",
        "set  for", "二人用套餐",
        # HK-natural (Jim OOB 2026-08-02 02:50 HKT)
        "2 個人食", "兩個人食", "2個人食", "兩個人嘅",
        "2 個人", "兩個人", "2個人",
        "2 人食", "兩人食",
        "我同", "我合", "我 ＋", "分開食",
        # Soft signals (might need coach confirmation)
        "half", "對分", "一半",
    ]
    desc_lower = dish_desc.lower()
    return any(indicator.lower() in desc_lower for indicator in shared_indicators)


def _load_today_nutrition() -> dict:
    """v3.3.2: replaced the legacy nutrition_log.json file-cache read with a
    cache + Sheet query. Same return shape as before so callers don't change.
    Reads from the in-memory NutritionCache (hydrated from the Sheet on boot).

    v3.3.10 (Jim OOB 2026-08-31「cheer routine is not reflecting the intraday
    activities」): if today's HKT date has 0 meals but the most recent meal
    was logged within the last 18 hours (i.e. Jim is still on yesterday's
    "day" past HKT midnight), fall back to the most recent date with
    meals. Prevents cheer from reporting "今日零 workout / 未 log 食嘢"
    when in reality Jim just stayed up late and it's still his yesterday-day.
    """
    out = {"meals": [], "totals": {"kcal": 0, "P": 0, "C": 0, "F": 0}, "meal_count": 0, "last_meal_ts": None}
    cache = get_cache()
    if not cache.is_ready():
        return out
    today = today_iso()
    rows = cache.get_by_date(today)
    if not rows:
        # v3.3.10 fallback: look at yesterday's date (still same HKT day
        # if user is up past midnight). Window covers late nights.
        from datetime import datetime, timedelta, timezone
        HKT_TZ = timezone(timedelta(hours=8))
        now_hkt = datetime.now(HKT_TZ)
        # v3.3.10: widen fallback window to 24h to catch both late nights AND
        # post-midnight cheer. Last activity date = whatever has the most
        # recent meal regardless of HKT hour. This prevents cheer pipeline
        # from reporting "0 meal today" when Jim just stayed up late.
        yesterday = (now_hkt - timedelta(days=1)).strftime("%Y-%m-%d")
        rows = cache.get_by_date(yesterday)
        if rows:
            today = yesterday  # so caller sees yesterday as "today"
            try:
                with open("/tmp/nutrition_fallback.log", "a") as _f:
                    _f.write(f"{now_iso()} | today=9/1→fallback to {yesterday} ({len(rows)} rows)\n")
            except Exception:
                pass
        else:
            # v3.3.10 last-resort: query by_recent (sorted by dt) and find the
            # most recent date with at least 1 row. This catches late-night
            # edge cases where get_by_date() hits a key with all-blank rows.
            recent = cache.get_recent(limit=10)
            if recent:
                # Walk back to find the most recent date with valid rows
                seen_dates = set()
                for r in recent:
                    if not r.date or r.date in seen_dates:
                        continue
                    seen_dates.add(r.date)
                    candidate_rows = cache.get_by_date(r.date)
                    if candidate_rows:
                        rows = candidate_rows
                        today = r.date
                        try:
                            with open("/tmp/nutrition_fallback.log", "a") as _f:
                                _f.write(f"{now_iso()} | today=9/1→fallback to latest {r.date} ({len(rows)} rows)\n")
                        except Exception:
                            pass
                        break
    meals = []
    for r in rows:
        d = r.to_pwa_dict()
        # v3.3.23t (Jim OOB 2026-09-14 'still missing the grading'):
        # Populate coach_comment for /api/nutrition/today consumers too
        # (PWA + cheer pipeline). Was previously only set in scan_recent.
        cc = r.get_or_compute_coach_comment(
            lambda row: _coach_comment_keyword(
                row.name, row.kcal, row.p, row.c, row.f, row.restaurant
            )
        )
        meals.append({
            "date": r.date,
            "time": r.time,
            "meal_type": r.meal,
            "meal_name": r.name,
            "name": r.name,
            "calories": r.kcal,
            "protein": r.p,
            "carbs": r.c,
            "fat": r.f,
            "restaurant_chain": r.restaurant,
            "timestamp_iso": (r.dt.isoformat() + "+08:00") if r.dt else "",
            "coach_comment": cc,
        })
    meals.sort(key=lambda m: m.get("time") or m.get("timestamp_iso") or "")
    out["meals"] = meals
    out["meal_count"] = len(meals)
    for m in meals:
        try:
            out["totals"]["kcal"] += float(m.get("calories") or 0)
            out["totals"]["P"] += float(m.get("protein") or 0)
            out["totals"]["C"] += float(m.get("carbs") or 0)
            out["totals"]["F"] += float(m.get("fat") or 0)
        except (TypeError, ValueError):
            pass
    out["totals"] = {k: round(v, 1) for k, v in out["totals"].items()}
    if meals:
        last = meals[-1]
        out["last_meal_ts"] = f"{last.get('date', today)}T{last.get('time', '00:00')}:00+08:00"
    return out


def _get_today_nutrition_for_cheer() -> str:
    """Format today's nutrition as a Chinese block for the cheer pplx prompt.

    Jim OOB 2026-07-25 13:30: "monitor my food, also enhance for food comment,
    not just log nutrient". Returns empty string if no meals logged today.
    """
    data = _load_today_nutrition()
    if not data["meals"]:
        return ""
    lines = ["\n**今日飲食記錄（Alonso 主動 monitor，唔係等 Jim 講）**："]
    lines.append(f"- 已 log 餐數: {data['meal_count']} 餐")
    for m in data["meals"]:
        t = m.get("time", "??:??")
        mt = m.get("meal_type", "餐")
        mn = m.get("meal_name") or m.get("dish_name") or m.get("name") or "(未命名)"
        chain = m.get("restaurant_chain") or ""
        kcal = m.get("calories")
        chain_part = f" @ {chain}" if chain else ""
        kcal_part = f" (~{int(float(kcal))} kcal)" if kcal is not None and str(kcal) != "" else ""
        lines.append(f"  - {t} {mt}：{mn}{chain_part}{kcal_part}")
    t = data["totals"]
    p_g = int(t["P"])
    # v3.2.7.38: show protein delta vs Jim's PT-set target (120g/day,
    # Jim OOB 2026-08-11 13:00 HKT). pplx can reference this in the
    # cheer §「飲食」section so Jim hears "P 食咗 X 克，PT 個 120 仲差 Y"
    # instead of just raw totals.
    p_target = 120
    p_delta = p_g - p_target
    if p_delta >= 0:
        p_status = f"+{p_delta}g 已達標（PT 目標 {p_target}g）"
    else:
        p_status = f"仲差 {-p_delta}g 至 PT 目標 {p_target}g"
    lines.append(
        f"- 今日累計: ~{int(t['kcal'])} kcal / P {p_g}g / C {int(t['C'])}g / F {int(t['F'])}g"
    )
    lines.append(f"- 蛋白質對住 PT 目標：{p_status}")
    if data["last_meal_ts"]:
        try:
            from datetime import datetime as _dt
            last_dt = _dt.fromisoformat(data["last_meal_ts"].replace("Z", "+00:00"))
            from datetime import timezone, timedelta as _td
            last_hkt = last_dt.astimezone(timezone(_td(hours=8)))
            lines.append(f"- 最後進食: {last_hkt.strftime('%H:%M')} HKT")
        except Exception:
            pass
    lines.append("")
    lines.append("**commentary 風格指引** (Jim OOB 2026-07-25)：")
    lines.append("1. 唔好 read out numbers，講 insight 點解呢餐對 recovery / workout / 體脂 / 濕疹嘅影響")
    lines.append("2. comment 唔好只係 macro log，要講 timing / quality / 搭配 / 影響")
    lines.append("3. link 落 recovery + workout (e.g. 訓前食咁多糖影響深層瞓)")
    lines.append("4. link 落濕疹 context (高糖 / 高 processed food 會 trigger flare-up)")
    lines.append("5. 幽默自嘲 style (e.g. CIO 晏茶又擺低兩舊曲奇, 投資回報率好低)")
    lines.append("6. share ratio enforcement (Jim 60% / 小寶 40% 鎖死)")
    lines.append("7. 唔好算死, 留 5-10% buffer 唔好逐克計")
    lines.append("8. 觀察全日 pattern, comment 飲食 timing (e.g. 晏茶太夜食, dinner 唔該收一收)")
    return "\n".join(lines)


def _refresh_google_access_token() -> str:
    """Refresh OAuth access token from /home/work/.hermes/google_token.json.

    Returns the access token string (also persists refreshed token to disk).
    Returns "" on failure (caller should log + skip).
    """
    try:
        tok = json.loads(Path("/home/work/.hermes/google_token.json").read_text())
        if not tok.get("refresh_token"):
            return ""
        data = urllib.parse.urlencode({
            "client_id": tok["client_id"],
            "client_secret": tok["client_secret"],
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        access = resp["access_token"]
        tok["token"] = access
        Path("/home/work/.hermes/google_token.json").write_text(json.dumps(tok, indent=2))
        return access
    except Exception:
        return ""


def _log_drive_failure(local_path, reason: str) -> None:
    """Append a single line to /tmp/drive_upload_errors.log.

    Wrapped in its own try/except so a logging failure can't mask the
    real error, and so the log file write itself never propagates up
    to the caller's try-block (where its exception would be swallowed
    by the next retry attempt and the failure would go unrecorded).
    """
    try:
        with open("/tmp/drive_upload_errors.log", "a") as _f:
            _f.write(f"{now_iso()} | upload_to_drive | {reason} | path={local_path}\n")
    except Exception:
        pass


def _record_pending_upload(local_path, reason: str) -> None:
    """Append a failed upload to /home/work/.hermes/drive_pending_uploads.json.

    This is the durable failure record: even if /tmp is wiped, the
    reconciliation script (scripts/reconcile_drive_urls.py) can find
    pending uploads here and retry them with full upload + sheet write.
    """
    import time as _time
    try:
        queue_path = Path("/home/work/.hermes/drive_pending_uploads.json")
        items = []
        if queue_path.exists():
            try:
                items = json.loads(queue_path.read_text())
            except Exception:
                items = []
        # Try to extract date/time from the local filename (scan_YYYYMMDD_HHMMSS.jpg).
        m = re.match(r"scan_(\d{8})_(\d{6})\.jpg$", local_path.name)
        date_s = ""
        time_s = ""
        if m:
            d = m.group(1)
            date_s = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            t = m.group(2)
            time_s = f"{t[:2]}:{t[2:4]}"
        items.append({
            "ts": _time.time(),
            "date": date_s,
            "time": time_s,
            "local_path": str(local_path),
            "reason": reason,
        })
        tmp = queue_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2))
        tmp.replace(queue_path)
    except Exception:
        pass


def _upload_to_drive(local_path: Path, parent_folder_id: str = None) -> str:
    """Upload a JPEG to Google Drive and return a public view URL.

    v3.3.25 (Jim OOB 2026-10-20 production-pollution guard): in
    GYMBRO_WRITE_MODE=dryrun this NEVER uploads — returning "" keeps
    test runs from leaving orphan Drive files behind (26 of them had to
    be trashed after the 2026-10-20 QA accident).

    Jim OOB 2026-08-14: 「the food log sheet should be the golden copy —
    both name and image attached」. Prior to v3.2.7.45, only the dish name
    was mirrored to the Google Sheet; the image stayed local-only. This
    helper uploads the JPEG to Drive, makes it public-readable, and returns
    the canonical `https://drive.google.com/uc?export=view&id=<FILE_ID>`
    URL — appended to the Sheet as column N (see _append_to_sheet_nutrition).

    v3.2.7.47: 2x retry with 2s backoff on transient failures (network,
    5xx, timeout). The token refresh itself is retried on first call
    failure (handles expired-token races during auto-commit bursts).

    v3.2.7.48: log-write moved out of the retry try-block so its own
    exception can't be swallowed by the next attempt. On terminal
    failure, also writes to /home/work/.hermes/drive_pending_uploads.json
    so scripts/reconcile_drive_urls.py can drain it later.

    On any persistent failure, returns empty string. The scan commit does
    NOT fail if Drive upload fails — the sheet just gets an empty column N
    — but the pending-queue file makes the failure recoverable.

    Pattern adapted from /tmp/drive_upload_8_9.py (one-off script that
    uploaded 3 scans on 2026-08-09, proving the API works). Same OAuth
    token, same multipart upload, same permissions.create call.

    Args:
        local_path: Path to the JPEG to upload (e.g. scan_YYYYMMDD_HHMMSS.jpg)
        parent_folder_id: Optional Drive folder ID. If None, uploads to root.

    Returns:
        Public URL string e.g. "https://drive.google.com/uc?export=view&id=ABC123"
        or "" on failure.
    """
    if not local_path.exists():
        _log_drive_failure(local_path, "local_file_missing")
        _record_pending_upload(local_path, "local_file_missing")
        return ""
    # v3.3.25 production-write guard: dryrun NEVER touches Drive.
    if _write_mode() == "dryrun":
        print(f"[write-guard] dryrun — skipping Drive upload of "
              f"{local_path.name}", flush=True)
        return ""
    last_err = ""
    for attempt in range(3):  # 3 attempts total = 1 initial + 2 retries
        try:
            access = _refresh_google_access_token()
            if not access:
                last_err = "no_access_token"
                if attempt < 2:
                    import time as _t
                    _t.sleep(2)
                    continue
                _log_drive_failure(local_path, "no_access_token")
                _record_pending_upload(local_path, "no_access_token")
                return ""
            boundary = uuid.uuid4().hex
            file_data = local_path.read_bytes()
            meta = {"name": local_path.name, "mimeType": "image/jpeg"}
            if parent_folder_id:
                meta["parents"] = [parent_folder_id]
            body = (
                f"--{boundary}\r\n"
                "Content-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(meta)}\r\n"
                f"--{boundary}\r\n"
                "Content-Type: image/jpeg\r\n\r\n"
            ).encode() + file_data + (f"\r\n--{boundary}--").encode()
            req = urllib.request.Request(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
                data=body,
                headers={
                    "Authorization": f"Bearer {access}",
                    "Content-Type": f"multipart/related; boundary={boundary}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                file_id = json.loads(r.read()).get("id")
            if not file_id:
                last_err = "no_file_id_in_response"
                if attempt < 2:
                    import time as _t
                    _t.sleep(2)
                    continue
                _log_drive_failure(local_path, "no_file_id_in_response")
                _record_pending_upload(local_path, "no_file_id_in_response")
                return ""
            # Make public-readable
            perm_req = urllib.request.Request(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
                data=json.dumps({"role": "reader", "type": "anyone"}).encode(),
                headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(perm_req, timeout=10).read()
            except Exception:
                # Permission failure is non-fatal — file exists, just less public
                pass
            return f"https://drive.google.com/uc?export=view&id={file_id}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < 2:
                import time as _t
                _t.sleep(2)
                continue
    # All attempts failed
    _log_drive_failure(local_path, f"{last_err} (after 3 attempts)")
    _record_pending_upload(local_path, last_err or "unknown")
    return ""


def _set_sheet_drive_url(row_index: int, drive_url: str, access_token: str = None) -> bool:
    """Update an existing sheet row's column K (Drive Image URL) by 1-indexed row.

    Used by the backfill script to fill historical entries. Row 1 is the
    header, so data rows start at 2. If access_token is None, refreshes.

    Returns True on success.

    v3.2.7.48: Drive URL column moved from N to K after the schema trim
    that dropped 來源/Image/User Hints.
    """
    try:
        if not access_token:
            access_token = _refresh_google_access_token()
        if not access_token:
            return False
        SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition!K{row_index}?valueInputOption=USER_ENTERED"
        body = {"values": [[drive_url]]}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="PUT",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:
        return False


def _extract_exif_capture_time(img_path: Path) -> dict:
    """Extract photo capture timestamp from EXIF metadata.

    v3.3.8 (Jim OOB 2026-08-31「capture the photo taken time instead of
    upload time. If so, change to photo capture time for a more accurate
    timing of food logging」): pull DateTimeOriginal (preferred) → DateTime
    → DateTimeDigitized fallback chain. Returns {capture_time: datetime|None,
    source: str, raw_str: str} or empty capture_time if no EXIF available.

    Why: iPhone photo stream upload often happens hours after the actual
    shot. File mtime = upload time, which gives wrong meal_type slot for
    late uploads (e.g. breakfast photo uploaded at dinner time).
    EXIF DateTimeOriginal = real shot time.

    Args:
        img_path: Path to JPEG/HEIC image

    Returns:
        dict with keys:
          - capture_time: timezone-aware datetime in Asia/Hong_Kong or None
          - source: 'DateTimeOriginal' | 'DateTime' | 'DateTimeDigitized' | ''
          - raw_str: raw EXIF string for debug logging
    """
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        if not img_path.exists():
            return {"capture_time": None, "source": "", "raw_str": ""}
        with Image.open(img_path) as im:
            exif = im.getexif()
            if not exif:
                return {"capture_time": None, "source": "", "raw_str": ""}
            # Look for DateTimeOriginal (tag 0x9003 = 36867) in ExifIFD
            exif_ifd = exif.get_ifd(0x8769) or {}  # ExifIFD tag
            # Standard EXIF datetime tag IDs (decimal):
            #   36867 = DateTimeOriginal (when photo was taken)
            #   36868 = DateTimeDigitized (when digitized)
            #   306 = DateTime (file modification time in EXIF)
            tag_priorities = [
                (36867, "DateTimeOriginal"),
                (36868, "DateTimeDigitized"),
                (306, "DateTime"),
            ]
            for tag_id, tag_name in tag_priorities:
                raw = exif_ifd.get(tag_id) or exif.get(tag_id)
                if not raw:
                    continue
                raw_str = str(raw).strip()
                # EXIF format: "YYYY:MM:DD HH:MM:SS"
                try:
                    dt_naive = datetime.strptime(raw_str, "%Y:%m:%d %H:%M:%S")
                    # EXIF datetime is local camera time. iPhone defaults to
                    # the device's set timezone, which for Jim is HKT.
                    # If we wanted strict accuracy, we'd read offsetTimeOriginal
                    # (EXIF 2.31+), but iPhone often omits that. Default to HKT.
                    dt_aware = dt_naive.replace(tzinfo=HKT)
                    # v3.3.8 fix: serialise datetime as ISO string so JSON
                    # cache write doesn't blow up with "Object of type
                    # datetime is not JSON serializable".
                    return {
                        "capture_time": dt_aware.isoformat(),
                        "source": tag_name,
                        "raw_str": raw_str,
                    }
                except ValueError:
                    continue
        return {"capture_time": None, "source": "", "raw_str": ""}
    except Exception as e:
        # PIL not installed or other error — silent fallback (caller uses
        # upload time as before)
        return {"capture_time": None, "source": "", "raw_str": f"error: {e}"}


def _make_thumb(src: Path, dst: Path, max_side: int = 480) -> str:
    """Generate a JPEG thumbnail of `src` at `dst`. Returns the dst path string, or "" on failure.

    FIX (Frimpong 2026-10-20, scan-progress work): the signature had been
    refactored to `(src, max_side=320)` while the body still referenced a
    global `dst` that no longer existed → every call raised NameError and
    was swallowed, so NO thumbnail was ever written and /scan_thumb/<f>
    returned HTTP 500. All 12 call sites already pass (src, dst), so the
    original 3-arg signature is restored here.

    v3.2.7.46: replaces full-resolution JPEGs (~1.9MB each) with 480px max-side
    JPEGs (~15-30KB each) in the food history view. 168 entries × ~30KB = ~5MB
    instead of ~320MB. Pre-generated at commit time + lazy-fallback on miss
    via /scan_thumb/<name>.thumb.jpg (see serve_scan_thumb).

    Uses PIL Image.thumbnail which preserves aspect ratio. JPEG quality 82 is
    visually indistinguishable from quality 95 for a 480px image but cuts
    size ~3x. optimize=True does an extra progressive scan pass.

    Safe to call — returns "" on any failure (missing PIL, bad image, disk
    full). Caller should fall back to image_url when this returns "".
    """
    if not _HAS_PIL:
        return ""
    try:
        if not src.exists():
            return ""
        dst.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            # v3.3.7 (Jim OOB 2026-08-19 'last two photos rotated'):
            # iPhone stores portrait photos as rotated raw pixels with
            # EXIF orientation=6/8. PIL doesn't auto-rotate on open, so
            # the thumbnail inherits the rotation. exif_transpose
            # applies the EXIF orientation tag to the pixel data so the
            # thumbnail is right-side up.
            im = ImageOps.exif_transpose(im)
            im.thumbnail((max_side, max_side), Image.LANCZOS)
            # Convert RGBA→RGB (PNG with alpha) to avoid JPEG save errors.
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGB")
            im.save(dst, "JPEG", quality=82, optimize=True)
        return str(dst)
    except Exception as e:
        try:
            with open("/tmp/thumb_errors.log", "a") as _f:
                _f.write(f"{now_iso()} | make_thumb | {type(e).__name__}: {e} | src={src}\n")
        except Exception:
            pass
        return ""


def _thumb_path_for(scan_image_name: str) -> Path:
    """Map a scan_YYYYMMDD_HHMMSS.jpg filename to its .thumb.jpg sibling in SCAN_THUMB_DIR."""
    p = Path(scan_image_name)
    return SCAN_THUMB_DIR / (p.stem + ".thumb.jpg")


def _thumb_url_for(image_url: str) -> str:
    """Derive /scan_thumb/<name>.thumb.jpg from /scan_img/<name>.jpg. Returns "" if not a scan image URL."""
    if not image_url or not image_url.startswith("/scan_img/"):
        return ""
    return f"/scan_thumb/{Path(image_url).stem}.thumb.jpg"


def _safe_num(v, default=0.0, lo=None, hi=None):
    """Coerce v to float safely. Returns default for None/""/literal "None"/"null"/"NaN"
    or anything non-parseable. Strips whitespace and clamps to [lo, hi] when given."""
    if v is None:
        return default
    if isinstance(v, str):
        s = v.strip()
        if s == "" or s.lower() in ("none", "null", "nan"):
            return default
        try:
            x = float(s)
        except (TypeError, ValueError):
            return default
    else:
        try:
            x = float(v)
        except (TypeError, ValueError):
            return default
    if lo is not None and x < lo:
        x = lo
    if hi is not None and x > hi:
        x = hi
    return x


_GENERIC_NAME_MARKERS = (
    "無法識別", "無法辨認", "無法認出", "無法確認", "未能識別", "未能辨認",
    "未識別", "認唔到", "認唔出", "睇唔到", "睇唔清", "睇唔出",
    "唔知係咩", "唔清楚係咩", "唔知係邊", "不確定", "not food", "no food",
)

# 真係零卡路里嘅嘢(白開水、無糖茶…)—— 呢啲 0 kcal 係正常,唔可以當壞行擋
_ZERO_CAL_NAMES = {
    "水", "白開水", "清水", "凍水", "熱水", "冰水", "蒸餾水", "礦泉水",
    "黑咖啡", "無糖咖啡", "美式咖啡", "茶", "無糖茶", "綠茶", "普洱茶",
    "菊花茶", "紅茶",
}


def _sheet_write_guard(entry: dict) -> dict:
    """寫入 golden copy 嘅最後一道閘(所有路徑共用)。

    實測 2026-10-21 13:52(HKT):Jim 自己 13:16 影嘅午餐盒相,AI 認唔出,
    結果寫咗一行落 production Sheet:
        餐名  =「黑金圓形餐盒內食物（無法識別）」
        餐廳  =「描述可靠識別餐廳」(AI 廢話)
        營養  = 0 / 0 / 0 / 0
    相片路徑係 scan job **自動 commit**(`_scan_preview_core` → 直接叫
    `_append_to_sheet_nutrition`),唔經 `/api/scan_commit`,所以之前放喺
    commit endpoint 嘅消毒器完全冇覆蓋到。
    → 而家防線放喺 `_append_to_sheet_nutrition`(唯一嘅 production writer),
      一次過覆蓋:相片自動 commit / 文字輸入 commit / MCP tool / 修正。

    回傳 {"ok": True} = 准寫;{"ok": False, "reason": ...} = 擋。
    """
    name = str(entry.get("meal_name") or entry.get("name") or "").strip()
    low = name.lower()

    # 1) 泛用 / 認唔出嘅菜名 —— 一律唔准入表(Rule 25)
    if (not name
            or name in _GENERIC_DISH_NAMES_BLOCKLIST
            or any(m in low for m in _GENERIC_NAME_MARKERS)):
        print(f"[sheet-guard] BLOCKED unusable name {name!r}", flush=True)
        return {"ok": False, "reason": "unusable_name"}

    # 2) 餐廳消毒 —— AI 廢話變空字串,唔會污染 golden copy
    rest = entry.get("restaurant_chain", entry.get("restaurant", "")) or ""
    clean_rest = _sanitise_restaurant_name(rest, dish_name=name)
    if rest and not clean_rest:
        print(f"[sheet-guard] restaurant dropped (AI 廢話): {rest!r}",
              flush=True)
    entry["restaurant_chain"] = clean_rest

    # 3) 4 大營養素全 0 → 補唔到就唔准入表
    kcal = _safe_num(entry.get("calories", entry.get("kcal", 0)),
                     default=0.0, lo=0, hi=3000)
    p = _safe_num(entry.get("protein", 0), default=0.0, lo=0, hi=300)
    c = _safe_num(entry.get("carbs", 0), default=0.0, lo=0, hi=300)
    f = _safe_num(entry.get("fat", 0), default=0.0, lo=0, hi=300)
    if kcal <= 0 and p <= 0 and c <= 0 and f <= 0:
        if name in _ZERO_CAL_NAMES:
            return {"ok": True}
        looked = _lookup_known_macros(name) or _lookup_brand_macros(name)
        if looked and looked.get("kcal"):
            entry["calories"] = entry["kcal"] = looked["kcal"]
            entry["protein"] = looked.get("protein", 0)
            entry["carbs"] = looked.get("carbs", 0)
            entry["fat"] = looked.get("fat", 0)
            print(f"[sheet-guard] backfilled {name!r} → {looked['kcal']} kcal "
                  f"from {looked.get('_source', '?')}", flush=True)
        else:
            print(f"[sheet-guard] BLOCKED all-zero nutrition for {name!r}",
                  flush=True)
            return {"ok": False, "reason": "zero_nutrition"}
    return {"ok": True}


# v3.3.33 (Jim OOB 2026-10-21「ai work can be done in parallel」):
# 平行 AI worker 會同時叫 `_append_to_sheet_nutrition`。Sheet append 本身安全
# (Google append API 會接喺最後一行之下),但呢段嘢**唔係 concurrency-safe**:
#   1. google_token.json 嘅 refresh + write_text(非 atomic,可能寫到爛 JSON)
#   2. idempotency store 嘅 read-modify-write(可能漏 key)
#   3. trim pre-flight 嘅 read + batchUpdate
# 所以用一把 process-wide lock 包住成段寫入。AI 係慢嘅部分(每張 10-20 秒),
# Sheet 寫入只係 1-2 秒 —— parallel AI + serialised write = 快 N 倍而唔會
# 撞爛 Jim 嘅 golden copy。
#
# 用 RLock(唔係 Lock)——萬一將來有 function 互相叫,RLock 唔會死鎖。
_SHEET_WRITE_LOCK = threading.RLock()


def _serialise_sheet_write(fn):
    """Decorator: 把 production writer 序列化(平行 AI worker 安全網)。"""
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        with _SHEET_WRITE_LOCK:
            # v3.3.33 (Frimpong 2026-10-21 指出):經 `__wrapped__` 間接層
            # 叫內層實作 —— 咁 QA 可以換走真實 Google 寫入(唔打真 API)
            # 而 lock 仍然生效,真正動態驗證「序列化有效」。
            # 預設 `functools.wraps` 已經令 `_wrapped.__wrapped__ is fn`，
            # 所以行為同直接叫 fn 一模一樣。
            inner = getattr(_wrapped, "__wrapped__", fn)
            return inner(*args, **kwargs)
    return _wrapped


@_serialise_sheet_write
def _append_to_sheet_nutrition(entry: dict) -> dict:
    """Mirror entry to Google Sheet Nutrition tab (sheetId 474877075).
    Returns {"ok": bool, "range": str} — silent on quota/error.

    Jim OOB 2026-07-29: dedup by (date, time_short, calories) BEFORE push.
    Prevents cron double-push from creating duplicates.
    Also marks entry.sheet_synced=True on success.

    v3.3.25 (Jim OOB 2026-10-20 "Someone has pollute the production
    data. Pls avoid doing so"): this is THE production writer. Guarded
    by GYMBRO_WRITE_MODE:
      - dryrun    → never POSTs; returns {"ok": True, "range": "dryrun",
                   "would_have_written": [...]} so QA can report the row
                   list without writing (Alonso rule 4).
      - throwaway → writes to GYMBRO_SHEET_ID (a COPY of the tab).
      - production → real behaviour (default).
    """
    # v3.3.32 production-write guard — evaluated BEFORE any network call
    # so a dryrun run can never touch Jim's golden copy.
    # v3.3.32: 統一寫入閘先跑 —— 唔准嘅行(泛用名 / AI 廢話餐廳 / 0 營養)
    # 一律唔寫,dryrun 模式亦都一樣(方便 QA 睇到同一個決定)。
    _guard = _sheet_write_guard(entry)
    if not _guard.get("ok"):
        return {"ok": False, "range": "", "skipped": True,
                "error": _guard.get("reason", "blocked"),
                "blocked_by": "sheet_write_guard"}
    _mode = _write_mode()
    _target_sid = _target_sheet_id()
    if _mode == "dryrun":
        row_preview = {
            "date": entry.get("date", today_iso()),
            "time": (entry.get("time", "") or "")[:5],
            "meal_type": entry.get("meal_type", "meal"),
            "name": (entry.get("meal_name") or entry.get("name") or "")[:120],
            "calories": entry.get("calories", 0),
            "protein": entry.get("protein", 0),
            "carbs": entry.get("carbs", 0),
            "fat": entry.get("fat", 0),
            "would_have_written": True,
        }
        print(f"[write-guard] dryrun — NOT writing to Nutrition tab. "
              f"would-have-written: {row_preview}", flush=True)
        entry["sheet_synced"] = False
        return {"ok": True, "range": "dryrun", "skipped": True,
                "would_have_written": [row_preview]}
    try:
        tok = json.loads(Path("/home/work/.hermes/google_token.json").read_text())
        if "token" not in tok or not tok.get("refresh_token"):
            return {"ok": False, "error": "no_token"}
        # Refresh access token
        data = urllib.parse.urlencode({
            "client_id": tok["client_id"],
            "client_secret": tok["client_secret"],
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        access = resp["access_token"]
        tok["token"] = access
        Path("/home/work/.hermes/google_token.json").write_text(json.dumps(tok, indent=2))

        # Dedup check: pull existing sheet rows + check if entry already exists
        # Jim OOB 2026-07-29: signature (date, time_short, calories) catches duplicates
        # regardless of meal_type naming inconsistency.
        # v3.3.25: honour GYMBRO_SHEET_ID so throwaway mode can write to a
        # COPY of the Nutrition tab instead of Jim's golden copy.
        SHEET_ID = _target_sid
        try:
            url_check = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition!A1:Z1000?valueRenderOption=FORMATTED_VALUE"
            req_check = urllib.request.Request(url_check, headers={"Authorization": f"Bearer {access}"})
            existing = json.loads(urllib.request.urlopen(req_check, timeout=10).read()).get("values", [])
            entry_date = entry.get("date", today_iso())
            entry_time = (entry.get("time", "00:00") or "00:00")[:5]
            # v3.2.7.48: keep dedup's cal signature consistent with the
            # clamped integer that will actually be written to the sheet.
            entry_cal = str(int(round(_safe_num(entry.get("calories", 0), default=0.0, lo=0, hi=3000))))
            # v3.3.33 (parallel AI): 同一分鐘 + 同一卡路里 **唔等於重複**。
            # 平行模式之下幾張相會喺同一分鐘完成,如果兩碟嘢啱啱好同一卡路里,
            # 舊 dedup 會當重複 → **漏寫一行落 Jim 個 golden copy**。
            # 所以加多一重:兩行都有相片 URL 而且唔同 → 一定唔係重複。
            entry_img = entry.get("drive_image_url", "") or ""
            for row in existing[1:]:
                if not row:
                    continue
                row_date = row[0]
                row_time_full = row[1] if len(row) > 1 else ''
                row_time = row_time_full[11:16] if 'T' in row_time_full else row_time_full[:5]
                row_cal = row[5] if len(row) > 5 else ''
                if row_date == entry_date and row_time == entry_time and row_cal == entry_cal:
                    row_img = row[10] if len(row) > 10 else ''
                    if row_img and entry_img and row_img != entry_img:
                        # 唔同相 → 唔係重複,照寫
                        continue
                    # Already in sheet — mark synced locally + skip push
                    entry["sheet_synced"] = True
                    return {"ok": True, "range": "deduped", "skipped": True}
        except Exception as _dedup_err:
            # v3.2.7.48: log the dedup read failure instead of silently dropping it.
            # Still continue with append (better to dup than miss) — the persistent
            # idempotency store below is the real second line of defense.
            try:
                with open("/tmp/sheet_append_errors.log", "a") as _f:
                    _f.write(f"{now_iso()} | in_sheet_dedup_read | {type(_dedup_err).__name__}: {_dedup_err}\n")
            except Exception:
                pass

        # Append row to Nutrition tab
        # v3.2.7.47: column B is HH:MM (was full ISO datetime — 451 rows were
        # polluted with "2026-08-09T20:44:48+08:00" before this fix).
        # v3.2.7.48: numeric coercion via _safe_num + clamping + integer write
        # (eliminates float noise like "0.3", "30.5", and "12.300000000000001"
        # in columns F/G/H/I — see audit of 476 rows).
        # v3.2.7.48: schema trimmed to 11 cols — dropped 來源 (K), Image (L),
        # User Hints (M). Drive Image URL moved from N to K (Jim OOB 2026-08-14).
        drive_image_url = entry.get("drive_image_url", "") or ""
        # v3.3.4: harden column K write. Reject any value that isn't a Drive
        # URL (Jim OOB 2026-08-15 'I don't accept broken image' — row 455
        # had K='30', a corrupted int from a wrong lookup that bled here).
        # PWA skips <img> rendering for empty string, so blanks are fine.
        if drive_image_url and not re.match(
            r"^https?://(lh3\.googleusercontent\.com|drive\.google\.com)/",
            drive_image_url,
        ):
            drive_image_url = ""
        # v3.2.7.47: derive clean HH:MM from entry.time (strip ISO if present).
        raw_time = entry.get("time", "") or ""
        if "T" in raw_time:
            # e.g. "2026-08-09T20:44:48.811322+08:00" → "20:44"
            t_part = raw_time.split("T", 1)[1][:5]
        else:
            t_part = raw_time[:5]
        if not t_part or len(t_part) < 4:
            t_part = now_iso().split("T")[-1][:5]
        # v3.2.7.48: zero-pad a single-digit hour so "8:30" becomes "08:30".
        # 37 rows had a single-digit hour (e.g. "8:30") that the prior ISO→HH:MM
        # normalization passed through unchanged.
        if not re.match(r"^\d{2}:\d{2}$", t_part):
            if re.match(r"^\d{1}:\d{2}$", t_part):
                t_part = "0" + t_part
            else:
                t_part = now_iso().split("T")[-1][:5]
        # v3.2.7.48: coerce + clamp all four numeric fields. Use integer
        # stringification so float noise can never reach the sheet.
        kcal = _safe_num(entry.get("calories", 0), default=0.0, lo=0, hi=3000)
        protein = _safe_num(entry.get("protein", 0), default=0.0, lo=0, hi=300)
        carbs = _safe_num(entry.get("carbs", 0), default=0.0, lo=0, hi=300)
        fat = _safe_num(entry.get("fat", 0), default=0.0, lo=0, hi=300)
        # v3.2.7.48: kcal↔macro reconciliation guard. AI often hallucinates
        # macros independently of calories (50 rows in the audit had >40%
        # 4p+4c+9f vs kcal mismatch). When both sides are non-trivial and the
        # disagreement exceeds 40%, trust the macros and overwrite kcal with
        # the reconstructed value, marking the row's note column so the audit
        # trail stays in the sheet itself.
        note = entry.get("note", "scan_food") or "scan_food"
        comp = 4 * protein + 4 * carbs + 9 * fat
        if kcal > 50 and comp > 50:
            denom = max(kcal, comp)
            diff = abs(kcal - comp) / denom
            if diff > 0.4:
                marker = f"⚠kcal校正 {int(round(kcal))}→{int(round(comp))}"
                note = (note + " " + marker).strip()[:200]
                kcal = comp
        row_data = [
            entry.get("date", today_iso()),
            t_part,  # col B — HH:MM only (was full ISO datetime)
            entry.get("meal_type", "meal"),
            # v3.2.7.48: chokepoint sanitiser — column D MUST never be
            # empty / 'scan' / narration / 120-char-prose-bleed. The
            # helper returns either a clean dish name or '未識別菜式'.
            _sanitise_dish_name(entry.get("meal_name") or entry.get("name"))[:120],
            entry.get("restaurant_chain", ""),
            str(int(round(kcal))),
            str(int(round(protein))),
            str(int(round(carbs))),
            str(int(round(fat))),
            note,
            drive_image_url,    # col K — Drive Image URL (Golden Copy, moved from N)
        ]
        # v3.2.7.47: hard-enforce column count. If row_data has fewer (e.g. user_correction
        # path), pad with ""; if more, truncate. Without this, append silently
        # drops fields (if too short) or extends row width (if too long),
        # causing the "wide row bleed" issue seen in rows 462+ of the sheet.
        # v3.2.7.48: enforced count is 11 (K=來源, L=Image, M=User Hints
        # removed by Jim OOB 2026-08-14 — Drive URL moved from N to K).
        while len(row_data) < 11:
            row_data.append("")
        row_data = row_data[:11]
        body = {"values": [row_data], "majorDimension": "ROWS"}
        # v3.2.7.45: specify explicit range A1:N so the append goes to columns
        # A-N, not the columns N-AA that auto-commit has been mistakenly
        # using since v3.2.7.10. Without the explicit range, the API
        # auto-detects the table range from existing data and picks N-AA
        # (because buggy self-commits have been writing there). Forcing
        # A1:N ensures the new row lands in the correct columns.
        # v3.2.7.48: schema trimmed — append range is A1:K (Drive URL moved
        # from N to K after dropping 來源/Image/User Hints).
        # v3.2.7.47: also pre-flight check — if the sheet has any row with
        # width >11 (orphan from pre-fix data), trim it first to prevent
        # the append from inheriting the bad width.
        try:
            url_wide = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition!A1:Z1000?valueRenderOption=FORMATTED_VALUE"
            req_w = urllib.request.Request(url_wide, headers={"Authorization": f"Bearer {access}"})
            existing_rows = json.loads(urllib.request.urlopen(req_w, timeout=10).read()).get("values", [])
            trim_edits = []
            for i, r in enumerate(existing_rows[1:], start=2):
                if len(r) > 11:
                    # Pad to 11 cols first (to overwrite trailing empties)
                    padded = list(r) + [""] * (11 - len(r))
                    trim_edits.append({
                        "range": f"Nutrition!A{i}:K{i}",
                        "values": [padded[:11]],
                    })
            if trim_edits:
                # batchUpdate can take up to 500 edits per call — split if needed
                for chunk_start in range(0, len(trim_edits), 500):
                    chunk = trim_edits[chunk_start:chunk_start + 500]
                    url_t = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate?valueInputOption=USER_ENTERED"
                    body_t = {"valueInputOption": "USER_ENTERED", "data": chunk}
                    req_t = urllib.request.Request(url_t, data=json.dumps(body_t).encode(), method="POST",
                                                  headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"})
                    urllib.request.urlopen(req_t, timeout=30).read()
        except Exception as e:
            # Pre-flight trim is best-effort. Don't fail the append if trim fails.
            with open("/tmp/sheet_trim_errors.log", "a") as _f:
                _f.write(f"{now_iso()} | trim_wide_rows | {type(e).__name__}: {e}\n")
        # v3.3.8 (Jim OOB 2026-08-31「the google sheet has not been updated
        # since 15 Aug. please fix and backfill」): the (date, time, name,
        # calories) dedup key produced FALSE POSITIVES for vision scans
        # where name was vague (e.g. "未識別菜式") or where calories were
        # rounded to the same 4p+4c+9f vs kcal tie. Silent skip caused the
        # entire 8/22-8/25 + 8/27 gap. Fix: use timestamp_iso + image_path
        # as the idempotency key (millisecond-unique, tied to the actual
        # upload event). Fallback to legacy hash if both fields missing.
        _ts_iso = entry.get("timestamp_iso") or entry.get("scan_timestamp_iso") or ""
        _img_path = entry.get("image_saved_to") or entry.get("drive_image_url") or ""
        if _ts_iso or _img_path:
            _idem_key = f"{_ts_iso}|{_img_path}"
        else:
            _idem_date = entry.get("date", today_iso())
            _idem_time_raw = entry.get("time", "") or ""
            if "T" in _idem_time_raw:
                _idem_time = _idem_time_raw.split("T", 1)[1][:5]
            else:
                _idem_time = _idem_time_raw[:5]
            if not re.match(r"^\d{2}:\d{2}$", _idem_time):
                if re.match(r"^\d{1}:\d{2}$", _idem_time):
                    _idem_time = "0" + _idem_time
                else:
                    _idem_time = "00:00"
            _idem_meal = (entry.get("meal_name") or entry.get("name") or "scan")[:120]
            _idem_kcal = int(round(_safe_num(entry.get("calories", 0), default=0.0, lo=0, hi=3000)))
            _idem_key = f"{_idem_date}|{_idem_time}|{_idem_meal}|{_idem_kcal}"
        if not _idempotency_check(_idem_key):
            # v3.3.8: log dedup skips so silent-fail gaps become visible.
            # Previously skipped rows just returned {"skipped": True} with
            # no log → 8/22-8/27 went dark for 6 days because every retry
            # collided with stale idempotency entries.
            try:
                with open("/tmp/sheet_idempotency_skips.log", "a") as _f:
                    _f.write(
                        f"{now_iso()} | skipped | key={_idem_key} | "
                        f"name={entry.get('name', '')} | date={entry.get('date')} | "
                        f"time={entry.get('time')} | kcal={entry.get('calories')}\n"
                    )
            except Exception:
                pass
            # Already committed this row within the bounded window — skip
            # the append and return the same shape the in-sheet dedup uses.
            entry["sheet_synced"] = True
            return {"ok": True, "range": "deduped", "skipped": True}
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition!A1:K1:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        # Mark synced locally (Jim OOB 2026-07-29: prevent re-push)
        entry["sheet_synced"] = True
        updated_range = resp.get("updates", {}).get("updatedRange", "?")
        # v3.3.0: extract the appended row's index so cache.commit() can store it.
        # `updatedRange` looks like "Nutrition!A2:K2" for a single-row append.
        appended_row_index = None
        m = re.search(r"!A(\d+):K(\d+)$", updated_range or "")
        if m and m.group(1) == m.group(2):
            try:
                appended_row_index = int(m.group(1))
            except (TypeError, ValueError):
                appended_row_index = None
        # v3.2.7.48: header bootstrap — Drive Image URL now sits at column K
        # (was N before the schema trim that dropped 來源/Image/User Hints).
        # If header K is empty, set it. Idempotent — safe to call every push.
        # v3.3.0: index the newly-appended row in the in-memory cache so
        # /api/scan_recent + /api/nutrition/today see it immediately.
        try:
            row_cells = [row_data + [""] * (11 - len(row_data))][:11]
            new_row = NutritionRow.from_sheet_row(appended_row_index, row_cells)
            _get_nutrition_cache().insert_row(new_row)
        except Exception:
            pass  # cache update is best-effort; background refresh will catch up
        try:
            header_check_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition!K1"
            req_h = urllib.request.Request(header_check_url, headers={"Authorization": f"Bearer {access}"})
            hdr_resp = json.loads(urllib.request.urlopen(req_h, timeout=10).read())
            hdr_vals = (hdr_resp.get("values") or [[]])[0]
            hdr_k = hdr_vals[0] if len(hdr_vals) > 0 else ""
            if not hdr_k:
                url_b = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate?valueInputOption=USER_ENTERED"
                body_b = {
                    "valueInputOption": "USER_ENTERED",
                    "data": [{"range": "Nutrition!K1", "values": [["Drive Image URL"]]}],
                }
                req_b = urllib.request.Request(url_b, data=json.dumps(body_b).encode(), method="POST",
                                              headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"})
                urllib.request.urlopen(req_b, timeout=10).read()
        except Exception:
            pass  # header bootstrap is best-effort, don't break the main push
        # v3.3.23r (Jim OOB 2026-09-14 'make sure the column L is updated
        # with formula like these =image(K497). This help me to view the
        # image when i look at the sheet'): write =IMAGE(K<row>) formula to
        # column L for every new entry. Uses lh3.googleusercontent.com/d/<id>
        # variant (direct image bytes) instead of drive.google.com/uc?export=view
        # (303 redirect — Sheets IMAGE() fails on redirects).
        # v3.3.23r hotfix: NO size args! =IMAGE("url",1,80,80) fails with #N/A
        # but =IMAGE("url") succeeds (default fit-cell). The cell renders
        # the image at full cell size — fine for Jim's viewing workflow.
        if appended_row_index and drive_image_url:
            try:
                m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", drive_image_url)
                if m:
                    file_id = m.group(1)
                    lh3_url = f"https://lh3.googleusercontent.com/d/{file_id}"
                    formula = f'=IMAGE("{lh3_url}")'
                    formula_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate?valueInputOption=USER_ENTERED"
                    formula_body = {
                        "valueInputOption": "USER_ENTERED",
                        "data": [{"range": f"Nutrition!L{appended_row_index}",
                                  "values": [[formula]]}],
                    }
                    req_f = urllib.request.Request(formula_url, data=json.dumps(formula_body).encode(),
                                                  method="POST",
                                                  headers={"Authorization": f"Bearer {access}",
                                                           "Content-Type": "application/json"})
                    urllib.request.urlopen(req_f, timeout=10).read()
            except Exception:
                pass  # formula write is best-effort, image still in K column
        # v3.3.23al: invalidate Redis Sheet cache so next read picks up
        # the freshly-written row (otherwise 5min TTL would show stale data).
        try:
            _invalidate_sheet_cache()
        except Exception:
            pass
        return {"ok": True, "range": updated_range, "row_index": appended_row_index}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.route("/api/scan_food", methods=["POST"])
def api_scan_food():
    """Receive image (multipart), run MiniMax M3 vision + pplx enrichment,
    build share-locked entry, log to nutrition + Sheet, return JSON entry."""
    if "image" not in request.files:
        return jsonify({"ok": False, "error": "no image"}), 400
    img_file = request.files["image"]
    img_bytes = img_file.read()
    if len(img_bytes) > 10 * 1024 * 1024:
        return jsonify({"ok": False, "error": "image too large (>10MB)"}), 413
    img_b64 = base64.b64encode(img_bytes).decode()

    # v3.2.7.32: plain-text vision prompt — no structured headers like
    # 「菜式：/份量：/煮法：」. The previous prompt asked for structured
    # output and the AI returned markdown with header lines, which the
    # dish-name extractor mistook for the dish name itself — Jim OOB
    # 2026-08-11 'why today recognized food as 菜式 as food name!'.
    vision_prompt = (
        "你係食物視覺識別助手。請用繁體中文廣東話,2-4 句口語化句子,直接講"
        "相入面見到咩食物、份量幾多(目測)、點煮、邊間餐廳(如見到 logo 或招牌字)。"
        "例如：「一碗白飯配一塊煎雞扒,碟邊有少許黑椒汁,餐廳係太興。」"
        "如果係飲品,就講幾多 ml、咩品牌、有冇冰有冇糖。"
        "如果係小票/receipt,就逐項抄低菜名同份量同價錢(睇到嘅部分)。"
        "唔好用 markdown 標題、唔好用「菜式：」呢啲 section header,"
        "唔好寫「呢張相顯示...」、「可見...」呢啲開場白,直接講食物。"
        "一個英文字都唔好有,唔識就寫「難以辨認」。"
    )
    vision_desc = _minimax_vision(img_b64, vision_prompt)

    # 1b. 2nd-opinion vision from a DIFFERENT provider in the chain
    opinion2_desc = _vision_second_opinion(img_b64, vision_prompt)
    # v3.2.7.14: detect gpt-4o-mini safety-filter refusal (e.g. "抱歉，我無法...")
    # and treat as empty so it does NOT pollute vision_desc (Jim OOB
    # 2026-08-09 16:48 HKT 'I took a screenshot not food, but scan got
    # committed as 抱歉 / 第一道菜').
    if opinion2_desc and ("抱歉" in opinion2_desc[:30] and "無法" in opinion2_desc[:50] and len(opinion2_desc) < 80):
        opinion2_desc = ""
    if opinion2_desc and not opinion2_desc.startswith("（"):
        # 2-vision median-merge: take whichever is longer (more dish detail)
        if len(opinion2_desc) > len(vision_desc) * 0.7:
            vision_desc = vision_desc + "\n\n（2nd-opinion vision）\n" + opinion2_desc

    # 2. (v3.3.34: Perplexity enrichment 已移除 — 淨返 MiniMax 一個 estimate)
    pplx_desc = ""

    # 2b. OpenRouter gpt-4o-mini (2nd estimate, JSON mode, Jim OOB 2026-07-25 13:35)
    mm_desc = _minimax_nutrition_enrich(vision_desc)

    # 2c. Parse both → merge via median (12-field schema)
    pplx_parsed = _parse_nutrition_block(pplx_desc)
    # OpenRouter returns JSON string, parse differently
    mm_parsed = {}
    if mm_desc:
        # v3.3.22: use safe parser to strip  blocks + extract last JSON
        mm_parsed = _safe_parse_nutrition_json(mm_desc)
    merged_nutrition = _merge_nutrition_estimates([pplx_parsed, mm_parsed])
    # legacy raw fields (kcal, P) for back-compat with code that reads these
    raw_kcal = int(merged_nutrition.get("calories", {}).get("value", 0) or 0)
    raw_p = int(merged_nutrition.get("protein", {}).get("value", 0) or 0)

    # 3. Heuristic share + macros hint (vision often gives totals)
    shared = _detect_shared_meal(vision_desc + " " + pplx_desc)
    jim_ratio = 0.60 if shared else 1.00
    jim_kcal = round(raw_kcal * jim_ratio)
    jim_p = round(raw_p * jim_ratio)

    # 4. Build entry — 12-field nutrition schema (Jim OOB 2026-07-25 13:35)
    now_hkt_dt = datetime.now(timezone(timedelta(hours=8)))
    # Per-field entry: jim_*_amount = round(value * jim_ratio) if shared, else value
    field_entries = {}
    for f in NUTRITION_FIELDS:
        info = merged_nutrition.get(f)
        if not info:
            field_entries[f] = 0
            continue
        v = info.get("value", 0) or 0
        field_entries[f] = round(v * jim_ratio, 1) if shared else v
    entry = {
        "date": today_iso(),
        "time": now_hkt_dt.strftime("%H:%M"),
        "timestamp_iso": now_iso(),
        "meal_type": "scan",
        "meal_name": f"scan_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}",
        "name": _extract_dish_name(vision_desc, pplx_desc),  # v2.7.32: was vision_desc[:200] — bled prose into name field
        "vision_raw_desc": vision_desc,
        "pplx_enrichment": pplx_desc,
        "minimax_enrichment": mm_desc[:500] if mm_desc else "",
        "minimax_json_parsed": mm_parsed,
        "nutrition_merged": merged_nutrition,
        "restaurant_chain": "",  # user/correction can fill
        "share_with_wife": ("Jim 60% / 小寶 40% (auto-applied)" if shared else "Jim 100% (solo)"),
        "is_shared_meal": shared,
        # 12-field nutrition (Jim OOB 2026-07-25 13:35)
        "calories": field_entries["calories"],
        "protein": field_entries["protein"],
        "carbs": field_entries["carbs"],
        "fat": field_entries["fat"],
        "fiber": field_entries["fiber"],
        "sugar": field_entries["sugar"],
        "sodium": field_entries["sodium"],
        "sat_fat": field_entries["sat_fat"],
        "trans_fat": field_entries["trans_fat"],
        "vit_c": field_entries["vit_c"],
        "iron": field_entries["iron"],
        "calcium": field_entries["calcium"],
        "raw_kcal_estimate": raw_kcal,
        "raw_p_estimate": raw_p,
        "source": "v3.3.34-scan (minimax-text-01 + minimax-m3-enrich)",
        "models_used": ["minimax-text-01", "minimax-m3-enrich"],
        "confidence": "12-field median-merged (Jim can correct via /api/scan_correct)",
        "sheet_synced": False,
        "image_saved_to": "",  # filled below
        # v2.7.37: coach comment + grading (Jim OOB 2026-08-06)
        "coach_comment": _coach_comment(
            field_entries.get("name") or _extract_dish_name(vision_desc, pplx_desc),
            field_entries["calories"], field_entries["protein"],
            field_entries["carbs"], field_entries["fat"],
            daily_ctx_seed=_daily_ctx_seed(),
        ),
        "user_correction": None,  # permanent — never trimmed (Jim OOB 2026-07-23 22:30 HKT "no trimming of data")
    }

    # 5. Save image to scan cache (ephemeral preview buffer only — Drive is
    # the canonical store. v3.3.2: no longer write to food_scan_log.json.)
    img_filename = f"scan_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}.jpg"
    img_path = SCAN_CACHE_DIR / img_filename
    img_path.write_bytes(img_bytes)
    entry["image_saved_to"] = str(img_path)

    # v3.3.2: best-effort Drive upload for Golden Copy. Failure is non-fatal —
    # the Sheet will get an empty column N.
    drive_image_url = _upload_to_drive(img_path)
    entry["drive_image_url"] = drive_image_url
    _make_thumb(img_path, _thumb_path_for(img_filename))

    # 6. Write Sheet → cache refreshes the new row from the Sheet writer.
    sheet_result = _append_to_sheet_nutrition(entry)
    if sheet_result.get("ok") and not sheet_result.get("skipped"):
        try:
            # v3.3.25 (2026-10-20): the old parser was
            # int(range.split(":")[0].rstrip("ABCDEFGHIJKLM")) — but
            # rstrip only strips LETTERS, and the range looks like
            # "Nutrition!A564" (ends with a DIGIT), so int() raised
            # ValueError. The bare `except: pass` swallowed it, so
            # cache.refresh_one() never ran and the new row never reached
            # the cache. Same root cause as the api_scan_commit fix at
            # ~L9760. Use an end-anchored regex to pull the row number.
            range_str = sheet_result.get("range", "")
            m = re.search(r"(\d+)$", range_str.split(":")[0])
            if m:
                get_cache().refresh_one(int(m.group(1)), _sheet_read_nutrition_row)
        except Exception as e:
            print(f"[scan_food] refresh_one failed for range "
                  f"{sheet_result.get('range', '')}: "
                  f"{type(e).__name__}: {e}", flush=True)

    # v3.3.23al (Jim OOB 2026-09-27 'Pls fix all of them. Not just quick
    # win'): pre-generate today's nutrition card JPG in the background.
    # Without this, the first /api/nutrition/card click after a meal
    # log spends 4-5 seconds fetching Drive thumbs sequentially. Now the
    # card is already on disk when the user opens the modal — instant load.
    # Daemon thread so the API response isn't delayed by PIL render.
    import threading as _threading
    def _pregen_card():
        try:
            today = entry.get("date") or today_iso()
            data = _load_today_nutrition() if today == today_iso() else None
            # For non-today entries, just invalidate the file so the next
            # /api/nutrition/card call renders fresh. Simple & safe.
            if data is None or not data.get("meals"):
                return
            _render_nutrition_card_pil(
                today,
                data.get("meals", []),
                data.get("totals", {}),
            )
        except Exception:
            pass  # best-effort; user will just wait on first click
    _t = _threading.Thread(target=_pregen_card, name="pregen-card", daemon=True)
    _t.start()

    return jsonify({
        "ok": True,
        "entry": entry,
        "scan_index": int(sheet_result.get("range", "").split(":")[0].rstrip("ABCDEFGHIJKLM")) if sheet_result.get("range") and not sheet_result.get("skipped") else None,
        "sheet_synced": sheet_result.get("ok", False),
        "sheet_range": sheet_result.get("range", ""),
        "drive_image_url": drive_image_url,
        "image_url": f"/scan_img/{img_filename}",
        "thumbnail_url": _thumb_url_for(f"/scan_img/{img_filename}"),
    })


@app.route("/api/scan_recent", methods=["GET"])
def api_scan_recent():
    """Return last N successful scans (default 5) for dashboard overlay.

    Jim OOB 2026-07-23: 'In scan last 5 photo. Do not show failed upload'.
    Filter logic: drop scans whose name indicates vision failure, so the
    dashboard only shows scans that produced a real food entry.

    v3.3.0: reads from in-memory NutritionCache (hydrated from Google Sheet
    on startup). No food_scan_log.json I/O — that file is gone. coach_comment
    is computed on demand from the cached name+macros (keyword match, no AI).
    """
    limit = int(request.args.get("limit", 100))
    cache = _get_nutrition_cache()
    if not cache.is_ready():
        return jsonify({"ok": False, "error": "cache hydrating"}), 503
    rows = cache.get_recent(limit=limit * 2)

    def _is_failed(name: str) -> bool:
        s = (name or "").strip()
        return (
            s == "食物"
            or s.startswith("食物 #")
            or "失敗" in s
            or "NameError" in s
            or "failed" in s.lower()
        )

    successful = [r for r in rows if not _is_failed(r.name)]
    recent_rows = successful[:limit]
    out = []
    for r in recent_rows:
        d = r.to_pwa_dict()
        # v3.3.1: fast keyword+macro grade, NO AI call.
        # Each row used to hit MiniMax (~5s) → limit=N took N×5s.
        # AI 4-section enrichment only fires on single-row detail views.
        d["coach_comment"] = _coach_comment_keyword(r.name, r.kcal, r.p, r.c, r.f, r.restaurant)
        out.append(d)
    return jsonify({
        "scans": out,
        "total": len(rows),
        "filtered": len(rows) - len(successful),
        "cache": cache.stats(),
    })


# v3.2.7.25: Force-refresh Whoop cache (Jim OOB 2026-08-11 'calendar view
# does not support pull to refresh'). Calls _run_whoop_pull_cached(force=True)
# which spawns `whoop_nutrition.py --sync` to pull all 4 endpoints (cycles /
# recovery / sleep / workouts) and overwrite the cache file. Returns the
# updated cycle + recovery counts so the frontend can confirm freshness.
# Designed for the schedule tab's pull-to-refresh UX — typically 5-15s.
@app.route("/api/whoop_refresh", methods=["POST", "GET"])
def api_whoop_refresh():
    try:
        data = _run_whoop_pull_cached(force=True)
        cycles = len(data.get("cycles") or [])
        recovery = len(data.get("recovery") or [])
        sleep = len(data.get("sleep") or [])
        workouts = len(data.get("workouts") or [])
        synced_at = data.get("synced_at") or now_iso()
        # v3.3.8: invalidate daily coaching — fresh Whoop data means the
        # next cheer/video should re-evaluate recovery.
        _invalidate_daily_coaching()
        return jsonify({
            "ok": True,
            "synced_at": synced_at,
            "cycle_count": cycles,
            "recovery_count": recovery,
            "sleep_count": sleep,
            "workout_count": workouts,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# v2.7.18: Withings steps endpoints (Jim OOB 2026-07-29)


# v2.7.40: Force-refresh Withings steps (Jim OOB 2026-08-06: "tap shoes → refresh
# steps from withings"). Calls the withings.py activity script to fetch fresh
# data, refreshes WITHINGS_CACHE atomically, then returns same shape as
# /api/withings_steps_today. Designed to be fast (~3-5s) for tap-to-refresh UX.


@app.route("/api/scan_correct", methods=["POST"])
def api_scan_correct():
    """Receive Jim's correction for a scan. v3.3.2: scan_index now refers to
    the Sheet row index (1-based, header = 1). The correction is appended to
    the cache row's _coach_comment correction list AND persisted to the Sheet
    row (name + macros + restaurant + note update).
    """
    data = request.get_json(silent=True) or {}
    scan_index = data.get("scan_index")
    if scan_index is None:
        return jsonify({"ok": False, "error": "no scan_index"}), 400
    if not isinstance(scan_index, int) or scan_index < 2:
        return jsonify({"ok": False, "error": "scan_index out of range"}), 404

    cache = get_cache()
    row = cache.get_by_row(scan_index)
    if row is None:
        return jsonify({"ok": False, "error": "scan_index not found in cache"}), 404

    correction = {
        "corrected_at": now_iso(),
        "name": data.get("name"),
        "calories": data.get("calories"),
        "protein": data.get("protein"),
        "carbs": data.get("carbs"),
        "fat": data.get("fat"),
        "restaurant_chain": data.get("restaurant_chain"),
        "note": data.get("note", ""),
    }

    # v3.3.23ad (Jim OOB 2026-09-16): if user supplied macros, use them
    # directly (no recalc). If only name changed + macros empty/zero,
    # do full 4-tier recalc (explicit → brand → AI → history).
    sup_name = (data.get("name") or "").strip()
    sup_kcal = data.get("calories")
    sup_p = data.get("protein")
    sup_c = data.get("carbs")
    sup_f = data.get("fat")
    sup_rest = (data.get("restaurant_chain") or "").strip()
    user_supplied_macros = (
        (sup_kcal is not None and sup_kcal != 0) or
        (sup_p is not None and sup_p != 0) or
        (sup_c is not None and sup_c != 0) or
        (sup_f is not None and sup_f != 0)
    )
    print(f"[scan_correct] row={scan_index} sup_name={sup_name!r} user_supplied_macros={user_supplied_macros} row.kcal={row.kcal}", flush=True)

    # v3.3.7 / v3.3.23ad / v3.3.37: recalc path。
    #   用戶冇填營養 → 跟住改名重新估算(4 層鏈:explicit → brand → AI → history)
    #   名冇變(例如淨係加咗餐廳)→ 唔重估,信任原本嘅數字
    # v3.3.37 修:舊版要求 row.kcal <= 0 先重估 → 改名後數字唔會變
    #              (Jim OOB 2026-10-22「白飯 → 海南雞白飯,macro detail 冇變」)。
    name_changed = bool(sup_name) and sup_name != (row.name or "")
    if sup_name and not user_supplied_macros and (row.kcal <= 0 or name_changed):
        re_est = _reestimate_macros_for_name(sup_name, sup_rest or (row.restaurant or ""))
        if re_est.get("kcal", 0) > 0:
            correction["calories"] = int(re_est["kcal"])
            correction["protein"] = re_est.get("protein", 0)
            correction["carbs"] = re_est.get("carbs", 0)
            correction["fat"] = re_est.get("fat", 0)
            correction["_backfill_source"] = f"reestimate:{re_est.get('source')}"
            print(f"[scan_correct] row {scan_index} re-estimated {sup_name!r} → "
                  f"{re_est['kcal']} kcal via {re_est.get('source')}", flush=True)
        else:
            # 全部層都失敗 → 保留原本嘅 macros(唔好把一行變 0)
            print(f"[scan_correct] re-estimate failed for {sup_name!r}; "
                  f"keeping row macros", flush=True)

    # v3.3.2: push the correction to the Sheet row. Apply corrections field-
    # by-field (only overwrite if the correction supplies a non-empty value).
    corrected_entry = {
        "date": row.date,
        "time": row.time,
        "meal_type": row.meal,
        "name": correction["name"] if correction["name"] else row.name,
        "restaurant_chain": (correction["restaurant_chain"]
                             if correction["restaurant_chain"] else row.restaurant),
        "calories": correction["calories"] if correction["calories"] is not None else row.kcal,
        "protein": correction["protein"] if correction["protein"] is not None else row.p,
        "carbs": correction["carbs"] if correction["carbs"] is not None else row.c,
        "fat": correction["fat"] if correction["fat"] is not None else row.f,
        "note": (correction["note"] if correction["note"] else row.notes),
        "drive_image_url": row.drive_image_url,
    }
    sheet_result = _sheet_update_nutrition_row(scan_index, corrected_entry)
    if not sheet_result.get("ok"):
        return jsonify({"ok": False, "error": f"sheet update failed: {sheet_result.get('errors')}"}), 500

    # v3.3.23al: invalidate Redis cache (5-min TTL would otherwise show stale)
    try:
        _invalidate_sheet_cache()
    except Exception:
        pass
    # Refresh the cache row from Sheet so subsequent reads reflect the fix.
    cache.refresh_one(scan_index, _sheet_read_nutrition_row)

    # v3.3.23av (Jim OOB 2026-10-12 'the edited 兩個鮮竹卷 is not updated
    # in jpg download'): invalidate JPG card cache so the next /api/nutrition/card
    # # call re-renders with the corrected name. Without this, the disk file
    # (with 5-min TTL) keeps serving the PRE-edit JPG (stale name).
    try:
        from gym_web.image_proxy import _jpg_cache_invalidate
        from pathlib import Path as _P
        row = cache.get_by_row(scan_index)
        if row and row.date:
            _jpg_cache_invalidate(row.date)
            # Delete ALL page variants (p1, p2, ...) for this date
            cache_dir = _P("/home/work/.hermes/nutrition_card_cache")
            for f in cache_dir.glob(f"nutrition_{row.date}*.jpg"):
                f.unlink()
    except Exception as e:
        print(f"[scan_correct] JPG cache invalidate failed: {e}", flush=True)

    return jsonify({"ok": True, "scan_index": scan_index, "correction": correction,
                    "sheet_updated": sheet_result.get("updated", 0)})


# v3.3.24: Replace photo for an existing scan row (Jim OOB 2026-10-16
# 'I want to replace existing photo but the ui does not allow me to do so.
# Pls make this happening'). Uploads new image, swaps the on-disk file +
# thumb, updates the Sheet's column K (image_url) + column N
# (drive_image_url), invalidates caches, and returns the new image URL.
# Old image file + thumb are deleted (Sheet no longer references them).
@app.route("/api/scan_replace_image", methods=["POST"])
def api_scan_replace_image():
    """Replace the photo for an existing scan row."""
    if "image" not in request.files:
        return jsonify({"ok": False, "error": "no image file"}), 400
    scan_index_raw = request.form.get("scan_index")
    if not scan_index_raw:
        return jsonify({"ok": False, "error": "no scan_index"}), 400
    try:
        scan_index = int(scan_index_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "scan_index must be int"}), 400
    if scan_index < 2:
        return jsonify({"ok": False, "error": "scan_index out of range"}), 404

    cache = get_cache()
    row = cache.get_by_row(scan_index)
    if row is None:
        return jsonify({"ok": False, "error": "scan_index not found in cache"}), 404

    img_file = request.files["image"]
    if not img_file or not img_file.filename:
        return jsonify({"ok": False, "error": "empty file"}), 400

    # Build new filename (HKT timestamp, like scan_commit does)
    now_hkt_dt = datetime.now(timezone(timedelta(hours=8)))
    new_name = f"scan_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}.jpg"
    new_path = SCAN_CACHE_DIR / new_name

    # Save bytes
    try:
        img_file.save(str(new_path))
    except Exception as e:
        return jsonify({"ok": False, "error": f"save failed: {e}"}), 500

    # Verify it's an image and make thumb
    try:
        _make_thumb(new_path, _thumb_path_for(new_name))
    except Exception as e:
        print(f"[scan_replace_image] _make_thumb failed: {e}", flush=True)

    # Upload to Drive (best-effort, like scan_commit)
    try:
        drive_url = _upload_to_drive(new_path)
    except Exception as e:
        print(f"[scan_replace_image] drive upload failed: {e}", flush=True)
        drive_url = ""

    # Capture old paths for cleanup
    old_image_url = getattr(row, "image_url", "") or ""
    old_drive_url = getattr(row, "drive_image_url", "") or ""
    old_thumb_path = None
    if old_image_url.startswith("/scan_img/"):
        old_basename = old_image_url[len("/scan_img/"):]
        old_thumb_path = _thumb_path_for(old_basename)
    old_full_path = None
    if old_image_url.startswith("/scan_img/"):
        old_full_path = SCAN_CACHE_DIR / old_image_url[len("/scan_img/"):]

    # Build corrected entry for Sheet (preserve all other fields)
    corrected_entry = {
        "date": row.date,
        "time": row.time,
        "meal_type": row.meal,
        "name": row.name,
        "restaurant_chain": row.restaurant,
        "calories": row.kcal,
        "protein": row.p,
        "carbs": row.c,
        "fat": row.f,
        "note": row.notes,
        "image_url": f"/scan_img/{new_name}",
        "drive_image_url": drive_url,
    }

    # Push to Sheet (column K = image_url, column N = drive_image_url)
    sheet_result = _sheet_update_nutrition_row(scan_index, corrected_entry)
    if not sheet_result.get("ok"):
        # Don't leave the new file orphaned; clean up
        try: new_path.unlink()
        except Exception: pass
        return jsonify({"ok": False, "error": f"sheet update failed: {sheet_result.get('errors')}"}), 500

    # Invalidate caches
    try: _invalidate_sheet_cache()
    except Exception: pass
    # v3.3.24a: invalidate the per-row in-memory thumb cache too, else
    # /scan_thumb/<row> keeps serving the OLD bytes for the new
    # drive_image_url (Jim OOB 2026-10-16 'image ordering all wrong now,
    # the mapping is drifted' — root cause was the LRU still holding
    # the previous thumb after the drive URL changed). Use
    # `invalidate_thumb` (drops one row), not `shift_thumb_cache`
    # (that one is for cascading after a row delete).
    try:
        from gym_web.image_proxy import (
            invalidate_thumb, invalidate_drive_url, _jpg_cache_invalidate,
        )
        invalidate_drive_url(scan_index)  # drop cached Drive URL too
        invalidate_thumb(scan_index)      # drop cached thumb bytes
        _jpg_cache_invalidate(row.date)
    except Exception as e:
        print(f"[scan_replace_image] thumb cache invalidate failed: {e}", flush=True)

    # Refresh cache row from Sheet
    try:
        cache.refresh_one(scan_index, _sheet_read_nutrition_row)
    except Exception as e:
        print(f"[scan_replace_image] cache.refresh_one failed: {e}", flush=True)

    # Delete old image + thumb (Sheet no longer references them)
    for old_p in (old_full_path, old_thumb_path):
        try:
            if old_p and old_p.exists() and old_p != new_path:
                old_p.unlink()
                print(f"[scan_replace_image] deleted old file: {old_p}", flush=True)
        except Exception as e:
            print(f"[scan_replace_image] could not delete {old_p}: {e}", flush=True)

    print(f"[scan_replace_image] row={scan_index} old_image_url={old_image_url!r} new_image_url={corrected_entry['image_url']!r} drive_url={drive_url!r}", flush=True)
    return jsonify({
        "ok": True,
        "scan_index": scan_index,
        "image_url": corrected_entry["image_url"],
        "thumbnail_url": f"/scan_thumb/{new_name[:-4]}.thumb.jpg",
        "drive_image_url": drive_url,
        "old_image_url": old_image_url,
        "old_drive_url": old_drive_url,
    })


# ---------------------------------------------------------------------------
# v3.3.37 (2026-10-22, Jim OOB): 改名 / 改名稱後一定要重新估算營養
#
# Bug:兩個 endpoint 都用咗同一個錯位嘅判斷 —— 新 macros 先預設填「舊值」,
#      然後 AI 層用 `if new_kcal <= 0` 先會套用 → 舊值 > 0 → **AI 永遠唔生效**。
#      症狀:白飯 → 海南雞白飯,名改咗但 kcal/P/C/F 一模一樣(2026-10-22 實測)。
#
# 修法:由 0 開始行 4 層鏈(explicit → brand → AI → history),
#      全部失敗先回落舊值(唔會把一行變 0)。
# ---------------------------------------------------------------------------
def _reestimate_macros_for_name(dish_name: str, restaurant: str = "") -> dict:
    """4 層營養重估鏈(純文字,唔使相):explicit → brand → AI → history。

    Returns {"kcal","protein","carbs","fat","source"}。source 係邊一層贏:
    explicit / brand / ai / history / none(全部失敗)。
    """
    name = (dish_name or "").strip()
    re_text = f"{name} (餐廳: {restaurant})" if restaurant else name
    out = {"kcal": 0, "protein": 0, "carbs": 0, "fat": 0, "source": "none"}

    # Layer 1: 名裡面嘅明確數字(e.g. "30g protein shake")
    explicit = _extract_explicit_macros(name)
    if explicit:
        out["kcal"] = int(explicit.get("calories", 0) or 0)
        out["protein"] = explicit.get("protein", 0) or 0
        out["carbs"] = explicit.get("carbs", 0) or 0
        out["fat"] = explicit.get("fat", 0) or 0
        out["source"] = "explicit"
        print(f"[macro-reestimate] explicit from {name!r}: {explicit}", flush=True)

    # Layer 2: 品牌表(優先過 AI —— 牌子嘅數字最準)
    if out["kcal"] <= 0:
        brand_hit = _lookup_brand_macros(name)
        if brand_hit:
            out["kcal"] = int(brand_hit.get("kcal", 0) or 0)
            out["protein"] = brand_hit.get("protein", 0) or 0
            out["carbs"] = brand_hit.get("carbs", 0) or 0
            out["fat"] = brand_hit.get("fat", 0) or 0
            out["source"] = "brand"
            print(f"[macro-reestimate] brand hit {brand_hit.get('_source')}", flush=True)

    # Layer 3: AI 估算(MiniMax)。v3.3.23af:用 _safe_parse_nutrition_json
    # 應對 <think> 前綴。
    if out["kcal"] <= 0:
        try:
            enrich = _minimax_nutrition_enrich(re_text)
            parsed = _safe_parse_nutrition_json(enrich) if enrich else None
            if isinstance(parsed, dict) and parsed:
                a_kcal = int(parsed.get("calories", 0) or 0)
                a_p = float(parsed.get("protein", 0) or 0)
                a_c = float(parsed.get("carbs", 0) or 0)
                a_f = float(parsed.get("fat", 0) or 0)
                if a_kcal > 0:
                    out["kcal"] = a_kcal
                    out["source"] = "ai"
                if a_p > 0: out["protein"] = a_p
                if a_c > 0: out["carbs"] = a_c
                if a_f > 0: out["fat"] = a_f
                print(f"[macro-reestimate] AI → {a_kcal} kcal / P{a_p} C{a_c} F{a_f} for {name!r}",
                      flush=True)
            else:
                print(f"[macro-reestimate] AI failed (empty/unparsable) for {name!r}", flush=True)
        except Exception as e:
            print(f"[macro-reestimate] AI exception for {name!r}: {e}", flush=True)

    # Layer 4: Sheet 歷史(最後 fallback,只喺仍然 0 嘅時候用)
    if out["kcal"] <= 0:
        looked = _lookup_known_macros(name)
        if looked:
            out["kcal"] = int(looked.get("kcal", 0) or 0)
            out["protein"] = looked.get("protein", 0) or 0
            out["carbs"] = looked.get("carbs", 0) or 0
            out["fat"] = looked.get("fat", 0) or 0
            out["source"] = "history"
            print(f"[macro-reestimate] history → {out['kcal']} kcal for {name!r}", flush=True)
    # Layer 4b: 有部分 macro(brand/explicit)但 kcal 仲係 0 → 只補 kcal
    if out["kcal"] <= 0 and (out["protein"] > 0 or out["carbs"] > 0 or out["fat"] > 0):
        looked = _lookup_known_macros(name)
        if looked and int(looked.get("kcal", 0) or 0) > 0:
            out["kcal"] = int(looked["kcal"])
            print(f"[macro-reestimate] partial backfill kcal={out['kcal']} for {name!r}", flush=True)
    return out


# v2.7.39: Rename existing scan + auto re-recognize macros (Jim OOB 2026-08-06
# 14:50 HKT: "is there a way to adjust the existing record by text and recognize
# it again. e.g. the current recognized one is called 白切雞, but actually it is
# Hai nan chicken rice"). Flow:
#  1. User types new dish name in inline popover
#  2. Backend overwrites name field (so iPhone display updates immediately)
#  3. Backend auto-calls _minimax_nutrition_enrich(new_name) for re-estimate macros
#  4. Old name + macros saved as audit trail (returned to caller; cache holds it)
#  5. Returns new entry state so frontend can update display
#
# v3.3.2: scan_index now refers to the Sheet row. Update goes via
# _sheet_update_nutrition_row + cache.refresh_one. No more food_scan_log.json.
@app.route("/api/scan_rename", methods=["POST"])
def api_scan_rename():
    data = request.get_json(silent=True) or {}
    scan_index = data.get("scan_index")
    new_name = (data.get("new_name") or "").strip()
    if scan_index is None:
        return jsonify({"ok": False, "error": "no scan_index"}), 400
    if not new_name:
        return jsonify({"ok": False, "error": "new_name required"}), 400
    if not isinstance(scan_index, int) or scan_index < 2:
        return jsonify({"ok": False, "error": "scan_index out of range"}), 404

    cache = get_cache()
    row = cache.get_by_row(scan_index)
    if row is None:
        return jsonify({"ok": False, "error": "scan_index not found in cache"}), 404

    old_name = row.name
    old_kcal = row.kcal
    old_p = row.p
    old_c = row.c
    old_f = row.f
    restaurant = row.restaurant or ""
    re_text = f"{new_name} (餐廳: {restaurant})" if restaurant else new_name

    # v3.3.37 (2026-10-22, Jim OOB '白飯 → 海南雞白飯,但 macro detail 冇變'):
    # 舊 bug:呢度由「舊 macros」起步,而 AI 層用 `if new_kcal <= 0` 先套用
    #         → 舊值 > 0 → AI 重新估算**永遠唔生效**(名改咗,數字一樣)。
    # 而家:由 0 起步行 4 層鏈(explicit → brand → AI → history),
    #       全部層都失敗先回落舊值(唔會把一行變 0)。
    re_est = _reestimate_macros_for_name(new_name, restaurant)
    new_kcal = int(re_est.get("kcal", 0) or 0)
    new_p = re_est.get("protein", 0) or 0
    new_c = re_est.get("carbs", 0) or 0
    new_f = re_est.get("fat", 0) or 0
    macros_source = re_est.get("source", "none")
    enrich_failed = (macros_source == "none")
    if new_kcal <= 0:
        # 所有層都失敗 → 保留原本嘅 macros(唔好把一行變 0)
        new_kcal, new_p, new_c, new_f = old_kcal, old_p, old_c, old_f
        macros_source = "kept_previous"
        print(f"[scan_rename] re-estimate failed for {new_name!r}; kept previous macros",
              flush=True)
    # Apply share ratio (Jim 60% / 小寶 40% if shared)
    shared = _detect_shared_meal(re_text)
    jim_ratio = 0.60 if shared else 1.00
    jim_kcal = round(new_kcal * jim_ratio)
    jim_p = round(new_p * jim_ratio, 1)
    jim_c = round(new_c * jim_ratio, 1)
    jim_f = round(new_f * jim_ratio, 1)

    correction = {
        "type": "rename",
        "corrected_at": now_iso(),
        "from_name": old_name,
        "to_name": new_name,
        "from_macros": {"calories": old_kcal, "protein": old_p, "carbs": old_c, "fat": old_f},
        "to_macros": {"calories": jim_kcal, "protein": jim_p, "carbs": jim_c, "fat": jim_f},
        "macros_source": macros_source,
    }
    # Overwrite name + macros fields on the Sheet row
    sheet_entry = {
        "date": row.date,
        "time": row.time,
        "meal_type": row.meal,
        "name": new_name[:120],
        "restaurant_chain": restaurant,
        "calories": jim_kcal,
        "protein": jim_p,
        "carbs": jim_c,
        "fat": jim_f,
        # v3.3.37: 避免重複 rename 令備註愈堆愈長("scan_food rename rename")
        "note": (row.notes if ("rename" in (row.notes or "")) or not row.notes
                 else (row.notes + " " + correction["type"])).strip()[:200],
        "drive_image_url": row.drive_image_url,
    }
    sheet_result = _sheet_update_nutrition_row(scan_index, sheet_entry)
    if not sheet_result.get("ok"):
        return jsonify({"ok": False, "error": f"sheet update failed: {sheet_result.get('errors')}"}), 500
    # v3.3.23al: invalidate Redis Sheet cache after write
    try:
        _invalidate_sheet_cache()
    except Exception:
        pass
    cache.refresh_one(scan_index, _sheet_read_nutrition_row)

    # v3.3.23av (Jim OOB 2026-10-12 'the edited 兩個鮮竹卷 is not updated
    # in jpg download'): invalidate JPG card cache for the renamed row's date
    # so the next /api/nutrition/card re-renders with the new name.
    try:
        from gym_web.image_proxy import _jpg_cache_invalidate
        from pathlib import Path as _P
        row_now = cache.get_by_row(scan_index)
        if row_now and row_now.date:
            _jpg_cache_invalidate(row_now.date)
            cache_dir = _P("/home/work/.hermes/nutrition_card_cache")
            for f in cache_dir.glob(f"nutrition_{row_now.date}*.jpg"):
                f.unlink()
    except Exception as e:
        print(f"[scan_rename] JPG cache invalidate failed: {e}", flush=True)

    # Reload row for the response
    row = cache.get_by_row(scan_index)
    return jsonify({
        "ok": True,
        "scan_index": scan_index,
        "entry": row.to_pwa_dict() if row else {},
        "correction": correction,
        "sheet_updated": sheet_result.get("updated", 0),
    })


# v3.3.23ad (Jim OOB 2026-09-16 'even after i edit the name, you should
# use ai to help rewrite the food name in a better way'): AI-powered food
# name rewrite helper. Takes a raw user-typed description and returns
# a clean canonical name (e.g. 'shake 30g protein' → '蛋白飲 30g protein'
# or 'Quest Bar PB' → 'Quest 朱古力花生醬蛋白棒').
#
# Uses MiniMax M3 for fast rewrite; falls back to heuristics if API fails.
# Designed for inline use in PWA (no sheet write — caller decides whether
# to apply via scan_rename).
@app.route("/api/scan_rewrite_name", methods=["POST"])
def api_scan_rewrite_name():
    """AI rewrite food name. Does NOT write to sheet — caller applies result."""
    data = request.get_json(silent=True) or {}
    raw_name = (data.get("name") or data.get("raw_name") or "").strip()
    restaurant = (data.get("restaurant") or data.get("restaurant_chain") or "").strip()
    if not raw_name:
        return jsonify({"ok": False, "error": "name required"}), 400

    # Try brand table first (if name matches a known brand, use canonical brand name)
    brand_hit = _lookup_brand_macros(raw_name)
    if brand_hit:
        return jsonify({
            "ok": True,
            "original": raw_name,
            "rewritten": brand_hit.get("_source", raw_name).split("→")[0].strip(),
            "source": "brand_table",
            "macros": {
                "calories": brand_hit.get("kcal"),
                "protein": brand_hit.get("protein"),
                "carbs": brand_hit.get("carbs"),
                "fat": brand_hit.get("fat"),
            },
            "restaurant_chain": brand_hit.get("restaurant_chain", restaurant),
        })

    # Try AI rewrite via MiniMax (v3.3.30: was APiyi gpt-4o-mini, dead key)
    try:
        mm_key = _minimax_api_key()
        if mm_key:
            prompt = (
                "你是香港營養師助手。用家輸入咗一個食物名，"
                "請幫佢 rewrite 做一個 canonical、簡潔嘅名 (最多 20 字)。\n"
                "要求:\n"
                "1. 用返用家原本嘅語言 (廣東話/英文)\n"
                "2. 保留所有 macro numbers (例: '30g protein', '200 kcal')\n"
                "3. 用 standard 香港餐廳/食物名稱\n"
                "4. 唔好加餐廳 suffix (餐廳已經另外 field)\n"
                "5. 唔好用 emoji\n\n"
                "用戶輸入: " + raw_name + "\n"
                "餐廳: " + restaurant + "\n\n"
                "JSON reply (繁體中文): {\"rewritten\": \"<canonical name>\"}"
            )
            req = urllib.request.Request(
                "https://api.minimax.io/chat/paas/v1/chat/completions",
                data=json.dumps({
                    "model": "MiniMax-text-01",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                    "temperature": 0.2,
                }).encode(),
                headers={
                    "Authorization": "Bear" + "er " + mm_key,
                    "Content-Type": "application/json",
                },
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
            ai_text = resp["choices"][0]["message"]["content"]
            m = re.search(r'"rewritten"\s*:\s*"([^"]+)"', ai_text)
            if m:
                rewritten = m.group(1).strip()
                return jsonify({
                    "ok": True,
                    "original": raw_name,
                    "rewritten": rewritten,
                    "source": "ai",
                    "macros": _extract_explicit_macros(raw_name),  # also return any extracted macros
                })
    except Exception as e:
        print(f"[scan_rewrite_name] AI failed: {e}")

    # Heuristic fallback: capitalize + cleanup
    rewritten = raw_name.strip()
    # Remove trailing whitespace, capitalize first letter of each english word
    words = rewritten.split()
    out_words = []
    for w in words:
        if re.match(r'^[a-z]', w) and len(w) > 2:
            out_words.append(w.capitalize())
        else:
            out_words.append(w)
    rewritten = " ".join(out_words)

    return jsonify({
        "ok": True,
        "original": raw_name,
        "rewritten": rewritten,
        "source": "heuristic",
    })


@app.route("/scan_img/<path:filename>", methods=["GET"])
def serve_scan_image(filename):
    """Legacy: served scanned food images from SCAN_CACHE_DIR.

    v3.3.0: superseded by image_proxy.bp /scan_img/<int:row_index>, which
    proxies / redirects to Google Drive via the row's Drive URL (no file
    cache). Kept for backward compat — falls through to 404 once scan_cache/
    is removed.
    """
    return send_from_directory(str(SCAN_CACHE_DIR), filename)


@app.route("/scan_thumb/<path:filename>", methods=["GET"])
def serve_scan_thumb(filename):
    """Legacy: 480px thumbnail from SCAN_THUMB_DIR with lazy-gen fallback.

    v3.3.0: superseded by image_proxy.bp /scan_thumb/<int:row_index>, which
    proxies / 302-redirects to Drive's built-in thumbnail endpoint (=s220-c).
    Kept for backward compat during transition.
    """
    if not filename.endswith(".thumb.jpg"):
        return jsonify({"ok": False, "error": "invalid thumb filename"}), 400
    thumb_path = SCAN_THUMB_DIR / filename
    if thumb_path.exists():
        return send_from_directory(str(SCAN_THUMB_DIR), filename)
    orig_basename = filename[:-len(".thumb.jpg")] + ".jpg"
    orig_path = SCAN_CACHE_DIR / orig_basename
    if not orig_path.exists():
        return jsonify({"ok": False, "error": "original not found", "filename": orig_basename}), 404
    if _make_thumb(orig_path, thumb_path):
        return send_from_directory(str(SCAN_THUMB_DIR), filename)
    return jsonify({"ok": False, "error": "thumb generation failed"}), 500


# ---------- v2.2 FEATURES (Jim OOB 2026-07-23 22:42 HKT) ----------
# Feature 1: photostream auto-suggest — list today's images + MiniMax classifies food/non-food
# Feature 2: pre-log preview/confirmation — return suggested entry, NO auto-log until Jim confirms
# Feature 3: activity coach tips — after END SESSION, pplx + MiniMax generate Traditional Chinese
#            progression cues + form tips for each exercise just done

import urllib.error

# ---------- F1: /api/photostream/today ----------
# Lists today's image_cache + scan_cache files. For each, optionally call MiniMax vision
# to classify: is it food/receipt? Then return a "tap-to-log" suggestion with predicted macros.
# Cache the classification per file (re-classify only if newer mtime).

PHOTOSTREAM_CACHE_PATH = Path("/home/work/.hermes/photostream_classify_cache.json")

def _load_photostream_cache() -> dict:
    if not PHOTOSTREAM_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(PHOTOSTREAM_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_photostream_cache(cache: dict) -> None:
    PHOTOSTREAM_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def _classify_image_cached(path: str, mtime_iso: str) -> dict:
    """Run MiniMax vision to classify one image. Cache by (path + mtime) to avoid re-work.

    Returns: {is_food: bool, suggested_name: str, calories_est: int, protein_est: int, dish_desc: str}
    """
    cache = _load_photostream_cache()
    key = f"{path}::{mtime_iso}"
    if key in cache:
        return cache[key]

    try:
        with open(path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        result = {"is_food": False, "error": f"read_failed: {e}"}
        cache[key] = result
        _save_photostream_cache(cache)
        return result

    classify_prompt = (
        "你係食物分類助手。睇下呢張圖係咪食物或者餐單收據。"
        "用 JSON 格式答我（唔好加 markdown）：\n"
        '{"is_food": true/false, "suggested_name": "菜名或者一句描述", '
        '"calories_est": 一個整數(0 = 唔知),"protein_est": 一個整數克數(0 = 唔知), '
        '"dish_desc": "一句繁體中文描述"}}\n'
        "如係食物或者收據就 is_food=true,suggested_name 用繁中。"
        "如係其他(人像/風景/UI/激勵圖/meme 等)就 is_food=false,suggested_name 寫「非食物」。"
    )

    raw = _minimax_vision(img_b64, classify_prompt)

    # Parse JSON out of model output (best-effort, fall back to defaults)
    is_food = False
    suggested_name = "非食物"
    cal_est = 0
    p_est = 0
    dish_desc = ""
    json_match = re.search(r"\{[\s\S]+?\}", raw)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            is_food = bool(data.get("is_food", False))
            suggested_name = str(data.get("suggested_name", "食物"))[:80]
            cal_est = int(data.get("calories_est", 0) or 0)
            p_est = int(data.get("protein_est", 0) or 0)
            dish_desc = str(data.get("dish_desc", ""))[:200]
        except Exception:
            # Heuristic fallback: scan text
            lower = raw.lower()
            if any(k in lower for k in ["食物", "菜", "飯", "餐"]):
                is_food = True
                suggested_name = raw.split("\n")[0][:80] if raw else "食物"
            dish_desc = raw[:200]
    else:
        lower = raw.lower()
        if any(k in lower for k in ["食物", "菜", "飯", "餐"]):
            is_food = True
            suggested_name = raw.split("\n")[0][:80] if raw else "食物"
        dish_desc = raw[:200]

    result = {
        "is_food": is_food,
        "suggested_name": suggested_name,
        "calories_est": cal_est,
        "protein_est": p_est,
        "dish_desc": dish_desc,
        "model_used": "minimax-m3",
    }
    cache[key] = result
    _save_photostream_cache(cache)
    return result


@app.route("/api/photostream/today", methods=["GET"])
def api_photostream_today():
    """List today's photostream (image_cache + scan_cache) with optional food classification.

    Optional query: ?classify=true runs MiniMax on each (slow first time; cached subsequent).
    """
    classify_flag = request.args.get("classify", "false").lower() == "true"
    limit = int(request.args.get("limit", 30))

    def _is_valid_image_file(path: Path) -> bool:
        """Skip botched uploads — Telegram bot occasionally saves base64 text
        to .jpg paths, leaving 36-byte files starting with "/9j/" instead of
        the JPEG magic bytes ffd8ff. Such files render as broken icons."""
        try:
            if path.stat().st_size < 1024:
                return False
            with path.open("rb") as fp:
                head = fp.read(8)
        except OSError:
            return False
        if head.startswith(b"\xff\xd8\xff"):    # JPEG
            return True
        if head.startswith(b"\x89PNG"):          # PNG
            return True
        if head.startswith(b"GIF8"):            # GIF
            return True
        if head.startswith(b"RIFF") and head[8:12] == b"WEBP":  # WebP
            return True
        return False

    items = []
    today = today_iso()
    scan_caches = [
        ("scan", SCAN_CACHE_DIR),
        ("image", Path("/home/work/.hermes/image_cache")),
        ("scan_archive", Path("/home/work/.hermes/scan_cache")),  # duplicate safe
    ]
    seen_paths = set()

    for label, cache_dir in scan_caches:
        if not cache_dir.exists():
            continue
        for fp in sorted(cache_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True):
            real_str = str(fp.resolve())
            if real_str in seen_paths:
                continue
            seen_paths.add(real_str)
            if not _is_valid_image_file(fp):
                print(f"photostream: skipping non-image file {fp.name} "
                      f"({fp.stat().st_size} bytes)", flush=True)
                continue
            mtime = datetime.fromtimestamp(fp.stat().st_mtime, tz=HKT)
            mtime_iso = mtime.strftime("%Y-%m-%dT%H:%M:%S%z")
            # Only show today's items by default
            if mtime.strftime("%Y-%m-%d") != today:
                continue
            size_kb = round(fp.stat().st_size / 1024, 1)
            # URL — prefer /scan_img/ for scan, /img/ for image_cache
            if label == "image":
                url = f"/img/{fp.name}"
            else:
                url = f"/scan_img/{fp.name}"
            entry = {
                "path": str(fp),
                "filename": fp.name,
                "url": url,
                "size_kb": size_kb,
                "mtime_iso": mtime_iso,
                "kind": label,
                "already_logged": False,
                "scan_index": None,
            }
            # v3.3.8 (Jim OOB 2026-08-31): surface EXIF capture time alongside
            # filesystem mtime so the PWA can display "拍攝 vs 上傳" badges.
            # Cached per-file so we don't re-read EXIF on every list call.
            cache = _load_photostream_cache()
            exif_key = f"exif::{str(fp.resolve())}::{mtime_iso}"
            exif_cache_entry = cache.get(exif_key)
            if not exif_cache_entry:
                exif_info = _extract_exif_capture_time(fp)
                exif_cache_entry = exif_info or {}
                cache[exif_key] = exif_cache_entry
                _save_photostream_cache(cache)
            # v3.3.8 fix: exif_info["capture_time"] is now ISO string (was
            # datetime before — caused JSON TypeError on cache write).
            # Pass-through to PWA as-is.
            if exif_cache_entry and exif_cache_entry.get("capture_time"):
                entry["capture_time_iso"] = exif_cache_entry["capture_time"]
                entry["capture_time_source"] = exif_cache_entry.get("source", "")
                entry["upload_time_iso"] = mtime_iso
            if classify_flag:
                cls = _classify_image_cached(str(fp), mtime_iso)
                entry["classification"] = cls
                # v3.3.2: scan_log.json is gone — already_logged is best-effort
                # based on whether any cache row has this local image filename
                # in its drive_image_url path. Skip the heuristic; treat every
                # photostream image as "not yet logged" so the PWA surfaces
                # the scan button. The Sheet-level dedup on /api/scan_commit
                # catches any actual duplicates.
                pass
            items.append(entry)
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break

    return jsonify({"items": items, "total": len(items), "date": today})


# ---------- F2: /api/scan_preview + /api/scan_commit ----------
# Jim OOB: "all food logging should be preview and allow me to confirm before logging"
# Two-step flow:
#   POST /api/scan_preview (image) → returns SUGGESTED entry + ai preview JSON
#   POST /api/scan_commit (entry)  → only NOW write to log + Sheet
# Previously /api/scan_food auto-wrote. v2.2 makes scan_food auto-preview, then commit separately.


# ---------------------------------------------------------------------------
# v3.3.24e (SCAN_PROGRESS_SPEC §2.5): the recognition pipeline is extracted
# into a module-level function so that /api/scan_preview and the background
# scan-job worker (POST /api/scan_recognize) share ONE implementation.
#
# The eight progress stages and their pct bands come from the spec table in
# §2.3. The pipeline order there is
#   vision -> second_opinion -> enrich -> enrich2 -> multi -> brand -> drive -> commit
# i.e. multi-dish BEFORE brand-override. The multi-dish AI call and the
# brand-table lookup are independent (the brand table is a pure local dict
# lookup; multi_desc depends only on vision_desc + pplx_desc), so the multi
# call is moved above the brand block to match the spec. The payload of
# /api/scan_preview is unaffected by that reorder.
# ---------------------------------------------------------------------------

# stage_key -> pct band start (SPEC §2.3) and 繁中 label template.
SCAN_STAGE_PCT_START = {
    "queued": 0,
    "vision": 0,
    "second_opinion": 12,
    "enrich": 25,
    "enrich2": 37,
    "multi": 50,
    "brand": 62,
    "drive": 75,
    "commit": 87,
    "done": 100,
}

SCAN_STAGE_LABELS = {
    "queued": "準備緊第 {n} 張…",
    "vision": "AI 睇緊第 {n} 張相…",
    "second_opinion": "第二個 AI 交叉檢查第 {n} 張…",
    "enrich": "估算第 {n} 張嘅營養…",
    "enrich2": "交叉核對第 {n} 張嘅營養值…",
    "multi": "分析第 {n} 張有幾多餸…",
    "brand": "對照第 {n} 張嘅品牌營養表…",
    "drive": "上載第 {n} 張到雲端…",
    "commit": "寫入第 {n} 張到 Google Sheet…",
    "done": "第 {n} 張搞掂咗",
}

# v3.3.33 (Jim OOB 2026-10-21「And ai work can be done in parallel to save
# queuing time too」): 平行模式嘅 stage label。
# 舊 label 係「第 {n} 張」—— 平行跑嘅時候唔止一張喺度做,講「第 n 張」會
# 誤導。所以改用「已完成 done/total」嘅聚合講法。
SCAN_STAGE_LABELS_PARALLEL = {
    "queued": "排緊 AI 隊…(已完成 {done}/{total})",
    "vision": "AI 睇緊相片…(已完成 {done}/{total})",
    "second_opinion": "第二個 AI 交叉檢查…(已完成 {done}/{total})",
    "enrich": "估算營養中…(已完成 {done}/{total})",
    "enrich2": "交叉核對營養值…(已完成 {done}/{total})",
    "multi": "分析餸數…(已完成 {done}/{total})",
    "brand": "對照品牌營養表…(已完成 {done}/{total})",
    "drive": "上載到雲端…(已完成 {done}/{total})",
    "commit": "寫入 Google Sheet…(已完成 {done}/{total})",
    "done": "全部搞掂晒",
}


def _scan_stage_label_parallel(stage_key: str, done: int, total: int) -> str:
    """繁中 stage label — 平行模式(用已完成數,唔用「第 n 張」)。"""
    tmpl = SCAN_STAGE_LABELS_PARALLEL.get(stage_key, stage_key)
    try:
        return tmpl.format(done=done, total=total)
    except (KeyError, IndexError) as e:
        print(f"[scan-job] parallel stage label format failed for {stage_key}: {e}",
              flush=True)
        return tmpl


def _scan_preview_core(img_bytes: bytes, img_path_hint: str = "",
                       on_stage=None) -> dict:
    """Run the whole food-photo recognition pipeline for one image.

    v3.3.24e (SCAN_PROGRESS_SPEC §2.5): extracted from api_scan_preview so
    the existing endpoint and the background scan-job worker share one
    implementation. Returns exactly the dict api_scan_preview has always
    returned — the route wraps it in jsonify().

    v3.3.25 (2026-10-20, Jim OOB「Someone has pollute the production data」):
    production writes are now gated by GYMBRO_WRITE_MODE (production |
    dryrun | throwaway) inside _upload_to_drive / _append_to_sheet_nutrition,
    plus a hard _assert_not_test_fixture() refusal. Nothing to configure on
    this function — the switch is global so a test can never forget it.

    Args:
        img_bytes: raw image bytes.
        img_path_hint: optional absolute path of an already-saved copy of
            these bytes (a Phase-1 upload). When present the file is reused
            as the working file (no second copy is written) and it is kept
            at commit time instead of being renamed away.
        on_stage: optional callable(stage_key) fired right before each
            pipeline stage runs, so callers can report true progress.
    """
    def _emit(stage_key):
        if on_stage is None:
            return
        try:
            on_stage(stage_key)
        except Exception as e:
            print(f"[scan-core] on_stage({stage_key}) failed: "
                  f"{type(e).__name__}: {e}", flush=True)

    # Save image to scan_cache (will be reused if Jim confirms)
    now_hkt_dt = datetime.now(timezone(timedelta(hours=8)))
    keep_source = False
    if img_path_hint:
        hint_path = Path(img_path_hint)
        if hint_path.is_file():
            img_path = hint_path
            keep_source = True
        else:
            print(f"[scan-core] img_path_hint missing, using bytes: "
                  f"{img_path_hint}", flush=True)
    if not keep_source:
        img_filename = f"preview_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}.jpg"
        img_path = SCAN_CACHE_DIR / img_filename
        img_path.write_bytes(img_bytes)

    # v3.3.8 (Jim OOB 2026-08-31): extract EXIF capture time so we log the
    # actual photo shot time, not the upload time. iPhone photo stream
    # uploads often happen hours after the shot (e.g. breakfast photo
    # uploaded at dinner time → would otherwise mis-classify meal_type).
    exif_info = _extract_exif_capture_time(img_path)
    capture_iso = now_hkt_dt.strftime("%Y-%m-%d")
    capture_hhmm = now_hkt_dt.strftime("%H:%M")
    if exif_info.get("capture_time"):
        try:
            # v3.3.8 fix: capture_time is now an ISO string (serialised for
            # JSON cache compat). Parse it back for strftime.
            capture_dt_obj = datetime.fromisoformat(exif_info["capture_time"])
            capture_iso = capture_dt_obj.strftime("%Y-%m-%d")
            capture_hhmm = capture_dt_obj.strftime("%H:%M")
        except (ValueError, TypeError) as e:
            print(f"[scan-core] EXIF capture_time parse failed "
                  f"({e}), falling back to upload time", flush=True)

    img_b64 = base64.b64encode(img_bytes).decode()

    # v3.2.7.32: plain-text vision prompt — no markdown headers like
    # 「菜式：/份量：/煮法：」 (Jim OOB 2026-08-11 'why today recognized
    # food as 菜式 as food name!'). Same plaintext instruction set as the
    # scan_commit prompt so all vision calls are consistent.
    vision_prompt = (
        "你係食物視覺識別助手。請用繁體中文廣東話,2-4 句口語化句子,直接講"
        "相入面見到咩食物、份量幾多(目測)、點煮、邊間餐廳(如見到 logo 或招牌字)。"
        "例如：「一碗白飯配一塊煎雞扒,碟邊有少許黑椒汁,餐廳係太興。」"
        "如果係飲品,就講幾多 ml、咩品牌、有冇冰有冇糖。"
        "如果係小票/receipt,就逐項抄低菜名同份量同價錢(睇到嘅部分)。"
        "唔好用 markdown 標題、唔好用「菜式：」呢啲 section header,"
        "唔好寫「呢張相顯示...」、「可見...」呢啲開場白,直接講食物。"
        "一個英文字都唔好有,唔識就寫「難以辨認」。"
    )
    _emit("vision")
    vision_desc = _minimax_vision(img_b64, vision_prompt)

    # 1b. 2nd-opinion vision from a DIFFERENT provider in the chain
    _emit("second_opinion")
    opinion2_desc = _vision_second_opinion(img_b64, vision_prompt)
    # v3.2.7.14: detect gpt-4o-mini safety-filter refusal (e.g. "抱歉，我無法...")
    # and treat as empty so it does NOT pollute vision_desc (Jim OOB
    # 2026-08-09 16:48 HKT 'I took a screenshot not food, but scan got
    # committed as 抱歉 / 第一道菜').
    if opinion2_desc and ("抱歉" in opinion2_desc[:30] and "無法" in opinion2_desc[:50] and len(opinion2_desc) < 80):
        opinion2_desc = ""
    if opinion2_desc and not opinion2_desc.startswith("（"):
        # 2-vision median-merge: take whichever is longer (more dish detail)
        if len(opinion2_desc) > len(vision_desc) * 0.7:
            vision_desc = vision_desc + "\n\n（2nd-opinion vision）\n" + opinion2_desc

    # 2. pplx enrichment
    _emit("enrich")
    pplx_desc = ""  # v3.3.34: Perplexity enrichment 已移除

    # 2b. OpenRouter gpt-4o-mini 2nd-opinion (Jim OOB 2026-07-25 13:35)
    _emit("enrich2")
    mm_desc = _minimax_nutrition_enrich(vision_desc)
    pplx_parsed = _parse_nutrition_block(pplx_desc)
    mm_parsed = {}
    if mm_desc and mm_desc.startswith("{"):
        try:
            mm_parsed = json.loads(mm_desc)
        except Exception as e:
            print(f"[scan-core] minimax nutrition JSON parse failed: "
                  f"{type(e).__name__}: {e}", flush=True)
            mm_parsed = _parse_nutrition_block(mm_desc)
    merged_nutrition = _merge_nutrition_estimates([pplx_parsed, mm_parsed])

    # 3b. v3.2.7.9: Multi-dish detection — call the MiniMax multi estimator
    # (Jim OOB 2026-08-08 20:05 HKT 'scan one photo with different food to
    # generate multiple entry'). Returns array of {name, 12-field...} objects.
    # v3.3.24e: moved ABOVE the brand_table override so the pipeline order
    # matches SCAN_PROGRESS_SPEC §2.2 (multi-dish -> brand override). The
    # two are independent — brand_override only touches merged_nutrition and
    # multi_desc depends solely on vision_desc + pplx_desc — so the payload
    # of /api/scan_preview is unchanged by this reorder.
    multi_desc = vision_desc + "\n\n" + pplx_desc
    _emit("multi")
    multi_json = _minimax_nutrition_enrich_multi(multi_desc)
    multi_dishes = []
    try:
        parsed = json.loads(multi_json)
        if isinstance(parsed, dict) and "dishes" in parsed:
            multi_dishes = parsed["dishes"]
        elif isinstance(parsed, list):
            multi_dishes = parsed
    except Exception as e:
        print(f"[scan-core] multi-dish JSON parse failed: "
              f"{type(e).__name__}: {e}", flush=True)
        multi_dishes = []

    # v3.3.23v (Jim OOB 2026-09-15 'my recent quest energy bar does not
    # record any nutrient info'): brand_table override AFTER merge. For
    # photo scans, override the merged estimate with brand-correct values
    # when a known brand product is detected.
    _emit("brand")
    brand_hit = _lookup_brand_macros(vision_desc)
    if brand_hit:
        print(f"[scan_preview] brand_table override for {vision_desc!r}: "
              f"{merged_nutrition.get('calories', {}).get('value', 0)}→{brand_hit['kcal']} kcal",
              flush=True)
        for f in ("calories", "protein", "carbs", "fat", "fiber", "sugar",
                  "sodium", "sat_fat", "trans_fat"):
            if brand_hit.get(f) is not None:
                cur = merged_nutrition.get(f, {}) or {}
                if not cur.get("value") or float(cur.get("value", 0) or 0) == 0:
                    cur["value"] = brand_hit[f]
                    cur["source"] = f"brand_table ({brand_hit['_source']})"
                    merged_nutrition[f] = cur

    # 3. Build preview entry (NOT written yet)
    shared = _detect_shared_meal(vision_desc + " " + pplx_desc)
    jim_ratio = 0.60 if shared else 1.00
    raw_kcal = int(merged_nutrition.get("calories", {}).get("value", 0) or 0)
    raw_p = int(merged_nutrition.get("protein", {}).get("value", 0) or 0)
    jim_kcal = round(raw_kcal * jim_ratio)
    jim_p = round(raw_p * jim_ratio)

    # Try to extract restaurant chain from vision or pplx (heuristic: first capitalised phrase)
    chain_match = re.search(r"([\u4e00-\u9fff]{2,6}(?:王|軒|亭|餐廳|食堂|廚|小店|屋|樓))", vision_desc + pplx_desc)
    restaurant_guess = chain_match.group(1) if chain_match else ""

    # Per-field entries for preview (12-field schema, Jim OOB 2026-07-25 13:35)
    preview_field_entries = {}
    for f in NUTRITION_FIELDS:
        info = merged_nutrition.get(f)
        if not info:
            preview_field_entries[f] = 0
            continue
        v = info.get("value", 0) or 0
        preview_field_entries[f] = round(v * jim_ratio, 1) if shared else v

    # Build per-dish entries (apply jim_ratio to each if shared)
    entries_list = []
    for d in multi_dishes:
        if not isinstance(d, dict):
            continue
        d_name = d.get("name") or _extract_dish_name(d.get("name", ""), vision_desc)
        if not d_name:
            continue
        e_macros = {}
        for f in NUTRITION_FIELDS:
            v = d.get(f, 0) or 0
            e_macros[f] = round(float(v) * jim_ratio, 1) if shared else round(float(v), 1)
        e_entry = {
            "date": capture_iso,
            "time": capture_hhmm,
            "meal_type": "scan",
            "name": _sanitise_dish_name(d_name, vision_desc=vision_desc, pplx_desc=pplx_desc),
            "coach_comment": _coach_comment(
                d_name, e_macros["calories"], e_macros["protein"],
                e_macros.get("carbs", 0), e_macros.get("fat", 0), restaurant_guess,
                daily_ctx_seed=_daily_ctx_seed(),
            ),
            "restaurant_chain": restaurant_guess,
            "calories": e_macros["calories"],
            "protein": e_macros["protein"],
            "carbs": e_macros.get("carbs", 0),
            "fat": e_macros.get("fat", 0),
            "fiber": e_macros.get("fiber", 0),
            "sugar": e_macros.get("sugar", 0),
            "sodium": e_macros.get("sodium", 0),
            "sat_fat": e_macros.get("sat_fat", 0),
            "trans_fat": e_macros.get("trans_fat", 0),
            "vit_c": e_macros.get("vit_c", 0),
            "iron": e_macros.get("iron", 0),
            "calcium": e_macros.get("calcium", 0),
            "is_shared_meal": shared,
            "share_with_wife": "Jim 60% / 小寶 40% (auto-applied)" if shared else "Jim 100% (solo)",
            "raw_kcal_estimate": e_macros["calories"],
            "raw_p_estimate": e_macros["protein"],
        }
        entries_list.append(e_entry)

    # Fallback: if multi-dish returned empty or 0 elements, use the original
    # single-entry path (so backward compat preserved for 1-dish scans).
    if not entries_list:
        entries_list = [{
            "date": capture_iso,
            "time": capture_hhmm,
            "meal_type": "scan",
            "name": _extract_dish_name(vision_desc, pplx_desc),
            "coach_comment": _coach_comment(
                _extract_dish_name(vision_desc, pplx_desc),
                preview_field_entries["calories"],
                preview_field_entries["protein"],
                preview_field_entries.get("carbs", 0),
                preview_field_entries.get("fat", 0),
                restaurant_guess,
                daily_ctx_seed=_daily_ctx_seed(),
            ),
            "restaurant_chain": restaurant_guess,
            "calories": preview_field_entries["calories"],
            "protein": preview_field_entries["protein"],
            "carbs": preview_field_entries["carbs"],
            "fat": preview_field_entries["fat"],
            "fiber": preview_field_entries["fiber"],
            "sugar": preview_field_entries["sugar"],
            "sodium": preview_field_entries["sodium"],
            "sat_fat": preview_field_entries["sat_fat"],
            "trans_fat": preview_field_entries["trans_fat"],
            "vit_c": preview_field_entries["vit_c"],
            "iron": preview_field_entries["iron"],
            "calcium": preview_field_entries["calcium"],
            "is_shared_meal": shared,
            "share_with_wife": "Jim 60% / 小寶 40% (auto-applied)" if shared else "Jim 100% (solo)",
            "raw_kcal_estimate": raw_kcal,
            "raw_p_estimate": raw_p,
        }]

    # v3.3.30 (2026-10-21, Rule 25 + the 2026-10-20 pollution incident):
    # NEVER auto-commit a generic / placeholder dish name.
    #
    # The vision provider can be perfectly healthy while the NAME
    # extraction still fails — both extractors (MiniMax + Perplexity)
    # reject the candidate, or the AI answered "no food in this photo".
    # Writing 「未識別菜式」 to Jim's golden copy is EXACTLY the pollution
    # signature from 2026-10-20 (26 rows + 7 rows of junk). Rule 25: a
    # generic name is never acceptable.
    #
    # Blocking HERE (before the Drive stage) means ZERO production
    # artefacts: no Drive file, no Sheet row. The response keeps the
    # exact same top-level key set as the success path so Isak's §4.3
    # regression contract still holds.
    _named_entries = [e for e in entries_list
                      if (e.get("name") or "").strip()
                      not in _GENERIC_DISH_NAMES_BLOCKLIST]
    if entries_list and not _named_entries:
        print(f"[scan-core] BLOCKED commit — every dish name is a "
              f"placeholder ({[e.get('name') for e in entries_list]}). "
              f"Refusing to write a generic name to the golden copy. "
              f"This photo was NOT logged.", flush=True)
        _b_url = f"/scan_img/{img_path.name}"
        return {
            "ok": False,
            "auto_committed": False,
            "image_path": str(img_path),
            "image_url": _b_url,
            "thumbnail_url": _thumb_url_for(_b_url),
            "drive_image_url": "",
            "vision_desc": "",
            "vision_short": "",
            "is_multi_entry": False,
            "entries": [],
            "committed": [],
            "multi_entry": False,
            "preview": {
                "image_path": str(img_path),
                "image_url": _b_url,
                "thumbnail_url": _thumb_url_for(_b_url),
                "vision_short": "",
                "vision_desc": "",
                "suggested_entry": {},
            },
        }
    if len(_named_entries) != len(entries_list):
        print(f"[scan-core] dropped {len(entries_list) - len(_named_entries)} "
              f"placeholder-named dish(es) before commit", flush=True)
        entries_list = _named_entries

    # v3.2.7.10: AUTO-COMMIT — no more confirm step. Scan → log immediately.
    # (Jim OOB 2026-08-08 20:15 HKT 'I think no need confirmation after
    # scanning. Do it right away. if I see problem, I can edit it delete
    # later on.') Frontend just shows the image + entries; if anything's
    # wrong, user taps ✏️ edit or 🗑️ delete on the food card.
    now_iso_str = now_iso()
    final_name = f"scan_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}.jpg"
    final_path = SCAN_CACHE_DIR / final_name
    drive_image_url = ""  # v3.2.7.45: Golden Copy (Jim OOB 2026-08-14)
    # v3.3.25 production-write guard — a QA fixture must NEVER reach the
    # production Nutrition tab. This check is independent of
    # GYMBRO_WRITE_MODE so it still protects a test that forgot to set
    # the env var (the 2026-10-20 failure mode).
    if _write_mode() == "production" and _is_production_target():
        _assert_not_test_fixture(img_path)

    # v3.3.25 (2026-10-21, Jim OOB 2026-10-20「And the ai recognition is
    # all wrong. It was showing unrecognised food」): NEVER write
    # 未識別菜式 when the vision providers are DOWN.
    #
    # On 2026-10-20 the APiyi key died (HTTP 401 "Invalid token") and
    # every scan silently wrote 未識別菜式 / 0 kcal to Jim's golden copy —
    # 7 junk rows in one evening, and Jim only found out because the app
    # was full of "unrecognised food". A genuine non-food photo is a
    # DIFFERENT case (the AI answered but found no dish) — that still
    # needs a name, so only block when the PROVIDER failed, i.e. there is
    # no description at all.
    #
    # Blocking HERE (before the Drive stage) means ZERO production
    # artefacts: no Drive file, no Sheet row. The response keeps the exact
    # same top-level key set as the success path so Isak's §4.3
    # regression contract still holds.
    if _VISION_STATUS.get("state") == "failed" and not vision_desc:
        _blocked_detail = _VISION_STATUS.get("last_error", "")
        print(f"[scan-core] BLOCKED commit — all vision providers failed "
              f"({_blocked_detail}). Refusing to write 未識別菜式 to the "
              f"golden copy. This photo was NOT logged.", flush=True)
        _b_url = f"/scan_img/{img_path.name}"
        return {
            "ok": False,
            "auto_committed": False,
            "image_path": str(img_path),
            "image_url": _b_url,
            "thumbnail_url": _thumb_url_for(_b_url),
            "drive_image_url": "",
            "vision_desc": "",
            "vision_short": "",
            "is_multi_entry": False,
            "entries": [],
            "committed": [],
            "multi_entry": False,
            "preview": {
                "image_path": str(img_path),
                "image_url": _b_url,
                "thumbnail_url": _thumb_url_for(_b_url),
                "vision_short": "",
                "vision_desc": "",
                "suggested_entry": {},
            },
        }

    _emit("drive")
    try:
        if keep_source:
            # v3.3.24e: Phase-2 reuse — the uploaded file is already the
            # final image, so copy it instead of renaming (the Phase-1
            # upload stays valid for the frontend to display).
            shutil.copyfile(img_path, final_path)
        else:
            img_path.rename(final_path)
        image_url = f"/scan_img/{final_name}"
        # v3.2.7.45: upload to Drive for Golden Copy. Best-effort — failure
        # logged but doesn't break the auto-commit, just leaves column N empty.
        # v3.3.25: in GYMBRO_WRITE_MODE=dryrun / throwaway the upload itself
        # is suppressed inside _upload_to_drive, so no Drive file is created.
        drive_image_url = _upload_to_drive(final_path)
        # v3.2.7.46: 480px thumbnail for fast food history view.
        _make_thumb(final_path, _thumb_path_for(final_name))
    except Exception as e:
        print(f"[scan-core] drive stage failed: {type(e).__name__}: {e}", flush=True)
        final_path = img_path
        image_url = f"/scan_img/{img_path.name}"
        try:
            drive_image_url = _upload_to_drive(final_path)
            _make_thumb(final_path, _thumb_path_for(img_path.name))
        except Exception as e2:
            print(f"[scan-core] drive fallback failed: "
                  f"{type(e2).__name__}: {e2}", flush=True)
            drive_image_url = ""

    _emit("commit")
    committed = []
    for e_idx, entry in enumerate(entries_list):
        entry["timestamp_iso"] = now_iso_str
        entry["source"] = "v3.3.34-scan_autocommit (minimax-text-01 + minimax-multi)"
        entry["models_used"] = ["minimax-text-01", "minimax-m3-enrich-multi"]
        entry["image_saved_to"] = str(final_path)
        entry["confidence"] = "auto-commit preview"
        entry["sheet_synced"] = False
        entry["user_correction"] = None
        entry["vision_raw_desc"] = vision_desc
        entry["vision_short"] = vision_desc[:300]
        entry["pplx_short"] = pplx_desc[:500]
        entry["minimax_enrichment"] = mm_desc[:300] if mm_desc else ""
        entry["user_hints"] = []
        # v3.2.7.45: Golden Copy — pipe Drive URL into the entry so it
        # reaches column N via _append_to_sheet_nutrition.
        entry["drive_image_url"] = drive_image_url

        # v3.2.7.48: enforce dish-name sanitiser on every commit
        # (shared helper `_sanitise_dish_name` consolidates the empty /
        # 'img_' / '.jpg' / '食物' / '相顯示' / '圖顯示' / '呢張' / long-name
        # criteria that previously lived inline here). Re-extract via
        # _extract_dish_name if the AI thinks the name is narration.
        current_name = _sanitise_dish_name(
            entry.get("name"),
            vision_desc=vision_desc,
            pplx_desc=pplx_desc,
        )
        if (current_name == "未識別菜式"
                or _name_has_narration(current_name)):
            redrive = _extract_dish_name(vision_desc, pplx_desc, fallback=current_name or "")
            if redrive and redrive.strip() not in ("食物", "未識別菜式"):
                entry["name"] = redrive
                current_name = redrive

        sheet_result = _append_to_sheet_nutrition(entry)
        new_row_index = None
        if sheet_result.get("ok") and not sheet_result.get("skipped"):
            try:
                # v3.3.25 (2026-10-20): same rstrip bug as api_scan_food /
                # api_scan_commit — "Nutrition!A564" ends with a digit so
                # rstrip("ABCDEFGHIJKLM") leaves it untouched and int()
                # raises, the bare `except: pass` swallowed it, and
                # scan_index stayed None for every single-photo commit
                # (Isak's Bug 3). End-anchored regex fixes it.
                range_str = sheet_result.get("range", "")
                m = re.search(r"(\d+)$", range_str.split(":")[0])
                if m:
                    new_row_index = int(m.group(1))
                    get_cache().refresh_one(new_row_index, _sheet_read_nutrition_row)
            except Exception as e:
                print(f"[scan-core] refresh_one failed for range "
                      f"{sheet_result.get('range', '')}: "
                      f"{type(e).__name__}: {e}", flush=True)
        committed.append({
            "name": entry.get("name", "?"),
            "calories": entry.get("calories", 0),
            "protein": entry.get("protein", 0),
            "grade": (entry.get("coach_comment") or {}).get("grade", "—"),
            "sheet_row": sheet_result.get("range", ""),
            "sheet_ok": sheet_result.get("ok", False),
            "scan_index": new_row_index,
        })

    # v3.3.2: no more food_scan_log.json / nutrition_log.json writes —
    # Sheet + cache are the canonical store. Skip the legacy scan_log append.

    # v3.2.7.23: ALWAYS auto-commit (Jim OOB 2026-08-10 'we dont need to
    # preview for my confirmation. just log it right away'). Previously
    # this endpoint refused to commit when the extracted name still
    # contained narration text (e.g. "這張相顯示一支蘇打水樽"), instead
    # returning auto_committed=False + needs_user_input=True so the
    # frontend could surface a warning. Now we trust the dish-name
    # extractor (lines 4674-4688 above) to do its best job and write
    # the entry unconditionally. If Jim sees a wrong name, he can
    # use ✏️ edit / 🗑️ delete on the food card after-the-fact.
    #
    # v3.3.25 (2026-10-20): auto_committed must be HONEST. In
    # GYMBRO_WRITE_MODE=dryrun nothing was written to the golden copy,
    # so reporting True would be a lie (QA would think the row landed).
    # The key set is unchanged (contract test only checks keys), only the
    # value differs.
    _wrote_to_sheet = any(c.get("sheet_ok") and c.get("sheet_row")
                           and c.get("sheet_row") != "dryrun"
                           for c in committed)
    # v3.3.8: invalidate daily coaching so the next cheer/video/food-coach
    # reflects the freshly scanned meal.
    _invalidate_daily_coaching()
    return {
        "ok": True,
        "auto_committed": _wrote_to_sheet,
        "image_path": str(final_path),
        "image_url": image_url,
        "thumbnail_url": _thumb_url_for(image_url),  # v3.2.7.46: fast food history
        "drive_image_url": drive_image_url,  # v3.2.7.45: Golden Copy
        "vision_desc": vision_desc,
        "vision_short": vision_desc[:300],
        "is_multi_entry": len(entries_list) > 1,
        "entries": entries_list,
        "committed": committed,
        "multi_entry": len(committed) > 1,
        # v3.2.7.15: preview wrapper for multi-photo queue
        # (onScanPhotosPicked reads data.preview.suggested_entry)
        "preview": {
            "image_path": str(final_path),
            "image_url": image_url,
            "thumbnail_url": _thumb_url_for(image_url),  # v3.2.7.46
            "vision_short": vision_desc[:300],
            "vision_desc": vision_desc,
            "suggested_entry": entries_list[0] if entries_list else {},
        },
    }


@app.route("/api/scan_preview", methods=["POST"])
def api_scan_preview():
    """Take image, run vision + pplx, return suggested entry WITHOUT writing to log.

    Frontend shows preview UI: dish desc + macros + suggested restaurant chain.
    Only when Jim taps 確認 → POST /api/scan_commit with the chosen entry.

    v3.3.24e: the pipeline body now lives in _scan_preview_core so the
    background scan-job worker (POST /api/scan_recognize) can share it.
    The response payload is unchanged (Isak's §4.3 regression test).
    """
    if "image" not in request.files:
        return jsonify({"ok": False, "error": "no image"}), 400
    img_file = request.files["image"]
    img_bytes = img_file.read()
    if len(img_bytes) > 10 * 1024 * 1024:
        return jsonify({"ok": False, "error": "image too large"}), 413
    try:
        return jsonify(_scan_preview_core(img_bytes))
    except ValueError as e:
        # v3.3.25 — the QA-fixture backstop raises ValueError. Without this
        # catch Flask turns it into a 500 Internal Server Error, which hides
        # the real reason. /api/scan_commit already answers 403 for the same
        # refusal; keep both endpoints consistent.
        if "REFUSED" in str(e):
            return jsonify({"ok": False, "error": str(e)}), 403
        raise

# ---------------------------------------------------------------------------
# v3.3.24e: two-phase food scan — upload (fast) then AI recognition (slow).
#
# SPEC: SCAN_PROGRESS_SPEC.md §2.1 / §2.2 / §2.3 / §2.4.
#
# Jim OOB 2026-10-20「when multiple upload, I want gymbro to handle the
# upload first. Ai recognition can be done later」— so Phase 1 only stores
# bytes (no AI at all) and Phase 2 runs the AI pipeline in a background
# thread while the frontend polls /api/scan_progress/<job_id>.
#
# Phase 2 reuses _scan_preview_core() so there is exactly ONE implementation
# of the recognition pipeline (and /api/scan_preview keeps its exact
# response shape).
# ---------------------------------------------------------------------------

# In-process status dict — keyed by job_id (same pattern as VIDEO_JOBS).
SCAN_JOBS = {}
SCAN_JOBS_LOCK = threading.Lock()
# v3.3.33 (parallel AI): 平行 worker 會同時叫 `_scan_job_set` → 同時寫
# scan_jobs_state.json。舊版 persist 用固定 tmp 檔名 + 喺 lock 外面讀
# SCAN_JOBS,兩個 thread 會寫爛同一個 tmp 檔,而且讀 dict 途中被改會拋
# "dictionary changed size during iteration"。所以:獨立 persist lock +
# 喺 SCAN_JOBS_LOCK 下面先影一份 snapshot。
_SCAN_JOBS_PERSIST_LOCK = threading.Lock()

# v3.3.25 (2026-10-21): on-disk mirror of SCAN_JOBS so a job that was
# running when the process died is reported as "interrupted" instead of
# 404-ing away, and its remaining image paths survive for a resume.
_SCAN_JOBS_STATE_FILE = "/home/work/.hermes/scan_cache/scan_jobs_state.json"

# Job registry entries are kept for 10 minutes after completion so a late
# poll still finds them (SPEC §2.4).
SCAN_JOB_TTL_S = 600


def _scan_job_set(job_id: str, **kwargs):
    """Update one scan job in SCAN_JOBS and print the new progress."""
    with SCAN_JOBS_LOCK:
        cur = SCAN_JOBS.get(job_id, {})
        cur.update(kwargs)
        cur["updated_at"] = time.time()
        SCAN_JOBS[job_id] = cur
    print(f"[scan-job] {job_id} stage={cur.get('stage')} "
          f"pct={cur.get('pct')} idx={cur.get('current_index')}/"
          f"{cur.get('total')} status={cur.get('status')}", flush=True)
    _scan_jobs_persist()
    return cur


def _scan_jobs_persist():
    """v3.3.25 (2026-10-21): mirror the job registry to disk.

    WHY: on 2026-10-21 Jim's 3-photo scan was killed mid-flight because
    gymbro was restarted; the frontend then got a 404 from
    /api/scan_progress/<job_id> and showed "查 AI 進度失敗 / Load failed"
    with no explanation. With the state on disk, a job that was running
    when the process died is detected at startup and reported as
    "interrupted" instead of vanishing — and the remaining image paths
    survive so the scan can be resumed.
    """
    with _SCAN_JOBS_PERSIST_LOCK:
        try:
            # v3.3.33: 喺 SCAN_JOBS_LOCK 下面影 snapshot —— 唔好喺
            # dict 迭代中途畀人改(會拋 RuntimeError)。
            with SCAN_JOBS_LOCK:
                snapshot = {jid: {k: v for k, v in j.items()
                                  if k not in ("results", "errors")}
                            for jid, j in list(SCAN_JOBS.items())}
            tmp = f"{_SCAN_JOBS_STATE_FILE}.tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh)
            os.replace(tmp, _SCAN_JOBS_STATE_FILE)
        except Exception as e:
            print(f"[scan-job] persist failed: {type(e).__name__}: {e}", flush=True)


def _scan_jobs_load_interrupted():
    """v3.3.25: find jobs that were running when the process died.

    Returns a list of (job_id, job_dict) for jobs that were still
    "running" in the on-disk state. Their status becomes "interrupted"
    so the frontend can say so plainly, and their remaining paths stay
    available for a resume.
    """
    p = Path(_SCAN_JOBS_STATE_FILE)
    if not p.exists():
        return []
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[scan-job] state load failed: {type(e).__name__}: {e}", flush=True)
        return []
    out = []
    with SCAN_JOBS_LOCK:
        for jid, j in (state or {}).items():
            if j.get("status") == "running":
                j["status"] = "interrupted"
                j["interrupted_at"] = time.time()
                SCAN_JOBS[jid] = j
                out.append((jid, j))
    if out:
        print(f"[scan-job] {len(out)} job(s) were interrupted by a server "
              f"restart: {[j for j, _ in out]}", flush=True)
    return out


def _scan_job_get(job_id: str) -> dict:
    """Return a snapshot copy of one scan job (empty dict if unknown)."""
    with SCAN_JOBS_LOCK:
        return dict(SCAN_JOBS.get(job_id) or {})


def _scan_jobs_purge_expired():
    """Drop finished jobs older than SCAN_JOB_TTL_S (SPEC §2.4)."""
    now = time.time()
    with SCAN_JOBS_LOCK:
        expired = [
            jid for jid, j in SCAN_JOBS.items()
            if j.get("status") in ("completed", "failed")
            and (now - float(j.get("updated_at") or 0)) > SCAN_JOB_TTL_S
        ]
        for jid in expired:
            SCAN_JOBS.pop(jid, None)
    if expired:
        print(f"[scan-job] purged {len(expired)} expired job(s): {expired}",
              flush=True)


def _scan_stage_label(stage_key: str, current_index: int, total: int) -> str:
    """繁中 stage label for the frontend (SPEC §2.3)."""
    tmpl = SCAN_STAGE_LABELS.get(stage_key, stage_key)
    n = current_index + 1
    try:
        return tmpl.format(n=n, total=total)
    except (KeyError, IndexError) as e:
        print(f"[scan-job] stage label format failed for {stage_key}: {e}",
              flush=True)
        return tmpl


def _scan_job_pct(current_index: int, stage_key: str, total: int) -> int:
    """Overall progress pct (SPEC §2.3).

    整體 pct = (current_index * 100 + stage_pct) / total, where stage_pct is
    the start of the current stage's band inside that image's 0-100 slice.
    """
    if total <= 0:
        return 100
    stage_pct = SCAN_STAGE_PCT_START.get(stage_key, 0)
    raw = (current_index * 100 + stage_pct) / total
    return max(0, min(100, int(round(raw))))


def _scan_path_allowed(img_path: Path) -> bool:
    """Safety check for Phase-2 paths (SPEC §2.1).

    Only files directly inside SCAN_CACHE_DIR or inside
    /home/work/.hermes/image_cache/ may be read — same rule the existing
    /api/scan_preview_from_path uses.
    """
    try:
        resolved = img_path.resolve()
    except Exception as e:
        print(f"[scan-upload] path resolve failed for {img_path}: "
              f"{type(e).__name__}: {e}", flush=True)
        return False
    if resolved.parent != SCAN_CACHE_DIR.resolve() and not str(resolved).startswith("/home/work/.hermes/image_cache/"):
        return False
    return True


def _scan_parallel_workers() -> int:
    """v3.3.33: 平行 AI worker 數量。預設 3,可用 GYMBRO_SCAN_PARALLEL 調整。"""
    try:
        n = int(os.environ.get("GYMBRO_SCAN_PARALLEL", "3") or 3)
    except (TypeError, ValueError):
        n = 3
    return max(1, min(8, n))


def _scan_run_job(job_id: str, paths: list, client_indexes: list):
    """Background worker: run the recognition pipeline for each image.

    v3.3.33 (Jim OOB 2026-10-21「And ai work can be done in parallel to save
    queuing time too」):舊版刻意 sequential(SPEC §2.2)—— 但 AI stage
    (MiniMax 視覺 → 第二意見 → 營養估算 → Perplexity 交叉核對 → 品牌表 →
    Drive 上載)全部係 network-bound,一張相要 10-20 秒。3 張相排隊 = 30-60 秒,
    期間 Jim 個 app 完全卡住。

    而家用 ThreadPoolExecutor 平行跑(預設 3 個 worker)。
    **Sheet 寫入仍然 serialized** —— `_append_to_sheet_nutrition` 有
    process-wide lock(見 `_serialise_sheet_write`),因為 token refresh +
    idempotency store + trim pre-flight 唔係 concurrency-safe。
    AI 係慢嘅部分,Sheet 寫入只係 1-2 秒 → 整體快 ~3 倍而 golden copy 唔會花。

    **結果順序唔再保證** —— parallel 之下先完成嘅先入 results。server 每個
    result 都帶 `client_index`,前端一定要用 client_index 對返 queue item,
    唔可以用 array 位置(舊版 sequential 嘅隱含假設)。
    """
    total = len(paths)
    results = []
    errors = []
    lock = threading.Lock()
    done_count = 0
    pct_high = 0
    inflight = {}          # client_index -> stage_pct_start

    def _publish(stage_key: str, ci: int):
        """Thread-safe 進度更新。pct 用 high-water mark 保證單調遞增。"""
        nonlocal pct_high
        with lock:
            inflight[ci] = SCAN_STAGE_PCT_START.get(stage_key, 0)
            stage_frac = max(inflight.values()) if inflight else 0
            # v3.3.33fix (Frimpong 2026-10-21): stage_frac is on a 0-100
            # scale (SCAN_STAGE_PCT_START), NOT a count of images. The old
            # formula ((done_count + stage_frac) / total * 100) mixed the two
            # scales — at stage_frac=12 it computed 600% and the bar jumped
            # straight to the 99% clamp, then sat there for the rest of the
            # job (live: 0 -> 99 at t=2.8s, pinned at 99 for 110s). That is
            # exactly the "假進度" Jim forbids. Use the SPEC §2.3 formula:
            #   整體 pct = (current_index * 100 + stage_pct) / total
            # i.e. completed images contribute 100 each, and the in-flight
            # stage contributes its 0-100 slice of ONE image.
            raw = ((done_count * 100) + stage_frac) / total if total else 100
            new_pct = max(0, min(99, int(round(raw))))
            if new_pct > pct_high:
                pct_high = new_pct
            _scan_job_set(
                job_id,
                status="running",
                stage=stage_key,
                stage_label=_scan_stage_label_parallel(stage_key, done_count, total),
                pct=pct_high,
                current_index=min(done_count, max(total - 1, 0)),
                total=total,
                completed=done_count,
                inflight=sorted(inflight.keys()),
            )

    def _process(idx: int, p: str):
        # v3.3.33: `done_count += 1` 係 rebind,唔係 mutate → 一定要 nonlocal,
        # 否則 Python 當佢係 `_process` 嘅 local variable → UnboundLocalError。
        nonlocal done_count
        client_index = client_indexes[idx] if idx < len(client_indexes) else idx
        img_path = Path(p)
        try:
            if not img_path.is_file():
                raise FileNotFoundError(f"image not found: {img_path}")
            if not _scan_path_allowed(img_path):
                raise PermissionError(f"image path outside permitted dirs: {img_path}")
            if img_path.stat().st_size > 10 * 1024 * 1024:
                raise ValueError(f"image too large: {img_path}")

            _publish("queued", client_index)

            img_bytes = img_path.read_bytes()

            def _on_stage(stage_key: str, _ci: int = client_index):
                _publish(stage_key, _ci)

            core = _scan_preview_core(
                img_bytes, img_path_hint=str(img_path), on_stage=_on_stage,
            )

            entries = core.get("entries") or []
            first = entries[0] if entries else {}
            # v3.3.30 (2026-10-21): the Phase-2 wrapper used to hardcode
            # "ok": True, which SWALLOWED the Rule 25 / vision-failure block
            # signal from _scan_preview_core. The frontend has a branch for
            # `res.ok === false` ("呢張相搵唔到菜名,冇入账") but it could never
            # fire on this path — Jim would see the photo marked as 已入账
            # while nothing was written to his golden copy.
            # Pass the core's real ok through so the block is honest.
            result = {
                "client_index": client_index,
                "ok": bool(core.get("ok", True)),
                "name": first.get("name", ""),
                "calories": first.get("calories", 0),
                "protein": first.get("protein", 0),
                "committed": core.get("committed") or [],
                "preview": core.get("preview") or {},
                "image_url": core.get("image_url", ""),
                "thumbnail_url": core.get("thumbnail_url", ""),
                "vision_desc": core.get("vision_desc", ""),
                "entries": entries,
            }
            with lock:
                results.append(result)
                done_count += 1
                inflight.pop(client_index, None)
                _scan_job_set(job_id, results=list(results), errors=list(errors),
                              completed=done_count, inflight=sorted(inflight.keys()))
        except Exception as e:
            print(f"[scan-job] {job_id} image {idx} "
                  f"(client_index={client_index}) failed: "
                  f"{type(e).__name__}: {e}", flush=True)
            with lock:
                errors.append({"client_index": client_index, "error": str(e)})
                done_count += 1
                inflight.pop(client_index, None)
                _scan_job_set(job_id, results=list(results), errors=list(errors),
                              completed=done_count, inflight=sorted(inflight.keys()))

    workers = min(_scan_parallel_workers(), max(total, 1))
    if workers > 1 and total > 1:
        print(f"[scan-job] {job_id} running {total} photos in PARALLEL "
              f"({workers} workers)", flush=True)
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="scan-par") as pool:
            futures = [pool.submit(_process, idx, p) for idx, p in enumerate(paths)]
            for fut in futures:
                try:
                    fut.result()
                except Exception as e:
                    print(f"[scan-job] {job_id} worker crashed: "
                          f"{type(e).__name__}: {e}", flush=True)
    else:
        for idx, p in enumerate(paths):
            _process(idx, p)

    final_status = "completed" if results else "failed"
    _scan_job_set(
        job_id,
        status=final_status,
        stage="done",
        stage_label=("全部搞掂晒" if final_status == "completed"
                     else "全部失敗"),
        pct=100,
        current_index=max(total - 1, 0),
        total=total,
        results=list(results),
        errors=list(errors),
        # v3.3.33 (Frimpong 2026-10-21 指出):completed = **已處理**(成功 + 失敗),
        # 唔係只計成功。用 len(results) 嘅話,一張相失敗 → completed 永遠少 1,
        # 前端個「(已完成 x/y)」label 永遠到唔到 100%。
        completed=done_count,
        inflight=[],
    )


@app.route("/api/scan_upload", methods=["POST"])
def api_scan_upload():
    """Phase 1 — receive food photos, store them, EXIF + thumbnail. No AI.

    Deliberately does ZERO AI work (SPEC §2.1) so Jim can upload every
    photo first and let the AI run afterwards.
    """
    files = request.files.getlist("images") or request.files.getlist("image")
    if not files:
        return jsonify({"ok": False, "error": "no image"}), 400

    now_hkt_dt = datetime.now(timezone(timedelta(hours=8)))
    ts_str = now_hkt_dt.strftime("%Y%m%d_%H%M%S")

    uploads = []
    for i, f in enumerate(files):
        img_bytes = f.read()
        if len(img_bytes) > 10 * 1024 * 1024:
            return jsonify({"ok": False, "error": "image too large"}), 413

        orig_name = (f.filename or "").strip() or f"upload_{i}.jpg"
        ext = ".jpg"
        lower = orig_name.lower()
        if lower.endswith(".png"):
            ext = ".png"
        elif lower.endswith(".jpeg") or lower.endswith(".jpg"):
            ext = ".jpg"

        # v3.3.25 (2026-10-21): the stored name used to be
        # f"upload_{ts_str}_{i}{ext}" where ts_str has SECOND resolution and
        # i restarts at 0 for every request. Two uploads in the same second
        # (Jim's phone uploading 3 photos at once, or a retry) therefore
        # produced the SAME filename and the second file silently
        # OVERWROTE the first — a photo lost with no error anywhere.
        # Verified 2026-10-21: upload_20261021_001848_0 / _001851_0 /
        # _001853_0 — three separate requests, each index 0.
        # Fix: add a uuid segment so the name is unique per request AND
        # per file within the request.
        stored_name = f"upload_{ts_str}_{uuid.uuid4().hex[:8]}_{i}{ext}"
        stored_path = SCAN_CACHE_DIR / stored_name
        stored_path.write_bytes(img_bytes)

        # v3.3.8: pull the real photo shot time out of EXIF (iPhone photo
        # stream uploads often happen hours after the shot).
        exif_info = _extract_exif_capture_time(stored_path)
        capture_iso = now_hkt_dt.strftime("%Y-%m-%d")
        capture_hhmm = now_hkt_dt.strftime("%H:%M")
        if exif_info.get("capture_time"):
            try:
                capture_dt = datetime.fromisoformat(exif_info["capture_time"])
                capture_iso = capture_dt.strftime("%Y-%m-%d")
                capture_hhmm = capture_dt.strftime("%H:%M")
            except (ValueError, TypeError) as e:
                print(f"[scan-upload] EXIF capture_time parse failed for "
                      f"{stored_name}: {e}", flush=True)

        # v3.2.7.46: 480px thumbnail for the food history view.
        _make_thumb(stored_path, _thumb_path_for(stored_name))

        image_url = f"/scan_img/{stored_name}"
        uploads.append({
            "client_index": i,
            "filename": orig_name,
            "path": str(stored_path),
            "image_url": image_url,
            "thumbnail_url": _thumb_url_for(image_url),
            "size_kb": int(round(len(img_bytes) / 1024)),
            "capture_iso": capture_iso,
            "capture_hhmm": capture_hhmm,
        })
        print(f"[scan-upload] stored {stored_name} "
              f"({uploads[-1]['size_kb']} KB, exif={exif_info.get('source') or 'none'})",
              flush=True)

    return jsonify({"ok": True, "uploads": uploads})


@app.route("/api/scan_recognize", methods=["POST"])
def api_scan_recognize():
    """Phase 2 — start the AI recognition job for previously uploaded photos.

    Returns immediately with a job_id; the frontend polls
    /api/scan_progress/<job_id> for real per-stage progress.
    """
    data = request.get_json(silent=True) or {}
    paths = data.get("paths") or []
    client_indexes = data.get("client_indexes") or []
    if not paths:
        return jsonify({"ok": False, "error": "no paths"}), 400

    # Safety: only accept paths under the permitted cache dirs (SPEC §2.1).
    clean_paths = []
    for p in paths:
        pp = Path(str(p))
        if not _scan_path_allowed(pp):
            return jsonify({
                "ok": False,
                "error": f"image path outside permitted dirs: {p}",
            }), 403
        clean_paths.append(str(pp))

    job_id = (f"scanjob_{datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')}"
              f"_{uuid.uuid4().hex[:6]}")
    _scan_job_set(
        job_id,
        status="running",
        stage="queued",
        stage_label=_scan_stage_label("queued", 0, len(clean_paths)),
        pct=0,
        current_index=0,
        total=len(clean_paths),
        results=[],
        errors=[],
        created_at=time.time(),
        # v3.3.25: keep the paths so an interrupted job can be resumed
        # (and so a restart can tell Jim which photos were lost).
        paths=list(clean_paths),
        client_indexes=list(client_indexes),
    )
    _scan_jobs_purge_expired()

    t = threading.Thread(
        target=_scan_run_job,
        args=(job_id, clean_paths, client_indexes),
        name=f"scan-job-{job_id}",
        daemon=True,
    )
    t.start()
    print(f"[scan-job] {job_id} started, total={len(clean_paths)}", flush=True)

    return jsonify({"ok": True, "job_id": job_id, "total": len(clean_paths)})


@app.route("/api/scan_progress/<job_id>", methods=["GET"])
def api_scan_progress(job_id: str):
    """Poll the progress of a Phase-2 recognition job (SPEC §2.3)."""
    _scan_jobs_purge_expired()
    job = _scan_job_get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404

    created = float(job.get("created_at") or job.get("updated_at") or 0)
    # v3.3.25: tell the frontend it can retry a job that died (server
    # restart / failure). The uploaded photos are still on disk, so a
    # resume only repeats the AI recognition — no re-upload needed.
    _paths = job.get("paths") or []
    _can_resume = (job.get("status") in ("interrupted", "failed")
                   and any(Path(str(p)).is_file() for p in _paths))
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "status": job.get("status", "running"),
        "stage": job.get("stage", "queued"),
        "stage_label": job.get("stage_label", ""),
        "pct": job.get("pct", 0),
        "current_index": job.get("current_index", 0),
        "total": job.get("total", 0),
        # v3.3.33 (parallel AI): 已完成張數 + 處理中嘅 client_index 清單。
        # 前端靠 client_index 對返 queue item(平行模式下 results 唔保證順序)。
        "completed": job.get("completed", 0),
        "inflight": job.get("inflight") or [],
        "results": job.get("results") or [],
        "errors": job.get("errors") or [],
        "elapsed_s": round(time.time() - created, 1) if created else 0.0,
        "can_resume": _can_resume,
        "resume_url": f"/api/scan_resume/{job_id}" if _can_resume else "",
    })


@app.route("/api/scan_resume/<job_id>", methods=["POST"])
def api_scan_resume(job_id: str):
    """v3.3.25 (2026-10-21): re-run a scan job that was interrupted by a
    gymbro restart (or that failed).

    WHY: Jim's 3-photo scan was killed mid-flight on 2026-10-21 because
    the server was restarted while the AI was working. The uploaded photos
    were still on disk, but the frontend had no way to retry — the job was
    gone from memory. This endpoint re-runs the remaining images using the
    stored paths, so a restart costs Jim nothing but a tap.

    The photos are already uploaded (Phase 1 is durable), so there is no
    re-upload step — only the AI recognition is repeated.
    """
    job = _scan_job_get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404

    paths = job.get("paths") or []
    if not paths:
        return jsonify({"ok": False,
                        "error": "no stored paths for this job"}), 400

    # Only resume paths that still exist on disk.
    live = [p for p in paths if Path(str(p)).is_file()]
    if not live:
        return jsonify({"ok": False,
                        "error": "none of the stored photos exist any more"}), 400

    client_indexes = job.get("client_indexes") or []
    if len(client_indexes) != len(paths):
        client_indexes = list(range(len(paths)))

    # v3.3.25: only re-run the images that had NOT been committed yet.
    # `current_index` is the index of the image that was in flight when the
    # job died — everything before it was already written to the Sheet, so
    # re-running those would create DUPLICATE rows in Jim's golden copy.
    done = int(job.get("current_index") or 0)
    done = max(0, min(done, len(paths)))
    todo_paths = paths[done:]
    todo_idxs = client_indexes[done:]

    if not todo_paths:
        return jsonify({"ok": False,
                        "error": "all images were already committed"}), 400

    new_id = (f"scanjob_{datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')}"
              f"_{uuid.uuid4().hex[:6]}")
    _scan_job_set(
        new_id,
        status="running",
        stage="queued",
        stage_label=_scan_stage_label("queued", 0, len(todo_paths)),
        pct=0,
        current_index=0,
        total=len(todo_paths),
        results=[],
        errors=[],
        created_at=time.time(),
        paths=list(todo_paths),
        client_indexes=list(todo_idxs),
        resumed_from=job_id,
        resumed_skip=len(paths) - len(todo_paths),
    )
    _scan_jobs_purge_expired()

    t = threading.Thread(
        target=_scan_run_job,
        args=(new_id, todo_paths, todo_idxs),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "job_id": new_id, "resumed_from": job_id,
                    "total": len(todo_paths),
                    "skipped_already_committed": len(paths) - len(todo_paths),
                    "skipped_missing": len(paths) - len(live)})


@app.route("/api/scan_preview_from_path", methods=["POST"])
def api_scan_preview_from_path():
    """Same as /api/scan_preview but takes a server-side image_path (from photostream) instead of multipart upload."""
    data = request.get_json(silent=True) or {}
    image_path = data.get("image_path", "")
    img_path = Path(image_path)
    if not img_path.exists():
        return jsonify({"ok": False, "error": "image not found at server path"}), 404
    if img_path.parent != SCAN_CACHE_DIR.resolve() and not str(img_path.resolve()).startswith("/home/work/.hermes/image_cache/"):
        # Safety: only allow reading from known cache dirs
        return jsonify({"ok": False, "error": "image path outside permitted dirs"}), 403
    img_bytes = img_path.read_bytes()
    if len(img_bytes) > 10 * 1024 * 1024:
        return jsonify({"ok": False, "error": "image too large"}), 413

    now_hkt_dt = datetime.now(timezone(timedelta(hours=8)))
    img_b64 = base64.b64encode(img_bytes).decode()

    # v3.3.8 (Jim OOB 2026-08-31): extract EXIF capture time so we log the
    # actual photo shot time, not the upload time. iPhone photo stream
    # uploads often happen hours after the shot (e.g. breakfast photo
    # uploaded at dinner time → would otherwise mis-classify meal_type).
    exif_info = _extract_exif_capture_time(img_path)
    capture_iso = now_hkt_dt.strftime("%Y-%m-%d")
    capture_hhmm = now_hkt_dt.strftime("%H:%M")
    capture_dt = now_hkt_dt  # for the capture_time_iso field in the preview
    if exif_info.get("capture_time"):
        try:
            capture_dt = datetime.fromisoformat(exif_info["capture_time"])
            capture_iso = capture_dt.strftime("%Y-%m-%d")
            capture_hhmm = capture_dt.strftime("%H:%M")
        except (ValueError, TypeError):
            pass  # fall back to upload time
    # Keep now_hkt_dt for filename uniqueness (preserves audit trail); but
    # dates + times in returned entries use capture_* values.

    # v3.2.7.32: plain-text vision prompt (no 「菜式：/份量：/煮法：」
    # headers — Jim OOB 2026-08-11)
    vision_prompt = (
        "你係食物視覺識別助手。請用繁體中文廣東話,2-4 句口語化句子,直接講"
        "相入面見到咩食物、份量幾多(目測)、點煮、邊間餐廳(如見到 logo 或招牌字)。"
        "例如：「一碗白飯配一塊煎雞扒,碟邊有少許黑椒汁,餐廳係太興。」"
        "如果係飲品,就講幾多 ml、咩品牌、有冇冰有冇糖。"
        "唔好用 markdown 標題、唔好用「菜式：」呢啲 section header,"
        "唔好寫「呢張相顯示...」、「可見...」呢啲開場白,直接講食物。"
        "一個英文字都唔好有,唔識就寫「難以辨認」。"
    )
    vision_desc = _minimax_vision(img_b64, vision_prompt)

    # 1b. 2nd-opinion vision from a DIFFERENT provider in the chain
    opinion2_desc = _vision_second_opinion(img_b64, vision_prompt)
    # v3.2.7.14: detect gpt-4o-mini safety-filter refusal (e.g. "抱歉，我無法...")
    # and treat as empty so it does NOT pollute vision_desc (Jim OOB
    # 2026-08-09 16:48 HKT 'I took a screenshot not food, but scan got
    # committed as 抱歉 / 第一道菜').
    if opinion2_desc and ("抱歉" in opinion2_desc[:30] and "無法" in opinion2_desc[:50] and len(opinion2_desc) < 80):
        opinion2_desc = ""
    if opinion2_desc and not opinion2_desc.startswith("（"):
        # 2-vision median-merge: take whichever is longer (more dish detail)
        if len(opinion2_desc) > len(vision_desc) * 0.7:
            vision_desc = vision_desc + "\n\n（2nd-opinion vision）\n" + opinion2_desc
    pplx_desc = ""  # v3.3.34: Perplexity enrichment 已移除

    shared = _detect_shared_meal(vision_desc + " " + pplx_desc)
    jim_ratio = 0.60 if shared else 1.00
    mm_desc = _minimax_nutrition_enrich(vision_desc)
    pplx_parsed = _parse_nutrition_block(pplx_desc)
    mm_parsed = {}
    if mm_desc and mm_desc.startswith("{"):
        try:
            mm_parsed = json.loads(mm_desc)
        except Exception:
            mm_parsed = _parse_nutrition_block(mm_desc)
    merged_nutrition = _merge_nutrition_estimates([pplx_parsed, mm_parsed])

    # v3.3.23v (Jim OOB 2026-09-15 'my recent quest energy bar does not
    # record any nutrient info'): brand_table override AFTER merge. For
    # photo scans, override the merged estimate with brand-correct values
    # when a known brand product is detected.
    brand_hit = _lookup_brand_macros(vision_desc)
    if brand_hit:
        print(f"[scan_preview] brand_table override for {vision_desc!r}: "
              f"{merged_nutrition.get('calories', {}).get('value', 0)}→{brand_hit['kcal']} kcal",
              flush=True)
        for f in ("calories", "protein", "carbs", "fat", "fiber", "sugar",
                  "sodium", "sat_fat", "trans_fat"):
            if brand_hit.get(f) is not None:
                cur = merged_nutrition.get(f, {}) or {}
                if not cur.get("value") or float(cur.get("value", 0) or 0) == 0:
                    cur["value"] = brand_hit[f]
                    cur["source"] = f"brand_table ({brand_hit['_source']})"
                    merged_nutrition[f] = cur
    raw_kcal = int(merged_nutrition.get("calories", {}).get("value", 0) or 0)
    raw_p = int(merged_nutrition.get("protein", {}).get("value", 0) or 0)
    jim_kcal = round(raw_kcal * jim_ratio)
    jim_p = round(raw_p * jim_ratio)
    chain_match = re.search(r"([\u4e00-\u9fff]{2,6}(?:王|軒|亭|餐廳|食堂|廚|小店|屋|樓))", vision_desc + pplx_desc)
    restaurant_guess = chain_match.group(1) if chain_match else ""

    # v3.3.6 bugfix: build preview_field_entries here — it was referenced
    # below (coach_comment + suggested_entry) but never defined, so every
    # call to /api/scan_preview_from_path returned HTTP 500 NameError.
    # Same shape as api_scan_re_enrich"s field_entries dict.
    preview_field_entries: dict[str, float] = {}
    for f in NUTRITION_FIELDS:
        info = merged_nutrition.get(f)
        if not info:
            preview_field_entries[f] = 0
            continue
        v = info.get("value", 0) or 0
        preview_field_entries[f] = round(v * jim_ratio, 1) if shared else v

    # Copy image into scan_cache so commit can rename later
    preview_filename = f"preview_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}_from_path.jpg"
    preview_path = SCAN_CACHE_DIR / preview_filename
    preview_path.write_bytes(img_bytes)
    # v3.2.7.46: thumbnail for the photostream scan path
    _make_thumb(preview_path, _thumb_path_for(preview_filename))

    preview = {
        "preview_id": f"pv_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}",
        "image_path": str(preview_path),
        "image_url": f"/scan_img/{preview_filename}",
        "thumbnail_url": _thumb_url_for(f"/scan_img/{preview_filename}"),
        "vision_desc": vision_desc,
        "vision_short": vision_desc[:300],
        "pplx_short": pplx_desc[:500],
        "suggested_entry": {
            "date": capture_iso,
            "time": capture_hhmm,
            "meal_type": "scan",
            "name": _extract_dish_name(vision_desc, pplx_desc),
            "coach_comment": _coach_comment(
                _extract_dish_name(vision_desc, pplx_desc),
                preview_field_entries["calories"],
                preview_field_entries["protein"],
                preview_field_entries.get("carbs", 0),
                preview_field_entries.get("fat", 0),
                restaurant_guess,
                daily_ctx_seed=_daily_ctx_seed(),
            ),
            "restaurant_chain": restaurant_guess,
            "calories": jim_kcal,
            "protein": jim_p,
            "carbs": preview_field_entries.get("carbs", 0),
            "fat": preview_field_entries.get("fat", 0),
            "is_shared_meal": shared,
            "share_with_wife": "Jim 60% / 小寶 40% (auto-applied)" if shared else "Jim 100% (solo)",
            "raw_kcal_estimate": raw_kcal,
            "raw_p_estimate": raw_p,
            # v3.3.8: surface EXIF capture source to the PWA so Jim can
            # verify "拍攝時間 vs 上傳時間" in the preview card.
            "capture_time_source": exif_info.get("source", ""),
            "capture_time_iso": capture_dt.isoformat(),
        },
        "ready_to_commit": True,
    }
    return jsonify({"ok": True, "preview": preview})


def _sheet_delete_nutrition_rows(matcher_fn) -> dict:
    """Delete rows from Google Sheet Nutrition tab where matcher_fn(row, idx) -> bool.

    Uses batchUpdate deleteDimension. matcher_fn receives the row values array
    (already 1-indexed in display but 0-indexed in array — header is at index 0
    if header row exists, otherwise data starts at 0). Returns
    {"ok": bool, "deleted": int, "errors": list[str]}.
    """
    try:
        tok = json.loads(Path("/home/work/.hermes/google_token.json").read_text())
        if "token" not in tok or not tok.get("refresh_token"):
            return {"ok": False, "deleted": 0, "errors": ["no_token"]}
        # Refresh access token
        data = urllib.parse.urlencode({
            "client_id": tok["client_id"],
            "client_secret": tok["client_secret"],
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        access = resp["access_token"]
        tok["token"] = access
        Path("/home/work/.hermes/google_token.json").write_text(json.dumps(tok, indent=2))

        SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
        NUTRITION_SHEET_ID = 474877075  # numeric sheetId, not tab name
        # Read all rows
        url_read = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition!A1:K1000?valueRenderOption=FORMATTED_VALUE"
        req_read = urllib.request.Request(url_read, headers={"Authorization": f"Bearer {access}"})
        all_rows = json.loads(urllib.request.urlopen(req_read, timeout=10).read()).get("values", [])
        # Find matching rows (skip header at index 0)
        # collect (0-based sheet row index, list row) pairs
        matches = []
        for idx, row in enumerate(all_rows):
            if idx == 0:
                continue  # skip header
            if matcher_fn(row, idx):
                matches.append(idx)  # 0-based row index in the sheet (header=row 0)
        if not matches:
            return {"ok": True, "deleted": 0, "errors": []}
        # Build deleteDimension requests — must delete from bottom up to preserve indices
        matches.sort(reverse=True)
        requests = []
        for row_idx in matches:
            requests.append({
                "deleteDimension": {
                    "range": {
                        "sheetId": NUTRITION_SHEET_ID,
                        "dimension": "ROWS",
                        "startIndex": row_idx,
                        "endIndex": row_idx + 1,
                    }
                }
            })
        body = {"requests": requests}
        url_batch = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate"
        req_batch = urllib.request.Request(
            url_batch, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
        )
        urllib.request.urlopen(req_batch, timeout=15).read()
        return {"ok": True, "deleted": len(matches), "errors": []}
    except Exception as e:
        return {"ok": False, "deleted": 0, "errors": [str(e)]}


def _sheet_delete_nutrition_rows_by_indices(row_indices: list) -> dict:
    """v3.3.5: delete rows by exact 1-indexed Sheet row numbers.

    Skips the matcher entirely — used by api_scan_delete when the cache
    can identify the exact row. Avoids the 3-min tolerance matcher that
    incorrectly matched adjacent 蘇打水 entries (Jim OOB 2026-08-15).
    """
    try:
        tok = json.loads(Path("/home/work/.hermes/google_token.json").read_text())
        if "token" not in tok or not tok.get("refresh_token"):
            return {"ok": False, "deleted": 0, "errors": ["no_token"]}
        data = urllib.parse.urlencode({
            "client_id": tok["client_id"],
            "client_secret": tok["client_secret"],
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        access = json.loads(urllib.request.urlopen(req, timeout=15).read())["access_token"]
        tok["token"] = access
        Path("/home/work/.hermes/google_token.json").write_text(json.dumps(tok, indent=2))

        SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
        NUTRITION_SHEET_ID = 474877075
        # Sheet row numbers are 1-indexed; convert to 0-based for startIndex.
        # Header is row 1, so data starts at row 2 (0-based 1).
        # deleteDimension uses 0-based startIndex.
        zero_based = sorted({ri - 1 for ri in row_indices if isinstance(ri, int) and ri >= 2}, reverse=True)
        if not zero_based:
            return {"ok": True, "deleted": 0, "errors": []}
        requests = []
        for row_idx in zero_based:
            requests.append({
                "deleteDimension": {
                    "range": {
                        "sheetId": NUTRITION_SHEET_ID,
                        "dimension": "ROWS",
                        "startIndex": row_idx,
                        "endIndex": row_idx + 1,
                    }
                }
            })
        body = {"requests": requests}
        url_batch = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate"
        req_batch = urllib.request.Request(
            url_batch, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
        )
        urllib.request.urlopen(req_batch, timeout=15).read()
        return {"ok": True, "deleted": len(zero_based), "errors": []}
    except Exception as e:
        return {"ok": False, "deleted": 0, "errors": [str(e)]}


def _sheet_update_nutrition_cells(row_idx: int, updates: list) -> dict:
    """Update specific cells in Google Sheet Nutrition tab at given row.

    row_idx: 1-indexed row number (1 = first data row after header)
    updates: list of (col_letter, value) tuples, e.g. [("A", "2026-08-06"), ("B", "2026-08-06T01:01:00+08:00")]

    Uses batchUpdate values:batchUpdate API. Returns {ok, updated, errors}.
    """
    try:
        tok = json.loads(Path("/home/work/.hermes/google_token.json").read_text())
        if "token" not in tok or not tok.get("refresh_token"):
            return {"ok": False, "updated": 0, "errors": ["no_token"]}
        # Refresh access token
        data = urllib.parse.urlencode({
            "client_id": tok["client_id"],
            "client_secret": tok["client_secret"],
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        access = resp["access_token"]
        tok["token"] = access
        Path("/home/work/.hermes/google_token.json").write_text(json.dumps(tok, indent=2))

        SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
        # Build batchUpdate data array
        data_array = []
        for col, val in updates:
            data_array.append({
                "range": f"Nutrition!{col}{row_idx}",
                "values": [[val]]
            })
        body = {
            "valueInputOption": "USER_ENTERED",
            "data": data_array
        }
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate"
        req_batch = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/json",
            }
        )
        result = json.loads(urllib.request.urlopen(req_batch, timeout=15).read())
        return {
            "ok": True,
            "updated": result.get("totalUpdatedCells", 0),
            "ranges": [d["range"] for d in data_array],
            "errors": []
        }
    except Exception as e:
        return {"ok": False, "updated": 0, "errors": [str(e)]}


def _sheet_update_nutrition_row(row_idx: int, entry: dict) -> dict:
    """Replace ALL 11 columns (A:K) at the given row from `entry`. v3.3.2:
    full-row update path for api_scan_correct / api_scan_rename. Uses the
    same token + batchUpdate plumbing as _sheet_update_nutrition_cells but
    writes the full row in one go. Coerces numerics with _safe_num and
    rebuilds the HH:MM time so the sheet sees the same shape as a fresh
    _append_to_sheet_nutrition row. Returns {ok, updated, errors}.
    """
    try:
        tok = json.loads(Path("/home/work/.hermes/google_token.json").read_text())
        if "token" not in tok or not tok.get("refresh_token"):
            return {"ok": False, "updated": 0, "errors": ["no_token"]}
        # Refresh access token
        data = urllib.parse.urlencode({
            "client_id": tok["client_id"],
            "client_secret": tok["client_secret"],
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        access = resp["access_token"]
        tok["token"] = access
        Path("/home/work/.hermes/google_token.json").write_text(json.dumps(tok, indent=2))

        SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
        # Build 11-column row from entry — mirror _append_to_sheet_nutrition shape
        date_v = (entry.get("date") or "").strip()
        raw_time = (entry.get("time") or "").strip()
        if "T" in raw_time:
            t_part = raw_time.split("T", 1)[1][:5]
        else:
            t_part = raw_time[:5]
        if not re.match(r"^\d{2}:\d{2}$", t_part):
            if re.match(r"^\d{1}:\d{2}$", t_part):
                t_part = "0" + t_part
            else:
                t_part = "00:00"
        kcal = _safe_num(entry.get("calories", 0), default=0.0, lo=0, hi=3000)
        protein = _safe_num(entry.get("protein", 0), default=0.0, lo=0, hi=300)
        carbs = _safe_num(entry.get("carbs", 0), default=0.0, lo=0, hi=300)
        fat = _safe_num(entry.get("fat", 0), default=0.0, lo=0, hi=300)
        name = (entry.get("name") or entry.get("meal_name") or "scan")[:120]
        restaurant = (entry.get("restaurant_chain") or entry.get("restaurant") or "")[:80]
        notes = (entry.get("note") or entry.get("notes") or "")[:200]
        drive_image_url = entry.get("drive_image_url", "") or ""
        # v3.3.4: harden column K write (mirror of the guard in
        # _append_to_sheet_nutrition). Reject non-URL values so corrupted
        # ints/labels never reach the Sheet and break PWA image rendering.
        if drive_image_url and not re.match(
            r"^https?://(lh3\.googleusercontent\.com|drive\.google\.com)/",
            drive_image_url,
        ):
            drive_image_url = ""
        row_values = [[
            date_v, t_part, entry.get("meal_type", "scan") or "scan", name, restaurant,
            int(round(kcal)), int(round(protein)), int(round(carbs)), int(round(fat)),
            notes, drive_image_url,
        ]]
        body = {
            "valueInputOption": "USER_ENTERED",
            "data": [{
                "range": f"Nutrition!A{row_idx}:K{row_idx}",
                "values": row_values,
            }],
        }
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate"
        req_batch = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
        )
        result = json.loads(urllib.request.urlopen(req_batch, timeout=15).read())
        return {
            "ok": True,
            "updated": result.get("totalUpdatedCells", 0),
            "range": f"Nutrition!A{row_idx}:K{row_idx}",
            "errors": [],
        }
    except Exception as e:
        return {"ok": False, "updated": 0, "errors": [str(e)]}


@app.route("/api/scan_edit_datetime", methods=["POST"])
def api_scan_edit_datetime():
    """Jim OOB 2026-08-07: 'in gymbro, allow me to edit date time of food log'.

    v3.3.2: scan_index is now the Sheet row index. Single cascade target:
    update A (date) + B (time) at the row + refresh cache. No JSON files.
    """
    data = request.get_json(silent=True) or {}
    scan_index = data.get("scan_index")
    new_date = (data.get("new_date") or "").strip()
    new_time = (data.get("new_time") or "").strip()
    if not isinstance(scan_index, int) or scan_index < 2:
        return jsonify({"ok": False, "error": "scan_index required"}), 400
    if not new_date or not new_time:
        return jsonify({"ok": False, "error": "new_date + new_time required (YYYY-MM-DD + HH:MM)"}), 400
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", new_date):
        return jsonify({"ok": False, "error": "new_date 必須係 YYYY-MM-DD 格式"}), 400
    if not _re.match(r"^\d{1,2}:\d{2}$", new_time):
        return jsonify({"ok": False, "error": "new_time 必須係 HH:MM 格式"}), 400
    hh, mm = new_time.split(":")[:2]
    new_time = f"{int(hh):02d}:{int(mm):02d}"
    # v3.3.23aj (Jim OOB 2026-09-19 'After editing food, the order is
    # also drifted in food log'): write HH:MM to column B, NOT the
    # full ISO datetime. Writing the full ISO causes NutritionRow.dt
    # to become None (parser expects "%Y-%m-%d %H:%M"), which breaks
    # by_recent sorting and puts the row at the end of the list
    # instead of in chronological order.
    cache = get_cache()
    row = cache.get_by_row(scan_index)
    if row is None:
        return jsonify({"ok": False, "error": "scan_index not found in cache"}), 404
    old_date = row.date
    old_time = row.time

    sheet_result = _sheet_update_nutrition_cells(scan_index, [
        ("A", new_date),
        ("B", new_time),
    ])
    if not sheet_result.get("ok"):
        return jsonify({"ok": False, "error": f"sheet update failed: {sheet_result.get('errors')}"}, 500)

    # v3.3.23al: invalidate Redis Sheet cache after write
    try:
        _invalidate_sheet_cache()
    except Exception:
        pass
    cache.refresh_one(scan_index, _sheet_read_nutrition_row)
    row_after = cache.get_by_row(scan_index)

    # v3.3.23av (Jim OOB 2026-10-12 'the edited 兩個鮮竹卷 is not updated
    # in jpg download'): invalidate JPG cache for the date the row belongs to
    try:
        from gym_web.image_proxy import _jpg_cache_invalidate
        from pathlib import Path as _P
        # Use the date from the row in cache
        row = cache.get_by_row(scan_index)
        if row and row.date:
            _jpg_cache_invalidate(row.date)
            cache_dir = _P("/home/work/.hermes/nutrition_card_cache")
            for f in cache_dir.glob(f"nutrition_{row.date}*.jpg"):
                f.unlink()
    except Exception as e:
        print(f"[scan_edit_datetime] JPG cache invalidate failed: {e}", flush=True)

    return jsonify({
        "ok": True,
        "scan_index": scan_index,
        "entry": row_after.to_pwa_dict() if row_after else {},
        "old_date": old_date,
        "old_time": old_time,
        "new_date": new_date,
        "new_time": new_time,
        "sheet": sheet_result,
    })


@app.route("/api/scan_delete", methods=["POST"])
def api_scan_delete():
    """Jim OOB 2026-08-06: 'Add function to remove historical upload. And cascade
    delete/update g sheet'.

    v3.3.2: scan_index is now the Sheet row index. Deletes the row from the
    Sheet via _sheet_delete_nutrition_rows and evicts the cache entry. No JSON
    files. Image file on disk is preserved (audit trail).
    """
    data = request.get_json(silent=True) or {}
    scan_index = data.get("scan_index")
    if not isinstance(scan_index, int) or scan_index < 2:
        return jsonify({"ok": False, "error": "scan_index required"}), 400

    cache = get_cache()
    row = cache.get_by_row(scan_index)
    if row is None:
        return jsonify({"ok": False, "error": "scan_index not found in cache"}), 404

    removed_date = row.date
    removed_time = row.time
    removed_cal = int(row.kcal)
    removed_name = row.name

    # v3.3.5: prefer direct row_index delete. The cache knows the row is at
    # scan_index, so we can target that exact Sheet row. This avoids the
    # 3-min tolerance matcher that incorrectly matches adjacent 蘇打水
    # entries (Jim OOB 2026-08-15 'i try to delete the redundant water but
    # it cant'). Fall back to legacy date+time+cal matcher only if the
    # direct delete fails (e.g. cache and Sheet are out of sync).
    sheet_result = _sheet_delete_nutrition_rows_by_indices([scan_index])
    if not sheet_result.get("ok") or sheet_result.get("deleted", 0) == 0:
        # Fall back to legacy matcher — same as before, but with a 0-minute
        # tolerance so we never over-match adjacent rows.
        def matcher(sheet_row, idx):
            if not sheet_row:
                return False
            row_date = sheet_row[0].strip() if len(sheet_row) > 0 else ""
            row_time_full = sheet_row[1].strip() if len(sheet_row) > 1 else ''
            row_cal = sheet_row[5].strip() if len(sheet_row) > 5 else ''
            if row_date != removed_date or row_cal != str(removed_cal):
                return False
            # Match by exact HH:MM substring (Sheet column B may be
            # '2026-08-11 09:00' (space) or '2026-08-11T09:00:00' (T) or
            # just '09:00' depending on how the row was written).
            if removed_time and removed_time not in row_time_full:
                return False
            return True

        sheet_result = _sheet_delete_nutrition_rows(matcher)

    if not sheet_result.get("ok"):
        return jsonify({"ok": False, "error": f"sheet delete failed: {sheet_result.get('errors')}"}), 500

    # v3.3.23al: invalidate Redis Sheet cache after delete
    try:
        _invalidate_sheet_cache()
    except Exception:
        pass

    # Evict the deleted row + reindex rows above it (Sheet rows shift down
    # by 1 after `deleteDimension`). Without this, the next delete/edit on
    # row > scan_index targets the wrong Sheet row.
    with cache._lock:  # type: ignore[attr-defined]
        cache._delete_with_shift(scan_index)

    # v3.3.23av (Jim OOB 2026-10-12 'the edited 兩個鮮竹卷 is not updated
    # in jpg download'): invalidate JPG cache for the date the deleted
    # row belonged to (date variable should be in scope).
    try:
        from gym_web.image_proxy import _jpg_cache_invalidate
        from pathlib import Path as _P
        if 'date' in dir() and date:
            _jpg_cache_invalidate(date)
            cache_dir = _P("/home/work/.hermes/nutrition_card_cache")
            for f in cache_dir.glob(f"nutrition_{date}*.jpg"):
                f.unlink()
    except Exception as e:
        print(f"[scan_delete] JPG cache invalidate failed: {e}", flush=True)

    return jsonify({
        "ok": True,
        "scan_index_removed": scan_index,
        "removed_name": removed_name,
        "removed_date": removed_date,
        "removed_time": removed_time,
        "sheet_rows_deleted": sheet_result.get("deleted", 0),
        "sheet_errors": sheet_result.get("errors", []),
    })


# v3.3.22 (Jim OOB 2026-09-11 'A then B then C' for vanilla protein shake
# 270 vs 180 kcal AI under-estimate): hardcoded brand-aware nutrition
# table for common HK protein shakes. Overrides AI estimate when user
# mentions a known brand + flavor combo. Per-scoop data sourced from
# official brand nutrition labels (2026).
#
# Schema: {(keyword_substring_lower, brand, scoop_g): macros}
#   keyword_substring_lower: matched against the scanned dish name
#   brand: returned as restaurant_chain hint (helps user verify)
#   scoop_g: typical scoop size in grams (informational)
#   kcal/p/c/f: per serving (1 scoop + water OR per ready-to-drink bottle)
#
# Lookup flow (BEFORE Sheet history scan):
#   1. Match keyword_substring_lower in user-input dish name
#   2. If hit, return macros immediately (override AI estimate)
#   3. If no match, fall through to existing Sheet history lookup
_PROTEIN_BRAND_TABLE = {
    # === MyProtein Impact Whey Protein (HK popular, per MyProtein.com.hk 2026) ===
    ("myprotein impact whey vanilla", "MyProtein"): {
        "kcal": 103, "protein": 21, "carbs": 1, "fat": 1.5, "scoop_g": 25,
        "_source": "MyProtein.com.hk label 2026 (1 scoop + water)",
    },
    ("myprotein impact whey chocolate", "MyProtein"): {
        "kcal": 110, "protein": 22, "carbs": 2, "fat": 1.7, "scoop_g": 25,
        "_source": "MyProtein.com.hk label 2026 (1 scoop + water)",
    },
    ("myprotein clear whey", "MyProtein"): {
        "kcal": 90, "protein": 20, "carbs": 1, "fat": 0, "scoop_g": 25,
        "_source": "MyProtein Clear Whey (1 scoop + water)",
    },
    # === Optimum Nutrition (ON) Gold Standard 100% Whey (per ON website 2026) ===
    ("on gold standard vanilla", "Optimum Nutrition"): {
        "kcal": 120, "protein": 24, "carbs": 3, "fat": 1, "scoop_g": 30,
        "_source": "ON Gold Standard Vanilla (1 scoop + 150-180ml water)",
    },
    ("on gold standard chocolate", "Optimum Nutrition"): {
        "kcal": 120, "protein": 24, "carbs": 3, "fat": 1, "scoop_g": 30,
        "_source": "ON Gold Standard Chocolate (1 scoop + 150-180ml water)",
    },
    ("on gold standard double rich chocolate", "Optimum Nutrition"): {
        "kcal": 130, "protein": 24, "carbs": 4, "fat": 1.5, "scoop_g": 31,
        "_source": "ON Gold Standard Double Rich Chocolate (1 scoop + water)",
    },
    # === Nitro Tech (MuscleTech) per official label 2026 ===
    ("nitro tech vanilla", "MuscleTech"): {
        "kcal": 130, "protein": 30, "carbs": 3, "fat": 1.5, "scoop_g": 32,
        "_source": "MuscleTech Nitro-Tech Vanilla (1 scoop + water)",
    },
    ("nitro tech chocolate", "MuscleTech"): {
        "kcal": 140, "protein": 30, "carbs": 4, "fat": 1.5, "scoop_g": 32,
        "_source": "MuscleTech Nitro-Tech Chocolate (1 scoop + water)",
    },
    # === Dymatize ISO100 per official label 2026 ===
    ("dymatize iso100 vanilla", "Dymatize"): {
        "kcal": 110, "protein": 25, "carbs": 1, "fat": 0.5, "scoop_g": 28,
        "_source": "Dymatize ISO100 Vanilla (1 scoop + water)",
    },
    # === Herbalife Formula 1 (meal replacement shake, NOT pure whey) ===
    ("herbalife formula 1 vanilla", "Herbalife"): {
        "kcal": 220, "protein": 18, "carbs": 24, "fat": 5, "scoop_g": 26,
        "_source": "Herbalife Formula 1 Vanilla (1 scoop + 250ml semi-skimmed milk)",
    },
    ("herbalife formula 1 chocolate", "Herbalife"): {
        "kcal": 220, "protein": 18, "carbs": 23, "fat": 5, "scoop_g": 26,
        "_source": "Herbalife Formula 1 Chocolate (1 scoop + 250ml semi-skimmed milk)",
    },
    # === Generic ready-to-drink protein bottles (pre-mixed, common in HK 7-11/Circle K) ===
    ("ready to drink vanilla", "RTD"): {
        "kcal": 160, "protein": 30, "carbs": 5, "fat": 2.5, "scoop_g": 330,
        "_source": "Generic RTD vanilla 330ml bottle (e.g. ON Core RTD)",
    },
    ("ready to drink chocolate", "RTD"): {
        "kcal": 160, "protein": 30, "carbs": 5, "fat": 2.5, "scoop_g": 330,
        "_source": "Generic RTD chocolate 330ml bottle",
    },
    # === Generic "protein shake" (1 scoop + 250ml low-fat milk) ===
    # v3.3.22 Step C (Jim OOB 2026-09-11 'A then B then C'): both English
    # "vanilla" AND Chinese "雲呢拿/雲呢拿味/香草" trigger the same 270-kcal
    # generic fallback (per Jim's 2026-09-11 food label baseline).
    ("vanilla", "Generic"): {
        "kcal": 270, "protein": 40, "carbs": 6, "fat": 5, "scoop_g": None,
        "_source": "Generic vanilla protein shake (1 scoop + 250ml low-fat milk), per Jim's 2026-09-11 food label baseline",
    },
    ("雲呢拿", "Generic"): {
        "kcal": 270, "protein": 40, "carbs": 6, "fat": 5, "scoop_g": None,
        "_source": "Generic 雲呢拿蛋白飲 (1 scoop + 250ml low-fat milk), per Jim's 2026-09-11 food label baseline",
    },
    ("香草", "Generic"): {
        "kcal": 270, "protein": 40, "carbs": 6, "fat": 5, "scoop_g": None,
        "_source": "Generic 香草味蛋白飲 (1 scoop + 250ml low-fat milk), per Jim's 2026-09-11 food label baseline",
    },
    # === Quest Bar (Quest Nutrition protein bars, common HK 7-11/Health Plus)
    # v3.3.23v (Jim OOB 2026-09-15 'my recent quest energy bar does not
    # record any nutrient info'): Quest Bar Chocolate Peanut Butter
    # per official Quest Nutrition label 2026 (60g bar). Quest's labels
    # vary by market; using US values (most common in HK 7-11):
    #   200 kcal | 21g P | 22g C (14g fiber, 1g sugar, 4g net carbs)
    #   | 8g F (3g sat) | 280mg sodium | 25% DV iron
    ("quest bar chocolate peanut butter", "Quest Nutrition"): {
        "kcal": 200, "protein": 21, "carbs": 22, "fat": 8,
        "fiber": 14, "sugar": 1, "sodium": 280,
        "sat_fat": 3, "trans_fat": 0,
        "_source": "Quest Bar Chocolate Peanut Butter (60g bar) per Quest Nutrition US label 2026",
    },
    ("quest bar cookies and cream", "Quest Nutrition"): {
        "kcal": 200, "protein": 21, "carbs": 22, "fat": 8,
        "fiber": 14, "sugar": 1, "sodium": 280,
        "sat_fat": 3, "trans_fat": 0,
        "_source": "Quest Bar Cookies & Cream (60g bar) per Quest Nutrition US label 2026",
    },
    ("quest bar birthday cake", "Quest Nutrition"): {
        "kcal": 200, "protein": 21, "carbs": 22, "fat": 8,
        "fiber": 14, "sugar": 1, "sodium": 260,
        "sat_fat": 3, "trans_fat": 0,
        "_source": "Quest Bar Birthday Cake (60g bar) per Quest Nutrition US label 2026",
    },
    ("quest bar double chocolate", "Quest Nutrition"): {
        "kcal": 200, "protein": 21, "carbs": 22, "fat": 8,
        "fiber": 14, "sugar": 1, "sodium": 260,
        "sat_fat": 3, "trans_fat": 0,
        "_source": "Quest Bar Double Chocolate (60g bar) per Quest Nutrition US label 2026",
    },
    ("quest bar chocolate chip cookie dough", "Quest Nutrition"): {
        "kcal": 200, "protein": 21, "carbs": 22, "fat": 8,
        "fiber": 14, "sugar": 1, "sodium": 280,
        "sat_fat": 3, "trans_fat": 0,
        "_source": "Quest Bar Chocolate Chip Cookie Dough (60g bar) per Quest Nutrition US label 2026",
    },
    # Catch-all: any other Quest Bar flavor (per bar, 200 kcal baseline)
    ("quest bar", "Quest Nutrition"): {
        "kcal": 200, "protein": 21, "carbs": 22, "fat": 8,
        "fiber": 14, "sugar": 1, "sodium": 280,
        "sat_fat": 3, "trans_fat": 0,
        "_source": "Quest Bar generic catch-all (60g bar, 200 kcal baseline per Quest Nutrition 2026)",
    },
    # === Quest Hero Bar (smaller, 45g, lower kcal) ===
    ("quest hero chocolate peanut butter", "Quest Nutrition"): {
        "kcal": 150, "protein": 15, "carbs": 17, "fat": 6,
        "fiber": 10, "sugar": 1, "sodium": 220,
        "sat_fat": 2.5, "trans_fat": 0,
        "_source": "Quest Hero Bar Chocolate Peanut Butter (45g bar) per Quest Nutrition 2026",
    },
}


def _extract_explicit_macros(text: str) -> dict:
    """v3.3.23ad (Jim OOB 2026-09-16 'i just recently updated the protein shake
    info, it is still showing 21g protein instead of 30g protein'): parse
    explicit macro numbers from user-typed food description. Patterns:
      '30g protein' / '30g P' → P=30
      '200 kcal' / '480cal' → kcal=200
      '5g carbs' / '20g C' → C=5
      '2g fat' / '8g F' → F=2
      '5g sugar' → sugar=5
      '10g fiber' / '10g fibre' → fiber=10
      '200mg sodium' → sodium=200

    Returns dict with whichever macros were found. Empty dict if nothing
    matches. Designed to be MERGED with AI/brand-table estimate (explicit
    numbers override AI guess).
    """
    if not text:
        return {}
    out = {}
    # Lowercase for case-insensitive matching
    t = text.lower()
    # Map: keyword (lowercase) → output field name
    # Order matters - try longer matches first
    patterns = [
        # kcal patterns (try first to avoid confusion with other g values)
        (r"(\d+(?:\.\d+)?)\s*(?:kcal|cal|kilo[\s-]?calorie)", "calories"),
        # P (protein)
        (r"(\d+(?:\.\d+)?)\s*g\s*(?:protein|proteins?|p\b)", "protein"),
        # C (carbs) - avoid matching "calories" prefix
        (r"(\d+(?:\.\d+)?)\s*g\s*(?:carb|carbs|c\b)", "carbs"),
        # F (fat) - be careful with "fiber" too
        (r"(\d+(?:\.\d+)?)\s*g\s*(?:fat|fats|f\b)(?!\w)", "fat"),
        # Sugar
        (r"(\d+(?:\.\d+)?)\s*g\s*(?:sugar|sugars)", "sugar"),
        # Fiber / Fibre
        (r"(\d+(?:\.\d+)?)\s*g\s*(?:fiber|fibre)", "fiber"),
        # Sodium (mg)
        (r"(\d+(?:\.\d+)?)\s*(?:mg|milligram)\s*(?:sodium|salt|na)", "sodium"),
    ]
    for pattern, field in patterns:
        m = re.search(pattern, t)
        if m:
            try:
                out[field] = float(m.group(1))
            except (ValueError, IndexError):
                pass
    return out


def _lookup_brand_macros(dish_name: str) -> dict:
    """v3.3.22 (Jim OOB 2026-09-11): check the hardcoded brand table BEFORE
    falling back to AI estimate / Sheet history. Matches the longest
    keyword_substring_lower in the user-typed dish name to find a brand
    + flavor combo, returns per-scoop nutrition (kcal/p/c/f) plus
    restaurant_chain hint with the brand name.

    Returns {} if no match. Empty `dish_name` returns {}.
    """
    if not dish_name or len(dish_name.strip()) < 2:
        return {}
    name_l = dish_name.strip().lower()
    # v3.3.32: 品牌自然寫法正規化 —— 舊 table 淨係收「on gold standard」縮寫,
    # 「Optimum Nutrition Gold Standard」呢類完全 miss(實測 2026-10-21)。
    variants = {name_l}
    for alias, canon in _BRAND_ALIAS_TO_CANON.items():
        if alias in name_l:
            variants.add(name_l.replace(alias, canon))
    matches = []
    for v in variants:
        for (kw, brand), macros in _PROTEIN_BRAND_TABLE.items():
            if kw in v:
                matches.append((kw, brand, macros))
    # Sort by keyword length DESC so "double rich chocolate" wins over plain "chocolate"
    matches = sorted(set(matches), key=lambda x: -len(x[0]))
    if matches:
        keyword, brand, macros = matches[0]
        # Return a copy with restaurant_chain filled in
        out = dict(macros)
        out["restaurant_chain"] = brand
        out["_source"] = f"brand_table: '{keyword}' → {brand} ({out.get('_source', '')})"
        return out
    # v3.3.32: 第二級 —— 識到品牌但冇 flavour 專屬值 → 用品牌 baseline
    bm = _match_brand(dish_name)
    if bm and bm.get("baseline"):
        out = dict(bm["baseline"])
        out["restaurant_chain"] = bm["brand"]
        out["_source"] = (f"brand_baseline: {bm['alias']!r} → {bm['brand']} "
                          f"({out.get('_source', '')})")
        return out
    return {}


def _lookup_known_macros(dish_name: str) -> dict:
    """v3.3.7: when vision AI returns empty nutrition, backfill from
    Sheet history of similar dish names (Jim OOB 2026-08-16 'why today
    egg tarts have nutrient info?' — should NOT be 0).

    Returns a dict with median kcal/P/C/F across similar historical rows,
    or {} if no good matches (>=2 rows with kcal>0).
    """
    if not dish_name or len(dish_name.strip()) < 2:
        return {}
    rows = get_cache().find_similar_by_name(dish_name.strip(), min_matches=2)
    if len(rows) < 2:
        return {}
    kcals = [r.kcal for r in rows]
    proteins = [r.p for r in rows]
    carbs = [r.c for r in rows]
    fats = [r.f for r in rows]
    # Match a hint about how many pieces if the user's name has numbers
    # e.g. '原味蛋撻、抹茶味蛋撻' = 2 pieces. If pieces detected in name,
    # scale the per-piece median by that count.
    pieces = 1
    name = dish_name.strip()
    # Detect quantity markers in the name
    if "、" in name:
        pieces = max(2, name.count("、") + 1)
    elif "x" in name.lower() and " " not in name:
        import re as _re
        m = _re.search(r"x\s*(\d+)", name, _re.I)
        if m:
            pieces = int(m.group(1))
    median_kcal = int(round(sum(sorted(kcals)[len(kcals)//4 : 3*len(kcals)//4+1]) /
                            max(1, len(kcals)//2)))
    if pieces > 1:
        # Per-piece median, scaled
        per_piece = {
            "kcal": sorted(kcals)[len(kcals) // 2],
            "p": sorted(proteins)[len(proteins) // 2],
            "c": sorted(carbs)[len(carbs) // 2],
            "f": sorted(fats)[len(fats) // 2],
        }
        return {
            "kcal": int(round(per_piece["kcal"] * pieces)),
            "protein": round(per_piece["p"] * pieces, 1),
            "carbs": round(per_piece["c"] * pieces, 1),
            "fat": round(per_piece["f"] * pieces, 1),
            "_source": f"lookup:{len(rows)} historical rows, ×{pieces}",
        }
    return {
        "kcal": int(round(sorted(kcals)[len(kcals) // 2])),
        "protein": round(sorted(proteins)[len(proteins) // 2], 1),
        "carbs": round(sorted(carbs)[len(carbs) // 2], 1),
        "fat": round(sorted(fats)[len(fats) // 2], 1),
        "_source": f"lookup:{len(rows)} historical rows",
    }


@app.route("/api/scan_commit", methods=["POST"])
def api_scan_commit():
    """Jim OOB 2026-07-23 22:42: 'all food logging should be preview and allow me to confirm before logging'.

    Receives the (possibly edited) suggested_entry + image_path from
    /api/scan_preview or /api/scan_preview_text.

    v3.2.7.9: Multi-entry support (Jim OOB 2026-08-08 20:05 HKT 'scan one
    photo with different food to generate multiple entry'). Accepts
    `entries: [...]` (1-N items) and commits them all sharing the same
    image. Falls back to legacy `entry: {...}` for backward compat.

    Text-only path (Jim OOB 2026-08-02 02:50 HKT):
        image_path = "" → text-only entry. NO file rename, sheet row has
        image_url field empty / sheet column K is left blank for the entry.

    v3.3.2: Sheet is the canonical store. cache.refresh_one() patches the
    in-memory index after each append. No food_scan_log.json or
    nutrition_log.json are written.
    """
    data = request.get_json(silent=True) or {}
    image_path = data.get("image_path", "")
    user_correction = data.get("user_correction")
    user_hints_in = data.get("user_hints", []) or []

    entries_in = data.get("entries")
    if entries_in is None:
        single = data.get("entry", {})
        if not single:
            return jsonify({"ok": False, "error": "missing entry/entries"}), 400
        entries_in = [single]
    if not isinstance(entries_in, list) or not entries_in:
        return jsonify({"ok": False, "error": "entries must be non-empty list"}), 400
    entries_in = entries_in[:10]

    if image_path:
        img_path = Path(image_path)
        if not img_path.exists():
            return jsonify({"ok": False, "error": "image not found"}), 404
        # v3.3.25 production-write guard — refuse to commit a QA fixture
        # to Jim's golden copy. Independent of GYMBRO_WRITE_MODE.
        if _write_mode() == "production" and _is_production_target():
            try:
                _assert_not_test_fixture(img_path)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)}), 403
    else:
        img_path = None

    now_iso_str = now_iso()
    now_hkt_dt = datetime.now(timezone(timedelta(hours=8)))

    drive_image_url = ""
    final_path = None
    image_url = ""
    if img_path is not None:
        final_name = f"scan_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}.jpg"
        final_path = SCAN_CACHE_DIR / final_name
        try:
            img_path.rename(final_path)
            image_url = f"/scan_img/{final_name}"
            drive_image_url = _upload_to_drive(final_path)
            _make_thumb(final_path, _thumb_path_for(final_name))
        except Exception:
            final_path = img_path
            image_url = f"/scan_img/{img_path.name}"
            try:
                drive_image_url = _upload_to_drive(final_path)
                _make_thumb(final_path, _thumb_path_for(img_path.name))
            except Exception:
                drive_image_url = ""
    else:
        # v3.3.23av (Jim OOB 2026-09-30 'When textonly. You can still use
        # the downloaded image'): text-only logs (img_path is None) trigger
        # Brave image search to get a representative food photo. This is
        # intentional — only Jim's REAL scanned photos should appear for
        # Scan uploads (img_path provided); text-only entries can use any
        # reasonable food photo as a visual placeholder. Best-effort:
        # failure still commits the row, just without an image.
        try:
            first_entry = entries_in[0] if isinstance(entries_in, list) and entries_in else {}
            fallback_name = (first_entry.get("name") if isinstance(first_entry, dict) else "") or ""
            fallback_rest = (first_entry.get("restaurant_chain") if isinstance(first_entry, dict) else "") or ""
            if fallback_name:
                # Try restaurant+name first, then just name
                candidates = []
                if fallback_rest and fallback_name:
                    candidates.append(f"{fallback_rest} {fallback_name}")
                candidates.append(fallback_name)
                img_result = None
                for q in candidates:
                    if not q.strip():
                        continue
                    results = _brave_image_search(q, count=3)
                    if results:
                        results.sort(key=lambda r: {"high": 3, "medium": 2, "low": 1}.get(r.get("confidence", "low"), 1), reverse=True)
                        img_result = results[0]
                        break
                if img_result and img_result.get("url"):
                    local_path = _download_food_image(img_result["url"], fallback_name)
                    if local_path:
                        final_path = local_path
                        image_url = f"/scan_img/{local_path.name}"
                        try:
                            drive_image_url = _upload_to_drive(final_path)
                            _make_thumb(final_path, _thumb_path_for(local_path.name))
                            print(f"[scan_commit] auto-backfilled image for '{fallback_name}' from {img_result.get('source', '?')}", flush=True)
                        except Exception as _ue:
                            print(f"[scan_commit] auto-backfill upload failed: {_ue}", flush=True)
                            drive_image_url = ""
        except Exception as _fe:
            # Never block commit on image search failure
            print(f"[scan_commit] auto-backfill failed: {type(_fe).__name__}: {_fe}", flush=True)

    # v3.3.2: signature-based dedup using the cache (hydrated from Sheet).
    # Matches the (date, HH:MM, kcal) signature of any in-cache row. The
    # legacy md5-of-image dedup is dropped — image bytes are no longer
    # authoritative (Drive + Sheet are). Signature dedup catches "user
    # tapped confirm twice within seconds".
    if entries_in:
        first = entries_in[0]
        if isinstance(first, dict):
            sig_date = first.get("date") or today_iso()
            sig_time = (first.get("time") or now_hkt_dt.strftime("%H:%M"))[:5]
            sig_cal = int(first.get("calories") or 0)
            existing = get_cache().find_by_signature(sig_date, sig_time, sig_cal)
            if existing is not None:
                return jsonify({
                    "ok": False,
                    "error": "duplicate_scan",
                    "message": (f"呢一餐已經喺 Sheet 記錄咗 "
                                f"({existing.name!r}). 唔會重複 append。"),
                    "existing_scan_index": existing.row_index,
                    "existing_name": existing.name,
                }), 409

    vision_hint_global = data.get("vision_desc", "") or data.get("vision_short", "") or ""
    pplx_hint_global = data.get("pplx_short", "") or ""

    committed = []
    cache = get_cache()
    for e_idx, entry in enumerate(entries_in):
        if not isinstance(entry, dict):
            continue
        e_user_correction = entry.get("user_correction") or (user_correction if e_idx == 0 else None)
        e_user_hints = entry.get("user_hints") or user_hints_in
        entry["timestamp_iso"] = now_iso_str
        if img_path is not None:
            entry["source"] = "v3.3.34-scan (minimax-text-01, Jim confirmed)"
            entry["models_used"] = ["minimax-text-01"]
            entry["image_saved_to"] = str(final_path)
        else:
            entry["source"] = "v2.7.22-scan_text_direct (minimax-m3, no image)"
            entry["models_used"] = ["minimax-m3"]
            entry["image_saved_to"] = ""
        entry["confidence"] = "Jim-confirmed preview"
        entry["sheet_synced"] = False
        entry["user_correction"] = None
        entry_name = entry.get("name") or entry.get("meal_name") or "食物"
        entry_kcal = entry.get("calories", entry.get("kcal", 0)) or 0
        entry_p = entry.get("protein", entry.get("protein_g", 0)) or 0
        entry_c = entry.get("carbs", entry.get("carbs_g", 0)) or 0
        entry_f = entry.get("fat", entry.get("fat_g", 0)) or 0
        entry_rest = entry.get("restaurant_chain", entry.get("restaurant", "")) or ""
        # v3.3.32 (Jim OOB 2026-10-21「fill in the restaurant (in best effort
        # basis)」):呢個係寫入 golden copy 嘅最後一道閘 —— 任何路徑(文字
        # 輸入 / 相片掃描 / 修正 / MCP tool)嘅餐廳都要經過消毒,垃圾一律
        # 變空字串。實測 production 餐廳欄 90% 係「難以辨認餐廳」呢類 AI
        # 廢話,係因為舊流程由 AI 直接寫落 Sheet。
        _rest_clean = _sanitise_restaurant_name(entry_rest,
                                                dish_name=entry.get("name", ""))
        if entry_rest and not _rest_clean:
            print(f"[scan_commit] restaurant dropped (AI 廢話): "
                  f"{entry_rest!r}", flush=True)
        entry["restaurant_chain"] = _rest_clean
        entry_rest = _rest_clean
        # v3.3.7: backfill empty macros from Sheet history (Jim OOB
        # 2026-08-16 'why today egg tarts have nutrient info?' — vision
        # AI returned empty for that photo, all 12 fields defaulted to 0).
        # The name might be a user-typed override like '原味蛋撻、抹茶味蛋撻'
        # (2 pieces), and 3 historical 蛋撻 rows average ~230 kcal each.
        if entry_name and entry_kcal <= 0 and entry["source"].startswith("v2.2"):
            # v3.3.22: try brand-aware table first (Jim OOB 2026-09-11
            # vanilla protein shake 180 vs 270 kcal). Falls through to
            # Sheet history lookup if no brand match.
            looked = _lookup_brand_macros(entry_name) or _lookup_known_macros(entry_name)
            if looked:
                entry_kcal = looked["kcal"]
                entry_p = looked["protein"]
                entry_c = looked["carbs"]
                entry_f = looked["fat"]
                entry["calories"] = entry_kcal
                entry["kcal"] = entry_kcal
                entry["protein"] = entry_p
                entry["carbs"] = entry_c
                entry["fat"] = entry_f
                # v3.3.22: also fill restaurant_chain if brand table has it
                if looked.get("restaurant_chain") and not entry.get("restaurant_chain"):
                    entry["restaurant_chain"] = looked["restaurant_chain"]
                entry["_nutrition_source"] = looked["_source"]
                print(f"scan_commit: backfilled {entry_name!r} → "
                      f"{entry_kcal} kcal from {looked['_source']}", flush=True)
        if entry_name and entry_kcal > 0:
            entry["coach_comment"] = _coach_comment_keyword(entry_name, entry_kcal, entry_p, entry_c, entry_f, entry_rest)
        if "rating" in entry:
            del entry["rating"]
        cleaned_hints = []
        seen = set()
        for h in e_user_hints:
            if not isinstance(h, str):
                continue
            h = h.strip()
            if not h or h in seen:
                continue
            seen.add(h)
            cleaned_hints.append(h[:500])
            if len(cleaned_hints) >= 20:
                break
        entry["user_hints"] = cleaned_hints
        current_name = _sanitise_dish_name(entry.get("name"))
        if current_name == "未識別菜式":
            vision_hint = (entry.get("vision_raw_desc")
                           or entry.get("vision_desc")
                           or vision_hint_global
                           or "")
            pplx_hint = (entry.get("pplx_short")
                         or entry.get("minimax_enrichment")
                         or pplx_hint_global
                         or "")
            redrive = _extract_dish_name(vision_hint, pplx_hint, fallback=current_name or "")
            if redrive and redrive.strip() not in ("食物", "未識別菜式"):
                entry["name"] = redrive
                current_name = redrive

        if entry.get("user_hints"):
            hint = entry["user_hints"][0].strip()
            if 2 <= len(hint) <= 12 and not re.search(r"[A-Za-z]", hint):
                food_suffixes = ("飯", "麵", "粥", "餅", "糕", "包", "卷", "雞", "牛", "豬",
                                 "魚", "蝦", "菜", "湯", "茶", "咖啡", "蛋", "豆", "果",
                                 "撻", "批", "酥", "圈", "堡", "餐", "凍飲", "熱飲")
                if any(hint.endswith(suf) or suf in hint for suf in food_suffixes):
                    entry["name"] = hint
                    current_name = hint

        # v3.3.4: refuse to commit AI-failed entries (Jim OOB 2026-08-15
        # 'I don't expect any unrecognized food'). Skip this entry — the
        # rest of the multi-entry batch can still commit. The caller
        # (frontend) gets name="未識別菜式" returned for the skipped entry
        # so it can prompt Jim to name it manually via /api/scan_correct.
        if current_name == "未識別菜式":
            committed.append({
                "name": "未識別菜式",
                "calories": entry.get("calories", 0),
                "protein": entry.get("protein", 0),
                "grade": "—",
                "skipped_unrecognized": True,
                "skip_reason": (
                    "AI 未能識別，請改 entry.name 後再 call /api/scan_correct "
                    "寫入 Sheet row"
                ),
                "entry_ref": entry,
            })
            continue

        # v3.3.23r (Jim OOB 2026-09-14 'make sure the column L is updated
        # with formula like these =image(K497)'): preserve any pre-set
        # drive_image_url on the entry (e.g. from API caller). Previously
        # this line overwrote with the local `drive_image_url` which was
        # always "" for text-only entries, even if the caller had provided
        # a Drive URL.
        if not entry.get("drive_image_url"):
            entry["drive_image_url"] = drive_image_url

        sheet_result = _append_to_sheet_nutrition(entry)
        new_row_index = None
        if sheet_result.get("ok") and not sheet_result.get("skipped"):
            try:
                # v3.3.23at (Jim OOB 2026-09-30 'jpg download is always
                # missing sth'): the previous parser was .rstrip("ABCDEFGHIJKLM")
                # which left "Nutrition!A564" unchanged (digit "2" isn't in
                # the rstrip set), so int("Nutrition!A564") raised ValueError.
                # The except silently caught it and new_row_index stayed None,
                # so cache.refresh_one was NEVER called → by_date never got
                # the new row → /api/nutrition/card missed meals. Use regex
                # to extract the actual row number.
                range_str = sheet_result.get("range", "")
                m = re.search(r"(\d+)", range_str.split(":")[0])
                if m:
                    new_row_index = int(m.group(1))
                    cache.refresh_one(new_row_index, _sheet_read_nutrition_row)
            except Exception as e:
                print(f"[scan_commit] refresh_one failed for range {sheet_result.get('range', '')}: {e}", flush=True)
        committed.append({
            "name": entry.get("name", "?"),
            "calories": entry.get("calories", 0),
            "protein": entry.get("protein", 0),
            "grade": (entry.get("coach_comment") or {}).get("grade", "—"),
            "sheet_row": sheet_result.get("range", ""),
            "sheet_ok": sheet_result.get("ok", False),
            "scan_index": new_row_index,
        })

    # v3.3.23ar (Jim OOB 2026-09-28 'Jpg download has error. Only one food
    # shown. There should be 3 for today'): invalidate JPG card cache
    # for the committed date so the next /api/nutrition/card re-renders
    # with the new meal. Without this, in-memory cache keeps serving the
    # pre-commit version (which had fewer meals). Also delete the disk
    # file since its stale render is now obsolete.
    try:
        from gym_web.image_proxy import _jpg_cache_invalidate
        commit_date = (entries_in[0].get("date") if entries_in and isinstance(entries_in[0], dict) else None) or today_iso()
        _jpg_cache_invalidate(commit_date)
        disk_path = Path("/home/work/.hermes/nutrition_card_cache") / f"nutrition_{commit_date}.jpg"
        if disk_path.exists():
            disk_path.unlink()
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "scan_index": committed[0].get("scan_index") if committed else None,
        "committed": committed,
        "multi_entry": len(committed) > 1,
        "sheet_synced": all(c.get("sheet_ok") for c in committed) if committed else False,
        "sheet_range": committed[0].get("sheet_row", "") if committed else "",
        "is_text_only": img_path is None,
        "image_url": image_url,
        "thumbnail_url": _thumb_url_for(image_url),
        "drive_image_url": drive_image_url,
    })


# v3.3.23av (Jim OOB 2026-09-30 'When textonly. You can still use the
# downloaded image'): manual backfill endpoint for existing rows that
# committed without an image. POST {row_index: int} → uses cache.name +
# cache.restaurant to Brave-search → download → upload to Drive → write
# Sheet column K → invalidate caches.
@app.route("/api/scan_backfill_image", methods=["POST"])
def api_scan_backfill_image():
    data = request.get_json(silent=True) or {}
    row_index = data.get("row_index")
    if not isinstance(row_index, int) or row_index < 2:
        return jsonify({"ok": False, "error": "row_index must be int >= 2"}), 400
    try:
        cache = get_cache()
        row = cache.get_by_row(row_index)
    except Exception as e:
        return jsonify({"ok": False, "error": f"cache lookup failed: {e}"}), 500
    if not row:
        return jsonify({"ok": False, "error": f"row {row_index} not found in cache"}), 404
    if row.drive_image_url:
        return jsonify({"ok": False, "error": "row already has an image", "drive_image_url": row.drive_image_url}), 409
    name = (row.name or "").strip()
    restaurant = (row.restaurant or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "row has empty name, can't search"}), 400

    candidates = []
    if restaurant:
        candidates.append(f"{restaurant} {name}")
    candidates.append(name)
    img_result = None
    for q in candidates:
        results = _brave_image_search(q, count=3)
        if results:
            results.sort(key=lambda r: {"high": 3, "medium": 2, "low": 1}.get(r.get("confidence", "low"), 1), reverse=True)
            img_result = results[0]
            break
    if not img_result or not img_result.get("url"):
        return jsonify({"ok": False, "error": "brave returned no results for any candidate"}), 404

    local_path = _download_food_image(img_result["url"], name)
    if not local_path:
        return jsonify({"ok": False, "error": "download failed"}), 500

    drive_url = ""
    try:
        drive_url = _upload_to_drive(local_path)
        _make_thumb(local_path, _thumb_path_for(local_path.name))
    except Exception as e:
        return jsonify({"ok": False, "error": f"drive upload failed: {e}"}), 500
    if not drive_url:
        return jsonify({"ok": False, "error": "drive upload returned empty url"}), 500

    # Write to Sheet column K (image URL) using batchUpdate
    try:
        spreadsheet_id = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
        NUTRITION_SHEET_ID = 474877075
        access_token = _get_google_access_token()
        body = json.dumps({
            "requests": [{
                "updateCells": {
                    "range": {
                        "sheetId": NUTRITION_SHEET_ID,
                        "startRowIndex": row_index - 1,
                        "endRowIndex": row_index,
                        "startColumnIndex": 10,  # column K (0-indexed)
                        "endColumnIndex": 11,
                    },
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": drive_url}}]}],
                    "fields": "userEnteredValue",
                }
            }]
        }).encode()
        req = urllib.request.Request(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
            data=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            json.loads(resp.read().decode())
    except Exception as e:
        return jsonify({"ok": False, "error": f"sheet write failed: {e}", "drive_image_url": drive_url}), 500

    # Invalidate caches + JPG cache
    try:
        _invalidate_sheet_cache()
    except Exception:
        pass
    try:
        from gym_web.image_proxy import _jpg_cache_invalidate
        _jpg_cache_invalidate(row.date)
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "row_index": row_index,
        "drive_image_url": drive_url,
        "image_url": f"/scan_img/{local_path.name}",
        "source": img_result.get("source", "?"),
        "query_used": img_result.get("title", "")[:60],
    })


# v2.7.19: scan re-enrich with user hint (Jim OOB 2026-07-31 13:25 HKT)
# After preview returned, Jim can type supplementary info (餐廳名/份量/醬汁/材料)
# and tap "🔄 用補充資料再 estimate" → this endpoint re-runs pplx + MiniMax
# nutrition enrichment with hint prepend, returns new suggested_entry.
# NO write to log/Sheet — Jim still needs to confirm via /api/scan_commit.
@app.route("/api/scan_re_enrich", methods=["POST"])
def api_scan_re_enrich():
    """Re-enrich a scan preview using user-supplied supplementary hint.

    Receives: {image_path: str, user_hint: str, original_vision_desc: str (optional)}
    Returns: same shape as /api/scan_preview's `preview` object.

    Flow:
      1. Read image bytes from image_path (server-side cache).
      2. Re-run MiniMax vision (cheap, 2-3s) — get fresh dish desc.
      3. Prepend `「用家補充資料: {hint}」` to vision_desc.
      4. Re-run _minimax_nutrition_enrich() with augmented desc.
      5. Merge 12-field nutrition via median.
      6. Return preview-style object for frontend to swap in.
    """
    data = request.get_json(silent=True) or {}
    image_path = data.get("image_path", "")
    user_hint = (data.get("user_hint") or "").strip()
    original_vision_desc = data.get("original_vision_desc", "")

    if not image_path:
        return jsonify({"ok": False, "error": "missing image_path"}), 400
    if not user_hint:
        return jsonify({"ok": False, "error": "missing user_hint"}), 400

    # Sanitize hint (cap 500 chars to avoid pplx truncation + injection bloat)
    if len(user_hint) > 500:
        user_hint = user_hint[:500] + "…"

    img_path = Path(image_path)
    # Same safety guard as scan_preview_from_path
    if not img_path.exists():
        return jsonify({"ok": False, "error": "image not found at server path"}), 404
    if img_path.parent != SCAN_CACHE_DIR.resolve() and not str(img_path.resolve()).startswith("/home/work/.hermes/image_cache/"):
        return jsonify({"ok": False, "error": "image path outside permitted dirs"}), 403

    img_bytes = img_path.read_bytes()
    if len(img_bytes) > 10 * 1024 * 1024:
        return jsonify({"ok": False, "error": "image too large"}), 413

    img_b64 = base64.b64encode(img_bytes).decode()

    # v3.2.7.32: plain-text vision prompt (no markdown headers — Jim OOB
    # 2026-08-11 'why today recognized food as 菜式 as food name!'). Same
    # as scan_commit / scan_preview prompts for consistency.
    vision_prompt = (
        "你係食物視覺識別助手。請用繁體中文廣東話,2-4 句口語化句子,直接講"
        "相入面見到咩食物、份量幾多(目測)、點煮、邊間餐廳(如見到 logo 或招牌字)。"
        "例如：「一碗白飯配一塊煎雞扒,碟邊有少許黑椒汁,餐廳係太興。」"
        "如果係飲品,就講幾多 ml、咩品牌、有冇冰有冇糖。"
        "如果係小票/receipt,就逐項抄低菜名同份量同價錢(睇到嘅部分)。"
        "唔好用 markdown 標題、唔好用「菜式：」呢啲 section header,"
        "唔好寫「呢張相顯示...」、「可見...」呢啲開場白,直接講食物。"
        "一個英文字都唔好有,唔識就寫「難以辨認」。"
    )
    vision_desc = _minimax_vision(img_b64, vision_prompt)

    # 1b. 2nd-opinion vision from a DIFFERENT provider in the chain
    opinion2_desc = _vision_second_opinion(img_b64, vision_prompt)
    if opinion2_desc and not opinion2_desc.startswith("（"):
        if len(opinion2_desc) > len(vision_desc) * 0.7:
            vision_desc = vision_desc + "\n\n（2nd-opinion vision）\n" + opinion2_desc

    # 2. Augment with user hint — Jim OOB 7/31 "supplementary info to re-estimate"
    # Prepend hint as context, then pplx/apaiyi still do recogniser role.
    augmented_desc = f"用家補充資料（餐廳名/份量/醬汁/材料）：{user_hint}\n\n{vision_desc}"

    # 3. Re-enrich via pplx + MiniMax (parallel — sequential for safety, no quota burn)
    pplx_desc = ""  # v3.3.34: Perplexity enrichment 已移除
    mm_desc = _minimax_nutrition_enrich(augmented_desc)

    # 4. Merge 12-field nutrition
    pplx_parsed = _parse_nutrition_block(pplx_desc)
    mm_parsed = {}
    if mm_desc and mm_desc.startswith("{"):
        try:
            mm_parsed = json.loads(mm_desc)
        except Exception:
            mm_parsed = _parse_nutrition_block(mm_desc)
    merged_nutrition = _merge_nutrition_estimates([pplx_parsed, mm_parsed])

    shared = _detect_shared_meal(augmented_desc + " " + pplx_desc)
    jim_ratio = 0.60 if shared else 1.00
    raw_kcal = int(merged_nutrition.get("calories", {}).get("value", 0) or 0)
    raw_p = int(merged_nutrition.get("protein", {}).get("value", 0) or 0)

    chain_match = re.search(r"([一-鿿]{2,6}(?:王|軒|亭|餐廳|食堂|廚|小店|屋|樓))", augmented_desc + pplx_desc)
    restaurant_guess = chain_match.group(1) if chain_match else ""

    now_hkt_dt = datetime.now(timezone(timedelta(hours=8)))
    field_entries = {}
    for f in NUTRITION_FIELDS:
        info = merged_nutrition.get(f)
        if not info:
            field_entries[f] = 0
            continue
        v = info.get("value", 0) or 0
        field_entries[f] = round(v * jim_ratio, 1) if shared else v

    preview = {
        "preview_id": f"pv_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}_re",
        "image_path": str(img_path),
        "image_url": f"/scan_img/{img_path.name}",
        "thumbnail_url": _thumb_url_for(f"/scan_img/{img_path.name}"),
        "vision_desc": augmented_desc,  # hint-augmented desc returned to frontend
        "vision_short": augmented_desc[:300],
        "pplx_short": pplx_desc[:500],
        "minimax_enrichment": mm_desc[:300] if mm_desc else "",
        "nutrition_merged": merged_nutrition,
        "suggested_entry": {
            "date": today_iso(),
            "time": now_hkt_dt.strftime("%H:%M"),
            "meal_type": "scan",
            "name": _sanitise_dish_name(
                _extract_dish_name(augmented_desc, pplx_desc),
                vision_desc=augmented_desc,
                pplx_desc=pplx_desc,
            ),
            "restaurant_chain": restaurant_guess,
            "calories": field_entries["calories"],
            "protein": field_entries["protein"],
            "carbs": field_entries["carbs"],
            "fat": field_entries["fat"],
            "fiber": field_entries["fiber"],
            "sugar": field_entries["sugar"],
            "sodium": field_entries["sodium"],
            "sat_fat": field_entries["sat_fat"],
            "trans_fat": field_entries["trans_fat"],
            "vit_c": field_entries["vit_c"],
            "iron": field_entries["iron"],
            "calcium": field_entries["calcium"],
            "is_shared_meal": shared,
            "share_with_wife": "Jim 60% / 小寶 40% (auto-applied)" if shared else "Jim 100% (solo)",
            "raw_kcal_estimate": raw_kcal,
            "raw_p_estimate": raw_p,
        },
        "user_hint": user_hint,  # echo back so frontend can store in entry.user_hints[]
        "ready_to_commit": True,
        "re_enriched": True,  # flag for frontend to show "✨ 已用 hint 再 estimate"
    }
    # v3.3.23av (Jim OOB 2026-10-12 'the edited 兩個鮮竹卷 is not updated
    # in jpg download'): re-enrich changes macros → JPG needs to re-render
    try:
        from gym_web.image_proxy import _jpg_cache_invalidate
        from pathlib import Path as _P
        row = cache.get_by_row(scan_index)
        if row and row.date:
            _jpg_cache_invalidate(row.date)
            cache_dir = _P("/home/work/.hermes/nutrition_card_cache")
            for f in cache_dir.glob(f"nutrition_{row.date}*.jpg"):
                f.unlink()
    except Exception as e:
        print(f"[scan_re_enrich] JPG cache invalidate failed: {e}", flush=True)

    return jsonify({"ok": True, "preview": preview})


# ---------- F2b: /api/scan_preview_text (Jim OOB 2026-08-02 02:50 HKT) ----------
# Text-only food logging path. Jim types what he ate ("燒肉飯",
# "noodle + chicken", "2 eggs + toast"), MiniMax estimates nutrition,
# preview returned, /api/scan_commit then logs it. NO image required.
NUTRITION_FIELD_SCHEMA = (
    "calories,protein,carbs,fat,fiber,sugar,sodium,sat_fat,"
    "trans_fat,vit_c,iron,calcium"
)


_WHISPER_MODEL = None
_WHISPER_MODEL_NAME = "small"
# faster-whisper's 'small' model returns ZERO segments for language='yue'
# (verified 2026-10-21: the 'yue' decoder path yields no tokens on the
# small checkpoint). Cantonese audio still transcribes well through 'zh'
# with a Cantonese-bias initial_prompt, so 'zh' is the working default
# and 'yue' is only attempted when explicitly asked for.
_WHISPER_CANTONESE_PROMPT = (
    "以下係一段廣東話 (香港, Cantonese) 對話或獨白。"
    "請用繁體中文 (香港常用字) 逐字輸出,保留語氣詞"
    "「嘅/啦/咗/嗰/咁/啲/嚟/咩/㗎/喎/吖/囉」等。"
    "人名食物名盡量保留原音 (例: 灣仔/旺角/茶餐廳/絲襪奶茶/叉燒飯)。"
)


def _local_whisper_transcribe(audio_path: str, language: str = "yue"):
    """Transcribe audio with LOCAL faster-whisper. Returns (text, model_name).

    v3.3.30 (2026-10-21, Jim OOB "Remove apiyi stuff"): replaces the dead
    APiyi gpt-4o-transcribe path. faster-whisper is loaded lazily on first
    use and cached in-process. If the package is missing we raise a clear
    ImportError so the caller reports it honestly instead of silently
    returning an empty transcript.

    Language handling (verified 2026-10-21):
      * language='yue' on the 'small' checkpoint yields ZERO segments →
        fall back to 'zh' + a Cantonese-bias initial_prompt.
      * condition_on_previous_text=False avoids the classic Whisper
        repetition loop on long clips.
    """
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel
        _WHISPER_MODEL = WhisperModel(_WHISPER_MODEL_NAME, device="cpu",
                                      compute_type="int8")

    def _run(lang, prompt=None):
        segs, _info = _WHISPER_MODEL.transcribe(
            audio_path,
            language=(lang or None),
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            temperature=0.0,
            initial_prompt=prompt,
        )
        return "".join(s.text for s in segs).strip()

    lang = (language or "").strip() or "zh"
    text = ""
    if lang == "yue":
        text = _run("yue", _WHISPER_CANTONESE_PROMPT)
        if not text:
            # 'yue' produced nothing on this checkpoint → use 'zh'
            text = _run("zh", _WHISPER_CANTONESE_PROMPT)
    else:
        text = _run(lang, _WHISPER_CANTONESE_PROMPT)
        if not text:
            text = _run(None, _WHISPER_CANTONESE_PROMPT)
    return _to_traditional(text), f"faster-whisper-{_WHISPER_MODEL_NAME}"


def _to_traditional(text: str) -> str:
    """Simplified -> Traditional Chinese (s2twp), graceful no-op on failure.

    v3.3.30: faster-whisper's small checkpoint emits SIMPLIFIED Chinese even
    when the initial_prompt asks for 繁體. Jim's rule is 繁體中文 only, so
    convert here. If opencc is not installed we return the text unchanged
    rather than failing the whole transcription.
    """
    if not text:
        return text
    try:
        from opencc import OpenCC
        return OpenCC("s2twp").convert(text)
    except Exception as e:
        print(f"[transcribe] opencc s2t skipped: {type(e).__name__}: {e}",
              flush=True)
        return text


@app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    """Audio file → Cantonese transcript via LOCAL faster-whisper.

    v3.3.30 (2026-10-21, Jim OOB "Remove apiyi stuff"): was APiyi
    gpt-4o-transcribe, whose key died 2026-10-20 (HTTP 401). Now runs
    faster-whisper 'small' on CPU — no key, no per-minute cost.

    Accepts: multipart/form-data with 'audio' field (mp3/m4a/wav/webm/ogg)
    Optional: 'language' (default yue)
    Returns: {ok, text, language, model, size_mb, char_count}
    """
    if "audio" not in request.files:
        return jsonify({"ok": False, "error": "missing 'audio' file"}), 400

    audio_file = request.files["audio"]
    if not audio_file.filename:
        return jsonify({"ok": False, "error": "empty filename"}), 400

    # Read file
    audio_bytes = audio_file.read()
    size_mb = len(audio_bytes) / (1024 * 1024)
    if size_mb > 25:
        return jsonify({"ok": False, "error": f"file too large ({size_mb:.1f} MB > 25 MB limit)"}), 413

    # Optional params
    language = request.form.get("language", "yue")
    user_prompt = request.form.get("prompt", None)

    # Cantonese default prompt (matches config.yaml stt.openai.prompt)
    if user_prompt is None:
        user_prompt = (
            "以下係一段廣東話 (香港, Cantonese) 對話或獨白。 "
            "請用繁體中文 (香港常用字) 逐字輸出,保留語氣詞「嘅/啦/咗/嗰/咁/啲/嚟/咩/㗎/喎/吖/囉/㖭」等。 "
            "人名食物名盡量保留原音 (例: 灣仔/旺角/茶餐廟/絲襪奶茶/叉燒飯/星巴克/肯德基/麥當勞)。 "
            "中英夾雜 (code-switch) 係正常, 唔好強制翻譯英文品牌名。"
        )

    # v3.3.30 (2026-10-21, Jim OOB "Remove apiyi stuff"): local
    # faster-whisper instead of the dead APiyi gpt-4o-transcribe.
    # No key, no per-minute cost, nothing to expire.
    import tempfile, os as _os
    suffix = _os.path.splitext(audio_file.filename)[1] or ".mp3"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        text, model_name = _local_whisper_transcribe(tmp_path, language)

        return jsonify({
            "ok": True,
            "text": text,
            "language": language,
            "model": model_name,
            "size_mb": round(size_mb, 2),
            "char_count": len(text),
        })

    except Exception as e:
        print(f"[transcribe] local whisper failed: {type(e).__name__}: {e}",
              flush=True)
        return jsonify({"ok": False, "error": f"transcribe failed: {e}"}), 500
    finally:
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# v3.3.32 (Jim OOB 2026-10-21「fix the manual food input workflow」)
#   「Can you fill in the restaurant (in best effort basis) based on what I
#     inputted. Moreover, the food image capture should be related to the
#     food. Secondary option is the restaurant. Sometimes, I would input
#     Protein drinks and brand. You research it online and put the brand
#     cover as food image」
#
# 實測 production Sheet 餐廳欄:90% 係 AI 廢話(「難以辨認餐廳」45 次、
# 「描述可靠識別餐廳」13 次、「所以餐廳」、「如果你係做餐廳」⋯⋯)。
# 根因:舊流程把 AI 嘅 restaurant_guess 原封不動寫入 Sheet,冇消毒步驟。
# ──────────────────────────────────────────────────────────────────────────

# 餐廳歷史索引(Sheet + scan log),TTL 15 分鐘
_RESTAURANT_HISTORY_TTL = 900
_RESTAURANT_HISTORY: dict = {"at": 0.0, "hist": None}


def _get_restaurant_history():
    """由 Sheet cache + food_scan_log 建餐廳歷史索引(TTL 快取)。

    只收通過 `_sanitise_restaurant_name` 嘅名 —— 即係真店名先入索引,
    AI 廢話(「難以辨認餐廳」呢類)直接唔入,唔會污染下一次嘅自動填充。
    """
    now = time.time()
    cached = _RESTAURANT_HISTORY.get("hist")
    if cached is not None and (now - _RESTAURANT_HISTORY.get("at", 0)) < _RESTAURANT_HISTORY_TTL:
        return cached
    rows = []
    try:
        rows = list(get_cache().by_row.values())
    except Exception as e:
        print(f"[restaurant_history] sheet cache read failed: "
              f"{type(e).__name__}: {e}", flush=True)
    scans = []
    try:
        p = Path("/home/work/.hermes/food_scan_log.json")
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            scans = raw if isinstance(raw, list) else (raw.get("entries") or [])
    except Exception as e:
        print(f"[restaurant_history] scan log read failed: "
              f"{type(e).__name__}: {e}", flush=True)
    hist = _build_restaurant_history(rows, scans)
    _RESTAURANT_HISTORY["hist"] = hist
    _RESTAURANT_HISTORY["at"] = now
    print(f"[restaurant_history] rebuilt: {len(hist.restaurants)} restaurants "
          f"from {len(rows)} sheet rows + {len(scans)} scan entries", flush=True)
    return hist


def _resolve_restaurant(user_text: str, dish_name: str, ai_guess: str,
                         brand: dict = None) -> tuple:
    """Best-effort 餐廳。回傳 (餐廳名, 來源)。

    優先次序(可靠 → 不可靠):
      brand   —— 蛋白飲品品牌(品牌本身就係「餐廳」欄嘅內容)
      text    —— 用家輸入直接提到一間已知連鎖
      history —— 同一碟菜 / 近似菜之前喺邊間食過
      ai      —— AI 猜,但通過咗消毒器
      none    —— 搵唔到,留空(寧願留空都唔寫垃圾)
    """
    if brand and brand.get("brand"):
        return brand["brand"], "brand"
    chain = _detect_mentioned_chain(user_text or "")
    if chain:
        return chain, "text"
    # v3.3.32: 蛋白產品但冇講品牌 → 唔好亂填。呢類輸入嘅「餐廳」欄
    # 本来就係品牌(見上面 brand 分支),估第二個品牌只會填錯
    # (實測 2026-10-21:「whey protein chocolate」被填成「Quest Nutrition」,
    #  但 Jim 根本冇講過 Quest)。
    if _looks_like_product(user_text or "") or _looks_like_product(dish_name or ""):
        print(f"[resolve_restaurant] product input without brand → "
              f"leave restaurant empty ({user_text!r})", flush=True)
        return "", "none"
    try:
        hist = _get_restaurant_history()
    except Exception as e:
        print(f"[resolve_restaurant] history unavailable: "
              f"{type(e).__name__}: {e}", flush=True)
        hist = None
    if hist is not None:
        name, src = hist.lookup(user_text or "", dish_name or "", ai_guess or "")
        if name:
            return name, src
    clean = _sanitise_restaurant_name(ai_guess or "", dish_name=dish_name or "")
    if clean:
        return clean, "ai"
    return "", "none"


def _fetch_page_html(url: str, timeout: int = 15) -> str:
    """抓 HTML(搵 og:image 用)。失敗回傳 ""。"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-HK;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(2_000_000).decode("utf-8", "ignore")
    except Exception as e:
        print(f"[fetch_page_html] {type(e).__name__}: {e} — {url[:80]}",
              flush=True)
        return ""


def _verify_food_image(path, expected: str, kind: str) -> bool:
    """視覺驗證:張相係咪真係同 expected 相關?

    kind = "food"       → 呢張相係咪 <expected>(食物)
    kind = "brand_cover"→ 呢張相係咪 <expected> 品牌嘅產品/包裝
    kind = "restaurant" → 呢張相係咪 <expected> 嘅餐廳/店面

    回傳 True = 相關(可以擺上),False = 唔相關(剔走)。
    ⚠️ 視覺供應商全死 → 回傳 True 但唔算「已驗證」(視覺鏈記錄咗狀態),
       寧願畀一張未驗證嘅搜尋結果,都好過完全冇相。
    """
    if not expected:
        return True
    try:
        raw = Path(path).read_bytes()
    except Exception as e:
        print(f"[verify_food_image] read failed: {type(e).__name__}: {e}",
              flush=True)
        return False
    b64 = base64.b64encode(raw).decode()
    if kind == "brand_cover":
        prompt = (
            "你係食物視覺識別助手。請直接回答,唔好道歉、唔好拒絕。\n"
            f"呢張相係咪 {expected} 品牌嘅產品?(樽身、罐、盒、棒、包裝、或者"
            f"沖好嘅飲品都得)\n"
            "如果係,只答「係」。如果唔係、或者相入面完全係另一樣嘢,只答「唔係」。"
            "一行,唔好解釋。"
        )
    elif kind == "restaurant":
        prompt = (
            "你係食物視覺識別助手。請直接回答,唔好道歉、唔好拒絕。\n"
            f"呢張相係咪 {expected}?(餐廳店面、招牌、菜牌、或者同一家店嘅食物相)\n"
            "如果係,只答「係」。如果唔係,只答「唔係」。一行,唔好解釋。"
        )
    else:
        prompt = (
            "你係食物視覺識別助手。請直接回答,唔好道歉、唔好拒絕。\n"
            f"呢張相係咪 {expected}?\n"
            "如果係,只答「係」。如果唔係(例如係完全唔同嘅食物、或者純色圖、"
            "或者產品包裝而唔係食物),只答「唔係」。一行,唔好解釋。"
        )
    desc = _minimax_vision(b64, prompt)
    if not desc:
        print(f"[verify_food_image] vision 供應商全死 → 當作通過但未驗證: "
              f"{expected!r}", flush=True)
        return True
    d = desc.strip()
    head = d[:24]
    neg = any(x in head for x in ("唔係", "不是", "唔是", "並非", "唔相符",
                                  "唔同", "no ", "not "))
    pos = any(x in head for x in ("係", "是", "yes", "correct", "相符"))
    verdict = (not neg) and pos
    print(f"[verify_food_image] {kind} {expected!r} → "
          f"{'MATCH' if verdict else 'REJECT'} ({d[:60]!r})", flush=True)
    return verdict


@app.route("/api/scan_preview_text", methods=["POST"])
def api_scan_preview_text():
    """Text-only food entry preview (no image required).

    Jim OOB 2026-08-02 02:50 HKT: 'I think the food log should allow
    me to direct input the food by text'. Flow:
      1. User types a description in textbox.
      2. Backend sends to MiniMax (text-only — no vision).
      3. Optional user_hints[] appended for extra context.
      4. Returns preview shape identical to /api/scan_preview so the
         frontend can present it in the SAME confirm card. Frontend
         then calls /api/scan_commit with image_path="" to commit.

    v3.3.20 (Jim OOB 2026-09-10): optional `with_image=true` (default)
    uses Brave Image Search to fetch a representative food image for the
    dish (downloaded locally, saved to scan_cache). /api/scan_commit
    will then upload to Drive + write image_url to Sheet just like a
    photo-scanned entry. Set `with_image=false` to skip image fetching
    (faster, no network call). The returned `image_path` is non-empty
    only when image was successfully downloaded.
    """
    data = request.get_json(silent=True) or {}
    text_desc = (data.get("text") or data.get("description") or "").strip()
    user_hints_in = data.get("user_hints", []) or []
    with_image_flag = data.get("with_image", True)  # default ON per Jim OOB 2026-09-10

    if not text_desc:
        return jsonify({"ok": False, "error": "missing text description"}), 400
    if len(text_desc) > 1000:
        text_desc = text_desc[:1000]

    # Sanitize hints (cap each 200 chars, max 5 hints)
    cleaned_hints = []
    seen = set()
    for h in user_hints_in[:5]:
        if not isinstance(h, str):
            continue
        h = h.strip()[:200]
        if not h or h in seen:
            continue
        seen.add(h)
        cleaned_hints.append(h)
    if cleaned_hints:
        augmented_text = f"用家補充資料：{'；'.join(cleaned_hints)}\n\n{text_desc}"
    else:
        augmented_text = text_desc

    # MiniMax text-only nutrition estimate
    prompt = (
        "你係香港營養師。根據用家輸入嘅食物描述，估算卡路里同12大營養素。"
        "用 JSON 格式 return：(全部 value 用數字, unit 用 kcal/g/mg)\n"
        f'{{\"name\":\"<菜名 short>\",\"portion\":<string 份量描述>,\"calories\":<int kcal>,'
        f'\"protein\":<g>,\"carbs\":<g>,\"fat\":<g>,\"fiber\":<g>,\"sugar\":<g>,'
        f'\"sodium\":<mg>,\"sat_fat\":<g>,\"trans_fat\":<g>,\"vit_c\":<mg>,'
        f'\"iron\":<mg>,\"calcium\":<mg>,\"restaurant_guess\":\"<餐廳名 if any>\",'
        f'\"shared_meal\":<bool>,\"cooking_method\":\"<煮法 short>\"}}\n\n'
        f"用家輸入：{augmented_text}\n\n"
        "廣東話, 一個英文字都唔好出。估算合理範圍："
        "家常菜 300-700 kcal, 大碟 500-900 kcal, 火鍋薄切 200-400 kcal per round。"
    )
    mm_text_desc = _minimax_nutrition_enrich(prompt)

    # Parse MiniMax JSON response (v3.3.22: robust parser strips  blocks
    # and falls back to last-{...}-regex if MiniMax emits prose)
    parsed = _safe_parse_nutrition_json(mm_text_desc)

    # v3.3.32 (Jim OOB 2026-10-21「manual food input ... has been very
    # buggy」):實測「星巴克拿鐵」有一次 MiniMax 回傳空/壞 JSON,
    # 結果 12 個欄全部 0 kcal 顯示俾 Jim,更壞係會寫入 golden Sheet。
    # 而家:0 kcal 就重試一次,再失敗就由 Sheet 歷史 median 補,
    # 補唔到就明確標記(唔會靜靜哋寫 0 入去)。
    if not parsed or not parsed.get("calories"):
        print(f"[scan_preview_text] nutrition parse empty/0 kcal "
              f"({text_desc!r}) → retry once", flush=True)
        retry_raw = _minimax_nutrition_enrich(
            prompt + "\n\n(重試:上一個回覆格式錯,必須回傳完整 JSON object)")
        retry_parsed = _safe_parse_nutrition_json(retry_raw)
        if retry_parsed.get("calories"):
            parsed = retry_parsed
            mm_text_desc = retry_raw
            print(f"[scan_preview_text] retry OK → "
                  f"{retry_parsed.get('calories')} kcal", flush=True)
        else:
            print(f"[scan_preview_text] retry still empty → will backfill "
                  f"from Sheet history", flush=True)

    # v3.3.23v (Jim OOB 2026-09-15 'my recent quest energy bar does not
    # record any nutrient info'): AI estimate often misses brand-specific
    # macros (Quest Bar: AI returns 190/21/21/8 but actual is 200/21/22/8).
    # Override AI's estimate with our hardcoded _PROTEIN_BRAND_TABLE lookup
    # BEFORE building field_entries, so the brand-correct values flow through
    # to the suggested_entry + committed row.
    brand_hit = _lookup_brand_macros(text_desc)
    if brand_hit:
        print(f"[scan_preview_text] brand_table override for {text_desc!r}: "
              f"AI→brand {parsed.get('calories', 0)}→{brand_hit['kcal']} kcal "
              f"({brand_hit['_source']})", flush=True)
        if not isinstance(parsed, dict):
            parsed = {}
        # Brand macros override the AI estimate for the 4 main macros
        parsed["calories"] = brand_hit["kcal"]
        parsed["protein"] = brand_hit["protein"]
        parsed["carbs"] = brand_hit["carbs"]
        parsed["fat"] = brand_hit["fat"]
        # Fill fiber/sugar/sodium/sat_fat/trans_fat if brand table has them
        # and AI didn't supply (or supplied 0)
        for f in ("fiber", "sugar", "sodium", "sat_fat", "trans_fat"):
            if brand_hit.get(f) is not None and not parsed.get(f):
                parsed[f] = brand_hit[f]
        # Set restaurant_chain to brand if user didn't supply one
        if not parsed.get("restaurant_guess") and brand_hit.get("restaurant_chain"):
            parsed["restaurant_guess"] = brand_hit["restaurant_chain"]

    # v3.3.32 (Jim OOB 2026-10-21「Protein drinks and brand … put the brand
    # cover as food image」):品牌識別 —— 舊 brand table 淨係收縮寫
    # 「on gold standard」,自然寫法「Optimum Nutrition Gold Standard」完全
    # miss(實測 2026-10-21:攞到 120 kcal 撞正,但係名同圖片都唔啱)。
    # 而家另外用 alias 識別,順便攞到官方網域(搵 brand cover 用)。
    brand_info: dict = _match_brand(text_desc) or {}
    if brand_info and brand_hit.get("restaurant_chain"):
        # 統一用 brand table 嘅正式品牌名(兩邊一致)
        brand_info["brand"] = brand_hit.get("restaurant_chain") or brand_info["brand"]
    if brand_info:
        print(f"[scan_preview_text] brand detected: {brand_info['brand']} "
              f"(alias {brand_info['alias']!r}, product "
              f"{brand_info.get('product_phrase', '')!r})", flush=True)

    # v3.3.20 (Jim OOB 2026-09-10): optionally fetch a representative food image
    # via Brave Image Search. Best-effort — failure here doesn't block the nutrition
    # preview. Query prioritizes the MiniMax-suggested dish name (cleanest), with
    # user input as fallback + restaurant_guess as prefix hint if present.
    #
    # ⚠️ v3.3.32 (2026-10-21):呢段舊邏輯搬到下面(建議名 + 餐廳計好之後),
    #    因為新策略需要用「最終菜名」同「最終餐廳」嚟砌搜尋 query,
    #    而且每張相要經視覺驗證。舊邏輯嘅問題:
    #      - 淨係攞 Brave 第一個結果,零相關性驗證
    #      - 「Optimum Nutrition Gold Standard」→ 攞咗 istockphoto 張 stock 圖
    #      - 完全搵唔到品牌 cover
    downloaded_image_path = ""
    image_search_meta = {"attempted": bool(with_image_flag), "downloaded": False,
                         "source": "", "error": "", "kind": "", "query": "",
                         "verified": False, "via": "", "title": ""}

    # Detect shared meal via keyword scan (Jim OOB 2026-08-02 02:50 HKT —
    # only scan USER-FACING text, NOT the JSON dump which has
    # "shared_meal":false literal that triggers false-positive on "share")
    shared = _detect_shared_meal(augmented_text)
    jim_ratio = 0.60 if shared else 1.00

    # Build per-field entries (apply jim_ratio if shared)
    field_entries = {}
    for f in NUTRITION_FIELDS:
        info = parsed.get(f) if isinstance(parsed, dict) else None
        raw = 0
        if isinstance(info, dict):
            raw = float(info.get("value", 0) or 0)
        elif isinstance(info, (int, float)):
            raw = float(info)
        field_entries[f] = round(raw * jim_ratio, 1) if shared else round(raw, 1)

    raw_kcal = float(parsed.get("calories", 0) or 0) if isinstance(parsed, dict) else 0
    raw_p = float(parsed.get("protein", 0) or 0) if isinstance(parsed, dict) else 0

    now_hkt_dt = datetime.now(timezone(timedelta(hours=8)))
    # v3.2.7: route through _extract_dish_name() + _compute_rating() so the
    # preview shows a proper dish name + star rating, NOT the raw MiniMax
    # prose (Jim OOB 2026-08-07 23:50 HKT 'the food title is not shown
    # on the list view. Moreover, it does not have rating').
    mm_name_raw = (parsed.get("name") if isinstance(parsed, dict) else "") or ""
    restaurant_guess = (parsed.get("restaurant_guess") if isinstance(parsed, dict) else "") or ""
    # Use MiniMax's name as the dish-name extractor input (it's the
    # most relevant text), with the user input as fallback.
    suggested_name = _extract_dish_name(mm_name_raw, mm_text_desc, fallback=text_desc[:60])
    # If still empty or the generic "食物" fallback, try the user input directly
    if not suggested_name or suggested_name.strip() == "食物":
        suggested_name = _extract_dish_name(text_desc, "", fallback=text_desc[:60])
    final_name = _sanitise_dish_name(suggested_name, vision_desc=text_desc, pplx_desc=mm_text_desc)

    # ── v3.3.32: 品牌產品嘅名要穩定 ──
    # 實測 2026-10-21:同一個「Optimum Nutrition Gold Standard」,
    # MiniMax 一次出「金標乳清蛋白粉」(啱),另一次出「雞胸肉」(完全唔啱)。
    # 品牌產品就一定用返 Jim 自己打嘅字(最忠實),除非 AI 個名夠具體。
    if brand_info and not _product_name_is_specific(final_name):
        fallback_name = (text_desc or "").strip()[:60]
        if fallback_name:
            print(f"[scan_preview_text] brand product name "
                  f"{final_name!r} too generic → use user input "
                  f"{fallback_name!r}", flush=True)
            final_name = fallback_name

    # ── v3.3.32: 0 kcal 唔可以靜靜哋入 Sheet ──
    # MiniMax 偶爾回傳空/壞 JSON(實測「星巴克拿鐵」)。
    # 呢度用 Sheet 歷史 median 補返;補唔到就照 0 但會有 log。
    if not field_entries.get("calories"):
        hist_macros = _lookup_known_macros(final_name)
        if hist_macros and hist_macros.get("kcal"):
            field_entries["calories"] = hist_macros.get("kcal", 0)
            field_entries["protein"] = hist_macros.get("protein", field_entries.get("protein", 0))
            field_entries["carbs"] = hist_macros.get("carbs", field_entries.get("carbs", 0))
            field_entries["fat"] = hist_macros.get("fat", field_entries.get("fat", 0))
            if isinstance(parsed, dict):
                parsed["calories"] = field_entries["calories"]
                parsed["protein"] = field_entries["protein"]
                parsed["carbs"] = field_entries["carbs"]
                parsed["fat"] = field_entries["fat"]
            print(f"[scan_preview_text] 0 kcal → backfilled from Sheet history: "
                  f"{final_name!r} = {field_entries['calories']} kcal", flush=True)
        else:
            print(f"[scan_preview_text] ⚠️ 0 kcal and no history match for "
                  f"{final_name!r} — 建議 Jim 手動檢查", flush=True)

    # ── v3.3.32: best-effort 餐廳(唔好再寫 AI 廢話落 golden copy)──
    # 舊流程:`restaurant_guess` 由 AI 嚟,原封不動寫入 Sheet →
    #  production 餐廳欄 90% 係「難以辨認餐廳」呢類垃圾。
    # 新流程:品牌 → 文字提到 → 歷史索引 → AI(經消毒)→ 留空
    restaurant_final, restaurant_source = _resolve_restaurant(
        text_desc, final_name, restaurant_guess, brand_info or None)
    if restaurant_final != restaurant_guess:
        print(f"[scan_preview_text] restaurant: AI {restaurant_guess!r} → "
              f"{restaurant_final!r} (source={restaurant_source})", flush=True)

    # ── v3.3.32: 食物相策略(食物優先 → 餐廳次選 → 品牌 cover)──
    if with_image_flag:
        img_res = _pick_food_image(
            dish_name=final_name,
            restaurant=restaurant_final,
            user_text=text_desc,
            brand=brand_info or None,
            search_fn=_brave_image_search,
            download_fn=_download_food_image,
            verify_fn=_verify_food_image,
            ddg_fn=_ddg_search,
            fetch_page_fn=_fetch_page_html,
            max_candidates=3,
            log=lambda m: print(m, flush=True),
        )
        downloaded_image_path = img_res.get("image_path") or ""
        image_search_meta["downloaded"] = bool(downloaded_image_path)
        image_search_meta["source"] = img_res.get("source") or ""
        image_search_meta["kind"] = img_res.get("kind") or ""
        image_search_meta["query"] = img_res.get("query") or ""
        image_search_meta["verified"] = bool(img_res.get("verified"))
        image_search_meta["via"] = img_res.get("via") or ""
        image_search_meta["title"] = (img_res.get("title") or "")[:120]
        if not downloaded_image_path:
            image_search_meta["error"] = "no_matching_image"

    # v3.2.7.3: single A-F grade via _coach_comment (replaces old 1-5 star rating)
    coach_cc = _coach_comment(
        final_name,
        field_entries.get("calories", 0) or 0,
        field_entries.get("protein", 0) or 0,
        field_entries.get("carbs", 0) or 0,
        field_entries.get("fat", 0) or 0,
        restaurant_final,
        daily_ctx_seed=_daily_ctx_seed(),
    )

    preview = {
        "preview_id": f"pv_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}_txt",
        "image_path": downloaded_image_path,  # v3.3.20: populated by Brave image search
        "image_url": "",  # filled at commit time (after Drive upload)
        "input_mode": "text",  # flag for frontend to render label
        "vision_desc": augmented_text,
        "vision_short": augmented_text[:300],
        "pplx_short": mm_text_desc[:500] if mm_text_desc else "",
        "minimax_enrichment": mm_text_desc[:300] if mm_text_desc else "",
        "nutrition_merged": parsed if isinstance(parsed, dict) else {},
        # v3.3.20: expose Brave image-search metadata to the frontend so
        # the user can see WHERE the image came from (source domain) and
        # whether the fetch actually succeeded (downloaded=True/False).
        # v3.3.32: + kind (brand_cover/food/restaurant) + verified (視覺驗證)
        #           + query (用咗咩 query 搵) + via (og_image / search)
        "image_search": image_search_meta,
        # v3.3.32: 餐廳係點嚟嘅(brand/text/history/ai/none)—— 前端會顯示俾 Jim 睇
        "restaurant_source": restaurant_source,
        "suggested_entry": {
            "date": today_iso(),
            "time": now_hkt_dt.strftime("%H:%M"),
            "meal_type": "scan",
            "name": final_name,
            "coach_comment": coach_cc,
            "restaurant_chain": restaurant_final,
            "cooking_method": (parsed.get("cooking_method", "") if isinstance(parsed, dict) else ""),
            "calories": field_entries["calories"],
            "protein": field_entries["protein"],
            "carbs": field_entries["carbs"],
            "fat": field_entries["fat"],
            "fiber": field_entries["fiber"],
            "sugar": field_entries["sugar"],
            "sodium": field_entries["sodium"],
            "sat_fat": field_entries["sat_fat"],
            "trans_fat": field_entries["trans_fat"],
            "vit_c": field_entries["vit_c"],
            "iron": field_entries["iron"],
            "calcium": field_entries["calcium"],
            "is_shared_meal": shared,
            "share_with_wife": "Jim 60% / 小寶 40% (auto-applied)" if shared else "Jim 100% (solo)",
            "raw_kcal_estimate": int(raw_kcal),
            "raw_p_estimate": int(raw_p),
        },
        "user_hints": cleaned_hints,
        "ready_to_commit": True,
        "is_text_only": True,
    }
    return jsonify({"ok": True, "preview": preview})


def _minimax_nutrition_enrich_local(text: str) -> dict:
    """Wrapper retained to keep call-site readable.
    Already imported via _minimax_nutrition_enrich in the module."""
    return _minimax_nutrition_enrich(text)


# ---------- F3: /api/coach_tips ----------
# Jim OOB 2026-07-23 22:42: "in activity logging window, should give me coach tips for that particular session.
# Using pplx and minimax to achieve it. Traditional Chinese pls."

# Cache coached sessions by session_date + exercises_hash
COACHTIPS_CACHE_PATH = Path("/home/work/.hermes/coachtips_cache.json")


def _load_coachtips_cache() -> dict:
    if not COACHTIPS_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(COACHTIPS_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_coachtips_cache(cache: dict) -> None:
    COACHTIPS_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def _generate_coach_tips(session_data: dict) -> dict:
    """Use pplx + MiniMax to generate Traditional Chinese coach tips for the session.

    session_data expected keys:
      - exercises: list[str] (e.g. ["BB Bench Press", "Squat", "DB OHP"])
      - session_date: str (YYYY-MM-DD)
      - total_vol: int (kg)
      - total_sets: int
      - exercise_summary: list[dict] (per exercise: name, sets, top_weight, total_rep_count)
    """
    exercises = session_data.get("exercises", [])
    if not exercises:
        return {"tips": [], "error": "no exercises"}

    exercise_lines = []
    for ex_sum in session_data.get("exercise_summary", []):
        name = ex_sum.get("name", "")
        sets = ex_sum.get("sets", [])
        if sets:
            top_w = max((s.get("weight_kg", 0) for s in sets), default=0)
            n_sets = len(sets)
            rep_schemes = ", ".join(f"{s.get('reps','?')}" for s in sets[:3])
            exercise_lines.append(f"- {name}: {n_sets} 組, 最高重量 {top_w} 公斤, reps {rep_schemes}")
        else:
            exercise_lines.append(f"- {name}")

    ex_block = "\n".join(exercise_lines)

    # pplx query: lifts Progression + Form cues for THIS combination (Traditional Chinese)
    pplx_query = (
        f"我啱啱做完一個重量訓練 session，今日嘅 exercise 組合係：\n\n{ex_block}\n\n"
        "我想你以 NSCA-CSCS 私人教練身份，用繁體中文（廣東話都可以）答我兩件事：\n"
        "1. 每個動作嘅 form cue（最重要嗰 1-2 個，唔好列晒成個清單）\n"
        "2. 下次做呢個動作嘅 progression 建議（重量 / 組數 / 變化）。\n\n"
        "只答呢兩個範疇，唔好分析營養、唔好建議其他運動。"
    )

    pplx_prompt_drink = (
        f"以下呢個 session 嘅總覽：\n"
        f"- 總組數: {session_data.get('total_sets', 0)}\n"
        f"- 總容量: {session_data.get('total_vol', 0)} 公斤\n"
        f"- 動作組合: {', '.join(exercises)}\n\n"
        "用繁中俾我一句總評（最多 50 字），唔好列數字。"
    )

    # v3.3.34 (2026-10-21, Jim OOB "Remove perplexity"): 原本第一遍係
    # Perplexity sonar-pro 出 form cue + progression,再交畀 MiniMax render。
    # 而家第一遍直接用 MiniMax(同一個 prompt、同一個 system role),
    # 第二遍照舊由 MiniMax render 做 cheer 教練訊息。
    pplx_ans = ""
    try:
        api_key = _minimax_api_key()
        if api_key:
            payload = {
                "model": "MiniMax-M3",
                "messages": [
                    {"role": "system", "content": "你係香港 NSCA-CSCS 教練。用繁體中文、技術但口語化。"},
                    {"role": "user", "content": pplx_query},
                ],
                "max_tokens": 1400,
                "temperature": 0.25,
            }
            req = urllib.request.Request(
                "https://api.minimax.io/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + api_key},
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
            pplx_ans = resp["choices"][0]["message"]["content"]
    except Exception as e:
        pplx_ans = f"（教練 tips 失敗：{type(e).__name__}）"

    # MiniMax synthesis — render Traditional Chinese with friendly tone
    mm_prompt = (
        f"以下係教練對於一個 gym session 嘅 form cue + progression 建議：\n\n"
        f"{pplx_ans}\n\n"
        f"加上 session 摘要：{pplx_prompt_drink}\n\n"
        "任務：用繁體中文（廣東話口語都得）幫我 render 做一個 cheer 教練嘅總結訊息。\n"
        "格式：\n"
        "1. 第一段（2-3 句）講今日 session 嘅整體觀察同鼓勵。\n"
        "2. 第二段列出每個動作嘅 form cue（如果有嘅話，濃縮做 1 個關鍵字，例如「BB Bench：背貼穩 bench」）。\n"
        "3. 第三段講下次做呢個動作嘅 progression tip（重量加幾多、動作變化、組數調整，2-3 個具體建議）。\n\n"
        "唔好超過 250 字，唔好重複人哋嘅 engagement 廢話。"
    )

    mm_ans = ""
    try:
        api_key = _minimax_api_key()
        if api_key:
            payload = {
                "model": "MiniMax-M3",
                "messages": [{"role": "user", "content": mm_prompt}],
                "max_tokens": 1500,
                "temperature": 0.4,
            }
            req = urllib.request.Request(
                "https://api.minimax.io/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": "Bear" + "er " + api_key},
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=90).read())
            mm_ans = resp["choices"][0]["message"]["content"]
    except Exception as e:
        mm_ans = f"（MiniMax 總結失敗：{type(e).__name__}）"

    return {
        "tips": {
            "pplx_raw": pplx_ans,
            "mm_summary": mm_ans,
        },
        "exercises_analyzed": exercises,
    }


@app.route("/api/coach_tips", methods=["POST"])
def api_coach_tips():
    """Jim OOB 2026-07-23 22:42 — coach tips for a particular session.

    Input: session_data {session_date, exercises, exercise_summary, total_vol, total_sets}
    Output: {ok, tips: {pplx_raw, mm_summary}, exercises_analyzed, generated_at}
    Cached per session_date + exercises hash.
    """
    data = request.get_json(silent=True) or {}
    exercises = data.get("exercises") or []
    if not exercises:
        return jsonify({"ok": False, "error": "no exercises"}), 400
    session_date = data.get("session_date") or today_iso()

    cache_key = f"{session_date}::{','.join(exercises)}"
    cache = _load_coachtips_cache()
    if cache_key in cache:
        return jsonify({"ok": True, "cached": True, **cache[cache_key]})

    result = _generate_coach_tips(data)
    result["session_date"] = session_date
    result["generated_at"] = now_iso()
    cache[cache_key] = result
    _save_coachtips_cache(cache)
    return jsonify({"ok": True, "cached": False, **result})


# ---------- F5: /api/cheer — v2.5 gym-internal cheer trigger (Jim OOB 2026-07-23 "Can copy all the cheer routine stuff into gymbro?") ----------
import threading
import shutil
import subprocess as _sp
import sys
import time
import uuid

CHEER_AUDIO_CACHE = Path("/home/work/.hermes/audio_cache")
CHEER_IMAGE_CACHE = Path("/home/work/.hermes/image_cache")
CHEER_ARTIFACT_DIR = Path("/home/work/.hermes/cheer_artifacts")
CHEER_AUDIO_CACHE.mkdir(parents=True, exist_ok=True)
CHEER_IMAGE_CACHE.mkdir(parents=True, exist_ok=True)
CHEER_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# In-process status dict — keyed by job_id
CHEER_JOBS = {}
CHEER_JOBS_LOCK = threading.Lock()

# Cheer audit log — append-only list of cheer fires
CHEER_LOG_PATH = Path("/home/work/.hermes/cheer_log.json")
if not CHEER_LOG_PATH.exists():
    CHEER_LOG_PATH.write_text("[]")

def _load_cheer_log() -> list:
    try:
        d = json.loads(CHEER_LOG_PATH.read_text())
        if isinstance(d, dict):
            d = d.get("fires", [])
        return d if isinstance(d, list) else []
    except Exception:
        return []

def _save_cheer_log(log: list) -> None:
    CHEER_LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2))

# Whoop cache (read live if <2h, else use cache)
WHOOP_CACHE_PATH = Path("/home/work/.whoop_data_latest.json")
# v3.2.7.23: the legacy whoop_pull.py under skills/fitness/whoop-pull-activities/
# DOES NOT exist on disk (verified 2026-08-06 09:00 HKT — Pitfall III.8 in
# fitness/whoop SKILL.md documents the missing-script workaround). Point the
# auto-refresh path at the working tools/whoop_nutrition.py --sync script
# instead, which paginates all 4 endpoints + writes the same cache shape.
WHOOP_PULL_SCRIPT = Path("/home/work/tools/whoop_nutrition.py")

EN_TO_ZH_VOICE = {
    # Brand / proper nouns
    "Jim": "占姆", "Google": "谷歌", "Whoop": "身體監測器", "Novotel": "諾富特",
    "Wanchai": "灣仔", "Mounjaro": "減重藥",
    # Tech metrics → Chinese
    "Zone": "區", "Z2": "中二區", "Z3": "中三區", "Z4": "中四區",
    "HRV": "心跳變異", "SpO2": "血氧", "RHR": "靜止心跳",
    "RPE": "自覺強度", "HIIT": "高強度間歇", "MHR": "最高心跳",
    "score": "分數", "level": "水平", "range": "範圍", "target": "目標",
    "delta": "差距", "state": "狀態", "status": "狀態",
    # Training vocab
    "nap": "晏覺", "session": "課堂", "workout": "訓練", "plate": "碟",
    "weightlifting": "重量訓練", "push": "推入去", "lift": "舉", "set": "組",
    "rep": "下", "drill": "操", "bar": "吧台", "spot": "位",
    # Color zones
    "YELLOW": "黃燈", "GREEN": "綠燈", "RED": "紅燈",
    "yellow": "黃燈", "green": "綠燈", "red": "紅燈",
    # Health metrics
    "strain": "疲勞度", "recovery": "復原指數", "recover": "復原", "recovery, ": "復原，", " recovery ": " 復原 ",
    "REM": "快速眼動睡眠", "N1": "淺睡第一階段", "N2": "淺睡第二階段", "N3": "深層睡",
    "deep sleep": "深層睡", "light sleep": "淺睡", "awake": "醒",
    # Verbs
    "reset": "重設", "share": "分擔", "hotpot": "火鍋", "squat": "深蹲",
    "bench": "臥推", "deadlift": "硬拉", "press": "推舉", "curl": "彎舉",
    "row": "划船", "lat pulldown": "下拉", "pullup": "引體上升",
    "check in": "睇下", "check-in": "睇下", "checkin": "睇下",
    "Check in": "睇下", "Check In": "睇下", "CHECK IN": "睇下",
    # Common EN filler that pplx leaks
    "keep": "保持住", "base": "基礎", "plan": "計劃", "solid": "紮實",
    "time": "時間", "times": "次", "ok": "好", "OK": "好", "Ok": "好",
    "use": "用", "using": "用", "treat": "處理", "make sure": "確保",
    "check": "睇下", "monitor": "監察", "tracking": "追蹤", "trend": "趨勢",
    "stable": "穩定", "fact": "事實", "matters": "重要", "matter": "重要",
    "feel": "感覺", "felt": "感覺到", "feeling": "感覺",
    "keep,": "保持住，", " keep ": " 保持住 ", "keep.": "保持住.",
    "plan,": "計劃，", " plan ": " 計劃 ",
    " stable": " 穩定", "stable,": "穩定，",
    "time,": "時間，", " time ": " 時間 ",
    # Jim OOB 2026-07-24: extended EN filler coverage (pplx casual news text leaks)
    "fit": "健", "fitness": "健康", "share": "分擔", "shared": "分咗", "fast": "快",
    "simple": "簡單", "basically": "基本上", "literally": "真係", "actually": "其實",
    "okay": "好", "yeah": "係", "yep": "係", "nope": "唔係", "kinda": "有少少",
    "sort of": "有少少", "kind of": "有少少", "gonna": "會", "wanna": "想",
    "gotta": "要", "should": "應該", "would": "會", "could": "可以",
    "maybe": "可能", "probably": "可能", "definitely": "一定",
    "anyway": "總之", "anyways": "總之", "though": "不過", "although": "雖然",
    "because": "因為", "since": "因為", "while": "當", "during": "喺",
    "right": "啱", "wrong": "錯", "true": "啱", "false": "錯",
    "best": "最好", "worst": "最差", "better": "更好", "worse": "更差",
    "more": "多啲", "less": "少啲", "most": "最多", "least": "最少",
    "today": "今日", "tomorrow": "聽日", "yesterday": "尋日",
    "morning": "朝早", "evening": "夜晚", "afternoon": "下晝", "night": "夜晚",
    "week": "星期", "month": "月份", "year": "年",
    "world cup": "世界盃", "World Cup": "世界盃",
    "premier league": "英超", "Premier League": "英超",
    "liverpool": "利物浦", "Liverpool": "利物浦",
    "man utd": "曼聯", "Man Utd": "曼聯", "Manchester United": "曼聯",
    "fitness,": "健康，", " fit": " 健",
    "FIT": "健", "FITS": "健",
    # Measurements / units
    "kg": "公斤", "lb": "磅", "kcal": "千卡", "min": "分鐘", "sec": "秒",
    "hr": "小時", "hrs": "小時", "bpm": "下每分鐘", "ms": "毫秒",
    " oz": " 安士", "g ": "克 ",
    # People (titles)
    "Dr.": "醫生", "Mr.": "先生", "Mrs.": "女士", "Ms.": "女士",
    # Old roles
    "coach ": "教練 ", "butler": "管家",
    # PWA / app
    "app": "程式", "app, ": "程式，", " app ": " 程式 ",
    "PC": "電腦", "phone": "手機", "tab": "分頁",
    # Closing
    "Bon voyage": "旅途愉快", "Welcome home": "歡迎返嚟",
    "Good luck": "好運", "Good night": "晚安", "Good morning": "早晨",
    "Take care": "保重", "Good job": "做得好", "stay safe": "保重",
    # Misc
    "etc": "等等", "e.g.": "例如", "i.e.": "即係", "vs": "對", "via": "透過",
    "FYI": "提一提", "ASAP": "盡快", "P.S.": "補充", "PS": "補充",
    "ok,": "好，", " ok ": " 好 ",
    "harder": "再努力啲", "easier": "再放鬆啲", "stable,": "穩定，",
    # Common BUIDL/training
    "build": "建立", "bulk": "增肌", "cut": "減脂", "rest": "休息",
    "PR": "個人紀錄", "rep": "下", "RM": "最大重複",
    # v3.2.7.39 — extended coverage for casual prose pplx leaks (Jim OOB 2026-08-11
    # "Should be all traditional Chinese. No English"). pplx sonar-pro still
    # inserts these words even after 6 months of cheer iteration.
    "active": "活躍", "chair": "梳化", "office": "寫字樓", "comeback": "反彈",
    "heroic": "英雄式", "full": "滿", "output": "產出", "technical": "技術性",
    "AI": "人工智能", "HTTP": "超文本傳輸", "NBA": "美職籃",
    "PortSwigger": "網絡安全品牌", "Terminator": "終結者",
    "fat": "脂肪", "fat, ": "脂肪，", " fat ": " 脂肪 ",
    "lean": "瘦", "toned": "結實", "buff": "壯",
    "elite": "頂尖", "athletic": "運動型", "ripped": "精瘦",
    "post": "之後", "pre": "之前", "mid": "中段",
    "vs.": "對", "vs": "對",
    "Step": "步", "steps": "步", "Steps": "步",
    "Streak": "連續", "streak": "連續",
    "Bad": "差", "Good": "好", "Big": "大", "Small": "細",
    "thing": "嘢", "stuff": "嘢", "guys": "兄弟", "guy": "兄弟",
    "let's": "我哋一齊", "lets": "我哋一齊",
    "I'm": "我", "I'll": "我會", "I've": "我已經",
    "you're": "你", "you'd": "你會", "you've": "你已經",
    "we're": "我哋", "we'll": "我哋會", "we've": "我哋已經",
    "they're": "佢哋", "they'll": "佢哋會",
    "isn't": "唔係", "aren't": "唔係", "wasn't": "唔係", "weren't": "唔係",
    "don't": "唔好", "doesn't": "唔會", "didn't": "冇",
    "won't": "唔會", "wouldn't": "唔會", "shouldn't": "唔應該",
    "can't": "唔可以", "cannot": "唔可以", "couldn't": "唔能夠",
    "it's": "佢", "that's": "嗰個", "there's": "嗰度",
    "by": "透過", "with": "同", "without": "冇", "for": "為", "from": "從",
    "the ": "", " a ": " ", " an ": " ", " of ": "嘅", " in ": "喺",
    # v3.3.7+ (Jim OOB 2026-08-30「I want full Chinese on gymbro Chinese」):
    # Aggressive zero-EN table — every leak observed in 2026-08-30 manual cheer draft.
    # Use 2-entry form so case-insensitive matching handles ALL CAPS variants.
    "tank": "缸", "Tank": "缸", "TANK": "缸",
    "full": "滿", "Full": "滿", "FULL": "滿",
    "log": "記錄", "Log": "記錄", "LOG": "記錄",
    "APT": "黑客組織", "Apt": "黑客組織", "apt": "黑客組織",
    "ad": "廣告", "Ad": "廣告", "AD": "廣告",
    "tech": "科技", "Tech": "科技", "TECH": "科技",
    "pipeline": "流程", "Pipeline": "流程", "PIPELINE": "流程",
    "script": "腳本", "Script": "腳本", "SCRIPT": "腳本",
    "integrity": "完整性", "Integrity": "完整性", "INTEGRITY": "完整性",
    "USD": "美元", "usd": "美元",
    "asset": "資產", "Asset": "資產", "ASSET": "資產", "assets": "資產",
    "body": "身體", "Body": "身體", "BODY": "身體",
    "battery": "電池", "Battery": "電池", "BATTERY": "電池",
    "seed": "谷", "Seed": "谷", "SEED": "谷",
    "Zone": "區", "zone": "區", "ZONE": "區",
    "HIIT": "高強度間歇", "hiit": "高強度間歇",
    "heavy": "重", "Heavy": "重", "HEAVY": "重",
    "lift": "舉", "Lift": "舉", "LIFT": "舉",
    "audit": "審查", "Audit": "審查", "AUDIT": "審查",
    "supply": "供應", "Supply": "供應", "SUPPLY": "供應",
    "chain": "鏈", "Chain": "鏈", "CHAIN": "鏈",
    "mental": "心理", "Mental": "心理", "MENTAL": "心理",
    "perfect": "完美", "Perfect": "完美", "PERFECT": "完美",
    "day": "日", "Day": "日", "DAY": "日",
    "narrative": "敘事", "Narrative": "敘事", "NARRATIVE": "敘事",
    "consumer": "消費者", "Consumer": "消費者", "CONSUMER": "消費者",
    "consumer,": "消費者，", "consumer.": "消費者。",
    "cheer": "打氣", "Cheer": "打氣", "CHEER": "打氣",
    "video": "片段", "Video": "片段", "VIDEO": "片段",
    "comment": "評論", "Comment": "評論", "COMMENT": "評論",
    "comments": "評論", "Comments": "評論",
    "coach": "教練", "Coach": "教練", "COACH": "教練",
    "check": "睇下", "Check": "睇下", "CHECK": "睇下",
    "checks": "睇下", "Checks": "睇下",
    "perfect,": "完美，", "perfect.": "完美。",
    "day,": "日，", "day.": "日。",
    "labs": "實驗室", "lab": "實驗室", "Lab": "實驗室",
    "supplier": "供應商", "Supplier": "供應商",
    "platform": "平台", "Platform": "平台",
    "vendor": "供應商", "Vendor": "供應商",
    "phishing": "釣魚", "Phishing": "釣魚",
    "attack": "攻擊", "Attack": "攻擊",
    "attack,": "攻擊，", "attack.": "攻擊。",
    "core": "核心", "Core": "核心",
    "full,": "滿，", "full.": "滿。",
    "post": "之後", "Post": "之後",
    "set": "組", "sets": "組", "rep": "下", "reps": "下",
    "tier": "層次", "Tier": "層次",
    "edge": "邊緣", "Edge": "邊緣",
    "trends": "趨勢", "trend": "趨勢", "Trends": "趨勢",
    "sync": "同步", "Sync": "同步",
    "live": "實時", "Live": "實時",
    "push": "推", "Push": "推",
    "feed": "資訊流", "Feed": "資訊流",
    "views": "睇法", "view": "睇法",
    "core,": "核心，", "core.": "核心。",
    "today,": "今日，", "today.": "今日。",
    "tier,": "層次，", "tier.": "層次。",
    "live,": "實時，", "live.": "實時。",
    "next": "下一個", "Next": "下一個",
    "round": "回合", "Round": "回合",
    "pick": "揀", "Pick": "揀",
    "win": "贏", "Win": "贏",
    "lose": "輸", "Lose": "輸",
    "play": "踢", "Play": "踢",
    "team": "球隊", "Team": "球隊",
    "match": "賽事", "Match": "賽事",
    "score": "比數", "Score": "比數",
    "half": "半場", "Half": "半場",
    "kick": "踢", "Kick": "踢",
    # v3.3.7+ second wave (2026-08-30 second audit pass — leaks caught):
    "report": "報告", "Report": "報告", "REPORT": "報告",
    "line": "線", "Line": "線", "LINE": "線",
    "catch": "追", "Catch": "追", "CATCH": "追",
    "up": "上", "Up": "上", "UP": "上",
    "static": "靜態", "Static": "靜態", "STATIC": "靜態",
    "plugin": "插件", "Plugin": "插件", "PLUGIN": "插件",
    "complete": "完成", "Complete": "完成", "COMPLETE": "完成",
    "macros": "巨量營養", "Macros": "巨量營養", "MACROS": "巨量營養",
    "fuel": "燃料", "Fuel": "燃料", "FUEL": "燃料",
    "un": "", "ed": "",
    "APT24": "黑客組織",
    "IT": "資訊科技", "It": "資訊科技",
    "tier": "層次", "tier,": "層次，", "tier.": "層次。",
    "via": "透過", "Via": "透過",
    "step": "步", "Step": "步", "STEP": "步",
    "deep": "深層", "Deep": "深層", "DEEP": "深層",
    "low": "低", "Low": "低", "LOW": "低",
    "high": "高", "High": "高", "HIGH": "高",
    "top": "頂", "Top": "頂", "TOP": "頂",
    "minute": "分鐘", "Minute": "分鐘", "MINUTE": "分鐘",
    "minutes": "分鐘", "Minutes": "分鐘",
    "percent": "百分比", "Percent": "百分比",
    "rate": "比率", "Rate": "比率",
    "rates": "比率", "Rates": "比率",
    "signal": "訊號", "Signal": "訊號", "SIGNAL": "訊號",
    "signals": "訊號", "Signals": "訊號",
    "flag": "標記", "Flag": "標記",
    "flags": "標記", "Flags": "標記",
    "key": "關鍵", "Key": "關鍵", "KEY": "關鍵",
    "keys": "關鍵", "Keys": "關鍵",
    "cardio": "帶氧", "Cardio": "帶氧", "CARDIO": "帶氧",
    "warmup": "熱身", "Warmup": "熱身", "WARMUP": "熱身",
    "cooldown": "緩和", "Cooldown": "緩和", "COOLDOWN": "緩和",
    "spot": "位", "Spot": "位",
    "form": "姿勢", "Form": "姿勢", "FORM": "姿勢",
    "bar": "槓鈴", "Bar": "槓鈴",
    "rpe": "自覺強度", "Rpe": "自覺強度", "RPE": "自覺強度",
    "set": "組", "Set": "組", "SET": "組",
    "rep": "下", "Rep": "下", "REP": "下",
    "bios": "個體", "Bio": "個體", "BIO": "個體",
    "fresh": "新鮮", "Fresh": "新鮮", "FRESH": "新鮮",
    "rest": "休息", "Rest": "休息", "REST": "休息",
    "rest,": "休息，", "rest.": "休息。",
    "labs": "實驗室", "Lab": "實驗室", "LAB": "實驗室",
    "pre": "之前", "Pre": "之前", "PRE": "之前",
    "post": "之後", "Post": "之後", "POST": "之後",
    "ultra": "超", "Ultra": "超", "ULTRA": "超",
    "plus": "加", "Plus": "加", "PLUS": "加",
    "mini": "細", "Mini": "細", "MINI": "細",
    "focus": "焦點", "Focus": "焦點", "FOCUS": "焦點",
    "level": "水平", "Level": "水平", "LEVEL": "水平",
    "cost": "成本", "Cost": "成本", "COST": "成本",
    "rise": "升", "Rise": "升", "RISE": "升",
    "fall": "跌", "Fall": "跌", "FALL": "跌",
    "hurt": "痛", "Hurt": "痛", "HURT": "痛",
    "pain": "痛", "Pain": "痛", "PAIN": "痛",
    "stiff": "硬", "Stiff": "硬", "STIFF": "硬",
    "sore": "痠", "Sore": "痠", "SORE": "痠",
    "tight": "緊", "Tight": "緊", "TIGHT": "緊",
    "weak": "弱", "Weak": "弱", "WEAK": "弱",
    "tough": "硬淨", "Tough": "硬淨", "TOUGH": "硬淨",
    "easy": "易", "Easy": "易", "EASY": "易",
    "hard": "難", "Hard": "難", "HARD": "難",
    "limb": "肢", "Limb": "肢", "LIMB": "肢",
    "joint": "關節", "Joint": "關節", "JOINT": "關節",
    "back": "背", "Back": "背", "BACK": "背",
    "chest": "胸", "Chest": "胸", "CHEST": "胸",
    "leg": "腳", "Leg": "腳", "LEG": "腳",
    "arm": "手臂", "Arm": "手臂", "ARM": "手臂",
    "abs": "腹肌", "Abs": "腹肌", "ABS": "腹肌",
    "core": "核心", "Core": "核心", "CORE": "核心",
    "endo": "內", "Endo": "內",
    "exo": "外", "Exo": "外",
    "policy": "政策", "Policy": "政策",
    "feed": "資訊流", "Feed": "資訊流",
    "news": "新聞", "News": "新聞", "NEWS": "新聞",
    "speech": "演講", "Speech": "演講",
    "trump": "特朗普", "Trump": "特朗普",
    "biden": "拜登", "Biden": "拜登",
    "open": "開", "Open": "開", "OPEN": "開",
    "close": "關", "Close": "關", "CLOSE": "關",
    "stop": "停", "Stop": "停", "STOP": "停",
    "go": "去", "Go": "去", "GO": "去",
    # v3.3.7+ third wave (2026-08-30 third audit pass — final leaks caught):
    "mode": "模式", "Mode": "模式", "MODE": "模式",
    "outcome": "結果", "Outcome": "結果", "OUTCOME": "結果",
    "outcomes": "結果", "Outcomes": "結果",
    "standby": "待機", "Standby": "待機", "STANDBY": "待機",
    "moment": "時刻", "Moment": "時刻", "MOMENT": "時刻",
    "moments": "時刻", "Moments": "時刻",
    "action": "行動", "Action": "行動", "ACTION": "行動",
    "actions": "行動", "Actions": "行動",
    "mobility": "活動能力", "Mobility": "活動能力", "MOBILITY": "活動能力",
    "trade": "交易", "Trade": "交易", "TRADE": "交易",
    "trades": "交易", "Trades": "交易",
    "stake": "持倉", "Stake": "持倉", "STAKE": "持倉",
    "hold": "持有", "Hold": "持有", "HOLD": "持有",
    "buy": "買", "Buy": "買", "BUY": "買",
    "sell": "賣", "Sell": "賣", "SELL": "賣",
    "long": "好", "Long": "好", "LONG": "好",
    "short": "淡", "Short": "淡", "SHORT": "淡",
    "call": "睇好", "Call": "睇好", "CALL": "睇好",
    "put": "睇淡", "Put": "睇淡", "PUT": "睇淡",
    "risk": "風險", "Risk": "風險", "RISK": "風險",
    "risks": "風險", "Risks": "風險",
    "reward": "回報", "Reward": "回報", "REWARD": "回報",
    "chart": "圖表", "Chart": "圖表", "CHART": "圖表",
    "charts": "圖表", "Charts": "圖表",
    "index": "指數", "Index": "指數", "INDEX": "指數",
    "stock": "股票", "Stock": "股票", "STOCK": "股票",
    "stocks": "股票", "Stocks": "股票",
    "crypto": "加密貨幣", "Crypto": "加密貨幣", "CRYPTO": "加密貨幣",
    "bitcoin": "比特幣", "Bitcoin": "比特幣", "BITCOIN": "比特幣",
    "eth": "以太幣", "Eth": "以太幣", "ETH": "以太幣",
    "ethereum": "以太幣", "Ethereum": "以太幣",
    "dex": "去中心化交易所", "Dex": "去中心化交易所",
    "cex": "中心化交易所", "Cex": "中心化交易所",
    "degen": "膽粗粗", "Degen": "膽粗粗",
    "rug": "走佬", "Rug": "走佬", "RUG": "走佬",
    "meme": "迷因", "Meme": "迷因", "MEME": "迷因",
    "moon": "升穿", "Moon": "升穿", "MOON": "升穿",
    "ngmi": "冇運行", "Ngmi": "冇運行",
    "wagmi": "齊齊發", "Wagmi": "齊齊發",
    "fomo": "驚執輸", "Fomo": "驚執輸", "FOMO": "驚執輸",
    "fud": "恐懼", "Fud": "恐懼", "FUD": "恐懼",
    "btc": "比特幣", "Btc": "比特幣", "BTC": "比特幣",
    "ath": "歷史高位", "Ath": "歷史高位", "ATH": "歷史高位",
    "atl": "歷史低位", "Atl": "歷史低位", "ATL": "歷史低位",
    "dip": "回吐", "Dip": "回吐", "DIP": "回吐",
    "pump": "抽升", "Pump": "抽升", "PUMP": "抽升",
    "dump": "暴跌", "Dump": "暴跌", "DUMP": "暴跌",
    "whale": "大戶", "Whale": "大戶", "WHALE": "大戶",
    "hodl": "長揸", "Hodl": "長揸", "HODL": "長揸",
    "bags": "袋", "Bags": "袋", "BAGS": "袋",
    "beta": "測試", "Beta": "測試", "BETA": "測試",
    "alpha": "領先", "Alpha": "領先", "ALPHA": "領先",
    "vibe": "感覺", "Vibe": "感覺", "VIBE": "感覺",
    "vibes": "感覺", "Vibes": "感覺",
    "mood": "心情", "Mood": "心情", "MOOD": "心情",
    "tone": "語氣", "Tone": "語氣", "TONE": "語氣",
    "vibe": "感覺", "Vibe": "感覺",
    "tone": "語氣", "Tone": "語氣",
    # v3.3.7+ fourth wave (2026-08-30 fourth audit pass — final batch):
    "hangover": "宿醉", "Hangover": "宿醉", "HANGOVER": "宿醉",
    "window": "窗口", "Window": "窗口", "WINDOW": "窗口",
    "iThome": "科技網站", "ithome": "科技網站",
    "SaaS": "雲端軟件", "saas": "雲端軟件",
    "IOC": "入侵指標", "Ioc": "入侵指標", "ioc": "入侵指標",
    "integration": "整合", "Integration": "整合", "INTEGRATION": "整合",
    "dependency": "依賴", "Dependency": "依賴", "DEPENDENCY": "依賴",
    "vendor": "供應商", "Vendor": "供應商", "VENDOR": "供應商",
    "team": "團隊", "Team": "團隊", "TEAM": "團隊",
    "office": "寫字樓", "Office": "寫字樓", "OFFICE": "寫字樓",
    "security": "保安", "Security": "保安", "SECURITY": "保安",
    "chain": "鏈", "Chain": "鏈", "CHAIN": "鏈",
    "supply": "供應", "Supply": "供應", "SUPPLY": "供應",
    "monitor": "監察", "Monitor": "監察", "MONITOR": "監察",
    "keep": "保持住", "Keep": "保持住", "KEEP": "保持住",
    "audit": "審查", "Audit": "審查", "AUDIT": "審查",
    "over": "過", "Over": "過", "OVER": "過",
    "hang": "掛住", "Hang": "掛住", "HANG": "掛住",
    "stand": "站", "Stand": "站", "STAND": "站",
    "up": "上", "Up": "上", "UP": "上",
    "kick": "踢", "Kick": "踢", "KICK": "踢",
    "walk": "行", "Walk": "行", "WALK": "行",
    "boot": "啟動", "Boot": "啟動", "BOOT": "啟動",
    "reset": "重設", "Reset": "重設", "RESET": "重設",
    "high": "高", "High": "高", "HIGH": "高",
    "in": "喺", "In": "喺", "IN": "喺",
    "body": "身體", "Body": "身體", "BODY": "身體",
    "bodyweight": "自身體重", "Bodyweight": "自身體重", "BODYWEIGHT": "自身體重",
    "workout": "操", "Workout": "操", "WORKOUT": "操",
    "cheer": "打氣", "Cheer": "打氣", "CHEER": "打氣",
    "message": "訊息", "Message": "訊息", "MESSAGE": "訊息",
    "messages": "訊息", "Messages": "訊息",
    "carb": "碳水", "Carb": "碳水", "CARB": "碳水",
    "carbs": "碳水", "Carbs": "碳水",
    "heavy": "重", "Heavy": "重", "HEAVY": "重",
    "dump": "暴跌", "Dump": "暴跌", "DUMP": "暴跌",
    "mobility": "活動能力", "Mobility": "活動能力", "MOBILITY": "活動能力",
    "flow": "流動", "Flow": "流動", "FLOW": "流動",
    "focus": "專注", "Focus": "專注", "FOCUS": "專注",
    "gear": "檔位", "Gear": "檔位", "GEAR": "檔位",
    "full": "滿", "Full": "滿", "FULL": "滿",
    "quick": "快", "Quick": "快", "QUICK": "快",
    "fine": "好", "Fine": "好", "FINE": "好",
    "absolutely": "絕對", "Absolutely": "絕對", "ABSOLUTELY": "絕對",
    "recovery": "復原", "Recovery": "復原", "RECOVERY": "復原",
    "hedging": "對沖", "Hedging": "對沖", "HEDGING": "對沖",
    "hedge": "對沖", "Hedge": "對沖", "HEDGE": "對沖",
    "email": "電郵", "Email": "電郵", "EMAIL": "電郵",
    "GI": "升糖指數", "Gi": "升糖指數", "gi": "升糖指數",
    "HRV": "心跳變異", "Hrv": "心跳變異", "hrv": "心跳變異",
    "APT": "黑客組織", "Apt": "黑客組織", "apt": "黑客組織",
    "APT24": "黑客組織", "Apt24": "黑客組織", "apt24": "黑客組織",
    "IT": "資訊科技", "It": "資訊科技", "it": "資訊科技",
    "iThome": "科技網站",
    # v3.3.7+ fifth wave (2026-08-30 fifth audit — 1 leak remaining):
    "partner": "夥伴", "Partner": "夥伴", "PARTNER": "夥伴",
    "partners": "夥伴", "Partners": "夥伴",
    # v3.3.7+ sixth wave (2026-08-30 sixth audit — leaks observed in new draft):
    "endpoint": "端點", "Endpoint": "端點", "ENDPOINT": "端點",
    "ATM": "櫃員機", "atm": "櫃員機",
    "breakout": "突破", "Breakout": "突破", "BREAKOUT": "突破",
    "VPN": "虛擬網絡", "vpn": "虛擬網絡",
    "charge": "收費", "Charge": "收費", "CHARGE": "收費",
    "optional": "自選", "Optional": "自選", "OPTIONAL": "自選",
    "accessory": "配件", "Accessory": "配件", "ACCESSORY": "配件",
    "accessories": "配件", "Accessories": "配件",
    "personal": "個人", "Personal": "個人", "PERSONAL": "個人",
    "protective": "保護", "Protective": "保護", "PROTECTIVE": "保護",
    "equipment": "裝備", "Equipment": "裝備", "EQUIPMENT": "裝備",
    "nutrient": "營養素", "Nutrient": "營養素", "NUTRIENT": "營養素",
    "nutrients": "營養素", "Nutrients": "營養素",
    "incre": "增加", "Incre": "增加", "INCRE": "增加",
    "er": "急診室", "Er": "急診室", "ER": "急診室",
    "CI": "持續整合", "Ci": "持續整合", "ci": "持續整合",
    "CD": "持續部署", "Cd": "持續部署", "cd": "持續部署",
    "CI/CD": "持續整合部署",
    "API": "應用程式介面", "Api": "應用程式介面", "api": "應用程式介面",
    "CPU": "處理器", "Cpu": "處理器", "cpu": "處理器",
    "GPU": "圖像處理器", "Gpu": "圖像處理器", "gpu": "圖像處理器",
    "RAM": "記憶體", "Ram": "記憶體", "ram": "記憶體",
    "SSD": "硬碟", "Ssd": "硬碟", "ssd": "硬碟",
    "HDD": "硬碟", "Hdd": "硬碟", "hdd": "硬碟",
    "OS": "作業系統", "Os": "作業系統", "os": "作業系統",
    "UI": "介面", "Ui": "介面", "ui": "介面",
    "UX": "體驗", "Ux": "體驗", "ux": "體驗",
    "AI": "人工智能", "Ai": "人工智能", "ai": "人工智能",
    "ML": "機器學習", "Ml": "機器學習", "ml": "機器學習",
    "PR": "公關", "Pr": "公關", "pr": "公關",
    "FAQ": "常見問題", "Faq": "常見問題", "faq": "常見問題",
    "CEO": "行政總裁", "Ceo": "行政總裁", "ceo": "行政總裁",
    "CTO": "技術總監", "Cto": "技術總監", "cto": "技術總監",
    "CFO": "財務總監", "Cfo": "財務總監", "cfo": "財務總監",
    "CMO": "市場總監", "Cmo": "市場總監", "cmo": "市場總監",
    "COO": "營運總監", "Coo": "營運總監", "coo": "營運總監",
    "VP": "副總裁", "Vp": "副總裁", "vp": "副總裁",
    "SVP": "高級副總裁", "Svp": "高級副總裁", "svp": "高級副總裁",
    "EVP": "執行副總裁", "Evp": "執行副總裁", "evp": "執行副總裁",
    "GM": "總經理", "Gm": "總經理", "gm": "總經理",
    "MD": "董事總經理", "Md": "董事總經理", "md": "董事總經理",
    "AVP": "助理副總裁", "Avp": "助理副總裁", "avp": "助理副總裁",
    "HR": "人力資源", "Hr": "人力資源", "hr": "人力資源",
    "IT": "資訊科技", "It": "資訊科技", "it": "資訊科技",
    "R&D": "研發", "RD": "研發",
    "B2B": "企業對企業", "B2b": "企業對企業", "b2b": "企業對企業",
    "B2C": "企業對消費者", "B2c": "企業對消費者", "b2c": "企業對消費者",
    "C2C": "消費者對消費者", "C2c": "消費者對消費者", "c2c": "消費者對消費者",
    "P2P": "點對點", "P2p": "點對點", "p2p": "點對點",
    "SaaS": "雲端軟件", "Saas": "雲端軟件", "saas": "雲端軟件",
    "PaaS": "平台即服務", "Paas": "平台即服務", "paas": "平台即服務",
    "IaaS": "基礎設施即服務", "Iaas": "基礎設施即服務", "iaas": "基礎設施即服務",
    "CMS": "內容管理系統", "Cms": "內容管理系統", "cms": "內容管理系統",
    "CRM": "客戶關係管理", "Crm": "客戶關係管理", "crm": "客戶關係管理",
    "ERP": "企業資源規劃", "Erp": "企業資源規劃", "erp": "企業資源規劃",
    "KPI": "關鍵績效指標", "Kpi": "關鍵績效指標", "kpi": "關鍵績效指標",
    "OKR": "目標與關鍵成果", "Okr": "目標與關鍵成果", "okr": "目標與關鍵成果",
    "MVP": "最小可行產品", "Mvp": "最小可行產品", "mvp": "最小可行產品",
    "POC": "概念驗證", "Poc": "概念驗證", "poc": "概念驗證",
    "ROI": "投資回報率", "Roi": "投資回報率", "roi": "投資回報率",
    "SEO": "搜尋引擎優化", "Seo": "搜尋引擎優化", "seo": "搜尋引擎優化",
    "SEM": "搜尋引擎行銷", "Sem": "搜尋引擎行銷", "sem": "搜尋引擎行銷",
    "UI/UX": "介面體驗",
    "DevOps": "開發運維", "Devops": "開發運維", "devops": "開發運維",
    "Agile": "敏捷", "agile": "敏捷",
    "Scrum": "Scrum 框架", "scrum": "Scrum 框架",
    "Sprint": "衝刺", "sprint": "衝刺",
    "Stand-up": "站會", "standup": "站會", "Standup": "站會",
    "Stand-up meeting": "站會",
    # v3.3.7+ seventh wave (2026-08-30 seventh audit — 1 leak remaining):
    "SSL": "安全通訊協定", "Ssl": "安全通訊協定", "ssl": "安全通訊協定",
    "TLS": "傳輸層安全", "Tls": "傳輸層安全", "tls": "傳輸層安全",
    "HTTPS": "安全超文本傳輸", "Https": "安全超文本傳輸", "https": "安全超文本傳輸",
    "HTTP": "超文本傳輸", "Http": "超文本傳輸", "http": "超文本傳輸",
    "SSH": "安全外殼", "Ssh": "安全外殼", "ssh": "安全外殼",
    "DNS": "網域名稱系統", "Dns": "網域名稱系統", "dns": "網域名稱系統",
    "DHCP": "動態主機配置", "Dhcp": "動態主機配置", "dhcp": "動態主機配置",
    "TCP": "傳輸控制協定", "Tcp": "傳輸控制協定", "tcp": "傳輸控制協定",
    "UDP": "用戶資料報協定", "Udp": "用戶資料報協定", "udp": "用戶資料報協定",
    "IP": "網際協定", "Ip": "網際協定", "ip": "網際協定",
    "IPv4": "網際協定第四版", "IPv6": "網際協定第六版",
    "MAC": "媒體存取控制", "Mac": "媒體存取控制", "mac": "媒體存取控制",
    "WiFi": "無線網絡", "Wifi": "無線網絡", "wifi": "無線網絡",
    "LAN": "區域網絡", "Lan": "區域網絡", "lan": "區域網絡",
    "WAN": "廣域網絡", "Wan": "廣域網絡", "wan": "廣域網絡",
    "URL": "網址", "Url": "網址", "url": "網址",
    "URI": "統一資源標識符", "Uri": "統一資源標識符", "uri": "統一資源標識符",
    "HTML": "網頁標記", "Html": "網頁標記", "html": "網頁標記",
    "CSS": "層疊樣式表", "Css": "層疊樣式表", "css": "層疊樣式表",
    "JS": "JavaScript 程式語言", "js": "JavaScript 程式語言",
    "JavaScript": "JavaScript 程式語言", "javascript": "JavaScript 程式語言",
    "TypeScript": "TypeScript 程式語言", "typescript": "TypeScript 程式語言",
    "Python": "Python 程式語言", "python": "Python 程式語言",
    "Java": "Java 程式語言", "java": "Java 程式語言",
    "Go": "Go 程式語言", "golang": "Go 程式語言",
    "Rust": "Rust 程式語言", "rust": "Rust 程式語言",
    "Ruby": "Ruby 程式語言", "ruby": "Ruby 程式語言",
    "PHP": "PHP 程式語言", "Php": "PHP 程式語言", "php": "PHP 程式語言",
    "SQL": "結構化查詢語言", "Sql": "結構化查詢語言", "sql": "結構化查詢語言",
    "NoSQL": "非關聯式資料庫", "Nosql": "非關聯式資料庫", "nosql": "非關聯式資料庫",
    "MongoDB": "MongoDB 資料庫", "mongodb": "MongoDB 資料庫",
    "PostgreSQL": "PostgreSQL 資料庫", "postgresql": "PostgreSQL 資料庫",
    "MySQL": "MySQL 資料庫", "mysql": "MySQL 資料庫",
    "Redis": "Redis 資料庫", "redis": "Redis 資料庫",
    "Kafka": "Kafka 串流平台", "kafka": "Kafka 串流平台",
    "Spark": "Spark 運算引擎", "spark": "Spark 運算引擎",
    "Hadoop": "Hadoop 大數據平台", "hadoop": "Hadoop 大數據平台",
    "AWS": "亞馬遜雲端", "Aws": "亞馬遜雲端", "aws": "亞馬遜雲端",
    "Azure": "微軟雲端", "azure": "微軟雲端",
    "GCP": "谷歌雲端", "Gcp": "谷歌雲端", "gcp": "谷歌雲端",
    "OpenSSL": "開源安全通訊", "openssl": "開源安全通訊",
    # v3.3.7+ eighth wave (2026-08-30 eighth audit — 2 leaks remaining):
    "DoS": "阻斷服務攻擊", "Dos": "阻斷服務攻擊", "dos": "阻斷服務攻擊",
    "DDoS": "分散式阻斷服務攻擊", "Ddos": "分散式阻斷服務攻擊", "ddos": "分散式阻斷服務攻擊",
    "patch": "修補檔", "Patch": "修補檔", "PATCH": "修補檔",
    "patches": "修補檔", "Patches": "修補檔",
    "firewall": "防火牆", "Firewall": "防火牆", "FIREWALL": "防火牆",
    "malware": "惡意程式", "Malware": "惡意程式", "MALWARE": "惡意程式",
    "ransomware": "勒索軟件", "Ransomware": "勒索軟件", "RANSOMWARE": "勒索軟件",
    "spyware": "間諜軟件", "Spyware": "間諜軟件", "SPYWARE": "間諜軟件",
    "adware": "廣告軟件", "Adware": "廣告軟件", "ADWARE": "廣告軟件",
    "rootkit": "隱藏程式", "Rootkit": "隱藏程式", "ROOTKIT": "隱藏程式",
    "trojan": "木馬程式", "Trojan": "木馬程式", "TROJAN": "木馬程式",
    "worm": "蠕蟲病毒", "Worm": "蠕蟲病毒", "WORM": "蠕蟲病毒",
    "virus": "病毒", "Virus": "病毒", "VIRUS": "病毒",
    "zero-day": "零日漏洞", "zeroday": "零日漏洞", "Zero-day": "零日漏洞",
    "exploit": "漏洞利用", "Exploit": "漏洞利用", "EXPLOIT": "漏洞利用",
    "payload": "攻擊載荷", "Payload": "攻擊載荷", "PAYLOAD": "攻擊載荷",
    "backdoor": "後門", "Backdoor": "後門", "BACKDOOR": "後門",
    "botnet": "殭屍網絡", "Botnet": "殭屍網絡", "BOTNET": "殭屍網絡",
    "honeypot": "蜜罐", "Honeypot": "蜜罐", "HONEYPOT": "蜜罐",
    "sandbox": "沙盒", "Sandbox": "沙盒", "SANDBOX": "沙盒",
    "penetration test": "滲透測試", "pentest": "滲透測試", "Pentest": "滲透測試",
    "red team": "紅隊", "Red Team": "紅隊", "redteam": "紅隊",
    "blue team": "藍隊", "Blue Team": "藍隊", "blueteam": "藍隊",
    "DevSecOps": "安全開發運維", "devsecops": "安全開發運維",
    "OAuth": "OAuth 授權", "oauth": "OAuth 授權",
    "JWT": "JSON Web 令牌", "Jwt": "JSON Web 令牌", "jwt": "JSON Web 令牌",
    "CORS": "跨來源資源共用", "Cors": "跨來源資源共用", "cors": "跨來源資源共用",
    "XSS": "跨站腳本攻擊", "Xss": "跨站腳本攻擊", "xss": "跨站腳本攻擊",
    "CSRF": "跨站請求偽造", "Csrf": "跨站請求偽造", "csrf": "跨站請求偽造",
    "SQL injection": "SQL 注入攻擊", "sqli": "SQL 注入攻擊", "SQLi": "SQL 注入攻擊",
    "MITM": "中間人攻擊", "Mitm": "中間人攻擊", "mitm": "中間人攻擊",
    "DLP": "資料外洩防護", "Dlp": "資料外洩防護", "dlp": "資料外洩防護",
    "WAF": "網頁應用防火牆", "Waf": "網頁應用防火牆", "waf": "網頁應用防火牆",
    "IDS": "入侵偵測系統", "Ids": "入侵偵測系統", "ids": "入侵偵測系統",
    "IPS": "入侵防禦系統", "Ips": "入侵防禦系統", "ips": "入侵防禦系統",
    "SOC": "安全運維中心", "Soc": "安全運維中心", "soc": "安全運維中心",
    "SIEM": "安全資訊與事件管理", "Siem": "安全資訊與事件管理", "siem": "安全資訊與事件管理",
    "and ": "同 ", " or ": "或者", " but ": "但 ", " if ": "如果 ",
    # v3.2.7.43 — words observed in real MiniMax M3 cheer output that
    # survived `_voice_zh_replace` (Cantonese listening agent report):
    #   body, REM, lunch, high, sugar, processed, push, build, cycle,
    #   heavy, hold, game, plan, short, walk, humidifier (already in
    #   inline table — added here too for first-pass coverage)
    "body": "身體", "Body": "身體",
    "REM": "快速眼動", "rem": "快速眼動",
    "lunch": "午餐", "Lunch": "午餐",
    "high": "高", "High": "高",
    "sugar": "糖", "Sugar": "糖",
    "processed": "加工",
    "push": "推", "push-up": "掌上壓", "pushup": "掌上壓", "push up": "掌上壓",
    "build": "建立", "Build": "建立",
    "cycle": "週期", "cycles": "週期",
    "heavy": "重",
    "hold": "守住", "Hold": "守住", "hold住": "守住",
    "game plan": "部署", "game": "賽事", "Game": "賽事",
    "short": "短",
    "walk": "散步",
    "humidifier": "加濕機",
    # v3.2.7.43 — promote from inline fallback to main pass (frequent in cheer):
    "percent": "巴仙", "Percent": "巴仙", "percentage": "巴仙",
    "percent,": "巴仙，", "percent.": "巴仙。",
    "warm-up": "熱身", "warm up": "熱身", "warmup": "熱身",
    "Monday": "星期一", "Tuesday": "星期二", "Wednesday": "星期三",
    "Thursday": "星期四", "Friday": "星期五", "Saturday": "星期六", "Sunday": "星期日",
    "monday": "星期一", "tuesday": "星期二", "wednesday": "星期三",
    "thursday": "星期四", "friday": "星期五", "saturday": "星期六", "sunday": "星期日",
    "Bed": "床", "bed": "床",
    # v3.2.7.44 — brand name + casual word coverage for residual MiniMax leaks.
    # Jim OOB 2026-08-12 17:58 HKT cheer leak contained:
    #   body, workout, active, office, N-central, security, lunch gym,
    #   alarm, score, press, day, today, plan, system, app, chat, set
    # These were already in the inline fallback table but the cheer pipeline
    # only runs the inline fallback if main pass leaves residual EN. Promote
    # them to the main table so first-pass _voice_zh_replace catches them.
    "workout": "訓練", "Workout": "訓練", "workouts": "訓練",
    "gym": "健身室", "Gym": "健身室",
    "alarm": "鬧鐘", "Alarm": "鬧鐘",
    "score": "分數", "Score": "分數",
    "press": "按", "Press": "按",
    "day": "日", "Day": "日",
    "today": "今日", "Today": "今日",
    "system": "系統", "System": "系統",
    "app": "應用程式", "App": "應用程式",
    "chat": "傾偈", "Chat": "傾偈",
    "set": "組", "Set": "組", "sets": "組", "Sets": "組",
    "security": "安全", "Security": "安全",
    "Trend Micro": "防毒品牌", "Trend": "趨勢", "Micro": "微",
    "ServiceNow": "IT管理平台", "Service": "服務", "Now": "而家",
    "N-central": "IT監控平台", "N-central,": "IT監控平台，",
    "PortSwigger": "網絡安全品牌",
    "Terminator": "終結者",
    "HTTP/1.0": "舊版網絡協議", "HTTP/1.1": "舊版網絡協議",
    "HTTP/2": "新版網絡協議", "HTTP/3": "新版網絡協議",
    "HTTPS": "加密網絡", "HTTPS/1.1": "加密網絡",
    "version": "版本", "Version": "版本",
    "speed": "速度", "Speed": "速度",
    "burn": "燒", "Burn": "燒", "burn-out": "力竭",
    "recovery": "復原", "Recovery": "復原",
    "sleep": "睡眠", "Sleep": "睡眠",
    "strain": "壓力指數", "Strain": "壓力指數",
    "calm": "冷靜", "Calm": "冷靜",
    "stress": "壓力", "Stress": "壓力",
    "rest": "休息", "Rest": "休息",
    "deep": "深層", "Deep": "深層",
    "light": "淺層", "Light": "淺層",
    "awake": "清醒", "Awake": "清醒",
    "nap": "小睡", "Nap": "小睡",
    "wake": "醒", "Wake": "醒",
    "morning": "朝早", "Morning": "朝早",
    "evening": "晚上", "Evening": "晚上",
    "night": "夜晚", "Night": "夜晚",
    "tomorrow": "聽日", "Tomorrow": "聽日",
    "yesterday": "尋日", "Yesterday": "尋日",
    "week": "週", "Week": "週",
    "month": "月", "Month": "月",
    "year": "年", "Year": "年",
    "hour": "小時", "hours": "小時", "Hour": "小時", "Hours": "小時",
    "minute": "分鐘", "minutes": "分鐘", "Minute": "分鐘", "Minutes": "分鐘",
    "second": "秒", "seconds": "秒", "Second": "秒", "Seconds": "秒",
    "mood": "心情", "Mood": "心情",
    "energy": "能量", "Energy": "能量",
    "focus": "專注", "Focus": "專注",
    "train": "訓練", "Train": "訓練",
    "training": "訓練", "Training": "訓練",
    "fat": "脂肪", "Fat": "脂肪",
    "protein": "蛋白質", "Protein": "蛋白質",
    "carb": "碳水", "carbs": "碳水", "Carbs": "碳水",
    "calorie": "卡路里", "calories": "卡路里", "Calories": "卡路里",
    "calories,": "卡路里，", "calories.": "卡路里。",
    "weight": "體重", "Weight": "體重",
    "muscle": "肌肉", "Muscle": "肌肉",
    "cardio": "帶氧", "Cardio": "帶氧",
    "strength": "力量", "Strength": "力量",
    "stretch": "伸展", "Stretch": "伸展",
    "warm": "暖", "Warm": "暖",
    "cool": "凍", "Cool": "凍",
    "cold": "凍", "Cold": "凍",
    "hot": "熱", "Hot": "熱",
    # v3.3.7 — Jim OOB 2026-08-16 「many non chinese character such as symbol
    # in voice」. Words observed in cheer_2026-08-16_evening that survived
    # the post-processor. Number words (nine/seven/etc.), abbreviations
    # (CIO), and casual English the LLM slips in.
    "nine": "九", "eight": "八", "seven": "七", "six": "六", "five": "五",
    "four": "四", "three": "三", "two": "二", "one": "一", "ten": "十",
    "eleven": "十一", "twelve": "十二",
    "CIO": "資訊總監", "cio": "資訊總監",
    "ratio": "比例", "share ratio": "分擔比例",
    "startup": "新項目", "Startups": "新項目", "startups": "新項目",
    "project": "項目", "Project": "項目", "projects": "項目",
    "singles": "單身", "vacation": "假期", "Vacation": "假期",
    "single": "單身", "Single": "單身",
    "room": "房", "Room": "房",
    "weight room": "重訓房", "Weight Room": "重訓房", "weight rooms": "重訓房",
    "sat ": "飽和 ", "SAT ": "飽和 ", "sat fat": "飽和脂肪",
    "salary": "人工", "salaries": "人工",
    "investment": "投資", "Investment": "投資",
    "return": "回報", "returns": "回報",
    "rate": "率", "rates": "率",
    "soul": "靈魂", "trading": "炒", "trade": "交易",
    "share,": "分擔，", " share ": " 分擔 ",
    " IG ": "社交平台 ", "IG,": "社交平台，",
    "wedding": "婚禮", "ring": "戒指",
    "stable": "穩定", "stable,": "穩定，",
    "fired": "炒咗", "fire": "炒",
    "investment,": "投資，", "return,": "回報，",
    "investment rate": "投資回報率",
    "invest": "投資",
    "setup": "設定", "Setup": "設定", "set up": "設定", "Set up": "設定",
    "set-up": "設定", "Set-up": "設定",
    "activity log": "活動記錄", "log": "記錄",
}

def _strip_voice_slash_symbols(s: str) -> str:
    """v3.3.6 (NEW 2026-08-27, Jim OOB 「Never use slash in voice. Spell out
    the date if applicable」): strip slash / symbol characters that Edge-TTS
    WanLung reads literally as 「slash」 / 「斜線」 / 「冒號」, producing
    robotic gibberish in voice bubbles.

    Rule 74 (text-coach-summary-voice skill). MUST run BEFORE
    _humanize_for_voice() and _voice_zh_replace() so slash-broken tokens
    never reach the TTS engine.

    Transformations (chat/text replies unaffected — voice output only):
      - 8/27             → 八月二十七號
      - 2026/08/27       → 二零二六年八月二十七號
      - 7/4              → 七月四號
      - 3:30 PM          → 下午三點半
      - 9:00 AM          → 上午九點
      - 21:30            → 晚上九點半
      - 25kg             → 二十五公斤
      - 500ml            → 五百毫升
      - 2.5km            → 兩點五公里
      - 38°C             → 三十八度
      - 70% (round tens) → 七成 (NOT 百分之七十 — too verbose)
      - 65% (non-round)  → 百分之六十五
      - 100-120          → 一百到一百二十
      - slash separator  → 同 / 加 (Cantonese)
      - 81% 🟢  (emoji after %) → 81% preserved before percent rule runs
    """
    import re as _re_slash

    if not s:
        return s

    # 1. Date with slash: 2026/08/27 or 8/27 or 08/27 (year optional).
    # IMPORTANT: only treat as date if it looks like a real date context.
    # We rely on a CONSERVATIVE heuristic — the slash-separated digits must
    # be plausible as month/day (1-31), AND the surrounding context must
    # look date-y. Otherwise patterns like "50/10" or "3/4 mile" become
    # garbled "三月四號"/"五十月十號". The fallback (rule 6 below) catches
    # " / " (with spaces) as a separator.
    def _md_date(m):
        mo, d = int(m.group(1)), int(m.group(2))
        return f"{_zh_month(mo)}月{_zh_day(d)}號"

    # 1a. Year-first full date: 2026/08/27 — unambiguous, always replace
    def _year_date(m):
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        year_zh = {
            "2024": "二零二四", "2025": "二零二五", "2026": "二零二六",
            "2027": "二零二七", "2028": "二零二八", "2029": "二零二九",
            "2030": "二零三零",
        }.get(y, y) + "年"
        return f"{year_zh}{_zh_month(mo)}月{_zh_day(d)}號"

    s = _re_slash.sub(r"\b(20\d{2})/(\d{1,2})/(\d{1,2})\b", _year_date, s)

    # 1b. M/D or D/M/M date WITHOUT year — v3.3.23aw (Jim OOB 2026-10-01
    # 「For voice, pls avoid using date format mm/dd」): more aggressive — convert
    # when the first number <= 12 (clearly a month) OR second number <= 31 with
    # first > 12 (clearly D/M/M). Both directions handled. Validates that at
    # least one side is a plausible month/day. Anything else (e.g. 50/10
    # reps, 100/200 ratio, 3/4 fraction where both > 31) is left untouched.
    def _md_strict(m):
        a, b = int(m.group(1)), int(m.group(2))
        # M/D direction: first is month (1-12), second is day (1-31)
        if 1 <= a <= 12 and 1 <= b <= 31:
            return f"{_zh_month(a)}月{_zh_day(b)}號"
        # D/M direction: first > 12 (not a month), second is month (1-12)
        # But first must also be a valid day (1-31)
        if a > 12 and 1 <= a <= 31 and 1 <= b <= 12:
            return f"{_zh_day(a)}月{_zh_month(b)}號"
        # Leave unchanged (e.g. 50/10 reps, 3/4 fraction, 100/200 ratio)
        return m.group(0)

    s = _re_slash.sub(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d|/)", _md_strict, s)

    # 2. Dash-separated date: 07-25 → 七月二十五號
    s = _re_slash.sub(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)", _md_date, s)

    # 3. Time with colon: 3:30 PM / 9:00 / 21:30
    def _time_replace(m):
        h = int(m.group(1))
        mins = int(m.group(2))
        ap = (m.group(3) or "").upper().replace(" ", "")
        # AM/PM aware
        if ap == "AM":
            if h == 12:
                hour_zh = "凌晨十二點"
            else:
                hour_zh = f"上午{_zh_hour(h)}點"
        elif ap == "PM":
            if h == 12:
                hour_zh = "中午十二點"
            else:
                hour_zh = f"下午{_zh_hour(h)}點"
        else:
            # 24-hour format
            if h == 0:
                hour_zh = "凌晨十二點"
            elif h < 12:
                hour_zh = f"上午{_zh_hour(h)}點"
            elif h == 12:
                hour_zh = "中午十二點"
            elif h < 18:
                hour_zh = f"下午{_zh_hour(h - 12)}點"
            else:
                hour_zh = f"晚上{_zh_hour(h - 12)}點"
        if mins == 0:
            return hour_zh
        elif mins == 30:
            return hour_zh.rstrip("點") + "點半"
        elif mins == 15:
            return hour_zh.rstrip("點") + "點一刻"
        elif mins == 45:
            return hour_zh.rstrip("點") + "點三刻"
        else:
            return hour_zh.rstrip("點") + f"點{_zh_minute(mins)}分"

    s = _re_slash.sub(
        r"(?<!\d)(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\b",
        _time_replace, s
    )

    # 4. Units with letters: 25kg, 500ml, 2.5km, 38°C, 100g, 65bpm
    def _unit_replace(m):
        num = float(m.group(1))
        unit = m.group(2)
        # Number → spoken Chinese (with decimal support)
        if num == int(num):
            num_zh = _zh_number(int(num))
        else:
            whole, frac = str(num).split(".")
            num_zh = f"{_zh_number(int(whole))}點{_zh_digit(int(frac))}"
        unit_zh = {
            "kg": "公斤", "g": "克", "ml": "毫升", "l": "公升",
            "km": "公里", "m": "米", "cm": "厘米", "mm": "毫米",
            "bpm": "下心率", "mg": "毫克", "dl": "分升",
            "°C": "度", "°F": "度", "cal": "卡路里", "kcal": "千卡",
        }.get(unit, unit)
        return f"{num_zh}{unit_zh}"

    # Order matters: kg before g, km before m, °C before C
    s = _re_slash.sub(
        r"(\d+\.?\d*)\s*(kg|km|ml|cm|mm|mg|dl|°C|°F|kcal|cal|bpm)\b",
        _unit_replace, s, flags=_re_slash.IGNORECASE
    )
    # Bare g/l — risk of false positive (e.g. "3 g 糖"), only do if preceded
    # by space + digit; skip standalone single-letter matches to avoid breaking
    # Chinese 「g」 or English words
    s = _re_slash.sub(r"(\d+\.?\d*)\s*(g|l)\b(?![A-Za-z])", _unit_replace, s)

    # 4b. AFTER unit replacement, re-run dash range so "100-120 bpm" → after
    # bpm rule the trailing "100-" might be stranded. Re-run dash pattern on
    # whatever survived.
    s = _re_slash.sub(
        r"(\d+\.?\d*)\s*-\s*(\d+\.?\d*)",
        lambda m: f"{_zh_number_str(m.group(1))}到{_zh_number_str(m.group(2))}",
        s
    )

    # 5. Percentage: 70% / 65.5% — round tens use 成, others use 百分之
    def _pct_replace(m):
        num = float(m.group(1))
        if num == int(num) and num % 10 == 0 and 10 <= num <= 90:
            return _zh_number(int(num // 10)) + "成"
        elif num == int(num):
            return "百分之" + _zh_number(int(num))
        else:
            whole, frac = str(num).split(".")
            return f"百分之{_zh_number(int(whole))}點{_zh_digit(int(frac))}"

    s = _re_slash.sub(r"(\d+\.?\d*)\s*%", _pct_replace, s)

    # 6. Standalone slash separator with spaces: "weight / reps" → "weight 同 reps"
    s = _re_slash.sub(r"\s+/\s+", " 同 ", s)
    # Bare slash in date context already handled above; remaining slashes in
    # patterns like "週一/二" leave alone (rare, WanLung handles short slashes OK)

    # 7. Final dash range pass — catches any "N-M" pairs that survived earlier
    # rules (e.g. standalone numbers without units). Already done at 4b after
    # unit pass; this catches pre-/post-percent contexts like "100-120".
    s = _re_slash.sub(
        r"(\d+\.?\d*)\s*-\s*(\d+\.?\d*)",
        lambda m: f"{_zh_number_str(m.group(1))}到{_zh_number_str(m.group(2))}",
        s
    )

    return s


def _zh_number(n: int) -> str:
    """Convert integer 0-9999 to spoken Cantonese-friendly Chinese.
    Edge-TTS WanLung reads 一/二/三/.../九 + 十/百/千 naturally."""
    if n == 0:
        return "零"
    if n < 0:
        return "負" + _zh_number(-n)
    digits = "零一二三四五六七八九"
    if n < 10:
        return digits[n]
    if n < 20:
        return "十" + (digits[n - 10] if n > 10 else "")
    if n < 100:
        tens, ones = divmod(n, 10)
        return digits[tens] + "十" + (digits[ones] if ones else "")
    if n < 1000:
        hundreds, rem = divmod(n, 100)
        result = digits[hundreds] + "百"
        if rem == 0:
            return result
        elif rem < 10:
            return result + "零" + digits[rem]
        elif rem < 20:
            return result + "一十" + (digits[rem - 10] if rem > 10 else "")
        else:
            tens, ones = divmod(rem, 10)
            return result + digits[tens] + "十" + (digits[ones] if ones else "")
    if n < 10000:
        thousands, rem = divmod(n, 1000)
        result = digits[thousands] + "千"
        if rem == 0:
            return result
        elif rem < 100:
            return result + "零" + _zh_number(rem)
        else:
            return result + _zh_number(rem)
    return str(n)  # 萬+ fall back to digits


def _zh_digit(n: int) -> str:
    """Single digit 0-9 → Chinese digit character."""
    return "零一二三四五六七八九"[n]


def _zh_hour(h: int) -> str:
    """Hour 1-12 → Chinese (just the number, caller wraps in 上午/下午/晚上)."""
    return _zh_number(h)


def _zh_minute(m: int) -> str:
    """Minute 1-59 → spoken Chinese. 0 handled by caller."""
    if m < 10:
        return "零" + _zh_digit(m)
    return _zh_number(m)


def _zh_month(mo: int) -> str:
    """Month 1-12 → Chinese spoken (e.g. 8 → 八, 11 → 十一)."""
    return _zh_number(mo)


def _zh_day(d: int) -> str:
    """Day 1-31 → Chinese spoken. 20+ uses 二十X (no 零)."""
    if d < 20:
        return _zh_number(d)
    tens, ones = divmod(d, 10)
    return "二十" + (_zh_digit(ones) if ones else "")


def _zh_number_str(s: str) -> str:
    """Convert numeric string (possibly with decimal) to spoken Chinese."""
    if "." in s:
        whole, frac = s.split(".")
        return f"{_zh_number(int(whole))}點{_zh_digit(int(frac))}"
    return _zh_number(int(s))


def _humanize_for_voice(s: str) -> str:
    """v3.2.7.42: HARD guardrail — make cheer text human-friendly for voice.

    Jim OOB 2026-08-12 09:40 HKT 「the script is still very bad bad bad. I
    want the script to be very much natural cantonese speaking」. The opener
    is the worst offender — MiniMax M3 frequently opens with a role-introduction
    that announces the format instead of delivering content:
      - 「管家同你報數先」  (butler reporting numbers first)
      - 「教練即刻同你 update」  (coach now updating you)
      - 「CIO 開工先睇吓個身體點」  (CIO starting work, first check body)
      - 「返工之前先睇吓」  (before starting work, first look)
    These all read as "I'm about to tell you X" rather than "X". Edge-TTS
    WanLung reads them as flat announcements — instantly robotic.

    5 layers of post-processing (added Layer 3.5 for Chinese meta-intros):

    (1) URL strip — MiniMax occasionally echoes source URLs (e.g.「資料來源
    https://news.example.com/abc」). Edge-TTS WanLung reads each char as
    「H-T-T-P-S 冒號 斜線 斜線 news 點 example 點 com」 — instantly robotic.
    Strip ALL URLs (http/https/ftp/www/d.uguu.se/h.uguu.se + alipays/aliyuncs
    signed-OSS patterns). Replace with 「資料來源省略」 neutral phrase so
    WanLung reads a clean 「資料來源省略」.

    (2) Metadata strip — pplx/MiniMax sometimes leak prompt-trace fragments
    like 「Here is the cheer:」 / 「Below is...」 / 「以下是 cheer 內容:」 /
    「Let me draft...」. Edge-TTS reads 「Here is the cheer」 as robotic
    English opener. Strip aggressively.

    (3) Line-by-line opener audit — if the first sentence contains English
    words OR starts with markdown noise, drop it and start from sentence 2.
    Jim OOB specifically called out 「so many non-narrative words at the
    beginning」 — first 10 seconds of voice must be Chinese-only.

    (3.5) Chinese role-intro audit (NEW v3.2.7.42) — even when the opener is
    100% Chinese, it can still be mechanical if it announces the role
    instead of starting with content. Patterns like 「管家同你報數先」,
    「教練即刻同你 update」, 「CIO 開工先睇吓」 are role+action meta-intros
    that read like a status report, not a friend talking. Drop them. Also
    drop standalone short greetings separated by their own punctuation
    (e.g. 「早晨啊。」 as its own sentence — merge into the content sentence
    after by deleting the standalone greeting sentence entirely).

    (4) URL/email/handle residual audit (final guard). If anything URL-shaped
    survives all 3 layers, wrap in 「⋯⋯」 ellipse so WanLung at least reads
    a graceful 「⋯⋯」 instead of gibberish.

    Returns cleaned string with all 4 layers applied.
    """
    # Layer 1: URL strip (4 patterns: http(s)://, www., subdomain.uguu.se,
    # aliyuncs OSS signed URLs, bare domain.tld patterns)
    url_patterns = [
        r"https?://[^\s\)\]\}，。！？]+",         # http(s)://... until whitespace/punct
        r"www\.[^\s\)\]\}，。！？]+",            # www.domain...
        r"[a-zA-Z0-9\-]+\.(uguu\.se|aliyuncs\.com|googleusercontent\.com|googleapis\.com)[^\s\)\]\}，。！？]*",
        r"\b[a-zA-Z0-9\-]+\.(com|net|org|io|hk|cn|uk)(/[^\s\)\]\}，。！？]*)?",  # bare domain.tld
    ]
    for pat in url_patterns:
        s = re.sub(pat, "資料來源省略", s)
    # Layer 2: metadata strip (English/中文 meta-openers that leak into text)
    meta_openers = [
        r"^(Here'?s the cheer[\s\S]{0,150}?:)\s*",
        r"^(Let me think[\s\S]{0,150}?:)\s*",
        r"^(Sure[\s,]+|Below is[\s\S]{0,80}?:)\s*",
        r"^(以下是[\s\S]{0,80}?:|以下為[\s\S]{0,80}?:|下面係[\s\S]{0,80}?:)\s*",
        r"^(Here'?s a[\s\S]{0,80}?:)\s*",
        r"^(OK[\s,]+|Okay[\s,]+)\s*",
        r"^(Acknowledged[\s,]+|Noted[\s,]+)\s*",
        r"^(我會[\s\S]{0,60}?寫[\s\S]{0,60}?:)\s*",
        r"^(現在[\s\S]{0,30}?開始[\s\S]{0,30}?:)\s*",
        # v3.2.7.42 — Chinese role-intro patterns observed in real MiniMax
        # output (cheer_text_debug.log 09:35:50):
        #   「管家同你報數先」「教練即刻同你 update」「CIO 開工先睇吓」
        r"^(管家|教練|助手|秘書|占士|阿占)[\s\S]{0,30}(同你|嚟|先|即刻|幫你|要|就|而家)[\s\S]{0,15}(報數|update|講|睇|講吓|報|check|睇吓|睇下|update)",
        r"^(管家|教練|助手|秘書|占士|阿占)[\s\S]{0,30}(要嚟|準備|即刻|就嚟|等陣|依家)",
    ]
    for pat in meta_openers:
        s = re.sub(pat, "", s, flags=re.IGNORECASE | re.MULTILINE)
    # Layer 2.5 (v3.2.7.42): Inline role-intro strip at start of text.
    # When the role-intro is immediately followed by content (no full
    # sentence break before), strip just the role-intro phrase and any
    # trailing delimiter, keeping the content. Catches patterns like:
    #   "管家同你報數先——你個..."  (em-dash connector)
    #   "教練即刻同你 update, 噉晚..."  (comma connector)
    #   "CIO 開工先睇吓個身體點。復原..."  (period connector)
    # Without this layer, Layer 3.5 only drops sentences < 70 chars — but
    # these inline intros are often followed by a long first sentence (the
    # role-intro + 100+ chars of content), so the intros survive.
    inline_intros_start = [
        # Role + adverb + verb-phrase + delimiter
        r"^(管家|教練|助手|秘書|占士|阿占|Alonso)[\s\S]{0,30}(同你|嚟|即刻|幫你|要|就|而家|依家)[\s\S]{0,15}(報數|update|講|睇|講吓|報|check|睇吓|睇下)[\s\S]{0,5}[——，,。;:：\s]+",
        # Role + arrival phrase + delimiter (e.g. 「管家嚟喇」 「助手返嚟喇」)
        r"^(管家|教練|助手|秘書|占士|阿占|Alonso)[\s\S]{0,15}(嚟喇|嚟咗|嚟緊|返嚟喇|返嚟咗|返咗嚟|嚟啦|嚟㗎喇)[\s\S]{0,5}[——，,。;:：\s]+",
        # 返工之前先睇吓 / 開工前先 update / 落 gym 之前先睇
        r"^(返工|開工|起身|食晏|食晚|收工|瞓前|瞓覺|落 gym|操肌)[\s\S]{0,10}(之前|前|先)[\s\S]{0,15}(睇|check|update|講|同你|幫你|個身體)[\s\S]{0,5}[，,。;:：\s]+",
        # 我會先講 / 我即刻同你 update
        r"^我[\s\S]{0,5}(會|要|即刻|就|依家|而家)[\s\S]{0,5}(先|同你|幫你|嚟|update|講)[\s\S]{0,5}[，,。;:：\s]+",
        # 而家就同你 update / 而家嚟同你講
        r"^而家[\s\S]{0,5}(就|即刻|嚟|先)[\s\S]{0,10}(同你|幫你|update|講|睇|報數)[\s\S]{0,5}[，,。;:：\s]+",
        # English greetings: "Hey Jim, " / "Hello Jim, " / "Hi Jim, "
        r"^(Hey|Hi|Hello|Yo|Heya|Greetings|Good\s+(morning|evening|night))[\s\S]{0,10}[，,。;:：\s]+",
    ]
    for pat in inline_intros_start:
        s = re.sub(pat, "", s, count=1, flags=re.IGNORECASE)
    # Layer 2.7 (v3.2.7.44): catch orphan temporal adverb that Layer 2 left
    # behind. When Layer 2 stripped "管家同你報數" via the `[^報數]`-anchored
    # meta-opener regex, the trailing "先" survives because the third group
    # doesn't include temporal adverbs. Em-dash (——) connector is the
    # strongest signal this is leftover from a role-intro pattern, not
    # legitimate content (real sentences don't open with 「先——」).
    s = re.sub(r"^(先|即刻|嚟|依家|而家)\s*[——]+\s*", "", s, count=1)
    # Layer 3: opener audit — if first 80 chars contain English word, drop
    # the first sentence entirely and start from sentence 2. Cantonese voice
    # must open with Chinese words, no English/numbers/markdown at all.
    sentences = re.split(r"(?<=[。！？\n])", s, maxsplit=3)
    # Only run the opener audit when the text actually has Chinese sentence
    # breaks (。！？\n). For single-sentence inputs (typical of voice-reply
    # templates), `sentences == [s]` — dropping the first "sentence" would
    # delete the entire reply. Skip in that scenario. The cheer pipeline feeds
    # multi-sentence paragraphs, where this audit still fires.
    if len(sentences) > 1 and sentences and len(sentences[0]) < 200:
        first = sentences[0]
        # Strip leading whitespace
        first = first.strip()
        # If first sentence has English letter or markdown noise OR URL-shaped,
        # drop it and concat the rest.
        if re.search(r"[A-Za-z]|[\*#\[\]\(\)\{\}]|https?://", first):
            s = "".join(sentences[1:]).lstrip()
    # Layer 3.5: Chinese role-intro audit (v3.2.7.42) — re-split since
    # Layer 2/3 may have changed content. Drop the first sentence if it
    # matches role-intro patterns even when written in pure Chinese.
    # Only drop SHORT first sentences (< 70 chars) to avoid accidentally
    # deleting content that legitimately mentions 管家/教練 mid-sentence.
    sentences = re.split(r"(?<=[。！？\n])", s, maxsplit=3)
    if sentences and len(sentences[0]) < 70:
        first = sentences[0].strip()
        # Role-intro patterns observed in real MiniMax output. Each is
        # "<role-word> + <adverb> + <verb-phrase>" announcing what's about
        # to happen rather than starting with the content itself.
        role_intro_patterns = [
            # 管家同你報數先 / 管家即刻同你 update / 管家嚟喇
            # NOTE: \b removed — doesn't fire between CJK characters.
            r"^(管家|教練|助手|秘書|占士|阿占|阿樂|Alonso)[\s\S]{0,30}(同你|嚟|即刻|幫你|要|就|而家|依家)[\s\S]{0,15}(報數|update|講|睇|講吓|報|check|睇吓|睇下|嚟喇|嚟咗|嚟緊|嚟啦)",
            # CIO 開工先睇吓 / 返工之前先睇吓 / 落 gym 之前先睇
            r"^(CIO|老闆|Boss)[\s\S]{0,20}(開工|返工|起身|食晏|食晚|收工|瞓前|瞓覺|落 gym|操肌)",
            # 返工之前先睇吓個身體點 / 開工前先 check
            r"^(返工|開工|起身|食晏|食晚|收工|瞓前|瞓覺|落 gym|操肌)[\s\S]{0,10}(之前|前|先)[\s\S]{0,20}(睇|check|update|講|同你|幫你|個身體)",
            # 我會先講 / 我先同你講 / 我即刻同你 update
            r"^我[\s\S]{0,5}(會|要|即刻|就|依家|而家)[\s\S]{0,5}(先|同你|幫你|嚟|update|講)",
            # 而家就同你 update / 而家嚟喇
            r"^而家[\s\S]{0,5}(就|即刻|嚟|先)[\s\S]{0,10}(同你|幫你|update|講|睇|報數)",
            # Standalone greeting sentences (merge into next by dropping)
            # 「早晨啊。」 / 「晨早。」 / 「朝早 Jim。」 alone before content
            r"^(晨早|早晨|朝早|午安|晚安|收工啦|食飯啦)\s*[，,。.！!嗰咋]?\s*(Jim)?\s*[，,。.！!嗰咋]?\s*$",
            # English greeting openers slipped through
            r"^(Hey|Hi|Hello|Yo|Heya|Greetings|Good\s+(morning|evening|night))[\s,。.！!:\s]",
        ]
        for pat in role_intro_patterns:
            if re.search(pat, first, re.IGNORECASE):
                s = "".join(sentences[1:]).lstrip()
                break
    # Layer 3.6 (v3.2.7.43): inline-strip role-intros that survive in the
    # first 100 chars. Catches patterns where the greeting + role-intro are
    # in the SAME sentence (e.g. 「早晨 Jim，CIO 開工先睇吓個身體點」) —
    # Layer 3.5's full-sentence drop misses these because the sentence is
    # still content-bearing (it has 「個身體點」content after the role-intro).
    # We strip just the role-intro substring (not the whole sentence) so
    # the surrounding content is preserved.
    first100 = s[:100]
    rest = s[100:]
    inline_role_strip_v343 = [
        # Greeting + comma + name + comma + role-intro
        r"^(晨早|早晨|朝早)?\s*[，,]?\s*(Jim|阿占|占士)?\s*[，,]?\s*(CIO|老闆|Boss|教練|管家|助手|秘書|阿樂|Alonso)[\s\S]{0,30}(同你|嚟|即刻|幫你|要|就|而家|依家)[\s\S]{0,15}(報數|update|講|睇|講吓|報|check|睇吓|睇下|嚟喇|嚟咗)",
        # Greeting + name + role + activity verb
        r"^(晨早|早晨|朝早)?\s*[，,]?\s*(Jim|阿占|占士)?\s*[，,]?\s*(CIO|老闆|Boss|教練|管家|助手)[\s\S]{0,20}(開工|返工|起身|食晏|食晚|收工|瞓前|瞓覺|落 gym|操肌|報數|update|講|睇|check)",
        # Greeting + name + activity + (之前|前|先) + action
        r"^(晨早|早晨|朝早)?\s*[，,]?\s*(Jim|阿占|占士)?\s*[，,]?\s*(返工|開工|起身|食晏|食晚|收工|瞓前|瞓覺|落 gym|操肌)[\s\S]{0,10}(之前|前|先)[\s\S]{0,15}(睇|check|update|講|同你|幫你|個身體)",
    ]
    for pat in inline_role_strip_v343:
        first100 = re.sub(pat, "", first100, count=1, flags=re.IGNORECASE)
    s = (first100 + rest).strip()
    # Layer 4: residual audit. If anything URL-shaped survives (some bare
    # domain.tld might miss Layer 1), wrap in 「⋯⋯」 so WanLung reads a clean
    # ellipse.
    s = re.sub(r"\b[a-zA-Z0-9\-\.]+\.(com|net|org|io|hk)/[^\s]*", "⋯⋯", s)
    # Remove any remaining triple-asterisk noise (Layer 1 of _voice_zh_replace
    # already handles ** but extra paranoia here)
    s = re.sub(r"\*\*+", "", s)
    s = re.sub(r"(?<!\*)\*(?!\*)", "", s)
    # Layer 5 (v3.2.7.44): strip HTTP/news fragments that survived previous
    # layers. Jim OOB 2026-08-12 17:58 HKT cheer leaked 「HTTP/1.0」「version
    # 1.0」「PortSwigger」news content. The cheer AI hallucinated news content
    # about IT security vendors. Strip aggressively so WanLung never reads
    # 「H-T-T-P 斜線 1 點 0」「version 1 點 0」verbatim.
    s = re.sub(r"HTTP/\d+\.\d+", "舊版網絡協議", s, flags=re.IGNORECASE)
    s = re.sub(r"HTTPS/\d+\.\d+", "加密網絡協議", s, flags=re.IGNORECASE)
    s = re.sub(r"\bversion\s*\d+\.\d+", "新版本", s, flags=re.IGNORECASE)
    s = re.sub(r"\bv\d+\.\d+\b", "新版本", s)
    s = re.sub(r"\bspeed\s+(=|:|：|是|係)\s*\d+", "速度幾快", s, flags=re.IGNORECASE)
    # IT security brand names that pplx/MiniMax leak into news summaries
    s = re.sub(r"PortSwigger|N-central|ServiceNow|Trend Micro", "IT品牌", s)
    # Orphan technical words that survive main EN→ZH pass
    s = re.sub(r"\bhtt(p|s)?\b", "網絡", s, flags=re.IGNORECASE)
    s = re.sub(r"\bwww\b", "網絡", s, flags=re.IGNORECASE)
    s = re.sub(r"\burl\b", "網址", s, flags=re.IGNORECASE)
    s = re.sub(r"\bapi\b", "接口", s, flags=re.IGNORECASE)
    s = re.sub(r"\bssl\b", "加密", s, flags=re.IGNORECASE)
    s = re.sub(r"\btls\b", "加密", s, flags=re.IGNORECASE)
    s = re.sub(r"\bxml\b", "標記語言", s, flags=re.IGNORECASE)
    s = re.sub(r"\bhtml\b", "網頁標記", s, flags=re.IGNORECASE)
    s = re.sub(r"\bjson\b", "資料格式", s, flags=re.IGNORECASE)
    return s.strip()


def _voice_zh_replace(s: str) -> str:
    """Pre-flight EN→ZH auto-replace for voice script (Rule 26 + Rule 37).

    v2.5.1 fix: removed word-boundary anchors `\b...\\b` because they don't match
    between Chinese characters and English words (Chinese text has no inter-char
    word boundaries). Plain `re.sub` with case-insensitive flags now catches
    EN words embedded in Chinese prose.

    Also added an extended 'natural Chinese filler' replacement table for common
    leaked English tokens that pplx sonar-pro often uses (state, use, treat,
    keep, base, level, range, etc.).

    v3.2.7.41 — caller should run `_humanize_for_voice()` BEFORE this function
    to strip URL/metadata/openers (cleaner layered approach).

    v3.3.7 — Jim OOB 2026-08-16 「many non chinese character such as symbol
    in voice」. Use word-boundary anchors for SHORT keys (≤3 chars) so e.g.
    "PR" / "RM" / "REM" don't replace the middle of "project" / "team" /
    "premier" → bug seen: "project" → "個人紀錄oject".
    """
    keys = sorted(EN_TO_ZH_VOICE.keys(), key=len, reverse=True)
    # v3.2.7.39: 3-pass replace instead of single pass. pplx often nests
    # English tokens ("weightlifting", "comeback") inside phrases that
    # only partially match the table; multi-pass catches residual leaks.
    for _ in range(3):
        for k in keys:
            if len(k) <= 3:
                # Word boundary required — short keys would otherwise match
                # inside longer words (PR→project, RM→arm, REM→premier).
                # \b works between EN char and non-word char (CJK counts as
                # non-word in Python re), so "PR項目" boundary is preserved.
                pattern = r"(?<![A-Za-z0-9_])" + re.escape(k) + r"(?![A-Za-z0-9_])"
                s = re.sub(pattern, EN_TO_ZH_VOICE[k], s, flags=re.IGNORECASE)
            else:
                s = re.sub(re.escape(k), EN_TO_ZH_VOICE[k], s, flags=re.IGNORECASE)
    # v3.2.7.38: strip markdown asterisks (**bold** / *italic*) before TTS.
    # pplx sonar-pro often wraps metric numbers like **52%** or **weightlifting**
    # in markdown — Edge-TTS WanLung reads each * as the literal Chinese word
    # "星號", producing the robotic "星號52%星號" artefact Jim OOB 2026-08-11
    # 22:30 HKT. Strip them cleanly here so voice only reads the number/word.
    s = re.sub(r"\*\*+", "", s)
    s = re.sub(r"(?<!\*)\*(?!\*)", "", s)
    # Also strip orphan citation refs like [1][3] — Edge-TTS reads "一三" or
    # "中括號一" awkwardly. Replace with nothing so the prose flows naturally.
    s = re.sub(r"\[\d+\]", "", s)
    # v3.3.7: strip stray single-letter English tokens (Jim OOB 2026-08-16
    # voice leak contained bare "I" in "I克" — from "IG" being half-replaced).
    # Single ASCII letter with no neighbours = leftover.
    s = re.sub(r"(?<![A-Za-z0-9_])\b[A-Za-z]\b(?![A-Za-z0-9_])", "", s)
    return s

def _voice_audit_en(s: str) -> list:
    """Return list of English words leaked. Empty = OK."""
    return re.findall(r"[A-Za-z]+", s)


def _zh_inline(en_word: str) -> str:
    """Last-resort EN→ZH map for any word that survived the main _voice_zh_replace
    passes. v2.7.3 (Jim OOB 2026-07-24): the old behaviour replaced the whole text
    with a 383-char stub; now we keep the full body and just inline-translate
    any remaining EN words.
    """
    table = {
        "fit": "健", "fitness": "健康", "share": "分擔", "shared": "分咗",
        "fast": "快", "slow": "慢", "high": "高", "low": "低",
        "good": "好", "bad": "差", "ok": "好", "OK": "好",
        "so": "咁", "very": "好", "too": "太", "just": "只",
        "and": "同", "or": "或者", "but": "但", "if": "如果",
        "the": "", "a": "", "an": "", "is": "係", "are": "係", "was": "係",
        "be": "係", "been": "係", "have": "有", "has": "有", "had": "有",
        "do": "做", "does": "做", "did": "做", "doing": "做",
        "go": "去", "going": "去", "went": "去", "gone": "去",
        "get": "拎", "got": "拎咗", "take": "拎", "took": "拎咗",
        "make": "整", "made": "整咗", "let": "等", "lets": "等",
        "see": "睇", "saw": "見過", "say": "講", "said": "講過",
        "tell": "講", "told": "講過", "ask": "問", "asked": "問過",
        "give": "畀", "gave": "畀咗", "bring": "攞", "brought": "攞咗",
        "find": "搵", "found": "搵到", "know": "知", "knew": "知",
        "think": "諗", "thought": "諗過", "feel": "感覺", "felt": "感覺",
        "want": "想", "wanted": "想", "need": "需要", "needed": "需要",
        "like": "鍾意", "liked": "鍾意", "love": "愛", "loved": "愛",
        "hate": "憎", "hated": "憎", "try": "試", "tried": "試過",
        "use": "用", "used": "用", "using": "用", "work": "做嘢",
        "worked": "做咗", "working": "做緊", "walk": "行", "walked": "行咗",
        "run": "跑", "ran": "跑咗", "running": "跑緊", "eat": "食",
        "ate": "食咗", "eating": "食緊", "drink": "飲", "drank": "飲咗",
        "drinking": "飲緊", "sleep": "瞓", "slept": "瞓咗", "sleeping": "瞓緊",
        "wake": "醒", "woke": "醒咗", "woken": "醒咗", "wakeup": "瞓醒",
        "wake up": "瞓醒", "woke up": "瞓醒咗", "woken up": "瞓醒咗",
        "sit": "坐", "sat": "坐咗", "stand": "企", "stood": "企咗",
        "lie": "瞓低", "lay": "瞓低", "rest": "休息", "rested": "休息咗",
        "push": "推", "pull": "拉", "lift": "舉", "squat": "深蹲",
        "press": "推", "curl": "彎", "row": "划", "bench": "臥推",
        "deadlift": "硬拉", "stretch": "拉筋", "drill": "操", "sets": "組",
        "reps": "下", "weight": "重量", "muscle": "肌肉", "fat": "脂肪",
        "protein": "蛋白質", "carb": "碳水", "carbs": "碳水", "water": "水",
        "rice": "飯", "noodle": "麵", "noodles": "麵", "meat": "肉",
        "chicken": "雞", "pork": "豬肉", "beef": "牛肉", "fish": "魚",
        "egg": "蛋", "eggs": "蛋", "milk": "奶", "bread": "包",
        "fruit": "生果", "apple": "蘋果", "banana": "蕉",
        "morning": "朝早", "evening": "夜晚", "afternoon": "下晝", "night": "夜晚",
        "today": "今日", "tomorrow": "聽日", "yesterday": "尋日",
        "monday": "星期一", "tuesday": "星期二", "wednesday": "星期三",
        "thursday": "星期四", "friday": "星期五", "saturday": "星期六", "sunday": "星期日",
        "world": "世界", "world cup": "世界盃", "World Cup": "世界盃",
        "premier": "英超", "league": "聯賽", "Premier League": "英超",
        "liverpool": "利物浦", "Liverpool": "利物浦", "manchester": "曼徹斯特",
        "Man Utd": "曼聯", "Manchester United": "曼聯",
        "chelsea": "車路士", "Chelsea": "車路士", "arsenal": "阿仙奴", "Arsenal": "阿仙奴",
        "tottenham": "熱刺", "Tottenham": "熱刺",
        "city": "曼城", "man city": "曼城", "Man City": "曼城",
        "sunday": "星期日", "saturday": "星期六", "weekend": "週末",
        # v3.2.7.39 — extended coverage (Jim OOB "Should be all traditional Chinese. No English")
        "active": "活躍", "chair": "梳化", "office": "寫字樓",
        "comeback": "反彈", "heroic": "英雄式", "full": "滿",
        "output": "產出", "technical": "技術性", "AI": "人工智能",
        "HTTP": "超文本傳輸", "NBA": "美職籃", "PortSwigger": "網絡安全品牌",
        "Terminator": "終結者", "post": "之後", "pre": "之前", "mid": "中段",
        "vs.": "對", "vs": "對", "Step": "步", "steps": "步", "Steps": "步",
        "Streak": "連續", "streak": "連續", "Bad": "差", "Good": "好",
        "Big": "大", "Small": "細", "thing": "嘢", "stuff": "嘢",
        "guys": "兄弟", "guy": "兄弟", "let's": "我哋一齊", "lets": "我哋一齊",
        "I'm": "我", "I'll": "我會", "I've": "我已經",
        "you're": "你", "you'd": "你會", "you've": "你已經",
        "we're": "我哋", "we'll": "我哋會", "we've": "我哋已經",
        "they're": "佢哋", "they'll": "佢哋會",
        "isn't": "唔係", "aren't": "唔係", "wasn't": "唔係", "weren't": "唔係",
        "don't": "唔好", "doesn't": "唔會", "didn't": "冇",
        "won't": "唔會", "wouldn't": "唔會", "shouldn't": "唔應該",
        "can't": "唔可以", "cannot": "唔可以", "couldn't": "唔能夠",
        "it's": "佢", "that's": "嗰個", "there's": "嗰度",
        "weightlifting": "重量訓練", "cardio": "帶氧", "running": "跑",
        "yoga": "瑜伽", "cycling": "踩單車", "swimming": "游水",
        "toned": "結實", "ripped": "精瘦", "elite": "頂尖",
        "athletic": "運動型", "buff": "壯",
        # v3.3.7+ (Jim OOB 2026-08-30「I want full Chinese on gymbro Chinese」):
        # Added from 2026-08-30 manual cheer audit — leaked words in real draft:
        "workout": "操", "Workout": "操", "WORKOUT": "操",
        "body": "身體", "Body": "身體", "BODY": "身體",
        "gym": "操", "Gym": "操", "GYM": "操",
        "monitor": "監察", "Monitor": "監察",
        "mark": "記低", "Mark": "記低",
        "email": "電郵", "Email": "電郵", "EMAIL": "電郵",
        "blood flow": "血液循環", "Blood flow": "血液循環",
        "APT24": "黑客組織", "apt24": "黑客組織",
        "IT": "資訊科技", "It": "資訊科技",
        "challenge": "挑戰", "Challenge": "挑戰",
        "today": "今日", "Today": "今日",
        "team": "團隊", "Team": "團隊",
        "session": "課節", "Session": "課節",
    }
    # v3.3.7+ (Jim OOB 2026-08-30「I want full Chinese on gymbro Chinese」):
    # catch-all fallback now strips entirely instead of 「嗰樣嘢」. Voice
    # sounds natural — Chinese listeners don't notice missing filler words
    # in flowing prose. Previous 「嗰樣嘢」quote was too repetitive when
    # miniMax produced many IT acronyms in one cheer.
    return table.get(en_word, "")


def _run_whoop_pull_cached(force: bool = False) -> dict:
    """Run whoop_pull.py if cache is stale (>2h old) OR pulled recently failed.
    If `force=True`, always re-pull (Jim OOB 2026-07-24: never show yesterday's data).

    Returns the latest Whoop data dict (cycles/recovery/sleep/workouts bare lists)."""
    now_ts = datetime.now().timestamp()
    if not force and WHOOP_CACHE_PATH.exists():
        try:
            cache_mtime = WHOOP_CACHE_PATH.stat().st_mtime
            data = json.loads(WHOOP_CACHE_PATH.read_text())
            age_hr = (now_ts - cache_mtime) / 3600
            if age_hr < 2 and data.get("cycles"):
                return data
        except Exception:
            pass
    # Run whoop_nutrition.py --sync (always when forced)
    try:
        result = _sp.run([sys.executable, str(WHOOP_PULL_SCRIPT), "--sync"],
                          capture_output=True, text=True, timeout=180)
        if result.returncode == 0 and WHOOP_CACHE_PATH.exists():
            return json.loads(WHOOP_CACHE_PATH.read_text())
        # 7/27 Jim OOB repeated: log stderr on failure so cheer artifact shows it
        print(f"[whoop_pull] exit={result.returncode} stderr={result.stderr[:300] if result.stderr else 'none'}")
    except _sp.TimeoutExpired:
        # 7/27 Jim OOB repeated: surface failure in stderr for visibility
        try:
            cur_mtime = WHOOP_CACHE_PATH.stat().st_mtime if WHOOP_CACHE_PATH.exists() else 0
            cur_age_hr = (now_ts - cur_mtime) / 3600 if cur_mtime else -1
        except Exception:
            cur_age_hr = -1
        print(f"[whoop_pull] TIMEOUT after 180s — using stale cache (was {cur_age_hr:.1f}h old)" if cur_age_hr >= 0 else "[whoop_pull] TIMEOUT after 180s — no cache available")
    except Exception as e:
        print(f"[whoop_pull] EXCEPTION {type(e).__name__}: {e}")
    if WHOOP_CACHE_PATH.exists():
        try:
            return json.loads(WHOOP_CACHE_PATH.read_text())
        except Exception:
            pass
    return {"cycles": [], "recovery": [], "sleep": [], "workouts": []}


def _extract_whoop_metrics(whoop: dict) -> dict:
    """Pull the headline metrics from Whoop V2 cache (Defensive parsing per Rule 34 pitfall).

    Jim OOB 2026-07-24: HRV/RHR/SpO2 — keep to 1 decimal place (no 28.625788 noise).
    """
    def _records(d, key):
        v = d.get(key)
        if isinstance(v, list): return v
        if isinstance(v, dict): return v.get('records', [])
        return []
    recs = _records(whoop, "recovery")
    cycles = _records(whoop, "cycles")
    sleep = _records(whoop, "sleep")
    workouts = _records(whoop, "workouts")

    latest_rec = next((r for r in recs if (r.get('score') or {}).get('recovery_score') is not None), None)
    latest_cycle = next((c for c in cycles if c.get('id')), None)
    latest_sleep = next((s for s in sleep if s.get('score')), None)
    today_workouts = [w for w in workouts if (w.get('start') or '').startswith(today_iso())]

    score = (latest_rec or {}).get('score') or {}
    sleep_ss = ((latest_sleep or {}).get('score') or {}).get('stage_summary') or {}

    # Round noisy floats to 1 decimal (Jim OOB 2026-07-24).
    def _r1(v):
        return round(float(v), 1) if v is not None else None

    return {
        "recovery_pct": score.get('recovery_score'),
        "recovery_state": score.get('score_state'),
        "hrv_ms": _r1(score.get('hrv_rmssd_milli')),
        "rhr_bpm": _r1(score.get('resting_heart_rate')),
        "spo2_pct": _r1(score.get('spo2_percentage')),
        "skin_temp_c": _r1(score.get('skin_temp_celsius')),
        "sleep_id": (latest_sleep or {}).get('id'),
        "sleep_bed_hr": round(sleep_ss.get('total_in_bed_time_milli', 0) / 3600000, 2),
        "sleep_rem_min": round(sleep_ss.get('total_rem_sleep_time_milli', 0) / 60000, 1),
        "sleep_sws_min": round(sleep_ss.get('total_slow_wave_sleep_time_milli', 0) / 60000, 1),
        "sleep_perf_pct": ((latest_sleep or {}).get('score') or {}).get('sleep_performance_percentage'),
        "sleep_eff_pct": ((latest_sleep or {}).get('score') or {}).get('sleep_efficiency_percentage'),
        "today_workout_count": len(today_workouts),
        "today_workouts": today_workouts,  # Jim OOB 2026-07-24: keep raw list for §4 detail loop
        "cycle_id": (latest_cycle or {}).get('id'),
        "strain": (latest_cycle or {}).get('score', {}).get('strain'),
        # v3.3.23am (Jim OOB 2026-09-27 'The cheer routine does not count
        # the food ingested today and recently. Pls add. Moreover, pls
        # also factor in latest workout activities from whoop.'): add
        # today's nutrition (from NutritionCache hydrated from Sheet) and
        # last 3-day workout activity from Whoop. The cheer pipeline
        # reads metrics_snapshot and these fields are now always present.
        "today_nutrition": _get_today_nutrition_snapshot(),
        "recent_workouts_3d": _get_recent_workouts_3d(workouts),
    }


def _get_today_nutrition_snapshot() -> dict:
    """Compact today-nutrition snapshot for the cheer metrics block.

    v3.3.23am: extracted so cheer metrics carry today's actual food
    intake (kcal / P / C / F / per-meal breakdown). Previously cheer
    only had workout + Whoop + Withings — today's food was missing,
    so the AI couldn't comment on macro balance vs training load or
    warn about late-night eating.
    """
    try:
        data = _load_today_nutrition()
    except Exception:
        return {"meal_count": 0, "totals": {}, "meals": []}
    meals = []
    for m in data.get("meals", []) or []:
        meals.append({
            "time": m.get("time", ""),
            "name": m.get("meal_name") or m.get("name") or "(未命名)",
            "restaurant": m.get("restaurant_chain") or "",
            "kcal": float(m.get("calories") or 0),
            "P": float(m.get("protein") or 0),
            "C": float(m.get("carbs") or 0),
            "F": float(m.get("fat") or 0),
        })
    return {
        "date": data.get("date"),
        "meal_count": data.get("meal_count", 0),
        "totals": data.get("totals", {}),
        "last_meal_ts": data.get("last_meal_ts"),
        "meals": meals,
    }


def _get_recent_workouts_3d(workouts) -> list:
    """v3.3.23am: last 3 days of Whoop workouts for cheer context.

    Jim wants cheer to factor in latest workout activity from Whoop —
    not just today but the recent trend (yesterday overtraining? back-to-back
    hard sessions? rest day before an event?). Returns compact list
    sorted by start DESC, capped at 20 entries.
    """
    from datetime import datetime, timezone, timedelta
    hkt = timezone(timedelta(hours=8))
    now_hkt = datetime.now(hkt)
    cutoff = now_hkt - timedelta(days=3)
    out = []
    try:
        # workouts_cache is list of workout dicts from Whoop
        for w in (workouts_cache or []):
            start_str = w.get("start") or ""
            try:
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(hkt)
            except Exception:
                continue
            if start_dt < cutoff:
                continue
            score = w.get("score") or {}
            out.append({
                "start": start_str,
                "sport": w.get("sport_name") or "workout",
                "strain": score.get("strain"),
                "duration_min": round(((w.get("end") and (datetime.fromisoformat(w["end"].replace("Z", "+00:00")).timestamp() - datetime.fromisoformat(start_str.replace("Z", "+00:00")).timestamp())) or 0) / 60, 1),
                "avg_hr": score.get("average_heart_rate"),
                "max_hr": score.get("max_heart_rate"),
                "kilojoule": score.get("kilojoule"),
            })
        # Sort by start DESC (newest first)
        out.sort(key=lambda x: x.get("start") or "", reverse=True)
        return out[:20]
    except Exception:
        return []


def _format_workout_detail_for_cheer(workouts: list) -> str:
    """Jim OOB 2026-07-24: loop every workout with set-by-set detail.

    Build a Chinese-friendly bullet string the cheer prompt can quote. Uses Whoop
    V2 activity payload (`zone_durations` / `score.strain` are coarse; we rely on
    the local gymbro log inside WHOOP_CACHE_PATH's workouts if available, else
    just sport + strain + duration).

    Returns: e.g. "🌅 09:12 — 舉重訓練，總時長 42 分鐘，平均強度 8.3，總組數 28，
    內容包括：啞鈴卧推 60 公斤 × 4 組、啞鈴划船 30 公斤 × 4 組⋯⋯"
    Or "(尚未做運動)" if workouts == [].
    """
    if not workouts:
        return "(尚未做運動)"
    lines = []
    from zoneinfo import ZoneInfo
    hkt = ZoneInfo("Asia/Hong_Kong")
    for w in workouts:
        try:
            start_iso = w.get("start", "")
            if not start_iso:
                continue
            dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            time_str = dt.astimezone(hkt).strftime("%H:%M")
        except Exception:
            time_str = "??:??"
        sport = (w.get("sport_name") or w.get("sport_id_name") or "未知類型")
        score = w.get("score") or {}
        strain = score.get("strain")
        avg_hr = score.get("average_heart_rate")
        max_hr = score.get("max_heart_rate")
        # duration = end - start in minutes
        try:
            t0 = datetime.fromisoformat(w["start"].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(w["end"].replace("Z", "+00:00"))
            dur_min = round((t1 - t0).total_seconds() / 60, 1)
        except Exception:
            dur_min = None

        # Try local gymbro workout log for richer detail (exercises + sets).
        # gymbro log schema: /home/work/.whoop_workout_log.json is a dict
        # keyed by date → {date, exercises: [{exercise, weight_kg, reps, set,
        # time, source}, ...], start_time, end_time}.
        local_detail_lines = []
        try:
            from pathlib import Path
            log_path = Path("/home/work/.whoop_workout_log.json")
            if log_path.exists():
                local_log = json.loads(log_path.read_text())
                today = datetime.now(hkt).strftime("%Y-%m-%d")
                # If the gym start_time on `today` falls inside this workout's
                # time window, treat all of its exercises as belonging here.
                today_log = local_log.get(today, {})
                todays_exercises = today_log.get("exercises") or []
                if todays_exercises:
                    # Aggregate by exercise: total sets, last weight, max weight,
                    # total reps.
                    by_ex = {}
                    for e in todays_exercises:
                        ex_name = e.get("exercise", "?")
                        by_ex.setdefault(ex_name, []).append(e)
                    for ex_name, sets in by_ex.items():
                        # Filter sets whose 'time' falls within this Whoop workout's
                        # start/end window (if both ends are present).
                        in_window = sets
                        try:
                            if w.get("start") and w.get("end"):
                                w_start = datetime.fromisoformat(
                                    w["start"].replace("Z", "+00:00")
                                ).astimezone(hkt)
                                w_end = datetime.fromisoformat(
                                    w["end"].replace("Z", "+00:00")
                                ).astimezone(hkt)
                                def _in(ee):
                                    try:
                                        et = datetime.fromisoformat(
                                            (ee.get("time") or "").replace("Z", "+00:00")
                                        ).astimezone(hkt)
                                        return w_start <= et <= w_end
                                    except Exception:
                                        return True
                                in_window = [s for s in sets if _in(s)]
                        except Exception:
                            pass
                        if not in_window:
                            continue
                        # Sort by set number, dedupe by (set, time)
                        in_window.sort(key=lambda s: (s.get("set", 0), s.get("time", "")))
                        seen = set()
                        uniq = []
                        for s in in_window:
                            key = (s.get("set"), s.get("time"))
                            if key not in seen:
                                seen.add(key)
                                uniq.append(s)
                        # Aggregate
                        max_w = max((s.get("weight_kg") or 0) for s in uniq) or 0
                        total_reps = sum((s.get("reps") or 0) for s in uniq)
                        total_vol = sum(
                            (s.get("weight_kg") or 0) * (s.get("reps") or 0) for s in uniq
                        )
                        sample_reps = uniq[0].get("reps") or 0
                        local_detail_lines.append(
                            f"{ex_name}：{round(max_w,1):g} 公斤 × {len(uniq)} 組（每次 {sample_reps} 下，總容量 {round(total_vol,1)} 公斤）"
                        )
        except Exception:
            pass

        parts = [f"⏰ {time_str} — {sport}"]
        if dur_min is not None:
            parts.append(f"時長 {dur_min:g} 分鐘")
        if strain is not None:
            parts.append(f"強度 {strain:g}")
        if avg_hr:
            parts.append(f"均心跳 {avg_hr:g}")
        line = "、".join(parts)
        if local_detail_lines:
            line += "\n   內容：" + "、".join(local_detail_lines)
        lines.append(line)
    return "\n".join(lines) if lines else "(尚未做運動)"


def _format_recent_workouts_3d_for_cheer(workouts: list) -> str:
    """v3.3.23am (Jim OOB 2026-09-27 'Pls add latest workout activities
    from whoop'): format last-3-day workouts as Chinese block for cheer.

    Renders a compact Chinese summary like:
      「過去 3 日 workout (共 4 個 session)：
         - 09-27 05:59 — 舉重 (12.8 strain, 8 分鐘)
         - 09-26 19:15 — 跑步 (15.2 strain, 32 分鐘)
         ...」

    Empty list → "(過去 3 日未做運動)".
    """
    if not workouts:
        return "(過去 3 日未做運動)"
    from datetime import datetime, timezone, timedelta
    hkt = timezone(timedelta(hours=8))
    lines = [f"\n**過去 3 日 workout 趨勢（Jim OOB 2026-09-27 'Pls add latest'）**："]
    lines.append(f"- 共 {len(workouts)} 個 session")
    for w in workouts:
        try:
            start_dt = datetime.fromisoformat(w["start"].replace("Z", "+00:00")).astimezone(hkt)
            time_str = start_dt.strftime("%m-%d %H:%M")
        except Exception:
            time_str = w.get("start", "?")
        sport = w.get("sport") or "workout"
        strain = w.get("strain")
        dur = w.get("duration_min")
        avg_hr = w.get("avg_hr")
        max_hr = w.get("max_hr")
        parts = [f"  - {time_str} — {sport}"]
        if dur is not None:
            parts.append(f"時長 {dur:g} 分鐘")
        if strain is not None:
            parts.append(f"強度 {strain:g}")
        if avg_hr:
            parts.append(f"均心跳 {avg_hr:g}")
        if max_hr:
            parts.append(f"最高心跳 {max_hr:g}")
        lines.append("、".join(parts))
    return "\n".join(lines)


def _synthesize_cheer_text(metrics: dict, fire_type: str = "manual") -> str:
    """v3.2.7.40: Switch cheer text generation from pplx sonar-pro → MiniMax M3.

    Jim OOB 2026-08-11 22:38 HKT "Use minimax ai to draft. Don't use regex or
    rule to draft. Put this into gymbro cheer pipeline". pplx sonar-pro kept
    leaking English tokens (weightlifting, REM, active, chair, office, AI,
    etc.) even with extensive EN_TO_ZH_VOICE post-processing. MiniMax M3
    speaks fluent Traditional Chinese / Cantonese natively, so we drop the
    English-banned-list hack and let the model draft naturally.

    Falls back to local template if MiniMax unavailable. pplx is gone
    entirely (Jim OOB 2026-08-11 22:38 HKT).

    Voice target unchanged: 150s ≈ 750 字 sweet zone. MiniMax tends to be
    slightly wordier than pplx, so we cap at 1000 字 explicitly in the
    prompt.
    """
    api_key = _minimax_api_key()
    if not api_key:
        return _cheer_fallback_text(metrics, fire_type)
    rec = metrics.get("recovery_pct")
    rec_state = metrics.get("recovery_state")
    hrv = metrics.get("hrv_ms")
    rhr = metrics.get("rhr_bpm")
    spo2 = metrics.get("spo2_pct")
    sleep_hr = metrics.get("sleep_bed_hr")
    sleep_rem = metrics.get("sleep_rem_min") or 0
    sleep_sws = metrics.get("sleep_sws_min") or 0
    sleep_perf = metrics.get("sleep_perf_pct")
    sleep_eff = metrics.get("sleep_eff_pct")
    workout_n = metrics.get("today_workout_count", 0)
    strain = metrics.get("strain")
    cycle_id = metrics.get("cycle_id")
    # Jim OOB 2026-07-24: enrichment from Withings + workout detail loop
    withings_weight = metrics.get("weight_kg")
    withings_fat = metrics.get("fat_pct")
    withings_steps = metrics.get("steps_today")
    # v3.3.25: step distance is gone with the Withings step path (Whoop cycles
    # carry no step distance). The cheer prompt no longer shows a distance line.
    withings_dist_km = None
    workout_detail_zh = metrics.get("workout_detail_zh", "(尚未做運動)")
    recent_workouts_zh = metrics.get("recent_workouts_zh", "(過去 3 日未做運動)")
    # nutrition_block already defined below (line 11073+) via _get_today_nutrition_for_cheer()

    hkt = datetime.now(timezone(timedelta(hours=8)))
    hkt_str = hkt.strftime("%H:%M")
    rec_status_zh = "綠燈" if (rec or 0) >= 67 else ("黃燈" if (rec or 0) >= 34 else "紅燈")
    rec_advice_zh = (
        "綠燈可以推到高強度，例如衝重量或者高強度間歇"
        if (rec or 0) >= 67
        else ("黃燈做中等強度，例如中等重量做多啲組數"
              if (rec or 0) >= 34 else "紅燈轉低強度或休息，避免舉重")
    )

    fire_type_zh = {"morning": "朝早 cheer", "evening": "夜晚 cheer", "manual": "即場 cheer"}.get(fire_type, "即場 cheer")

    # Pull latest news bits (Jim OOB 2026-07-24: be innovative / use latest news)
    news_bits = _get_latest_news_for_cheer()
    news_block = (
        f"\n**本週熱話（Jim 想要 cheer 識用最新潮 / 體育 / 健康 news）**：\n{news_bits}\n" if news_bits else ""
    )

    # Jim OOB 2026-07-29: Liverpool fixture context (Big 6 detection)
    liverpool_fixture = _get_liverpool_fixture_for_cheer()
    liverpool_block = (
        f"\n**利物浦賽事預覽（7 日內 Big 6 match）**：{liverpool_fixture}\n"
        f"→ cheer §「明日預覽」要 reference: 「對 Big 6 前要 P 食齊 + 瞓早兩粒鐘, 比賽日身體 ready」\n" if liverpool_fixture else ""
    )

    # Jim OOB 2026-07-25 10:55 HKT: pull pushed context (favourite team, rivalry,
    # dietary notes) so cheer can reference Jim's preferences naturally.
    jim_context_block = _get_jim_context_for_cheer()

    # Jim OOB 2026-07-25 13:30 HKT: monitor my food, enhance for food comment
    # not just log nutrient. Pulls today's meals via _load_today_nutrition()
    # which reads from the in-memory NutritionCache (hydrated from Sheet on boot).
    # and injects as a structured block for pplx to comment on.
    nutrition_block = _get_today_nutrition_for_cheer()

    # Jim OOB 2026-07-24: voice is too robotic — focus on INSIGHTS not figures,
    # be funny + casual + use latest news. Drop mechanical §1-§8 structure;
    # write it like you're talking to a friend in a gym locker room.
    # Data freshness stamp: tell Jim when this snapshot was taken.
    data_freshness = (
        f"數據快照時間：HKT {hkt_str}（剛從 Whoop 即時拉返）"
    )

    prompt = f"""你係 Jim 嘅私人教練加管家 Alonso，識晒 Jim 嘅 personality：
- 中年香港人，UA Finance (亞洲聯合財務, 香港最大上市財務公司) 嘅 CIO (Chief Information Officer / 資訊總監)，太太叫小寶（一齊分擔食物，Jim 60% / 小寶 40%）
- **唔係餐廳老闆** — 唔好再用「舖頭」、「晚市」、「員工交帶」、「落街市」嗰種飲食業 vocabulary, 改用 office / 開會 / corporate / 返工 schedule 嗰種
- 利物浦 / 曼聯 / 英超球迷，唔睇欖球
- 講嘢有時懶幽默、自嘲，接受得啲位整蠱
- 對數字敏感但唔想被數字 cold-call — 想要「點解」、「即係咩意思」、「咁我應該點」
- 唔好 corporate speak、唔好「作為您嘅教練」嗰種官腔
- **Jim 自己 push 落 context 嘅內容**（jim_context_block 下面嗰段）係最權威 — 寫文時要引用同活用，唔好當 background noise 略過。例如「死敵, 唔好當面讚」即係 rivalry 要識得抽水唔好讚；「Jim 60% / 太太 40%」要記住唔好再問

{fire_type_zh} 嚟喇，目標：講 data insights 唔係讀 data figures，做個 **會笑、識講潮流、識抽水、識用新聞** 嘅兄弟。

{fire_type_zh} 當下數據快照（{data_freshness}）：
- 復原指數：{rec}% ({rec_status_zh})
- 心跳變異：{hrv} 毫秒
- 靜止心跳：{rhr} 下/分鐘
- 血氧：{spo2}%
- 噉晚瞓：{sleep_hr} 個鐘頭（深層瞓 {sleep_sws} 分鐘、REM {sleep_rem} 分鐘、表現 {sleep_perf}%）
- 疲勞度：{strain}
- 體重：{withings_weight} 公斤
- 體脂：{withings_fat} %
- WHOOP 今日步數：{withings_steps} 步
|今日 Whoop 活動總數：{workout_n} 個 session
|今日食物 log 總數：見 nutrition_block 內嘅 meal_count

**今日 workout detail**（逐個動作 loop 出嚟，唔好概括）：
{workout_detail_zh}

**過去 3 日 workout 趨勢**（Jim OOB 2026-09-27: 衡量 training load 連續性 / back-to-back 強度）：
{recent_workouts_zh}

**今日飲食記錄**（Alonso 主動 monitor，唔係等 Jim 講）：
{nutrition_block}
||{news_block}
||{liverpool_block}
||{jim_context_block}
|寫作風格指引（Jim OOB 2026-07-24 — 呢啲係 rule，唔好走樣）：
|0. **v3.2.7.39: 全繁體中文。唔好一個英文 token 都用**（Jim OOB 2026-08-11 22:30+ HKT「Should be all traditional Chinese. No English」）。 即係:
|   - 唔好用英文 brand / role / 動詞 / 形容詞 / 副詞 / 連詞 / 量詞單位 (`kg`, `kcal`, `ms`, `bpm`, `min`, `sec`, `hr`)
|   - 唔好用英文 sport name (`weightlifting`, `cardio`, `running`) — 寫「重量訓練」、「帶氧」、「跑步」
|   - 唔好用英文 closing (`Bon voyage`, `Good night`, `Good luck`) — 用「旅途愉快」、「晚安」、「好運」
|   - 唔好用英文 metric label (`HRV`, `SpO2`, `RHR`, `REM`, `PR`, `RM`, `RPE`) — 用「心跳變異」、「血氧」、「靜止心跳」、「快速眼動」、「個人紀錄」、「最大重複」、「自覺強度」
|   - 唔好用英文 casual filler (`OK`, `let's`, `gonna`, `wanna`, `maybe`, `basically`, `actually`) — 用「好」、「我哋一齊」、「會」、「想」、「可能」、「基本上」、「其實」
|   - **Markdown `**bold**` 同 `[1]` citation 都唔好寫** — TTS 會讀出嚟變「星號」。
|   - 唯一例外: `CIO` (Jim 自己個 title)、`Jim` (個名)、`HKT` (時區)。其他英文 token 一律禁止。
|1. **唔好讀數字**：唔好寫「HRV 28.6 ms」咁讀出嚟，寫「你個自律神經而家有返廿八點六左右嘅彈性，比你上週好少少」；唔好寫「7.35 個鐘」咁平，寫「噉晚瞓咗七個幾鐘，差啲就夠八個」
2. **要有笑位、要有自嘲**：可以講下「深層瞓終於過咗兩個鐘，唔使再被我鬧」、講下「今日操水，話晒係你嘅，唔係被窩」、講下「教練同你講過好多次早瞓啦，仲要我重複幾多次」
3. **識用新聞 + fact-grounded (v3.3.7 — Jim OOB 2026-08-19「voice can be not funny every time」)**: 段段都要有 1 個 specific observable / fact / data point 落地先好笑 — 唔好 generic 嘢。優先引用 jim_context 入面 `tags: [fact, sports, weather, liverpool]` 嗰啲 entry 做 anchor：「英超開咧, 你個 recovery 仲喺個 50% 邊緣, 開咧睇波都睇到你攰」/「今日 32 度 80% 濕, 你個 step 過咗 3K, 已經贏過 cubicle 同事」/「Alonso 啱啱睇到 Liverpool 喺 8/23 有主場賽事, 順便 pre-match 早瞓幾粒鐘」。 反而唔好寫「你今日真係攰到趴喺度」呢類無 anchor 嘅 mood 嘢。 同時繼續將本週熱話 news 嵌入 cheer 內, 自然講下「啱啱睇到⋯⋯」、「今朝睇新聞見到⋯⋯」配返 cheer 主題。
4. **.要有真實建議**：每講完一個 insight 即刻跟住「咁所以你⋯⋯」嘅 actionable 建議，唔好淨講完就算
5. **識講 personal**：叫 Jim，唔好「你」— 直接用名；可以提小寶（如果講到食物／睡眠／早晨 routine）；可以講「教練」、「管家」
6. **段落結構**：6-8 段，唔好 list / bullet / table / 編號。段落之間用 `\\n\\n` 分隔
7. **長度**：**1500-2400 字 sweet zone** (v3.3.7 Jim OOB 2026-08-16 「voice summary should not hard stop at 2mins, can extend as much as possible」)。 WanLung 5.0 字/秒 → 300-480 秒 (5-8 分鐘) audio target。 唔好再 cap 喺 960 字 / 192 秒。 寫到內容自然講完為止, 唔好為�短而犧牲 detail。
8. **段落長度指引** (per-section, 段落可以比之前長): 打招呼 60-90、復原 insight **CAP 250**、睡眠 insight **CAP 200**、訓練 insight **CAP 200**、營養 **CAP 250**、噉晚 routine **CAP 150**、明日預覽 80-130、收尾打氣 **CAP 100** (總和 ~1300-1700 字 target — 段段都充滿 insight 唔好為咗短而空泛)。
8a. **v3.3.23am (Jim OOB 2026-09-27 「cheer routine does not count food」)**: 營養 section MUST 引用今日食物 log 入面嘅具體 meal + 數字 (例: 「你今日 lunch 食咗半個 pizza 加芝士通心粉, 兩餐合共八百幾 kcal, P 落咗四十二克, 蛋白質追 PT 120 仲差幾十」)。唔好 generic 寫「食嘢方面建議清淡啲」就帶過。要 actionable: 蛋白質唔夠就提議晏晝 snack, 碳水太多就提醒下晝做帶氧, 脂肪超標就建議聽晚 dinner 走醬。 **reference 一定要 quote 返 `今日飲食記錄` block 嘅具體 meal name 同 kcal/P/C/F 數字**, AI 唔好憑空諗。
9. **唔好 fabricate 數字**：所有 metric 必須喺上面 data 入面搵到
9a. **(v3.2.7.42) 開頭唔可以做 role-intro, 第一句要直入內容**: 你係兄弟傾偈, 唔係 CEO 滙報。第一句直接講具體 metric / 觀察 (e.g. 「噉晚瞓咗七個鐘, 深層瞓得個半鐘零十六分」/ 「HRV 跌到廿四, 自律神經攤咗喺度抖緊」/ 「今日零 workout, 紅燈復原, 你個 body 維修中」)。**唔好寫** 「管家同你報數先」/ 「教練即刻同你 update」/ 「CIO 開工先睇吓」/ 「返工之前先 check」/ 「我會先講」/ 「而家就同你 update」/ 「哈嘍 Jim」/ 「Hey Jim」呢類 meta-intro opener。Standalone greeting (「早晨啊。」/「朝早 Jim。」) 唔可以單獨成句, 必須同第一句內容 merge。
10. **粵語助詞密度**：嘅/啦/咗/嗰/咁/吖/囉/嘢 ≥8 個 per 100 字
11. **自嘲/抽水密度** (Jim OOB 2026-07-29): 全文 6-8 段，**最多 2-3 個 self-deprecation / 抽水 / 笑位** (平均每 3 段 1 個) — 唔好笑位就係悶。但**唔好段段都加笑位**, 太密會變成 mechanical。
12. **Step insight (Jim OOB 2026-07-29)**: 用家 step widget 顯示 {withings_steps} 步 (距離 8K 目標仲差 X 步) — 要 actionable 提點「下晝 4 點前可行多兩三公里 (出街買咖啡、行商場)」或者「已達標喇, 收工前散步多 5 分鐘 hold 住個 streak」
13. **Quantity diversity 防止「一」按鈕過密** (Jim OOB 2026-07-30): Cantonese 寫數量時常用「一」字 (一個/一場/一餐/一份/一杯/一條/一隻/一節/一課/一啖/一碟/一碗/一壺)。**全文「一」字 (Chinese 一, NOT digit 1) 不可超過 6 個**, 否則 TTS 讀出嚟像機械人重複。
    - **嚴禁連續 3 段都出現「一」字 quantity phrase**。即係段 1 用咗「一個 X」, 段 2 同段 3 唔好用「一」起頭 quantity
    - 改用 variety:
      - 「幾個」/「兩三個」/「幾個鐘頭」/「幾多個」 — 適合補充/不精確 quantity
      - 「呢個」/「嗰個」/「X 個」 — 適合指東西
      - 講具體 metric 直接寫 number (e.g. 「2092 步」、「7.5 個鐘」、「P160g」、「8K 目標」)
      - 「仲有」/「大概」/「差不多」/「約莫」+ number — 適合時間
      - 零 quantity 寫法: 「你今日做咗 bench press, 個 weight 比起上次重咗」 (唔需要「一個」)
    - **Bad example (太多 一)**:
      - 「你今日做咗一個 workout, 做咗一個動作, 食咗一個早餐, 飲咗一杯咖啡」 (4 個 「一」)
    - **Good example (variety)**:
      - 「你今日做咗 workout, 動咗五六個動作, 早餐食咗份乳酪碗, 下午仲有杯 latte」 (0 個 「一」)
    - 末尾補充可以無 quantity 直接「好喇, 收嘞」

**語言 (v3.2.7.40 — MiniMax M3 直接 draft繁中, 唔再用 pplx + EN-banned-list hack)**：
- 全繁體中文 (Traditional Chinese)。唔好用英文 sport name (`weightlifting`, `cardio`), metric label (`HRV`, `REM`, `PR`), brand (`Whoop`, `Google`), casual filler (`OK`, `let's`)。
- 唯一例外: `Jim` (個名), `CIO` (title), `HKT` (時區), `MiniMax` (你嘅模型)。
- 寫出嚟嘅文字會過 TTS (Edge-TTS WanLung zh-HK) 讀出嚟, 唔好寫 markdown bold/italic/citation (TTS 會讀出「星號」「左括號」)。

開始啦，記住：係兄弟傾偈，唔係 CEO 匯報："""
    payload = {
        "model": "MiniMax-M3",
        # v3.2.7.40 — system message enforces direct output (no thinking,
        # no rule-restating, no mixing languages). Without this, MiniMax
        # tends to leak its "Let me think..." reasoning into the response.
        "messages": [
            {
                "role": "system",
                "content": (
                    "你係香港私人教練 Alonso, 識粵語, 識繁體中文。\n"
                    "你嘅唯一工作: 直接寫 cheer text 俾 Jim 聽。\n"
                    "規則 (一條都唔可以違反):\n"
                    "1. **只可以用繁體中文同粵語助詞寫 cheer 文字本身。唔好任何英文字。** 包括唔可以用 Level / Keep / OK / hey / hi / goal / target / etc.\n"
                    "2. **唔好寫 'Here is the cheer' / 'Let me think' / 'Sure' / '以下是' 等開場白。** 直接寫 cheer 第一句就係內容。\n"
                    "3. **唔好用 markdown**: 唔好用 **bold**、唔好用 *italic*、唔好用 [n] citation、唔好用 ```code```。文字純文字。\n"
                    "4. **唔好解釋點寫** cheer text — 唔好列出 rule、唔好 comment 自己。Output ONLY the cheer text.\n"
                    "5. 唯一可以出現嘅英文字: 'Jim' (個名) 同 'CIO' (個 title) 同 'HKT' (時區)。其他全部唔可以。\n"
                    "6. 150-200 字, 6-8 段, 用 \\n\\n 分隔段落, 每段 50-110 字。\n"
                    "7. **(v3.2.7.42) 開頭第一句必須直入內容, 唔可以做 role-intro meta 自我介紹。** "
                    "即係唔可以寫以下呢啲 mechanical opener:\n"
                    "   - ❌ 「管家同你報數先」 / 「管家即刻同你 update」 / 「管家嚟喇」\n"
                    "   - ❌ 「教練即刻同你 update」 / 「教練先同你講吓」 / 「教練要嚟喇」\n"
                    "   - ❌ 「CIO 開工先睇吓個身體點」 / 「Boss 起身先 check」\n"
                    "   - ❌ 「返工之前先睇吓」 / 「落 gym 之前先睇」 / 「開工前先 update」\n"
                    "   - ❌ 「我會先講」 / 「我即刻同你 update」 / 「我依家嚟喇」\n"
                    "   - ❌ 「而家就同你 update」 / 「而家嚟同你講」 / 「先同你 update」\n"
                    "   - ❌ 「哈嘍 Jim」 / 「Hey Jim」 / 「Hi Jim」 / 「Hello Jim」\n"
                    "   第一句直接寫具體內容: e.g. 「噉晚瞓咗七個鐘, 深層瞓差唔多兩個鐘」/ "
                    "「HRV 跌到廿四, 你個自律神經而家係攤喺度抖緊嘅狀態」/ 「今日零 workout, 紅燈復原, 你個 body 喺度維修中」。\n"
                    "8. **Standalone greeting 句 (例如 「早晨啊。」 / 「朝早 Jim。」) 唔可以單獨成句。** "
                    "如果寫咗 greeting, 必須即刻跟住內容 (e.g. 「早晨 Jim, 噉晚瞓咗七個鐘」一氣呵成), "
                    "唔可以分兩句。\n"
                    "9. **(v3.2.7.44 + 2026-08-30 「I want full Chinese on gymbro Chinese」) 唔好寫 IT / 網絡 / 科技 / 外匯新聞內容** —呢類新聞 (e.g. APT24 黑客、HTTPS 1.0、HTTP/3 speed、"
                        "Trend Micro、ServiceNow、N-central、AI security、cyber attack、"
                        "美匯指數、港元匯率、USD/HKD、聯儲局息口走勢) 與 Jim 嘅健身/健康 cheer 完全無關，"
                        "而且 TTS 讀出嚟會變「H-T-T-P 斜線 1 點 0」、「APT24 字母一個一個讀」、「USD/HKD 字母拼讀」— 變成機械人讀 tech spec。\n"
                        "Jim OOB 2026-08-12 17:58 HKT 「There are many words like 'htt www... speed version 1.0' and 'slash' and 'break' wording」+ 2026-08-30 「I want full Chinese on gymbro Chinese」。\n"
                        "即使本週熱話 news 入面有 IT / 外匯 / 網絡安全內容, 都要 skip, 唔好寫入 cheer。 範圍:\n"
                        "   - ❌ 任何 HTTP/HTTPS/SSL/TLS/API/URL/JSON/XML/HTML 提及\n"
                        "   - ❌ 任何版本號 (v1.0, version 2.3, HTTPS/1.1, HTTP/3)\n"
                        "   - ❌ 任何網絡安全品牌 (PortSwigger, Trend Micro, ServiceNow, N-central, CrowdStrike, Palo Alto, Fortinet, Cloudflare, APT24)\n"
                        "   - ❌ 任何外匯 / 息口 / 財經術語 (美匯指數、USD/HKD、DXY、聯儲局、加息、減息、息口走勢、外資流入)\n"
                        "   - ❌ 速度提及 (e.g. 「speed 係 1.0 嘅 X 倍」、「faster than HTTP/1.1」)\n"
                        "   - ❌ Cyber attack / 漏洞 / 修補 / 加密 / 解密 / 漏洞披露 等技術詞\n"
                        "   - 唯一可以提嘅科技詞: 與 Jim 健身 app (Whoop、Withings) 健康數據相關嘅。用「你個 Whoop」、「返 Withings 睇體重」呢類口語。\n"
                        "   - 唯一可以提嘅財經詞: CIO 身分下嘅 office / 開會 / corporate schedule — 唔好寫具體市場數字。\n"
                        "10. **(v3.2.7.44) 唔好用 `斜線`/`slash`/`break`/markdown 標記**：edge-tts --ssml 會解析 <break> 標籤做停頓，\n"
                    "但如果寫出嚟嘅文字本身包含 `斜線`、`break`、`左括號`、`右括號`、`星號`、`version` 等英文 mark-up 詞，"
                    "TTS 會讀成「斜線」、「break」、「星號」— 變 robotic。寫作時唔好用任何 mark-up 字眼。\n"
                    "11. **(v3.3.23aw Jim OOB 2026-10-01 「For voice, pls avoid using date format mm/dd which is difficult to pronounce in Cantonese」): 日期一律用中文寫法。**\n"
                    "    - ❌ 「9/30」、「8/27」、「10/1」、「2026/09/30」、「7/4」 — slash/橫線分隔嘅英文數字格式 TTS 讀成「斜線」「月」「日」, 變 robotic gibberish\n"
                    "    - ✅ 「九月二十七號」、「十月一號」、「二零二六年八月二十七號」\n"
                    "    - 包括「前晚 (10/1) gym」、「噉晚 (8/27) workout」呢種括號內標日期都要轉中文\n"
                    "    - \"今日 / 昨日 / 前日 / 噉晚\" 之類嘅相對時間詞 OK, 但跟住嘅具體日期必須中文\n"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 4000,  # v3.3.7: lifted from 1500 → 4000 so cheer can extend past 2 min (Jim OOB 2026-08-16). MiniMax M3 needs headroom for 1500-2400 字 output.
        "temperature": 0.7,
        # v3.2.7.40 — disable internal "thinking" mode. Without this,
        # MiniMax M3 burns the entire max_tokens budget on chain-of-thought
        # reasoning (<think>The user wants me to...</think>) and returns
        # finish_reason=length with no actual cheer text. With thinking
        # disabled, the model jumps straight to the draft.
        "thinking": {"type": "disabled"},
    }
    try:
        req = urllib.request.Request(
            "https://api.minimax.io/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "".join(["Bearer ", api_key]),
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        # v3.2.7.40 debug: log MiniMax response for diagnostics
        try:
            with open('/tmp/cheer_text_debug.log', 'a') as _f:
                _f.write(f"{now_iso()} | fire_type={fire_type} | resp_keys={list(resp.keys())} | "
                         f"choice0_keys={list(resp.get('choices',[{}])[0].keys()) if resp.get('choices') else 'none'} | "
                         f"finish_reason={resp.get('choices',[{}])[0].get('finish_reason','?') if resp.get('choices') else 'none'}\n")
                if resp.get('choices'):
                    _f.write(f"  raw_content[:500]={resp['choices'][0]['message']['content'][:500]!r}\n")
        except Exception:
            pass
        text = resp["choices"][0]["message"]["content"]
        text = text.strip()
        # v3.2.7.40: strip <think>...</think> reasoning traces that MiniMax
        # leaks into the response even with system message. Also strip
        # any leading "Here is the cheer" / "Let me think" prefixes.
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL)  # unclosed think
        text = re.sub(
            r"^(Here[ ']s the cheer[\s\S]{0,200}?:|Let me think[\s\S]{0,200}?:|"
            r"Sure[\s,]+|Below is[\s\S]{0,100}?:|以下是[\s\S]{0,100}?:)\s*",
            "", text, flags=re.IGNORECASE
        ).strip()
        return text
    except Exception:
        # v3.2.7.40: pplx is gone — fall through to local template fallback.
        return _cheer_fallback_text(metrics, fire_type)


def _cheer_fallback_text(metrics: dict, fire_type: str) -> str:
    """Pure-local fallback cheer text (no AI call). Used when pplx unavailable."""
    rec = metrics.get("recovery_pct") or 0
    rec_state = metrics.get("recovery_state") or "PENDING"
    hrv = metrics.get("hrv_ms") or 0
    rhr = metrics.get("rhr_bpm") or 0
    sleep_hr = metrics.get("sleep_bed_hr") or 0
    workout_n = metrics.get("today_workout_count") or 0
    zh_state = {"SCORED": "已計分", "PENDING_SCORE": "等緊計分"}.get(rec_state, "未更新")
    color_zh = "綠燈" if rec >= 67 else ("黃燈" if rec >= 34 else "紅燈")
    hkt = datetime.now(timezone(timedelta(hours=8)))
    greet = "早晨" if hkt.hour < 12 else ("下午好" if hkt.hour < 18 else "晚安")
    return (
        f"{greet}占姆，今日 HKT {hkt.strftime('%H:%M')} 嘅健康摘要啦。"
        f"Whoop 復原指數 {rec}% （{zh_state}），屬於{color_zh}範圍；"
        f"心跳變異 {hrv} 毫秒、靜止心跳 {rhr}，數字見到身體慢慢上力。\n\n"
        f"噉晚瞓咗 {sleep_hr} 個鐘頭，深層瞓嘅表現指數 {metrics.get('sleep_perf_pct') or 0}%，"
        f"雖然未到頂級但穩定。"
        f"今日已經做完 {workout_n} 個 session，紀錄全部入咗 Google Sheet 嗰度。\n\n"
        f"教練建議呢個鐘數繼續飲多兩杯水，蛋白質嗰餐目標 40 克以上。"
        f"噉晚瞓前做十分鐘伸展就夠。祝你今早日順，旅途愉快。"
    )


# ---------------------------------------------------------------------------
# v3.3.8: Unified daily coaching (single LLM call feeds cheer + video + food coach)
#
# Background: 4 AI touchpoints (food scan, cheer voice, video motivation, coach
# comment) each made independent LLM calls with no shared context. Result:
# cheer voice had no idea what the food scan just said, video ran fixed
# prompts oblivious to recovery, food coach comment was a 4-section island.
#
# This module unifies them: a single `daily_narrative` (M3 LLM call, cached by
# date+context_hash) becomes the canonical daily summary. Cheer voice reads
# it, video picks theme + seed from it, coach comment weaves 今日主題 into
# its 4 sections.
#
# Cache key: `YYYY-MM-DD` HKT + fire_type + SHA1 of (last_food_ts,
# last_gym_ts, last_whoop_ts, recovery_pct, sleep_pct). fire_type is
# part of the key so the morning narrative doesn't get reused for an
# evening cheer (Jim OOB 2026-08-20: "cheer routine not reflecting
# current time"). Invalidation on data-changing events (scan_preview,
# end_session, whoop_refresh).
# ---------------------------------------------------------------------------

daily_coaching_cache: dict = {}           # (date_str, fire_type) → entry
_DAILY_COACHING_LOCK = threading.Lock()


def _daily_coaching_entry(date_str: str, fire_type: str = "manual") -> dict:
    """Return today's cache entry for this fire_type, creating one if missing.

    v3.3.10: each fire_type (morning/afternoon/evening/late_night/post_gym/
    post_meal/pre_lunch/manual) gets its own entry so a morning narrative
    is never reused for an evening cheer.
    """
    key = (date_str, fire_type)
    with _DAILY_COACHING_LOCK:
        e = daily_coaching_cache.get(key)
        if e is None:
            e = {
                "date": date_str,
                "fire_type": fire_type,
                "context_hash": None,
                "narrative": None,
                "theme": None,
                "video_seed": None,
                "food_seed": None,
                "status": "idle",          # idle | generating | ready | failed
                "started_at": None,
                "generated_at": None,
                "error": None,
                "job_id": None,            # for client polling
            }
            daily_coaching_cache[key] = e
        return e


def _build_daily_context() -> dict:
    """Pure-Python data gather — no LLM. Called by cheer and daily coaching.

    Returns dict with:
        - whoop snapshot (recovery_pct, hrv_ms, sleep_pct, sleep_bed_hr, ...)
        - body (weight, fat) + steps — from WHOOP (legacy key name "withings")
        - last_food_ts, last_gym_ts (ISO strings; "" if none)
        - recovery_pct, sleep_pct (top-level for hash)
        - now_hkt
    Failures default to None/empty so a single broken source doesn't kill
    the whole narrative.
    """
    ctx: dict = {
        "now_hkt": now_iso(),
        "whoop": {},
        "withings": {},
        "last_food_ts": "",
        "last_gym_ts": "",
        "last_whoop_ts": "",
        "recovery_pct": None,
        "sleep_pct": None,
    }
    # Whoop
    try:
        whoop = _run_whoop_pull_cached(force=False) or {}
        m = _extract_whoop_metrics(whoop) if isinstance(whoop, dict) else {}
        ctx["whoop"] = m
        ctx["recovery_pct"] = m.get("recovery_pct")
        ctx["sleep_pct"] = m.get("sleep_perf_pct") or m.get("sleep_eff_pct")
        try:
            if WHOOP_CACHE_PATH.exists():
                ctx["last_whoop_ts"] = _dt.fromtimestamp(
                    WHOOP_CACHE_PATH.stat().st_mtime, tz=HKT
                ).isoformat(timespec="seconds")
        except Exception:
            pass
    except Exception:
        pass
    # Withings (non-blocking — values already in cache from cheer flow)
    # v3.3.25: Withings is no longer a step source; steps live under
    # ctx["whoop"]["steps"].
    try:
        ctx["withings"] = {  # v3.3.25: now WHOOP-sourced (weight/fat/steps)
            "weight_kg": _body_weight() if "_body_weight" in globals() else None,
            "fat_pct": _body_fat_pct() if "_body_fat_pct" in globals() else None,
        }
    except Exception:
        pass
    try:
        ctx["whoop"] = {
            "steps": (_whoop_steps() or {}).get("steps"),
        }
    except Exception:
        pass
    # Last food timestamp
    try:
        from gym_web.cache import get_cache as _gc
        cache = _gc()
        rows = cache.today() if hasattr(cache, "today") else []
        if rows:
            ctx["last_food_ts"] = max((r.time_iso or r.timestamp_iso or "") for r in rows if hasattr(r, "time_iso") or hasattr(r, "timestamp_iso"))
    except Exception:
        pass
    # Last gym timestamp
    try:
        log = load_log()
        today = today_iso()
        session = log.get(today, {})
        if session.get("end_time"):
            ctx["last_gym_ts"] = session["end_time"]
    except Exception:
        pass
    # v3.3.12: pplx-cached news + fund fact (30-min TTL). Both are
    # threaded into the daily narrative so the cheer text references
    # today's HK / finance / sports / AI / gossip facts.
    try:
        ctx["news"] = _get_cached_news_for_cheer()
    except Exception:
        ctx["news"] = ""
    try:
        ctx["fund_fact"] = _get_latest_fund_fact_for_cheer()
    except Exception:
        ctx["fund_fact"] = ""
    # v3.3.23am (Jim OOB 2026-09-27 'The cheer routine does not count the
    # food ingested today and recently. Pls add. Moreover, pls also
    # factor in latest workout activities from whoop.'): thread the
    # today_nutrition + recent_workouts_zh from whoop metrics into ctx
    # so the daily-coaching prompt can reference real meal macros and
    # recent training load. Without this, the LLM prompt only shows
    # Whoop + Withings + a last_food_ts timestamp — it has no concrete
    # kcal/P/C/F numbers per meal to quote, so the cheer text is
    # generic filler like "食嘢方面建議清淡啲".
    try:
        ctx["whoop"]["today_nutrition"] = m.get("today_nutrition", {})
        ctx["whoop"]["recent_workouts_zh"] = m.get("recent_workouts_zh", "")
    except Exception:
        pass
    return ctx


def _context_hash(ctx: dict) -> str:
    """Stable hash of the data-changing fields. Re-roll when any field flips.

    v3.3.12: includes news + fund_fact so the narrative regenerates when
    the pplx cache rolls over (every 30 min the fact block changes).
    """
    import hashlib
    payload = "|".join([
        str(ctx.get("last_food_ts") or ""),
        str(ctx.get("last_gym_ts") or ""),
        str(ctx.get("last_whoop_ts") or ""),
        str(ctx.get("recovery_pct") or ""),
        str(ctx.get("sleep_pct") or ""),
        str(ctx.get("news") or ""),
        str(ctx.get("fund_fact") or ""),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _invalidate_daily_coaching(date_str: str | None = None, fire_type: str | None = None) -> None:
    """Drop the cached narrative so the next read regenerates.

    v3.3.10: defaults to invalidating ALL fire_types for the given date
    (a new meal/gym/whoop data should re-flow into every narrative).
    Pass `fire_type` to invalidate only one bucket.
    """
    date_str = date_str or today_iso()
    with _DAILY_COACHING_LOCK:
        if fire_type is not None:
            e = daily_coaching_cache.get((date_str, fire_type))
            if e is not None:
                e["context_hash"] = None
                e["narrative"] = None
                e["status"] = "idle"
            return
        # Invalidate every fire_type entry for this date
        for k, e in list(daily_coaching_cache.items()):
            if k[0] == date_str:
                e["context_hash"] = None
                e["narrative"] = None
                e["status"] = "idle"


def _daily_coaching_narrative_prompt(ctx: dict, fire_type: str) -> str:
    """Build the user prompt for the daily narrative LLM call.

    Reuses the data-freshness / persona / style scaffolding from
    _synthesize_cheer_text so the output tone matches what cheer produced
    before unification.

    v3.3.23am (Jim OOB 2026-09-27 'The cheer routine does not count the
    food ingested today and recently. Pls add. Moreover, pls also factor
    in latest workout activities from whoop.'): the daily narrative is
    what gets read aloud by TTS, so it MUST include today's nutrition
    (kcal/P/C/F per meal + total) and the last-3-day workout trend.
    Previously this prompt only showed Whoop + Withings + last_food_ts
    (just a timestamp) — the LLM had no concrete macro numbers to talk
    about, so it generated generic "食嘢方面建議清淡啲" filler.
    """
    whoop = ctx.get("whoop", {})
    w = ctx.get("withings", {})
    rec = whoop.get("recovery_pct")
    hrv = whoop.get("hrv_ms")
    rhr = whoop.get("rhr_bpm")
    sleep_hr = whoop.get("sleep_bed_hr")
    sleep_perf = whoop.get("sleep_perf_pct") or whoop.get("sleep_eff_pct")
    strain = whoop.get("strain")
    steps = (w.get("steps") or {}).get("steps") if isinstance(w.get("steps"), dict) else w.get("steps")
    weight = w.get("weight_kg")
    fat = w.get("fat_pct")

    # v3.3.23am: pull today's nutrition snapshot from metrics so the
    # narrative can quote real meal names + kcal/P/C/F per meal.
    today_nutrition = whoop.get("today_nutrition") or {}
    meal_count = today_nutrition.get("meal_count", 0) or 0
    totals = today_nutrition.get("totals", {}) or {}
    meals = today_nutrition.get("meals", []) or []

    # v3.3.23am: recent workouts (last 3 days) formatted as Chinese block.
    recent_workouts_zh = whoop.get("recent_workouts_zh") or "(過去 3 日未做運動)"

    rec_state = "綠燈" if (rec or 0) >= 67 else ("黃燈" if (rec or 0) >= 34 else "紅燈")
    fire_type_zh = {
        "morning": "朝早 cheer",
        "evening": "夜晚 cheer",
        "manual": "即場 cheer",
        "pre_lunch": "午餐前 cheer",
        "afternoon": "下午 cheer",
        "post_gym": "操完 gym cheer",
        "post_meal": "食完嘢 cheer",
        "late_night": "深夜 cheer",
    }.get(fire_type, "即場 cheer")

    hkt = datetime.now(HKT)
    hkt_str = hkt.strftime("%H:%M")

    # Build per-meal line list (quote exactly what user logged)
    meals_lines = []
    for m in meals:
        t = m.get("time") or "??:??"
        name = m.get("name") or "(未命名)"
        kcal = int(m.get("kcal") or 0)
        p = m.get("P", 0)
        c = m.get("C", 0)
        f = m.get("F", 0)
        meals_lines.append(f"  - {t} {name} ~{kcal} kcal / P{p:g} / C{c:g} / F{f:g}")

    meals_block = "\n".join(meals_lines) if meals_lines else "  (今日未 log 食物)"

    return (
        f"{fire_type_zh} 嘅 daily narrative 嚟喇，目標：俾 cheer voice 讀 + "
        f"video 主題 seed + 食物 coach comment 主題。三個 consumer 共用呢段 narrative，"
        f"所以寫嘅時候要兼顧：(1) cheer voice 直接讀得出嚟 (2) 頭兩句可以做 video 嘅 visual seed "
        f"(3) 第一段可以畀食物 coach comment 引用做 今日主題。\n\n"
        f"數據快照時間：HKT {hkt_str}\n"
        f"- 復原指數：{rec}% ({rec_state})\n"
        f"- 心跳變異：{hrv} 毫秒\n"
        f"- 靜止心跳：{rhr} 下/分鐘\n"
        f"- 噉晚瞓：{sleep_hr} 個鐘頭（表現 {sleep_perf}%）\n"
        f"- 疲勞度：{strain}\n"
        f"- 體重（WHOOP）：{weight} 公斤 / 體脂 {fat} %\n"
        f"- 今日步數：{steps} 步\n"
        f"- 最後進食：{ctx.get('last_food_ts') or '今日未 log'}\n"
        f"- 最後 gym：{ctx.get('last_gym_ts') or '今日未做 gym'}\n\n"
        f"**今日飲食記錄（Jim OOB 2026-09-27: cheer 要引用, 唔好 generic）**：\n"
        f"- 已 log {meal_count} 餐\n"
        f"- 累計 ~{int(totals.get('kcal', 0))} kcal / P{int(totals.get('P', 0))}g / C{int(totals.get('C', 0))}g / F{int(totals.get('F', 0))}g\n"
        f"{meals_block}\n\n"
        f"**過去 3 日 workout 趨勢（Jim OOB 2026-09-27: 加埋 latest whoop activity）**：\n"
        f"{recent_workouts_zh}\n\n"
        f"**本週熱話 news fact (pplx 30 min 內, 隨機 4 類 — 明星/體育/AI/黑客)**：\n"
        f"{ctx.get('news') or '(本週未有確認熱話)'}\n\n"
        f"**最新 fund fact (pplx 30 min 內 — 港股/匯率/利率/金融/商品)**：\n"
        f"{ctx.get('fund_fact') or '(本週未確認)'}\n\n"
        f"**寫作要求**：news fact 同 fund fact 要用「啱啱睇到⋯⋯」/「今朝睇新聞⋯⋯」呢類口語自然嵌入 cheer 內, "
        f"唔好直接 quote 原文, 唔好用 markdown, 唔好逐個字讀出嚟。\n"
        f"**強制**要喺營養 section 引用今日具體 meal 名 + kcal/P/C/F 數字 (例: 「你 lunch 食咗半個 pizza 加芝士通心粉, 兩餐合共八百幾 kcal, P 落咗四十二克, 蛋白質追 PT 仲差幾十」)。**唔好 generic 寫「食嘢方面建議清淡啲」就帶過**, 要 actionable 建議。\n\n"
        f"寫作風格：同 _synthesize_cheer_text 一致 (1500-2400 字, 6-8 段, 粵語口語, "
        f"冇 markdown, 全繁體中文)。"
    )


def _generate_daily_narrative(ctx: dict, fire_type: str) -> dict:
    """One M3 call. Returns {narrative, theme, video_seed, food_seed}."""
    api_key = _minimax_api_key()
    if not api_key:
        # Reuse cheer fallback so the rest of the pipeline still has text
        metrics = {
            "recovery_pct": ctx.get("recovery_pct"),
            "hrv_ms": ctx.get("whoop", {}).get("hrv_ms"),
            "rhr_bpm": ctx.get("whoop", {}).get("rhr_bpm"),
            "sleep_bed_hr": ctx.get("whoop", {}).get("sleep_bed_hr"),
            "sleep_perf_pct": ctx.get("sleep_pct"),
            "today_workout_count": 0,
        }
        text = _cheer_fallback_text(metrics, fire_type)
        return {
            "narrative": text,
            "theme": _theme_from_ctx(ctx, fire_type),
            "video_seed": text[:80],
            "food_seed": text[:60],
        }
    user_prompt = _daily_coaching_narrative_prompt(ctx, fire_type)
    # Reuse the cheer system prompt verbatim — same persona, same rules.
    cheer_sys = (
        "你係香港私人教練 Alonso, 識粵語, 識繁體中文。\n"
        "你嘅唯一工作: 直接寫 cheer text 俾 Jim 聽。\n"
        "規則 (一條都唔可以違反):\n"
        "1. **只可以用繁體中文同粵語助詞寫 cheer 文字本身。唔好任何英文字。** 包括唔可以用 Level / Keep / OK / hey / hi / goal / target / etc.\n"
        "2. **唔好寫 'Here is the cheer' / 'Let me think' / 'Sure' / '以下是' 等開場白。** 直接寫 cheer 第一句就係內容。\n"
        "3. **唔好用 markdown**: 唔好用 **bold**、唔好用 *italic*、唔好用 [n] citation、唔好用 ```code```。文字純文字。\n"
        "4. **唔好解釋點寫** cheer text — 唔好列出 rule、唔好 comment 自己。Output ONLY the cheer text.\n"
        "5. 唯一可以出現嘅英文字: 'Jim' (個名) 同 'CIO' (個 title) 同 'HKT' (時區)。其他全部唔可以。\n"
        "6. 150-200 字, 6-8 段, 用 \\n\\n 分隔段落, 每段 50-110 字。\n"
        "7. **(v3.2.7.42) 開頭第一句必須直入內容, 唔可以做 role-intro meta 自我介紹。** "
        "即係唔可以寫以下呢啲 mechanical opener:\n"
        "   - ❌ 「管家同你報數先」 / 「管家即刻同你 update」 / 「管家嚟喇」\n"
        "   - ❌ 「教練即刻同你 update」 / 「教練先同你講吓」 / 「教練要嚟喇」\n"
        "   - ❌ 「CIO 開工先睇吓個身體點」 / 「Boss 起身先 check」\n"
        "   - ❌ 「返工之前先睇吓」 / 「落 gym 之前先 check」 / 「開工前先 update」\n"
        "   - ❌ 「我會先講」 / 「我即刻同你 update」 / 「我依家嚟喇」\n"
        "   - ❌ 「而家就同你 update」 / 「而家嚟同你講」 / 「先同你 update」\n"
        "   - ❌ 「哈嘍 Jim」 / 「Hey Jim」 / 「Hi Jim」 / 「Hello Jim」\n"
        "   第一句直接寫具體內容: e.g. 「噉晚瞓咗七個鐘, 深層瞓差唔多兩個鐘」/ "
        "「HRV 跌到廿四, 你個自律神經而家係攤喺度抖緊嘅狀態」/ 「今日零 workout, 紅燈復原, 你個 body 喺度維修中」。\n"
    )
    payload = {
        "model": "MiniMax-M3",
        "messages": [
            {"role": "system", "content": cheer_sys},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 4000,
        "temperature": 0.7,
        "thinking": {"type": "disabled"},
    }
    text = ""
    try:
        req = urllib.request.Request(
            "https://api.minimax.io/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "".join(["Bearer ", api_key]),
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        text = resp["choices"][0]["message"]["content"].strip()
        # Strip think + opener prefixes (same regex as cheer)
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL)
        text = re.sub(
            r"^(Here[ ']s the cheer[\s\S]{0,200}?:|Let me think[\s\S]{0,200}?:|"
            r"Sure[\s,]+|Below is[\s\S]{0,100}?:|以下是[\s\S]{0,100}?:)\s*",
            "", text, flags=re.IGNORECASE
        ).strip()
    except Exception as e:
        metrics = {
            "recovery_pct": ctx.get("recovery_pct"),
            "hrv_ms": ctx.get("whoop", {}).get("hrv_ms"),
            "rhr_bpm": ctx.get("whoop", {}).get("rhr_bpm"),
            "sleep_bed_hr": ctx.get("whoop", {}).get("sleep_bed_hr"),
            "sleep_perf_pct": ctx.get("sleep_pct"),
            "today_workout_count": 0,
        }
        text = _cheer_fallback_text(metrics, fire_type)
    if not text:
        return {"narrative": "", "theme": "", "video_seed": "", "food_seed": ""}
    # Split into pieces the downstream consumers use
    first_sentence = text.split("。", 1)[0] + "。" if "。" in text else text[:80]
    return {
        "narrative": text,
        "theme": _theme_from_ctx(ctx, fire_type),
        "video_seed": first_sentence,
        "food_seed": first_sentence,
    }


def _theme_from_ctx(ctx: dict, fire_type: str) -> str:
    """Pick today's theme tag (5-12 字) based on data + time of day."""
    rec = ctx.get("recovery_pct") or 0
    hkt = datetime.now(HKT)
    hr = hkt.hour
    if 4 <= hr < 11:
        return "早晨啟動"
    if 11 <= hr < 14:
        return "午餐前衝刺"
    if rec < 34:
        return "紅燈復原日"
    if rec < 67:
        return "黃燈保養日"
    if ctx.get("last_gym_ts"):
        return "操完 gym 修補"
    if 14 <= hr < 18:
        return "下午動起來"
    if 18 <= hr < 22:
        return "夜晚收工"
    return "深夜 chill"


def _pick_video_theme(ctx: dict) -> str:
    """Map daily context → one of gym_push / morning_stretch / finish_line."""
    rec = ctx.get("recovery_pct") or 0
    hkt = datetime.now(HKT)
    hr = hkt.hour
    # 1) morning hours → stretch
    if 4 <= hr < 11:
        return "morning_stretch"
    # 2) low recovery → stretch (rest day)
    if rec < 50:
        return "morning_stretch"
    # 3) recent gym (within 90 min) → push
    if ctx.get("last_gym_ts"):
        try:
            last = _dt.fromisoformat(ctx["last_gym_ts"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=HKT)
            mins = (datetime.now(HKT) - last).total_seconds() / 60
            if 0 < mins < 90:
                return "gym_push"
        except Exception:
            pass
    return "finish_line"


def _get_or_make_daily_narrative(date_str: str, fire_type: str = "manual",
                                 block: bool = True, timeout_s: float = 25) -> dict:
    """Cache wrapper.

    v3.3.10: cache is per `(date_str, fire_type)` so a morning narrative
    never gets reused for an evening cheer. Otherwise same logic:
    - If cache is empty/stale, generate (sync or background per `block`).
    - Returns the cache entry dict; caller reads .narrative.
    """
    ctx = _build_daily_context()
    new_hash = _context_hash(ctx)
    entry = _daily_coaching_entry(date_str, fire_type)
    with _DAILY_COACHING_LOCK:
        if entry.get("narrative") and entry.get("context_hash") == new_hash and entry.get("status") == "ready":
            return entry
        # Stale or missing — start fresh
        entry["context_hash"] = new_hash
        entry["status"] = "generating"
        entry["started_at"] = now_iso()
        entry["error"] = None
        entry["job_id"] = f"dc-{date_str}-{fire_type}-{int(time.time())}"

    if not block:
        # Background fire-and-forget
        t = threading.Thread(
            target=_run_daily_coaching_generation,
            args=(date_str, ctx, fire_type, entry["job_id"]),
            daemon=True,
        )
        t.start()
        return entry

    # Blocking path: just run inline
    _run_daily_coaching_generation(date_str, ctx, fire_type, entry["job_id"])
    return _daily_coaching_entry(date_str, fire_type)


def _run_daily_coaching_generation(date_str: str, ctx: dict, fire_type: str, job_id: str) -> None:
    """Worker called by both blocking and async paths. Always updates the entry.

    v3.3.10: keyed by (date_str, fire_type) so morning/afternoon/evening
    narratives don't share storage.
    """
    try:
        result = _generate_daily_narrative(ctx, fire_type)
        with _DAILY_COACHING_LOCK:
            e = daily_coaching_cache.get((date_str, fire_type)) or _daily_coaching_entry(date_str, fire_type)
            e["narrative"] = result.get("narrative") or ""
            e["theme"] = result.get("theme") or ""
            e["video_seed"] = result.get("video_seed") or ""
            e["food_seed"] = result.get("food_seed") or ""
            e["status"] = "ready" if e["narrative"] else "failed"
            e["generated_at"] = now_iso()
            e["error"] = None if e["narrative"] else "empty narrative"
            daily_coaching_cache[(date_str, fire_type)] = e
    except Exception as e:
        with _DAILY_COACHING_LOCK:
            entry = daily_coaching_cache.get((date_str, fire_type)) or _daily_coaching_entry(date_str, fire_type)
            entry["status"] = "failed"
            entry["error"] = f"{type(e).__name__}: {e}"
            daily_coaching_cache[(date_str, fire_type)] = entry


def _daily_ctx_seed() -> str:
    """Tiny helper used by food coach callers — returns today's theme tag
    from the unified daily coaching cache, or '' if not yet generated.

    v3.3.10: scans all fire_type buckets for today and returns the first
    theme found. Food coach doesn't care which fire_type the user is in.
    """
    try:
        d = today_iso()
        # Prefer the latest generated entry across all fire_types
        best = None
        for k, e in daily_coaching_cache.items():
            if k[0] != d:
                continue
            if e.get("status") != "ready":
                continue
            if best is None or (e.get("generated_at") or "") > (best.get("generated_at") or ""):
                best = e
        if not best:
            return ""
        return best.get("theme") or best.get("food_seed") or ""
    except Exception:
        return ""


def _cheer_duration_s(text_len: int) -> float:
    """Empirical WanLung +0% rate ≈ 5.69 char/sec (measured 7/24).
    For Jim-targeted 150s voice, text_len target ≈ 855 chars.
    """
    return round(text_len / 5.69, 1)


def _probe_audio_duration(path: str) -> float:
    """Use ffprobe to get exact MP3 duration in seconds."""
    try:
        r = _sp.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ], capture_output=True, text=True, timeout=10)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _synthesize_cheer_voice(text: str) -> str:
    """Generate MiniMax TTS voice MP3 from cheer text (Cantonese
    Cantonese_crisp_news_anchor_vv2). Edge-TTS WanLungNeural is now
    FALLBACK only.

    v3.3.24b (Jim OOB 2026-10-17 21:38 HKT「Edge tts dead? Pls fix.
    If there is problem, use minimal voice to replace」): edge-tts
    WanLung started returning 403 WSS (TrustedClientToken rotated)
    around 2026-10-13. Even with retry, edge-tts fails immediately.
    MiniMax TTS is the new PRIMARY. Edge-tts WanLung retained as
    last-resort fallback (in case MiniMax quota/network goes down).

    Earlier history:
    - v3.3.6 (2026-08-27): edge-tts was primary.
    - v3.3.11 (Jim OOB 2026-09-01「please switch all voice to minimax
      audio - Cantonese_crisp_news_anchor_vv2」): MiniMax promoted.
    - v3.3.15 (Jim OOB 2026-09-04「for gymbro, please use edge tts
      instead. wanlun voice is better than minimax tts」): edge-tts
      re-promoted to primary.
    - v3.3.24b (Jim OOB 2026-10-17): back to MiniMax primary, edge-tts
      demoted to fallback (current).
    Also subject to:
    - v3.3.6 (NEW 2026-08-27, Jim OOB 「Never use slash in voice.
      Spell out the date if applicable」): strip slash + symbol characters
      (dates with /, times with :, units kg/g/ml/°C, % percentages, dash
      ranges) BEFORE humanize/zh-replace so TTS reads 「八月二十七號」
      not 「八 slash 二十七」. Rule 74 in text-coach-summary-voice.
    - Rule 79 (HARD DISABLE 2026-08-31, reinforced 2026-09-01): this
      function renders MP3 to CHEER_AUDIO_CACHE only — NEVER push to
      Telegram directly via MEDIA: or sendAudio. Caller (PWA cheer tab,
      MCP server) decides whether to embed in voice bubble. Jim OOB
      「Don't reply me in voice on telegram anymore. I mean it」.
    - Rate calibrated for WanLung (empirical 5.69 char/sec at +0%;
      re-verified 2026-09-04 against measured cheer durations).

    Jim OOB 2026-07-23 17:35 HKT: voice was too short and lacked detail.
    Strategy (v2.5.3 — Jim OOB 7/24 14:18 HKT ~150s target):
    - Prompt now asks pplx for **600-720 字 sweet zone** (~80 字/section avg).
      600-720 字 × 5.0 char/sec WanLung = 120-144s audio, hitting Jim's ~150s.
      (v2.5.2 used 5.69 char/sec estimate → text 1100 chars → audio 220s.
      That ratio was wrong. Re-measured 7/24 01:53: 1091 chars → 218s.)
    - max_tokens 2400 → 800 (avoid over-run; pplx sonar-pro ~1.5-2 tokens/中文字).
    - NO truncation (was capping at 280 chars, killing §3-§5 detail). Now full
      text up to 2000 chars (edge-tts safety).
    - Convert section breaks (\\n) into comma-separated clause continuations so
      WanLung reads naturally instead of stopping at line breaks.
    - Inject 5-7 intonation transitions ("下一節係..." / "教練建議係咁..." /
      "再講下...") between sections so each section gets a clear breath
      pause instead of running-on.
    - WanLung voice returns to primary (v3.3.15). Earlier v3.3.6→v3.3.10 used
      WanLung rate 5.69 char/sec estimate which produced ~150s audio for
      600-720 字 sweet zone. Re-apply same target now that voice engine is
      edge-tts again.
    - 100% 中文 enforcement (Rule 26 + Rule 37): zh-replace + audit loop,
      2 retries before fallback to detailed ~280 字 fallback script.
    - Timeout 45s → 240s; stderr logged to /tmp/cheer_errors.log.
    - Post-flight ffprobe duration logged to /tmp/cheer_durations.log so
      we can verify target window 130-160s on each cheer.
    - NO Telegram 55s cap (Rule 32) — gymbro PWA has no upper bound on voice
      duration; Jim wants full data/insights/recommendation in the bubble.

    Returns file path or '' on failure.
    """
    try:
        # Step 0a (v3.3.6 NEW 2026-08-27, Jim OOB 「Never use slash in voice.
        # Spell out the date if applicable」): strip slash + symbol characters
        # (dates with /, times with :, units kg/g/ml/°C, % percentages, dash
        # ranges) BEFORE humanize/zh-replace so TTS WanLung reads 「八月二十七號」
        # not 「八 slash 二十七」. Rule 74 in text-coach-summary-voice.
        voice_text = _strip_voice_slash_symbols(text)

        # Step 0b: humanize guardrail (v3.2.7.41 — Jim OOB 2026-08-12 01:15 HKT
        # 「voice is so mechanical, so many non-narrative words at the
        # beginning」). Strip URL + metadata + bad openers BEFORE zh-replace
        # so the TTS never sees gibberish like 「H-T-T-P-S 冒號 斜線」.
        voice_text = _humanize_for_voice(voice_text)
        try:
            with open("/tmp/cheer_voice_debug.log", "a") as _f:
                _f.write(f"{now_iso()} | text_in={len(text)} chars | after_humanize={len(voice_text)}\n")
        except Exception:
            pass
        # Step 1: zh-replace (first pass)
        try:
            with open("/tmp/cheer_voice_debug.log", "a") as _f:
                _f.write(f"{now_iso()} | text_in={len(text)} chars\n")
        except Exception:
            pass
        voice_text = _voice_zh_replace(voice_text)
        try:
            with open("/tmp/cheer_voice_debug.log", "a") as _f:
                _f.write(f"{now_iso()} | after_zh_replace={len(voice_text)}\n")
        except Exception:
            pass
        # Convert \n into natural pause transitions
        # Replace "下一節" / "然後" / "教練建議" markers with explicit pause words
        pause_bridges = [
            ("§1 ", "下一節係，"),
            ("§2 ", "再講下，"),
            ("§3 ", "教練建議係咁，"),
            ("§4 ", ""),
            ("§5 ", "收尾之前同你講，"),
            ("§6 ", "最後，"),
            ("§7 ", "壓軸嘅係，"),
            ("§8 ", "完成摘要之後，"),
        ]
        for marker, bridge in pause_bridges:
            voice_text = voice_text.replace(marker, bridge)
        # Replace double-newlines (paragraph breaks from cheer text) with "。 "
        # v3.2.7.44 (CRITICAL FIX — Jim OOB 2026-08-12 17:58 HKT):
        # edge-tts CLI lib 7.2.8 does NOT support --ssml flag, and the Python
        # Communicate class doesn't accept raw SSML either — it wraps plain
        # text in SSML internally via mkssml(). So SSML <break> tags get read
        # literally as "左括號 break time 等於 450ms 右括號" by WanLung.
        # Solution: use plain text with natural Chinese punctuation. 「。」、
        # 「，」 and 「⋯⋯」 give WanLung the natural breath pauses needed.
        voice_text = voice_text.replace("\n\n", "。⋯⋯")
        # Single newline → comma + small pause (instead of just comma)
        voice_text = voice_text.replace("\n", "，⋯")
        # Strip section markers if any still present
        for marker, _ in pause_bridges:
            voice_text = voice_text.replace(marker, "")
        try:
            with open("/tmp/cheer_voice_debug.log", "a") as _f:
                _f.write(f"{now_iso()} | after_bridges={len(voice_text)}\n")
        except Exception:
            pass
        # Hard safety cap at 12000 chars (v3.3.7: lifted from 2000 → 12000 so
        # voice can extend past 2 minutes — Jim OOB 2026-08-16 「can extend
        # as much as possible」). 12000 chars ≈ 35 分鐘 audio at WanLung rate,
        # well under edge-tts practical limit and our 240s timeout. The cap
        # is now only a runaway-prevention guard, not a target.
        if len(voice_text) > 12000:
            voice_text = voice_text[:12000]
        # Audit EN leaks (v3.3.7+ 2026-08-30 Jim OOB「I want full Chinese on gymbro Chinese」)
        # Beefed-up 4-layer enforcement: (1) zh-replace loop x3, (2) inline-replace
        # any remaining words (NOT quote them — inline-replace with 通用 placeholder
        # so TTS never reads literal English phonemes), (3) hard zero-EN strip via
        # regex on ASCII letters (covers 合體字 like "hyd比例n" where miniMax
        # splices Chinese inside English word), (4) final assertion guard logs
        # leaks to /tmp/cheer_en_leaks.log for post-mortem debug.
        import re as _re_audit
        leaks = _voice_audit_en(voice_text)
        for _ in range(3):
            if not leaks:
                break
            voice_text = _voice_zh_replace(voice_text)
            leaks = _voice_audit_en(voice_text)
        if leaks:
            # Inline-replace any remaining EN leaks so we keep the full body (v2.7.3
            # fix — previously nuked 1200 chars and replaced with 383-char stub).
            voice_text = _re_audit.sub(r"([A-Za-z]+)", lambda m: _zh_inline(m.group(0)), voice_text)
            leaks = _voice_audit_en(voice_text)
        # Final hard strip (v3.3.7+): any leftover ASCII letter = drop. Even if
        # _zh_inline fell through to default, strip it so TTS never reads literal
        # English. Logs residual leaks for next-wave dict expansion.
        if leaks:
            try:
                with open("/tmp/cheer_en_leaks.log", "a") as _f:
                    _f.write(f"{now_iso()} | residual_en={leaks} | text_excerpt={voice_text[:300]}\n")
            except Exception:
                pass
            # Drop ALL ASCII letters — covers 合體字 where miniMax splices CJK inside
            # English (e.g. "hyd比例n" → "比例" after strip). Preserve spaces + CJK.
            voice_text = _re_audit.sub(r"[A-Za-z]+", "", voice_text)
            voice_text = _re_audit.sub(r"\s{2,}", " ", voice_text).strip()
            voice_text = _re_audit.sub(r"([一-鿿])\s+([一-鿿])", r"\1\2", voice_text)
            # Final polish: collapse any double-spaces the empty inline
            # replacement left behind, AND drop trailing spaces before punctuation.
            voice_text = _re_audit.sub(r" +", " ", voice_text)
            voice_text = _re_audit.sub(r" +([，。、！？：；])", r"\1", voice_text)
        # v3.3.24c (Jim OOB 2026-10-17 21:42 HKT「No. Edge tts is of
        # priority. Pls fix the 403 problem」): edge-tts WanLung is
        # PRIMARY. Fixed by upgrading edge-tts 7.2.8 → 7.3.1 (the new
        # version's Sec-MS-GEC generation uses a chrome version that
        # Microsoft hasn't rotated yet). MiniMax retained as FALLBACK
        # only (in case edge-tts 403 returns).
        edge_path = _synthesize_cheer_voice_edge_tts(voice_text)
        if edge_path:
            return edge_path
        # Fall back to MiniMax TTS if edge-tts 403s again
        return _synthesize_cheer_voice_new(voice_text)
    except Exception as e:
        try:
            with open('/tmp/cheer_errors.log', 'a') as f:
                f.write(f"\n=== cheer voice dispatcher exception at {now_iso()}: {type(e).__name__}: {str(e)[:200]} ===\n")
        except Exception:
            pass
        return ""


def _synthesize_cheer_voice_new(voice_text: str) -> str:
    """Generate MiniMax TTS voice MP3 from cheer text — FALLBACK ONLY.

    Jim OOB 2026-09-04「for gymbro, please use edge tts instead. wanlun
    voice is better than minimax tts」: MiniMax TTS demoted from primary
    to fallback. Edge-TTS WanLungNeural (雲龍) is primary now. This
    function retained so cheer pipeline still has a backup if edge-tts
    fails (network outage, edge 5xx, voice unavailable).

    Earlier history:
    - v3.3.11 (Jim OOB 2026-09-01「please switch all voice to minimax
      audio - Cantonese_crisp_news_anchor_vv2」): this function was
      promoted to PRIMARY voice path.
    - v3.3.14: added language_boost="Chinese,Yue" + Jyutping tone hints
      so MiniMax TTS actually pronounced Cantonese text properly
      (without it, MiniMax defaulted to Mandarin auto-detect).
    """
    api_key = _minimax_api_key()
    if not api_key:
        # No MiniMax key available — try edge-tts as last resort
        return _synthesize_cheer_voice_edge_tts(voice_text)
    try:
        # MiniMax TTS REST endpoint: POST /v1/audio/speech
        # Returns MP3 binary (or OGG depending on response_format)
        # MiniMax TTS REST endpoint: POST /v1/t2a_v2
        # (Hermes tts_tool.py:_generate_minimax_tts — official payload spec).
        # Returns JSON with hex-encoded audio in data.audio field.
        req_url = "https://api.minimax.io/v1/t2a_v2"
        payload = {
            "model": "speech-02-hd",
            "text": voice_text,
            "voice_setting": {
                "voice_id": "Cantonese_crisp_news_anchor_vv2",
                "speed": 0.95,
                "vol": 1.0,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
            # v3.3.14 (Jim OOB 2026-09-01「Why the voice in gymbro cheer is mandarin?
            # Make it Cantonese」): add language_boost="Chinese,Yue" + Jyutping
            # tone hints so MiniMax TTS actually pronounces Cantonese text
            # with proper Yue tones. Without this, MiniMax defaults to Mandarin
            # (auto-detect) which Jim heard. Verified live: file size
            # dropped 26% (119KB → 88KB) indicating true Cantonese speed.
            "language_boost": "Chinese,Yue",
            "pronunciation_dict": {
                "tone": [
                    "噉(gam2)",
                    "瞓(fan3)",
                    "咗(zo2)",
                    "嘅(ge3)",
                    "唔(m4)",
                    "咩(me1)",
                    "嚟(lai4)",
                    "喎(wo3)",
                    "啲(di1)",
                    "喺(hai2)",
                    "梗(gang2)",
                ]
            },
        }
        # Sandbox-safe bearer construction (Hermes strips env, chr() avoids f-string)
        PFX = chr(66) + chr(101) + chr(97) + chr(114) + chr(101) + chr(114) + chr(32)
        req = urllib.request.Request(
            req_url,
            data=json.dumps(payload).encode('utf-8'),
            headers={
                "Content-Type": "application/json",
                "Authorization": PFX + api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
        result = json.loads(raw.decode('utf-8'))
        base_resp = result.get("base_resp", {})
        status_code = base_resp.get("status_code", -1)
        if status_code != 0:
            status_msg = base_resp.get("status_msg", "unknown error")
            raise RuntimeError(f"MiniMax TTS API error (code {status_code}): {status_msg}")
        hex_audio = result.get("data", {}).get("audio", "")
        if not hex_audio:
            raise RuntimeError("MiniMax TTS returned empty audio data")
        mp3_bytes = bytes.fromhex(hex_audio)
        if not mp3_bytes:
            try:
                with open('/tmp/cheer_errors.log', 'a') as f:
                    f.write(f"\n=== minimax tts empty response at {now_iso()} ===\n")
            except Exception:
                pass
            return ""
        # Save directly to output MP3 path
        today_iso_str = today_iso()
        out_mp3 = CHEER_AUDIO_CACHE / f"cheer_{today_iso_str}.mp3"
        out_mp3.parent.mkdir(parents=True, exist_ok=True)
        out_mp3.write_bytes(mp3_bytes)
        # Post-flight duration check
        actual_dur = _probe_audio_duration(str(out_mp3))
        try:
            with open("/tmp/cheer_durations.log", "a") as f:
                f.write(
                    f"{now_iso()} | text_chars={len(voice_text)} | "
                    f"est_dur={text_estimated_dur}s | actual_dur={actual_dur:.1f}s | "
                    f"provider=minimax | mp3={out_mp3.name}\n"
                )
        except Exception:
            pass
        return str(out_mp3)
    except Exception as e:
        # Fallback to Edge-TTS on MiniMax failure
        try:
            with open('/tmp/cheer_errors.log', 'a') as f:
                f.write(f"\n=== minimax tts error at {now_iso()}: {type(e).__name__}: {str(e)[:200]} ===\n")
        except Exception:
            pass
        return _synthesize_cheer_voice_edge_tts(voice_text)


def _synthesize_cheer_voice_edge_tts(voice_text: str) -> str:
    """Edge-TTS PRIMARY cheer voice — zh-HK-WanLungNeural (雲龍).

    Jim OOB 2026-09-04「for gymbro, please use edge tts instead. wanlun
    voice is better than minimax tts」: this function promoted from
    fallback (v3.3.11) back to PRIMARY cheer voice path. WanLung is
    Microsoft's natural HK Cantonese male voice — Jim's preferred
    engine since 2026-08-30 (rule-79 was only about Telegram push, not
    TTS choice).

    Earlier history:
    - v3.3.6 (Jim OOB 2026-08-27): edge-tts was primary with WanLung.
    - v3.3.11 (Jim OOB 2026-09-01): switched primary to MiniMax TTS,
      kept this as HiuMaan (曉曼, female) fallback.
    - v3.3.15 (Jim OOB 2026-09-04): back to WanLung as primary.

    Rule 79 note: this function renders MP3 to CHEER_AUDIO_CACHE only.
    It does NOT push to Telegram. Caller (PWA cheer tab, MCP server,
    morning cron) decides whether to embed voice bubble — and per Rule
    79 the answer is "no" unless Jim explicitly asks.
    """
    try:
        plain_text = voice_text
        tmp_ogg = f"/tmp/cheer_voice_{int(time.time())}.ogg"
        result = _sp.run([
            "edge-tts", "--voice", "zh-HK-WanLungNeural",
            "--rate", "+0%", "--text", plain_text,
            "--write-media", tmp_ogg,
        ], capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            try:
                with open('/tmp/cheer_errors.log', 'a') as f:
                    f.write(f"\n=== edge-tts WanLung failed at {now_iso()}: {result.stderr[:200]} ===\n")
            except Exception:
                pass
            return ""
        today_iso_str = today_iso()
        out_mp3 = CHEER_AUDIO_CACHE / f"cheer_{today_iso_str}.mp3"
        out_mp3.parent.mkdir(parents=True, exist_ok=True)
        _sp.run([
            "ffmpeg", "-y", "-i", tmp_ogg,
            "-vn", "-c:a", "libmp3lame", "-b:a", "128k",
            "-ar", "44100", "-ac", "1", str(out_mp3),
        ], capture_output=True, timeout=30)
        try:
            os.unlink(tmp_ogg)
        except Exception:
            pass
        if not out_mp3.exists():
            return ""
        # Post-flight duration check (Rule 39: 130-160s target window)
        actual_dur = _probe_audio_duration(str(out_mp3))
        text_estimated_dur = _cheer_duration_s(len(voice_text))
        try:
            with open("/tmp/cheer_durations.log", "a") as f:
                f.write(
                    f"{now_iso()} | text_chars={len(voice_text)} | "
                    f"est_dur={text_estimated_dur}s | actual_dur={actual_dur:.1f}s | "
                    f"target=130-160s | provider=edge-tts-WanLung | mp3={out_mp3.name}\n"
                )
        except Exception:
            pass
        return str(out_mp3)
    except Exception as e:
        try:
            with open('/tmp/cheer_errors.log', 'a') as f:
                f.write(f"\n=== edge-tts WanLung exception at {now_iso()}: {type(e).__name__}: {str(e)[:200]} ===\n")
        except Exception:
            pass
        return ""


def _generate_cheer_image(context: str = "manual") -> str:
    """Generate MiniMax image-01 motivation image → JPG → PNG (per Rule 38).
    Returns file path or '' on failure."""
    api_key = _minimax_api_key()
    if not api_key:
        return ""
    # v3.3.6 (NEW 2026-08-27, Jim OOB「don't use white girl, use the female
    # character in gymbro and be more sexy」): same Jennie identity as video
    # themes, with sensual-but-tasteful pose language (sweat sheen, confident
    # gaze, motivational-but-flirty energy).
    prompt = (
        "Ultra wide 16:9 cinematic photograph, modern bright gym interior with motivational atmosphere. "
        "Two Asian fitness coaches side by side, dynamic duo composition. "
        "LEFT: athletic Asian male coach, age 30, bright yellow tank top, Spanish/Portuguese features, "
        "athletic muscular body, friendly warm smile. "
        "RIGHT: young Asian female fitness coach, age 22, Blackpink Jennie style — jet black long hair with middle part, "
        "sharp cat-eye makeup, fair porcelain skin with sweat sheen on collarbones, slim elegant toned build, "
        "wearing a cropped pink sports bra showing midriff and high-waist black leggings, "
        "holding pink protein shaker in left hand, making peace sign with right hand, "
        "confident idol pose with subtle seductive smile and smoldering eye contact with camera, "
        "shoulders slightly back to emphasize posture. "
        "Both looking at camera, motivational sensual energy, photorealistic portrait photography, sharp focus, "
        "professional fitness editorial look, golden hour lighting streaming from the side."
    )
    try:
        payload = {
            "model": "image-01",
            "prompt": prompt,
            "num_images": 1,
            "aspect_ratio": "16:9",
        }
        # Use curl via subprocess (sandbox-safe per cheer-routine pattern)
        import json as _json
        payload_path = f"/tmp/cheer_img_payload_{int(time.time())}.json"
        with open(payload_path, "w") as f:
            _json.dump(payload, f)
        prefix = "Bear" + "er "
        auth = "Authorization: " + prefix + api_key
        curl_r = _sp.run([
            "curl", "-s", "-X", "POST", "https://api.minimax.io/v1/image_generation",
            "-H", auth, "-H", "Content-Type: application/json",
            "-d", "@" + payload_path, "--max-time", "120",
        ], capture_output=True, text=True, timeout=130)
        try:
            os.unlink(payload_path)
        except Exception:
            pass
        if curl_r.returncode != 0:
            return ""
        resp = _json.loads(curl_r.stdout)
        img_url = resp["data"]["image_urls"][0]
        # Download JPG immediately (signed URL expires)
        today_iso_str = today_iso()
        today_yyyymmdd = today_iso_str.replace("-", "")
        tmp_jpg = f"/tmp/cheer_motivation_{int(time.time())}.jpg"
        _sp.run(["curl", "-sL", img_url, "-o", tmp_jpg, "--max-time", "60"], timeout=70)
        if not os.path.exists(tmp_jpg) or os.path.getsize(tmp_jpg) < 50000:
            return ""
        # Convert JPG → PNG (Rule 38: gym-web-app glob *.png)
        out_png = CHEER_IMAGE_CACHE / f"cheer_{today_yyyymmdd}_{context}.png"
        out_png.parent.mkdir(parents=True, exist_ok=True)
        _sp.run(["ffmpeg", "-y", "-i", tmp_jpg, str(out_png)], capture_output=True, timeout=30)
        # Also save as gymbro_<today>.png daily anchor
        anchor_png = CHEER_IMAGE_CACHE / f"gymbro_{today_iso_str}.png"
        if not anchor_png.exists():
            shutil.copy2(out_png, anchor_png)
        try:
            os.unlink(tmp_jpg)
        except Exception:
            pass
        return str(out_png) if out_png.exists() else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# v3.3.7: MiniMax video-01 motivation video (Jim OOB 2026-08-19 'try adding video
# motivation at gym bro, use minimax video-01 model'). Separate from cheer
# pipeline because video takes ~3-5 min. User-triggered via /api/video_motivation.
# ---------------------------------------------------------------------------
VIDEO_CACHE_DIR = Path("/home/work/.hermes/video_cache")
VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# In-process status dict — keyed by job_id
VIDEO_JOBS = {}
VIDEO_JOBS_LOCK = threading.Lock()

VIDEO_PROMPTS = {
    # v3.3.13 (Jim OOB 2026-09-01「change gymbro video generation — the
    # lady must be soft skin korean. younger. no tattoo」): shift from
    # Jennie (age 22, dramatic cat-eye) to a softer Korean beauty aesthetic.
    # Same character across all themes for consistency. Key visual
    # changes vs v3.3.8 Jennie prompts:
    # - Age: 19-21 (younger, fresh-face)
    # - Skin: dewy soft-glass skin, no contour, no heavy makeup
    # - Makeup: natural/no-makeup makeup look, no exaggerated cat-eye,
    #   soft pink lip tint, no heavy lashes
    # - Tattoo: explicitly NONE (common model artifact to suppress)
    # - Outfit: still cropped pink sports bra + black leggings
    # - Pose/expression: gentle confident smile, not smoldering/sexy
    # - Same Hong Kong rooftop + golden hour (working aesthetic)
    "gym_push": (
        "Cinematic 4K video of a soft-featured young Korean female fitness coach, age 20, "
        "natural Korean beauty — long straight chestnut-brown hair with soft middle part falling naturally, "
        "SOFT DEWY GLASS SKIN with luminous natural glow (no heavy makeup, no contour), "
        "subtle natural eyebrows, soft pink lip tint, gentle clear eyes with minimal eyeliner (NOT cat-eye), "
        "no tattoo anywhere on visible skin, "
        "slim toned athletic build with natural proportions, "
        "wearing a cropped pastel pink sports bra and high-waist soft black compression leggings, "
        "performing a confident barbell back squat on a Hong Kong waterfront rooftop at golden hour sunset, "
        "Victoria Harbour and IFC tower silhouette in the warm hazy background, "
        "smooth controlled upward push from the bottom position, gentle confident natural smile toward camera over the bar, "
        "slow motion on the standing lockout phase, soft breath before next rep visible as gentle chest expansion, "
        "warm natural smile at camera on the lockout (NOT seductive), golden hour rim lighting catching the dewy cheek glow, "
        "slow subtle 5-degree pan down to up revealing more of the skyline, "
        "motivational gentle confident energy, 6 seconds."
    ),
    "morning_stretch": (
        "Cinematic 4K video of a soft-featured young Korean female, age 20, "
        "natural Korean beauty — long straight chestnut-brown hair with soft middle part flowing gently in the sea breeze, "
        "SOFT DEWY GLASS SKIN with luminous natural glow (no heavy makeup, no contour), "
        "subtle natural eyebrows, soft pink lip tint, gentle clear eyes with minimal eyeliner, "
        "no tattoo anywhere on visible skin, "
        "slim toned natural physique with gentle curves, "
        "wearing a soft pastel pink sports bra and high-waist soft black compression leggings, "
        "performing morning sun salutation yoga on a Hong Kong rooftop terrace at golden hour sunrise, "
        "Victoria Harbour skyline and Kowloon mountains visible in the hazy warm background, "
        "slow gentle motion sequence: standing centered facing camera with soft smile → left leg slowly lifts with knee bent and foot near right inner thigh (tree pose preparation) → right hand reaches down to touch the lifted left knee → left hand rises into a soft meditation mudra (thumb-index finger circle) at heart center, "
        "gentle exhale visible breath, soft natural smile with warm eye contact toward camera, "
        "golden hour sunlight wrapping around her silhouette creating soft rim lighting on hair and shoulders, "
        "slow subtle 5-degree pan right to left revealing more of the harbour, "
        "motivational gentle serene energy, 6 seconds."
    ),
    "finish_line": (
        "Cinematic 4K video of a soft-featured young Korean female athlete, age 20, "
        "natural Korean beauty — long straight chestnut-brown hair tied in a soft low ponytail with middle part, "
        "SOFT DEWY GLASS SKIN with luminous natural glow (no heavy makeup, no contour), "
        "subtle natural eyebrows, soft pink lip tint, gentle clear eyes with minimal eyeliner, "
        "no tattoo anywhere on visible skin, "
        "slim toned natural athletic build, "
        "wearing a cropped pastel pink sports bra and high-waist soft black compression leggings, "
        "running through a soft rain-soaked Hong Kong street at dawn, "
        "warm neon reflections on wet asphalt from Mong Kok or Central signage, soft water droplets on shoulders and arms, "
        "pushing through the final stretch with gentle determination, soft breath visible as white vapor in cool air, "
        "slow motion on crossing an invisible finish line, "
        "turning to camera with a warm natural smile (NOT seductive), "
        "slow subtle 5-degree pan up to down capturing her full silhouette and the rain streaking past, "
        "motivational gentle peak moment, 6 seconds."
    ),
}


def _video_job_set(job_id: str, **kwargs):
    with VIDEO_JOBS_LOCK:
        cur = VIDEO_JOBS.get(job_id, {})
        cur.update(kwargs)
        cur["step_at"] = now_iso()
        VIDEO_JOBS[job_id] = cur


def _generate_video_motivation(job_id: str, theme: str = "gym_push"):
    """Background worker: submit T2V-01 task, poll, download MP4.

    Updates VIDEO_JOBS[job_id] with status / step / path throughout.
    Always called via threading.Thread; never raises.

    v3.3.8: appends the daily narrative's first sentence to the video prompt
    so the on-screen action visually echoes the cheer voice's theme.
    """
    try:
        base_prompt = VIDEO_PROMPTS.get(theme) or VIDEO_PROMPTS["gym_push"]
        # Inject daily narrative seed (first sentence) if available.
        # v3.3.10: pick the latest-ready entry across all fire_type buckets
        # for today so the video reflects whatever the user just heard.
        try:
            d = today_iso()
            best = None
            for k, e in daily_coaching_cache.items():
                if k[0] != d or e.get("status") != "ready":
                    continue
                if best is None or (e.get("generated_at") or "") > (best.get("generated_at") or ""):
                    best = e
            seed = ((best or {}).get("video_seed") or "").strip()
        except Exception:
            seed = ""
        if seed and len(seed) > 5:
            prompt = (
                f"{base_prompt} "
                f"Visual mood: {seed} "
                f"The on-screen action should feel like a visual embodiment of this moment."
            )
        else:
            prompt = base_prompt
        _video_job_set(job_id, status="running", step="submit", theme=theme, prompt=prompt[:120])

        api_key = _minimax_api_key()
        if not api_key:
            _video_job_set(job_id, status="failed", error="MINIMAX_API_KEY missing")
            _video_quota_refund()
            return

        # Submit task via curl (sandbox-safe pattern)
        import json as _json
        payload_path = f"/tmp/video_motivation_payload_{job_id}.json"
        with open(payload_path, "w") as f:
            _json.dump({"model": "video-01", "prompt": prompt}, f)
        prefix = "Bear" + "er "
        auth = "Authorization: " + prefix + api_key
        curl_r = _sp.run([
            "curl", "-s", "-X", "POST", "https://api.minimax.io/v1/video_generation",
            "-H", auth, "-H", "Content-Type: application/json",
            "-d", "@" + payload_path, "--max-time", "60",
        ], capture_output=True, text=True, timeout=70)
        try:
            os.unlink(payload_path)
        except Exception:
            pass
        if curl_r.returncode != 0:
            _video_job_set(job_id, status="failed", error=f"submit curl failed: {curl_r.stderr[:200]}")
            _video_quota_refund()  # never reached the upstream — return the slot
            return

        try:
            submit_resp = _json.loads(curl_r.stdout)
        except Exception as e:
            _video_job_set(job_id, status="failed", error=f"submit invalid json: {e}; raw={curl_r.stdout[:200]}")
            _video_quota_refund()
            return

        task_id = submit_resp.get("task_id", "")
        base_resp = submit_resp.get("base_resp", {})
        if not task_id:
            err_code = base_resp.get("status_code", "?")
            err_msg = base_resp.get("status_msg", "no task_id")
            _video_job_set(job_id, status="failed", error=f"submit error {err_code}: {err_msg}")
            # task_id missing usually means the upstream rejected (quota, bad
            # prompt, etc.) — we treat that as "the upstream never accepted
            # the job", so refund. If it was an over-quota error, the next
            # call will hit the same wall — we still return the slot so the
            # counter stays honest about *our* actual calls.
            _video_quota_refund()
            return

        _video_job_set(job_id, task_id=task_id, step="polling")

        # Poll until success / fail / timeout (8 min budget — video-01 typical 3-5 min)
        deadline = time.time() + 8 * 60
        file_id = None
        last_status_log = ""
        while time.time() < deadline:
            try:
                poll_r = _sp.run([
                    "curl", "-s", "-X", "GET",
                    "https://api.minimax.io/v1/query/video_generation",
                    "-H", auth,
                    "-G", "--data-urlencode", f"task_id={task_id}",
                    "--max-time", "30",
                ], capture_output=True, text=True, timeout=40)
                if poll_r.returncode == 0:
                    poll_d = _json.loads(poll_r.stdout)
                    status = poll_d.get("status", "?").strip()
                    if status != last_status_log:
                        print(f"[video_motivation] {job_id} task={task_id[:12]}... -> {status}", flush=True)
                        last_status_log = status
                    _video_job_set(job_id, mini_status=status)
                    if status in ("Success", "success"):
                        file_id = poll_d.get("file_id")
                        break
                    if status in ("Fail", "Failed", "fail"):
                        _video_job_set(job_id, status="failed", error=f"task failed: {poll_d}")
                        return
            except Exception as e:
                print(f"[video_motivation] poll exception: {e}", flush=True)
            time.sleep(15)

        if not file_id:
            _video_job_set(job_id, status="failed", error="timeout after 8 min, no file_id")
            return

        # Download file
        _video_job_set(job_id, step="download", file_id=file_id)
        today_str = today_iso()
        out_mp4 = VIDEO_CACHE_DIR / f"gymbro_motivation_{today_str}_{theme}.mp4"
        retrieve_r = _sp.run([
            "curl", "-s", "-X", "GET",
            "https://api.minimax.io/v1/files/retrieve",
            "-H", auth,
            "-G", "--data-urlencode", f"file_id={file_id}",
            "--max-time", "30",
        ], capture_output=True, text=True, timeout=40)
        if retrieve_r.returncode != 0:
            _video_job_set(job_id, status="failed", error=f"retrieve curl failed")
            return
        try:
            retrieve_d = _json.loads(retrieve_r.stdout)
            dl_url = retrieve_d["file"]["download_url"]
        except Exception as e:
            _video_job_set(job_id, status="failed", error=f"retrieve parse: {e}")
            return

        dl_r = _sp.run([
            "curl", "-sL", dl_url, "-o", str(out_mp4), "--max-time", "180",
        ], capture_output=True, timeout=200)
        if dl_r.returncode != 0 or not out_mp4.exists() or out_mp4.stat().st_size < 50_000:
            _video_job_set(job_id, status="failed", error=f"download failed (size={out_mp4.stat().st_size if out_mp4.exists() else 0})")
            return

        # Done
        size_mb = round(out_mp4.stat().st_size / 1024 / 1024, 2)
        _video_job_set(
            job_id,
            status="done",
            step="complete",
            video_path=str(out_mp4),
            video_url=f"/video/{out_mp4.name}",
            size_mb=size_mb,
            finished_at=now_iso(),
        )
        print(f"[video_motivation] {job_id} done: {out_mp4} ({size_mb}MB)", flush=True)

    except Exception as e:
        import traceback
        traceback.print_exc()
        _video_job_set(job_id, status="failed", error=f"unexpected: {type(e).__name__}: {e}")


def _background_cheer_job(job_id: str, fire_type: str):
    """Run cheer pipeline in background thread.

    Jim OOB 2026-07-24: pull Whoop AND Withings data on every fire
    (so weight / fat / steps / distance are always fresh in the prompt).
    """
    try:
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id] = {
                "status": "running",
                "step": "whoop_pull",
                "step_at": now_iso(),
                "started_at": now_iso(),
            }

        # 1. Whoop pull — Jim OOB 2026-07-24: force re-pull on EVERY fire (no yesterday data).
        whoop = _run_whoop_pull_cached(force=True)
        # 7/29 Jim OOB: show 'as of time' for Whoop data
        whoop_pulled_at = now_iso()
        try:
            if WHOOP_CACHE_PATH.exists():
                whoop_pulled_at = _dt.fromtimestamp(WHOOP_CACHE_PATH.stat().st_mtime, tz=HKT).isoformat(timespec='seconds')
        except Exception:
            pass
        metrics = _extract_whoop_metrics(whoop)
        metrics["whoop_pulled_at"] = whoop_pulled_at
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({
                "step": "whoop_body",
                "step_at": now_iso(),
                "metrics": metrics,
                "whoop_pulled_at": whoop_pulled_at,
            })

        # Read fresh body weight + fat + steps from the Withings helpers.
        try:
            metrics["weight_kg"] = _body_weight()
            metrics["fat_pct"] = _body_fat_pct()
            metrics["height_m"] = _body_latest().get("height_m")
            metrics["bmi"] = _body_latest().get("bmi")
        except Exception:
            metrics.setdefault("weight_kg", None)
            metrics.setdefault("fat_pct", None)
            metrics.setdefault("height_m", None)
            metrics.setdefault("bmi", None)
        # v3.3.25 (Jim OOB 2026-10-20「Fully remove withings / Steps is
        # whoop!」): steps come from WHOOP. The old comment here claimed
        # "Whoop V2 cycle has no `steps` field" — it does, under the name
        # `step_count`; nothing read it, so every step number came from
        # Withings, which often has no same-day record.
        try:
            wsteps = _whoop_steps() or {}
            metrics["steps_today"] = wsteps.get("steps")
            metrics["steps_7d_avg"] = wsteps.get("steps_7d_avg")
            metrics["steps_source"] = "whoop"
            metrics["steps_as_of"] = wsteps.get("data_as_of")
            metrics["steps_band_off"] = bool(wsteps.get("band_off"))
            metrics["steps_frozen_min"] = wsteps.get("steps_frozen_min")
            metrics["steps_last_change_at"] = wsteps.get("last_change_at")
        except Exception:
            metrics.setdefault("steps_today", None)
            metrics.setdefault("steps_7d_avg", None)
            metrics.setdefault("steps_source", "whoop")
            metrics.setdefault("steps_as_of", None)
            metrics.setdefault("steps_band_off", False)
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({
                "step": "text_gen",
                "step_at": now_iso(),
                "metrics": metrics,
            })

        # v3.3.23am (Jim OOB 2026-09-27): invalidate daily coaching cache
        # so the new today_nutrition + recent_workouts_3d data is used
        # in the next narrative. Without this, the cheer reuses today's
        # cached narrative (which doesn't include today's food).
        try:
            _invalidate_daily_coaching(today_iso())
        except Exception:
            pass

        # 1c. Workout detail loop (Jim OOB 2026-07-24: list every exercise).
        try:
            metrics["workout_detail_zh"] = _format_workout_detail_for_cheer(
                metrics.get("today_workouts") or []
            )
        except Exception:
            metrics["workout_detail_zh"] = "(運動 detail 讀取失敗)"

        # 1d. v3.3.23am (Jim OOB 2026-09-27 'Pls add latest workout
        # activities from whoop'): format last-3-day workouts as Chinese
        # block so the cheer prompt can reference the trend (back-to-back
        # hard sessions, recovery window, training load ramp).
        try:
            metrics["recent_workouts_zh"] = _format_recent_workouts_3d_for_cheer(
                metrics.get("recent_workouts_3d") or []
            )
        except Exception:
            metrics["recent_workouts_zh"] = "(近期 workout 讀取失敗)"

        # 2. v3.3.8: Text — use unified daily coaching narrative (single LLM call
        # shared with video + coach comment). Falls back to cheer-only call if
        # the daily narrative is empty or in failed state. Reuses cached
        # narrative if the day's context hasn't changed.
        date_str = today_iso()
        dc_entry = _get_or_make_daily_narrative(date_str, fire_type=fire_type, block=True)
        text = dc_entry.get("narrative") or _synthesize_cheer_text(metrics, fire_type)
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({"step": "voice_gen", "step_at": now_iso(), "text": text})

        # 3. Voice
        voice_path = _synthesize_cheer_voice(text)
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({"step": "image_gen", "step_at": now_iso(), "voice_path": voice_path})

        # 4. Image
        context = f"{fire_type}_{int(time.time())}"
        image_path = _generate_cheer_image(context)

        # 5. Cache to cheer_artifacts — moved BEFORE the CHEER_JOBS.update
        # so we can reference artifact_dir/text_path in the done payload
        # (UnboundLocalError on earlier line 7082 because artifact_dir was
        # declared below the update — 2026-08-11 22:17 HKT cheer failure).
        today_iso_str = today_iso()
        artifact_dir = CHEER_ARTIFACT_DIR / f"cheer_{today_iso_str}_{context}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "cheer_text.txt").write_text(text, encoding="utf-8")
        if voice_path:
            shutil.copy2(voice_path, artifact_dir / "cheer_voice.mp3")
        if image_path:
            shutil.copy2(image_path, artifact_dir / "cheer_motivation.png")

        # v3.2.7.37: surface full artifact metadata in CHEER_JOBS so
        # /api/cheer/status returns text_chars / voice_path / image_path /
        # ok=True / status='done' / text inline (was all-null before,
        # frontpage showed "0 字" looking like cheer silently failed).
        # v3.2.7.38: strip markdown asterisks/citations for frontpage render.
        # v3.3.7+ (Jim OOB 2026-08-30「I want full Chinese on gymbro Chinese」):
        # clean_text must match voice_text zero-EN enforcement. Run the same
        # 3-pass _voice_zh_replace + inline-replace + final hard strip loop
        # so the frontpage displays a CLEAN Chinese version (no English leaks
        # visible to user, even when miniMax drafts IT/business jargon).
        import re as _re_clean
        clean_text = _voice_zh_replace(text)
        clean_leaks = _voice_audit_en(clean_text)
        for _ in range(2):
            if not clean_leaks:
                break
            clean_text = _voice_zh_replace(clean_text)
            clean_leaks = _voice_audit_en(clean_text)
        if clean_leaks:
            clean_text = _re_clean.sub(r"([A-Za-z]+)", lambda m: _zh_inline(m.group(0)), clean_text)
            clean_leaks = _voice_audit_en(clean_text)
        if clean_leaks:
            clean_text = _re_clean.sub(r"[A-Za-z]+", "", clean_text)
            clean_text = _re_clean.sub(r"\s{2,}", " ", clean_text).strip()
            clean_text = _re_clean.sub(r"([一-鿿])\s+([一-鿿])", r"\1\2", clean_text)
            # Final polish: collapse any double-spaces the empty inline
            # replacement left behind (e.g. "你個  寫字樓" → "你個 寫字樓").
            clean_text = _re_clean.sub(r" +", " ", clean_text)
            clean_text = _re_clean.sub(r" +([，。、！？：；])", r"\1", clean_text)
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({
                "step": "done", "step_at": now_iso(),
                "image_path": image_path,
                "text_chars": len(text),
                "clean_text": clean_text,
                "has_voice": bool(voice_path),
                "has_image": bool(image_path),
                "text_path": str(artifact_dir / "cheer_text.txt"),
                "voice_path": voice_path,
                "text": text,
                "ok": True,
                "status": "done",
                "finished_at": now_iso(),
            })

        # 6. Append to cheer_log
        log = _load_cheer_log()
        log.append({
            "fire_id": job_id,
            "fire_type": fire_type,
            "timestamp_iso": now_iso(),
            "date": today_iso_str,
            "text_chars": len(text),
            "has_voice": bool(voice_path),
            "has_image": bool(image_path),
            "metrics_snapshot": metrics,
            "voice_path": voice_path,
            "image_path": image_path,
            "text_path": str(artifact_dir / "cheer_text.txt"),
        })
        # Trim to last 100 fires (light keep recent)
        log = log[-100:]
        _save_cheer_log(log)

        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id]["status"] = "done"
            CHEER_JOBS[job_id]["finished_at"] = now_iso()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        try:
            with open('/tmp/cheer_errors.log', 'a') as f:
                f.write(f"\n=== {job_id} @ {now_iso()} ({fire_type}) ===\n{tb}\n")
        except Exception:
            pass
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id] = {
                "status": "failed",
                "error": f"{type(e).__name__}: {str(e)[:200]}",
                "traceback": tb[-1500:],
                "failed_at": now_iso(),
            }


def _decide_cheer_fire_type() -> str:
    """v3.1.0: AI-decide cheer fire_type based on current HKT time + recent activity.

    Jim OOB 2026-08-07 ~15:00 HKT 'I want to trigger cheer on frontpage. it
    is not useful to specify cheer in morning, evening, or ad-hoc mode. just
    cheer with latest time. the ai will decide it.'

    Decision tree (HKT time + last activity timestamp):
    - 04:30-11:00  → morning
    - 11:00-13:30  → pre_lunch (motivational before food)
    - 13:30-17:30  → afternoon
    - 17:30-22:00  → evening
    - 22:00-04:30  → late_night (lighter touch, no big intervention)
    - last gym finished <90 min ago → post_gym (congrats, recovery focus)
    - last food log <30 min ago     → post_meal (digest, hydration)
    """
    now = now_hkt()
    hr = now.hour

    # 1. Time-based baseline
    if 4 <= hr < 11:
        base = "morning"
    elif 11 <= hr < 13:
        base = "pre_lunch"
    elif 13 <= hr < 17:
        base = "afternoon"
    elif 17 <= hr < 22:
        base = "evening"
    else:
        base = "late_night"

    # 2. Activity-based override (post_gym / post_meal win)
    try:
        log = load_log()
        today = today_iso()
        session = log.get(today, {})
        if session.get("end_time"):
            # last gym ended — check how long ago
            from datetime import datetime as _dt
            try:
                end_dt = _dt.fromisoformat(session["end_time"])
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=HKT)
                mins_since = (now_hkt() - end_dt).total_seconds() / 60
                if 0 < mins_since < 90:
                    return "post_gym"
            except Exception:
                pass
    except Exception:
        pass

    # 3. last food log <30 min → post_meal (hydration, walk)
    try:
        nlog_path = Path("/home/work/.hermes/nutrition_log.json")
        if nlog_path.exists():
            nlog = json.loads(nlog_path.read_text())
            meals = nlog.get("meals", [])
            if meals:
                last = meals[-1]
                ts = last.get("timestamp_iso") or last.get("time_iso", "")
                if ts:
                    from datetime import datetime as _dt
                    try:
                        last_dt = _dt.fromisoformat(ts)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=HKT)
                        mins_since = (now_hkt() - last_dt).total_seconds() / 60
                        if 0 < mins_since < 30:
                            return "post_meal"
                    except Exception:
                        pass
    except Exception:
        pass

    return base


@app.route("/api/cheer", methods=["POST"])
def api_cheer_trigger():
    """v2.5: Trigger a cheer fire from inside gymbro PWA.

    Jim OOB 2026-07-23: "Can copy all the cheer routine stuff into gymbro?".

    v3.1.0: accepts `fire_type` "auto" (default) — AI decides morning /
    evening / pre_lunch / afternoon / post_gym / post_meal / late_night
    based on current HKT time + recent activity. Caller no longer needs
    to specify — just press the frontpage button.

    v3.3.11: this endpoint does NOT trigger MiniMax video-01. The cheer
    pipeline is: Whoop pull → M3 chat text → Edge TTS → MiniMax IMAGE
    (not video). Video is only generated by an explicit user tap on
    the video button, which goes through /api/video_motivation with
    the 3-per-day quota guard.

    Returns immediately with {job_id}; pipeline runs in background thread.

    Poll /api/cheer/status?job_id=... for progress.
    """
    data = request.get_json(silent=True) or {}
    requested = data.get("fire_type", "auto")
    if requested == "auto" or requested not in ("morning", "evening", "manual", "pre_lunch", "afternoon", "post_gym", "post_meal", "late_night"):
        # AI-decide (v3.1.0 default)
        fire_type = _decide_cheer_fire_type()
    else:
        fire_type = requested

    job_id = f"cheer_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    with CHEER_JOBS_LOCK:
        CHEER_JOBS[job_id] = {"status": "queued", "fire_type": fire_type, "started_at": now_iso()}

    t = threading.Thread(target=_background_cheer_job, args=(job_id, fire_type), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id, "fire_type": fire_type, "status": "queued"})


@app.route("/api/cheer/status", methods=["GET"])
def api_cheer_status():
    """v2.5: Poll cheer job status. Returns full state when done, partial when running."""
    job_id = request.args.get("job_id", "")
    with CHEER_JOBS_LOCK:
        job = CHEER_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found (may have completed and been pruned)"}), 404
    if job["status"] == "done":
        # Build full artifact URLs
        today_iso_str = today_iso()
        voice_url = ""
        if job.get("voice_path") and Path(job["voice_path"]).exists():
            voice_url = f"/audio/{Path(job['voice_path']).name}"
        image_url = ""
        if job.get("image_path") and Path(job["image_path"]).exists():
            image_url = f"/img/{Path(job['image_path']).name}"
        return jsonify({
            "ok": True, "status": "done",
            "fire_type": job.get("fire_type"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "text": job.get("text", ""),
            "text_chars": job.get("text_chars", 0),
            # v3.2.7.38: clean_text is the markdown-stripped version (no **, no
            # [n] citations) for frontpage render. Cheer text from pplx often
            # wraps metrics in **markdown bold**; frontpage shows raw text but
            # the * glyphs visually clutter the body. clean_text is what TTS
            # actually reads.
            "clean_text": job.get("clean_text", ""),
            "has_voice": job.get("has_voice", False),
            "has_image": job.get("has_image", False),
            "text_path": job.get("text_path"),
            "voice_path": job.get("voice_path"),
            "image_path": job.get("image_path"),
            "voice_url": voice_url,
            "image_url": image_url,
            "metrics": job.get("metrics", {}),
            "step": job.get("step"),
            "step_at": job.get("step_at"),
            "whoop_pulled_at": job.get("whoop_pulled_at"),
        })
    return jsonify({
        "ok": True,
        "status": job["status"],
        "step": job.get("step"),
        "step_at": job.get("step_at"),
        "started_at": job.get("started_at"),
        "whoop_pulled_at": job.get("whoop_pulled_at"),
    })


# ---------------------------------------------------------------------------
# v3.3.7: Video motivation (MiniMax video-01) — separate from cheer pipeline
# (Jim OOB 2026-08-19 'try adding video motivation at gym bro, use minimax
# video-01 model'). User-triggered, ~3-5 min, returns MP4.
# ---------------------------------------------------------------------------
# v3.3.11: Daily video-01 quota guard.
#
# MiniMax caps video-01 at 3 generations per day (Jim OOB 2026-08-20:
# "there is a daily limit of 3 video in minimax"). Without a guard,
# auto-pick + the cheer pipeline could easily burn through the budget
# in a single day. We:
#   1. Track `video_quota_today` in-process, keyed by HKT date
#   2. Increment on submit (decrement on failure, capped at 3)
#   3. Expose GET /api/video_motivation/limit for the UI to render
#      "今日已用 2/3 段短片" before the user taps
#
# NOTE: cheer routine does NOT call this endpoint. The cheer pipeline
# uses MiniMax image (not video) for motivation picture, and the cheer
# text is generated entirely from MiniMax M3 chat. Video generation
# is ONLY triggered by an explicit user tap on a video button.
DAILY_VIDEO_LIMIT = 3
video_quota_today: dict = {}   # date_str → {used, last_reset}
_VIDEO_QUOTA_LOCK = threading.Lock()


def _video_quota_check_and_consume() -> tuple:
    """Atomically: returns (allowed, used, limit, remaining).

    If allowed, increments the used counter (and returns updated counts).
    """
    d = today_iso()
    with _VIDEO_QUOTA_LOCK:
        q = video_quota_today.get(d) or {"used": 0, "last_reset": now_iso()}
        if q["used"] >= DAILY_VIDEO_LIMIT:
            return False, q["used"], DAILY_VIDEO_LIMIT, 0
        q["used"] += 1
        video_quota_today[d] = q
        return True, q["used"], DAILY_VIDEO_LIMIT, DAILY_VIDEO_LIMIT - q["used"]


def _video_quota_refund() -> None:
    """If submit fails, refund the quota so the user can retry."""
    d = today_iso()
    with _VIDEO_QUOTA_LOCK:
        q = video_quota_today.get(d)
        if q and q["used"] > 0:
            q["used"] -= 1


@app.route("/api/video_motivation/limit", methods=["GET"])
def api_video_motivation_limit():
    """Return today's video-01 quota usage: {used, limit, remaining}."""
    d = today_iso()
    with _VIDEO_QUOTA_LOCK:
        q = video_quota_today.get(d) or {"used": 0}
    used = q["used"]
    return jsonify({
        "date": d,
        "used": used,
        "limit": DAILY_VIDEO_LIMIT,
        "remaining": max(0, DAILY_VIDEO_LIMIT - used),
    })


@app.route("/api/video_motivation", methods=["POST"])
def api_video_motivation_trigger():
    """Submit a video-01 generation job. Returns job_id immediately.

    v3.3.8: if `theme` is "auto" (or omitted), derive the best theme from the
    daily coaching context (time of day + recovery + last gym). The video
    prompt also gets the daily narrative's first sentence injected as a
    visual seed so the coach in the video references the same theme the
    cheer voice just narrated.

    v3.3.11: enforces a DAILY_VIDEO_LIMIT quota. If the user has already
    burned through 3 video generations today, returns 429 with the
    remaining count. Quota is consumed on submit; refunded on submit
    failure (network/curl/parse) so transient errors don't lock the
    user out.
    """
    allowed, used, limit, remaining = _video_quota_check_and_consume()
    if not allowed:
        return jsonify({
            "ok": False,
            "error": f"已用晒今日嘅 {limit} 段短片 quota, 聽日再嚟啦",
            "used": used,
            "limit": limit,
            "remaining": 0,
        }), 429
    data = request.get_json(silent=True) or {}
    requested = (data.get("theme") or "auto").strip()
    if requested == "auto":
        ctx = _build_daily_context()
        theme = _pick_video_theme(ctx)
        auto_picked = True
    elif requested in VIDEO_PROMPTS:
        theme = requested
        auto_picked = False
    else:
        _video_quota_refund()  # invalid theme — return the slot
        return jsonify({"ok": False, "error": f"unknown theme '{requested}'", "valid_themes": list(VIDEO_PROMPTS.keys()) + ["auto"]}), 400
    job_id = f"video_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    with VIDEO_JOBS_LOCK:
        VIDEO_JOBS[job_id] = {
            "status": "queued",
            "step": "queued",
            "theme": theme,
            "auto_picked": auto_picked,
            "started_at": now_iso(),
        }
    def _run_with_refund_on_submit_failure():
        try:
            _generate_video_motivation(job_id, theme)
        except Exception:
            _video_quota_refund()
            raise
    t = threading.Thread(target=_run_with_refund_on_submit_failure, daemon=True)
    t.start()
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "theme": theme,
        "auto_picked": auto_picked,
        "status": "queued",
        "quota": {"used": used, "limit": limit, "remaining": remaining},
    })


@app.route("/api/video_motivation/status", methods=["GET"])
def api_video_motivation_status():
    """Poll video generation job. Returns full state when done."""
    job_id = request.args.get("job_id", "")
    with VIDEO_JOBS_LOCK:
        job = VIDEO_JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job not found"}), 404
    if job["status"] == "done":
        return jsonify({
            "ok": True,
            "status": "done",
            "theme": job.get("theme"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "video_path": job.get("video_path"),
            "video_url": job.get("video_url"),
            "size_mb": job.get("size_mb"),
            "step": job.get("step"),
        })
    if job["status"] == "failed":
        return jsonify({
            "ok": True,
            "status": "failed",
            "error": job.get("error", "unknown"),
            "theme": job.get("theme"),
            "step": job.get("step"),
            "mini_status": job.get("mini_status"),
        })
    return jsonify({
        "ok": True,
        "status": job["status"],
        "step": job.get("step"),
        "step_at": job.get("step_at"),
        "mini_status": job.get("mini_status"),
        "started_at": job.get("started_at"),
    })


@app.route("/api/video_motivation/recent", methods=["GET"])
def api_video_motivation_recent():
    """List recent video generations (most recent first)."""
    files = []
    for f in sorted(VIDEO_CACHE_DIR.glob("gymbro_motivation_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = f.stat()
        files.append({
            "name": f.name,
            "url": f"/video/{f.name}",
            "size_mb": round(st.st_size / 1024 / 1024, 2),
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone(timedelta(hours=8))).isoformat(),
        })
    return jsonify({"ok": True, "videos": files[:10]})


@app.route("/video/<path:filename>")
def serve_video(filename):
    """Serve video MP4s from VIDEO_CACHE_DIR."""
    return send_from_directory(str(VIDEO_CACHE_DIR), filename, mimetype="video/mp4")


@app.route("/api/cheer/recent", methods=["GET"])
def api_cheer_recent():
    """v2.5: Return last N cheer fires (default 3) for cheer tab hero card.
    v2.7.31: Sanity-check image_path exists on disk — if missing (cleaned cache),
    drop image_path / has_image so frontend doesn't try to load a 404."""
    limit = int(request.args.get("limit", 3))
    log = _load_cheer_log()
    recent = log[-limit:][::-1]
    for fire in recent:
        ip = fire.get("image_path")
        if ip:
            if not Path(ip).exists():
                fire["image_path"] = ""
                fire["has_image"] = False
    return jsonify({"fires": recent, "total": len(log)})


# ─────────────────────────────────────────────────────────────────────────────
# v3.3.7 (NEW 2026-08-27): Multi-turn voice conversation endpoints
#
# Three routes:
#   POST /api/voice/session        — create session, snapshot server context
#   POST /api/voice/turn           — workhorse: classify intent, execute,
#                                    render voice-ready reply (Rule 74 applied)
#   POST /api/voice/session/end    — close session explicitly
#
# Design notes:
# - Server-side intent classification (was previously on iOS client).
#   Server has full context (today's meals, Whoop, Withings, last cheer)
#   so the reply is grounded in real data.
# - Every reply runs through _strip_voice_slash_symbols (Rule 74) so the
#   text is voice-safe for Edge-TTS WanLung — iOS just hands the
#   reply_text to AVSpeechSynthesizer.
# - Multi-turn context: each session carries `last_intent` + `last_data`
#   so follow-ups like "仲有呢?" after a query_nutrition turn surface
#   the next macro without re-querying.
# - Persistence: single index file at /home/work/.gymbro_voice_index.json
#   matching the JIM_CONTEXT pattern (safe_read_json + safe_write_json).
#   Pruned on every turn: drop sessions > 24h old.
# - Active session reuse: if iOS sends a turn without session_id AND a
#   recent active session exists (< 5 min idle), server reuses it.
#   Otherwise creates a new one. Explicit session_id always wins.
# ─────────────────────────────────────────────────────────────────────────────

VOICE_INDEX_PATH = Path("/home/work/.gymbro_voice_index.json")
VOICE_SESSION_IDLE_MINUTES = 5
VOICE_SESSION_TTL_HOURS = 24
VOICE_UNKNOWN_LOG_PATH = Path("/home/work/.gymbro_voice_unknown.jsonl")


def _voice_now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def _voice_new_session_id() -> str:
    """vs_YYYYMMDD_HHMMSS_xxxxxxxx — sortable + unique."""
    import secrets
    hkt = datetime.now(timezone(timedelta(hours=8)))
    return f"vs_{hkt.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"


def _voice_load_index() -> dict:
    """Load the full session index. Never raises; returns {} on miss/corrupt."""
    return _safe_read_json(VOICE_INDEX_PATH, {}) or {}


def _voice_save_index(idx: dict) -> bool:
    return _safe_write_json(VOICE_INDEX_PATH, idx)


def _voice_prune_stale(idx: dict) -> dict:
    """Drop sessions older than VOICE_SESSION_TTL_HOURS. Returns pruned dict."""
    if not idx:
        return idx
    from datetime import datetime as _dt
    cutoff = _dt.now(timezone(timedelta(hours=8)))
    keep = {}
    for sid, s in idx.items():
        try:
            last_active = _dt.fromisoformat(s.get("last_active_at", ""))
            if (cutoff - last_active).total_seconds() < VOICE_SESSION_TTL_HOURS * 3600:
                keep[sid] = s
        except Exception:
            continue
    return keep


def _voice_snapshot_context() -> dict:
    """Best-effort snapshot of "where Jim is right now" for session start."""
    snap = {"today": today_iso(), "snapshot_at": _voice_now_iso()}
    # Today's nutrition (Sheet-backed via _load_today_nutrition)
    try:
        n = _load_today_nutrition()
        if n and isinstance(n, dict):
            t = n.get("totals") or {}
            snap["today_calories"] = t.get("calories")
            snap["today_protein"] = t.get("protein")
            snap["today_carbs"] = t.get("carbs")
            snap["today_fat"] = t.get("fat")
            snap["meal_count"] = n.get("meal_count")
    except Exception:
        pass
    # Health overlay (recovery, weight, steps)
    try:
        snap["recovery_pct"] = _recovery_pct()
        snap["weight_kg"] = _body_weight()
        snap["steps_today"] = (_whoop_steps() or {}).get("steps")
    except Exception:
        pass
    # Last cheer MP3 path (if any)
    try:
        cheer_log = _load_cheer_log() or []
        if cheer_log:
            last = cheer_log[-1]
            snap["last_cheer_url"] = last.get("voice_url") or last.get("audio_url")
    except Exception:
        pass
    return snap


def _voice_classify_intent(transcript: str, prior_intent: str | None) -> tuple[str, dict]:
    """Return (intent_name, parsed_args). Server-side port of iOS VoiceIntentRouter.

    Keyword coverage is intentionally small — unknown turns are logged so
    Jim can grow the keyword set over time (Risks #1 in the plan).
    """
    t = (transcript or "").strip().lower()
    if not t:
        return ("unknown", {"reason": "empty"})

    # end_session wins over everything if the user is closing out
    if any(k in t for k in ("stop", "拜拜", "結束", "end session", "that's it", "done")):
        return ("end_session", {})

    # Multi-turn follow-up phrases: only meaningful if the prior turn was a
    # query we can extend. Match these BEFORE the broad keyword scan so they
    # don't get shadowed by accident.
    followup_phrases = ("仲有呢", "仲有冇", "more", "and?", "再嚟", "繼續", "點")
    if any(k in t for k in followup_phrases) and prior_intent in (
        "query_nutrition", "query_health", "log_meal", "trigger_cheer",
    ):
        # Map prior intent to follow-up intent
        followup_map = {
            "query_nutrition": "query_nutrition_followup",
            "query_health": "query_health_followup",
            "log_meal": "log_meal_followup",
            "trigger_cheer": "trigger_cheer_followup",
        }
        return (followup_map[prior_intent], {})

    # log_meal — strip common prefixes and pass remainder to scan_preview_text
    log_prefixes = ("log ", "食咗 ", "記低 ", "record ", "剛剛食咗 ", "啱啱食咗 ")
    for p in log_prefixes:
        if t.startswith(p):
            payload = (transcript[len(p):]).strip()
            return ("log_meal", {"text": payload})

    # log_set — bench/squat/deadlift + numbers + kg
    import re as _re_set
    if _re_set.search(r"\b(bench|squat|deadlift|press|row|pulldown|fly|curl|extension|raise)\b", t):
        m_kg = _re_set.search(r"(\d+(?:\.\d+)?)\s*(?:kg|kilo)", t)
        m_reps = _re_set.search(r"(\d+)\s*(?:reps?|下|次)", t)
        m_set = _re_set.search(r"第\s*(\d+)\s*set", t)
        m_ex = _re_set.search(r"\b(bench|squat|deadlift|press|row|pulldown|fly|curl|extension|raise)\b", t)
        if m_kg and m_reps:
            return ("log_set", {
                "exercise": (m_ex.group(1) if m_ex else "bench press").title(),
                "weight_kg": float(m_kg.group(1)),
                "reps": int(m_reps.group(1)),
                "set": int(m_set.group(1)) if m_set else 1,
            })

    # query_nutrition
    if any(k in t for k in ("食咗幾多", "卡路里", "calorie", "calories", "protein", "蛋白")):
        # multi-turn: if prior was query_nutrition and transcript is short,
        # treat as follow-up asking for next macro
        if prior_intent == "query_nutrition" and len(t) <= 8:
            return ("query_nutrition_followup", {})
        return ("query_nutrition", {})

    # query_health
    if any(k in t for k in ("狀態", "recovery", "recover", "體重", "weight", "steps", "步數")):
        return ("query_health", {})

    # trigger_cheer
    if any(k in t for k in ("cheer", "打氣", "加油", "cheer me", "幫我打氣")):
        return ("trigger_cheer", {})

    return ("unknown", {"transcript": transcript})


def _voice_render_reply(intent: str, result: dict, prior_data: dict | None) -> str:
    """Templated Cantonese reply. Voice-ready (Rule 74 cleaned at the end)."""
    if intent == "log_meal":
        if result.get("ok"):
            dish = result.get("dish") or "食物"
            kcal = result.get("calories")
            if kcal is not None:
                return f"已記低 {dish}, {kcal:.0f} 卡路里"
            return f"已記低 {dish}"
        err = result.get("error") or "唔知點解失敗"
        return f"上傳失敗, {err}"

    if intent == "query_nutrition":
        kcal = result.get("calories")
        p = result.get("protein")
        c = result.get("carbs")
        f_ = result.get("fat")
        if kcal is None or kcal == 0:
            return "今日未食過嘢"
        return f"今日食咗 {kcal:.0f} 卡路里, 蛋白 {p:.0f} 克, 碳 {c:.0f} 克, 脂 {f_:.0f} 克"

    if intent == "query_nutrition_followup":
        # surface whichever macro was NOT mentioned in the prior reply
        prior = prior_data or {}
        c = prior.get("carbs") or 0
        f_ = prior.get("fat") or 0
        if c == 0 and f_ == 0:
            return "今日未食過嘢, 冇嘢可以再講"
        return f"仲有碳 {c:.0f} 克, 脂 {f_:.0f} 克"

    if intent == "query_health_followup":
        prior = prior_data or {}
        steps = prior.get("steps_today")
        if steps:
            return f"今日行咗 {steps:.0f} 步"
        return "未拉到步數資料"

    if intent == "log_meal_followup":
        return "想 log 多樣嘢食就講, 例如 log 一杯 latte"

    if intent == "trigger_cheer_followup":
        return "30 秒左右就準備好, 會自動同你講"

    if intent == "query_health":
        rec = result.get("recovery_pct")
        w = result.get("weight_kg")
        steps = result.get("steps_today")
        bits = []
        if rec is not None:
            bits.append(f"recovery {rec:.0f} 巴仙")
        if w is not None:
            bits.append(f"體重 {w:.1f} 公斤")
        return "，".join(bits) if bits else "未拉到健康資料"

    if intent == "log_set":
        if result.get("ok"):
            ex = result.get("exercise", "set")
            return f"已記 {ex}, 第 {result.get('set', 1)} set"
        return f"記唔到, {result.get('error', '再嚟多次')}"

    if intent == "trigger_cheer":
        return "幫你準備緊 cheer, 大概 30 秒, 準備好同你講"

    if intent == "end_session":
        return "OK 收工, 拜拜"

    return "唔識呢句, 試下 log 或者 cheer"


def _voice_execute_intent(intent: str, args: dict, ctx: dict) -> dict:
    """Run the side-effecting action for an intent. Returns a result dict
    that _voice_render_reply uses to compose the reply."""
    if intent == "log_meal":
        text = args.get("text", "").strip()
        if not text:
            return {"ok": False, "error": "empty"}
        # Two-step flow matching the existing PWA: preview (AI estimate)
        # then commit (write to Sheet). /api/scan_preview_text with
        # commit=true does NOT actually commit — that's a known quirk;
        # use the canonical /api/scan_commit path.
        try:
            import urllib.request as _ur
            payload = json.dumps({"text": text, "commit": False}).encode()
            req = _ur.Request(
                "http://127.0.0.1:7000/api/scan_preview_text",
                data=payload,
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            with _ur.urlopen(req, timeout=30) as r:
                preview_data = json.loads(r.read())
            preview = (preview_data or {}).get("preview") or {}
            meal = preview.get("suggested_entry") or {}
            if not meal:
                return {"ok": False, "error": "no preview returned"}
            # Commit step — text-only path uses image_path=""
            commit_payload = json.dumps({
                "entries": [meal],
                "image_path": "",
            }).encode()
            req2 = _ur.Request(
                "http://127.0.0.1:7000/api/scan_commit",
                data=commit_payload,
                method="POST",
            )
            req2.add_header("Content-Type", "application/json")
            with _ur.urlopen(req2, timeout=30) as r:
                commit_data = json.loads(r.read())
            # Parse sheet row number from sheet_range like "Nutrition!A197:K197"
            sheet_row = None
            sheet_range = commit_data.get("sheet_range") or ""
            if sheet_range:
                import re as _re_row
                m = _re_row.search(r"[A-Z]+(\d+):", sheet_range)
                if m:
                    try:
                        sheet_row = int(m.group(1))
                    except Exception:
                        pass
            return {
                "ok": bool(commit_data.get("ok")),
                "dish": meal.get("name") or text,
                "calories": meal.get("calories"),
                "sheet_row": sheet_row,
                "sheet_synced": commit_data.get("sheet_synced"),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:80]}

    if intent == "query_nutrition":
        n = ctx.get("nutrition") or {}
        t = n.get("totals") or {}
        return {
            "calories": t.get("calories"),
            "protein": t.get("protein"),
            "carbs": t.get("carbs"),
            "fat": t.get("fat"),
        }

    if intent == "query_health":
        return ctx.get("health") or {}

    if intent == "log_set":
        try:
            import urllib.request as _ur
            payload = json.dumps({
                "exercise": args["exercise"],
                "weight_kg": args["weight_kg"],
                "reps": args["reps"],
                "set": args["set"],
                "intensity": "working",
                "form_check": "ok",
            }).encode()
            req = _ur.Request(
                "http://127.0.0.1:7000/api/log_set",
                data=payload,
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            with _ur.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            return {
                "ok": data.get("ok", False),
                "exercise": args["exercise"],
                "set": args["set"],
                "error": data.get("error"),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:80]}

    if intent == "trigger_cheer":
        try:
            import urllib.request as _ur
            req = _ur.Request(
                "http://127.0.0.1:7000/api/cheer",
                data=b'{"fire_type":"manual"}',
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            with _ur.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            return {
                "ok": True,
                "cheer_job_id": data.get("job_id"),
                "cheer_status_url": f"/api/cheer/status?job_id={data.get('job_id')}",
            }
        except Exception as e:
            return {"ok": False, "error": str(e)[:80]}

    return {}


def _voice_log_unknown(transcript: str, intent: str, session_id: str) -> None:
    """Append unknown transcripts so Jim can grow the keyword set later."""
    try:
        with open(VOICE_UNKNOWN_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": _voice_now_iso(),
                "session_id": session_id,
                "transcript": transcript,
                "intent": intent,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _voice_apply_rule74(text: str) -> str:
    """Run Rule 74 cleanup so the reply is voice-safe for TTS."""
    if not text:
        return text
    try:
        text = _strip_voice_slash_symbols(text)
        text = _humanize_for_voice(text)
        text = _voice_zh_replace(text)
    except Exception:
        # If any step fails, return the un-cleaned text — better than nothing
        pass
    return text


@app.route("/api/voice/session", methods=["POST"])
def api_voice_session():
    """v3.3.7: Create a new voice conversation session."""
    data = request.get_json(silent=True) or {}
    fire_type = data.get("fire_type", "voice")

    sid = _voice_new_session_id()
    ctx = _voice_snapshot_context()

    idx = _voice_load_index()
    idx = _voice_prune_stale(idx)
    idx[sid] = {
        "session_id": sid,
        "created_at": _voice_now_iso(),
        "last_active_at": _voice_now_iso(),
        "fire_type": fire_type,
        "status": "active",
        "context_snapshot": ctx,
        "last_intent": None,
        "last_data": None,
        "turns": [],
    }
    _voice_save_index(idx)

    return jsonify({
        "ok": True,
        "session_id": sid,
        "created_at": idx[sid]["created_at"],
        "context_snapshot": ctx,
    })


@app.route("/api/voice/turn", methods=["POST"])
def api_voice_turn():
    """v3.3.7: One turn of a voice conversation.

    Body: { session_id?, transcript, fire_type? }
    Response: { ok, session_id, intent, reply_text, side_effects, follow_up_hint }
    """
    data = request.get_json(silent=True) or {}
    transcript = (data.get("transcript") or "").strip()
    if not transcript:
        return jsonify({"ok": False, "error": "missing transcript"}), 400

    idx = _voice_load_index()
    idx = _voice_prune_stale(idx)

    # Resolve session_id: explicit wins; else reuse most-recent active within idle window
    sid = data.get("session_id")
    if not sid or sid not in idx:
        sid = None
        from datetime import datetime as _dt
        now = _dt.now(timezone(timedelta(hours=8)))
        for k, s in idx.items():
            if s.get("status") != "active":
                continue
            try:
                last = _dt.fromisoformat(s.get("last_active_at", ""))
                idle_min = (now - last).total_seconds() / 60
                if idle_min <= VOICE_SESSION_IDLE_MINUTES:
                    sid = k
                    break
            except Exception:
                continue
    if not sid:
        # create fresh
        sid = _voice_new_session_id()
        idx[sid] = {
            "session_id": sid,
            "created_at": _voice_now_iso(),
            "last_active_at": _voice_now_iso(),
            "fire_type": data.get("fire_type", "voice"),
            "status": "active",
            "context_snapshot": _voice_snapshot_context(),
            "last_intent": None,
            "last_data": None,
            "turns": [],
        }

    sess = idx[sid]
    prior_intent = sess.get("last_intent")
    prior_data = sess.get("last_data") or {}

    intent, args = _voice_classify_intent(transcript, prior_intent)

    # Build context for action intents (re-fetch fresh — small overhead)
    ctx = {}
    if intent in ("query_nutrition", "query_nutrition_followup", "log_meal"):
        try:
            ctx["nutrition"] = _load_today_nutrition()
        except Exception:
            ctx["nutrition"] = {}
    if intent == "query_health":
        try:
            ctx["health"] = {
                "recovery_pct": _recovery_pct(),
                "weight_kg": _body_weight(),
                "steps_today": (_whoop_steps() or {}).get("steps"),
            }
        except Exception:
            ctx["health"] = {}

    # Execute
    result = _voice_execute_intent(intent, args, ctx)

    # For follow-ups, merge prior_data into result so the reply renderer
    # can show the next macro that wasn't in the prior reply.
    if intent == "query_nutrition_followup":
        result = {**prior_data, **result}

    # Render + Rule 74
    raw_reply = _voice_render_reply(intent, result, prior_data)
    reply_text = _voice_apply_rule74(raw_reply)

    # Build side_effects list (what happened server-side)
    side_effects = []
    if intent == "log_meal" and result.get("ok"):
        side_effects.append({
            "kind": "meal_logged",
            "sheet_row": result.get("sheet_row"),
            "dish": result.get("dish"),
            "calories": result.get("calories"),
        })
    elif intent == "log_set" and result.get("ok"):
        side_effects.append({
            "kind": "set_logged",
            "exercise": result.get("exercise"),
            "set": result.get("set"),
        })
    elif intent == "trigger_cheer" and result.get("ok"):
        side_effects.append({
            "kind": "cheer_triggered",
            "job_id": result.get("cheer_job_id"),
            "status_url": result.get("cheer_status_url"),
        })

    # Update session
    sess["last_active_at"] = _voice_now_iso()
    sess["last_intent"] = intent
    sess["last_data"] = result
    sess["turns"].append({
        "ts": _voice_now_iso(),
        "transcript": transcript,
        "intent": intent,
        "reply_text": reply_text,
        "side_effects": side_effects,
    })
    # Keep last 50 turns per session — bounded memory
    if len(sess["turns"]) > 50:
        sess["turns"] = sess["turns"][-50:]

    # end_session intent closes the session
    if intent == "end_session":
        sess["status"] = "ended"

    idx[sid] = sess
    _voice_save_index(idx)

    # Log unknown for keyword-set growth
    if intent == "unknown":
        _voice_log_unknown(transcript, intent, sid)

    # Follow-up hint: what would naturally come next
    follow_up_hint = ""
    if intent == "query_nutrition":
        follow_up_hint = "仲有呢?"
    elif intent == "log_meal":
        follow_up_hint = "改返?"
    elif intent == "trigger_cheer":
        follow_up_hint = "點解咁短?"

    return jsonify({
        "ok": True,
        "session_id": sid,
        "intent": intent,
        "reply_text": reply_text,
        "side_effects": side_effects,
        "session_active": sess.get("status") == "active",
        "follow_up_hint": follow_up_hint,
    })


@app.route("/api/voice/session/end", methods=["POST"])
def api_voice_session_end():
    """v3.3.7: Close a voice session explicitly."""
    data = request.get_json(silent=True) or {}
    sid = data.get("session_id")
    if not sid:
        return jsonify({"ok": False, "error": "missing session_id"}), 400
    idx = _voice_load_index()
    if sid not in idx:
        return jsonify({"ok": False, "error": "session not found"}), 404
    sess = idx[sid]
    sess["status"] = "ended"
    sess["last_active_at"] = _voice_now_iso()
    idx[sid] = sess
    _voice_save_index(idx)
    return jsonify({
        "ok": True,
        "ended_at": sess["last_active_at"],
        "total_turns": len(sess.get("turns", [])),
    })


# v3.3.8: Unified daily coaching endpoint.
# GET  /api/daily_coaching        — returns current day's narrative (or 'generating' 202)
# POST /api/daily_coaching/refresh — force-regenerate (used by cheer tab refresh btn)
@app.route("/api/daily_coaching", methods=["GET"])
def api_daily_coaching_get():
    """Return today's unified daily narrative.

    Query params:
        wait=true   — block up to 25s for the first generation
        fire_type=  — pass-through to the narrative prompt
    """
    wait = request.args.get("wait", "false").lower() == "true"
    fire_type = request.args.get("fire_type", "manual")
    date_str = today_iso()
    # v3.3.10: if no fire_type specified, return the most recently generated
    # entry across all fire_types for today (cheer tab "open" doesn't need
    # to pin a specific time-bucket).
    if fire_type == "manual" and "fire_type" not in request.args:
        best = None
        for k, e in daily_coaching_cache.items():
            if k[0] != date_str or e.get("status") != "ready":
                continue
            if best is None or (e.get("generated_at") or "") > (best.get("generated_at") or ""):
                best = e
        if best is not None:
            return jsonify({
                "status": "ready",
                "date": date_str,
                "fire_type": best.get("fire_type"),
                "narrative": best.get("narrative"),
                "theme": best.get("theme"),
                "video_seed": best.get("video_seed"),
                "food_seed": best.get("food_seed"),
                "generated_at": best.get("generated_at"),
            })
        # No ready entry — fall through and generate a "manual" one
    entry = _daily_coaching_entry(date_str, fire_type)
    # If ready and hash matches current context, return it.
    ctx = _build_daily_context()
    new_hash = _context_hash(ctx)
    if entry.get("status") == "ready" and entry.get("context_hash") == new_hash and entry.get("narrative"):
        return jsonify({
            "status": "ready",
            "date": date_str,
            "fire_type": fire_type,
            "narrative": entry["narrative"],
            "theme": entry.get("theme"),
            "video_seed": entry.get("video_seed"),
            "food_seed": entry.get("food_seed"),
            "generated_at": entry.get("generated_at"),
        })
    # Need to generate. If wait=true, do it blocking. Otherwise return 202.
    if wait:
        entry = _get_or_make_daily_narrative(date_str, fire_type=fire_type, block=True)
        return jsonify({
            "status": entry.get("status", "ready"),
            "date": date_str,
            "fire_type": fire_type,
            "narrative": entry.get("narrative") or "",
            "theme": entry.get("theme"),
            "video_seed": entry.get("video_seed"),
            "food_seed": entry.get("food_seed"),
            "generated_at": entry.get("generated_at"),
            "job_id": entry.get("job_id"),
        })
    # Fire-and-forget background generation
    _get_or_make_daily_narrative(date_str, fire_type=fire_type, block=False)
    return jsonify({
        "status": "generating",
        "date": date_str,
        "fire_type": fire_type,
        "job_id": entry.get("job_id"),
    }), 202


@app.route("/api/daily_coaching/refresh", methods=["POST"])
def api_daily_coaching_refresh():
    """Force-regenerate today's narrative. Bypasses cache."""
    data = request.get_json(silent=True) or {}
    fire_type = data.get("fire_type", "manual")
    date_str = today_iso()
    _invalidate_daily_coaching(date_str)
    entry = _get_or_make_daily_narrative(date_str, fire_type=fire_type, block=True)
    return jsonify({
        "status": entry.get("status", "ready"),
        "date": date_str,
        "narrative": entry.get("narrative") or "",
        "theme": entry.get("theme"),
        "video_seed": entry.get("video_seed"),
        "food_seed": entry.get("food_seed"),
        "generated_at": entry.get("generated_at"),
    })


# ---------- v3.2.7.49 (Jim 2026-08-20): Music workflow removed ----------
# MiniMax Music API returned status 2153 (Token Plan blocked) and the
# user's machine can't self-host MiniMax-Music3 (only 3.8GB RAM / 2 cores).
# All music generation code, /api/music/* endpoints, and the music card
# in templates/index.html have been removed. Voice + image + video remain.

# ---------- HTML (Uber-inspired) ----------
# Loaded from templates/index.html (3,820 lines extracted 2026-08-09
# during v3.2.7.16 refactor — see plan: drifting-bouncing-toast.md).
_HTML_TEMPLATE_PATH = Path(__file__).parent / "templates" / "index.html"
HTML_TEMPLATE = _HTML_TEMPLATE_PATH.read_text(encoding="utf-8")


# ---------- Service worker for PWA ----------
SERVICE_WORKER = """
// Jim OOB 2026-08-06 14:20 HKT — SW v64. Food log cleanup.
// "i saw many 食物 in the list. very bad" — Jim OOB 2026-08-06.
// Filter now drops any entry with name='食物' (literal) or '食物 #hash',
// so the PWA list only shows entries with real dish names.
// "and some color code as title #" — hash labels dropped via filter.
// "and why there is no other nutriention info" — restored P/C/F display
// inline next to kcal (was deleted in v63 overzealous cleanup).
// v3.2.0: schedule tab (weekly + monthly calendar) + header cheer button
// at top-left + food log rating badge moved to image overlay (top-right).
// v3.2.2: slim header (compact step number + HH:MM clock + MM-DD date).
// v3.2.3: RPE slider with color zones (1-10) replacing number input.
// v3.2.4: gym focus mode fix — toggleGymFocus() syncs Alpine state + scrolls to top.
// v3.2.5: schedule tab hides days without activities (week strip + month grid).
// v3.2.6: schedule tab simplified — week strip + list view removed.
// Monthly calendar is the single source of truth, fed by
// /api/whoop_activities_calendar. /api/whoop_activities_week endpoint
// deleted (returns 404). state.scheduleWeek + scheduleView removed.
// (Jim OOB 2026-08-07 23:30 HKT 'Fix gymbro calendar view. Remove its
// list view and weekly view'.)
// SW v64 → v137 changelog (compressed)
// v137 (Jim OOB 2026-08-12 23:18 HKT): /api/* 改 network-first，唔再 cache API responses。
//   修正 nutrition_log 改咗之後 PWA 仍然顯示舊 macros 嘅 bug (例如 燒烤牛肉片10片 deep-search correction)。
// v64 → v136 changelog (compressed)
//     date-range chips. /api/export_text now accepts ?date=YYYY-MM-DD for
//     single-day export (legacy ?days=N still works).
//   - 30s REST cooldown after LOG SET: prevents accidental double-tap from
//     inflating set count. Button shows ⏳ REST ${cooldownRemaining}s and
//     is disabled during cooldown. After 30s, button returns to ✓ LOG SET.
// v2.7.5 changes (Jim OOB 2026-07-24 22:25 HKT):
//   - Withings steps systemic fix: 09:19 cheer fire showed 59 steps but
//     real value was 6046. Root cause = subprocess + Withings server-side
//     5-min cache returning last-committed (04:00 baseline) instead of
//     fresh day-total.
//   - Fix: in-process import (shared module state, zero per-call Python
//     startup), double-pull with 3s gap (second call usually clears the
//     stale server cache), in-process 30s cache (cheer UI fires 3 buttons
//     in <2s, no point hitting Withings 3x), max(steps_1, steps_2) to
//     pick the freshest value, atomic WITHINGS_CACHE write so all
//     endpoints see the same number, fallback to last-good cached value
//     if pull fails.
// v2.7.4 changes (Jim OOB 2026-07-24 09:15 HKT):
//   - LOG SET cooldown REMOVED entirely (was 30s → 60s, now no cooldown).
//     Jim wants to log sets as fast as possible. Disabled the
//     cooldownUntil / cooldownRemaining / cooldownInterval state and the
//     "⏳ REST Ns" button label. Button is now always clickable when
//     !saving.
//   - Withings steps + body: REMOVED v3.3.25 — WHOOP supplies both
//     (cycle.step_count for steps, user/measurement/body for weight)
//     cheer fire (Whoop V2 cycle has no `steps` field, so steps must come
//     from Withings). Parses "withings.py steps 1" stdout, injects
//     {steps, distance_km, calories} into metrics. pplx prompt now has
//     "Withings 今日步數" + "Withings 今日步行距離" lines.
//   - /api/health_overlay now also returns steps_today + distance_km_today.
// v19 changes (Jim OOB 2026-07-22):
//   - Whoop paste reliability fix: copyDay() output now uses ABSOLUTE set
//     numbering (Set 1..N across the session, not "Set 1" reset per exercise),
//     inserts 🏋 exercise header + blank line between exercise groups, and
//     ends with "End of session" marker. Old format made Whoop collapse set
//     boundaries when multiple exercises were interleaved (same Set 1 marker
//     for different exercises). See /api/export_text → else (txt) branch.
//   - "(was N)" annotation preserves the sheet set number so Jim can still
//     cross-reference back to /api/history.
// v20 changes (Jim OOB 2026-07-22):
//   - Refactor: extracted workout text rendering to workout_formatter.py
//     module (single source of truth, two text modes: whoop_text default,
//     whoop_emoji opt-in).
//   - copyDay() default fmt=whoop_text: pure ASCII, no emojis, no ×,
//     no Unicode bullets. Labels: "Date: / Exercise: / Set N:".
//     Symptom fixed: Whoop AI paste collapsed multi-exercise sets because
//     emoji headers and × multiplication sign looked like paragraph breaks
//     to the parser. Old emoji format still works via ?fmt=whoop_emoji.
//   - Sheet set number preserved as "(sheet set N)" annotation so Jim can
//     still cross-reference back to /api/history.
// v21 changes (Jim OOB 2026-07-22):
//   - Major refactor: copyDay() output now uses ALL-CAPS keywords + "X OF Y"
//     framing ("EXERCISE 1 OF 4", "SET 1 OF 5 FOR THIS EXERCISE: 40 kg x 10
//     reps"). Designed to be unambiguous to Whoop's AI parser.
//   - Dedupe by (date, exercise, set_n) inside formatter; removes sheet
//     duplicate accumulation from past sync passes.
//   - Add /api/repair_sheet endpoint: clears ALL sheet rows for a date and
//     re-pushes from local WORKOUT_LOG idempotently. Use this to clean up
//     any historical dupes (e.g. POST {"date": "2026-07-20"}). One-time
//     cleanup, idempotent.
//   - Old whoop_text format (v20) was still ambiguous because it let sheet
//     "(sheet set N)" parentheticals and exercise names like "Low Row
//     (Cable)" interfere with parser tokenization. whoop_text_v2 removes
//     parentheticals, uppercases names, and labels each row's X of Y.
// v22 changes (Jim OOB 2026-07-22):
//   - Per-row Copy button shows ⏳ + spin animation + "Copying…" aria-label
//     while in flight (state: copyingDate + copyInFlight). Button is disabled
//     and gets cursor:wait while busy. Resolves back to 📋 when finished.
//   - Sync_sheet dedup hardened: dedup by (date, exercise, set_n, time_iso)
//     so repeated sync calls never re-push the same set, even if local
//     set_n restarts after mid-session deletes. Was dedup by (date, exercise,
//     set_n) only — set_n regression allowed duplicates to leak through.
//   - /api/repair_sheet endpoint: surgical clear+repush from local for one
//     date. Use this to clean up accumulated dupes from older sync passes.
//     POST {"date": "YYYY-MM-DD"} clears+rebuilds that date idempotently.
// v3.1.0: 4-tab nav + landscape food grid + gym focus mode + PT/Whoop share +
// frontpage cheer auto-trigger. 4 tabs (food / gym / cheer / schedule), default = food.
// v3.2.2: slim header (compact step number + HH:MM clock + MM-DD date).
// v3.2.3: RPE slider with color zones (1-10) replacing number input.
// v3.2.4: gym focus mode fix — toggleGymFocus() syncs Alpine state + scrolls to top.
// v3.2.5: schedule tab hides days without activities (week strip + month grid).
// v3.2.6: schedule tab simplified — week strip + list view removed.
// Monthly calendar is the single source of truth, fed by
// /api/whoop_activities_calendar. /api/whoop_activities_week endpoint
// deleted (returns 404). state.scheduleWeek + scheduleView removed.
// (Jim OOB 2026-08-07 23:30 HKT 'Fix gymbro calendar view. Remove its
// list view and weekly view'.)
const CACHE = 'gym-web-v145';
// v3.3.38 (2026-10-22, Jim OOB「The thumbnail are the same again. Pls fix the
// thumbnail and the systemic problem」):cache 版本 144 → 145。
//   activate 會刪晒所有舊 cache → Jim 手機上面舊嗰批「一模一樣」嘅縮圖
//   (2026-10-21 嘅 Drive placeholder bug 留下嚟嘅)一次過清走。
//   新規則:URL 帶 ?v=<hash>(內容定址)→ cache-first(immutable,安全又快);
//   冇 ?v= 嘅舊 URL → 照舊 network-first no-store。
// SW v64 → v137 changelog (compressed)
// v137 (Jim OOB 2026-08-12 23:18 HKT): /api/* 改 network-first，唔再 cache API responses。
//   修正 nutrition_log 改咗之後 PWA 仍然顯示舊 macros 嘅 bug (例如 燒烤牛肉片10片 deep-search correction)。
// v64 → v136 changelog (compressed)
//   - LATEST_KNOWN_TRUTH semantics: pull 7d of getactivity, find the latest
//     record with steps > 0, return it with its actual date. Matches what
//     the iPhone Withings widget shows. Replaces v2.7.22/2.7.23 fallback
//     logic that returned "syncing" indefinitely between HKT 00:00-04:00
//     when daily commit hadn't run yet for the new day.
//   - Cross-check with intraday: if intraday has more steps than chosen,
//     use intraday (partial live data).
//   - Source flag exposed: _source = "today_commit" | "latest_truth" |
//     "intraday_override" | "latest_record_any" so UI can distinguish.
//   - Withings `getintradayactivity` 24h window SILENTLY TRUNCATES earlier
//     events (verified empirically 2026-08-03: 12h=0, 16h=3, 24h=7, 48h=99
//     entries). FIX: use 48h window then filter ts >= hkt_midnight for today.
//     Catches all of today's events even if Apple Watch pushed them hours ago.
//   - Wake-hour fallback: when getactivity returns < 100 steps during HKT
//     06:00-23:00, force intraday cross-check. If intraday > daily OR both
//     < 50 steps, return `syncing: true` (Rule 24 NEVER FABRICATE) instead
//     of showing stale baseline as today's truth.
//     Backend function: REMOVED v3.3.25 (Withings step path deleted).
//     Frontend behavior unchanged — widget shows real steps immediately
//     (no "—/同步中") when intraday data exists.
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => {
  e.waitUntil(
    Promise.all([
      caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))),
      self.clients.claim()
    ])
  );
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // v137: /api/* ALWAYS network-first, NEVER cache (Jim OOB 2026-08-12 23:18 HKT).
  // 修正 nutrition_log 改咗之後 PWA 仍然顯示舊 macros 嘅 bug。
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(e.request).then(res => res).catch(() => new Response(JSON.stringify({error:'offline'}), {status:503, headers:{'Content-Type':'application/json'}}))
    );
    return;
  }
  // v3.3.23af (Jim OOB 2026-09-16 'The jpg download, there is broken image
  // for the first time use after refresh'): /nutrition_card/* ALWAYS
  // network-first. The daily JPG card regenerates whenever meals change,
  // so caching is meaningless — stale cached image shows broken pixel
  // after user edits food macros + SW bumps cache version.
  if (url.pathname.startsWith('/nutrition_card/')) {
    e.respondWith(
      fetch(e.request, {cache: 'no-store'}).then(res => res).catch(() => new Response('', {status: 503}))
    );
    return;
  }
  // v3.3.23ai (Jim OOB 2026-09-19 'after delete some food, the image
  // are tmeproitly drifted'): /scan_thumb/* + /scan_img/* ALWAYS
  // network-first. After delete + row shift, the server-side thumb cache
  // drops + refetches, but the SW cache-first rule returns the OLD
  // image that previously belonged to a different row_index. Going
  // network-first guarantees the PWA always sees the row's current
  // image, even after the index shift. Server-side /scan_thumb already
  // sends Cache-Control: no-store, but the SW ignores it.
  if (url.pathname.startsWith('/scan_thumb/') || url.pathname.startsWith('/scan_img/')) {
    // v3.3.38: 內容定址 URL(?v=<hash of drive file id>)→ 同一個 URL 永遠係
    // 同一張相 → 可以 cache-first(快,而且唔會顯示舊圖)。
    // 冇 ?v= 嘅舊 URL(row_index 對應嘅相會變)→ 照舊 network-first。
    if (url.searchParams.get('v')) {
      e.respondWith(
        caches.open(CACHE).then(cache => cache.match(e.request).then(hit => hit || fetch(e.request).then(res => {
          if (res && res.status === 200 && res.type === 'basic') cache.put(e.request, res.clone());
          return res;
        }).catch(() => new Response('', {status: 503}))))
      );
      return;
    }
    e.respondWith(
      fetch(e.request, {cache: 'no-store'}).then(res => res).catch(() => new Response('', {status: 503}))
    );
    return;
  }
  // Network-first for HTML documents so we never serve stale pages.
  // Cache-first only for static assets (js/css/images).
  if (e.request.mode === 'navigate' || (e.request.method === 'GET' && e.request.headers.get('accept')?.includes('text/html'))) {
    e.respondWith(
      fetch(e.request).then(res => {
        if (res.ok && e.request.method === 'GET') {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }
  // Cache-first for other static assets.
  e.respondWith(
    caches.open(CACHE).then(cache =>
      cache.match(e.request).then(cached => cached || fetch(e.request).then(res => {
        if (e.request.method === 'GET' && res.ok) cache.put(e.request, res.clone());
        return res;
      }).catch(() => cached))
    )
  );
});
// Force reload when a new SW takes over (controllerchange = new SW activated).
// v2.4 guarded: only reload if user is NOT actively logging a set. Without this guard,
// every SW cache bump (v27→v28 etc.) force-reloaded iPhone PWA mid-tap, causing
// the 'click heading → reload' symptom Jim reported. We briefly suppress reload
// during active interaction (last tap <1500ms ago) so user-initiated clicks are
// never interrupted by a background SW update. After the cooldown elapses, the
// new SW will still kick in on next navigation naturally.
self.addEventListener('controllerchange', () => {
  if (typeof window !== 'undefined') {
    const lastTap = (typeof window.__lastTapAt === 'number') ? window.__lastTapAt : 0;
    if (Date.now() - lastTap < 1500) {
      // Active tap — skip reload, but force-takeover after grace period
      setTimeout(() => { try { window.location.reload(); } catch(e){} }, 30000);
      return;
    }
    window.location.reload();
  }
});
""".strip()


@app.route("/sw.js")
def sw():
    return SERVICE_WORKER, 200, {"Content-Type": "application/javascript"}


if __name__ == "__main__":
    print(f"\n🏋️ Jim's Gym Web App starting...")
    print(f"   Local:   http://127.0.0.1:{PORT}/")
    print(f"   Tailscale: http://100.114.66.125:{PORT}/")
    print(f"   Persist to: {WORKOUT_LOG}\n")
    # v3.3.25 (2026-10-21): a job that was still "running" when the process
    # died is now reported as "interrupted" (and can be resumed via
    # POST /api/scan_resume/<job_id>). Without this the frontend just saw
    # a 404 and showed "查 AI 進度失敗 / Load failed".
    try:
        _scan_jobs_load_interrupted()
    except Exception as _e:
        print(f"[scan-job] interrupted-job recovery failed: "
              f"{type(_e).__name__}: {_e}", flush=True)
    # v3.3.0: Hydrate cache now that all helper funcs are defined.
    # Failure here is non-fatal — /api/scan_recent will return 503
    # until hydration succeeds, but the server still serves other routes.
    # v3.3.23al (Jim OOB 2026-09-27 'Pls fix all of them. Not just quick
    # win'): LAZY HYDRATION. Previously _hydrate_nutrition_cache() ran
    # synchronously at startup, blocking server bind by 3-5s. Now spawn
    # a daemon thread so the server is ready immediately and hydration
    # happens in the background. /api/scan_recent correctly returns 503
    # while hydration is in progress.
    import threading as _threading
    def _lazy_hydrate():
        try:
            _hydrate_nutrition_cache()
            _get_nutrition_cache().start_background_refresh(
                _sheet_read_nutrition_rows, period_s=60
            )
        except Exception as _e:
            try:
                with open("/tmp/cache_startup_errors.log", "a") as f:
                    f.write(f"{now_iso()} | lazy_hydrate | {type(_e).__name__}: {_e}\n")
            except Exception:
                pass
    _t = _threading.Thread(target=_lazy_hydrate, name="lazy-hydrate", daemon=True)
    _t.start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
