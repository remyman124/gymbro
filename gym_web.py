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
import json
import os
import re
import secrets
import urllib.request
import urllib.parse
import uuid
from datetime import datetime, timezone, timedelta, date
from datetime import datetime as _dt
from pathlib import Path

try:
    from PIL import Image, ImageOps
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

from flask import Flask, jsonify, request, render_template_string, send_from_directory
from workout_formatter import render as _render_text
from dotenv import load_dotenv
from gym_web.core import safe_read_json as _safe_read_json
from gym_web.idempotency import check_and_remember as _idempotency_check
from gym_web.cache import NutritionCache, NutritionRow, get_cache, get_cache as _get_nutrition_cache
from gym_web import image_proxy

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


@app.after_request
def add_no_cache_headers(response):
    """Jim OOB 2026-07-19: force no-cache so iPhone PWA picks up every code change immediately."""
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
    WORKOUT_LOG.write_text(json.dumps(log, indent=2, ensure_ascii=False))


def get_or_create_session():
    log = load_log()
    today = today_iso()
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
    elif "exercises" not in log[today]:
        log[today]["exercises"] = []
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
            "gym_web.core", "gym_web.whoop", "gym_web.withings",
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
        has_gym = any(a["sport"] == "weightlifting" for a in acts)
        if has_gym:
            gym_count += len([a for a in acts if a["sport"] == "weightlifting"])
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
            checks["whoop_cache_age_minutes"] = meta.get("sync_time_hkt_age_min", None)
        except Exception:
            checks["whoop_cache_age_minutes"] = "corrupt"
    # Withings
    withings_cache = Path("/home/work/.withings_latest_cache.json")
    checks["withings_cache"] = withings_cache.exists()
    # Google creds
    google_tok = Path("/home/work/.hermes/google_token.json")
    checks["google_token"] = google_tok.exists()
    # TG bot
    checks["telegram_bot_configured"] = bool(_os.environ.get("TELEGRAM_BOT_TOKEN")) or _os.path.exists("/home/work/.hermes/.env")
    # v3.3.2: removed `nutrition_log` + `food_scan_log` checks — the Sheet
    # is the canonical store now; those JSON files no longer exist.
    checks["workout_log"] = Path("/home/work/.whoop_workout_log.json").exists()
    # Health overlay API (composite)
    overall = all([
        checks.get("whoop_token"),
        checks.get("withings_cache"),
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
    set_n = data.get("set_n", 1)
    intensity = data.get("intensity") or detect_intensity(set_n)
    form_check = data.get("form_check", "ok")
    source = data.get("source", "gym-web-tap")

    if not exercise:
        return jsonify({"error": "exercise name required"}), 400

    session = get_or_create_session()
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
    log[today_iso()] = session
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
    log[today_iso()] = session
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
    log[today_iso()] = session
    save_log(log)
    return jsonify({"ok": True, "removed": removed, "remaining": len(session["exercises"])})


# ---------- Health overlay (Whoop recovery + Withings weight) — minimal 2 numbers ----------
WHOOP_CACHE = Path("/home/work/.whoop_data_latest.json")
WITHINGS_CACHE = Path("/home/work/.withings_latest_cache.json")

# gymbro PWA version — bump on every release
# v3.3.2: removed food_scan_log.json + nutrition_log.json + scan_cache +
# scan_thumb_cache from the WRITE path. Sheet + Drive are the only
# persistent store; cache hydrates from Sheet on boot.
__version__ = "3.3.5"


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
    return None


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


def _withings_body_latest():
    """Latest Withings body comp reading (any date, not just today).
    Returns dict {date, weight_kg, fat_pct, ...} or {} if none available.
    Falls back to most recent cache entry so Jim always sees his latest weigh-in.

    Jim OOB 2026-07-24: refresh cache from `withings.py body 7` every call so the
    latest 1-2 weeks readings are tried, then saved to WITHINGS_CACHE atomically.
    """
    # Step 1: try the cache first (fast path)
    d = _safe_read_json(WITHINGS_CACHE)
    body = d.get("body") if isinstance(d, dict) else None
    if isinstance(body, dict) and body.get("weight_kg"):
        return body
    # Step 2: refresh from Withings API by parsing the most recent body row.
    try:
        import subprocess, json as _json, re
        for window in (7, 14, 30, 90):
            r = subprocess.run(
                ["python3", "/home/work/.hermes/skills/withings/withings.py", "body", str(window)],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0 and r.stdout.strip():
                found = None
                for line in r.stdout.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 3 and re.match(r"\d{4}-\d{2}-\d{2}", parts[0]):
                        try:
                            found = {
                                "date": parts[0],
                                "weight_kg": float(parts[1]),
                                "fat_pct": float(parts[2]),
                            }
                            break  # got the most recent reading in this window
                        except (ValueError, IndexError):
                            continue
                if found is not None:
                    # Persist into the cache file so subsequent reads are fast.
                    try:
                        cur = _safe_read_json(WITHINGS_CACHE)
                        if not isinstance(cur, dict):
                            cur = {}
                        cur["body"] = found
                        cur["synced_at"] = now_iso()
                        tmp = str(WITHINGS_CACHE) + ".tmp"
                        Path(tmp).write_text(_json.dumps(cur, indent=2, ensure_ascii=False))
                        os.replace(tmp, str(WITHINGS_CACHE))
                    except Exception:
                        pass
                    return found
                # window returned no rows — try wider window
    except Exception:
        pass
    return {}


def _withings_weight():
    """Latest Withings weight in kg (any date). Jim OOB 2026-07-19: use latest, not just today."""
    body = _withings_body_latest()
    w = body.get("weight_kg")
    try:
        return round(float(w), 1) if w else None
    except (TypeError, ValueError):
        return None


def _withings_fat_pct():
    """Latest Withings body fat percentage (any date). Jim's goal: drive this down."""
    body = _withings_body_latest()
    f = body.get("fat_pct")
    try:
        return round(float(f), 1) if f else None
    except (TypeError, ValueError):
        return None


def _get_intraday_steps_today() -> dict:
    """Sum Withings intraday activity entries from HKT midnight to now.

    Returns dict {has_data: bool, steps, distance_km, calories}.
    If Withings has no intraday activity for today at all, returns
    {has_data: False} (caller should keep "syncing" signal — Rule 24
    NEVER FABRICATE: do NOT fall back to yesterday's number).

    v2.7.22 (Jim OOB 2026-08-02 19:48 HKT "step count is always syncing"):
    solves the case where iPhone HealthKit has pushed partial sync events
    to Withings but the daily-aggregation commit hasn't run yet.
    """
    try:
        import importlib as _il
        import sys as _sys
        if "/home/work/.hermes/skills/withings" not in _sys.path:
            _sys.path.insert(0, "/home/work/.hermes/skills/withings")
        withings_mod = _il.import_module("withings")
        get_intraday = withings_mod.get_intraday_activity
    except Exception:
        return {"has_data": False}

    try:
        from zoneinfo import ZoneInfo
        hkt = ZoneInfo("Asia/Hong_Kong")
    except Exception:
        from datetime import timezone as _tz, timedelta as _td
        hkt = _tz(_td(hours=8))

    now_hkt = datetime.now(hkt)
    hkt_midnight = now_hkt.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = int(hkt_midnight.astimezone(timezone.utc).timestamp())
    end_ts = int(datetime.now(timezone.utc).timestamp())

    # v2.7.23 FIX (verified 2026-08-03 14:00 HKT): Withings `getintradayactivity`
    # SILENTLY TRUNCATES earlier events when the window is < 24h. Empirical proof:
    #   12h window: 0 entries  |  16h window: 3 entries  |  24h window: 7 entries
    #   48h window: 99 entries (full backfill)  |  72h window: 58 entries
    # Use 48h window, then filter for ts >= hkt_midnight. This catches all today's
    # events even if Apple Watch pushed them hours ago.
    wider_start_ts = end_ts - 48 * 3600

    try:
        body = get_intraday(wider_start_ts, end_ts)
    except Exception:
        return {"has_data": False}

    series = (body or {}).get("series", {})
    if not series:
        return {"has_data": False}

    total_steps = 0
    total_dist = 0.0
    total_cal = 0.0
    n = 0
    for ts_str, entry in series.items():
        try:
            ts = int(ts_str)
        except (TypeError, ValueError):
            continue
        # Only count entries from HKT midnight onwards (Rule 24 honesty).
        if ts < start_ts:
            continue
        total_steps += entry.get("steps", 0) or 0
        total_dist += entry.get("distance", 0) or 0
        total_cal += entry.get("calories", 0) or 0
        n += 1

    if n == 0:
        return {"has_data": False}

    # v3.3.7: track the latest event ts in the series so the caller can
    # surface a Withings-side freshness stamp. NOTE: Withings `<unix_ts>`
    # keys are the *start of the measurement bucket*, not the iPhone
    # upload time — but they track within seconds of each other in
    # practice (HealthKit pushes near-real-time).
    latest_ts = max(
        (int(ts_str) for ts_str in series.keys() if str(ts_str).isdigit()),
        default=0,
    )

    return {
        "has_data": True,
        "steps": int(total_steps),
        "distance_km": round(total_dist / 1000, 2),
        "calories": round(total_cal, 1),
        "latest_event_ts": latest_ts,  # 0 if no events
    }


def _withings_steps_today() -> dict:
    """Today's Withings activity (steps / distance / calories).

    v2.7.25 CORRECT FIX (Jim OOB 2026-08-04 09:50 HKT "wait 6048 steps was ytd,
    not today"):

    PROBLEM: v2.7.24 showed 6048 as today because it returned the latest
    daily commit (yesterday's 8/3 final commit) when today's record
    was missing. That was a TRUTH VIOLATION — the user wants TODAY's
    running total, not yesterday's finalized number.

    CORRECT SEMANTICS (v2.7.25):
    1. TODAY's record only. If 8/4 is missing from getactivity, return
       honest signal — NEVER fall back to yesterday's number.
    2. If today record exists but has steps < 50 (boundary commit),
       cross-check with intraday for fresh events.
    3. If today record is missing AND intraday 48h window has fresh
       events from HKT midnight (Apple Watch partial sync), use intraday
       running total.
    4. If today is missing AND intraday is empty (Apple Watch truly
       hasn't synced since yesterday), return syncing: true.
    5. The iPhone Withings widget shows 6048 because it shows the latest
       KNOWN number regardless of date. gymbro must show TODAY's number
       even if 0, or syncing.

    v3.3.7 (Jim OOB 2026-08-17 'as-of timestamp should be earlier — Withings
    last sync was earlier but my actual step should have increased'): also
    return `data_as_of` reflecting Withings-side freshness, NOT when our
    cache was last written.
    - For `today_commit` source: Withings aggregates daily at HKT 04:00
      (the API exposes only `date: YYYY-MM-DD`, no per-record timestamp).
      Use HKT 04:00 of `today_str` as the data_as_of.
    - For intraday sources: use the latest event's `ts` from the intraday
      series (the unix_ts key).

    Returns dict {date, steps, distance_km, calories, _source, data_as_of_iso}.
    """
    import importlib
    from datetime import datetime, timezone, timedelta
    import time as _time

    # In-process 30s cache
    cache = _safe_read_json(WITHINGS_CACHE)
    cached_steps = (cache or {}).get("steps") or {}
    now_ts = _time.time()
    if isinstance(cached_steps, dict) and cached_steps.get("fetched_at_ts"):
        age = now_ts - (cached_steps.get("fetched_at_ts") or 0)
        if age < 30 and cached_steps.get("steps") is not None:
            return {
                "date": cached_steps.get("date", ""),
                "steps": cached_steps.get("steps"),
                "distance_km": cached_steps.get("distance_km"),
                "calories": cached_steps.get("calories"),
                "_source": cached_steps.get("_source", "cache"),
            }

    # In-process import
    try:
        import sys as _sys
        if "/home/work/.hermes/skills/withings" not in _sys.path:
            _sys.path.insert(0, "/home/work/.hermes/skills/withings")
        withings_mod = importlib.import_module("withings")
        get_activity = withings_mod.get_daily_activity
    except Exception:
        return {}

    hkt = timezone(timedelta(hours=8))
    today_str = datetime.now(hkt).strftime("%Y-%m-%d")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)

    # Pull 7 days of daily activity for context (but only TODAY matters).
    try:
        daily_records = get_activity(start, end)
    except Exception:
        return {}

    # v2.7.25: ONLY today's record is truth. If missing, honest signal.
    today_record = None
    for d in daily_records:
        if d.get("date") == today_str:
            today_record = d
            break

    if today_record:
        try:
            today_steps = int(today_record.get("steps") or 0)
        except (TypeError, ValueError):
            today_steps = 0

        # Cross-check with intraday for fresh partial sync
        intraday_today = _get_intraday_steps_today()
        intraday_today_steps = int(intraday_today.get("steps") or 0) if intraday_today.get("has_data") else 0
        intraday_latest_ts = intraday_today.get("latest_event_ts") or 0

        if intraday_today_steps > today_steps:
            # Intraday has more events than today's daily commit
            chosen = {
                "date": today_str,
                "steps": intraday_today_steps,
                "distance_m": (intraday_today.get("distance_km") or 0) * 1000,
                "calories": intraday_today.get("calories", 0),
                "_source": "intraday_override",
            }
            chosen_source = "intraday_override"
            # v3.3.7: data_as_of = latest intraday event ts (genuine)
            data_as_of_ts = intraday_latest_ts
        else:
            chosen = today_record
            chosen_source = "today_commit"
            # v3.3.7: ONLY use intraday_latest_ts when genuine iPhone sync
            # events exist. NEVER fabricate HKT 04:00 — that's a daily
            # commit timestamp, NOT the time iPhone pushed data, and
            # showing it as "as of 04:00" was misleading (Jim OOB
            # 2026-08-17 '04:00 is a bug'). If no intraday events, hide
            # the as-of label rather than guess.
            data_as_of_ts = intraday_latest_ts or 0
    else:
        # v2.7.25: NO today record. Try intraday; if empty, honest syncing.
        # NEVER show yesterday's value as today.
        intraday_today = _get_intraday_steps_today()
        if intraday_today.get("has_data") and intraday_today.get("steps", 0) > 0:
            chosen = {
                "date": today_str,
                "steps": intraday_today["steps"],
                "distance_m": (intraday_today.get("distance_km") or 0) * 1000,
                "calories": intraday_today.get("calories", 0),
                "_source": "intraday_only",
            }
            chosen_source = "intraday_only"
            # v3.3.7: genuine as-of from latest intraday event
            data_as_of_ts = intraday_today.get("latest_event_ts") or 0
        else:
            # Honest signal: today has no data, do NOT fall back to yesterday.
            return {
                "date": today_str,
                "steps": None,
                "distance_km": None,
                "calories": None,
                "syncing": True,
                "_source": "no_today_record",
            }

    # Extract values
    try:
        steps = int(chosen.get("steps") or 0)
    except (TypeError, ValueError):
        steps = 0
    distance_m = float(chosen.get("distance_m") or 0)
    calories = float(chosen.get("calories") or 0)
    record_date = today_str  # always today, never yesterday

    out = {
        "date": record_date,
        "steps": steps,
        "distance_km": round(distance_m / 1000, 2),
        "calories": round(calories, 1),
        "_source": chosen_source,
        # v3.3.7: Withings-side freshness (vs. cache write time = pulled_at).
        # Only set when an intraday event ts is available — i.e., a real
        # iPhone-side push. None when only the daily commit exists (we
        # don't fabricate HKT 04:00 — Jim OOB 2026-08-17 '04:00 is a bug').
        "data_as_of_ts": data_as_of_ts,
        "data_as_of_iso": (
            datetime.fromtimestamp(data_as_of_ts, tz=timezone.utc).isoformat()
            if data_as_of_ts else None
        ),
    }

    # Atomic write to cache
    try:
        cur = _safe_read_json(WITHINGS_CACHE) or {}
        if not isinstance(cur, dict):
            cur = {}
        cur["steps"] = {
            **out,
            "fetched_at_ts": now_ts,
            "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
        }
        tmp = str(WITHINGS_CACHE) + ".tmp"
        Path(tmp).write_text(json.dumps(cur, indent=2, ensure_ascii=False))
        os.replace(tmp, str(WITHINGS_CACHE))
    except Exception:
        pass
    return out


def _withings_yesterday() -> dict:
    """Yesterday's Withings daily commit (finalized value).

    v2.7.26 (Jim OOB 2026-08-04 09:55 HKT "perhaps show both yesterday and
    today record in the widget. try to squeeze two data into one widget
    but today one is larger").

    Returns dict {date, steps, distance_km, calories} or {} on no data.
    """
    import importlib
    from datetime import datetime, timezone, timedelta

    hkt = timezone(timedelta(hours=8))
    today_hkt = datetime.now(hkt)
    yesterday_str = (today_hkt - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        import sys as _sys
        if "/home/work/.hermes/skills/withings" not in _sys.path:
            _sys.path.insert(0, "/home/work/.hermes/skills/withings")
        withings_mod = importlib.import_module("withings")
        get_activity = withings_mod.get_daily_activity
    except Exception:
        return {}

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=2)  # yesterday falls in this window
    try:
        records = get_activity(start, end)
    except Exception:
        return {}

    for d in records:
        if d.get("date") == yesterday_str:
            try:
                steps = int(d.get("steps") or 0)
            except (TypeError, ValueError):
                steps = 0
            distance_m = float(d.get("distance_m") or 0)
            calories = float(d.get("calories") or 0)
            return {
                "date": yesterday_str,
                "steps": steps,
                "distance_km": round(distance_m / 1000, 2),
                "calories": round(calories, 1),
            }
    return {}


@app.route("/api/health_overlay")
def api_health_overlay():
    """Single endpoint for the hero overlay.
    - Top-left: Whoop recovery %
    - Top-right: Withings weight kg + fat % (latest reading, drives Jim's goal)
    - Steps: TODAY (large) + YESTERDAY (small) — paired widget
    """
    steps = _withings_steps_today() or {}
    yest = _withings_yesterday() or {}
    return jsonify({
        "recovery": _recovery_pct(),
        "weight_kg": _withings_weight(),
        "fat_pct": _withings_fat_pct(),
        "weight_date": (_withings_body_latest() or {}).get("date"),
        "steps_today": steps.get("steps"),
        "steps_date": steps.get("date"),
        "steps_syncing": steps.get("syncing", False),
        "distance_km_today": steps.get("distance_km"),
        "calories_today": steps.get("calories"),
        "steps_yesterday": yest.get("steps"),
        "distance_km_yesterday": yest.get("distance_km"),
        "calories_yesterday": yest.get("calories"),
    })



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
    """Format jim_context as a string block for the cheer pplx prompt.

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


@app.route("/api/nutrition/today")
def api_nutrition_today():
    """Return today's nutrition: per-meal list + totals + last_meal_ts.

    Used by gym_web PWA nutrition tab + cheer pipeline. iPhone PWA can
    also POST to /api/food_scan (existing) which writes to the same log
    this endpoint reads.

    v3.3.0: reads from in-memory NutritionCache instead of nutrition_log.json.
    """
    cache = _get_nutrition_cache()
    if not cache.is_ready():
        return jsonify({"ok": False, "error": "cache hydrating"}), 503
    today = today_iso()
    rows = cache.get_by_date(today)
    meals = []
    totals = {"kcal": 0.0, "P": 0.0, "C": 0.0, "F": 0.0}
    for r in rows:
        d = r.to_pwa_dict()
        # Match legacy meal schema so cheer pipeline stays unchanged
        meals.append({
            "date": r.date,
            "time": r.time,
            "meal_type": r.meal or "scan",
            "meal_name": r.name,
            "name": r.name,
            "dish_name": r.name,
            "calories": r.kcal,
            "protein": r.p,
            "carbs": r.c,
            "fat": r.f,
            "restaurant_chain": r.restaurant,
            "timestamp_iso": (r.dt.isoformat() if r.dt else "") + "+08:00",
            "drive_image_url": r.drive_image_url,
        })
        totals["kcal"] += r.kcal
        totals["P"] += r.p
        totals["C"] += r.c
        totals["F"] += r.f
    totals = {k: round(v, 1) for k, v in totals.items()}
    last_meal_ts = None
    if rows:
        last = rows[0]  # get_by_date returns time-DESC, newest first
        if last.dt:
            last_meal_ts = f"{last.date}T{last.time}:00+08:00"
    return jsonify({
        "date": today,
        "fetched_at": now_iso(),
        "meal_count": len(meals),
        "totals": totals,
        "last_meal_ts": last_meal_ts,
        "meals": meals,
        "cache": cache.stats(),
    })


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
        "description": "Quick gym workout logger with Whoop + Withings overlay",
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
    """Refresh Google OAuth access token using stored refresh_token."""
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
    """Return all rows from the Nutrition tab (A:K = 11 cols)."""
    access = _get_google_access_token()
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{NUTRITION_SHEET_ID}/values/"
        f"{NUTRITION_SHEET_TAB}!A1:K"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {access}"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
    return body.get("values", [])


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
                if set_n is None:
                    continue
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


# ---------- v2.1 FOOD SCAN (MiniMax M3 vision + pplx enrichment) ----------
# Jim OOB 2026-07-23 22:26 HKT: "Version will be able to scan food or food receipt
# to capture. Using MiniMax image recognition and pplx search"

SCAN_CACHE_DIR = Path("/home/work/.hermes/scan_cache")
SCAN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
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

# pplx API key (separate from MiniMax which is in hermes-torres)
def _pplx_api_key() -> str:
    return os.environ.get("PPLX_API_KEY", "")


def _get_latest_news_for_cheer() -> str:
    """Pull 1-2 fresh HK / sports / lifestyle news bits for the cheer prompt.
    Jim OOB 2026-07-24: 'use latest news to make it more innovative and funny'.
    Jim OOB 2026-07-29: random-pick 1 of 4 categories (gossip / sports / AI / hacker news).
    Returns a short Chinese string or '' on failure.
    """
    import random
    key = _pplx_api_key()
    if not key:
        return ""
    # Jim OOB 2026-07-29: random-pick 1 of 4 categories
    categories = [
        ("gossip", "娛樂圈 / 明星 / 藝人 gossip — 香港或國際明星新聞, 限本週 2026 年 7 月 22 至 29 日內發生。簡短 30-50 字繁中廣東話回覆, 可以用嚟做 cheer 收尾嘅有趣話題。唔好 fabricate, 唔肯定就講 \"(本週未有確認熱話)\"。"),
        ("sports", "體育 / 足球 / NBA / 網球 / 賽車 — 國際體育新聞, 限本週內發生。簡短 30-50 字繁中廣東話回覆。唔好 fabricate, 唔肯定就講 \"(本週未有確認熱話)\"。"),
        ("ai", "AI / 人工智能 / 大模型 / 生成式 AI — 業界新聞, 例如新模型發佈、產品更新、AI 政策。限本週內發生。簡短 30-50 字繁中廣東話回覆。唔好 fabricate, 唔肯定就講 \"(本週未有確認熱話)\"。"),
        ("hacker", "黑客 / 資訊安全 / 漏洞 / 數據外洩 / 開源工具新聞。限本週內發生。簡短 30-50 字繁中廣東話回覆。唔好 fabricate, 唔肯定就講 \"(本週未有確認熱話)\"。"),
    ]
    cat_name, cat_prompt = random.choice(categories)
    try:
        payload = {
            "model": "sonar-pro",
            "messages": [{"role": "user", "content": f"幫我搵 1 則最新嘅 {cat_prompt}"}],
            "max_tokens": 250,
            "temperature": 0.7,
        }
        req = urllib.request.Request(
            "https://api.perplexity.ai/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bear" + "er " + key,
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        content = (resp.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
        # Prepend category tag for cheer to reference
        cat_label = {"gossip": "🎭 明星娛樂", "sports": "⚽ 體育", "ai": "🤖 AI", "hacker": "💻 黑客"}.get(cat_name, "📰")
        return f"【{cat_label}】{content}" if content else ""
    except Exception:
        return ""


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
    key = _pplx_api_key()
    if not key:
        return ""
    try:
        payload = {
            "model": "sonar-pro",
            "messages": [{"role": "user", "content":
                "利物浦 (Liverpool FC) 下一場英超賽事係幾時? 對手係邊隊? 喺主場定作客?"
                "用繁中廣東話一句 30-50 字回覆, 例如「下週六 (7月25日) 主場對曼聯」, "
                "如果 7 日內冇賽事就答「7 日內冇利物浦比賽」。唔好 fabricate, 唔肯定就講「未確認」。"}],
            "max_tokens": 200,
            "temperature": 0.3,
        }
        req = urllib.request.Request(
            "https://api.perplexity.ai/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bear" + "er " + key,
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        content = (resp.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
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


def _apiyi_api_key() -> str:
    """Read APiyi key from env (loaded from .hermes/.env at startup).
    APiyi = OpenAI-compatible proxy → can call ChatGPT gpt-4o / gpt-4o-mini.
    Jim OOB 2026-07-25 17:12 HKT: 'Not openrouter using apiyi'.
    Verified live 2026-07-27 08:35 HKT: https://api.apiyi.com/v1/chat/completions
    with model gpt-4o-mini returns valid ChatGPT response.
    """
    return os.environ.get("APIYI_API_KEY", "")


# Canonical 12-field nutrition schema (Jim OOB 2026-07-25 13:35 HKT:
# "food recognition buggy, doesn't gather protein, carbs, fiber etc").
# Used by both pplx and apiyi enrich prompts + parser + Sheet header.
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


def _minimax_vision(img_b64: str, prompt: str) -> str:
    """Vision description.

    Jim OOB 2026-08-09: MiniMax api.minimax.io has zero vision models
    (all 'unknown model' HTTP 400). APiyi gpt-4o-mini triggers safety
    on brand-logo images (NOC coffee cup = blocked). APiyi gpt-4o works
    on the same images — it actually describes the photo.

    Kept the _minimax_vision function name for backward compat with 5
    call sites — internally delegates to _apiyi_vision_gpt4o.
    """
    result = _apiyi_vision_gpt4o(img_b64, prompt)
    if result:
        return result
    # Return empty so _extract_dish_name falls through to fallback; do NOT
    # return "（Vision 服務暫時不可用）" — that string is a generic label
    # and would itself become the dish name (Jim OOB 2026-08-09 16:48 HKT).
    return ""


def _apiyi_vision_analyze(img_b64: str, prompt: str) -> str:
    """2nd-opinion vision via APiyi gpt-4o-mini (Jim OOB 2026-07-26 19:35 HKT).
    Returns description text, or "" if key missing → graceful fallback.
    Median-merge logic in _merge_nutrition_estimates handles 1 vs 2 vision sources.
    """
    api_key = _apiyi_api_key()
    if not api_key:
        return ""
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
        ]}],
        "max_tokens": 1200,
        "temperature": 0.25,
    }
    try:
        req = urllib.request.Request(
            "https://api.apiyi.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bear" + "er " + api_key,
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=90).read())
        return resp["choices"][0]["message"]["content"]
    except Exception as e:
        return f"（APiyi vision 失敗：{type(e).__name__}）"


def _apiyi_vision_gpt4o(img_b64: str, prompt: str) -> str:
    """Primary vision via APiyi gpt-4o (Jim OOB 2026-08-09).

    Why gpt-4o not gpt-4o-mini: gpt-4o-mini triggers blanket safety filter
    on brand-logo images (e.g. NOC coffee cup, branded packaging) and
    returns "抱歉，我無法協助處理該請求" with zero description. gpt-4o
    on the same image returns an actual food description.

    Returns description text, or "" on any failure.
    """
    api_key = _apiyi_api_key()
    if not api_key:
        return ""
    payload = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
        ]}],
        "max_tokens": 1200,
        "temperature": 0.25,
    }
    try:
        req = urllib.request.Request(
            "https://api.apiyi.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bear" + "er " + api_key,
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=90).read())
        content = resp["choices"][0]["message"]["content"]
        # Detect safety-filter refusal (gpt-4o has lower rate but can still
        # trigger on certain images); treat as failure so fallback runs.
        if content and ("抱歉" in content[:30] and "無法" in content[:50] and len(content) < 80):
            return ""
        return content
    except Exception as e:
        return f"（APiyi gpt-4o vision 失敗：{type(e).__name__}）"


def _pplx_enrich(dish_desc: str) -> str:
    """Call pplx sonar-pro for nutrition enrichment of described dishes.

    Jim OOB 2026-07-25 13:35: ask for FULL 12-field nutrition (kcal, P, C, F,
    fiber, sugar, sodium, sat_fat, trans_fat, vit_c, iron, calcium), not just
    P/C/F/kcal. Used as 1st of 2 estimates (median-merged with OpenRouter).
    """
    api_key = _pplx_api_key()
    if not api_key:
        return "（PPLX 金鑰未設定）"
    fields_desc = "\n".join(
        f"- {f} ({NUTRITION_UNITS[f]})"
        for f in NUTRITION_FIELDS
    )
    prompt = (
        f"由以下香港/廣東話食物描述：\n\n「{dish_desc}」\n\n"
        "幫我做兩件事：\n"
        "1. 識別每樣菜式所屬嘅餐廳/連鎖/品牌（如：沙嗲王、KFC、大家樂、太興、添好運等），"
        "列出每樣嘅 standard portion / 標準份量。\n"
        "2. **必須**用以下 exact format, 每個 field 一行, field 喺 value 之前用全形冒號「：」:\n"
        f"{fields_desc}\n\n"
        "格式範例 (要跟足, 一個 field 一行, 唔好 narrative):\n"
        "卡路里：750\n蛋白質：38g\n碳水化合物：80g\n脂肪：25g\n膳食纖維：4g\n糖：6g\n鈉：950mg\n飽和脂肪：8g\n反式脂肪：0.5g\n維他命C：12mg\n鐵質：3mg\n鈣質：80mg\n\n"
        "重要：\n"
        "- 唔可以全部 12 個 field 都寫 0。就算餐廳 brand 唔識, 都要用「香港典型常見份量」estimate 落每個 field。\n"
        "- 例如：燒味例牌 → 用一般燒味店例牌 ~200g meat + 少飯約 450kcal / 35g P / 30g C / 22g F estimate; 蔬菜 ~120kcal; 炒飯例牌 ~600kcal / 18g P / 80g C / 22g F; 燒雞半隻 ~1100kcal / 70g P / 0g C / 75g F; 燒乳鴿一隻 ~250kcal / 25g P / 0g C / 15g F; 揚州炒飯例牌 ~700kcal / 22g P / 90g C / 28g F; 馬黛茶 ~5kcal / 0g P / 1g C / 0g F; 烚豬肉 ~250kcal / 25g P / 0g C / 16g F; 唐生菜/菜類 ~80-120kcal / 2-4g P。\n"
        "- 只有「明確零熱量」嘅嘢 (例如清水、黑咖啡、齋 tea、汽水 diet) 先可以 calories=0; 否則全部要有 non-zero estimate。\n"
        "- 飽和脂肪/反式脂肪如果係自煮 dish 一般 0.5-2g, 快餐先有高值 (3-10g)。\n"
        "- 維他命 C / 鐵質 / 鈣質係 micronutrient, 主要喺菜類/肉類, 用典型 baseline estimate (菜 10-30mg vitC, 紅肉 2-3mg iron, 乳/小骨魚 80-200mg calcium)。\n"
        "- sodium 用 mg (1g 鹽 ≈ 400mg sodium); 中式例牌 sodium 普遍 400-1000mg。\n"
        "- 餐廳 chain / 標準份量可以 narrative 寫, 但 12 個 nutrition field 必須 strict format\n\n"
        "3. 如有 brand-specific nutrition 數據（例如 KFC 雞件卡路里），用嗰啲 official 數。"
        "如無 brand-specific 數, 請用一般常見 portion。\n\n"
        "用繁體中文, 一個英文字都唔好有。餐廳裝修、裝飾、其他餐廳 唔好講。"
    )
    payload = {
        "model": "sonar-pro",
        "messages": [
            {"role": "system", "content": "你係香港連鎖餐廳 nutrition 查詢助手。用事實同官方數據回答, 唔好幻想, 唔識就寫 0。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 2400,
        "temperature": 0.2,
    }
    try:
        req = urllib.request.Request(
            "https://api.perplexity.ai/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bear" + "er " + api_key,
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        return resp["choices"][0]["message"]["content"]
    except Exception as e:
        return f"（PPLX enrichment 失敗：{type(e).__name__}）"


def _apiyi_nutrition_enrich(dish_desc: str) -> str:
    """v3.2.7.12: 12-field nutrition enrichment via MiniMax M3 (Jim OOB
    2026-08-08 20:55 HKT 'Use more minimax m3 ... Don't use gpt except
    food recognition dual scanning'). MiniMax is the canonical LLM —
    single source of truth for nutrition estimates + coach comments.

    v3.2.7.6+: MiniMax M3 via api.minimax.io. Returns text (or empty on
    failure) — caller parses via _parse_nutrition_block then median-merges
    with pplx.
    """
    api_key = _minimax_api_key()
    if not api_key:
        return ""  # graceful fallback — single-source pplx only
    fields_desc = ", ".join(
        f"{f} ({NUTRITION_UNITS[f]})" for f in NUTRITION_FIELDS
    )
    prompt = (
        f"Estimate per-portion nutrition for this HK/Cantonese food description:\n\n"
        f"「{dish_desc}」\n\n"
        f"Reply with ONLY a single JSON object, no markdown, no commentary. "
        f"Schema (all 12 fields required, use 0 if unknown, do NOT fabricate):\n"
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
            {"role": "system", "content": "You are a nutrition fact checker. Output ONLY valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 400,
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
        return resp["choices"][0]["message"]["content"]
    except Exception as e:
        return f"（APiyi enrichment 失敗：{type(e).__name__}）"


def _apiyi_nutrition_enrich_multi(dish_desc: str) -> str:
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
        return resp["choices"][0]["message"]["content"]
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
        # No API key → fail closed: assume narration so we don't auto-commit
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
        # Fail closed: if the AI can't decide, refuse to auto-commit.
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
            label_words = {
                "菜式", "菜名", "主菜", "副菜", "前菜", "甜品", "湯品",
                "飲品", "煮法", "份量", "份量大小", "份量（目測）",
                "詳細描述", "菜單", "餐牌", "品牌", "餐廳", "套餐",
                "菜式：", "菜名：", "未識別", "未能識別",
            }
            # v3.2.7.32: safety net — if M3 emitted a `進思考` block but
            # never closed (max_tokens cut off mid-think), discard the
            # partial think instead of saving narration as the dish.
            if "進思考" in dish:
                return ""
            if dish in label_words or not dish or len(dish) > 16:
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
                    return ""
                return dish2
            if 2 <= len(dish) <= 16:
                return dish
    except Exception:
        pass

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

    # Comment
    if final_grade == pre_grade:
        pre_comment = "整體均衡，可以接受。"
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
    """
    out = {"meals": [], "totals": {"kcal": 0, "P": 0, "C": 0, "F": 0}, "meal_count": 0, "last_meal_ts": None}
    cache = get_cache()
    if not cache.is_ready():
        return out
    today = today_iso()
    rows = cache.get_by_date(today)
    meals = []
    for r in rows:
        d = r.to_pwa_dict()
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


def _make_thumb(src: Path, dst: Path, max_side: int = 480) -> str:
    """Generate a JPEG thumbnail of `src` at `dst`. Returns the dst path string, or "" on failure.

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


def _append_to_sheet_nutrition(entry: dict) -> dict:
    """Mirror entry to Google Sheet Nutrition tab (sheetId 474877075).
    Returns {"ok": bool, "range": str} — silent on quota/error.

    Jim OOB 2026-07-29: dedup by (date, time_short, calories) BEFORE push.
    Prevents cron double-push from creating duplicates.
    Also marks entry.sheet_synced=True on success.
    """
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
        SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
        try:
            url_check = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition!A1:Z1000?valueRenderOption=FORMATTED_VALUE"
            req_check = urllib.request.Request(url_check, headers={"Authorization": f"Bearer {access}"})
            existing = json.loads(urllib.request.urlopen(req_check, timeout=10).read()).get("values", [])
            entry_date = entry.get("date", today_iso())
            entry_time = (entry.get("time", "00:00") or "00:00")[:5]
            # v3.2.7.48: keep dedup's cal signature consistent with the
            # clamped integer that will actually be written to the sheet.
            entry_cal = str(int(round(_safe_num(entry.get("calories", 0), default=0.0, lo=0, hi=3000))))
            for row in existing[1:]:
                if not row:
                    continue
                row_date = row[0]
                row_time_full = row[1] if len(row) > 1 else ''
                row_time = row_time_full[11:16] if 'T' in row_time_full else row_time_full[:5]
                row_cal = row[5] if len(row) > 5 else ''
                if row_date == entry_date and row_time == entry_time and row_cal == entry_cal:
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
        # v3.2.7.48: persistent idempotency check (Jim OOB 2026-08-14 audit).
        # The in-sheet dedup above is weakened by calories=0 vision failures
        # and by silent network blips, so add a second line of defense that
        # survives restarts. The key is the entry's stable identity as
        # written to the sheet — if we already committed this row, skip.
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

    # 1b. APiyi gpt-4o-mini vision 2nd-opinion (Jim OOB 2026-07-26 19:35 HKT)
    apiyi_vision_desc = _apiyi_vision_analyze(img_b64, vision_prompt)
    # v3.2.7.14: detect gpt-4o-mini safety-filter refusal (e.g. "抱歉，我無法...")
    # and treat as empty so it does NOT pollute vision_desc (Jim OOB
    # 2026-08-09 16:48 HKT 'I took a screenshot not food, but scan got
    # committed as 抱歉 / 第一道菜').
    if apiyi_vision_desc and ("抱歉" in apiyi_vision_desc[:30] and "無法" in apiyi_vision_desc[:50] and len(apiyi_vision_desc) < 80):
        apiyi_vision_desc = ""
    if apiyi_vision_desc and not apiyi_vision_desc.startswith("（"):
        # 2-vision median-merge: take whichever is longer (more dish detail)
        if len(apiyi_vision_desc) > len(vision_desc) * 0.7:
            vision_desc = vision_desc + "\n\n（ChatGPT 2nd-opinion vision）\n" + apiyi_vision_desc

    # 2. pplx enrichment (1st of 2 estimates for 12-field nutrition)
    pplx_desc = _pplx_enrich(vision_desc)

    # 2b. OpenRouter gpt-4o-mini (2nd estimate, JSON mode, Jim OOB 2026-07-25 13:35)
    apiyi_desc = _apiyi_nutrition_enrich(vision_desc)

    # 2c. Parse both → merge via median (12-field schema)
    pplx_parsed = _parse_nutrition_block(pplx_desc)
    # OpenRouter returns JSON string, parse differently
    apiyi_parsed = {}
    if apiyi_desc and apiyi_desc.startswith("{"):
        try:
            apiyi_parsed = json.loads(apiyi_desc)
        except Exception:
            apiyi_parsed = _parse_nutrition_block(apiyi_desc)
    merged_nutrition = _merge_nutrition_estimates([pplx_parsed, apiyi_parsed])
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
        "apiyi_enrichment": apiyi_desc[:500] if apiyi_desc else "",
        "apiyi_json_parsed": apiyi_parsed,
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
        "source": "v2.11-scan (minimax-m3 + pplx-sonar-pro + apiyi-gpt-4o-mini, median-merged)",
        "models_used": ["minimax-m3", "pplx-sonar-pro", "apiyi/gpt-4o-mini"],
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
            range_str = sheet_result.get("range", "")
            new_row_index = int(range_str.split(":")[0].rstrip("ABCDEFGHIJKLM"))
            get_cache().refresh_one(new_row_index, _sheet_read_nutrition_row)
        except Exception:
            pass

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
@app.route("/api/withings_steps_today", methods=["GET"])
def api_withings_steps_today():
    """Return today's Withings step count + distance + calories + yesterday.

    v2.7.26 (Jim OOB 2026-08-04 09:55 HKT "show both yesterday and today in
    widget, today larger"): paired response so the frontend widget can show
    today (large) + yesterday (small) side-by-side.

    Jim OOB 2026-08-02 02:44 HKT: when Withings has no today record,
    expose `syncing: true` so the UI can show "—" + 同步中 rather than
    freezing yesterday's number as today's count.
    """
    try:
        steps_data = _withings_steps_today() or {}
        yest_data = _withings_yesterday() or {}
        raw_steps = steps_data.get("steps")
        # v3.3.7 (Jim OOB 2026-08-17 「step count is always lagging because my
        # iphone is not updated to withings cloud yet」): expose two
        # timestamps so the UI can show "as of HH:MM" with the GENUINE
        # Withings-side data freshness:
        #   - pulled_at: when gymbro fetched from Withings cloud (UI ignores)
        #   - data_as_of: latest Withings intraday bucket-start ts (= what
        #     iPhone Withings widget shows). None when no fresh intraday
        #     events exist — never fall back to pulled_at (Jim OOB
        #     2026-08-17 'gymbro showing 13:52 but iPhone shows 12:10 WRONG':
        #     pulled_at was being used as data_as_of fallback, masking the
        #     real iPhone-side lag).
        withings_cache = _safe_read_json(WITHINGS_CACHE) or {}
        cache_entry = (withings_cache.get("steps") or {}) if isinstance(withings_cache, dict) else {}
        pulled_at_iso = cache_entry.get("fetched_at_iso")
        return jsonify({
            "date": steps_data.get("date", ""),
            "steps": None if raw_steps is None else int(raw_steps or 0),
            "distance_km": None if steps_data.get("distance_km") is None and steps_data.get("syncing") else steps_data.get("distance_km"),
            "calories": None if steps_data.get("calories") is None and steps_data.get("syncing") else steps_data.get("calories"),
            "syncing": bool(steps_data.get("syncing", False)),
            # v3.3.7: cache write time (gymbro-side). UI ignores this.
            "pulled_at": pulled_at_iso,
            # v3.3.7: Withings-side data freshness (the truthful as-of).
            # Only set when Withings has genuine intraday bucket data.
            # None when only the daily commit exists — UI hides the label
            # entirely rather than show a misleading time.
            "data_as_of": steps_data.get("data_as_of_iso"),
            "_source": steps_data.get("_source"),
            # v2.7.26: paired yesterday for widget display
            "yesterday": {
                "date": yest_data.get("date", ""),
                "steps": yest_data.get("steps"),
                "distance_km": yest_data.get("distance_km"),
                "calories": yest_data.get("calories"),
            } if yest_data else None,
        })
    except Exception as e:
        return jsonify({"steps": 0, "distance_km": 0, "calories": 0, "syncing": False, "error": str(e)[:120]})


# v2.7.40: Force-refresh Withings steps (Jim OOB 2026-08-06: "tap shoes → refresh
# steps from withings"). Calls the withings.py activity script to fetch fresh
# data, refreshes WITHINGS_CACHE atomically, then returns same shape as
# /api/withings_steps_today. Designed to be fast (~3-5s) for tap-to-refresh UX.
@app.route("/api/withings_refresh", methods=["POST", "GET"])
def api_withings_refresh():
    """Force-refresh Withings step data by calling withings.py activity getactivity
    and update WITHINGS_CACHE. Returns {ok, steps, distance_km, calories, syncing, pulled_at}."""
    import subprocess, json as _json
    try:
        # Step 1: call withings.py steps 1 (today's step count, fresh from API)
        r = subprocess.run(
            ["python3", "/home/work/.hermes/skills/withings/withings.py", "steps", "1"],
            capture_output=True, text=True, timeout=30,
        )
        pulled_at = now_iso()
        new_steps = None
        new_distance = None
        new_calories = None
        if r.returncode == 0 and r.stdout.strip():
            # Parse tabular output: "2026-08-06      3,113     2.32km      708kcal        0m"
            import re
            from datetime import datetime, timezone, timedelta
            hkt = timezone(timedelta(hours=8))
            today_hkt_str = datetime.now(hkt).strftime("%Y-%m-%d")
            # Walk all rows; prefer today's row, fall back to most recent
            today_row = None
            latest_row = None
            for line in r.stdout.strip().splitlines():
                m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+([\d,]+)\s+([\d.]+)km\s+(\d+)kcal", line)
                if not m:
                    continue
                row = (m.group(1), int(m.group(2).replace(",", "")), float(m.group(3)), int(m.group(4)))
                if row[0] == today_hkt_str:
                    today_row = row
                    break  # found today, use it
                if latest_row is None or row[0] > latest_row[0]:
                    latest_row = row
            chosen = today_row or latest_row
            if chosen:
                new_steps = chosen[1]
                new_distance = chosen[2]
                new_calories = chosen[3]
        # Step 2: update WITHINGS_CACHE so subsequent /api/withings_steps_today
        # returns fresh data
        try:
            cur = _safe_read_json(WITHINGS_CACHE)
            if not isinstance(cur, dict):
                cur = {}
            cur["_last_forced_refresh"] = pulled_at
            if new_steps is not None:
                # Save under a "today" key so steps_today picks it up
                from datetime import datetime, timezone, timedelta
                hkt = timezone(timedelta(hours=8))
                today_hkt = datetime.now(hkt).strftime("%Y-%m-%d")
                cur.setdefault("activities", {})
                cur["activities"][today_hkt] = {
                    "steps": new_steps, "distance_km": new_distance, "calories": new_calories,
                    "syncing": False, "source": "forced_refresh",
                }
                cur["_today_override"] = {"date": today_hkt, "steps": new_steps, "distance_km": new_distance, "calories": new_calories}
            tmp = str(WITHINGS_CACHE) + ".tmp"
            Path(tmp).write_text(_json.dumps(cur, indent=2, ensure_ascii=False))
            os.replace(tmp, str(WITHINGS_CACHE))
        except Exception as cache_err:
            print(f"[withings_refresh] cache update failed: {cache_err}")
        # Step 3: return fresh step data (caller can use this OR re-fetch /api/withings_steps_today)
        return jsonify({
            "ok": True,
            "steps": new_steps,
            "distance_km": new_distance,
            "calories": new_calories,
            "pulled_at": pulled_at,
            "stdout_preview": (r.stdout or "")[:300],
            "stderr_preview": (r.stderr or "")[:200] if r.returncode != 0 else "",
        })
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "withings.py timeout (>30s)"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 500


@app.route("/api/withings_steps_7d_avg", methods=["GET"])
def api_withings_steps_7d_avg():
    """Return 7-day average steps (Jim OOB 2026-07-29)."""
    try:
        cache = _safe_read_json(WITHINGS_CACHE) or {}
        # If cache has full 7d activities, use those; else fall back to whoop walk estimate
        acts = cache.get("activities_30d") or cache.get("activities") or []
        if not acts:
            # Try to compute from cache directly
            steps_list = []
            for k, v in cache.items():
                if isinstance(v, dict) and 'steps' in v and isinstance(v['steps'], (int, float)):
                    steps_list.append(v['steps'])
            if steps_list:
                return jsonify({"avg": int(sum(steps_list) / len(steps_list)), "samples": len(steps_list)})
            return jsonify({"avg": 0, "samples": 0})
        # Get last 7 daily records
        from datetime import datetime, timezone, timedelta
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        # Filter last 7 days
        recent = acts[-7:] if isinstance(acts, list) else []
        steps_vals = []
        for a in recent:
            if isinstance(a, dict):
                s = a.get("steps", 0)
                if isinstance(s, (int, float)):
                    steps_vals.append(s)
        avg = int(sum(steps_vals) / max(1, len(steps_vals))) if steps_vals else 0
        return jsonify({"avg": avg, "samples": len(steps_vals)})
    except Exception as e:
        return jsonify({"avg": 0, "samples": 0, "error": str(e)[:120]})


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

    # v3.3.7: if user supplied a name but left macros empty AND the
    # current row's macros are all 0 (vision AI failed earlier), look up
    # similar dish nutrition from Sheet history and backfill. Jim OOB
    # 2026-08-16 'why today egg tarts have nutrient info?' — row 164.
    sup_name = (data.get("name") or "").strip()
    sup_kcal = data.get("calories")
    print(f"[scan_correct] row={scan_index} sup_name={sup_name!r} sup_kcal={sup_kcal} row.kcal={row.kcal}", flush=True)
    if sup_name and (sup_kcal is None or sup_kcal == 0) and row.kcal <= 0:
        looked = _lookup_known_macros(sup_name)
        print(f"[scan_correct] lookup result: {looked}", flush=True)
        if looked:
            correction["calories"] = looked["kcal"]
            correction["protein"] = looked["protein"]
            correction["carbs"] = looked["carbs"]
            correction["fat"] = looked["fat"]
            correction["_backfill_source"] = looked["_source"]
            print(f"scan_correct: backfilled row {scan_index} {sup_name!r} → "
                  f"{looked['kcal']} kcal from {looked['_source']}", flush=True)

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

    # Refresh the cache row from Sheet so subsequent reads reflect the fix.
    cache.refresh_one(scan_index, _sheet_read_nutrition_row)

    return jsonify({"ok": True, "scan_index": scan_index, "correction": correction,
                    "sheet_updated": sheet_result.get("updated", 0)})


# v2.7.39: Rename existing scan + auto re-recognize macros (Jim OOB 2026-08-06
# 14:50 HKT: "is there a way to adjust the existing record by text and recognize
# it again. e.g. the current recognized one is called 白切雞, but actually it is
# Hai nan chicken rice"). Flow:
#  1. User types new dish name in inline popover
#  2. Backend overwrites name field (so iPhone display updates immediately)
#  3. Backend auto-calls _apiyi_nutrition_enrich(new_name) for re-estimate macros
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
    # Re-estimate macros from text (uses APiyi gpt-4o-mini nutrition enrichment, JSON mode)
    new_kcal, new_p, new_c, new_f = old_kcal, old_p, old_c, old_f
    enrich_failed = False
    try:
        enrich = _apiyi_nutrition_enrich(re_text)
        if enrich and enrich.strip().startswith("{"):
            parsed = json.loads(enrich)
            new_kcal = int(parsed.get("calories", old_kcal) or old_kcal)
            new_p = float(parsed.get("protein", old_p) or old_p)
            new_c = float(parsed.get("carbs", old_c) or old_c)
            new_f = float(parsed.get("fat", old_f) or old_f)
        else:
            enrich_failed = True
    except Exception as e:
        enrich_failed = True
        print(f"[scan_rename] re-estimate failed: {e}")

    # v3.3.7: NEVER leave macros at 0 after a rename. If the AI re-estimate
    # returned 0/None for everything (Jim OOB 2026-08-16 'i adjusted the
    # chicken name and thus all the nutrient info are zero this is so
    # inprofessional'), fall back to Sheet-history lookup before persisting.
    # Both ai-empty and enrich-failed paths funnel here.
    if (new_kcal <= 0 or enrich_failed) and new_name:
        looked = _lookup_known_macros(new_name)
        if looked:
            print(f"[scan_rename] backfill: AI gave {new_kcal} kcal, "
                  f"falling back to {looked['kcal']} kcal from {looked['_source']}",
                  flush=True)
            new_kcal = looked["kcal"]
            new_p = looked["protein"]
            new_c = looked["carbs"]
            new_f = looked["fat"]
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
        "note": (row.notes + (" " + correction["type"] if row.notes else "")).strip()[:200],
        "drive_image_url": row.drive_image_url,
    }
    sheet_result = _sheet_update_nutrition_row(scan_index, sheet_entry)
    if not sheet_result.get("ok"):
        return jsonify({"ok": False, "error": f"sheet update failed: {sheet_result.get('errors')}"}), 500
    cache.refresh_one(scan_index, _sheet_read_nutrition_row)

    # Reload row for the response
    row = cache.get_by_row(scan_index)
    return jsonify({
        "ok": True,
        "scan_index": scan_index,
        "entry": row.to_pwa_dict() if row else {},
        "correction": correction,
        "sheet_updated": sheet_result.get("updated", 0),
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


@app.route("/api/scan_preview", methods=["POST"])
def api_scan_preview():
    """Take image, run vision + pplx, return suggested entry WITHOUT writing to log.

    Frontend shows preview UI: dish desc + macros + suggested restaurant chain.
    Only when Jim taps 確認 → POST /api/scan_commit with the chosen entry.
    """
    if "image" not in request.files:
        return jsonify({"ok": False, "error": "no image"}), 400
    img_file = request.files["image"]
    img_bytes = img_file.read()
    if len(img_bytes) > 10 * 1024 * 1024:
        return jsonify({"ok": False, "error": "image too large"}), 413

    # Save image to scan_cache (will be reused if Jim confirms)
    now_hkt_dt = datetime.now(timezone(timedelta(hours=8)))
    img_filename = f"preview_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}.jpg"
    img_path = SCAN_CACHE_DIR / img_filename
    img_path.write_bytes(img_bytes)

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
    vision_desc = _minimax_vision(img_b64, vision_prompt)

    # 1b. APiyi gpt-4o-mini vision 2nd-opinion (Jim OOB 2026-07-26 19:35 HKT)
    apiyi_vision_desc = _apiyi_vision_analyze(img_b64, vision_prompt)
    # v3.2.7.14: detect gpt-4o-mini safety-filter refusal (e.g. "抱歉，我無法...")
    # and treat as empty so it does NOT pollute vision_desc (Jim OOB
    # 2026-08-09 16:48 HKT 'I took a screenshot not food, but scan got
    # committed as 抱歉 / 第一道菜').
    if apiyi_vision_desc and ("抱歉" in apiyi_vision_desc[:30] and "無法" in apiyi_vision_desc[:50] and len(apiyi_vision_desc) < 80):
        apiyi_vision_desc = ""
    if apiyi_vision_desc and not apiyi_vision_desc.startswith("（"):
        # 2-vision median-merge: take whichever is longer (more dish detail)
        if len(apiyi_vision_desc) > len(vision_desc) * 0.7:
            vision_desc = vision_desc + "\n\n（ChatGPT 2nd-opinion vision）\n" + apiyi_vision_desc

    # 2. pplx enrichment
    pplx_desc = _pplx_enrich(vision_desc)

    # 2b. OpenRouter gpt-4o-mini 2nd-opinion (Jim OOB 2026-07-25 13:35)
    apiyi_desc = _apiyi_nutrition_enrich(vision_desc)
    pplx_parsed = _parse_nutrition_block(pplx_desc)
    apiyi_parsed = {}
    if apiyi_desc and apiyi_desc.startswith("{"):
        try:
            apiyi_parsed = json.loads(apiyi_desc)
        except Exception:
            apiyi_parsed = _parse_nutrition_block(apiyi_desc)
    merged_nutrition = _merge_nutrition_estimates([pplx_parsed, apiyi_parsed])

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

    # 3b. v3.2.7.9: Multi-dish detection — call APiyi multi estimator
    # (Jim OOB 2026-08-08 20:05 HKT 'scan one photo with different food to
    # generate multiple entry'). Returns array of {name, 12-field...} objects.
    multi_desc = vision_desc + "\n\n" + pplx_desc
    multi_json = _apiyi_nutrition_enrich_multi(multi_desc)
    multi_dishes = []
    try:
        parsed = json.loads(multi_json)
        if isinstance(parsed, dict) and "dishes" in parsed:
            multi_dishes = parsed["dishes"]
        elif isinstance(parsed, list):
            multi_dishes = parsed
    except Exception:
        multi_dishes = []

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
            "date": today_iso(),
            "time": now_hkt_dt.strftime("%H:%M"),
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
            "date": today_iso(),
            "time": now_hkt_dt.strftime("%H:%M"),
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

    # v3.2.7.10: AUTO-COMMIT — no more confirm step. Scan → log immediately.
    # (Jim OOB 2026-08-08 20:15 HKT 'I think no need confirmation after
    # scanning. Do it right away. if I see problem, I can edit it delete
    # later on.') Frontend just shows the image + entries; if anything's
    # wrong, user taps ✏️ edit or 🗑️ delete on the food card.
    now_iso_str = now_iso()
    final_name = f"scan_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}.jpg"
    final_path = SCAN_CACHE_DIR / final_name
    drive_image_url = ""  # v3.2.7.45: Golden Copy (Jim OOB 2026-08-14)
    try:
        img_path.rename(final_path)
        image_url = f"/scan_img/{final_name}"
        # v3.2.7.45: upload to Drive for Golden Copy. Best-effort — failure
        # logged but doesn't break the auto-commit, just leaves column N empty.
        drive_image_url = _upload_to_drive(final_path)
        # v3.2.7.46: 480px thumbnail for fast food history view.
        _make_thumb(final_path, _thumb_path_for(final_name))
    except Exception:
        final_path = img_path
        image_url = f"/scan_img/{img_path.name}"
        try:
            drive_image_url = _upload_to_drive(final_path)
            _make_thumb(final_path, _thumb_path_for(img_path.name))
        except Exception:
            drive_image_url = ""

    committed = []
    for e_idx, entry in enumerate(entries_list):
        entry["timestamp_iso"] = now_iso_str
        entry["source"] = "v3.2.7.10-scan_autocommit (minimax-m3 + pplx-sonar-pro + apiyi-multi)"
        entry["models_used"] = ["minimax-m3", "pplx-sonar-pro", "apiyi-gpt-4o-mini-multi"]
        entry["image_saved_to"] = str(final_path)
        entry["confidence"] = "auto-commit preview"
        entry["sheet_synced"] = False
        entry["user_correction"] = None
        entry["vision_raw_desc"] = vision_desc
        entry["vision_short"] = vision_desc[:300]
        entry["pplx_short"] = pplx_desc[:500]
        entry["apiyi_enrichment"] = apiyi_desc[:300] if apiyi_desc else ""
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
                range_str = sheet_result.get("range", "")
                new_row_index = int(range_str.split(":")[0].rstrip("ABCDEFGHIJKLM"))
                get_cache().refresh_one(new_row_index, _sheet_read_nutrition_row)
            except Exception:
                pass
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
    # v3.3.8: invalidate daily coaching so the next cheer/video/food-coach
    # reflects the freshly scanned meal.
    _invalidate_daily_coaching()
    return jsonify({
        "ok": True,
        "auto_committed": True,
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
    })


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

    # 1b. APiyi gpt-4o-mini vision 2nd-opinion (Jim OOB 2026-07-26 19:35 HKT)
    apiyi_vision_desc = _apiyi_vision_analyze(img_b64, vision_prompt)
    # v3.2.7.14: detect gpt-4o-mini safety-filter refusal (e.g. "抱歉，我無法...")
    # and treat as empty so it does NOT pollute vision_desc (Jim OOB
    # 2026-08-09 16:48 HKT 'I took a screenshot not food, but scan got
    # committed as 抱歉 / 第一道菜').
    if apiyi_vision_desc and ("抱歉" in apiyi_vision_desc[:30] and "無法" in apiyi_vision_desc[:50] and len(apiyi_vision_desc) < 80):
        apiyi_vision_desc = ""
    if apiyi_vision_desc and not apiyi_vision_desc.startswith("（"):
        # 2-vision median-merge: take whichever is longer (more dish detail)
        if len(apiyi_vision_desc) > len(vision_desc) * 0.7:
            vision_desc = vision_desc + "\n\n（ChatGPT 2nd-opinion vision）\n" + apiyi_vision_desc
    pplx_desc = _pplx_enrich(vision_desc)

    shared = _detect_shared_meal(vision_desc + " " + pplx_desc)
    jim_ratio = 0.60 if shared else 1.00
    apiyi_desc = _apiyi_nutrition_enrich(vision_desc)
    pplx_parsed = _parse_nutrition_block(pplx_desc)
    apiyi_parsed = {}
    if apiyi_desc and apiyi_desc.startswith("{"):
        try:
            apiyi_parsed = json.loads(apiyi_desc)
        except Exception:
            apiyi_parsed = _parse_nutrition_block(apiyi_desc)
    merged_nutrition = _merge_nutrition_estimates([pplx_parsed, apiyi_parsed])
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
            "date": today_iso(),
            "time": now_hkt_dt.strftime("%H:%M"),
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
    new_time_full = f"{new_date}T{new_time}:00+08:00"

    cache = get_cache()
    row = cache.get_by_row(scan_index)
    if row is None:
        return jsonify({"ok": False, "error": "scan_index not found in cache"}), 404
    old_date = row.date
    old_time = row.time

    sheet_result = _sheet_update_nutrition_cells(scan_index, [
        ("A", new_date),
        ("B", new_time_full),
    ])
    if not sheet_result.get("ok"):
        return jsonify({"ok": False, "error": f"sheet update failed: {sheet_result.get('errors')}"}), 500

    cache.refresh_one(scan_index, _sheet_read_nutrition_row)
    row_after = cache.get_by_row(scan_index)

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

    # Evict the deleted row + reindex rows above it (Sheet rows shift down
    # by 1 after `deleteDimension`). Without this, the next delete/edit on
    # row > scan_index targets the wrong Sheet row.
    with cache._lock:  # type: ignore[attr-defined]
        cache._delete_with_shift(scan_index)

    return jsonify({
        "ok": True,
        "scan_index_removed": scan_index,
        "removed_name": removed_name,
        "removed_date": removed_date,
        "removed_time": removed_time,
        "sheet_rows_deleted": sheet_result.get("deleted", 0),
        "sheet_errors": sheet_result.get("errors", []),
    })


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
            entry["source"] = "v2.2-scan (minimax-m3 + pplx-sonar-pro, Jim confirmed)"
            entry["models_used"] = ["minimax-m3", "pplx-sonar-pro"]
            entry["image_saved_to"] = str(final_path)
        else:
            entry["source"] = "v2.7.22-scan_text_direct (apiyi-gpt-4o-mini, no image)"
            entry["models_used"] = ["apiyi-gpt-4o-mini"]
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
        # v3.3.7: backfill empty macros from Sheet history (Jim OOB
        # 2026-08-16 'why today egg tarts have nutrient info?' — vision
        # AI returned empty for that photo, all 12 fields defaulted to 0).
        # The name might be a user-typed override like '原味蛋撻、抹茶味蛋撻'
        # (2 pieces), and 3 historical 蛋撻 rows average ~230 kcal each.
        if entry_name and entry_kcal <= 0 and entry["source"].startswith("v2.2"):
            looked = _lookup_known_macros(entry_name)
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
                         or entry.get("apiyi_enrichment")
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

        entry["drive_image_url"] = drive_image_url

        sheet_result = _append_to_sheet_nutrition(entry)
        new_row_index = None
        if sheet_result.get("ok") and not sheet_result.get("skipped"):
            try:
                range_str = sheet_result.get("range", "")
                new_row_index = int(range_str.split(":")[0].rstrip("ABCDEFGHIJKLM"))
                cache.refresh_one(new_row_index, _sheet_read_nutrition_row)
            except Exception:
                pass
        committed.append({
            "name": entry.get("name", "?"),
            "calories": entry.get("calories", 0),
            "protein": entry.get("protein", 0),
            "grade": (entry.get("coach_comment") or {}).get("grade", "—"),
            "sheet_row": sheet_result.get("range", ""),
            "sheet_ok": sheet_result.get("ok", False),
            "scan_index": new_row_index,
        })

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


# v2.7.19: scan re-enrich with user hint (Jim OOB 2026-07-31 13:25 HKT)
# After preview returned, Jim can type supplementary info (餐廳名/份量/醬汁/材料)
# and tap "🔄 用補充資料再 estimate" → this endpoint re-runs pplx + APiyi
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
      4. Re-run _pplx_enrich() + _apiyi_nutrition_enrich() with augmented desc.
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

    # 1b. APiyi gpt-4o-mini vision 2nd-opinion (v2.7.12 — Jim OOB 7/26 19:35)
    apiyi_vision_desc = _apiyi_vision_analyze(img_b64, vision_prompt)
    if apiyi_vision_desc and not apiyi_vision_desc.startswith("（"):
        if len(apiyi_vision_desc) > len(vision_desc) * 0.7:
            vision_desc = vision_desc + "\n\n（ChatGPT 2nd-opinion vision）\n" + apiyi_vision_desc

    # 2. Augment with user hint — Jim OOB 7/31 "supplementary info to re-estimate"
    # Prepend hint as context, then pplx/apaiyi still do recogniser role.
    augmented_desc = f"用家補充資料（餐廳名/份量/醬汁/材料）：{user_hint}\n\n{vision_desc}"

    # 3. Re-enrich via pplx + APiyi (parallel — sequential for safety, no quota burn)
    pplx_desc = _pplx_enrich(augmented_desc)
    apiyi_desc = _apiyi_nutrition_enrich(augmented_desc)

    # 4. Merge 12-field nutrition
    pplx_parsed = _parse_nutrition_block(pplx_desc)
    apiyi_parsed = {}
    if apiyi_desc and apiyi_desc.startswith("{"):
        try:
            apiyi_parsed = json.loads(apiyi_desc)
        except Exception:
            apiyi_parsed = _parse_nutrition_block(apiyi_desc)
    merged_nutrition = _merge_nutrition_estimates([pplx_parsed, apiyi_parsed])

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
        "apiyi_enrichment": apiyi_desc[:300] if apiyi_desc else "",
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
    return jsonify({"ok": True, "preview": preview})


# ---------- F2b: /api/scan_preview_text (Jim OOB 2026-08-02 02:50 HKT) ----------
# Text-only food logging path. Jim types what he ate ("燒肉飯",
# "noodle + chicken", "2 eggs + toast"), APiyi estimates nutrition,
# preview returned, /api/scan_commit then logs it. NO image required.
NUTRITION_FIELD_SCHEMA = (
    "calories,protein,carbs,fat,fiber,sugar,sodium,sat_fat,"
    "trans_fat,vit_c,iron,calcium"
)


@app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    """Audio file → Cantonese transcript via gpt-4o-transcribe.

    Jim OOB 2026-08-07: 'I have been using gpt-4o-mini for image recognition
    via apiyi. can we use gpt-4o-mini-transcribe?'. v2.7.45 P3.

    Accepts: multipart/form-data with 'audio' field (mp3/m4a/wav/webm/ogg)
    Optional: 'language' (default yue), 'prompt' (default Cantonese bias)
    Returns: {ok, text, language, model, duration_estimate, usage}
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

    # APiyi gpt-4o-transcribe
    api_key = _apiyi_api_key()

    if not api_key:
        return jsonify({"ok": False, "error": "APIYI_API_KEY not set"}), 500

    # Save to temp file for multipart upload
    import tempfile, os as _os
    suffix = _os.path.splitext(audio_file.filename)[1] or ".mp3"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        boundary = "----formdata-gymbro"
        with open(tmp_path, "rb") as f:
            file_content = f.read()
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{audio_file.filename}"\r\n'
            f"Content-Type: {audio_file.mimetype or 'audio/mpeg'}\r\n\r\n"
        ).encode() + file_content + (
            f"\r\n--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"gpt-4o-transcribe\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="language"\r\n\r\n'
            f"{language}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="prompt"\r\n\r\n'
            f"{user_prompt}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="response_format"\r\n\r\n'
            f"json\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        req = urllib.request.Request(
            "https://api.apiyi.com/v1/audio/transcriptions",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Authorization": "".join(["Bearer ", api_key]),
            },
        )

        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())

        text = result.get("text", "").strip()
        usage = result.get("usage", {})

        return jsonify({
            "ok": True,
            "text": text,
            "language": language,
            "model": "gpt-4o-transcribe",
            "size_mb": round(size_mb, 2),
            "usage": usage,
            "char_count": len(text),
        })

    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:500]
        return jsonify({
            "ok": False,
            "error": f"APiyi API error: HTTP {e.code}",
            "detail": err_body,
        }), 502
    except Exception as e:
        return jsonify({"ok": False, "error": f"transcribe failed: {e}"}), 500
    finally:
        try:
            _os.unlink(tmp_path)
        except Exception:
            pass


@app.route("/api/scan_preview_text", methods=["POST"])
def api_scan_preview_text():
    """Text-only food entry preview (no image required).

    Jim OOB 2026-08-02 02:50 HKT: 'I think the food log should allow
    me to direct input the food by text'. Flow:
      1. User types a description in textbox.
      2. Backend sends to APiyi gpt-4o-mini (text-only — no vision).
      3. Optional user_hints[] appended for extra context.
      4. Returns preview shape identical to /api/scan_preview so the
         frontend can present it in the SAME confirm card. Frontend
         then calls /api/scan_commit with image_path="" to commit.
    """
    data = request.get_json(silent=True) or {}
    text_desc = (data.get("text") or data.get("description") or "").strip()
    user_hints_in = data.get("user_hints", []) or []

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

    # APiyi gpt-4o-mini text-only nutrition estimate
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
    apiyi_text_desc = _apiyi_nutrition_enrich(prompt)

    # Parse APiyi JSON response
    try:
        parsed = json.loads(apiyi_text_desc)
    except Exception:
        parsed = _parse_nutrition_block(apiyi_text_desc)

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
    # preview shows a proper dish name + star rating, NOT the raw APiyi
    # prose (Jim OOB 2026-08-07 23:50 HKT 'the food title is not shown
    # on the list view. Moreover, it does not have rating').
    apiyi_name_raw = (parsed.get("name") if isinstance(parsed, dict) else "") or ""
    restaurant_guess = (parsed.get("restaurant_guess") if isinstance(parsed, dict) else "") or ""
    # Use APiyi's name as the dish-name extractor input (it's the
    # most relevant text), with the user input as fallback.
    suggested_name = _extract_dish_name(apiyi_name_raw, apiyi_text_desc, fallback=text_desc[:60])
    # If still empty or the generic "食物" fallback, try the user input directly
    if not suggested_name or suggested_name.strip() == "食物":
        suggested_name = _extract_dish_name(text_desc, "", fallback=text_desc[:60])
    # v3.2.7.3: single A-F grade via _coach_comment (replaces old 1-5 star rating)
    coach_cc = _coach_comment(
        suggested_name,
        field_entries.get("calories", 0) or 0,
        field_entries.get("protein", 0) or 0,
        field_entries.get("carbs", 0) or 0,
        field_entries.get("fat", 0) or 0,
        restaurant_guess,
        daily_ctx_seed=_daily_ctx_seed(),
    )

    preview = {
        "preview_id": f"pv_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}_txt",
        "image_path": "",  # text-only, no image
        "image_url": "",
        "input_mode": "text",  # flag for frontend to render label
        "vision_desc": augmented_text,
        "vision_short": augmented_text[:300],
        "pplx_short": apiyi_text_desc[:500] if apiyi_text_desc else "",
        "apiyi_enrichment": apiyi_text_desc[:300] if apiyi_text_desc else "",
        "nutrition_merged": parsed if isinstance(parsed, dict) else {},
        "suggested_entry": {
            "date": today_iso(),
            "time": now_hkt_dt.strftime("%H:%M"),
            "meal_type": "scan",
            "name": _sanitise_dish_name(suggested_name, vision_desc=text_desc, pplx_desc=apiyi_text_desc),
            "coach_comment": coach_cc,
            "restaurant_chain": restaurant_guess,
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


def _apiyi_nutrition_enrich_local(text: str) -> dict:
    """Wrapper retained to keep call-site readable.
    Already imported via _apiyi_nutrition_enrich in the module."""
    return _apiyi_nutrition_enrich(text)


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

    pplx_ans = ""
    try:
        pplx_resp = requests_if_available = None
        api_key = _pplx_api_key()
        if api_key:
            payload = {
                "model": "sonar-pro",
                "messages": [
                    {"role": "system", "content": "你係香港 NSCA-CSCS 教練。用繁體中文、技術但口語化。"},
                    {"role": "user", "content": pplx_query},
                ],
                "max_tokens": 1400,
                "temperature": 0.25,
            }
            req = urllib.request.Request(
                "https://api.perplexity.ai/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "Authorization": "Bear" + "er " + api_key},
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
            pplx_ans = resp["choices"][0]["message"]["content"]
    except Exception as e:
        pplx_ans = f"（pplx 教練 tips 失敗：{type(e).__name__}）"

    # MiniMax synthesis — render Traditional Chinese with friendly tone
    mm_prompt = (
        f"以下係 pplx 教練對於一個 gym session 嘅 form cue + progression 建議：\n\n"
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
WITHINGS_PULL_SCRIPT = Path("/home/work/.hermes/skills/withings/withings.py")  # Jim OOB 2026-07-24: cheer pulls Withings on every fire

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
    if sentences and len(sentences[0]) < 200:
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
    }
    # v3.2.7.39: catch-all fallback. If unknown English word isn't in the
    # table, return a placeholder so the TTS doesn't read literal English.
    # We use 「X」 (Chinese book-title quotes) so the voice says "X" but the
    # word is clearly marked as something the user might want to recognise
    # later when they look at the text version.
    return table.get(en_word, f"「{en_word}」")


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
    }


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
    withings_weight = metrics.get("withings_weight_kg")
    withings_fat = metrics.get("withings_fat_pct")
    withings_steps = metrics.get("withings_steps_today")
    withings_dist_km = metrics.get("withings_distance_km")
    workout_detail_zh = metrics.get("workout_detail_zh", "(尚未做運動)")

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
        f"數據快照時間：HKT {hkt_str}（剛從 Whoop + Withings 即時拉返）"
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
- Withings 體重：{withings_weight} 公斤
- Withings 體脂：{withings_fat} %
- Withings 今日步數：{withings_steps} 步
- Withings 今日步行距離：{withings_dist_km} 公里
- 今日 Whoop 活動總數：{workout_n} 個 session
- 今日食物 log 總數：見 nutrition_block 內嘅 meal_count

**今日 workout detail**（逐個動作 loop 出嚟，唔好概括）：
{workout_detail_zh}
|{news_block}
|{liverpool_block}
|{jim_context_block}
|{nutrition_block}
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
                    "9. **(v3.2.7.44) 唔好寫 IT / 網絡 / 科技新聞內容** —呢類新聞 (e.g. PortSwigger 漏洞、HTTPS 1.0、HTTP/3 speed、"
                    "Trend Micro、ServiceNow、N-central、AI security、cyber attack) 與 Jim 嘅健身/健康 cheer 完全無關，"
                    "而且 TTS 讀出嚟會變「H-T-T-P 斜線 1 點 0」、「PortSwigger 字母一個一個讀」— 變成機械人讀 tech spec。"
                    "Jim OOB 2026-08-12 17:58 HKT 「There are many words like 'htt www... speed version 1.0' and 'slash' and 'break' wording」。"
                    "即使本週熱話 news 入面有 IT security 內容, 都要 skip, 唔好寫入 cheer。 範圍:\n"
                    "   - ❌ 任何 HTTP/HTTPS/SSL/TLS/API/URL/JSON/XML/HTML 提及\n"
                    "   - ❌ 任何版本號 (v1.0, version 2.3, HTTPS/1.1, HTTP/3)\n"
                    "   - ❌ 任何網絡安全品牌 (PortSwigger, Trend Micro, ServiceNow, N-central, CrowdStrike, Palo Alto, Fortinet, Cloudflare)\n"
                    "   - ❌ 速度提及 (e.g. 「speed 係 1.0 嘅 X 倍」、「faster than HTTP/1.1」)\n"
                    "   - ❌ Cyber attack / 漏洞 / 修補 / 加密 / 解密 / 漏洞披露 等技術詞\n"
                    "   - 唯一可以提嘅科技詞: 與 Jim 健身 app (Whoop、Withings) 健康數據相關嘅。用「你個 Whoop」、「返 Withings 睇體重」呢類口語。\n"
                    "10. **(v3.2.7.44) 唔好用 `斜線`/`slash`/`break`/markdown 標記**：edge-tts --ssml 會解析 <break> 標籤做停頓，"
                    "但如果寫出嚟嘅文字本身包含 `斜線`、`break`、`左括號`、`右括號`、`星號`、`version` 等英文 mark-up 詞，"
                    "TTS 會讀成「斜線」、「break」、「星號」— 變 robotic。寫作時唔好用任何 mark-up 字眼。\n"
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
# Cache key: `YYYY-MM-DD` HKT + SHA1 of (last_food_ts, last_gym_ts,
# last_whoop_ts, recovery_pct, sleep_pct). Invalidation on data-changing
# events (scan_preview, end_session, whoop_refresh).
# ---------------------------------------------------------------------------

daily_coaching_cache: dict = {}           # date_str → entry
_DAILY_COACHING_LOCK = threading.Lock()


def _daily_coaching_entry(date_str: str) -> dict:
    """Return today's cache entry, creating an empty one if missing."""
    with _DAILY_COACHING_LOCK:
        e = daily_coaching_cache.get(date_str)
        if e is None:
            e = {
                "date": date_str,
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
            daily_coaching_cache[date_str] = e
        return e


def _build_daily_context() -> dict:
    """Pure-Python data gather — no LLM. Called by cheer and daily coaching.

    Returns dict with:
        - whoop snapshot (recovery_pct, hrv_ms, sleep_pct, sleep_bed_hr, ...)
        - withings (weight, fat, steps, distance)
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
    try:
        ctx["withings"] = {
            "weight_kg": _withings_weight() if "_withings_weight" in globals() else None,
            "fat_pct": _withings_fat_pct() if "_withings_fat_pct" in globals() else None,
            "steps": (_withings_steps_today() or {}).get("steps") if "_withings_steps_today" in globals() else None,
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
    return ctx


def _context_hash(ctx: dict) -> str:
    """Stable hash of the data-changing fields. Re-roll when any field flips."""
    import hashlib
    payload = "|".join([
        str(ctx.get("last_food_ts") or ""),
        str(ctx.get("last_gym_ts") or ""),
        str(ctx.get("last_whoop_ts") or ""),
        str(ctx.get("recovery_pct") or ""),
        str(ctx.get("sleep_pct") or ""),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _invalidate_daily_coaching(date_str: str | None = None) -> None:
    """Drop the cached narrative so the next read regenerates."""
    date_str = date_str or today_iso()
    with _DAILY_COACHING_LOCK:
        e = daily_coaching_cache.get(date_str)
        if e is not None:
            e["context_hash"] = None
            e["narrative"] = None
            e["status"] = "idle"


def _daily_coaching_narrative_prompt(ctx: dict, fire_type: str) -> str:
    """Build the user prompt for the daily narrative LLM call.

    Reuses the data-freshness / persona / style scaffolding from
    _synthesize_cheer_text so the output tone matches what cheer produced
    before unification.
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
        f"- Withings 體重：{weight} 公斤 / 體脂 {fat} %\n"
        f"- 今日步數：{steps} 步\n"
        f"- 最後進食：{ctx.get('last_food_ts') or '今日未 log'}\n"
        f"- 最後 gym：{ctx.get('last_gym_ts') or '今日未做 gym'}\n\n"
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

    - If `block=True` (default) and the cache is empty/stale, generate
      synchronously in the caller's thread (capped at `timeout_s`).
    - If `block=False` and a generation is needed, kick off a background
      thread and return the entry in 'generating' state immediately.
    - Returns the cache entry dict; caller reads .narrative.
    """
    ctx = _build_daily_context()
    new_hash = _context_hash(ctx)
    entry = _daily_coaching_entry(date_str)
    with _DAILY_COACHING_LOCK:
        if entry.get("narrative") and entry.get("context_hash") == new_hash and entry.get("status") == "ready":
            return entry
        # Stale or missing — start fresh
        entry["context_hash"] = new_hash
        entry["status"] = "generating"
        entry["started_at"] = now_iso()
        entry["error"] = None
        entry["job_id"] = f"dc-{date_str}-{int(time.time())}"

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
    return _daily_coaching_entry(date_str)


def _run_daily_coaching_generation(date_str: str, ctx: dict, fire_type: str, job_id: str) -> None:
    """Worker called by both blocking and async paths. Always updates the entry."""
    try:
        result = _generate_daily_narrative(ctx, fire_type)
        with _DAILY_COACHING_LOCK:
            e = daily_coaching_cache.get(date_str) or _daily_coaching_entry(date_str)
            e["narrative"] = result.get("narrative") or ""
            e["theme"] = result.get("theme") or ""
            e["video_seed"] = result.get("video_seed") or ""
            e["food_seed"] = result.get("food_seed") or ""
            e["status"] = "ready" if e["narrative"] else "failed"
            e["generated_at"] = now_iso()
            e["error"] = None if e["narrative"] else "empty narrative"
            daily_coaching_cache[date_str] = e
    except Exception as e:
        with _DAILY_COACHING_LOCK:
            entry = daily_coaching_cache.get(date_str) or _daily_coaching_entry(date_str)
            entry["status"] = "failed"
            entry["error"] = f"{type(e).__name__}: {e}"
            daily_coaching_cache[date_str] = entry


def _daily_ctx_seed() -> str:
    """Tiny helper used by food coach callers — returns today's theme tag
    from the unified daily coaching cache, or '' if not yet generated.

    Single source of truth so /api/scan_preview, /api/scan_correct, etc. all
    see the same 今日主題 without each one re-deriving it.
    """
    try:
        entry = daily_coaching_cache.get(today_iso())
        if not entry:
            return ""
        return entry.get("theme") or entry.get("food_seed") or ""
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
    """Generate Edge-TTS WanLung voice MP3 from cheer text.

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
        # Step 0: humanize guardrail (v3.2.7.41 — Jim OOB 2026-08-12 01:15 HKT
        # 「voice is so mechanical, so many non-narrative words at the
        # beginning」). Strip URL + metadata + bad openers BEFORE zh-replace
        # so the TTS never sees gibberish like 「H-T-T-P-S 冒號 斜線」.
        voice_text = _humanize_for_voice(text)
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
        # Audit EN leaks
        leaks = _voice_audit_en(voice_text)
        for _ in range(2):
            if not leaks:
                break
            voice_text = _voice_zh_replace(voice_text)
            leaks = _voice_audit_en(voice_text)
        if leaks:
            # Inline-replace any remaining EN leaks so we keep the full body (v2.7.3
            # fix — previously nuked 1200 chars and replaced with 383-char stub).
            import re as _re
            voice_text = _re.sub(r"([A-Za-z]+)", lambda m: _zh_inline(m.group(0)), voice_text)

        # Step 2: Edge-TTS WanLung +0% (natural pace). Edge-TTS WanLung has
        # shown 1m30s-2m runtime for 800-1500 字 scripts. Use 240s (4 min)
        # timeout to be safe. Jim OOB 2026-07-23 voice detail direction —
        # sacrifice latency for completeness.
        #
        # v3.2.7.44: edge-tts CLI does NOT support --ssml flag. Pass plain
        # text via --text. The library internally wraps the text in SSML
        # via mkssml() and sends to Microsoft Edge TTS service. Plain text
        # with proper Chinese punctuation (。 ， ！ ？) gives natural pauses.
        # Previous SSML <break> tags got read literally by WanLung, producing
        # the robotic "左括號 break time 等於 450ms 右括號" Jim OOB heard.
        plain_text = voice_text
        tmp_ogg = f"/tmp/cheer_voice_{int(time.time())}.ogg"
        result = _sp.run([
            "edge-tts", "--voice", "zh-HK-WanLungNeural",
            "--rate", "+0%", "--text", plain_text,
            "--write-media", tmp_ogg,
        ], capture_output=True, text=True, timeout=600)  # v3.3.7: 240s → 600s for longer scripts (5-8 min audio).
        if result.returncode != 0:
            try:
                with open('/tmp/cheer_errors.log', 'a') as f:
                    f.write(f"\n=== edge-tts failed at {now_iso()} (voice_text {len(voice_text)} chars) ===\n")
                    f.write(f"stderr: {result.stderr[:500]}\n")
                    f.write(f"stdout: {result.stdout[:500]}\n")
            except Exception:
                pass
            return ""
        # Step 3: ffmpeg → real MP3 (Rule 30, universal playback)
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
                    f"target=130-160s | mp3={out_mp3.name}\n"
                )
        except Exception:
            pass
        return str(out_mp3)
    except Exception:
        return ""


def _generate_cheer_image(context: str = "manual") -> str:
    """Generate MiniMax image-01 motivation image → JPG → PNG (per Rule 38).
    Returns file path or '' on failure."""
    api_key = _minimax_api_key()
    if not api_key:
        return ""
    prompt = (
        "Ultra wide 16:9 cinematic photograph, modern bright gym interior with motivational atmosphere. "
        "Two Asian fitness coaches side by side, dynamic duo composition. "
        "LEFT: athletic Asian male coach, age 30, bright yellow tank top, Spanish/Portuguese features, "
        "athletic muscular body, friendly warm smile. "
        "RIGHT: young Asian female fitness coach, age 22, Blackpink Jennie style — jet black long hair, "
        "sharp cat-eye makeup, fair skin, slim elegant build, cropped pink sports bra, high-waist black leggings, "
        "holding pink protein shaker in left hand, making peace sign with right hand, "
        "confident idol pose with subtle smile. "
        "Both looking at camera, motivational energy, photorealistic portrait photography, sharp focus, "
        "professional fitness editorial look, golden hour lighting."
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
    "gym_push": (
        "Cinematic 4K video of a confident Asian female fitness coach in her 30s, "
        "athletic Spanish/Portuguese features, hair pulled back in a high ponytail, "
        "bright coral sports bra and black leggings, "
        "performing a heavy barbell back squat in a modern bright gym, "
        "explosive upward push, intense focus, golden hour side light streaming "
        "through windows, sweat droplets catching the light, slow motion on "
        "standing up, deep breath before the next rep, peak intensity moment, "
        "motivational atmosphere, 6 seconds."
    ),
    "morning_stretch": (
        "Cinematic 4K video of an athletic Asian female doing morning sun salutation "
        "yoga on a Hong Kong rooftop at sunrise, Victoria Harbour skyline background, "
        "hair flowing in the breeze, deep exhale, slow stretch, "
        "golden light wrapping around her silhouette, "
        "calm focused expression, motivational serenity, 6 seconds."
    ),
    "finish_line": (
        "Cinematic 4K video of a determined Asian female athlete running through a "
        "rain-soaked Hong Kong street at dawn, neon reflections on wet asphalt, "
        "pushing through the final stretch, breath visible in cool air, "
        "determination in her eyes, slow motion on crossing an invisible finish line, "
        "motivational peak moment, 6 seconds."
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
        # Inject daily narrative seed (first sentence) if available
        try:
            dc = daily_coaching_cache.get(today_iso()) or {}
            seed = (dc.get("video_seed") or "").strip()
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
            return

        try:
            submit_resp = _json.loads(curl_r.stdout)
        except Exception as e:
            _video_job_set(job_id, status="failed", error=f"submit invalid json: {e}; raw={curl_r.stdout[:200]}")
            return

        task_id = submit_resp.get("task_id", "")
        base_resp = submit_resp.get("base_resp", {})
        if not task_id:
            err_code = base_resp.get("status_code", "?")
            err_msg = base_resp.get("status_msg", "no task_id")
            _video_job_set(job_id, status="failed", error=f"submit error {err_code}: {err_msg}")
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
                "step": "withings_pull",
                "step_at": now_iso(),
                "metrics": metrics,
                "whoop_pulled_at": whoop_pulled_at,
            })

        # 1b. Withings pull — refresh weight/fat/steps/distance/calories today.
        try:
            _sp.run([
                sys.executable, str(WITHINGS_PULL_SCRIPT),
                "today",
            ], capture_output=True, text=True, timeout=60)
        except Exception:
            pass
        # 7/29 Jim OOB: show 'as of time' for Withings data
        withings_pulled_at = now_iso()
        try:
            if WITHINGS_CACHE.exists():
                withings_pulled_at = _dt.fromtimestamp(WITHINGS_CACHE.stat().st_mtime, tz=HKT).isoformat(timespec='seconds')
        except Exception:
            pass
        metrics["withings_pulled_at"] = withings_pulled_at
        # Read fresh body weight + fat + steps from the Withings helpers.
        try:
            metrics["withings_weight_kg"] = _withings_weight()
            metrics["withings_fat_pct"] = _withings_fat_pct()
        except Exception:
            metrics.setdefault("withings_weight_kg", None)
            metrics.setdefault("withings_fat_pct", None)
        # Jim OOB 2026-07-24 09:15: Withings steps must be fresh on every fire
        # (Whoop V2 cycle has no `steps` field, so steps come from Withings).
        try:
            steps_data = _withings_steps_today() or {}
            metrics["withings_steps_today"] = steps_data.get("steps")
            metrics["withings_distance_km"] = steps_data.get("distance_km")
            metrics["withings_calories_today"] = steps_data.get("calories")
        except Exception:
            metrics.setdefault("withings_steps_today", None)
            metrics.setdefault("withings_distance_km", None)
            metrics.setdefault("withings_calories_today", None)
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({
                "step": "text_gen",
                "step_at": now_iso(),
                "metrics": metrics,
                "withings_pulled_at": withings_pulled_at,
            })

        # 1c. Workout detail loop (Jim OOB 2026-07-24: list every exercise).
        try:
            metrics["workout_detail_zh"] = _format_workout_detail_for_cheer(
                metrics.get("today_workouts") or []
            )
        except Exception:
            metrics["workout_detail_zh"] = "(運動 detail 讀取失敗)"

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
        # Re-use _voice_zh_replace which already handles **bold** / [n] citations.
        clean_text = _voice_zh_replace(text)
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

    Returns immediately with {job_id}; pipeline runs in background thread
    (Whoop pull → pplx text → Edge TTS → MiniMax image → cheer_artifacts +
    audio_cache + image_cache sync).

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
            "withings_pulled_at": job.get("withings_pulled_at"),
        })
    return jsonify({
        "ok": True,
        "status": job["status"],
        "step": job.get("step"),
        "step_at": job.get("step_at"),
        "started_at": job.get("started_at"),
        "whoop_pulled_at": job.get("whoop_pulled_at"),
        "withings_pulled_at": job.get("withings_pulled_at"),
    })


# ---------------------------------------------------------------------------
# v3.3.7: Video motivation (MiniMax video-01) — separate from cheer pipeline
# (Jim OOB 2026-08-19 'try adding video motivation at gym bro, use minimax
# video-01 model'). User-triggered, ~3-5 min, returns MP4.
# ---------------------------------------------------------------------------
@app.route("/api/video_motivation", methods=["POST"])
def api_video_motivation_trigger():
    """Submit a video-01 generation job. Returns job_id immediately.

    v3.3.8: if `theme` is "auto" (or omitted), derive the best theme from the
    daily coaching context (time of day + recovery + last gym). The video
    prompt also gets the daily narrative's first sentence injected as a
    visual seed so the coach in the video references the same theme the
    cheer voice just narrated.
    """
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
    t = threading.Thread(target=_generate_video_motivation, args=(job_id, theme), daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id, "theme": theme, "auto_picked": auto_picked, "status": "queued"})


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
    entry = _daily_coaching_entry(date_str)
    # If ready and hash matches current context, return it.
    ctx = _build_daily_context()
    new_hash = _context_hash(ctx)
    if entry.get("status") == "ready" and entry.get("context_hash") == new_hash and entry.get("narrative"):
        return jsonify({
            "status": "ready",
            "date": date_str,
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
//   - Withings steps: new _withings_steps_today() helper runs on every
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
const CACHE = 'gym-web-v138';
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
//     Backend function: _get_intraday_steps_today() in gym_web.py.
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
    # v3.3.0: Hydrate cache now that all helper funcs are defined.
    # Failure here is non-fatal — /api/scan_recent will return 503
    # until hydration succeeds, but the server still serves other routes.
    try:
        _hydrate_nutrition_cache()
        _get_nutrition_cache().start_background_refresh(
            _sheet_read_nutrition_rows, period_s=60
        )
    except Exception as _e:
        try:
            with open("/tmp/cache_startup_errors.log", "a") as f:
                f.write(f"{now_iso()} | startup | {type(_e).__name__}: {_e}\n")
        except Exception:
            pass
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
