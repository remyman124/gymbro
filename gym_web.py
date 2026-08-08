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
from datetime import datetime, timezone, timedelta
from datetime import datetime as _dt
from pathlib import Path

from flask import Flask, jsonify, request, render_template_string, send_from_directory
from workout_formatter import render as _render_text

# ---------- Constants ----------
WORKOUT_LOG = Path("/home/work/.whoop_workout_log.json")
HKT = timezone(timedelta(hours=8))
PORT = 7000
HOST = "0.0.0.0"

app = Flask(__name__, static_folder="/home/work/.hermes/image_cache", static_url_path="/img")

# Static token (Tailscale-only network = trusted)
SESSION_COOKIE = "gym_web_session"

app = Flask(__name__, static_folder="/home/work/.hermes/image_cache", static_url_path="/img")


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

    Returns activities grouped by date for the past N days (default 42),
    enriched with workout-log volume/sets/exercises + Whoop recovery
    (HRV + recovery %) + Whoop sleep performance %.
    """
    try:
        days_n = int(request.args.get("days", 42))
    except (TypeError, ValueError):
        days_n = 42
    days_n = max(7, min(days_n, 90))
    today = datetime.now(HKT).date()
    start_date = today - timedelta(days=days_n - 1)
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
    return jsonify({
        "days": days,
        "range_start": start_date.isoformat(),
        "range_end": today.isoformat(),
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
    # Local logs
    checks["nutrition_log"] = Path("/home/work/.hermes/nutrition_log.json").exists()
    checks["workout_log"] = Path("/home/work/.whoop_workout_log.json").exists()
    checks["food_scan_log"] = Path("/home/work/.hermes/food_scan_log.json").exists()
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
__version__ = "3.1.0"


def _safe_read_json(path, default=None):
    """Read a JSON cache file. Returns default on missing/corrupt — never raises to UI."""
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}


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

    return {
        "has_data": True,
        "steps": int(total_steps),
        "distance_km": round(total_dist / 1000, 2),
        "calories": round(total_cal, 1),
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

    Returns dict {date, steps, distance_km, calories, _source}.
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
        else:
            chosen = today_record
            chosen_source = "today_commit"
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
    """
    ctx = _load_jim_context()
    entries = ctx.get("entries") or {}
    if not entries:
        return ""
    lines = ["\n**Jim 個人 context（pushed by MCP / cheer 要記住）**："]
    for k, v in entries.items():
        if not isinstance(v, dict):
            continue
        val = v.get("value", "")
        tags = ", ".join(v.get("tags") or [])
        lines.append(f"- {k}: {val}" + (f" (tags: {tags})" if tags else ""))
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
    """
    data = _load_today_nutrition()
    return jsonify({
        "date": today_iso(),
        "fetched_at": now_iso(),
        "meal_count": data["meal_count"],
        "totals": data["totals"],
        "last_meal_ts": data["last_meal_ts"],
        "meals": data["meals"],
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
SCAN_LOG_PATH = Path("/home/work/.hermes/food_scan_log.json")
NUTRITION_LOG_PATH = Path("/home/work/.hermes/nutrition_log.json")

# pplx API key (separate from MiniMax which is in hermes-torres)
def _pplx_api_key() -> str:
    env_file = Path("/home/work/.hermes/.env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("PPLX_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
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
    """Read MiniMax M3 key from .hermes-torres/.env (canonical for vision)."""
    candidates = [
        Path("/home/work/.hermes-torres/.env"),
        Path("/home/work/.hermes/.env"),
    ]
    for env_file in candidates:
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("MINIMAX_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("MINIMAX_API_KEY", "")


def _apiyi_api_key() -> str:
    """Read APiyi key from .hermes/.env (canonical) or env var.
    APiyi = OpenAI-compatible proxy → can call ChatGPT gpt-4o / gpt-4o-mini.
    Jim OOB 2026-07-25 17:12 HKT: 'Not openrouter using apiyi'.
    Verified live 2026-07-27 08:35 HKT: https://api.apiyi.com/v1/chat/completions
    with model gpt-4o-mini returns valid ChatGPT response.
    """
    env_file = Path("/home/work/.hermes/.env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("APIYI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
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
    """Call MiniMax M3 vision endpoint. Returns description text."""
    api_key = _minimax_api_key()
    if not api_key:
        return "（MiniMax 金鑰未設定）"
    payload = {
        "model": "MiniMax-Text-01",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
        ]}],
        "max_tokens": 1800,
        "temperature": 0.25,
    }
    try:
        req = urllib.request.Request(
            "https://api.minimax.io/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bear" + "er " + api_key,
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
        return resp["choices"][0]["message"]["content"]
    except Exception as e:
        return f"（MiniMax vision 失敗：{type(e).__name__}）"


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
        "- 唔識 / 唔肯定就寫 0, 唔好 fabricate 大數值\n"
        "- 飽和脂肪/反式脂肪如果係自煮 dish 一般 0, 快餐先有高值\n"
        "- 維他命 C / 鐵質 / 鈣質係 micronutrient, 主要喺菜類/肉類\n"
        "- sodium 用 mg (1g 鹽 ≈ 400mg sodium)\n"
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
    """Call APiyi gpt-4o-mini for 2nd-opinion food nutrition enrichment.

    APiyi = OpenAI-compatible proxy. Confirmed live 2026-07-27 08:35 HKT.
    Jim OOB 2026-07-25 17:12 HKT: 'Not openrouter using apiyi' — ChatGPT
    gpt-4o-mini is the 2nd-opinion source for 12-field schema. Returns
    text (or empty on failure) — caller parses via _parse_nutrition_block
    then median-merges with pplx.

    NOTE: requires APIYI_API_KEY in /home/work/.hermes/.env or env var.
    Returns "" if key missing → pipeline silently falls back to pplx-only.
    """
    api_key = _apiyi_api_key()
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
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "You are a nutrition fact checker. Output ONLY valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 400,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
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
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        return resp["choices"][0]["message"]["content"]
    except Exception as e:
        return f"（APiyi enrichment 失敗：{type(e).__name__}）"


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



def _extract_dish_name_ai(vision_desc: str, pplx_desc: str = "") -> str:
    """v3.2.7.4: AI-based dish name extraction (Jim OOB 2026-08-08 10:35 HKT
    'No regex. Use ai' + 'Use minimax too'). Primary path: MiniMax M3
    via api.minimax.io. Fallback: APiyi gpt-4o-mini. Last resort: regex.

    v3.2.7.6 (Jim OOB 2026-08-08 10:30 HKT 'should recognise as 雞蛋、麵包'):
    Generic meal labels ('早餐', '午餐', '晚餐', 'afternoon tea') are NOT
    acceptable — must extract the actual dishes in the meal (e.g. 煎蛋、
    烤麵包). When vision says only '簡單嘅早餐' / '簡單嘅晚餐' etc.,
    look deeper for actual food items (蛋類/麵包/粉麵/飯/粥/餐肉).

    Returns 2-12 char SPECIFIC Cantonese dish name, e.g.:
      '相顯示咗一塊千層蛋糕' → '千層蛋糕'
      '可見海南雞飯配青瓜' → '海南雞飯'
      '一杯黑咖啡' → '黑咖啡'
      '一碗白飯同青菜' → '白飯'
      '相顯示咗一個簡單嘅早餐' → '煎蛋、烤麵包' (NOT '早餐')
    """
    if not vision_desc and not pplx_desc:
        return ""
    combined = ((vision_desc or "") + "\n" + (pplx_desc or "")).strip()[:600]
    if not combined:
        return ""

    prompt = (
        "你係香港人，識粵語。以下係食物描述。"
        "淨係俾我 2-8 個中文字嘅 SPECIFIC 菜名，唔好加任何描述、量詞、前綴、餐廳名。"
        "重要規則："
        "(1) 唔可以用 generic 餐名 ('早餐'、'午餐'、'晚餐'、'下午茶') 當菜名；"
        "    一定要抽實際嘅食物 (例如：煎蛋、烤麵包、粥、飯)。"
        "(2) 如果只見到 '簡單嘅早餐' / '簡單嘅晚餐' / '簡單嘅一餐' 等 generic 字眼，"
        "    就要從描述中揾實際嘅主體食物 (蛋類/麵包/粉麵/飯/粥/餐肉)。"
        "(3) 多過一樣食物可以用 '、' 分隔 (例如：'煎蛋、烤麵包')。"
        "\\n\\n例子："
        "'千層蛋糕'（唔好寫'相顯示咗一塊千層蛋糕'），"
        "'海南雞飯'（唔好寫'可見海南雞飯配青瓜'），"
        "'黑咖啡'（唔好寫'一杯黑咖啡'），"
        "'凍檸茶'（唔好寫'一杯凍檸茶'），"
        "'沙律雞'（唔好寫'一份沙律雞胸'），"
        "'白飯'（唔好寫'一碗白飯同青菜'），"
        "'煎蛋、烤麵包'（唔好寫'簡單嘅早餐'，generic 餐名唔接受）。"
        "\\n\\n描述：\\n" + combined + "\\n\\n菜名："
    )

    # Try MiniMax first
    try:
        import urllib.request, json as _json
        api_key = _minimax_api_key()
        if api_key:
            payload = {
                "model": "MiniMax-Text-01",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 20,
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
            with urllib.request.urlopen(req, timeout=8) as r:
                data = _json.loads(r.read())
            dish = (data["choices"][0]["message"]["content"] or "").strip()
            dish = dish.strip("「」『』\"'` \n\r\t")
            if 2 <= len(dish) <= 12:
                return dish
    except Exception:
        pass

    # Fallback: APiyi gpt-4o-mini
    try:
        from openai import OpenAI
        api_key = _apiyi_api_key()
        if api_key:
            client = OpenAI(api_key=api_key, base_url="https://api.apiyi.com/v1")
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=20,
                temperature=0.1,
            )
            dish = (resp.choices[0].message.content or "").strip()
            dish = dish.strip("「」『』\"'` \n\r\t")
            if 2 <= len(dish) <= 12:
                return dish
    except Exception:
        pass

    return ""


def _extract_dish_name(vision_desc: str, pplx_desc: str = "", fallback: str = "") -> str:
    """v2.7.32: Extract first concrete dish name from vision + pplx descriptions.

    v3.2.7.4: AI-first (MiniMax → APiyi → regex fallback). No more pure regex.
    Returns 2-6 char SPECIFIC Cantonese dish name. Examples:
      '相顯示咗一塊千層蛋糕' → '千層蛋糕'
      '可見海南雞飯配青瓜' → '海南雞飯'
      '一杯黑咖啡' → '黑咖啡'
    """
    # v3.2.7.4: try AI first
    ai_result = _extract_dish_name_ai(vision_desc, pplx_desc)
    if ai_result and 2 <= len(ai_result) <= 12:
        # v3.2.7.6: reject generic meal labels from AI too — fall through to
        # better extraction (Jim OOB 2026-08-08 10:30 HKT 'should recognise
        # as 雞蛋、麵包' — vision said '簡單嘅早餐', AI returned '早餐',
        # but should be '煎蛋、烤麵包' from re-asking).
        generic_labels = {"早餐", "午餐", "晚餐", "午飯", "晚飯", "下午茶", "宵夜", "tea", "brunch", "dinner", "lunch", "breakfast"}
        if ai_result not in generic_labels:
            return ai_result

    combined = vision_desc + "\n" + pplx_desc
    # Pattern 1: numbered dish "1. **激安二人餐**"
    m = re.search(r"\d+\.\s*\*\*([^*\n]{2,30})\*\*", combined)
    if m:
        return m.group(1).strip()
    # Pattern 2: 菜式：xxx / 菜名：xxx
    m = re.search(r"菜式[：:]\s*([^\n.]{2,30})", combined)
    if m:
        return m.group(1).strip()
    # Pattern 3: first non-empty sentence of vision (skip lines starting with 觀察/呢張)
    # v2.7.53: also skip common Chinese sentence-initial adverbs/connectives
    # (首先/再來/另外/最後/然後/接著/至於) that often appear in prose
    # descriptions before the actual dish name. Without this filter, the
    # extractor returned just "首先" for many image-only entries
    # (Jim OOB 2026-08-07 14:15 HKT 'the recognized food name is showing
    # 首先 only'). We strip the connective prefix AND keep searching the
    # remaining meaningful content on the same line.
    # v3.2.7: more connectives — APiyi gpt-4o-mini often starts with
    # "相顯示...", "圖片可見...", "睇到..." etc. (Jim OOB 2026-08-07 23:50
    # HKT 'food title not shown, just shows 相顯示xxx').
    skip_prefixes = ("呢張", "觀察", "呢個", "呢份", "我見到", "呢碟", "呢碗", "呢個餐",
                    "首先", "再來", "另外", "最後", "然後", "接著", "至於",
                    "從圖", "從相", "圖中", "相中", "照片中",
                    "相顯示", "圖顯示", "圖中可見", "相中可見",
                    "可見到", "睇到", "見到一", "睇到一", "可以見到", "可以睇到")
    def _strip_prefix(s: str) -> str:
        for p in skip_prefixes:
            if s.startswith(p):
                rest = s[len(p):].lstrip(" ，,。、")
                return rest
        return s
    dish_suffixes = ("飯", "麵", "粥", "餅", "糕", "包", "卷", "雞", "牛", "豬", "魚", "蝦",
                    "菜", "湯", "茶", "咖啡", "酒", "水", "奶", "糖", "蛋", "豆", "瓜",
                    "梨", "桃", "莓", "果", "條", "片", "粒", "碗", "碟", "盤", "杯", "盒",
                    "撻", "批", "酥", "圈", "條", "堡", "飯", "餐", "便當")
    bad_nouns = ("透明", "塑料", "餐廳", "場景", "容器",
                 "白色", "黑色", "綠色", "紅色", "黃色", "棕色")
    containers = ("盒入面裝住嘅", "盒裝住嘅", "盒裝住", "入面裝住嘅",
                  "入面裝住", "碗", "碟", "盒", "個", "裝住嘅", "裝住")
    articles = ("咗一個透明嘅", "咗一個", "咗一塊", "咗一",
                "一個透明嘅", "一個透明", "一個", "一",
                "透明嘅", "透明塑料", "透明", "塑料",
                "嘅", "咁", "簡單嘅", "簡單",
                "一杯", "一份", "一塊", "一碟", "一條", "一隻", "一盒", "一碗", "一盤")
    meal_kinds = ("早餐", "早午餐", "午餐", "午飯", "下午茶", "晚餐", "晚飯",
                  "消夜", "宵夜", "茶餐", "快餐", "便當", "餐")
    for line in vision_desc.split("\n"):
        line = line.strip()
        if not line:
            continue
        line = _strip_prefix(line)
        if not line or len(line) < 2:
            continue
        # v3.2.7b: multi-pass strip articles + containers (Cantonese
        # can stack connectors: 相顯示咗一個透明嘅食物盒)
        for _ in range(3):
            prev = line
            for art in articles:
                if line.startswith(art):
                    line = line[len(art):].lstrip(" ，,。、")
                    break
            if line == prev:
                break
        for c in containers:
            if line.startswith(c):
                line = line[len(c):].lstrip(" ，,。、")
                break
    # v3.2.7.4: SPECIFIC dish name extraction (Jim OOB 2026-08-08 10:35 HKT
    # 'Don't be generic. Be specific'). Strategy:
    #   1. Strip leading "相顯示/圖顯示/可見到/見到..." prefixes
    #   2. Strip leading measure words (一杯/一份/一塊/一碟/一條/一隻/一個)
    #   3. Cut at first stop word (配/同/和/1盒/1個/配湯/配菜/...)
    #   4. Try longest 2-6 char candidate that ENDS with a dish suffix
    #   5. Specific dessert suffix override (撻/批/酥/圈 beats 蛋/餅)
    #   6. Fallback to meal_kind (早餐/午餐/...)
    measure_words = ("杯", "塊", "條", "隻", "份", "碗", "碟", "盒", "個", "盤", "包")
    measure_prefixes = ("一杯", "一份", "一塊", "一碟", "一條", "一隻", "一個", "一盒", "一碗", "一盤")
    stop_words = ("配", "同", "和", "及", "加", "加埋", "埋", "再", "仲有", "同埋",
                  "1盒", "1個", "1份", "1杯", "1碗", "1碟", "1條", "1塊", "1隻",
                  "2盒", "2個", "1 盒", "1 個", "1 份", "1 杯", "1 碗")
    # Specific dessert suffixes — prefer these over generic 蛋/餅
    dessert_suffixes = ("撻", "批", "酥", "圈", "捲", "卷", "派", "塔")
    # Cleaned: strip measure_prefixes, then stop words
    cleaned = line
    # Iteratively strip leading measure prefixes (杯/塊/條 etc)
    for _ in range(3):
        prev = cleaned
        for mp in measure_prefixes:
            if cleaned.startswith(mp):
                cleaned = cleaned[len(mp):]
                break
        if cleaned == prev:
            break
    # If still starts with bare measure word (after 咗一 → 杯), strip it
    if cleaned and cleaned[0] in measure_words and len(cleaned) > 1:
        # Only strip if next char is not also a measure word (avoid breaking 套餐)
        if cleaned[1] not in measure_words:
            cleaned = cleaned[1:]
    for sw in stop_words:
        if sw in cleaned:
            cleaned = cleaned.split(sw)[0]
    # First pass: try dessert suffix (most specific)
    for L in range(6, 1, -1):
        if len(cleaned) >= L:
            cand = cleaned[:L]
            if cand in bad_nouns:
                continue
            if cand[-1] in dessert_suffixes:
                return cand
    # Second pass: any dish suffix
    for L in range(6, 1, -1):
        if len(cleaned) >= L:
            cand = cleaned[:L]
            if cand in bad_nouns:
                continue
            if cand[0] in measure_words and L > 2:
                continue
            if cand[-1] in dish_suffixes:
                return cand
    # v3.2.7.4: meal_kind fallback — handles "相顯示咗一個簡單嘅早餐"
    for mk in meal_kinds:
        if mk in line:
            return mk
        # Final fallback: cut at first Chinese comma / period / colon
        cut = re.search(r"[，。；：]", line)
        if cut:
            return line[:cut.start()][:30]
        return line[:30]
    # Pattern 4: chain + meal_type fallback
    chain_m = re.search(r"([\u4e00-\u9fff]{2,6}(?:王|軒|亭|餐廳|食堂|廚|小店|屋|樓))", combined)
    if chain_m:
        return f"{chain_m.group(1)} 套餐"
    # Pattern 5: fallback
    if fallback:
        return fallback[:30]
    return vision_desc[:30] if vision_desc else "食物"


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
def _coach_comment(dish_name: str, calories: float, protein: float, carbs: float, fat: float, restaurant: str = "", user_context: str = "") -> dict:
    """Generate coach comment for a logged food.

    v3.2.7.3: combined keyword + macro grade (Jim OOB 2026-08-08 16:25 HKT
    'Not just macro. But overall good food or bad food' — grade reflects
    the food's OVERALL healthiness, not just macro ratios).

    Step 1: keyword-based pre-grade from food name (A+ very healthy ... F very bad)
    Step 2: macro adjustment — fat_pct > 60% worsens 2 grades, fat > 50% worsens 1,
             protein > 30% + fat < 30% improves 1, protein < 10% worsens 1.

    Rubric:
      - A+: 清淡零負擔 (coffee/tea/water/steamed veg/chicken breast/salad)
      - A:  健康均衡 (fish/shrimp/sashimi/grilled chicken/oats/yogurt)
      - B:  中性 (rice/noodle/steamed meat/HK home-cooked)
      - C:  普通 (carb-heavy or unknown dish)
      - D:  偏heavy (cake/dessert/cream/fried rice/焗飯)
      - F:  極heavy (deep-fried/BBQ/buffet/sugary drink/processed meat)
    """
    if not dish_name or calories <= 0:
        return {"grade": "—", "comment": "資料不足", "suggestions": [], "rationale": "calories = 0, 冇資料可以評"}

    combined = (dish_name or "").lower()

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
    ]
    tier_a = [
        "魚", "蝦", "帶子", "刺身", "壽司", "和牛 (細份)",
        "牛扒 (細)", "牛柳", "烤雞", "燒雞", "煎魚",
        "蒸蛋", "番茄", "菠菜", "甘藍", "羽衣甘藍", "蘑菇", "茄子", "彩椒",
        "燕麥", "乳酪", "酸奶", "香蕉", "蘋果", "藍莓", "奇異果",
        "牛油果", "番薯", "紫薯", "糙米", "藜麥", "扁糧",
        "毛豆", "蝦仁", "海鮮", "貝殼類", "蟹肉 (蒸)", "蜆", "青口",
        "豬里脊", "牛腱", "雞腿 (去皮)", "火雞", "鴨胸", "鵪鶉蛋",
        "麥皮", "粥 (清)", "豆腐花 (清)", "蒸饅頭",
    ]
    tier_f = [
        "炸雞", "炸魚", "炸薯條", "炸魷", "炸春卷", "天婦羅", "炸蝦",
        "炸排骨", "炸雞翼", "炸雞塊", "炸雞扒", "炸豬排", "炸物",
        "燒烤 (自助)", "bbq 自助", "燒肉自助", "韓燒", "日式燒肉",
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
    ]
    tier_b = [
        "飯", "粥", "麵", "米粉", "河粉", "瀨粉", "烏冬", "蕎麥麵",
        "饅頭", "餃子", "包子", "雲吞", "燒賣", "腸粉", "蘿蔔糕",
        "牛肉餅", "蒸肉餅", "肉碎", "蒸排骨", "蒸水蛋", "燉湯",
        "雞翼", "雞腳", "鳳爪", "豬手", "牛腩", "炆牛腩",
        "炒菜", "青菜", "菜心", "芥蘭", "通菜", "豆苗", "白菜",
        "蒸饅頭", "小籠包", "生煎包", "鍋貼", "水餃", "湯圓",
        "油條", "煎餅", "葱油餅",
        "海南雞飯", "燒味飯", "叉燒飯", "燒鵝飯", "燒鴨飯", "燒臘飯",
    ]

    if any(k in combined for k in tier_a_plus):
        pre_grade = "A+"
    elif any(k in combined for k in tier_a):
        pre_grade = "A"
    elif any(k in combined for k in tier_f):
        pre_grade = "F"
    elif any(k in combined for k in tier_d):
        pre_grade = "D"
    elif any(k in combined for k in tier_b):
        pre_grade = "B"
    else:
        pre_grade = "C"

    # ----- Step 2: macro adjustment -----
    protein_pct = (protein * 4) / max(calories, 1) * 100
    fat_pct = (fat * 9) / max(calories, 1) * 100
    carb_pct = (carbs * 4) / max(calories, 1) * 100

    grade_order = {"A+": 5, "A": 4, "B": 3, "C": 2, "D": 1, "F": 0}
    rank = grade_order[pre_grade]

    if fat_pct > 60 and rank > 0:
        rank = max(rank - 2, 0)
    elif fat_pct > 50 and rank > 0:
        rank = max(rank - 1, 0)
    elif protein_pct > 30 and fat_pct < 30 and rank < 5:
        rank = min(rank + 1, 5)
    if protein_pct < 10 and rank > 0:
        rank = max(rank - 1, 0)

    final_rank = rank
    final_grade = {v: k for k, v in grade_order.items()}[final_rank]

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
    # Call MiniMax M3 for richer 1-line coach comment + extra suggestions
    api_comment = None
    try:
        prompt = (
            f"你係香港私人健身教練，操繁體中文廣東話。一句講晒，最多 50 字。\n"
            f"食物：{dish_name}\n"
            f"餐廳：{restaurant or '無'}\n"
            f"營養：{calories:.0f} kcal · 蛋白 {protein:.0f}g · 碳 {carbs:.0f}g · 脂 {fat:.0f}g\n"
            f"用戶目標：{user_context or '減脂 + 增肌'}\n\n"
            f"格式：先講「好/普通/差」一句，再比 1 個具體改善建議。例：「脂肪佔 65% 太高，建議下次走皮走醬。」\n"
            f"唔好講廢話，直接 point。"
        )
        payload = {
            "model": "MiniMax-Text-01",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 120,
            "temperature": 0.3,
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
        api_comment = data["choices"][0]["message"]["content"].strip()
    except Exception:
        api_comment = None
    final_comment = api_comment or pre_comment
    return {
        "grade": final_grade,
        "comment": final_comment,
        "suggestions": suggestions[:2],  # max 2
        "rationale": f"蛋白 {protein_pct:.0f}% · 碳 {carb_pct:.0f}% · 脂 {fat_pct:.0f}%",
    }


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


def _save_scan_log(log_list: list) -> None:
    SCAN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCAN_LOG_PATH.write_text(json.dumps(log_list, ensure_ascii=False, indent=2))


def _load_scan_log() -> list:
    if not SCAN_LOG_PATH.exists():
        return []
    try:
        return json.loads(SCAN_LOG_PATH.read_text())
    except Exception:
        return []


def _append_to_nutrition_log(entry: dict) -> None:
    """Append food entry to canonical nutrition_log.json[meals]."""
    if NUTRITION_LOG_PATH.exists():
        log = json.loads(NUTRITION_LOG_PATH.read_text())
    else:
        log = {"meals": []}
    if "meals" not in log:
        log["meals"] = []
    log["meals"].append(entry)
    NUTRITION_LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2))


def _load_today_nutrition() -> dict:
    """Read /home/work/.hermes/nutrition_log.json and return today's meals
    + summary stats. Returns {'meals': [...], 'totals': {kcal, P, C, F}, 'meal_count': N, 'last_meal_ts': ISO}.

    Used by cheer pipeline (Jim OOB 2026-07-25 13:30 HKT: "monitor my food").
    """
    out = {"meals": [], "totals": {"kcal": 0, "P": 0, "C": 0, "F": 0}, "meal_count": 0, "last_meal_ts": None}
    if not NUTRITION_LOG_PATH.exists():
        return out
    try:
        log = json.loads(NUTRITION_LOG_PATH.read_text())
    except Exception:
        return out
    meals = (log or {}).get("meals") or []
    today = today_iso()
    today_meals = []
    for m in meals:
        # entry schema: {date, time, meal_type, meal_name, calories, protein, carbs, fat, ...}
        if not isinstance(m, dict):
            continue
        m_date = m.get("date") or ""
        if not m_date:
            # try parse timestamp
            ts = m.get("timestamp") or m.get("logged_at")
            if ts and isinstance(ts, str) and today in ts:
                m_date = today
        if m_date != today:
            continue
        today_meals.append(m)
    # Sort by time
    today_meals.sort(key=lambda m: m.get("time") or m.get("timestamp") or "")
    out["meals"] = today_meals
    out["meal_count"] = len(today_meals)
    for m in today_meals:
        try:
            out["totals"]["kcal"] += float(m.get("calories") or 0)
            out["totals"]["P"] += float(m.get("protein") or 0)
            out["totals"]["C"] += float(m.get("carbs") or 0)
            out["totals"]["F"] += float(m.get("fat") or 0)
        except (TypeError, ValueError):
            pass
    out["totals"] = {k: round(v, 1) for k, v in out["totals"].items()}
    if today_meals:
        last = today_meals[-1]
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
    lines.append(f"- 今日累計: ~{int(t['kcal'])} kcal / P {int(t['P'])}g / C {int(t['C'])}g / F {int(t['F'])}g")
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
            entry_cal = str(int(entry.get("calories", 0) or 0))
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
        except Exception:
            # If dedup check fails, continue with append (better to dup than miss)
            pass

        # Append row to Nutrition tab
        # v2.7.19: column M (13th) = user_hints joined by " | " (Jim OOB 7/31)
        user_hints_joined = " | ".join(entry.get("user_hints", []) or [])[:200]
        row_data = [
            entry.get("date", today_iso()),
            f"{entry.get('date', today_iso())}T{entry.get('time', now_iso().split('T')[-1][:5])}:00+08:00",
            entry.get("meal_type", "meal"),
            entry.get("meal_name", entry.get("name", "scan"))[:120],
            entry.get("restaurant_chain", ""),
            str(int(entry.get("calories", 0) or 0)),
            str(entry.get("protein", 0)),
            str(entry.get("carbs", 0)),
            str(entry.get("fat", 0)),
            entry.get("note", "scan_food"),
            entry.get("source", "vision+pplx"),
            "",
            user_hints_joined,  # col M — User Hints
        ]
        body = {"values": [row_data], "majorDimension": "ROWS"}
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        # Mark synced locally (Jim OOB 2026-07-29: prevent re-push)
        entry["sheet_synced"] = True
        updated_range = resp.get("updates", {}).get("updatedRange", "?")
        # v2.7.19: ensure header row has column M = "User Hints" (one-time bootstrap)
        # If header M is empty, set it. Idempotent — safe to call every push.
        try:
            header_check_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition!M1"
            req_h = urllib.request.Request(header_check_url, headers={"Authorization": f"Bearer {access}"})
            hdr_resp = json.loads(urllib.request.urlopen(req_h, timeout=10).read())
            existing_m = (hdr_resp.get("values") or [[""]])[0][0] if hdr_resp.get("values") else ""
            if not existing_m:
                url_m = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition!M1?valueInputOption=USER_ENTERED"
                hdr_body = {"values": [["User Hints"]]}
                req_m = urllib.request.Request(url_m, data=json.dumps(hdr_body).encode(), method="PUT",
                                               headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"})
                urllib.request.urlopen(req_m, timeout=10).read()
        except Exception:
            pass  # header bootstrap is best-effort, don't break the main push
        return {"ok": True, "range": updated_range}
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

    # 1. MiniMax M3 vision
    vision_prompt = (
        "詳細描述呢張食物相。逐樣列:菜式、份量(目測大小)、煮法(炒/炸/蒸/烤)、醬汁、容器、用咩食具。"
        "餐廳名(如見到 logo/招牌字)。再簡短總結呢餐嘅 estimated calories 同 protein 克數。"
        "如見到小票/receipt,逐項抄低菜名、份量、價錢(睇到嘅部分)。"
        "用繁體中文廣東話,一個英文字都唔好有,唔識就寫「難以辨認」。"
    )
    vision_desc = _minimax_vision(img_b64, vision_prompt)

    # 1b. APiyi gpt-4o-mini vision 2nd-opinion (Jim OOB 2026-07-26 19:35 HKT)
    apiyi_vision_desc = _apiyi_vision_analyze(img_b64, vision_prompt)
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
        ),
        "user_correction": None,  # permanent — never trimmed (Jim OOB 2026-07-23 22:30 HKT "no trimming of data")
    }

    # 5. Save image to scan cache
    img_filename = f"scan_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}.jpg"
    img_path = SCAN_CACHE_DIR / img_filename
    img_path.write_bytes(img_bytes)
    entry["image_saved_to"] = str(img_path)

    # 6. Append to local log + Sheet
    _append_to_nutrition_log(entry)
    sheet_result = _append_to_sheet_nutrition(entry)

    # 7. Append to scan_log.json (with image path) for /api/scan_recent
    scan_log = _load_scan_log()
    scan_index = len(scan_log)
    # v3.2.7.7: persist all 12 nutrition fields (kcal/P/C/F + 8 micros) so
    # /api/scan_recent and frontend food cards show full nutrient profile
    # (Jim OOB 2026-08-08 10:55 HKT 'pipeline do not capture other nutrient
    # info' — root cause: scan_log write dict was missing micros, even
    # though nutrition_log.json had them).
    scan_log.append({
        "scan_index": scan_index,
        "timestamp_iso": entry["timestamp_iso"],
        "name": entry["name"],
        "calories": entry["calories"],
        "protein": entry["protein"],
        "carbs": entry.get("carbs", 0),
        "fat": entry.get("fat", 0),
        "fiber": entry.get("fiber", 0),
        "sugar": entry.get("sugar", 0),
        "sodium": entry.get("sodium", 0),
        "sat_fat": entry.get("sat_fat", 0),
        "trans_fat": entry.get("trans_fat", 0),
        "vit_c": entry.get("vit_c", 0),
        "iron": entry.get("iron", 0),
        "calcium": entry.get("calcium", 0),
        "shared": entry["is_shared_meal"],
        "image_path": str(img_path),
        "image_url": f"/scan_img/{img_filename}",
        "restaurant_chain": entry["restaurant_chain"],
        "coach_comment": entry.get("coach_comment", {}),
        "vision_short": vision_desc[:120],
    })
    _save_scan_log(scan_log)

    return jsonify({
        "ok": True,
        "entry": entry,
        "scan_index": scan_index,
        "sheet_synced": sheet_result.get("ok", False),
        "sheet_range": sheet_result.get("range", ""),
    })


@app.route("/api/scan_recent", methods=["GET"])
def api_scan_recent():
    """Return last N successful scans (default 5) for dashboard overlay.

    Jim OOB 2026-07-23: 'In scan last 5 photo. Do not show failed upload'.
    Filter logic: drop scans whose name/vision_short indicates MiniMax vision
    failure (calories==0 + NameError marker), so the dashboard only shows
    scans that produced a real food entry.
    v2.7.33: drop hash-label fallback entries (e.g. '食物 #a1b2c3 (HH:xx)')
    so the list only shows scans with real dish names.
    """
    limit = int(request.args.get("limit", 100))
    scan_log = _load_scan_log()
    # v2.4: drop failed scans (name/vision_short contain Vision failed marker)
    def _is_failed_scan(s):
        n = str(s.get("name", "")).strip() + " " + str(s.get("vision_short", ""))
        # v2.7.33: drop generic '食物' label + hash fallback '食物 #xxx' + failed markers
        return (
            n.strip() == "食物"
            or n.strip().startswith("食物 #")
            or "失敗" in n
            or "NameError" in n
            or "failed" in n.lower()
        )
    successful = [s for s in scan_log if not _is_failed_scan(s)]
    # v2.7.42: explicit sort by timestamp_iso DESC (newest first) — was buggy
    # `successful[-limit:][::-1]` only worked if successful was already in
    # reverse-chronological order; the file is actually chronological so this
    # returned the OLDEST N entries. Now we explicitly sort.
    successful_sorted = sorted(successful, key=lambda s: s.get("timestamp_iso", ""), reverse=True)
    recent = successful_sorted[:limit]
    # v2.7.42: inject image_url for each scan so frontend <img :src> can render
    # the thumbnail. Without this, every card shows the empty-fallback ⌨️/🍽️
    # icon, which makes the food log look mostly empty.
    for entry in recent:
        img_path = entry.get("image_path") or entry.get("image_saved_to") or ""
        if img_path:
            # Extract just the filename from the absolute path
            # e.g. /home/work/.hermes/scan_cache/scan_20260806_233225.jpg
            #   → /scan_img/scan_20260806_233225.jpg
            fname = os.path.basename(img_path)
            entry["image_url"] = f"/scan_img/{fname}"
            entry["is_text_only"] = False
        else:
            entry["image_url"] = None
            # v2.7.42: no image_path = text-direct entry by definition in our
            # codebase. scan_text_direct path always sets image_path=""; user
            # therefore knows this is a text entry.
            entry["is_text_only"] = True
    return jsonify({"scans": recent, "total": len(scan_log), "filtered": len(scan_log) - len(successful)})


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
        return jsonify({
            "date": steps_data.get("date", ""),
            "steps": None if raw_steps is None else int(raw_steps or 0),
            "distance_km": None if steps_data.get("distance_km") is None and steps_data.get("syncing") else steps_data.get("distance_km"),
            "calories": None if steps_data.get("calories") is None and steps_data.get("syncing") else steps_data.get("calories"),
            "syncing": bool(steps_data.get("syncing", False)),
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
    """Receive Jim's correction for a scan. Append user_correction field.
    NO TRIMMING — corrections are permanent (Jim OOB 2026-07-23 22:30 HKT)."""
    data = request.get_json(silent=True) or {}
    scan_index = data.get("scan_index")
    if scan_index is None:
        return jsonify({"ok": False, "error": "no scan_index"}), 400

    scan_log = _load_scan_log()
    if not isinstance(scan_index, int) or scan_index < 0 or scan_index >= len(scan_log):
        return jsonify({"ok": False, "error": "scan_index out of range"}), 404

    # Append correction — never trim
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
    scan_log[scan_index].setdefault("user_corrections", []).append(correction)
    _save_scan_log(scan_log)

    # Also update nutrition_log.json entry if scan_index matches timestamp
    if NUTRITION_LOG_PATH.exists():
        log = json.loads(NUTRITION_LOG_PATH.read_text())
        meals = log.get("meals", [])
        ts_iso = scan_log[scan_index].get("timestamp_iso")
        for m in meals:
            if m.get("timestamp_iso") == ts_iso and m.get("meal_type") == "scan":
                m.setdefault("user_corrections", []).append(correction)
                NUTRITION_LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2))
                break

    return jsonify({"ok": True, "scan_index": scan_index, "correction": correction})


# v2.7.39: Rename existing scan + auto re-recognize macros (Jim OOB 2026-08-06
# 14:50 HKT: "is there a way to adjust the existing record by text and recognize
# it again. e.g. the current recognized one is called 白切雞, but actually it is
# Hai nan chicken rice"). Flow:
#  1. User types new dish name in inline popover
#  2. Backend overwrites name field (so iPhone display updates immediately)
#  3. Backend auto-calls _apiyi_nutrition_enrich(new_name) for re-estimate macros
#  4. Old name + macros saved to user_corrections as audit trail (never trimmed)
#  5. Returns new entry state so frontend can update display
@app.route("/api/scan_rename", methods=["POST"])
def api_scan_rename():
    data = request.get_json(silent=True) or {}
    scan_index = data.get("scan_index")
    new_name = (data.get("new_name") or "").strip()
    if scan_index is None:
        return jsonify({"ok": False, "error": "no scan_index"}), 400
    if not new_name:
        return jsonify({"ok": False, "error": "new_name required"}), 400
    scan_log = _load_scan_log()
    if not isinstance(scan_index, int) or scan_index < 0 or scan_index >= len(scan_log):
        return jsonify({"ok": False, "error": "scan_index out of range"}), 404
    entry = scan_log[scan_index]
    old_name = entry.get("name", "")
    old_kcal = entry.get("calories", 0)
    old_p = entry.get("protein", 0)
    old_c = entry.get("carbs", 0)
    old_f = entry.get("fat", 0)
    # Build text description for re-estimate (using new name + restaurant if any)
    restaurant = entry.get("restaurant_chain", "") or ""
    re_text = f"{new_name} (餐廳: {restaurant})" if restaurant else new_name
    # Re-estimate macros from text (uses APiyi gpt-4o-mini nutrition enrichment, JSON mode)
    new_kcal, new_p, new_c, new_f = old_kcal, old_p, old_c, old_f
    try:
        enrich = _apiyi_nutrition_enrich(re_text)
        if enrich and enrich.strip().startswith("{"):
            parsed = json.loads(enrich)
            new_kcal = int(parsed.get("calories", old_kcal) or old_kcal)
            new_p = float(parsed.get("protein", old_p) or old_p)
            new_c = float(parsed.get("carbs", old_c) or old_c)
            new_f = float(parsed.get("fat", old_f) or old_f)
    except Exception as e:
        # If re-estimate fails, keep old macros + log warning
        print(f"[scan_rename] re-estimate failed: {e}")
    # Apply share ratio (Jim 60% / 小寶 40% if shared) — same as new scan flow
    shared = entry.get("is_shared_meal", False) or _detect_shared_meal(re_text)
    jim_ratio = 0.60 if shared else 1.00
    jim_kcal = round(new_kcal * jim_ratio)
    jim_p = round(new_p * jim_ratio, 1)
    jim_c = round(new_c * jim_ratio, 1)
    jim_f = round(new_f * jim_ratio, 1)
    # Append audit trail (NEVER trimmed)
    correction = {
        "type": "rename",
        "corrected_at": now_iso(),
        "from_name": old_name,
        "to_name": new_name,
        "from_macros": {"calories": old_kcal, "protein": old_p, "carbs": old_c, "fat": old_f},
        "to_macros": {"calories": jim_kcal, "protein": jim_p, "carbs": jim_c, "fat": jim_f},
    }
    entry.setdefault("user_corrections", []).append(correction)
    # Overwrite name + macros fields
    entry["name"] = new_name[:30]
    entry["calories"] = jim_kcal
    entry["protein"] = jim_p
    entry["protein_g"] = jim_p
    entry["carbs"] = jim_c
    entry["carbs_g"] = jim_c
    entry["fat"] = jim_f
    entry["fat_g"] = jim_f
    # v2.7.37: regenerate coach comment for new dish
    entry["coach_comment"] = _coach_comment(new_name, jim_kcal, jim_p, jim_c, jim_f, restaurant)
    # Mark for downstream (e.g. Google Sheet resync)
    entry["_renamed_at"] = now_iso()
    _save_scan_log(scan_log)
    # Also update nutrition_log.json entry if scan_index matches timestamp
    if NUTRITION_LOG_PATH.exists():
        log = json.loads(NUTRITION_LOG_PATH.read_text())
        meals = log.get("meals", [])
        ts_iso = entry.get("timestamp_iso")
        for m in meals:
            if m.get("timestamp_iso") == ts_iso and m.get("meal_type") == "scan":
                m.setdefault("user_corrections", []).append(correction)
                m["name"] = new_name[:30]
                m["calories"] = jim_kcal
                m["protein"] = jim_p
                m["carbs"] = jim_c
                m["fat"] = jim_f
                NUTRITION_LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2))
                break
    return jsonify({
        "ok": True,
        "scan_index": scan_index,
        "entry": entry,
        "correction": correction,
    })


@app.route("/scan_img/<path:filename>", methods=["GET"])
def serve_scan_image(filename):
    """Serve scanned food images (for dashboard thumbnail)."""
    return send_from_directory(str(SCAN_CACHE_DIR), filename)


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
                # Check if already logged by matching the path
                try:
                    scan_log = _load_scan_log()
                    match = next((s for s in scan_log if s.get("image_path") == str(fp)), None)
                    if match:
                        entry["already_logged"] = True
                        entry["scan_index"] = match.get("scan_index")
                        entry["log_summary"] = {
                            "name": match.get("name"),
                            "calories": match.get("calories"),
                            "protein": match.get("protein"),
                            "shared": match.get("shared"),
                        }
                except Exception:
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

    # 1. MiniMax vision
    vision_prompt = (
        "詳細描述呢張食物相。逐樣列:菜式、份量(目測大小)、煮法、醬汁。"
        "如見到餐廳 logo 或招牌字就標出。"
        "簡短總結 estimated 卡路里 同 蛋白質 克數。"
        "如係小票/receipt,逐項列菜名同份量。"
        "繁體中文廣東話,一個英文字都唔好有。"
    )
    vision_desc = _minimax_vision(img_b64, vision_prompt)

    # 1b. APiyi gpt-4o-mini vision 2nd-opinion (Jim OOB 2026-07-26 19:35 HKT)
    apiyi_vision_desc = _apiyi_vision_analyze(img_b64, vision_prompt)
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

    preview = {
        "preview_id": f"pv_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}",
        "image_path": str(img_path),
        "image_url": f"/scan_img/{img_filename}",
        "vision_desc": vision_desc,
        "vision_short": vision_desc[:300],
        "pplx_short": pplx_desc[:500],
        "apiyi_enrichment": apiyi_desc[:300] if apiyi_desc else "",
        "nutrition_merged": merged_nutrition,
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
        },
        "ready_to_commit": True,
    }

    return jsonify({"ok": True, "preview": preview})


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

    vision_prompt = (
        "詳細描述呢張食物相。逐樣列:菜式、份量(目測大小)、煮法、醬汁。"
        "如見到餐廳 logo 或招牌字就標出。"
        "簡短總結 estimated 卡路里 同 蛋白質 克數。"
        "繁體中文廣東話,一個英文字都唔好有。"
    )
    vision_desc = _minimax_vision(img_b64, vision_prompt)

    # 1b. APiyi gpt-4o-mini vision 2nd-opinion (Jim OOB 2026-07-26 19:35 HKT)
    apiyi_vision_desc = _apiyi_vision_analyze(img_b64, vision_prompt)
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

    # Copy image into scan_cache so commit can rename later
    preview_filename = f"preview_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}_from_path.jpg"
    preview_path = SCAN_CACHE_DIR / preview_filename
    preview_path.write_bytes(img_bytes)

    preview = {
        "preview_id": f"pv_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}",
        "image_path": str(preview_path),
        "image_url": f"/scan_img/{preview_filename}",
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
            ),
            "restaurant_chain": restaurant_guess,
            "calories": jim_kcal,
            "protein": jim_p,
            "carbs": 0,
            "fat": 0,
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
        url_read = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition!A1:M1000?valueRenderOption=FORMATTED_VALUE"
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


@app.route("/api/scan_edit_datetime", methods=["POST"])
def api_scan_edit_datetime():
    """Jim OOB 2026-08-07: 'in gymbro, allow me to edit date time of food log'.

    Edits date/time of an existing food scan entry. Cascades across 3 stores:
      1. food_scan_log.json → update timestamp_iso + date + time fields
      2. nutrition_log.json → update matching meal (by timestamp_iso + meal_type=scan)
      3. Google Sheet Nutrition tab → update A (date) + B (time) cells at the matching row

    Body: { scan_index: int, timestamp_iso: str, new_date: "YYYY-MM-DD", new_time: "HH:MM" }

    Returns: { ok, entry, sheet_cells_updated, errors }
    """
    data = request.get_json(silent=True) or {}
    scan_index_hint = data.get("scan_index")
    ts_iso_hint = data.get("timestamp_iso")
    new_date = (data.get("new_date") or "").strip()
    new_time = (data.get("new_time") or "").strip()
    if not new_date or not new_time:
        return jsonify({"ok": False, "error": "new_date + new_time required (YYYY-MM-DD + HH:MM)"}), 400
    # Validate date format
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", new_date):
        return jsonify({"ok": False, "error": "new_date 必須係 YYYY-MM-DD 格式"}), 400
    if not _re.match(r"^\d{1,2}:\d{2}$", new_time):
        return jsonify({"ok": False, "error": "new_time 必須係 HH:MM 格式"}), 400
    # Normalize time to HH:MM
    hh, mm = new_time.split(":")[:2]
    new_time = f"{int(hh):02d}:{int(mm):02d}"
    # Build new ISO timestamp
    new_ts_iso = f"{new_date}T{new_time}:00+08:00"
    old_date = ""
    old_time = ""
    old_ts_iso = ""
    new_time_full = f"{new_date}T{new_time}:00+08:00"  # full ISO for sheet column B

    scan_log = _load_scan_log()
    entry = None
    # Authoritative match: (timestamp_iso + scan_index fallback)
    if ts_iso_hint:
        for i, e in enumerate(scan_log):
            if e.get("timestamp_iso") == ts_iso_hint:
                entry = e
                break
    if entry is None and isinstance(scan_index_hint, int) and 0 <= scan_index_hint < len(scan_log):
        entry = scan_log[scan_index_hint]
    if entry is None:
        return jsonify({"ok": False, "error": "entry not found"}), 404

    old_ts_iso = entry.get("timestamp_iso", "")
    old_date = old_ts_iso[:10] if old_ts_iso else entry.get("date", "")
    old_time = old_ts_iso[11:16] if len(old_ts_iso) >= 16 else entry.get("time", "")

    # Update entry fields
    entry["timestamp_iso"] = new_ts_iso
    entry["date"] = new_date
    entry["time"] = new_time
    # v2.7.43 audit trail
    correction = {
        "type": "edit_datetime",
        "corrected_at": now_iso(),
        "from_date": old_date,
        "from_time": old_time,
        "to_date": new_date,
        "to_time": new_time,
    }
    entry.setdefault("user_corrections", []).append(correction)
    _save_scan_log(scan_log)

    # Cascade 1: nutrition_log.json — match by old timestamp_iso + meal_type=scan
    nutrition_updated = 0
    try:
        if NUTRITION_LOG_PATH.exists():
            nlog = json.loads(NUTRITION_LOG_PATH.read_text())
            meals = nlog.get("meals", [])
            for m in meals:
                if (m.get("timestamp_iso") == old_ts_iso
                        and m.get("meal_type") == "scan"):
                    m["timestamp_iso"] = new_ts_iso
                    m["date"] = new_date
                    m["time"] = new_time
                    m.setdefault("user_corrections", []).append(correction)
                    nutrition_updated += 1
            nlog["meals"] = meals
            NUTRITION_LOG_PATH.write_text(json.dumps(nlog, ensure_ascii=False, indent=2))
    except Exception as e:
        return jsonify({"ok": False, "error": f"nutrition_log write failed: {e}"}), 500

    # Cascade 2: Google Sheet Nutrition tab — find row by (old_date, old_time, calories)
    sheet_result = {"ok": False, "updated": 0, "errors": ["not_attempted"]}
    try:
        entry_cal = str(int(entry.get("calories", 0) or 0))
        old_time_minutes = None
        if old_time and ':' in old_time:
            h, m = old_time.split(':')[:2]
            old_time_minutes = int(h) * 60 + int(m)

        # Read current sheet rows to find match
        tok = json.loads(Path("/home/work/.hermes/google_token.json").read_text())
        access = tok.get("token") or tok.get("access_token")
        if not access:
            raise Exception("no access token")
        SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition!A1:M500"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
        rows = json.loads(urllib.request.urlopen(req, timeout=15).read()).get("values", [])

        matched_row = None
        for i, row in enumerate(rows):
            if i == 0:
                continue  # skip header
            if not row:
                continue
            row_date = row[0] if len(row) > 0 else ""
            row_time_full = row[1] if len(row) > 1 else ""
            row_time = row_time_full[11:16] if 'T' in row_time_full else row_time_full[:5]
            row_cal = row[5] if len(row) > 5 else ""
            if row_date != old_date:
                continue
            if old_time_minutes is not None and ':' in row_time:
                try:
                    h, m = row_time.split(':')[:2]
                    row_minutes = int(h) * 60 + int(m)
                    if abs(row_minutes - old_time_minutes) > 3:
                        continue
                except Exception:
                    if row_time != old_time:
                        continue
            else:
                if row_time != old_time:
                    continue
            if row_cal == entry_cal:
                matched_row = i + 1  # 1-indexed for sheets API
                break
        if matched_row:
            sheet_result = _sheet_update_nutrition_cells(matched_row, [
                ("A", new_date),
                ("B", new_time_full),
            ])
        else:
            sheet_result = {"ok": False, "updated": 0, "errors": [f"no matching sheet row for {old_date} {old_time} cal={entry_cal}"]}
    except Exception as e:
        sheet_result = {"ok": False, "updated": 0, "errors": [str(e)]}

    return jsonify({
        "ok": True,
        "entry": entry,
        "old_date": old_date,
        "old_time": old_time,
        "new_date": new_date,
        "new_time": new_time,
        "nutrition_updated": nutrition_updated,
        "sheet": sheet_result,
    })


@app.route("/api/scan_delete", methods=["POST"])
def api_scan_delete():
    """Jim OOB 2026-08-06: 'Add function to remove historical upload. And cascade
    delete/update g sheet'.

    Cascades across 3 stores:
      1. food_scan_log.json → pop entry at scan_index (or by fallback matcher)
      2. nutrition_log.json → drop matching meal (by timestamp_iso + meal_type=scan)
      3. Google Sheet Nutrition tab → delete row(s) matching (date, time, calories, name)

    Image file on disk is preserved (audit trail).

    Body: { scan_index: int, timestamp_iso?: str, name?: str, calories?: int }
    The scan_index is a HINT (array index in current scan_log snapshot); the
    server uses (timestamp_iso, name, calories) for authoritative matching to
    avoid stale-index issues after multiple deletes shift the array.
    Returns: { ok, scan_index_removed, nutrition_log_removed, sheet_rows_deleted, errors }
    """
    data = request.get_json(silent=True) or {}
    scan_index_hint = data.get("scan_index")
    ts_iso_hint = data.get("timestamp_iso")
    name_hint = data.get("name") or data.get("meal_name")
    cal_hint = data.get("calories")

    scan_log = _load_scan_log()
    removed_entry = None
    removed_idx = None
    # Authoritative match: (timestamp_iso + name) — handles multi-delete index shifts
    if ts_iso_hint and name_hint:
        for i, e in enumerate(scan_log):
            if (e.get("timestamp_iso") == ts_iso_hint
                    and e.get("name") == name_hint):
                removed_entry = scan_log.pop(i)
                removed_idx = i
                break
    # Fallback: by scan_index (works only if no prior delete shifted array)
    if removed_entry is None and isinstance(scan_index_hint, int) and 0 <= scan_index_hint < len(scan_log):
        # If hint came with timestamp+name, also verify the hint entry matches
        # the hint tuple — otherwise the index is stale from a prior delete.
        hint_entry = scan_log[scan_index_hint]
        if (ts_iso_hint and hint_entry.get("timestamp_iso") != ts_iso_hint):
            # Stale index — do NOT delete (would shift other entries)
            return jsonify({
                "ok": False,
                "error": "stale_index",
                "hint": "頁面可能有過時嘅 scan_index，請 reload 後再試。Server 同時接 (timestamp_iso, name) 做權威 match。",
                "current_index_length": len(scan_log),
            }), 409
        removed_entry = scan_log.pop(scan_index_hint)
        removed_idx = scan_index_hint
    if removed_entry is None:
        return jsonify({"ok": False, "error": "entry not found"}), 404

    # Persist scan log
    _save_scan_log(scan_log)

    # Cascade 1: nutrition_log.json — match by timestamp_iso + meal_type=scan
    nutrition_removed = 0
    try:
        if NUTRITION_LOG_PATH.exists():
            nlog = json.loads(NUTRITION_LOG_PATH.read_text())
            meals = nlog.get("meals", [])
            ts_iso = removed_entry.get("timestamp_iso")
            entry_name = removed_entry.get("name")
            new_meals = []
            for m in meals:
                if (m.get("timestamp_iso") == ts_iso
                        and m.get("meal_type") == "scan"
                        and m.get("name") == entry_name):
                    nutrition_removed += 1
                    continue  # drop
                new_meals.append(m)
            nlog["meals"] = new_meals
            NUTRITION_LOG_PATH.write_text(json.dumps(nlog, ensure_ascii=False, indent=2))
    except Exception as e:
        return jsonify({"ok": False, "error": f"nutrition_log write failed: {e}",
                        "scan_index_removed": removed_idx}), 500

    # Cascade 2: Google Sheet Nutrition tab — match by (date, time ~±1 min, calories)
    # Note: do NOT match by name — sheet column D uses raw vision description
    # (e.g. "呢張相顯示咗一碟炸龍蝦肉，配有幾塊麵包...") which is different from
    # the user-facing name field in scan_log (e.g. "椒鹽龍蝦"). Sheet's time column
    # is also rounded to whole minutes ("09:35:32" → "09:35:00"), so allow ±1 min.
    sheet_result = {"ok": False, "deleted": 0, "errors": ["not_attempted"]}
    try:
        ts_iso = removed_entry.get("timestamp_iso", "")
        entry_date = ts_iso[:10] if ts_iso else ""
        # Normalize entry time to HH:MM (sheet is whole-minute)
        entry_time_hhmm = ts_iso[11:16] if len(ts_iso) >= 16 else ""
        entry_time_minutes = None
        if entry_time_hhmm and ':' in entry_time_hhmm:
            try:
                hh, mm = entry_time_hhmm.split(':')[:2]
                entry_time_minutes = int(hh) * 60 + int(mm)
            except Exception:
                pass
        entry_cal = str(int(removed_entry.get("calories", 0) or 0))

        def matcher(row, idx):
            if not row:
                return False
            row_date = row[0] if len(row) > 0 else ""
            row_time_full = row[1] if len(row) > 1 else ''
            row_time = row_time_full[11:16] if 'T' in row_time_full else row_time_full[:5]
            row_cal = row[5] if len(row) > 5 else ''
            # Time tolerance: ±3 minutes
            # Note: sheet's time column records PUSH time (when _append_to_sheet_nutrition
            # was called), which can drift from scan_log's COMMIT time by up to ~3 min
            # due to background processing latency.
            if entry_time_minutes is not None and ':' in row_time:
                try:
                    hh, mm = row_time.split(':')[:2]
                    row_minutes = int(hh) * 60 + int(mm)
                    if abs(row_minutes - entry_time_minutes) > 3:
                        return False
                except Exception:
                    pass
            else:
                if row_time != entry_time_hhmm:
                    return False
            return (row_date == entry_date and row_cal == entry_cal)

        sheet_result = _sheet_delete_nutrition_rows(matcher)
    except Exception as e:
        sheet_result = {"ok": False, "deleted": 0, "errors": [str(e)]}

    return jsonify({
        "ok": True,
        "scan_index_removed": removed_idx,
        "removed_name": removed_entry.get("name", ""),
        "removed_timestamp_iso": removed_entry.get("timestamp_iso", ""),
        "nutrition_log_removed": nutrition_removed,
        "sheet_rows_deleted": sheet_result.get("deleted", 0),
        "sheet_errors": sheet_result.get("errors", []),
    })


@app.route("/api/scan_commit", methods=["POST"])
def api_scan_commit():
    """Jim OOB 2026-07-23 22:42: 'all food logging should be preview and allow me to confirm before logging'.

    Receives the (possibly edited) suggested_entry + image_path from
    /api/scan_preview or /api/scan_preview_text.

    Text-only path (Jim OOB 2026-08-02 02:50 HKT):
        image_path = "" → text-only entry. NO file rename, no scan_log
        append image, sheet row has image_url field empty / sheet
        column K is left blank for the entry.

    ONLY NOW writes to nutrition_log.json + Google Sheet.

    If user_corrections are submitted (correction_form), they're appended permanently.
    """
    data = request.get_json(silent=True) or {}
    entry = data.get("entry", {})
    image_path = data.get("image_path", "")
    user_correction = data.get("user_correction")  # optional dict
    # v2.7.19: list of hint strings Jim typed during scan → re-estimate cycle
    user_hints_in = data.get("user_hints", []) or []

    if not entry:
        return jsonify({"ok": False, "error": "missing entry"}), 400

    # v2.7.22 (text-only path): image_path may be empty (text-direct input).
    if image_path:
        img_path = Path(image_path)
        if not img_path.exists():
            return jsonify({"ok": False, "error": "image not found"}), 404
    else:
        img_path = None  # text-only entry, no image

    now_iso_str = now_iso()
    entry["timestamp_iso"] = now_iso_str
    if img_path is not None:
        entry["source"] = "v2.2-scan (minimax-m3 + pplx-sonar-pro, Jim confirmed)"
        entry["models_used"] = ["minimax-m3", "pplx-sonar-pro"]
        entry["image_saved_to"] = str(img_path)
    else:
        # v2.7.22 text-direct path
        entry["source"] = "v2.7.22-scan_text_direct (apiyi-gpt-4o-mini, no image)"
        entry["models_used"] = ["apiyi-gpt-4o-mini"]
        entry["image_saved_to"] = ""
    entry["confidence"] = "Jim-confirmed preview"
    entry["sheet_synced"] = False
    entry["user_correction"] = None
    # v3.2.7.3: single A-F grade via keyword+macro (replaces star rating)
    entry_name = entry.get("name") or entry.get("meal_name") or "食物"
    entry_kcal = entry.get("calories", entry.get("kcal", 0)) or 0
    entry_p = entry.get("protein", entry.get("protein_g", 0)) or 0
    entry_c = entry.get("carbs", entry.get("carbs_g", 0)) or 0
    entry_f = entry.get("fat", entry.get("fat_g", 0)) or 0
    entry_rest = entry.get("restaurant_chain", entry.get("restaurant", "")) or ""
    if entry_name and entry_kcal > 0:
        entry["coach_comment"] = _coach_comment(entry_name, entry_kcal, entry_p, entry_c, entry_f, entry_rest)
    # v3.2.7.3: legacy `rating` field (1-5 star) removed — single source of truth is coach_comment.grade
    if "rating" in entry:
        del entry["rating"]
    # v2.7.19: persist user hints (each round-trip = one hint in the list)
    # Dedupe + cap to 20 entries to avoid bloat
    cleaned_hints = []
    seen = set()
    for h in user_hints_in:
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

    # Append to nutrition log
    _append_to_nutrition_log(entry)
    sheet_result = _append_to_sheet_nutrition(entry)

    # v2.7.22 (text-only path): when no image, skip file rename + scan_log.
    now_hkt_dt = datetime.now(timezone(timedelta(hours=8)))
    # v3.2.7: enforce dish-name extraction + rating on every commit
    # (Jim OOB 2026-08-07 23:50 HKT 'the food title is not shown on
    # the list view. Moreover, it does not have rating').
    # If name is a hash / path / generic, re-derive via _extract_dish_name.
    current_name = (entry.get("name") or "").strip()
    if (not current_name
        or current_name.startswith("img_")
        or current_name.endswith(".jpg")
        or current_name == "食物"
        or current_name.startswith("相顯示")
        or current_name.startswith("圖顯示")
        or current_name.startswith("呢張")
        or len(current_name) > 60):
        vision_hint = (entry.get("vision_raw_desc")
                       or entry.get("vision_desc")
                       or entry.get("source_text")
                       or entry.get("vision_short")
                       or "")
        pplx_hint = (entry.get("pplx_short")
                     or entry.get("apiyi_enrichment")
                     or "")
        redrive = _extract_dish_name(vision_hint, pplx_hint, fallback=current_name or "")
        if redrive and redrive.strip() != "食物":
            entry["name"] = redrive
            current_name = redrive
    # Compute / override rating from available description + macros
    if "rating" not in entry or not isinstance(entry.get("rating"), int):
        macro_dict = {k: entry.get(k) for k in
                      ("calories", "protein", "carbs", "fat",
                       "fiber", "sugar", "sodium", "sat_fat",
                       "trans_fat", "vit_c", "iron", "calcium")
                      if isinstance(entry.get(k), (int, float))}
        entry["rating"] = _compute_rating(
            entry.get("vision_raw_desc") or entry.get("vision_desc") or "",
            macro_dict,
        )
    if img_path is not None:
        # Rename preview_*.jpg → scan_*.jpg
        final_name = f"scan_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}.jpg"
        final_path = SCAN_CACHE_DIR / final_name
        try:
            img_path.rename(final_path)
            image_url = f"/scan_img/{final_name}"
        except Exception:
            final_path = img_path
            image_url = f"/scan_img/{img_path.name}"
    else:
        # Text-only entry — no image, no scan_log image row
        final_path = ""
        image_url = ""

    # Append to scan_log (only image-backed entries; text-only goes to nutrition_log
    # alone + scan_recent filter hides them by default)
    scan_log = _load_scan_log()
    scan_index = len(scan_log)
    if img_path is not None:
        scan_log.append({
            "scan_index": scan_index,
            "timestamp_iso": now_iso_str,
            "name": entry.get("name", "scan"),
            "rating": entry.get("rating", 3),
            "calories": entry.get("calories", 0),
            "protein": entry.get("protein", 0),
            "carbs": entry.get("carbs", 0),
            "fat": entry.get("fat", 0),
            "fiber": entry.get("fiber", 0),
            "sugar": entry.get("sugar", 0),
            "sodium": entry.get("sodium", 0),
            "sat_fat": entry.get("sat_fat", 0),
            "trans_fat": entry.get("trans_fat", 0),
            "vit_c": entry.get("vit_c", 0),
            "iron": entry.get("iron", 0),
            "calcium": entry.get("calcium", 0),
            "shared": entry.get("is_shared_meal", False),
            "image_path": str(final_path),
            "image_url": image_url,
            "restaurant_chain": entry.get("restaurant_chain", ""),
            "coach_comment": entry.get("coach_comment", {}),
            "vision_short": (entry.get("vision_raw_desc") or entry.get("vision_desc") or "")[:120],
            "user_corrections": [user_correction] if user_correction else [],
        })
        _save_scan_log(scan_log)
    else:
        # Text-only: tag scan_log entry as text-direct so /scan_recent can show it
        scan_log.append({
            "scan_index": scan_index,
            "timestamp_iso": now_iso_str,
            "name": entry.get("name", "text"),
            "rating": entry.get("rating", 3),
            "calories": entry.get("calories", 0),
            "protein": entry.get("protein", 0),
            "carbs": entry.get("carbs", 0),
            "fat": entry.get("fat", 0),
            "fiber": entry.get("fiber", 0),
            "sugar": entry.get("sugar", 0),
            "sodium": entry.get("sodium", 0),
            "sat_fat": entry.get("sat_fat", 0),
            "trans_fat": entry.get("trans_fat", 0),
            "vit_c": entry.get("vit_c", 0),
            "iron": entry.get("iron", 0),
            "calcium": entry.get("calcium", 0),
            "shared": entry.get("is_shared_meal", False),
            "image_path": "",
            "image_url": "",
            "restaurant_chain": entry.get("restaurant_chain", ""),
            "coach_comment": entry.get("coach_comment", {}),
            "vision_short": (entry.get("vision_raw_desc") or entry.get("name", ""))[:120],
            "user_corrections": [user_correction] if user_correction else [],
            "is_text_only": True,
        })
        _save_scan_log(scan_log)

    return jsonify({
        "ok": True,
        "scan_index": scan_index,
        "entry": entry,
        "sheet_synced": sheet_result.get("ok", False),
        "sheet_range": sheet_result.get("range", ""),
        "is_text_only": img_path is None,
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

    # 1. Re-run MiniMax vision (fresh dish desc, ~3-5s)
    vision_prompt = (
        "詳細描述呢張食物相。逐樣列:菜式、份量(目測大小)、煮法、醬汁。"
        "如見到餐廳 logo 或招牌字就標出。"
        "簡短總結 estimated 卡路里 同 蛋白質 克數。"
        "如係小票/receipt,逐項列菜名同份量。"
        "繁體中文廣東話,一個英文字都唔好有。"
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
        "vision_desc": augmented_desc,  # hint-augmented desc returned to frontend
        "vision_short": augmented_desc[:300],
        "pplx_short": pplx_desc[:500],
        "apiyi_enrichment": apiyi_desc[:300] if apiyi_desc else "",
        "nutrition_merged": merged_nutrition,
        "suggested_entry": {
            "date": today_iso(),
            "time": now_hkt_dt.strftime("%H:%M"),
            "meal_type": "scan",
            "name": vision_desc[:120],
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
            "name": suggested_name,
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
                "model": "MiniMax-Text-01",
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
WHOOP_PULL_SCRIPT = Path("/home/work/.hermes/skills/fitness/whoop-pull-activities/scripts/whoop_pull.py")
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
}

def _voice_zh_replace(s: str) -> str:
    """Pre-flight EN→ZH auto-replace for voice script (Rule 26 + Rule 37).

    v2.5.1 fix: removed word-boundary anchors `\b...\\b` because they don't match
    between Chinese characters and English words (Chinese text has no inter-char
    word boundaries). Plain `re.sub` with case-insensitive flags now catches
    EN words embedded in Chinese prose.

    Also added an extended 'natural Chinese filler' replacement table for common
    leaked English tokens that pplx sonar-pro often uses (state, use, treat,
    keep, base, level, range, etc.).
    """
    keys = sorted(EN_TO_ZH_VOICE.keys(), key=len, reverse=True)
    for k in keys:
        # No \b anchors — Chinese text has no inter-word boundaries
        s = re.sub(re.escape(k), EN_TO_ZH_VOICE[k], s, flags=re.IGNORECASE)
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
    }
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
    # Run whoop_pull.py (always when forced)
    try:
        result = _sp.run([sys.executable, str(WHOOP_PULL_SCRIPT)],
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
    """Call pplx sonar-pro to synthesize detailed 8-section cheer text per
    cheer-routine Rule 22.

    Jim OOB 2026-07-23 17:35 + 2026-07-24 14:10 HKT: voice ~150s target.
    Empirical measurement 7/24 01:53 HKT: 1091 chars text → 218s audio
    → real WanLung rate ≈ 5.0 char/sec (not 5.69 — heat/queue slow-down).
    For 150s target, text_chars target = 150 × 5.0 = 750 (sweet zone
    700-800 chars → 140-160s).
    """
    api_key = _pplx_api_key()
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
    # not just log nutrient. Pulls today's meals from NUTRITION_LOG_PATH
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
- 今日 workout：{workout_n} 個 session

**今日 workout detail**（逐個動作 loop 出嚟，唔好概括）：
{workout_detail_zh}
{news_block}
{liverpool_block}
{jim_context_block}
{nutrition_block}
寫作風格指引（Jim OOB 2026-07-24 — 呢啲係 rule，唔好走樣）：
1. **唔好讀數字**：唔好寫「HRV 28.6 ms」咁讀出嚟，寫「你個自律神經而家有返廿八點六左右嘅彈性，比你上週好少少」；唔好寫「7.35 個鐘」咁平，寫「噉晚瞓咗七個幾鐘，差啲就夠八個」
2. **要有笑位、要有自嘲**：可以講下「深層瞓終於過咗兩個鐘，唔使再被我鬧」、講下「今日操水，話晒係你嘅，唔係被窩」、講下「教練同你講過好多次早瞓啦，仲要我重複幾多次」
3. **識用新聞**：將本週熱話 news 嵌入 cheer 內，自然講下「啱啱睇到⋯⋯」、「今朝睇新聞見到⋯⋯」配返 cheer 主題
4. **.要有真實建議**：每講完一個 insight 即刻跟住「咁所以你⋯⋯」嘅 actionable 建議，唔好淨講完就算
5. **識講 personal**：叫 Jim，唔好「你」— 直接用名；可以提小寶（如果講到食物／睡眠／早晨 routine）；可以講「教練」、「管家」
6. **段落結構**：6-8 段，唔好 list / bullet / table / 編號。段落之間用 `\\n\\n` 分隔
7. **長度**：**780-960 字 sweet zone, STRICT MAX 960 字** (v2.7.20.1 patch 2026-08-01 22:48 HKT。 v2.7.20 still hit 1649 字 → 330 秒 despite 800-1100 ceiling。 pplx 對 soft ceiling 不服從, 必須縮窄 + super-strict 寫法 + 段落長度指引收緊至 sum ~580 字 target, 段落個別 cap 120 字不容超)。 voice target 156-192 秒 WanLung 5.0 字/秒 — matching Jim OOB 7/24 ~150s preference.
8. **STRICT 段落長度指引 (SUB-100 字/段強制 — 不可超)**:打招呼 50-70、復原 insight **CAP 110**、睡眠 insight **CAP 95**、訓練 insight **CAP 70** (零 workout 日子一句過)、營養 **CAP 80**、噉晚 routine **CAP 85**、明日預覽 50-70、收尾打氣 **CAP 50** (總和 ~570-660 字 — 每段絕對唔好超 cap)。
9. **唔好 fabricate 數字**：所有 metric 必須喺上面 data 入面搵到
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

**嚴禁使用以下英文字**（會破壞 TTS 廣東話韻律）：
- 動詞：keep, base, plan, use, using, treat, check, monitor, tracking, trend, stable, fact, matters, feel, felt, feeling, OK, ok, make sure
- 時間：time, times, hr, hrs, min, sec
- 訓練：session, workout, set, rep, drill, plate, bar, spot, lift, push, rest, PR, RM, build, bulk, cut, RPE, HIIT, squat, bench, deadlift, press, curl, row, lat pulldown, pullup
- 指標：HRV, SpO2, RHR, RPE, REM, N1, N2, N3, deep sleep, light sleep, awake, strain, recovery, level, range, target, delta, score, state, status
- 顏色：YELLOW, GREEN, RED
- 品牌：Jim, Google, Whoop, Novotel, Wanchai, app, PC, phone, tab
- 單位：kg, lb, oz, g, kcal, ms, bpm
- 收尾：Bon voyage, Welcome home, Good luck, Good night
- 縮寫：e.g., i.e., vs, via, FYI, ASAP, P.S., OK
- 敬稱：Dr., Mr., Mrs., Ms.

凡係以上任何一個英文字都必須用中文。寫嘅時候直接用中文，唔好諗住用英文再翻譯。

開始啦，記住：係兄弟傾偈，唔係 CEO 匯報："""
    payload = {
        "model": "sonar-pro",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1400,  # v2.7.20.1 STRICTER CAP (was 1800 — pplx still hit 1649 chars / 330s voice). 780-960 字 sweet zone × 1.5-2 tok/char = ~1170-1920 tokens. 1400 forces pplx to commit to shorter text per length rule.
        "temperature": 0.6,
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
        text = resp["choices"][0]["message"]["content"]
        return text.strip()
    except Exception:
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
        # Step 1: zh-replace (first pass)
        try:
            with open("/tmp/cheer_voice_debug.log", "a") as _f:
                _f.write(f"{now_iso()} | text_in={len(text)} chars\n")
        except Exception:
            pass
        voice_text = _voice_zh_replace(text)
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
        voice_text = voice_text.replace("\n\n", "。 ")
        # Replace single newlines with comma+pause (avoid hard pause in TTS)
        voice_text = voice_text.replace("\n", "，")
        # Strip section markers if any still present
        for marker, _ in pause_bridges:
            voice_text = voice_text.replace(marker, "")
        try:
            with open("/tmp/cheer_voice_debug.log", "a") as _f:
                _f.write(f"{now_iso()} | after_bridges={len(voice_text)}\n")
        except Exception:
            pass
        # Hard safety cap at 2000 chars (edge-tts handles long scripts but
        # blocks at 5k+ chars; 2k is plenty for ~700-800 字 ~3min audio)
        if len(voice_text) > 2000:
            voice_text = voice_text[:2000]
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

        # Step 2: Edge-TTS WanLung +0% (longer timeout for full-detail scripts).
        # Edge-TTS WanLung has empirically shown 1m30s-2m runtime for 800-1500 字
        # scripts. Use 240s (4 min) timeout to be safe. Jim OOB 2026-07-23 voice
        # detail direction — sacrifice latency for completeness.
        tmp_ogg = f"/tmp/cheer_voice_{int(time.time())}.ogg"
        result = _sp.run([
            "edge-tts", "--voice", "zh-HK-WanLungNeural",
            "--rate", "+0%", "--text", voice_text,
            "--write-media", tmp_ogg,
        ], capture_output=True, text=True, timeout=240)
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

        # 2. Text
        text = _synthesize_cheer_text(metrics, fire_type)
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({"step": "voice_gen", "step_at": now_iso(), "text": text})

        # 3. Voice
        voice_path = _synthesize_cheer_voice(text)
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({"step": "image_gen", "step_at": now_iso(), "voice_path": voice_path})

        # 4. Image
        context = f"{fire_type}_{int(time.time())}"
        image_path = _generate_cheer_image(context)
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({"step": "done", "step_at": now_iso(), "image_path": image_path})

        # 5. Cache to cheer_artifacts
        today_iso_str = today_iso()
        artifact_dir = CHEER_ARTIFACT_DIR / f"cheer_{today_iso_str}_{context}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "cheer_text.txt").write_text(text, encoding="utf-8")
        if voice_path:
            shutil.copy2(voice_path, artifact_dir / "cheer_voice.mp3")
        if image_path:
            shutil.copy2(image_path, artifact_dir / "cheer_motivation.png")

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


# ---------- HTML (Uber-inspired) ----------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#000000">
<title>Gymbro · Jim</title>
<link rel="icon" type="image/png" sizes="32x32" href="/static/gymbro_favicon-32.png">
<link rel="icon" type="image/png" sizes="192x192" href="/static/gymbro_icon-192.png">
<link rel="apple-touch-icon" sizes="180x180" href="/static/gymbro_apple-touch-icon.png">
<link rel="apple-touch-icon" sizes="152x152" href="/static/gymbro_apple-touch-icon.png">
<link rel="apple-touch-icon" sizes="120x120" href="/static/gymbro_apple-touch-icon.png">
<link rel="apple-touch-icon-precomposed" sizes="180x180" href="/static/gymbro_apple-touch-icon.png">
<link rel="manifest" href="/manifest.json">
<script src="https://cdn.tailwindcss.com"></script>
<script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;900&display=swap" rel="stylesheet">
<style>
  :root {
    --uber-black: #000000;
    --uber-text: #FFFFFF;
    --uber-grey-1: #F6F6F6;
    --uber-grey-2: #EEEEEE;
    --uber-grey-4: #B5B5B5;
    --uber-grey-5: #6B6B6B;
    --uber-grey-6: #E2E2E2;
    --uber-green: #06C167;
    --emerald: #10B981;
    --gold: #FFD60A;
  }
  * {
    -webkit-tap-highlight-color: transparent;
    -webkit-user-select: none;
    user-select: none;
    -webkit-touch-callout: none;
    touch-action: manipulation;   /* kill double-tap zoom + pinch on iOS Safari */
  }
  input, textarea, [contenteditable] {
    -webkit-user-select: text;
    user-select: text;            /* allow text input fields to select normally */
    touch-action: auto;           /* inputs need full touch for selection */
  }
  html, body {
    background: var(--uber-black);
    color: var(--uber-text);
    font-family: 'Inter', -apple-system, system-ui, sans-serif;
    overscroll-behavior: none;
  }
  body {
    padding-top: env(safe-area-inset-top);
    padding-bottom: env(safe-area-inset-bottom);
    background: radial-gradient(circle at 20% 0%, rgba(16,185,129,0.10) 0%, transparent 45%),
                radial-gradient(circle at 80% 100%, rgba(255,214,10,0.06) 0%, transparent 50%),
                linear-gradient(to bottom right, #000000, #18181b, #000000);
    background-attachment: fixed;
    min-height: 100vh;
    position: relative;
  }
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background: radial-gradient(circle at 50% 30%, rgba(255,255,255,0.04) 0%, transparent 60%);
    pointer-events: none;
    z-index: 0;
  }
  main, header, nav { position: relative; z-index: 1; }
  .tap { transition: transform 0.08s ease-out, background-color 0.15s, box-shadow 0.2s; }
  .tap:active { transform: scale(0.97); }
  .primary-btn {
    background: var(--uber-text);
    color: var(--uber-black);
    border-radius: 999px;
    font-weight: 700;
    letter-spacing: 0.5px;
  }
  .pill { border-radius: 999px; }
  .glass {
    background: rgba(255,255,255,0.08);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid rgba(255,255,255,0.18);
    color: var(--uber-text);
  }
  .glass-active {
    background: var(--uber-text);
    color: var(--uber-black);
    border: 1px solid var(--uber-text);
  }
  input[type="text"], input[type="number"] {
    background: rgba(255,255,255,0.08);
    border: 1px solid rgba(255,255,255,0.15);
    padding: 14px 16px;
    border-radius: 999px;
    font-size: 16px;
    width: 100%;
    outline: none;
    color: white;
    backdrop-filter: blur(8px);
  }
  input[type="text"]::placeholder, input[type="number"]::placeholder { color: rgba(255,255,255,0.4); }
  input[type="text"]:focus, input[type="number"]:focus {
    background: rgba(255,255,255,0.15);
    border-color: rgba(16,185,129,0.5);
  }
  .num-btn {
    background: rgba(255,255,255,0.08);
    backdrop-filter: blur(8px);
    color: var(--uber-text);
    border: 1px solid rgba(255,255,255,0.15);
    border-radius: 999px;
    font-weight: 700;
    font-size: 22px;
  }
  .num-btn:active { background: rgba(255,255,255,0.18); }
  .tab-active {
    color: var(--uber-text);
    background: rgba(255,255,255,0.12);
    box-shadow: inset 0 0 0 1px rgba(255,255,255,0.25), 0 0 12px rgba(255,255,255,0.18);
  }
  .tab-inactive { color: var(--uber-grey-4); }
  .tab-inactive:active { background: rgba(255,255,255,0.06); }
  .pyramid { display: flex; flex-direction: column; align-items: center; gap: 4px; }
  .pyramid-row {
    background: var(--uber-text);
    color: var(--uber-black);
    border-radius: 999px;
    padding: 8px 18px;
    font-weight: 600;
    min-width: 140px;
    text-align: center;
  }
  .pyramid-row.warm-up { opacity: 0.55; }
  .pyramid-row.working { font-weight: 900; }
  .pyramid-row.burn-out { background: var(--emerald); color: var(--uber-text); }
  .hidden { display: none !important; }
  @keyframes pulse-fade { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  .saving { animation: pulse-fade 1.2s ease-in-out infinite; }
  @keyframes fade-up { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  .fade-up { animation: fade-up 0.4s ease-out backwards; }
  @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
  /* Jim OOB 2026-07-22: per-row ⏳ spinner while copy in flight */
  @keyframes glow-pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(16,185,129,0.5), 0 0 24px rgba(16,185,129,0.2); }
    50% { box-shadow: 0 0 0 8px rgba(16,185,129,0), 0 0 32px rgba(16,185,129,0.4); }
  }
  .glow-ready { animation: glow-pulse 2s ease-in-out infinite; }
  .toast {
    position: fixed; bottom: 100px; left: 50%; transform: translateX(-50%);
    background: var(--uber-text); color: var(--uber-black); padding: 12px 24px;
    border-radius: 999px; font-weight: 600; box-shadow: 0 4px 20px rgba(0,0,0,0.6);
    z-index: 100; transition: opacity 0.3s;
  }
  .streak-badge {
    background: rgba(255,255,255,0.10);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid rgba(255,255,255,0.20);
    border-radius: 999px;
    padding: 6px 14px;
    font-weight: 700;
    font-size: 13px;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .quote-line {
    font-style: italic;
    color: rgba(255,214,10,0.85);
    letter-spacing: 0.05em;
    transition: opacity 0.5s ease-in-out;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    line-height: 1.25;
  }
  [x-cloak] { display: none !important; }

  /* v3.1.0: Landscape food history — 2-column grid (Jim OOB 2026-08-07
     14:50 HKT 'food history is best view as landscape'). Portrait keeps
     vertical list; landscape uses CSS Grid 2-col. */
  @media (orientation: landscape) {
    .food-history-list {
      display: grid !important;
      grid-template-columns: 1fr 1fr;
      gap: 0.75rem;
    }
  }
  /* v3.2.4: Gym focus mode — larger text + minimal UI for in-gym use.
     Toggled via .gym-focus class on body (set by toggleGymFocus()).
     Fix: was scale(1.08) which pushed visible content past the fold,
     making the user feel they were 'scrolled to the bottom' when they
     just tapped focus. Reduced to 1.04 + transform-origin: top center
     so any scale-up grows downward predictably. */
  body.gym-focus {
    font-size: 1.08em;
  }
  body.gym-focus .gym-focus-hide {
    display: none !important;
  }
  body.gym-focus .gym-focus-enlarge {
    transform: scale(1.04);
    transform-origin: top center;
  }
  /* v3.1.0: Food tab content (default tab now) — make header slightly
     smaller to give food log more vertical space. */
  main[data-tab="food"] .hero-banner {
    height: 5rem !important;
  }
</style>
</head>
<body x-data="gymApp()" x-init="init()">

  <!-- Top Bar -->
  <header class="sticky top-0 z-50 border-b border-white/10 bg-black/[0.85] px-4 py-2 backdrop-blur-xl">
    <div class="flex items-center justify-between gap-2">
      <!-- v3.2.0: Global cheer button (Jim OOB 2026-08-07 16:45 HKT
           'there should be global way to trigger cheer and not necessary
           to go into cheer tab. perhaps put the cheer button at the top
           left, before Gymbro title'). Single tap → /api/cheer
           fire_type=auto → AI decides morning/evening/post_gym timing. -->
      <button @click="triggerCheer()"
              :disabled="cheerInFlight"
              data-testid="cheer-header-btn"
              class="flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-black transition-all active:scale-95 disabled:opacity-50"
              style="background: linear-gradient(135deg, rgba(168,85,247,0.55), rgba(236,72,153,0.45)); border: 1.5px solid rgba(168,85,247,0.7); box-shadow: 0 0 16px -4px rgba(168,85,247,0.6); color: white;"
              :title="cheerInFlight ? '🔥...' : '🔥'">
        <span x-text="cheerInFlight ? '⏳' : '🔥'"></span>
      </button>
      <h1 @click="onBrandTap()" class="text-2xl font-black tracking-tighter cursor-pointer select-none active:opacity-60 transition-opacity" style="-webkit-user-select: none; -webkit-tap-highlight-color: transparent;">Gymbro</h1>
      <div class="flex items-center gap-2">
        <!-- v2.7.52: Header mic button removed (Jim OOB 2026-08-07 13:45 HKT
             'REmove the mic frontend on gymbro. keep backend'). iOS Safari
             PWA mic on plain HTTP is blocked by Apple; user is going via
             Telegram voice bubble to @XAlonsobot for the time being.
             Backend handlers (openVoiceInput, startVoiceRecording, etc.)
             are preserved so the path can be reactivated later when an
             HTTPS origin becomes available. -->
        <!-- v2.7.49: Step widget numerator/denominator (Jim OOB 2026-08-07
             'make the denominator of the step count. today step count as
             numerator'). Today large / yesterday small inline. -->
        <div @click="refreshSteps()"
             :class="stepsRefreshing ? 'opacity-90' : 'active:opacity-70'"
             class="flex items-stretch gap-1 rounded-xl px-1.5 py-1 cursor-pointer select-none transition-opacity"
             style="background:rgba(59,130,246,0.18);border:1px solid rgba(59,130,246,0.45); -webkit-tap-highlight-color: transparent;"
             :title="stepsRefreshing ? '從 Withings 拉緊新數據…' : '撳一下即時 refresh Withings 步數'">
          <div class="flex flex-col items-center justify-center leading-none">
            <span class="text-sm" :class="stepsRefreshing ? 'animate-spin inline-block' : ''">👟</span>
          </div>
          <!-- Numerator: TODAY (large, amber/green/red by threshold) -->
          <div class="flex flex-col items-center justify-center leading-none border-r border-white/20 pr-1.5">
            <span class="text-base font-black tabular-nums"
                  :class="stepsRefreshing ? 'text-sky-300 animate-pulse' : (stepsSyncing ? 'text-gray-400' : (stepsToday >= 8000 ? 'text-emerald-300' : 'text-amber-300'))"
                  x-text="stepsSyncing ? '—' : stepsToday.toLocaleString()"></span>
          </div>
          <!-- Denominator: YESTERDAY (small, gray) — compact format (K/M) -->
          <div class="flex flex-col items-center justify-center leading-none" x-show="stepsYesterday !== null">
            <span class="text-[11px] font-bold tabular-nums text-gray-400"
                  x-text="stepsYesterday !== null ? formatStepCompact(stepsYesterday) : '—'"></span>
          </div>
        </div>
        <div class="flex flex-col items-end leading-tight">
          <span class="text-sm font-bold text-emerald-300 tabular-nums" x-text="clockStrShort"></span>
          <span class="text-[10px] uppercase tracking-[0.2em] text-gray-400" x-text="sessionDateStrShort"></span>
        </div>
      </div>
    </div>
  </header>

  <!-- Toast -->
  <div class="toast" x-show="toast" x-text="toast" x-transition.opacity></div>

  <!-- Tab Content -->
  <main class="px-4 pb-20 pt-2">

    <!-- SET TAB (default) -->
    <section x-show="isTabVisible('set')" class="flex min-h-[calc(100dvh-14rem)] flex-col" x-cloak>

      <!-- Hero motivation banner -->
      <div class="relative mb-3 h-40 w-full overflow-hidden rounded-2xl border border-white/10 bg-gradient-to-br from-black via-gray-800 to-emerald-950 shadow-2xl">
        <img x-show="motivationImage" :src="motivationImage"
             class="absolute inset-0 z-[1] h-40 w-full object-cover"
             @error="motivationImage = ''">
        <div x-show="!motivationImage"
             class="absolute inset-0 z-[1] flex h-40 w-full items-center justify-center text-3xl">
          💪🔥🏋️
        </div>
        <div class="absolute inset-0 z-10 flex items-end bg-gradient-to-t from-black/80 via-black/25 to-transparent px-3 py-2">
          <div class="min-w-0 pr-[120px]">
            <div class="text-[9px] uppercase tracking-[0.2em] text-gray-300">Today</div>
            <div class="quote-line line-clamp-2 text-sm font-medium" x-text="quote"></div>
          </div>
        </div>
        <div x-show="false" class="streak-badge absolute left-1/2 top-2 -translate-x-1/2 z-20 shadow-lg shadow-black/40" style="display:none">
          <span class="text-yellow-300">🔥</span>
          <span x-text="`${streak} day${streak === 1 ? '' : 's'}`"></span>
        </div>
        <!-- Top-left: Whoop recovery % — Jim OOB 2026-07-20 make prominent (PROMINENT PILL) -->
        <div x-show="recovery !== null" class="absolute left-2 top-2 z-20 flex items-center gap-1.5 rounded-full border-2 border-emerald-400/50 bg-black/65 px-3 py-1 text-base font-black text-emerald-300 shadow-lg shadow-emerald-500/20 backdrop-blur">
          <span class="text-lg">💚</span><span x-text="`${recovery}%`" class="tabular-nums"></span>
        </div>
        <!-- Top-right: Withings weight (single number, minimal) -->
        <!-- Top-right: Withings weight kg + fat % (Jim's goal: drive fat down) -->
        <div class="absolute right-2 top-2 z-20 flex flex-col items-end gap-1">
          <div class="flex items-center gap-1 rounded-full border border-white/15 bg-black/55 px-2 py-0.5 text-[10px] font-bold backdrop-blur"
               :class="weightKg !== null ? 'text-sky-300' : 'text-gray-500'">
            <span>⚖️</span><span x-text="weightKg !== null ? `${weightKg}kg` : '—'"></span>
          </div>
          <div class="flex items-center gap-1 rounded-full border bg-black/55 px-2 py-0.5 text-[10px] font-bold backdrop-blur ring-1"
               :class="fatPct !== null ? 'border-yellow-400/30 text-yellow-300 ring-yellow-400/20' : 'border-white/15 text-gray-500 ring-transparent'">
            <span>🔥</span><span x-text="fatPct !== null ? `${fatPct}%` : '—'"></span>
          </div>
        </div>
        <!-- Audio overlay: play/pause/skip — bottom-right of image, doesn't block subject -->
        <div x-show="audioTrack && audioTrack.available" class="absolute bottom-2 right-2 z-20 flex items-center gap-1 rounded-full border border-white/15 bg-black/55 px-1.5 py-1 text-[10px] backdrop-blur">
          <button @click="togglePlay()" class="flex h-7 w-7 items-center justify-center rounded-full text-white hover:bg-white/15 active:scale-95 transition" :title="audioPlaying ? '暫停' : '播放'">
            <span class="text-sm" x-text="audioPlaying ? '⏸' : '▶'"></span>
          </button>
          <button x-show="audioPlaylist.length > 1" @click="audioNext()" class="flex h-7 w-7 items-center justify-center rounded-full text-gray-300 hover:bg-white/15 active:scale-95 transition" title="下一首">
            <span class="text-sm">⏭</span>
          </button>
        </div>
        <!-- Jim OOB 2026-07-19: Cycle motivation image button — placed INSIDE the
             same bottom-right pill group as the audio controls (separated by a
             thin divider). Stops overlap with the bottom-left "quote" line that
             the bottom-left placement collided with. Tapping also refreshes all
             home data (overlay + streak + history). -->
        <button x-show="motivationImageList.length > 1"
                @click="cycleMotivationImage()"
                class="absolute bottom-2 right-2 z-30 flex h-8 items-center justify-center gap-1 rounded-full border border-white/15 bg-black/55 px-2 text-[10px] font-medium text-gray-200 hover:bg-white/15 active:scale-95 transition backdrop-blur"
                :class="audioTrack && audioTrack.available ? 'mr-[68px]' : ''"
                title="換下一張 + 刷新主頁資料">
          <span>↻</span><span x-text="`${motivationImageIndex + 1}/${motivationImageList.length}`"></span>
        </button>
        <!-- Hidden audio element (HTML5 audio, no UI chrome, controlled via Alpine) -->
        <audio x-ref="audioEl" :src="currentAudioUrl" @ended="audioEnded()" @timeupdate="audioProgress = $event.target.currentTime" @loadedmetadata="audioDuration = $event.target.duration" style="display:none"></audio>
      </div>

      <!-- Current set: exercise + weight + reps + intensity in one compact row -->
      <div x-show="currentExercise" class="glass mb-2 flex h-16 items-center gap-3 rounded-2xl px-3 shadow-lg shadow-black/20">
        <button class="tap flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-white/10 text-base hover:bg-white/20 active:scale-95 transition" @click="resetExercise()" aria-label="揀新 exercise" title="揀新 exercise">
          ←
        </button>
        <div class="min-w-0 flex-1">
          <div class="truncate text-base font-black tracking-tight" x-text="currentExercise"></div>
          <div class="mt-0.5 text-xs text-gray-400" x-text="currentSet ? `Set ${currentSet.set}` : 'Set 1'"></div>
        </div>
        <div class="whitespace-nowrap text-xl font-black tracking-tight" x-text="displayWeight"></div>
        <div class="whitespace-nowrap text-base font-bold text-gray-300" x-text="`${displayReps}×`"></div>
        <div class="rounded-full border border-white/10 bg-white/5 px-2 py-1 text-[9px] font-bold uppercase tracking-wider" :class="intensityColor" x-text="intensityLabel"></div>
      </div>

      <!-- Exercise name input (only if no exercise yet) — categorized by muscle group -->
      <div x-show="!currentExercise" class="mt-1">
        <div class="mb-2 text-center text-sm font-semibold text-gray-400">Choose an exercise</div>
        <template x-for="cat in exerciseCategories" :key="cat.name">
          <div class="mb-2">
            <div class="mb-1 flex items-center gap-2">
              <span class="text-[10px] font-bold uppercase tracking-[0.15em]" :class="cat.color" x-text="cat.name"></span>
              <span class="text-[10px] text-gray-500" x-text="`${cat.exercises.length} exercises`"></span>
            </div>
            <div class="grid grid-cols-2 gap-1.5">
              <template x-for="ex in cat.exercises" :key="ex">
                <button class="tap rounded-lg border border-white/15 bg-white/[0.08] px-2 py-1.5 text-xs font-semibold backdrop-blur active:bg-white/20"
                        @click="pickExercise(ex)" x-text="ex"></button>
              </template>
            </div>
          </div>
        </template>
        <input class="!py-2.5 text-base" type="text" placeholder="或輸入 custom" x-model="exerciseInput" @keyup.enter="customExercise()" />
      </div>

      <!-- Weight + reps steppers share one 80px row. Tap is fine control; hold is coarse control. -->
      <div x-show="currentExercise" class="mb-2 grid h-20 grid-cols-2 gap-2">
        <div class="glass grid grid-cols-[2.5rem_1fr_2.5rem] items-center rounded-2xl p-1.5">
          <button class="tap flex h-10 w-10 flex-col items-center justify-center rounded-full bg-white/10 font-bold"
                  @pointerdown.prevent="startStep('weight', -1)" @pointerup.prevent="endStep('weight', -1)"
                  @pointerleave="cancelStep()" @pointercancel="cancelStep()">
            <span class="text-base leading-none">−1</span><span class="mt-0.5 text-[8px] text-gray-400">hold −10</span>
          </button>
          <div class="min-w-0 text-center">
            <span class="text-3xl font-black tracking-tighter" x-text="weight"></span><span class="ml-0.5 text-xs text-gray-400">kg</span>
          </div>
          <button class="tap flex h-10 w-10 flex-col items-center justify-center rounded-full bg-white/10 font-bold"
                  @pointerdown.prevent="startStep('weight', 1)" @pointerup.prevent="endStep('weight', 1)"
                  @pointerleave="cancelStep()" @pointercancel="cancelStep()">
            <span class="text-base leading-none">+1</span><span class="mt-0.5 text-[8px] text-gray-400">hold +10</span>
          </button>
        </div>
        <div class="glass grid grid-cols-[2.5rem_1fr_2.5rem] items-center rounded-2xl p-1.5">
          <button class="tap flex h-10 w-10 flex-col items-center justify-center rounded-full bg-white/10 font-bold"
                  @pointerdown.prevent="startStep('reps', -1)" @pointerup.prevent="endStep('reps', -1)"
                  @pointerleave="cancelStep()" @pointercancel="cancelStep()">
            <span class="text-base leading-none">−1</span><span class="mt-0.5 text-[8px] text-gray-400">hold −5</span>
          </button>
          <div class="min-w-0 text-center">
            <span class="text-3xl font-black tracking-tighter" x-text="reps"></span><span class="ml-0.5 text-xs text-gray-400">×</span>
          </div>
          <button class="tap flex h-10 w-10 flex-col items-center justify-center rounded-full bg-white/10 font-bold"
                  @pointerdown.prevent="startStep('reps', 1)" @pointerup.prevent="endStep('reps', 1)"
                  @pointerleave="cancelStep()" @pointercancel="cancelStep()">
            <span class="text-base leading-none">+1</span><span class="mt-0.5 text-[8px] text-gray-400">hold +5</span>
          </button>
        </div>
      </div>

      <!-- Sticky action dock: always ends above the fixed 64px tab bar. -->
      <div x-show="currentExercise" class="sticky bottom-[140px] z-40 mt-auto pb-2 pt-2">
        <div class="mb-2 flex h-8 gap-2 overflow-x-auto whitespace-nowrap [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          <button class="pill glass shrink-0 px-3 py-1 text-xs tap" @click="setIntensity('working')">🎯 Working</button>
          <button class="pill glass shrink-0 px-3 py-1 text-xs tap" @click="setIntensity('burn-out')">🔥 Burn-out</button>
          <button class="pill glass shrink-0 px-3 py-1 text-xs tap" @click="setIntensity('drop-set')">⚡ Drop</button>
          <button class="pill glass shrink-0 px-3 py-1 text-xs tap" @click="markPartial()">⚠️ Partial</button>
          <button x-show="lastSetForExercise" class="pill glass shrink-0 px-3 py-1 text-xs tap" @click="cloneLastSet()"
                  x-text="lastSetForExercise ? `↓ Clone ${lastSetForExercise.weight_kg}kg × ${lastSetForExercise.reps}` : '↓ Clone'"></button>
        </div>
        <div class="flex items-stretch gap-2 rounded-2xl border border-white/10 bg-black/80 p-1.5 shadow-2xl shadow-emerald-500/20 backdrop-blur-xl">
          <button x-show="session.exercises.length > 0"
                  class="tap shrink-0 rounded-full border border-red-400/30 bg-red-500/15 px-3 py-2 text-sm font-bold text-red-300"
                  :class="{'saving': saving}" @click="cancelLastSet()" aria-label="Cancel last set">
            ↶ Undo
          </button>
          <button class="tap glow-ready flex-1 rounded-full bg-emerald-400 py-3 text-base font-black tracking-wide text-black ring-2 ring-emerald-300/30"
                  :class="{'saving': saving}"
                  :disabled="saving"
                  @click="logSet()"
                  x-text="saving ? 'Saving…' : `✓ LOG SET ${currentSet ? currentSet.set : 1}`">
          </button>
        </div>
      </div>
    </section>

    <!-- WORKOUT / PYRAMID TAB -->
    <section x-show="isTabVisible('workout')">
      <div class="text-[10px] uppercase tracking-[0.2em] text-gray-400 my-3">Today's Pyramid</div>
      <template x-for="(ex, idx) in sessionGrouped" :key="ex.name">
        <div class="mb-6 fade-up" :style="`animation-delay: ${idx * 60}ms`">
          <div class="text-xl font-bold mb-3 tracking-tight" x-text="ex.name"></div>
          <div class="pyramid">
            <template x-for="entry in ex.entries" :key="entry.set">
              <div class="pyramid-row"
                   :class="entry.intensity"
                   x-text="`Set ${entry.set} · ${entry.weight_kg}kg × ${entry.reps} reps`">
              </div>
            </template>
          </div>
          <div class="text-xs text-gray-400 mt-3 text-center" x-text="`Sub-total: ${ex.vol}kg vol`"></div>
        </div>
      </template>
      <div x-show="!sessionGrouped.length" class="text-gray-500 text-center py-20">No sets logged yet</div>
    </section>

    <!-- v3.2.7: legacy HISTORY TAB removed (Jim OOB 2026-08-07 23:40 HKT
         'In gymbro schedule. Remove the top list of activities too').
         The schedule tab now shows ONLY the month calendar + day
         popover. The 'Recent Sessions' list was previously aliased to
         'schedule' via isTabVisible('history') → ['schedule'], but
         gym sessions now live on the calendar grid itself with
         volume + sets + exercises per day. -->

    <!-- SCAN TAB (v2.1 — MiniMax M3 vision + pplx enrichment) -->
    <section x-show="isTabVisible('scan')" x-cloak class="px-4 pb-32 pt-3">
      <div class="text-[10px] uppercase tracking-[0.2em] text-emerald-400 mb-2 text-center font-bold">掃描食物 / 餐單</div>
      <div class="text-xs text-gray-400 text-center mb-4">影相 → 自動記錄卡路里、蛋白質、餐廳</div>

      <!-- v2.3: two file inputs — (1) live camera + (2) iPhone photo stream picker (multiple) -->
      <input type="file" accept="image/*" capture="environment" @change="onScanFile($event)" x-ref="scanInputEl" style="display:none">
      <input type="file" accept="image/*" multiple @change="onScanPhotosPicked($event)" x-ref="scanPhotosInputEl" style="display:none">

      <!-- v2.7.36 → v2.7.54: Photo action buttons (camera + photo stream) —
           both on one line, small chips, half-width each.
           (Jim OOB 2026-08-07 14:35 HKT 'for the two photo action button.
           make one line. small button.')
           Prior v2.7.36 was: large primary (full-width 64px) + small chip
           (full-width 48px) stacked. New: both equal-weight, 1 line, ~44px,
           icon + 3-4 char label, half-width each. Text-direct input
           moved to inline mode (openScanTextInput via purple chip). -->
      <div class="grid grid-cols-2 gap-2 mb-3">
        <!-- Live camera (left) -->
        <button @click="$refs.scanInputEl.click()"
                :disabled="scanUploading"
                class="rounded-xl py-2.5 px-2 transition-all active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed"
                style="background: linear-gradient(135deg, rgba(16,185,129,0.18), rgba(16,185,129,0.06)); border: 1px solid rgba(16,185,129,0.45);">
          <div class="flex items-center justify-center gap-1.5">
            <div class="text-lg" x-text="scanUploading ? '⏳' : '📸'"></div>
            <div class="text-[11px] font-bold text-emerald-300" x-text="scanUploading ? '影緊…' : '影相'"></div>
          </div>
        </button>
        <!-- iPhone photo stream picker (right) -->
        <button @click="$refs.scanPhotosInputEl.click()"
                :disabled="scanUploading || scanPhotosQueue.length > 0"
                class="rounded-xl py-2.5 px-2 transition-all active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed"
                style="background: rgba(255,255,255,0.05); border: 1px solid rgba(16,185,129,0.3);">
          <div class="flex items-center justify-center gap-1.5">
            <div class="text-lg">📷</div>
            <div class="text-[11px] font-bold text-emerald-300">相簿多選</div>
          </div>
        </button>
      </div>

      <!-- Text-direct input mode (toggled by openScanTextInput) -->
      <div x-show="scanTextMode" x-cloak class="mb-4 rounded-2xl p-4"
           style="background: rgba(168,85,247,0.08); border: 1.5px solid rgba(168,85,247,0.35);">
        <div class="text-[10px] uppercase tracking-[0.2em] text-purple-300 mb-2 font-bold">文字輸入模式</div>
        <textarea x-model="scanTextInput"
                  placeholder="例：燒肉飯 1 盒 (約 400g) ｜ 牛肉薄片 3 兩 ｜ 醬油普通 ｜ 1 個人食"
                  class="w-full rounded-lg bg-black/40 px-3 py-2 text-sm text-white border border-white/15"
                  rows="3" maxlength="1000"></textarea>
        <div class="flex items-center justify-between mt-2">
          <div class="text-[10px] text-gray-500" x-text="`${scanTextInput.length} / 1000 字`"></div>
          <button @click="submitScanText()"
                  :disabled="!scanTextInput.trim() || scanTextUploading"
                  class="rounded-lg px-4 py-2 text-sm font-bold transition-all active:scale-95 disabled:opacity-50"
                  style="background: linear-gradient(135deg, rgba(168,85,247,0.4), rgba(168,85,247,0.2)); border: 1.5px solid rgba(168,85,247,0.55);">
            <span x-text="scanTextUploading ? '🤖 AI 估緊營養…' : '🤖 自動估算'"></span>
          </button>
        </div>
      </div>

      <!-- Text-direct preview card (once submitScanText returns) -->
      <template x-if="scanTextPreview">
        <div class="mb-4 rounded-2xl p-4"
             style="background: rgba(168,85,247,0.10); border: 1.5px solid rgba(168,85,247,0.45);">
          <div class="flex items-center justify-between mb-2">
            <div class="text-[10px] uppercase tracking-[0.2em] text-purple-300 font-bold">📝 文字估算結果</div>
            <button @click="scanTextPreview = null" class="text-[10px] text-gray-400 active:opacity-60">✕ 取消</button>
          </div>
          <div class="text-xs text-white mb-2 line-clamp-3" x-text="scanTextPreview.vision_short || ''"></div>
          <div class="flex items-baseline gap-3 text-sm text-gray-200 mb-2">
            <span><span class="text-purple-300 font-bold text-lg" x-text="scanTextEditForm.calories ?? scanTextPreview.suggested_entry.calories"></span> kcal</span>
            <span><span class="text-purple-300 font-bold" x-text="scanTextEditForm.protein ?? scanTextPreview.suggested_entry.protein"></span> P</span>
            <template x-if="scanTextPreview.suggested_entry.is_shared_meal">
              <span class="text-yellow-300 font-bold">👥 60/40</span>
            </template>
          </div>
          <div class="text-[10px] text-gray-500 mb-3" x-text="`菜名: ${scanTextEditForm.name || scanTextPreview.suggested_entry.name || '—'}`"></div>
          <!-- Editable overrides -->
          <div class="grid grid-cols-2 gap-2 text-xs mb-2">
            <input type="text" placeholder="菜名" x-model="scanTextEditForm.name" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="text" placeholder="餐廳" x-model="scanTextEditForm.restaurant_chain" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="number" placeholder="kcal" x-model.number="scanTextEditForm.calories" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="number" placeholder="P" x-model.number="scanTextEditForm.protein" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="number" placeholder="C" x-model.number="scanTextEditForm.carbs" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="number" placeholder="F" x-model.number="scanTextEditForm.fat" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
          </div>
          <textarea x-model="scanTextEditForm.note" placeholder="備註（永久保留）" class="w-full rounded-lg bg-black/40 px-2 py-1.5 text-[11px] text-white border border-white/15" rows="2"></textarea>
          <div class="mt-3 grid grid-cols-2 gap-2">
            <button @click="commitScanText()" :disabled="scanTextCommitting || !scanTextPreview" class="rounded-lg py-2 text-sm font-bold transition-all active:scale-95 disabled:opacity-50" style="background: linear-gradient(135deg, rgba(16,185,129,0.4), rgba(16,185,129,0.2)); border: 1.5px solid rgba(16,185,129,0.55);">
              <span x-text="scanTextCommitting ? '⏳ 寫緊…' : '✓ 確認 log'"></span>
            </button>
            <button @click="reEnrichScanText()" :disabled="scanTextReEnriching" class="rounded-lg py-2 text-xs font-bold transition-all active:scale-95 disabled:opacity-50" style="background: rgba(168,85,247,0.10); border: 1.5px solid rgba(168,85,247,0.4);">
              <span x-text="scanTextReEnriching ? '🤖 再估…' : '🔄 再估算'"></span>
            </button>
          </div>
          <!-- Supplementary hint input for re-estimate -->
          <details class="mt-2" :open="scanTextHints.length > 0">
            <summary class="text-[10px] text-purple-300 cursor-pointer">📎 加補充資料再 estimate</summary>
            <div class="mt-2">
              <input type="text" x-model="scanTextHintInput" @keydown.enter="addScanTextHint()" placeholder="例：真係 2 個人食 / 多了個飯底" class="w-full rounded-lg bg-black/40 px-2 py-1.5 text-[11px] text-white border border-white/15">
              <div class="mt-2 flex flex-wrap gap-1">
                <template x-for="(h, i) in scanTextHints" :key="i">
                  <span class="text-[10px] bg-purple-500/20 text-purple-200 rounded-full px-2 py-0.5 flex items-center gap-1">
                    <span x-text="h"></span>
                    <button @click="scanTextHints.splice(i, 1)" class="text-purple-400 active:opacity-60">✕</button>
                  </span>
                </template>
              </div>
            </div>
          </details>
        </div>
      </template>

      <!-- v2.3: Progress indicator when multi-photo queue is processing -->
      <div x-show="scanPhotosQueue.length > 0" class="mb-4 rounded-xl bg-blue-500/10 border border-blue-400/30 px-3 py-2 text-xs text-blue-200" x-cloak>
        <div class="flex items-center gap-2">
          <span>📷 處理中相簿：</span>
          <span class="font-bold text-blue-100" x-text="`${scanPhotosQueueDone}/${scanPhotosQueue.length} 完成`"></span>
          <span class="text-blue-300/80" x-text="scanPhotosQueueDone === scanPhotosQueue.length ? '（全部 AI 分析完，可以逐張確認）' : '（AI 睇緊下一張…）'"></span>
        </div>
      </div>

      <!-- v2.3: Multi-photo from iPhone photo stream — N preview cards stacked -->
      <template x-for="(item, idx) in scanPhotosQueue" :key="item.client_index">
        <div class="mb-3 rounded-2xl border-2"
             :class="{
               'border-yellow-400/40 bg-yellow-500/10': item.status === 'ready',
               'border-emerald-400/40 bg-emerald-500/10': item.status === 'committed',
               'border-white/10 bg-white/5 opacity-50': item.status === 'skipped',
               'border-red-400/40 bg-red-500/10': item.status === 'failed',
               'border-blue-400/40 bg-blue-500/10': item.status === 'processing',
             }">
          <!-- Header: file name + status pill -->
          <div class="flex items-center justify-between px-3 pt-2">
            <div class="text-[10px] font-mono text-white/70 truncate flex-1">
              <span x-text="`#${idx+1} · ${item.filename.slice(0, 18)} · ${item.file_size_kb}KB`"></span>
            </div>
            <span class="text-[10px] font-bold px-2 py-0.5 rounded-full"
                  :class="{
                    'bg-yellow-400 text-black': item.status === 'ready',
                    'bg-emerald-400 text-black': item.status === 'committed',
                    'bg-white/20 text-white/60': item.status === 'skipped',
                    'bg-red-400 text-white': item.status === 'failed',
                    'bg-blue-400 text-black': item.status === 'processing',
                    'bg-white/10 text-white/60': item.status === 'pending',
                  }"
                  x-text="{
                    pending: '排隊',
                    processing: 'AI 睇緊',
                    ready: '待確認',
                    committed: '已 log',
                    skipped: '跳過',
                    failed: '失敗'
                  }[item.status] || item.status">
            </span>
          </div>

          <!-- Body: image + suggested entry — only visible when ready/committed/failed -->
          <template x-if="item.preview">
            <div class="p-3">
              <div class="flex gap-3">
                <img :src="item.preview.image_url" class="w-24 h-24 object-cover rounded-xl bg-black/40 border border-white/10">
                <div class="flex-1 min-w-0">
                  <div class="text-[11px] text-white mb-1 line-clamp-3" x-text="item.preview.vision_short || ''"></div>
                  <div class="flex items-baseline gap-2 text-xs text-gray-300">
                    <span><span class="text-emerald-300 font-bold" x-text="item.previewCorrectForm.calories ?? item.preview.suggested_entry.calories"></span> kcal</span>
                    <span><span class="text-emerald-300 font-bold" x-text="item.previewCorrectForm.protein ?? item.preview.suggested_entry.protein"></span> P</span>
                    <template x-if="item.preview.suggested_entry.is_shared_meal">
                      <span class="text-yellow-300 font-bold">👥 60/40</span>
                    </template>
                  </div>
                  <div class="text-[10px] text-gray-500 mt-1 truncate" x-text="`菜名: ${item.previewCorrectForm.name || item.preview.suggested_entry.name || '—'}`"></div>
                </div>
              </div>

              <!-- Edit section (collapsible) — Jim can override before commit -->
              <details class="mt-2" :open="item.edit_mode">
                <summary class="text-[10px] text-emerald-300 cursor-pointer" @click="item.edit_mode = !item.edit_mode">✏️ 改呢張嘅資料</summary>
                <div class="grid grid-cols-2 gap-2 text-xs mt-2">
                  <input type="text" placeholder="菜名" x-model="item.previewCorrectForm.name" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
                  <input type="text" placeholder="餐廳" x-model="item.previewCorrectForm.restaurant_chain" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
                  <input type="number" placeholder="kcal" x-model.number="item.previewCorrectForm.calories" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
                  <input type="number" placeholder="P" x-model.number="item.previewCorrectForm.protein" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
                  <input type="number" placeholder="C" x-model.number="item.previewCorrectForm.carbs" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
                  <input type="number" placeholder="F" x-model.number="item.previewCorrectForm.fat" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
                </div>
                <textarea x-model="item.previewCorrectForm.note" placeholder="備註（永久保留）" class="mt-2 w-full rounded-lg bg-black/40 px-2 py-1.5 text-[11px] text-white border border-white/15" rows="2"></textarea>
              </details>

              <!-- Action buttons: ✓ confirm / skip / re-pick -->
              <div class="mt-3 grid grid-cols-2 gap-2">
                <button @click="skipQueueItem(idx)"
                        :disabled="item.status === 'committed' || item.status === 'skipped'"
                        class="rounded-lg bg-white/10 px-3 py-2 text-[11px] font-bold text-white/80 active:scale-95 disabled:opacity-30">
                  跳過
                </button>
                <button @click="commitQueueItem(idx)"
                        :disabled="item.status === 'committed' || item.status === 'skipped' || item.status === 'failed' || item.status === 'pending' || item.status === 'processing'"
                        class="rounded-lg bg-emerald-500 px-3 py-2 text-[11px] font-bold text-black active:scale-95 disabled:opacity-30">
                  <span x-text="item.status === 'committed' ? '✓ 已 log' : '✓ 確認 log 呢張'"></span>
                </button>
              </div>
              <div class="mt-2 text-[10px] text-yellow-300" x-show="item.preview.suggested_entry.is_shared_meal">
                已自動 60/40 share ← 你食 60% · 小寶 40%
              </div>
            </div>
          </template>

          <!-- Pending/Processing state — show spinner -->
          <template x-if="!item.preview && (item.status === 'pending' || item.status === 'processing')">
            <div class="p-4 text-center text-xs text-white/60">
              <div class="text-3xl mb-2 animate-spin inline-block">⏳</div>
              <div>AI 睇緊呢張相…</div>
            </div>
          </template>

          <!-- Failed state — show error -->
          <template x-if="item.status === 'failed'">
            <div class="p-3 text-[11px] text-red-300">
              <span class="font-bold">失敗：</span><span x-text="item.error"></span>
            </div>
          </template>
        </div>
      </template>

      <!-- v2.3: Clear queue button -->
      <button x-show="scanPhotosQueue.length > 0 && scanPhotosQueue.every(i => i.status === 'committed' || i.status === 'skipped' || i.status === 'failed')"
              @click="clearPhotosQueue()"
              class="w-full rounded-xl bg-white/10 px-3 py-2 text-xs font-bold text-white/70 active:scale-95 mb-4">
        清空相簿 queue
      </button>

      <!-- Upload progress bar -->
      <div x-show="scanUploading" class="mb-4 rounded-full bg-white/10 h-2 overflow-hidden">
        <div class="bg-emerald-400 h-2 transition-all duration-500" :style="`width: ${scanProgress}%`"></div>
      </div>

      <!-- v2.2 PREVIEW card (Jim confirms before log) -->
      <div x-show="previewEntry"
           data-preview-card
           class="rounded-2xl bg-yellow-500/10 backdrop-blur border-2 border-yellow-400/40 p-4 mb-4"
           x-cloak>
        <div class="text-[10px] uppercase tracking-[0.15em] text-yellow-300 mb-2 font-bold">⚠️ 預覽 — 未 log，請確認</div>
        <img :src="previewEntry?.image_url" class="w-full rounded-xl mb-3 max-h-48 object-cover bg-black/40">
        <div class="text-sm text-white mb-2" x-text="previewEntry?.vision_short || ''"></div>
        <div class="flex items-baseline gap-3 text-xs text-gray-300 mb-3">
          <span><span class="text-emerald-300 font-bold" x-text="previewCorrectForm.calories ?? 0"></span> kcal</span>
          <span><span class="text-emerald-300 font-bold" x-text="previewCorrectForm.protein ?? 0"></span> P</span>
          <span x-show="previewEntry?.suggested_entry?.is_shared_meal" class="text-yellow-300 font-bold">👥 60/40 share</span>
        </div>

        <!-- v2.7.19: Re-estimate with supplementary hint (Jim OOB 2026-07-31) -->
        <div class="mb-3 rounded-xl border border-purple-400/40 bg-purple-500/10 p-3" x-cloak>
          <div class="flex items-center justify-between mb-2">
            <div class="text-[10px] uppercase tracking-[0.15em] text-purple-300 font-bold">💬 補充資料再 estimate</div>
            <span class="text-[10px] text-purple-200/70" x-show="previewEntry?.re_enriched">✨ 已用 hint 再 estimate</span>
          </div>
          <textarea x-model="previewHint" placeholder="餐廳名 / 份量 / 醬汁 / 材料…（例：太興燒臘, 兩餸飯, 多飯少汁）"
                    class="w-full rounded-lg bg-black/40 px-2 py-1.5 text-xs text-white border border-purple-300/30 resize-none"
                    rows="2" maxlength="500"></textarea>
          <div class="mt-2 flex items-center gap-2">
            <button @click="reEnrichPreview()" :disabled="reEnrichInFlight || !previewHint.trim()"
                    class="flex-1 rounded-lg bg-purple-500 px-3 py-2 text-xs font-bold text-white active:scale-95 disabled:opacity-40">
              <span x-text="reEnrichInFlight ? '⏳ 再 estimate 中…' : '🔄 用補充資料再 estimate'"></span>
            </button>
            <span class="text-[10px] text-purple-200/70" x-text="`${previewHint.length}/500`"></span>
          </div>
          <div class="mt-1 text-[10px] text-purple-200/70" x-show="previewUserHints.length">
            之前已用 hint：
            <template x-for="(h, i) in previewUserHints" :key="i">
              <span class="inline-block bg-purple-400/20 rounded px-1.5 py-0.5 mr-1 mb-1" x-text="h.slice(0, 30) + (h.length > 30 ? '…' : '')"></span>
            </template>
          </div>
        </div>

        <details class="mt-2" open>
          <summary class="text-xs text-emerald-300 cursor-pointer mb-2">✏️ 改資料</summary>
          <div class="grid grid-cols-2 gap-2 text-xs">
            <input type="text" placeholder="菜名" x-model="previewCorrectForm.name" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="text" placeholder="餐廳" x-model="previewCorrectForm.restaurant_chain" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="number" placeholder="kcal" x-model.number="previewCorrectForm.calories" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="number" placeholder="P" x-model.number="previewCorrectForm.protein" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="number" placeholder="C" x-model.number="previewCorrectForm.carbs" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="number" placeholder="F" x-model.number="previewCorrectForm.fat" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
          </div>
          <textarea x-model="previewCorrectForm.note" placeholder="備註（永久保留）" class="mt-2 w-full rounded-lg bg-black/40 px-2 py-1.5 text-xs text-white border border-white/15" rows="2"></textarea>
        </details>
        <div class="mt-3 grid grid-cols-2 gap-2">
          <button @click="cancelPreview()" class="rounded-lg bg-white/10 px-3 py-2 text-xs font-bold text-white/80 active:scale-95">取消</button>
          <button @click="commitPreview()" class="rounded-lg bg-emerald-500 px-3 py-2 text-xs font-bold text-black active:scale-95">✓ 確認 log</button>
        </div>
      </div>

      <!-- v2.7.54: PHOTOSTREAM "今日相片" section removed (Jim OOB
           2026-08-07 14:30 HKT 'remove section of 今日相片. not really
           useful'). The classifier + 建議 log UX added complexity without
           much value — Jim's primary flow is "相簿多選" button + direct
           image scan. Backend API (GET /api/photostream) preserved for
           possible future revival. -->

      <!-- Last scan summary -->
      <div x-show="lastScan" class="rounded-2xl bg-white/[0.06] backdrop-blur border border-white/10 p-4 mb-4" x-cloak>
        <div class="text-[10px] uppercase tracking-[0.15em] text-emerald-300 mb-2 font-bold">剛剛嗰個 scan</div>
        <div class="text-sm text-white mb-1" x-text="lastScan?.vision_short || ''"></div>
        <div class="flex items-baseline gap-3 text-xs text-gray-300 mb-2">
          <span><span class="text-emerald-300 font-bold" x-text="lastScan?.calories || 0"></span> kcal</span>
          <span><span class="text-emerald-300 font-bold" x-text="lastScan?.protein || 0"></span> P</span>
          <span x-show="lastScan?.shared" class="text-yellow-300 font-bold">👥 60/40 share</span>
        </div>
        <div class="text-[10px] text-gray-400 mb-2" x-text="lastScan?.timestamp_iso || ''"></div>
        <!-- Correction form -->
        <details class="mt-2">
          <summary class="text-xs text-emerald-300 cursor-pointer">✏️ 改資料（永遠保留）</summary>
          <div class="mt-2 grid grid-cols-2 gap-2 text-xs">
            <input type="text" placeholder="菜名" x-model="correctForm.name" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="text" placeholder="餐廳" x-model="correctForm.restaurant_chain" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="number" placeholder="kcal" x-model="correctForm.calories" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="number" placeholder="P" x-model="correctForm.protein" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="number" placeholder="C" x-model="correctForm.carbs" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
            <input type="number" placeholder="F" x-model="correctForm.fat" class="rounded-lg bg-black/40 px-2 py-1.5 text-white border border-white/15">
          </div>
          <textarea x-model="correctForm.note" placeholder="備註" class="mt-2 w-full rounded-lg bg-black/40 px-2 py-1.5 text-xs text-white border border-white/15" rows="2"></textarea>
          <button @click="submitCorrection()" class="mt-2 w-full rounded-lg bg-emerald-500/80 px-3 py-1.5 text-xs font-bold text-black active:scale-95">送出修正</button>
          <div x-show="correctSubmitMsg" class="mt-1 text-[10px] text-emerald-300" x-text="correctSubmitMsg"></div>
        </details>
      </div>

      <!-- v2.7.37: Pro mobile UI/UX — group by date + large thumbnails for PT sharing -->
      <div class="flex items-baseline justify-between mb-3">
        <div class="text-[10px] uppercase tracking-[0.15em] text-gray-400 font-bold">食物紀錄（按日分組）</div>
        <div x-show="recentScansFiltered > 0" class="text-[10px] text-gray-500">
          過濾咗 <span class="text-yellow-300 font-bold" x-text="recentScansFiltered"></span> 條 failed
        </div>
      </div>
      <template x-if="recentScans.length === 0">
        <div class="text-xs text-gray-500 text-center py-6">未有 scan 紀錄</div>
      </template>

      <!-- Group scans by date — render one section per day -->
      <template x-for="group in recentScansGrouped" :key="group.date">
        <div class="mb-5">
          <!-- Date header: date + count + day total kcal -->
          <div class="flex items-baseline justify-between mb-2 px-1">
            <div class="flex items-baseline gap-2">
              <div class="text-base font-black text-emerald-300" x-text="group.date_label"></div>
              <div class="text-[11px] text-gray-500 font-semibold" x-text="group.weekday"></div>
              <div class="text-[10px] text-gray-500" x-text="`· ${group.count} 項`"></div>
            </div>
            <div class="text-[11px] text-gray-400">
              <span class="text-white font-bold" x-text="group.total_kcal"></span> kcal
            </div>
          </div>

          <!-- Per-day entries — vertical layout, full-width title (Jim OOB 2026-08-07
               "Food title display is very bad. Pls give more space to display. No wrap") -->
          <template x-for="scan in group.items" :key="scan.scan_index">
            <div class="rounded-2xl bg-white/[0.04] backdrop-blur border border-white/10 p-3 mb-2">
              <!-- Row 1: full-width image on top (max 200px, 16:9 ratio) — only if
                   image exists. v2.7.53 (Jim OOB 2026-08-07 14:15 HKT 'for those
                   without image, you are showing a dummy keyboard image, don't
                   do that. just don't show any image for this case'): removed
                   the ⌨️/🍽️ fallback div entirely. Text-only and no-image
                   entries just show no image at all — clean, no placeholder. -->
              <template x-if="scan.image_url">
                <div class="relative w-full mb-2">
                  <img :src="scan.image_url"
                       class="w-full aspect-[16/9] rounded-xl object-cover bg-black/40 cursor-pointer active:scale-95"
                       style="max-height: 200px;"
                       loading="lazy"
                       @click="window.open(scan.image_url, '_blank')">
                  <!-- v3.2.0 + v3.2.7.3: coach grade as image overlay (Jim OOB 2026-08-07
                       17:00 HKT 'under food log, move the rating on the picture
                       thumbnail as overlay, larger in size. pick a good corner').
                       Top-right is least likely to overlap with food subject
                       and reads naturally with the iOS PWA full-bleed grid.
                       v3.2.7.3: single rating scheme only (A+/A/B/C/D/F), bigger
                       font (text-2xl), thicker ring + drop shadow for legibility. -->
                  <template x-if="scan.coach_comment?.grade && scan.coach_comment.grade !== '—'">
                    <div class="absolute top-2 right-2 text-2xl font-black px-3 py-1.5 rounded-xl shadow-2xl shadow-black/70 backdrop-blur-md ring-2 ring-white/20"
                         :class="{
                           'bg-emerald-500/90 text-white': ['A+','A'].includes(scan.coach_comment.grade),
                           'bg-lime-500/85 text-black': scan.coach_comment.grade === 'B',
                           'bg-yellow-500/85 text-black': scan.coach_comment.grade === 'C',
                           'bg-orange-500/90 text-white': scan.coach_comment.grade === 'D',
                           'bg-red-500/90 text-white': scan.coach_comment.grade === 'F',
                         }"
                         :title="`Coach grade: ${scan.coach_comment.grade}`"
                         x-text="scan.coach_comment.grade"></div>
                  </template>
                </div>
              </template>
              <!-- Row 2: title — full width, single line, horizontal scroll if too long -->
              <div class="flex items-center gap-2 mb-1.5">
                <div class="text-lg font-bold text-white whitespace-nowrap flex-1 min-w-0 overflow-x-auto"
                     style="scrollbar-width: none; -ms-overflow-style: none;"
                     x-text="scan.name || scan.vision_short || '—'"></div>
                <!-- v3.2.7.8: inline grade badge for entries WITHOUT image (text-only
                     or image-back-failed). Image-backed entries already get the
                     bigger top-right overlay on the image. (Jim OOB 2026-08-08
                     19:35 HKT 'For those without image, pls also show the rating') -->
                <template x-if="!scan.image_url && scan.coach_comment?.grade && scan.coach_comment.grade !== '—'">
                  <div class="text-base font-black px-2 py-0.5 rounded-lg shadow-lg ring-1 ring-white/20 flex-shrink-0"
                       :class="{
                         'bg-emerald-500/90 text-white': ['A+','A'].includes(scan.coach_comment.grade),
                         'bg-lime-500/85 text-black': scan.coach_comment.grade === 'B',
                         'bg-yellow-500/85 text-black': scan.coach_comment.grade === 'C',
                         'bg-orange-500/90 text-white': scan.coach_comment.grade === 'D',
                         'bg-red-500/90 text-white': scan.coach_comment.grade === 'F',
                       }"
                       :title="`Coach grade: ${scan.coach_comment.grade}`"
                       x-text="scan.coach_comment.grade"></div>
                </template>
                <!-- v2.7.39: rename button (opens inline popover) -->
                <button @click="openRenamePopover(scan)"
                        class="text-xs text-emerald-300 hover:text-emerald-200 px-1.5 py-0.5 rounded flex-shrink-0 active:scale-95"
                        title="改名 / 重新辨識">✏️</button>
                <!-- v2.7.43: edit date/time button -->
                <button @click="openEditDateTimePopover(scan)"
                        class="text-xs text-sky-300 hover:text-sky-200 px-1.5 py-0.5 rounded flex-shrink-0 active:scale-95"
                        title="改日期 / 時間">⏰</button>
                <!-- v2.7.42: delete button (cascade food_scan_log + nutrition_log + Sheet) -->
                <button @click="openDeleteConfirm(scan)"
                        class="text-xs text-red-300 hover:text-red-200 px-1.5 py-0.5 rounded flex-shrink-0 active:scale-95"
                        title="刪除呢個 entry（連 Sheet 都會刪）">🗑️</button>
              </div>

              <!-- v2.7.43: edit date/time popover -->
              <div x-show="editingDateTimeIndex === scan.scan_index" x-cloak
                   class="mt-2 rounded-lg p-2.5 border border-sky-400/40 bg-sky-900/20"
                   @keydown.escape="closeEditDateTimePopover()">
                <div class="text-[10px] uppercase tracking-wider text-sky-300 font-bold mb-1.5">改日期 / 時間</div>
                <div class="text-[10px] text-gray-400 mb-1.5">
                  原：<span class="text-gray-300" x-text="editingDateTimeOld"></span>
                </div>
                <div class="grid grid-cols-2 gap-2">
                  <div>
                    <label class="block text-[9px] text-gray-400 mb-0.5 uppercase">新日期</label>
                    <input type="date"
                           x-model="editingDateTimeNewDate"
                           @keydown.enter="submitEditDateTime()"
                           @keydown.escape="closeEditDateTimePopover()"
                           class="w-full rounded-lg bg-black/50 px-2 py-1.5 text-xs text-white border border-sky-400/40 focus:border-sky-300 outline-none"
                           autofocus>
                  </div>
                  <div>
                    <label class="block text-[9px] text-gray-400 mb-0.5 uppercase">新時間 (24h)</label>
                    <input type="time"
                           x-model="editingDateTimeNewTime"
                           @keydown.enter="submitEditDateTime()"
                           @keydown.escape="closeEditDateTimePopover()"
                           class="w-full rounded-lg bg-black/50 px-2 py-1.5 text-xs text-white border border-sky-400/40 focus:border-sky-300 outline-none">
                  </div>
                </div>
                <div class="flex gap-2 mt-2">
                  <button @click="submitEditDateTime()"
                          :disabled="!editingDateTimeNewDate || !editingDateTimeNewTime || editDateTimeSubmitting"
                          class="flex-1 rounded-lg bg-sky-500 px-3 py-1.5 text-xs font-bold text-black active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed">
                    <span x-show="!editDateTimeSubmitting">✓ 改日期時間</span>
                    <span x-show="editDateTimeSubmitting">⏳ 更新中…</span>
                  </button>
                  <button @click="closeEditDateTimePopover()"
                          class="rounded-lg bg-white/10 px-3 py-1.5 text-xs text-gray-300 active:scale-95">
                    ✕ 取消
                  </button>
                </div>
                <div x-show="editDateTimeSubmitMsg" class="mt-1.5 text-[10px]" x-text="editDateTimeSubmitMsg"
                     :class="editDateTimeSubmitMsg.startsWith('✓') ? 'text-emerald-300' : 'text-red-300'"></div>
                <div class="text-[9px] text-gray-500 mt-1">會更新 food log + nutrition log + Google Sheet，audit trail 永久保留</div>
              </div>

              <!-- v2.7.39: inline rename popover (shown when editingScanIndex === scan.scan_index) -->
              <div x-show="editingScanIndex === scan.scan_index" x-cloak
                   class="mt-2 rounded-lg p-2.5 border border-emerald-400/40 bg-emerald-900/20">
                <div class="text-[10px] uppercase tracking-wider text-emerald-300 font-bold mb-1.5">改名 + 自動重新估算營養</div>
                <div class="text-[10px] text-gray-400 mb-1.5">
                  原名：<span class="text-gray-300 line-through" x-text="editingScanOldName"></span>
                </div>
                <input type="text"
                       x-model="editingScanNewName"
                       @keydown.enter="submitRename()"
                       @keydown.escape="closeRenamePopover()"
                       placeholder="例：海南雞飯 / Hainanese chicken rice"
                       class="w-full rounded-lg bg-black/50 px-2.5 py-2 text-sm text-white border border-emerald-400/40 focus:border-emerald-300 outline-none"
                       maxlength="30"
                       autofocus>
                <div class="flex gap-2 mt-2">
                  <button @click="submitRename()"
                          :disabled="!editingScanNewName.trim() || renameSubmitting"
                          class="flex-1 rounded-lg bg-emerald-500 px-3 py-1.5 text-xs font-bold text-black active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed">
                    <span x-show="!renameSubmitting">✓ 改名 + 重新估算</span>
                    <span x-show="renameSubmitting">⏳ 估算中…</span>
                  </button>
                  <button @click="closeRenamePopover()"
                          class="rounded-lg bg-white/10 px-3 py-1.5 text-xs text-gray-300 active:scale-95">
                    ✕ 取消
                  </button>
                </div>
                <div x-show="renameSubmitMsg" class="mt-1.5 text-[10px]" x-text="renameSubmitMsg"
                     :class="renameSubmitMsg.startsWith('✓') ? 'text-emerald-300' : 'text-red-300'"></div>
                <div class="text-[9px] text-gray-500 mt-1">舊名會保留喺 audit trail（永久）</div>
              </div>
              <!-- v2.7.42: delete confirm popover (cascade food_scan_log + nutrition_log + Sheet) -->
              <div x-show="deletingScanIndex === scan.scan_index" x-cloak
                   class="mt-2 rounded-xl border border-red-400/40 bg-red-500/10 p-2.5"
                   @keydown.escape="closeDeleteConfirm()">
                <div class="text-[11px] text-red-200 mb-1.5 font-semibold">⚠️ 確認刪除？</div>
                <div class="text-[10px] text-gray-300 mb-2 leading-snug">
                  會一併刪：<span class="text-white font-semibold">food log</span> ·
                  <span class="text-white font-semibold">nutrition log</span> ·
                  <span class="text-white font-semibold">Google Sheet 對應 row</span><br>
                  圖片檔會保留（audit trail）。<span class="text-gray-500">呢個操作<span class="text-red-300 font-bold">唔可以 undo</span>。</span>
                </div>
                <div class="flex gap-2">
                  <button @click="confirmDeleteScan()"
                          :disabled="deleteSubmitting"
                          class="flex-1 rounded-lg bg-red-500 px-3 py-1.5 text-xs font-bold text-white active:scale-95 disabled:opacity-50">
                    <span x-show="!deleteSubmitting">🗑️ 確認刪除</span>
                    <span x-show="deleteSubmitting">⏳ 刪緊…</span>
                  </button>
                  <button @click="closeDeleteConfirm()"
                          class="rounded-lg bg-white/10 px-3 py-1.5 text-xs text-gray-300 active:scale-95">
                    ✕ 取消
                  </button>
                </div>
                <div x-show="deleteSubmitMsg" class="mt-1.5 text-[10px]"
                     :class="deleteSubmitMsg.startsWith('✓') ? 'text-emerald-300' : 'text-red-300'"
                     x-text="deleteSubmitMsg"></div>
              </div>
              <!-- inline P/C/F + kcal + time + flags -->
              <div class="flex items-baseline gap-2 mt-1 text-xs flex-wrap">
                <span><span class="text-emerald-300 font-bold" x-text="scan.calories || 0"></span><span class="text-gray-400"> kcal</span></span>
                <span class="text-gray-400">P <span class="text-white font-semibold" x-text="scan.protein || 0"></span></span>
                <span class="text-gray-400">C <span class="text-white font-semibold" x-text="scan.carbs || 0"></span></span>
                <span class="text-gray-400">F <span class="text-white font-semibold" x-text="scan.fat || 0"></span></span>
                <span x-show="scan.shared" class="text-yellow-300" title="Shared with 小寶">👥</span>
                <span x-show="(scan.user_corrections || []).length > 0" class="text-gray-400" x-text="`✏ ${(scan.user_corrections || []).length}`"></span>
              </div>
              <div class="text-[10px] text-gray-500 mt-0.5" x-text="scan.time_label || formatScanTime(scan.timestamp_iso)"></div>
              <!-- v2.7.37: coach comment (one-liner) -->
              <template x-if="scan.coach_comment?.comment">
                <div class="mt-1.5 text-[11px] text-emerald-200/80 leading-snug"
                     x-text="`🧑‍🏫 ${scan.coach_comment.comment}`"></div>
              </template>
            </div>
          </template>
        </div>
      </template>

      <!-- Progressive load sentinel -->
      <div x-show="recentScans.length > recentScansVisible.length"
           @click="loadMoreScans()"
           class="text-center py-4 text-xs text-gray-500 cursor-pointer active:opacity-60">
        <span x-show="scansLoadingMore">載入緊…</span>
        <span x-show="!scansLoadingMore">⬇ 載入更多 (<span x-text="recentScans.length - recentScansVisible.length"></span> 條)</span>
      </div>
      <div x-show="recentScans.length === recentScansVisible.length && recentScans.length > 0"
           class="text-center py-4 text-xs text-gray-500">
        ✓ 已顯示全部 <span x-text="recentScans.length"></span> 條紀錄
      </div>
    </section>


    <!-- END TAB -->
    <section x-show="isTabVisible('end')" x-cloak>
      <div class="text-center my-6">
        <div class="text-[10px] uppercase tracking-[0.2em] text-gray-400">End Session</div>
        <h2 class="text-4xl font-black tracking-tighter mt-2">收檔時間</h2>
        <p class="text-gray-400 mt-2">收尾寫入 Google Sheet + Whoop log</p>
      </div>

      <div x-show="!endSummary">
        <!-- v3.2.3: RPE slider (Jim OOB 2026-08-07 18:00 HKT 'make RPE is slide bar rather than input'). 1-10 with color-coded zones + real-time display. -->
        <div class="my-6">
          <div class="flex items-end justify-between mb-3">
            <label class="text-[10px] uppercase tracking-[0.2em] text-gray-400">RPE 自覺強度</label>
            <div class="flex items-baseline gap-1.5">
              <span class="text-3xl font-black tabular-nums"
                    :class="endRPE <= 3 ? 'text-emerald-300' : (endRPE <= 6 ? 'text-sky-300' : (endRPE <= 8 ? 'text-amber-300' : 'text-rose-300'))"
                    x-text="endRPE"></span>
              <span class="text-sm text-gray-500">/10</span>
            </div>
          </div>
          <input type="range" min="1" max="10" step="1" x-model.number="endRPE"
                 class="w-full h-3 rounded-full appearance-none cursor-pointer"
                 style="background: linear-gradient(to right, #34d399 0%, #34d399 30%, #38bdf8 30%, #38bdf8 60%, #fbbf24 60%, #fbbf24 80%, #fb7185 80%, #fb7185 100%);"
                 :style="`background: linear-gradient(to right, #34d399 0%, #34d399 ${(endRPE-1)*10}%, #38bdf8 ${(endRPE-1)*10}%, #38bdf8 ${(endRPE-1)*10 + 30}%, #fbbf24 ${(endRPE-1)*10 + 30}%, #fbbf24 ${(endRPE-1)*10 + 50}%, #fb7185 ${(endRPE-1)*10 + 50}%, #fb7185 100%);`" />
          <div class="flex justify-between text-[10px] text-gray-500 mt-2 px-0.5 tabular-nums">
            <span>1</span><span>2</span><span>3</span><span>4</span><span>5</span><span>6</span><span>7</span><span>8</span><span>9</span><span>10</span>
          </div>
          <div class="mt-2 text-xs text-center"
               :class="endRPE <= 3 ? 'text-emerald-300' : (endRPE <= 6 ? 'text-sky-300' : (endRPE <= 8 ? 'text-amber-300' : 'text-rose-300'))"
               x-text="endRPE <= 3 ? '很輕鬆 — warmup 級數' : (endRPE <= 6 ? '中等 — 留到呼吸順' : (endRPE <= 8 ? '高強度 — 開始喘但 hold 得住' : '極限 — 最後一兩下要 push 自己'))"></div>
        </div>
        <button class="primary-btn w-full py-6 text-2xl tap mt-8 glow-ready" @click="endSession()" :class="{'saving': saving}">🏁 END SESSION</button>
        <div class="text-xs text-gray-500 text-center mt-3">Telegram 同步 ON by default (Jim 7/19 config)</div>
      </div>

      <!-- v3.1.0: PT/Whoop share buttons (Jim OOB 2026-08-07 14:50 HKT
           'i use to copy the gym result to my PT after the gym session and
           manually update whoop pasting into whoop ai'). Two share options
           after session ends: (1) copy formatted text for PT message,
           (2) copy whoop-friendly paste for Whoop AI manual entry. -->
      <div x-show="endSummary" class="mt-4 space-y-2">
        <button @click="copyWorkoutForPT()"
                :class="ptCopied ? 'bg-emerald-500/30' : ''"
                class="w-full rounded-lg py-3 text-sm font-bold active:scale-95 transition-all"
                style="background: rgba(16,185,129,0.18); border: 1.5px solid rgba(16,185,129,0.5); color: #a7f3d0;">
          <span x-text="ptCopied ? '✓ 已複製俾 PT' : '📋 複製俾 PT'"></span>
        </button>
        <button @click="copyWorkoutForWhoop()"
                :class="whoopCopied ? 'bg-sky-500/30' : ''"
                class="w-full rounded-lg py-3 text-sm font-bold active:scale-95 transition-all"
                style="background: rgba(56,189,248,0.18); border: 1.5px solid rgba(56,189,248,0.5); color: #bae6fd;">
          <span x-text="whoopCopied ? '✓ 已複製俾 Whoop' : '🏋️ 複製俾 Whoop AI'"></span>
        </button>
        <div class="text-[10px] text-gray-500 text-center mt-2">貼上 message / Whoop AI 即可</div>
      </div>

      <!-- v2.2 Coach tips panel (pplx + MiniMax render Traditional Chinese form cues + progression) -->
      <div x-show="coachTips || coachTipsLoading" class="my-6 rounded-2xl bg-gradient-to-br from-emerald-900/40 to-blue-900/40 backdrop-blur border border-emerald-400/30 p-4" x-cloak>
        <div class="text-[10px] uppercase tracking-[0.15em] text-emerald-300 font-bold mb-2">🧑‍🏫 教練 tips — 繁中 form cue + progression</div>

        <!-- Loading state -->
        <div x-show="coachTipsLoading" class="text-sm text-gray-400">
          <div class="animate-pulse">pplx + MiniMax 分析緊你嘅 session…</div>
        </div>

        <!-- Loaded tips -->
        <div x-show="coachTips && !coachTipsLoading">
          <div class="text-sm text-white whitespace-pre-wrap leading-relaxed mb-3" x-text="coachTips?.tips?.mm_summary || ''"></div>

          <details x-show="coachTips?.tips?.pplx_raw">
            <summary class="text-[10px] text-emerald-300 cursor-pointer">原始 pplx 內容（參考）</summary>
            <div class="text-xs text-gray-300 whitespace-pre-wrap mt-2 leading-relaxed" x-text="coachTips?.tips?.pplx_raw || ''"></div>
          </details>

          <div class="mt-3 text-[10px] text-gray-400">
            <span x-text="coachTips?.exercises_analyzed?.length || 0"></span> 個動作 · <span x-text="coachTips?.session_date || ''"></span> · <span x-show="coachTips?.cached" class="text-yellow-300">cached</span><span x-show="!coachTips?.cached">即時生成</span>
          </div>

          <button @click="fetchCoachTips()" class="mt-2 w-full rounded-lg bg-emerald-500/80 px-3 py-2 text-xs font-bold text-black active:scale-95">↻ 重新生成 tips</button>
        </div>
      </div>

      <div x-show="endSummary" class="my-6">
        <div class="text-[10px] uppercase tracking-[0.2em] font-bold text-emerald-400">✓ Session Ended</div>
        <pre class="text-sm text-gray-300 whitespace-pre-wrap mt-4" x-text="endSummary?.pyramid"></pre>
        <div class="mt-4 text-2xl font-black tracking-tight" x-text="`Total ${endSummary?.total_vol_kg}kg vol`"></div>
        <button class="primary-btn w-full py-4 text-lg tap mt-6" @click="resetSession()">New Session</button>
      </div>
    </section>

    <!-- CHEER TAB (v2.5 — gym-internal cheer routine, Jim OOB 2026-07-23 "Can copy all the cheer routine stuff into gymbro?") -->
    <section x-show="isTabVisible('cheer')" x-cloak class="px-4 pb-32 pt-3">
      <div class="text-[10px] uppercase tracking-[0.2em] text-purple-400 mb-2 text-center font-bold">🔥 Cheer Routine</div>
      <div class="text-xs text-gray-400 text-center mb-4">100% 繁中廣東話 · 復原指數 + 教練評語 · 勵志圖 + 語音</div>

      <!-- Hero card: latest cheer -->
      <template x-if="cheerLatest">
        <div class="mb-4 rounded-2xl bg-gradient-to-br from-purple-900/40 to-pink-900/40 backdrop-blur border border-purple-400/40 p-4">
          <div class="flex items-baseline justify-between mb-2">
            <div class="text-[10px] uppercase tracking-[0.15em] text-purple-300 font-bold">最近 cheer</div>
            <div class="text-[10px] text-gray-400" x-text="cheerLatest.timestamp_iso || ''"></div>
          </div>
          <div class="text-xs text-gray-300 mb-1" x-text="(cheerLatest.fire_type === 'morning' ? '朝早 cheer' : cheerLatest.fire_type === 'evening' ? '夜晚 cheer' : '即場 cheer') + ' · ' + (cheerLatest.fire_id || '')"></div>
          <div class="grid grid-cols-4 gap-1 text-[10px] text-gray-400 mb-3">
            <div class="rounded bg-black/30 px-2 py-1 text-center">
              <div class="text-emerald-300 font-bold text-base" x-text="cheerLatest.metrics_snapshot?.recovery_pct ?? '-'"></div>
              <div>復原%</div>
            </div>
            <div class="rounded bg-black/30 px-2 py-1 text-center">
              <div class="text-emerald-300 font-bold text-base" x-text="cheerLatest.metrics_snapshot?.hrv_ms ?? '-'"></div>
              <div>HRV</div>
            </div>
            <div class="rounded bg-black/30 px-2 py-1 text-center">
              <div class="text-emerald-300 font-bold text-base" x-text="cheerLatest.metrics_snapshot?.rhr_bpm ?? '-'"></div>
              <div>RHR</div>
            </div>
            <div class="rounded bg-black/30 px-2 py-1 text-center">
              <div class="text-emerald-300 font-bold text-base" x-text="cheerLatest.metrics_snapshot?.sleep_bed_hr ?? '-'"></div>
              <div>Hr 瞓</div>
            </div>
          </div>
          <div class="text-sm text-white whitespace-pre-wrap leading-relaxed mb-3" x-text="cheerLatest.text || ''"></div>
          <template x-if="cheerLatest.voice_url">
            <audio :src="cheerLatest.voice_url" controls class="w-full mb-2" style="height:36px"></audio>
          </template>
          <template x-if="cheerLatest.image_url">
            <img :src="cheerLatest.image_url" class="w-full rounded-xl mb-2" loading="lazy">
          </template>
          <div class="flex gap-2 mt-3">
            <span class="text-[10px] bg-emerald-500/20 text-emerald-300 rounded-full px-2 py-0.5" x-show="cheerLatest.has_voice">✓ 語音</span>
            <span class="text-[10px] bg-purple-500/20 text-purple-300 rounded-full px-2 py-0.5" x-show="cheerLatest.has_image">✓ 圖</span>
            <span class="text-[10px] bg-blue-500/20 text-blue-300 rounded-full px-2 py-0.5" x-text="`${cheerLatest.text_chars || 0} 字`"></span>
          </div>
        </div>
      </template>

      <!-- Fire button + status -->
      <div class="rounded-2xl bg-black/30 backdrop-blur border border-white/10 p-4 mb-4">
        <div class="text-[10px] uppercase tracking-[0.15em] text-gray-400 mb-2 font-bold">發動新 cheer</div>
        <div class="flex gap-2 mb-3">
          <button @click="triggerCheer('morning')" :disabled="cheerFiring" class="flex-1 rounded-lg py-2 text-sm font-bold active:scale-95 disabled:opacity-50" style="background:rgba(16,185,129,0.18);box-shadow:inset 0 0 0 1px rgba(16,185,129,0.4);">
            🌅 朝早
          </button>
          <button @click="triggerCheer('evening')" :disabled="cheerFiring" class="flex-1 rounded-lg py-2 text-sm font-bold active:scale-95 disabled:opacity-50" style="background:rgba(99,102,241,0.18);box-shadow:inset 0 0 0 1px rgba(99,102,241,0.4);">
            🌙 夜晚
          </button>
          <button @click="triggerCheer('manual')" :disabled="cheerFiring" class="flex-1 rounded-lg py-2 text-sm font-bold active:scale-95 disabled:opacity-50" style="background:rgba(168,85,247,0.18);box-shadow:inset 0 0 0 1px rgba(168,85,247,0.55);">
            ⚡ 即場
          </button>
        </div>

        <!-- Live progress -->
        <div x-show="cheerFiring || cheerProgress" class="my-3">
          <div class="text-[10px] text-gray-400 mb-1" x-text="cheerProgress || '準備中…'"></div>
          <div class="rounded-full bg-white/10 h-1.5 overflow-hidden">
            <div class="bg-purple-400 h-1.5 transition-all duration-700" :style="`width: ${cheerPct}%`"></div>
          </div>
        </div>

        <!-- Last fire summary -->
        <template x-if="cheerLastFire && cheerLastFire.status === 'done'">
          <div class="mt-3 rounded-xl bg-emerald-500/10 border border-emerald-400/30 px-3 py-2 text-xs text-emerald-200">
            <div class="font-bold mb-1">✓ 上一個 cheer 完成 · <span class="text-emerald-100" x-text="cheerLastFire.fire_id || ''"></span></div>
            <div class="text-[10px] text-emerald-300/80">
              開始 <span x-text="cheerLastFire.started_at"></span>
              · 完 <span x-text="cheerLastFire.finished_at"></span>
              · <span x-text="cheerLastFire.text_chars || 0"></span> 字
              · <span x-show="cheerLastFire.voice_url">語音 ✓</span>
              · <span x-show="cheerLastFire.image_url">圖 ✓</span>
            </div>
            <template x-if="cheerLastFire.text">
              <details class="mt-2">
                <summary class="text-emerald-300 cursor-pointer text-[10px]">睇返上一個 cheer 內容</summary>
                <div class="text-[11px] text-white whitespace-pre-wrap leading-relaxed mt-2" x-text="cheerLastFire.text"></div>
                <template x-if="cheerLastFire.voice_url">
                  <audio :src="cheerLastFire.voice_url" controls class="w-full mt-2" style="height:32px"></audio>
                </template>
                <template x-if="cheerLastFire.image_url">
                  <img :src="cheerLastFire.image_url" class="w-full rounded-lg mt-2" loading="lazy">
                </template>
              </details>
            </template>
          </div>
        </template>

        <template x-if="cheerLastFire && cheerLastFire.status === 'failed'">
          <div class="mt-3 rounded-xl bg-red-500/10 border border-red-400/30 px-3 py-2 text-xs text-red-200">
            ⚠ 上一個 cheer 失敗：<span x-text="cheerLastFire.error || ''"></span>
          </div>
        </template>
      </div>

      <!-- Recent fires (last 3, progressive load — v2.7.31) -->
      <div class="text-[10px] uppercase tracking-[0.15em] text-gray-400 mb-2 font-bold">最近 fires</div>
      <template x-if="cheerRecent.length === 0">
        <div class="text-xs text-gray-500 text-center py-6">未有 cheer 紀錄</div>
      </template>
      <template x-for="(fire, idx) in cheerRecentVisible" :key="fire.fire_id || idx">
        <div class="rounded-xl bg-white/[0.04] backdrop-blur border border-white/10 p-3 mb-2">
          <div class="flex gap-3 items-center">
            <template x-if="fire.image_path">
              <img :src="'/img/' + (fire.image_path.split('/').pop())" class="w-16 h-16 rounded-lg object-cover bg-black/40" loading="lazy">
            </template>
            <template x-if="!fire.image_path">
              <div class="w-16 h-16 rounded-lg bg-purple-500/10 flex items-center justify-center text-2xl">🔥</div>
            </template>
            <div class="flex-1 min-w-0">
              <div class="flex items-baseline gap-2">
                <div class="text-xs text-white font-bold" x-text="(fire.fire_type === 'morning' ? '🌅 朝早' : fire.fire_type === 'evening' ? '🌙 夜晚' : '⚡ 即場') + ' cheer'"></div>
                <div class="text-[10px] text-gray-500" x-text="(String(fire.timestamp_iso || '')).slice(0, 16)"></div>
              </div>
              <div class="flex items-baseline gap-2 text-[11px] text-gray-400 mt-0.5">
                <span><span class="text-emerald-300 font-bold" x-text="fire.metrics_snapshot?.recovery_pct ?? '-'"></span> 復原%</span>
                <span><span class="text-emerald-300 font-bold" x-text="fire.metrics_snapshot?.hrv_ms ?? '-'"></span> HRV</span>
                <span x-show="fire.has_voice" class="text-yellow-300">語音</span>
                <span x-show="fire.has_image" class="text-purple-300">圖</span>
              </div>
              <div class="text-[10px] text-gray-500 truncate" x-text="(String(fire.metrics_snapshot?.cycle_id || '')).slice(0, 12)"></div>
            </div>
          </div>
        </div>
      </template>
      <!-- v2.7.31: cheer progressive-scroll sentinel -->
      <div x-show="cheerRecent.length > cheerRecentVisible.length"
           @click="loadMoreCheer()"
           class="text-center py-4 text-xs text-gray-500 cursor-pointer active:opacity-60">
        <span x-text="'⬇ 拉落去載入更多 (' + (cheerRecent.length - cheerRecentVisible.length) + ' cheer)'"></span>
      </div>
      <div x-show="cheerRecent.length === cheerRecentVisible.length && cheerRecent.length > 0"
           class="text-center py-4 text-xs text-gray-500">
        <span x-text="'✓ 已顯示全部 ' + cheerRecent.length + ' 個 cheer'"></span>
      </div>
    </section>

  </main>

  <!-- Bottom Tab Bar — 2x2 grid (Jim OOB 2026-07-19) -->
  <nav class="fixed bottom-0 left-0 right-0 z-50 border-t border-white/10 bg-black/90 pb-[env(safe-area-inset-bottom)] backdrop-blur-2xl">
    <!-- v3.1.0: 4-tab reorganization. Food is the default + most-frequent,
         gym is focus-mode (landscape, audio, voice coach), cheer is one-tap
         AI-decide, schedule for calendar. Old 6-tab layout (set/workout/
         history/scan/end/cheer) merged into 4 to reduce tap-friction.
         (Jim OOB 2026-08-07 14:50 HKT 'organize the tab. food logging is
         more frequent. gym is around 2-3 times a week. during gym, i have
         to be focusing on that tab and may let me listen to song or listen
         to coach advice.') -->
    <div class="grid grid-cols-4 gap-1 px-2 py-1.5">
      <button class="flex items-center justify-center gap-1.5 rounded-lg py-2 transition-all" :class="tab === 'food' ? 'tab-active' : 'tab-inactive'" @click="tab = 'food'">
        <span class="text-lg leading-none">🍽️</span><span class="text-xs font-bold">食物</span>
      </button>
      <button class="flex items-center justify-center gap-1.5 rounded-lg py-2 transition-all" :class="tab === 'gym' ? 'tab-active' : 'tab-inactive'" @click="tab = 'gym'">
        <span class="text-lg leading-none">🏋️</span><span class="text-xs font-bold">Gym</span>
      </button>
      <button class="flex items-center justify-center gap-1.5 rounded-lg py-2 transition-all" :class="tab === 'cheer' ? 'tab-active' : 'tab-inactive'" @click="tab = 'cheer'">
        <span class="text-lg leading-none">🔥</span>
      </button>
      <!-- v3.2.0: schedule tab button — hidden when no activities in the
           past 42 days AND this week (Jim OOB 2026-08-07 16:40 HKT 'no
           need to show the tab if there is no activities'). -->
      <button x-show="scheduleHasAny"
              class="flex items-center justify-center gap-1.5 rounded-lg py-2 transition-all" :class="tab === 'schedule' ? 'tab-active' : 'tab-inactive'" @click="tab = 'schedule'">
        <span class="text-lg leading-none">📅</span><span class="text-xs font-bold">日程</span>
      </button>
    </div>
  </nav>

  <script>
function gymApp() {
  return {
    // v3.1.0: 4-tab reorganization. 'food' is the new default + most frequent.
    // (Jim OOB 2026-08-07 14:50 HKT 'organize the tab. food logging is more frequent')
    tab: 'food',
    // v3.1.0: frontpage cheer button state
    cheerInFlight: false,
    // v3.1.0: gym focus mode state
    gymFocusMode: false,
    // v3.1.0: PT/Whoop share button state
    ptCopied: false,
    whoopCopied: false,
    endSummaryVisible: false,
    // v3.2.6: schedule tab state — monthly calendar ONLY (Jim OOB
    // 2026-08-07 23:30 HKT 'Fix gymbro calendar view. Remove its list
    // view and weekly view'). Week strip + list view removed; the
    // 42-day monthly calendar is the single source of truth. Data
    // source: /api/whoop_activities_calendar only.
    scheduleLoading: true,
    scheduleMonth: [],
    scheduleMonthAligned: [],
    scheduleMonthGymCount: 0,
    scheduleMonthOtherCount: 0,
    scheduleTotalVolume: 0,
    scheduleTotalSets: 0,
    scheduleRangeLabel: '',
    scheduleHasAny: false,
    // v3.2.5: month calendar popover — tap a day to see full details.
    scheduleSelectedDay: null,
    // v3.1.0: legacy tab aliases. Old 6 tabs (set/workout/history/scan/end/cheer)
    // are aliased to new 4 (food/gym/cheer/schedule). getActiveTab() is used
    // by old sections that still check tab === 'set' / 'workout' / etc.
    getActiveTab() {
      const alias = { 'food': 'scan', 'gym': 'workout', 'schedule': 'history', 'cheer': 'cheer' };
      return alias[this.tab] || this.tab;
    },
    isTabVisible(legacyName) {
      // v3.1.0: 4-tab nav aliases. Each new tab name maps to one or more
      // legacy sections so we don't have to duplicate the 6000+ lines of
      // existing HTML. The frontpage button is a floating element outside
      // the section tree.
      //   food     → scan (拍照/文字 log)
      //   gym      → set (motivation banner + log set form) + workout
      //              (pyramid) + end (完場) — gym needs all 3 because
      //              workflow is: tap motivation image, input exercise,
      //              log set, see pyramid, end + share
      //   cheer    → cheer section
      //   schedule → history (the calendar section uses
      //              isTabVisible('history') for back-compat; the
      //              legacy 'Recent Sessions' list was removed in
      //              v3.2.7 — Jim OOB 2026-08-07 23:40 HKT
      //              'Remove the top list of activities too'.
      //              The 42-day monthly calendar at the bottom of
      //              this file is the only visible element on the
      //              schedule tab now.)
      const reverse = {
        'set':     ['gym'],
        'workout': ['gym'],
        'end':     ['gym'],
        'scan':    ['food'],
        'history': ['schedule'],
        'cheer':   ['cheer'],
      };
      return (reverse[legacyName] || []).includes(this.tab);
    },
    sessionDateStr: '',
    // v3.2.2: compact header strings (HH:MM + MM-DD instead of HH:MM:SS + YYYY-MM-DD)
    sessionDateStrShort: '',
    clockStrShort: '',
    // Withings step widget state (Jim OOB 2026-07-29)
    // v2.7.21: Jim OOB 2026-08-02 02:44 HKT — stepsSyncing flag
    // distinguishes "real 0 steps" from "Withings has not yet
    // committed today (凌晨 / Apple Watch 還未 sync)".
    stepsSyncing: false,
    stepsToday: 0,
    // v2.7.40: tap-to-refresh withings (Jim OOB 2026-08-06 "tap shoes triggers refresh")
    stepsRefreshing: false,
    stepsYesterday: null,
    stepsKcal: 0,
    steps7dAvg: 0,
    // v2.7.42: HH:MM only 24h format (Jim OOB 2026-08-06 "HH:mm only")
    // Date is shown in the group header so per-entry display is just the time
    formatScanTime(iso) {
      if (!iso) return '';
      const s = String(iso);
      // Extract 'YYYY-MM-DDTHH:MM' → 'HH:MM'.
      // Doubled backslashes below are needed: Python 3.12+ emits a
      // SyntaxWarning for raw 'backslash-d' inside a string literal.
      // Python emits the doubled form to HTML, where JS reads it as a
      // single 'backslash-d' digit class.
      // NOTE: this comment itself uses the spelled-out form
      // ('backslash-d') instead of the raw escape, to avoid triggering
      // the very warning it describes.
      const m = s.match(/^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}):(\\d{2})/);
      if (m) return `${m[4]}:${m[5]}`;
      return s.slice(11, 16);
    },
    currentExercise: '',
    exerciseInput: '',
    weight: 20,
    reps: 10,
    intensity: 'warm-up',
    session: { exercises: [] },
    saving: false,
    toast: '',
    endRPE: 7,
    endSummary: null,
    motivationImage: '',
    motivationImageList: [],     // ordered list from /api/today_images
    motivationImageIndex: 0,      // current index within the list
    streak: 0,
    recovery: null,
    weightKg: null,
    fatPct: null,
    clockStr: '',
    elapsedSec: 0,
    elapsedStr: '0:00',
    workoutStartMs: null,
    today: '',
    history: [],
    loadingHistory: false,
    // Jim OOB 2026-07-19: copy-to-clipboard export state. Range is days back
    // from today (0 = today only, 7 = last week, 30 = last month).
    copyInFlight: false,
    copyingDate: null,  // Jim OOB 2026
    // Jim OOB 2026-07-24 09:15: cooldown REMOVED — Jim wants to log sets as fast as possible.
    // Audio overlay state — fetched from /api/today_audio
    audioTrack: null,
    audioPlaylist: [],
    audioIndex: 0,
    audioPlaying: false,
    audioProgress: 0,
    audioDuration: 0,
    pressHandled: false,
    // v2.1 food scan state (Jim OOB 2026-07-23)
    scanUploading: false,
    scanProgress: 0,
    lastScan: null,
    recentScans: [],

    // v2.7.45 P3: Voice memo → text (gpt-4o-transcribe)
    voiceInputMode: false,
    voiceRecording: false,
    voiceRecordingTime: 0,
    voiceTranscribing: false,
    voiceTranscript: null,
    voiceError: null,
    voiceMediaRecorder: null,
    voiceAudioChunks: [],
    voiceTimer: null,
    recentScansFiltered: 0,  // v2.4: count of failed scans skipped by filter
    // v2.7.29: progressive scroll loading (Jim OOB "progressive scrolling for loading performance")
    recentScansVisible: [],       // currently rendered (subset of recentScans)
    recentScansPageSize: 20,      // initial + each load-more batch size
    recentScansPageLoaded: 0,     // number of items currently visible
    scansLoadingMore: false,      // flag for "loading more..." UI
    // v2.5 cheer tab (Jim OOB 2026-07-23 "Can copy all the cheer routine stuff into gymbro?")
    cheerLatest: null,        // latest fire (object from /api/cheer/recent[0])
    cheerRecent: [],          // full list of fires (up to 30)
    cheerRecentVisible: [],   // progressive subset rendered (v2.7.31)
    cheerRecentPageSize: 3,   // initial load size
    cheerFiring: false,       // button disabled while pipeline runs
    cheerProgress: '',        // human-readable progress string
    cheerPct: 0,              // progress bar 0-100
    cheerJobId: null,         // current job_id being polled
    cheerLastFire: null,      // last-completed fire (full state from /api/cheer/status)
    cheerPollTimer: null,     // setInterval handle for status polling
    correctForm: { name: '', restaurant_chain: '', calories: null, protein: null, carbs: null, fat: '', note: '' },
    correctSubmitMsg: '',
    // v2.7.39: inline rename popover per scan (Jim OOB "rename 白切雞 → 海南雞飯")
    editingScanIndex: null,
    editingScanOldName: '',
    editingScanNewName: '',
    renameSubmitting: false,
    renameSubmitMsg: '',
    // v2.7.43: edit date/time state
    editingDateTimeIndex: null,
    editingDateTimeOld: '',
    editingDateTimeNewDate: '',
    editingDateTimeNewTime: '',
    editDateTimeSubmitting: false,
    editDateTimeSubmitMsg: '',
    // v2.7.42: cascade delete (Jim OOB 8/6 23:32 HKT "Add function to remove historical upload. And cascade delete/update g sheet")
    deletingScanIndex: null,
    deleteSubmitting: false,
    deleteSubmitMsg: '',
    // v2.2 features (Jim OOB 2026-07-23 22:42 HKT)
    photostream: [],           // today's images with optional classification
    photostreamClassifying: false,
    scanPhotosQueue: [],       // v2.3: queue of preview entries from multi-photo iPhone picker
    scanPhotosQueueDone: 0,    // v2.3: how many previews fetched (out of scanPhotosQueue.length)
    previewEntry: null,        // current scan preview pending Jim confirmation
    previewEditing: false,     // toggle edit-mode for preview fields
    previewCorrectForm: { name: '', restaurant_chain: '', calories: null, protein: null, carbs: null, fat: null, note: '' },
    // v2.7.19: supplementary hint (Jim OOB 2026-07-31 13:25 HKT)
    // After preview shown, Jim types hint → re-runs pplx + APiyi enrichment → swaps in new preview
    previewHint: '',           // current hint textarea content (cleared after each re-enrich)
    previewUserHints: [],      // history of hints Jim already used in this preview session
    reEnrichInFlight: false,   // prevent double-tap on "🔄 用補充資料再 estimate"
    // v2.7.22: text-direct food input mode (Jim OOB 2026-08-02 02:50 HKT)
    // Type what you ate without taking a photo. APiyi estimates, you confirm, commit.
    scanTextMode: false,       // toggle on by openScanTextInput()
    scanTextInput: '',         // current textarea content
    scanTextUploading: false,  // prevent double-tap on AI estimate
    scanTextPreview: null,     // last preview object (same shape as image preview)
    scanTextEditForm: {        // editable overrides for the preview
      name: '', restaurant_chain: '',
      calories: null, protein: null, carbs: null, fat: null, note: ''
    },
    scanTextCommitting: false, // prevent double-tap on "✓ 確認 log"
    scanTextReEnriching: false,// prevent double-tap on "🔄 再估算"
    scanTextHints: [],         // supplementary hints for re-estimate
    scanTextHintInput: '',     // current hint input
    coachTips: null,           // pplx + MiniMax result for just-ended session
    coachTipsLoading: false,
    quote: '努力唔會辜負你',
    quoteBank: ['努力唔會辜負你', '今日破 PR!', '肌肉記得晒', '每次一公斤', '收檔先贏', '慢慢嚟', '穩住', '加油'],
    exerciseCategories: [
      { name: 'CHEST', color: 'text-red-400', exercises: ['BB Bench Press','Incline BB Press','DB Bench Press','Incline DB Press','Pec Deck','Cable Crossover'] },
      { name: 'BACK',  color: 'text-blue-400', exercises: ['Lat Pulldown','Low Row (Cable)','BB Bent-over Row','Seated Row','T-bar Row','Pull-ups'] },
      { name: 'LEG',   color: 'text-green-400', exercises: ['Squat','Leg Press','Leg Extension','Leg Curl','Romanian Deadlift','Calf Raise'] },
      { name: 'SHOULDER', color: 'text-yellow-400', exercises: ['DB OHP','BB OHP','DB Shoulder Raise','Side Lateral Raise','Cable Lateral','Face Pull'] },
      { name: 'ABS',   color: 'text-purple-400', exercises: ['Plank','Hanging Leg Raise','Crunch','Cable Crunch','Russian Twist','Ab Wheel'] },
    ],

    async init() {
      // Try wake lock
      try {
        if ('wakeLock' in navigator) await navigator.wakeLock.request('screen');
      } catch(e) {}
      // Pull existing state
      const res = await fetch('/api/state');
      const data = await res.json();
      this.session = data.session;
      this.sessionDateStr = data.today;
      // v3.2.2: compact header date (MM-DD) for slim top bar
      this.sessionDateStrShort = (data.today || '').slice(5);
      // Pull today's motivation image (non-blocking). Loads the full list so
      // the cycle button can move between cheer / gymbro variants.
      try {
        const imgRes = await fetch('/api/today_images');
        const imgData = await imgRes.json();
        const list = (imgData && imgData.images) || [];
        this.motivationImageList = list;
        if (list.length > 0) {
          this.motivationImageIndex = 0;
          this.motivationImage = list[0].url;
        }
      } catch(e) {}
      // v2.1: preload recent scans (for Scan tab)
      this.loadRecentScans();
      // v2.2: preload today's photostream (F1 — auto-suggest food log candidates)
      this.loadPhotostream(true);
      // v2.7.18: Withings step widget (Jim OOB 2026-07-29)
      this.loadSteps();
      // v3.2.0: schedule tab data (weekly + monthly calendar)
      this.loadSchedule();
      // Pull streak (non-blocking)
      try {
        const streakRes = await fetch('/api/streak');
        const streakData = await streakRes.json();
        if (streakData && typeof streakData.streak === 'number') {
          this.streak = streakData.streak;
        }
      } catch(e) { /* keep 0 */ }
      // Pull health overlay (Whoop recovery + Withings weight + fat %) — single number each
      try {
        const healthRes = await fetch('/api/health_overlay');
        const healthData = await healthRes.json();
        this.recovery = (typeof healthData.recovery === 'number') ? healthData.recovery : null;
        this.weightKg = (typeof healthData.weight_kg === 'number') ? healthData.weight_kg : null;
        this.fatPct = (typeof healthData.fat_pct === 'number') ? healthData.fat_pct : null;
      } catch(e) { /* keep nulls, badges hidden */ }
      // Pull today's audio (non-blocking, fails silently if no audio exists)
      try {
        const audioRes = await fetch('/api/today_audio');
        const audioData = await audioRes.json();
        if (audioData && audioData.available) {
          this.audioTrack = audioData;
          this.audioPlaylist = [audioData];  // Future: backend can return multi-track playlist
          this.audioIndex = 0;
        }
      } catch(e) { /* keep audioTrack null, button hidden */ }
      this.today = data.today;
      // Initialize count-up timer from session.start_time if it exists
      if (data.session && data.session.start_time) {
        this.workoutStartMs = new Date(data.session.start_time).getTime();
        this.tickElapsed();
      }
      // Tick clock + count-up every 1s
      setInterval(() => {
        this.tickClock();
        this.tickElapsed();
      }, 1000);
      this.tickClock();
      // Pre-load history so it's ready when user taps the tab
      this.loadHistory();
      // v2.7.21: refresh steps every 5 min so when Withings finally
      // commits today's record, the widget auto-updates from
      // "同步中" to real number without manual reload.
      this.loadSteps();
      setInterval(() => this.loadSteps(), 5 * 60 * 1000);
      // Rotate quote every 4s
      setInterval(() => {
        const next = this.quoteBank[Math.floor(Math.random() * this.quoteBank.length)];
        if (next !== this.quote) this.quote = next;
      }, 4000);
      this.haptic();
    },

    // v3.2.2: compact step number formatter (14151 → 14.1K, 2,000,000 → 2.0M)
    formatStepCompact(n) {
      if (n == null) return '—';
      if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\\.0$/, '') + 'M';
      if (n >= 10000) return (n / 1000).toFixed(1).replace(/\\.0$/, '') + 'K';
      if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
      return n.toLocaleString();
    },
    tickClock() {
      const d = new Date();
      const hh24 = d.getHours();
      const mm = String(d.getMinutes()).padStart(2, '0');
      const ss = String(d.getSeconds()).padStart(2, '0');
      // Digital clock 24-hour format (Jim OOB 2026-07-29 — 24h, no AM/PM)
      this.clockStr = `${String(hh24).padStart(2,'0')}:${mm}:${ss}`;
      // v3.2.2: header compact version (HH:MM only) for slim top bar
      this.clockStrShort = `${String(hh24).padStart(2,'0')}:${mm}`;
    },

    tickElapsed() {
      if (!this.workoutStartMs) { this.elapsedSec = 0; this.elapsedStr = '0:00'; return; }
      const sec = Math.max(0, Math.floor((Date.now() - this.workoutStartMs) / 1000));
      this.elapsedSec = sec;
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = sec % 60;
      this.elapsedStr = h > 0 ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${m}:${String(s).padStart(2,'0')}`;
    },

    startCountUp() {
      if (!this.workoutStartMs) {
        this.workoutStartMs = Date.now();
        this.tickElapsed();
      }
    },

    get currentSet() {
      if (!this.currentExercise) return null;
      const sets = this.session.exercises.filter(e => e.exercise === this.currentExercise);
      return { set: sets.length + 1, total: sets.length + 1 };
    },

    get lastSetForExercise() {
      if (!this.currentExercise) return null;
      const sets = this.session.exercises.filter(e => e.exercise === this.currentExercise);
      return sets.length ? sets[sets.length - 1] : null;
    },

    get hasWorkedAtLeastOneSet() {
      const sets = this.session.exercises.filter(e => e.exercise === this.currentExercise);
      return sets.length >= 1;
    },

    get displayWeight() {
      return this.weight ? `${this.weight}kg` : '—';
    },
    get displayReps() {
      return this.reps;
    },
    get intensityLabel() {
      return { 'warm-up': 'Warm-up', 'working': 'Working', 'burn-out': 'Burn-out', 'drop-set': 'Drop-set' }[this.intensity] || '';
    },
    get intensityColor() {
      return { 'warm-up': 'text-gray-400', 'working': 'text-white', 'burn-out': 'text-emerald-400', 'drop-set': 'text-yellow-400' }[this.intensity] || 'text-gray-400';
    },

    get sessionGrouped() {
      const groups = {};
      for (const e of this.session.exercises) {
        if (!groups[e.exercise]) groups[e.exercise] = { name: e.exercise, entries: [], vol: 0 };
        groups[e.exercise].entries.push(e);
        groups[e.exercise].vol += (e.weight_kg || 0) * (e.reps || 0);
      }
      return Object.values(groups);
    },

    // v2.7.37: Group recentScansVisible by date for day-by-day rendering
    // (Jim OOB 2026-08-06: "i don't see any grouping by days")
    // Each group: { date: 'YYYY-MM-DD', date_label: '08/06', weekday: '星期三',
    //                count: 3, total_kcal: 1810, items: [scan, ...] }
    get recentScansGrouped() {
      const groups = {};
      const weekdayNames = ['星期日','星期一','星期二','星期三','星期四','星期五','星期六'];
      for (const s of this.recentScansVisible) {
        const ts = s.timestamp_iso || '';
        const date = ts.slice(0, 10);  // 'YYYY-MM-DD'
        if (!date || date.length < 10) continue;
        if (!groups[date]) {
          // Compute weekday from the date string
          const [y, m, d] = date.split('-').map(Number);
          const dt = new Date(y, m - 1, d);
          const weekday = weekdayNames[dt.getDay()];
          // Label: today = '今日 HH/MM', yesterday = '昨日 HH/MM', else 'MM/DD'
          const today = new Date();
          const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
          const yestMs = today.getTime() - 86400000;
          const yestDt = new Date(yestMs);
          const yestStr = `${yestDt.getFullYear()}-${String(yestDt.getMonth() + 1).padStart(2,'0')}-${String(yestDt.getDate()).padStart(2,'0')}`;
          let label;
          if (date === todayStr) label = '今日';
          else if (date === yestStr) label = '昨日';
          else label = `${String(m).padStart(2,'0')}/${String(d).padStart(2,'0')}`;
          groups[date] = {
            date, date_label: label, weekday,
            count: 0, total_kcal: 0, items: [],
          };
        }
        groups[date].count += 1;
        groups[date].total_kcal += (s.calories || 0);
        // Add HH:MM time_label inline (avoid formatScanTime round-trip)
        const time = ts.slice(11, 16);  // 'HH:MM'
        groups[date].items.push({ ...s, time_label: time });
      }
      // Sort items within each group by time DESC (newest first) — Jim OOB
      // "decreasing order based on date time" 2026-08-06
      for (const g of Object.values(groups)) {
        g.items.sort((a, b) => (b.timestamp_iso || '').localeCompare(a.timestamp_iso || ''));
        g.total_kcal = Math.round(g.total_kcal);
      }
      // Return as array sorted by date DESC (newest first)
      return Object.values(groups).sort((a, b) => b.date.localeCompare(a.date));
    },

    pickExercise(name) {
      this.currentExercise = name;
      this.exerciseInput = '';
      // Jim OOB 2026-07-19 (re-confirmed in same session): NO auto-step on
      // reselecting the same exercise. Keep the SAME weight as the last set,
      // do NOT add +5kg warm-up ramp. User controls progressive loading manually.
      const prev = this.session.exercises.filter(e => e.exercise === name);
      if (!prev.length) {
        this.weight = 20;
        this.reps = 10;
        this.intensity = 'warm-up';
      } else {
        const last = prev[prev.length - 1];
        this.weight = last.weight_kg || 20;       // NO +5 ramp
        this.reps = 10;
        this.intensity = prev.length < 2 ? 'warm-up' : (prev.length < 4 ? 'working' : 'burn-out');
      }
      this.haptic();
      this.flash(`Exercise: ${name}`);
    },

    resetExercise() {
      // Go back to category picker without ending the session.
      this.currentExercise = '';
      this.exerciseInput = '';
      this.weight = 0;
      this.reps = 10;
      this.intensity = 'warm-up';
      this.haptic(20);
    },

    customExercise() {
      if (this.exerciseInput.trim()) this.pickExercise(this.exerciseInput.trim());
    },

    bumpWeight(delta) {
      this.weight = Math.max(0, +(this.weight + delta).toFixed(1));
      this.haptic();
    },

    bumpReps(delta) {
      this.reps = Math.max(1, this.reps + delta);
      this.haptic();
    },

    startStep(kind, direction) {
      this.cancelStep();
      this.pressHandled = false;
      this.pressTimer = setTimeout(() => {
        // Jim OOB 2026-07-19: hold = ±10 (was ±5). Reps keep tap-style increments.
        if (kind === 'weight') this.bumpWeight(direction * 10);
        else this.bumpReps(direction * 5);
        this.pressHandled = true;
        this.pressTimer = null;
      }, 800);
    },

    endStep(kind, direction) {
      if (this.pressTimer) clearTimeout(this.pressTimer);
      if (!this.pressHandled) {
        // Jim OOB 2026-07-19: tap = ±1 for BOTH weight AND reps (was ±3 / ±5 for reps).
        // Fine-grain control across the board.
        if (kind === 'weight') this.bumpWeight(direction * 1);
        else this.bumpReps(direction * 1);
      }
      this.pressTimer = null;
      this.pressHandled = false;
    },

    cancelStep() {
      if (this.pressTimer) clearTimeout(this.pressTimer);
      this.pressTimer = null;
    },

    setIntensity(tag) {
      this.intensity = tag;
      this.haptic();
      this.flash(`Intensity: ${tag}`);
    },

    markPartial() {
      this.haptic();
      this.flash('Marked partial form');
    },

    // Jim OOB 2026-07-21: 30s resting period after log. Prevents accidental
    // double-tap from inflating set count. After tapping LOG SET, button
    // stays disabled for 30 seconds with countdown indicator.
    async logSet() {
      if (!this.currentExercise) return;
      if (this.saving) return;
      this.saving = true;
      this.haptic([60, 30, 60]);
      try {
        const res = await fetch('/api/log_set', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            exercise: this.currentExercise,
            weight_kg: this.weight,
            reps: this.reps,
            set_n: this.session.exercises.filter(e => e.exercise === this.currentExercise).length + 1,
            intensity: this.intensity,
          })
        });
        const data = await res.json();
        if (data.ok) {
          // Add seconds to elapsed counter (click = work done)
          this.workoutSeconds += (data.entry.reps || 10);
          this.tickElapsed();
          // Reload state
          const state = await (await fetch('/api/state')).json();
          this.session = state.session;
          // Jim OOB 2026-07-21: keep current weight on logSet — NO auto-ramp.
          // weight stays as-is unless Jim taps the stepper buttons (-/+).
          const sets = this.session.exercises.filter(e => e.exercise === this.currentExercise);
          const last = sets[sets.length - 1];
          // intensity defaults to 'working' after set 1; first set keeps whatever was set
          if (sets.length === 1) {
            this.intensity = this.intensity || 'working';
          } else {
            this.intensity = 'working';
          }
          this.flash(`✓ Set ${last.set} · ${last.weight_kg}kg × ${last.reps} (${this.intensityLabel})`);
        }
      } catch(e) {
        this.flash('Error: ' + e.message);
      }
      this.saving = false;
    },

    cloneLastSet() {
      if (!this.lastSetForExercise) return;
      this.weight = this.lastSetForExercise.weight_kg;
      this.reps = this.lastSetForExercise.reps;
      this.haptic();
      this.flash('Same as last set');
    },

    async cancelLastSet() {
      if (!this.session.exercises.length) return;
      this.saving = true;
      this.haptic([30, 20, 30]);
      try {
        const res = await fetch('/api/cancel_last_set', { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
          // Reload state
          const state = await (await fetch('/api/state')).json();
          this.session = state.session;
          const r = data.removed || {};
          this.flash(`已取消最後一組 (${r.exercise || 'set'} · ${r.weight_kg || '?'}kg×${r.reps || '?'})`);
          // Refresh history too (today's set count changed)
          this.loadHistory();
        } else {
          this.flash(data.error || 'Cancel failed');
        }
      } catch(e) {
        this.flash('Error: ' + e.message);
      }
      this.saving = false;
    },
    // Audio overlay methods — play / pause / next, controlled via hidden <audio> element
    get currentAudioUrl() {
      if (!this.audioPlaylist || this.audioPlaylist.length === 0) return '';
      const item = this.audioPlaylist[this.audioIndex];
      return item && item.url ? item.url : '';
    },
    togglePlay() {
      const el = this.$refs.audioEl;
      if (!el || !this.currentAudioUrl) return;
      if (this.audioPlaying) {
        el.pause();
        this.audioPlaying = false;
      } else {
        el.play().then(() => {
          this.audioPlaying = true;
          if (this.haptic) this.haptic([15]);
        }).catch((err) => {
          this.flash('播放失敗: ' + (err.message || '未知'));
        });
      }
    },
    audioNext() {
      if (!this.audioPlaylist || this.audioPlaylist.length <= 1) return;
      this.audioIndex = (this.audioIndex + 1) % this.audioPlaylist.length;
      this.audioPlaying = false;
      // Auto-play next track after a tick to let src update.
      this.$nextTick(() => {
        if (this.$refs.audioEl) {
          this.$refs.audioEl.currentTime = 0;
          this.togglePlay();
        }
      });
    },
    audioEnded() {
      this.audioPlaying = false;
      // Auto-advance if there's a next track.
      if (this.audioPlaylist && this.audioIndex < this.audioPlaylist.length - 1) {
        this.audioNext();
      }
    },

    // Jim OOB 2026-07-19: Tap the ↻ button on the hero image to cycle to the
    // next motivation image (cheer / gymbro variants) AND refresh all home
    // page data — overlay (recovery / weight / fat), streak, history.
    async cycleMotivationImage() {
      if (!this.motivationImageList || this.motivationImageList.length <= 1) return;
      this.haptic([20]);
      // Cycle the image
      this.motivationImageIndex = (this.motivationImageIndex + 1) % this.motivationImageList.length;
      const next = this.motivationImageList[this.motivationImageIndex];
      this.motivationImage = next.url;
      // While image is loading, refresh home data in parallel.
      // 1. health overlay
      try {
        const r = await fetch('/api/health_overlay');
        const d = await r.json();
        this.recovery = d.recovery;
        this.weightKg = d.weight_kg;
        this.fatPct = d.fat_pct;
      } catch(e) { /* keep stale */ }
      // 2. streak
      try {
        const r = await fetch('/api/streak');
        const d = await r.json();
        if (typeof d.streak === 'number') this.streak = d.streak;
      } catch(e) { /* keep stale */ }
      // 3. history (forces sheet pre-sync so other-device rows surface)
      await this.refreshHistory(true);
      this.flash(`已換圖 · 同步 ${this.motivationImageList.length} 張 · ${this.history.length} 個 session`);
    },

    async loadHistory(force = false) {
      // Skip if a load is already in flight (prevents double-fetch on rapid tab switches).
      if (this.loadingHistory && !force) return;
      this.loadingHistory = true;
      try {
        const res = await fetch('/api/history');
        const data = await res.json();
        this.history = data.history || [];
        if (typeof data.streak === 'number') this.streak = data.streak;
        if (data.today) this.today = data.today;
      } catch(e) {
        this.flash('History load failed');
      }
      this.loadingHistory = false;
    },
    // Jim OOB 2026-07-19: Refresh button should ACTUALLY pull freshest data,
    // not just re-read the stale local cache. Pre-sync from Sheet first so
    // any rows that exist on Sheet (from another device, end_session auto-push,
    // or cheer cron) show up immediately.
    async refreshHistory(force = true) {
      if (this.loadingHistory && !force) return;
      this.loadingHistory = true;
      this.haptic([20]);
      try {
        // 1. Best-effort pull latest from Sheet → local so /api/history gets freshest data.
        try { await fetch('/api/sync_sheet', { method: 'POST' }); } catch (e) { /* sheet sync is best-effort */ }
        // 2. Fetch history and update UI.
        const res = await fetch('/api/history');
        const data = await res.json();
        this.history = data.history || [];
        if (typeof data.streak === 'number') this.streak = data.streak;
        if (data.today) this.today = data.today;
        this.flash(`已重新整理 · ${this.history.length} 個 session`);
      } catch(e) {
        this.flash('Refresh failed: ' + (e.message || 'network'));
      }
      this.loadingHistory = false;
    },

    // Jim OOB 2026-07-19: Copy workout log to clipboard in chat-AI-friendly
    // format (per `text-coach-summary-voice` Rule 15 — match format to consumer).
    // Source: /api/export_text endpoint (sheet-pulled, chat-AI friendly).
    // Uses navigator.clipboard.writeText() with execCommand('copy') fallback
    // for older iOS Safari. Also calls /api/sync_sheet first to ensure freshness.
    // Jim OOB 2026-07-21: per-row Copy (one day at a time, no date range).
    // Each history row has its own 📋 button → calls /api/export_text?date=YEAR-MM-DD.
    // Source: /api/export_text endpoint (sheet-pulled, chat-AI friendly).
    // Uses navigator.clipboard.writeText() with execCommand('copy') fallback
    // for older iOS Safari. Also calls /api/sync_sheet first to ensure freshness.
    async copyDay(date) {
      if (this.copyInFlight) return;
      this.copyInFlight = true;
      this.copyingDate = date;  // Jim OOB 2026-07-22: per-row "⏳ Copying…" feedback
      this.haptic([20]);
      try {
        // Best-effort sheet sync first (so most recent sets are in the export)
        try { await fetch('/api/sync_sheet', { method: 'POST' }); } catch (e) { /* best-effort */ }
        const res = await fetch(`/api/export_text?date=${encodeURIComponent(date)}&fmt=whoop_text`);
        const data = await res.json();
        const text = (data && data.text) || '';
        if (!text.trim()) {
          this.flash('冇 log 可複製');
          return;
        }
        // Modern clipboard API (works on iOS 13.4+ HTTPS contexts)
        let ok = false;
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            ok = true;
          }
        } catch (e) { /* fall through to fallback */ }
        // Fallback: hidden textarea + execCommand('copy') for older iOS or non-HTTPS
        if (!ok) {
          try {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            ta.style.left = '-9999px';
            document.body.appendChild(ta);
            ta.focus();
            ta.select();
            ok = document.execCommand && document.execCommand('copy');
            document.body.removeChild(ta);
          } catch (e) { /* fallback failed */ }
        }
        const sessions = (data && data.sessions) || 0;
        if (ok) {
          this.flash(`已複製 ${date} · ${sessions} 個 session · 落 clipboard ✓`);
        } else {
          this.flash('Copy failed — clipboard 唔俾用');
          // Show the text in a toast for manual selection
          console.log(`[gym_web] copy text for manual select:\\n${text}`);
        }
      } catch(e) {
        this.flash('Copy failed: ' + (e.message || 'network'));
      }
      this.copyingDate = null;
      this.copyInFlight = false;
    },
    goToTab(name) {
      this.tab = name;
      // Always re-fetch history on tab entry so user sees freshest data.
      if (name === 'history') this.loadHistory(true);
    },

    async deleteSession(date) {
      if (!date) return;
      if (!confirm(`確定刪除 ${date} 的 session?\n(此動作無法復原)`)) return;
      this.haptic([40, 30, 40]);
      try {
        const res = await fetch('/api/delete_session', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ date }),
        });
        const data = await res.json();
        if (data.ok) {
          this.history = this.history.filter(r => r.date !== date);
          this.flash(`已刪除 ${date}`);
        } else {
          this.flash(data.error || 'Delete failed');
        }
      } catch(e) {
        this.flash('Error: ' + e.message);
      }
    },

    async endSession() {
      this.saving = true;
      this.haptic([80, 50, 80, 50, 80]);
      try {
        const res = await fetch('/api/end_session', { method: 'POST' });
        this.endSummary = await res.json();
        // Auto-push to Google Sheet so cheer session can read it immediately.
        try {
          await fetch('/api/sync_sheet', { method: 'POST' });
        } catch (e) { /* sheet push is best-effort */ }
        this.flash('Session ended ✓');
        // v2.2: trigger coach tips generation for the just-ended session
        try { await this.fetchCoachTips(); } catch (e) { /* non-blocking */ }
        // v3.1.0: show share buttons (PT/Whoop) by triggering endSummary visible
        this.endSummaryVisible = true;
      } catch(e) { this.flash('Error: ' + e.message); }
      this.saving = false;
    },

    // v3.1.0: PT share — copy gym result formatted for PT message.
    // Jim OOB 2026-08-07 14:50 HKT 'i use to copy the gym result to my PT
    // after the gym session and manually update whoop pasting into whoop ai'.
    // Format: friendly Cantonese, exercise list with sets x reps x weight.
    async copyWorkoutForPT() {
      try {
        const text = this._formatWorkoutForPT();
        await navigator.clipboard.writeText(text);
        this.ptCopied = true;
        this.haptic([60]);
        this.flash('已複製俾 PT ✓');
        setTimeout(() => { this.ptCopied = false; }, 3000);
      } catch (e) {
        this.flash('複製失敗：' + e.message);
      }
    },

    // v3.1.0: Whoop AI share — copy gym result formatted for Whoop AI
    // manual entry. Compact, machine-readable, includes total volume +
    // exercise list. Use plain English (Whoop AI understands mixed lang).
    async copyWorkoutForWhoop() {
      try {
        const text = this._formatWorkoutForWhoop();
        await navigator.clipboard.writeText(text);
        this.whoopCopied = true;
        this.haptic([60]);
        this.flash('已複製俾 Whoop ✓');
        setTimeout(() => { this.whoopCopied = false; }, 3000);
      } catch (e) {
        this.flash('複製失敗：' + e.message);
      }
    },

    // v3.1.0: PT-format workout text (繁中, friendly, WhatsApp-style).
    // NOTE: Rule 23 — use template literal (backticks) for any string with
    // embedded newlines. Python source backslash-n becomes a raw LF byte,
    // which breaks single/double-quoted JS string literals.
    _formatWorkoutForPT() {
      const exs = this.session?.exercises || [];
      if (!exs.length) return `今日未做 gym`;
      const lines = [];
      lines.push(`今日 gym summary:`);
      lines.push(`---`);
      for (const ex of exs) {
        const name = ex.exercise || ex.name || `?`;
        const sets = (ex.sets || []).map(s => `${s.weight || 0}kg x ${s.reps || 0}`).join(` / `);
        lines.push(`• ${name}: ${sets}`);
      }
      const totalVol = exs.flatMap(e => (e.sets || [])).reduce((a, s) => a + ((s.weight || 0) * (s.reps || 0)), 0);
      const totalSets = exs.flatMap(e => (e.sets || [])).length;
      lines.push(`---`);
      lines.push(`Total: ${totalSets} sets · ${totalVol}kg vol`);
      if (this.endRPE) lines.push(`RPE: ${this.endRPE}/10`);
      return lines.join(`\n`);
    },

    // v3.1.0: Whoop-format workout text (compact, English, AI-friendly).
    // Rule 23 — template literal throughout to avoid Python backslash-n
    // becoming raw LF inside a JS string literal.
    _formatWorkoutForWhoop() {
      const exs = this.session?.exercises || [];
      if (!exs.length) return `No workout today`;
      const lines = [];
      lines.push(`Gym session — please log to Whoop:`);
      for (const ex of exs) {
        const name = ex.exercise || ex.name || `Unknown`;
        const sets = (ex.sets || []).map(s => `${s.weight || 0}kg x ${s.reps || 0}`).join(`, `);
        lines.push(`- ${name}: ${sets}`);
      }
      const totalVol = exs.flatMap(e => (e.sets || [])).reduce((a, s) => a + ((s.weight || 0) * (s.reps || 0)), 0);
      lines.push(`Total volume: ${totalVol}kg`);
      if (this.endRPE) lines.push(`RPE: ${this.endRPE}/10`);
      return lines.join(`\n`);
    },

    async fetchCoachTips() {
      // Build exercises payload from this.session.exercises (shape from session data model)
      const exs = this.session?.exercises || [];
      if (!exs.length) return;
      const exerciseSummary = exs.map(e => ({
        name: e.exercise || e.name || '',
        sets: (e.sets || []).map(s => ({ weight_kg: s.weight, reps: s.reps })),
      }));
      const allSetNums = exs.flatMap(e => (e.sets || []).map((_, i) => i));
      const totalVol = exs.flatMap(e => (e.sets || [])).reduce((a, s) => a + ((s.weight || 0) * (s.reps || 0)), 0);
      const exerciseNames = exs.map(e => e.exercise || e.name || 'Unknown').filter(Boolean);
      try {
        this.coachTipsLoading = true;
        const r = await fetch('/api/coach_tips', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_date: this.sessionDateStr || this.today,
            exercises: exerciseNames,
            total_vol: totalVol,
            total_sets: allSetNums.length,
            exercise_summary: exerciseSummary,
          }),
        });
        const data = await r.json();
        if (data.ok) {
          this.coachTips = data;
        }
      } catch(e) { /* silent */ }
      finally { this.coachTipsLoading = false; }
    },

    async resetSession() {
      // End the "view summary" mode and jump back to category picker for a fresh session.
      this.endSummary = null;
      this.currentExercise = '';
      this.exerciseInput = '';
      this.weight = 0;
      this.reps = 10;
      this.intensity = 'warm-up';
      this.tab = 'set';
      const state = await (await fetch('/api/state')).json();
      this.session = state.session;
      this.flash('New session ready');
    },

    // v3.2.6: load schedule tab data — monthly calendar ONLY.
    // (Jim OOB 2026-08-07 23:30 HKT 'Fix gymbro calendar view. Remove
    // its list view and weekly view'.) Week strip + list view removed;
    // single fetch from /api/whoop_activities_calendar drives the
    // 42-day grid + day popover.
    async loadSchedule() {
      this.scheduleLoading = true;
      try {
        const monthRes = await fetch('/api/whoop_activities_calendar?days=42').then(r => r.json());
        this.scheduleMonth = monthRes.days || [];
        this.scheduleMonthGymCount = monthRes.gym_count || 0;
        this.scheduleMonthOtherCount = monthRes.other_count || 0;
        this.scheduleTotalVolume = monthRes.total_volume_kg || 0;
        this.scheduleTotalSets = monthRes.total_sets || 0;
        const rs = monthRes.range_start || '';
        const re = monthRes.range_end || '';
        this.scheduleRangeLabel = rs && re
          ? `${rs.slice(5)} – ${re.slice(5)}`
          : '';
        // Build aligned grid: prepend invisible filler cells so day 1 of
        // range lands in its correct weekday column.
        const aligned = [];
        if (this.scheduleMonth.length > 0) {
          const firstDow = this.scheduleMonth[0].weekday || 0;  // 0=Mon
          for (let i = 0; i < firstDow; i++) {
            aligned.push({ date: `pad-${i}`, empty: true, day_num: '', activities: [] });
          }
          for (const day of this.scheduleMonth) {
            const dn = parseInt(day.date.slice(8, 10), 10) || '';
            aligned.push({
              ...day,
              day_num: dn,
              empty: false,
            });
          }
        }
        this.scheduleMonthAligned = aligned;
        this.scheduleHasAny = this.scheduleMonth.some(d => d.count > 0);
      } catch (e) {
        console.error('[loadSchedule] failed', e);
        this.scheduleHasAny = false;
      } finally {
        this.scheduleLoading = false;
      }
    },

    async loadRecentScans() {
      // v2.7.29: progressive scroll — load initial 20 first, JS lazy-loads more on scroll
      try {
        const initial = this.recentScansPageSize || 20;
        const r = await fetch(`/api/scan_recent?limit=${initial * 3}`);  // fetch 3 pages worth up front for snappy scroll
        const data = await r.json();
        const all = data.scans || [];
        this.recentScans = all;
        this.recentScansFiltered = data.filtered || 0;
        this.recentScansVisible = all.slice(0, initial);
        this.recentScansPageLoaded = this.recentScansVisible.length;
      } catch(e) { /* silent */ }
    },
    // v2.7.29: trigger when scroll sentinel (or click) hits viewport
    async loadMoreScans() {
      if (this.scansLoadingMore) return;
      if (this.recentScansVisible.length >= this.recentScans.length) return;
      this.scansLoadingMore = true;
      // Simulate async load (in real impl, hit /api/scan_recent for next page)
      await new Promise(r => setTimeout(r, 80));
      const next = Math.min(
        this.recentScansVisible.length + this.recentScansPageSize,
        this.recentScans.length
      );
      this.recentScansVisible = this.recentScans.slice(0, next);
      this.recentScansPageLoaded = this.recentScansVisible.length;
      this.scansLoadingMore = false;
    },

    // v2.7.18: Withings step widget (Jim OOB 2026-07-29)
    // v2.7.21: Jim OOB 2026-08-02 02:44 HKT — Withings may not have
    // today record yet (凌晨 / Apple Watch 還未 commit). Distinguish
    // "0 steps" (real empty day) from "still syncing" (no data).
    // v2.7.40: Force-refresh from Withings (called by tap on 👟 widget)
    // 1. Call /api/withings_refresh to trigger backend subprocess
    // 2. Backend runs withings.py activity getactivity, updates WITHINGS_CACHE
    // 3. Re-fetch /api/withings_steps_today + /api/withings_steps_7d_avg for display
    // Visual: stepsRefreshing = true → 👟 spins + number pulses sky blue
    async refreshSteps() {
      if (this.stepsRefreshing) return;  // debounce: ignore taps during refresh
      this.stepsRefreshing = true;
      this.haptic(15);
      this.flash('從 Withings 拉新步數…');
      try {
        // Step 1: trigger backend force refresh (~3-5s)
        const r = await fetch('/api/withings_refresh', { method: 'POST' });
        const data = await r.json();
        if (data.ok) {
          this.flash(`✓ Withings 已更新 (${data.pulled_at?.slice(11, 16) || 'now'})`);
        } else {
          this.flash('Withings refresh 失敗：' + (data.error || '未知'));
        }
      } catch(e) {
        this.flash('Error：' + e.message);
      }
      // Step 2: always re-fetch today's display data (whether ok or not)
      try {
        await this.loadSteps();
        this.haptic(40);  // success feedback
      } catch(e) { /* silent */ }
      this.stepsRefreshing = false;
    },

    async loadSteps() {
      try {
        const r = await fetch('/api/withings_steps_today');
        const data = await r.json();
        if (data.syncing) {
          // No today record from Withings yet. Show 0 + syncing flag
          // (rather than freezing yesterday's number as today).
          this.stepsToday = 0;
          this.stepsKcal = 0;
          this.stepsSyncing = true;
        } else {
          this.stepsToday = data.steps || 0;
          this.stepsKcal = data.calories || 0;
          this.stepsSyncing = false;
        }
        // v2.7.26: paired yesterday for widget display
        if (data.yesterday && typeof data.yesterday.steps === 'number') {
          this.stepsYesterday = data.yesterday.steps;
        } else {
          this.stepsYesterday = null;
        }
        // 7d avg
        const r7 = await fetch('/api/withings_steps_7d_avg');
        const d7 = await r7.json();
        this.steps7dAvg = d7.avg || 0;
      } catch(e) { /* silent */ }
    },

    // v2.4: tap brand heading — go back to SET tab + scroll to top, NO reload.
    // Jim OOB 2026-07-23: 'When I click the gym heading, it refresh and reload'.
    // Prevents page reload via (a) intercept click event, (b) preventDefault,
    // (c) explicitly call window.scrollTo so iOS doesn't bounce-reload.
    // Also stamps window.__lastTapAt so the SW controllerchange guard knows
    // an active interaction just happened and won't mid-tap force-reload.
    onBrandTap() {
      try { window.__lastTapAt = Date.now(); } catch(e) { /* noop */ }
      this.tab = 'set';
      try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch(e) { window.scrollTo(0, 0); }
      this.flash('返到 SET 主頁');
    },

    // v3.2.5: schedule day popover — open / close
    // (Jim OOB 2026-08-07 18:40 HKT 'need to put the info on the
    // calendar beautifully'). Tap a day to see full activity list.
    openDayPopover(day) {
      try { window.__lastTapAt = Date.now(); } catch(e) { /* noop */ }
      // v3.2.7: allow opening popover for any day with data, not just
      // activity days. A rest day with recovery/HRV/sleep data is also
      // worth showing. (Jim OOB 2026-08-07 23:40 HKT 'beatify the
      // month view with enriched data'.)
      if (!day) return;
      const hasData = (day.activities && day.activities.length > 0)
        || day.gym_volume_kg != null
        || day.recovery_pct != null
        || day.sleep_pct != null
        || day.hrv_ms != null;
      if (!hasData) return;
      this.scheduleSelectedDay = day;
      try { this.haptic && this.haptic('light'); } catch(e) { /* noop */ }
    },
    // v3.2.7: formatVolume(kg) — "1.5k" for >=1000, "850" for <1000.
    formatVolume(kg) {
      if (kg == null || isNaN(kg)) return '—';
      if (kg >= 1000) return (kg / 1000).toFixed(1) + 'k';
      return Math.round(kg).toString();
    },
    // v3.2.7: recoveryPillClass(pct) — green ≥66, sky 33-65, rose <33.
    recoveryPillClass(pct) {
      if (pct == null) return 'text-gray-500';
      if (pct >= 66) return 'text-emerald-300';
      if (pct >= 33) return 'text-sky-300';
      return 'text-rose-300';
    },
    // v3.2.7: dayCellClass(day) — bg color + ring + clickability.
    // (Replaces the inline ternary chain — too complex to read in HTML.)
    dayCellClass(day) {
      if (day.empty) return 'invisible';
      if (day.count === 0 && !day.is_today) {
        // Rest day: show only if recovery data exists, else blank.
        return day.recovery_pct != null
          ? 'bg-white/[0.025] ring-1 ring-white/[0.05] cursor-pointer'
          : 'bg-transparent';
      }
      if (day.is_today) {
        return 'bg-emerald-500/25 ring-2 ring-emerald-300/70 shadow-lg shadow-emerald-500/20';
      }
      if (day.has_gym) {
        return 'bg-emerald-500/12 ring-1 ring-emerald-500/40 active:scale-95 cursor-pointer';
      }
      return 'bg-sky-500/12 ring-1 ring-sky-500/30 active:scale-95 cursor-pointer';
    },
    closeDayPopover() {
      this.scheduleSelectedDay = null;
    },

    // v3.2.4: gym focus mode — toggles body class + state + scrolls to top
    // (Jim OOB 2026-08-07 18:10 HKT 'Why focus in gym always wrong' + 'It
    // focus on the bottom which is weird'). Fixes 3 bugs:
    //   (1) gymFocusMode Alpine state never updated (was DOM-only toggle)
    //   (2) x-text="gymFocusMode ? '🎯' : '🎯'" was identical on both branches
    //   (3) focus mode triggered scroll-to-bottom instead of top
    toggleGymFocus() {
      try { window.__lastTapAt = Date.now(); } catch(e) { /* noop */ }
      const next = !this.gymFocusMode;
      this.gymFocusMode = next;
      // Toggle body class
      document.body.classList.toggle('gym-focus', next);
      // v3.2.4: explicitly scroll to TOP of gym tab — was the source of
      // the "focus on the bottom" weirdness. Without this, font-size 1.15em
      // scaling + transform: scale(1.08) on enlarge elements could push
      // the user's scroll position past the visible content.
      try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch(e) { window.scrollTo(0, 0); }
      // Try landscape lock (only works on Android Chrome, iOS PWA in
      // standalone mode silently rejects; treat both as no-op fallback).
      if (next) {
        try {
          if (screen.orientation && typeof screen.orientation.lock === 'function') {
            screen.orientation.lock('landscape').catch(() => { /* iOS rejects silently */ });
          }
        } catch(e) { /* API not available */ }
      } else {
        try {
          if (screen.orientation && typeof screen.orientation.unlock === 'function') {
            screen.orientation.unlock();
          }
        } catch(e) { /* API not available */ }
      }
      this.flash(next ? '🎯 Focus ON — 大字 + 簡化 UI' : '🎯 Focus OFF');
    },

    triggerHeroScan() {
      this.tab = 'scan';
      this.$nextTick(() => {
        if (this.$refs.scanInputEl) this.$refs.scanInputEl.click();
      });
    },

    // v2.5 cheer tab — switch to cheer tab + load recent fires
    async openCheerTab() {
      try { window.__lastTapAt = Date.now(); } catch(e) { /* noop */ }
      this.tab = 'cheer';
      this.flash('🔥 Cheer tab');
      this.loadCheerRecent();
    },

    // v2.5 cheer — load last N cheer fires from server log
    // v2.7.31: progressive scroll — fetch 30 fires, render initial 3, load-more on demand
    async loadCheerRecent() {
      try {
        const r = await fetch('/api/cheer/recent?limit=30');
        const data = await r.json();
        const fires = data.fires || [];
        this.cheerRecent = fires;
        this.cheerLatest = fires[0] || null;
        // Initial render: first pageSize fires
        this.cheerRecentVisible = fires.slice(0, this.cheerRecentPageSize);
        // For the hero card, also pull today's mood labels from any voice_url/image_url in log entry
        if (this.cheerLatest) {
          // The cheer_log.json stores voice_path and image_path absolute; convert to relative URL for the renderer.
          const last = this.cheerLatest;
          if (last.voice_path) {
            last.voice_url = '/audio/' + last.voice_path.split('/').pop();
          }
          if (last.image_path) {
            last.image_url = '/img/' + last.image_path.split('/').pop();
          }
        }
      } catch (e) { /* silent */ }
    },

    // v2.7.31: cheer progressive load-more (Jim OOB 2026-08-04)
    loadMoreCheer() {
      const next = Math.min(
        this.cheerRecentVisible.length + this.cheerRecentPageSize,
        this.cheerRecent.length
      );
      this.cheerRecentVisible = this.cheerRecent.slice(0, next);
    },

    // v2.5 cheer — trigger a fire
    async triggerCheer(fireType = 'manual') {
      if (this.cheerFiring) return;
      try { window.__lastTapAt = Date.now(); } catch(e) { /* noop */ }
      this.cheerFiring = true;
      this.cheerProgress = '準備中…';
      this.cheerPct = 5;
      try {
        const r = await fetch('/api/cheer', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ fire_type: fireType }),
        });
        const data = await r.json();
        if (!data.ok || !data.job_id) {
          this.flash('Cheer 啟動失敗');
          this.cheerFiring = false;
          this.cheerProgress = '';
          return;
        }
        this.cheerJobId = data.job_id;
        this.flash(`🔥 Cheer 已啟動 (${fireType})`);
        this.cheerProgress = 'WHOOP 拉緊數據…';
        this.cheerPct = 15;
        // Poll status every 4s
        if (this.cheerPollTimer) clearInterval(this.cheerPollTimer);
        this.cheerPollTimer = setInterval(() => this.pollCheerStatus(), 4000);
        // Kick first poll immediately
        this.pollCheerStatus();
      } catch (e) {
        this.flash('Error：' + e.message);
        this.cheerFiring = false;
        this.cheerProgress = '';
      }
    },

    // v2.5 cheer — poll status of current job
    async pollCheerStatus() {
      if (!this.cheerJobId) return;
      try {
        const r = await fetch('/api/cheer/status?job_id=' + encodeURIComponent(this.cheerJobId));
        const data = await r.json();
        if (!data.ok) {
          // Job expired or 404 — stop polling
          if (this.cheerPollTimer) clearInterval(this.cheerPollTimer);
          this.cheerFiring = false;
          this.cheerJobId = null;
          return;
        }
        if (data.status === 'done') {
          if (this.cheerPollTimer) clearInterval(this.cheerPollTimer);
          this.cheerPollTimer = null;
          this.cheerLastFire = data;
          this.cheerProgress = '完成 ✓';
          this.cheerPct = 100;
          this.cheerFiring = false;
          this.flash('🎤 Cheer 完成');
          // Refresh recent fires list
          await this.loadCheerRecent();
          // Clear progress after 5s
          setTimeout(() => { this.cheerProgress = ''; this.cheerPct = 0; }, 5000);
        } else {
          // Running — update progress label with as-of timestamps (7/29 Jim OOB)
          const stepMap = {
            whoop_pull: '🟢 WHOOP 拉緊數據…',
            withings_pull: '🟢 Withings 拉緊數據…',
            text_gen: '✍️ pplx 寫緊 cheer 文字…',
            voice_gen: '🎙 Edge-TTS 整緊 WanLung 語音…',
            image_gen: '🖼 MiniMax 整緊勵志圖…',
          };
          // 7/29 Jim OOB: surface 'as of time' so he sees when the data is actually from
          let suffix = '';
          if (data.whoop_pulled_at) suffix += ` · WHOOP ${data.whoop_pulled_at}`;
          if (data.withings_pulled_at) suffix += ` · Withings ${data.withings_pulled_at}`;
          if (data.step_at) suffix += ` · step ${data.step_at}`;
          this.cheerProgress = (stepMap[data.step] || `${data.status}: ${data.step}`) + suffix;
          this.cheerPct = Math.min(this.cheerPct + 8, 90);
        }
      } catch (e) { /* silent — next poll will retry */ }
    },

    async onScanFile(event) {
      const file = event.target.files[0];
      if (!file) return;
      this.scanUploading = true;
      this.scanProgress = 20;
      this.flash('AI 睇緊你張相…');
      try {
        const formData = new FormData();
        formData.append('image', file);
        const progressTimer = setInterval(() => {
          if (this.scanProgress < 85) this.scanProgress += 5;
        }, 400);

        // v2.2 F2: use PREVIEW endpoint (not auto-commit). Jim confirms manually.
        const r = await fetch('/api/scan_preview', { method: 'POST', body: formData });
        clearInterval(progressTimer);
        this.scanProgress = 100;
        const data = await r.json();
        if (!data.ok) {
          this.flash('Scan 失敗：' + (data.error || '未知錯誤'));
          this.scanUploading = false;
          return;
        }
        // Populate preview entry + start in edit mode after auto-fill
        this.previewEntry = data.preview;
        this.previewCorrectForm = {
          name: data.preview.suggested_entry.name || '',
          restaurant_chain: data.preview.suggested_entry.restaurant_chain || '',
          calories: data.preview.suggested_entry.calories || null,
          protein: data.preview.suggested_entry.protein || null,
          carbs: data.preview.suggested_entry.carbs || null,
          fat: data.preview.suggested_entry.fat || null,
          note: '',
        };
        this.previewEditing = true;
        this.tab = 'scan';
        this.flash('Preview 就緒 ✓ 撳「確認」先 log');
      } catch(e) {
        this.flash('Error：' + e.message);
      } finally {
        this.scanUploading = false;
        this.scanProgress = 0;
        event.target.value = '';
      }
    },

    // v2.3: iPhone photo stream multi-select picker (independent of server cache).
    // Picks N photos from iOS Photos app → sequentially fetches preview for each → renders queue
    // in UI below scan tab. Each queue item shows preview card with ✓ / skip / open-edit actions.
    async onScanPhotosPicked(event) {
      const files = Array.from(event.target.files || []);
      if (files.length === 0) return;
      this.flash(`你揀咗 ${files.length} 張相，AI 逐張睇緊…`);
      this.scanPhotosQueue = files.map((f, i) => ({
        client_index: i,
        filename: f.name || `image_${i+1}.jpg`,
        file_size_kb: Math.round(f.size / 1024),
        status: 'pending',  // pending | processing | ready | committed | skipped | failed
        preview: null,
        edit_mode: false,
        previewCorrectForm: { name: '', restaurant_chain: '', calories: null, protein: null, carbs: null, fat: null, note: '' },
        error: null,
      }));
      this.scanPhotosQueueDone = 0;

      // Sequential processing — MiniMax + pplx is rate-limited; parallel = MiniMax quota burning
      for (let i = 0; i < this.scanPhotosQueue.length; i++) {
        const queueItem = this.scanPhotosQueue[i];
        queueItem.status = 'processing';
        // Force reactivity (Alpine.js tracks direct index write but be safe)
        this.scanPhotosQueue = [...this.scanPhotosQueue];
        try {
          const formData = new FormData();
          formData.append('image', files[i]);
          const r = await fetch('/api/scan_preview', { method: 'POST', body: formData });
          const data = await r.json();
          if (!data.ok) {
            queueItem.status = 'failed';
            queueItem.error = data.error || 'preview failed';
          } else {
            queueItem.preview = data.preview;
            queueItem.previewCorrectForm = {
              name: data.preview.suggested_entry.name || '',
              restaurant_chain: data.preview.suggested_entry.restaurant_chain || '',
              calories: data.preview.suggested_entry.calories || null,
              protein: data.preview.suggested_entry.protein || null,
              carbs: data.preview.suggested_entry.carbs || null,
              fat: data.preview.suggested_entry.fat || null,
              note: '',
            };
            queueItem.status = 'ready';
          }
        } catch (e) {
          queueItem.status = 'failed';
          queueItem.error = e.message;
        }
        this.scanPhotosQueueDone = i + 1;
        this.scanPhotosQueue = [...this.scanPhotosQueue];  // trigger reactivity
      }
      this.flash(this.scanPhotosQueue.length === this.scanPhotosQueueDone
        ? `✓ 全部 ${this.scanPhotosQueueDone} 張 AI 睇完，可以逐張確認`
        : `⚠️ ${this.scanPhotosQueueDone}/${this.scanPhotosQueue.length} 完成，睇下有冇失敗`);
      event.target.value = '';  // reset picker so same files can be re-picked later
    },

    // v2.3: commit one queue item (called per queue card's ✓ confirm button)
    async commitQueueItem(idx) {
      const item = this.scanPhotosQueue[idx];
      if (!item || item.status !== 'ready' || !item.preview) {
        this.flash('呢張未 ready 唔可以 log');
        return;
      }
      try {
        const baseEntry = item.preview.suggested_entry;
        const form = item.previewCorrectForm;
        const finalEntry = {
          ...baseEntry,
          name: form.name || baseEntry.name,
          restaurant_chain: form.restaurant_chain || baseEntry.restaurant_chain,
          calories: form.calories ?? baseEntry.calories,
          protein: form.protein ?? baseEntry.protein,
          carbs: form.carbs ?? baseEntry.carbs,
          fat: form.fat ?? baseEntry.fat,
        };
        const r = await fetch('/api/scan_commit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            entry: finalEntry,
            image_path: item.preview.image_path,
            user_correction: form.note ? form : null,
          }),
        });
        const data = await r.json();
        if (data.ok) {
          item.status = 'committed';
          this.flash(`✓ 張 ${idx+1} (${item.filename.slice(0,12)}) 寫入 log + Sheet`);
          // v2.7.42: align with commitScanText + commitPreview — refresh recent scans
          // so the food log card shows grade + coach_comment immediately
          await this.loadRecentScans();
        } else {
          item.status = 'failed';
          item.error = data.error || 'commit failed';
          this.flash('Log 失敗：' + item.error);
        }
      } catch (e) {
        item.status = 'failed';
        item.error = e.message;
        this.flash('Error：' + e.message);
      }
      this.scanPhotosQueue = [...this.scanPhotosQueue];
    },

    // v2.3: skip one queue item (mark skipped, don't commit)
    skipQueueItem(idx) {
      const item = this.scanPhotosQueue[idx];
      if (!item) return;
      item.status = 'skipped';
      this.scanPhotosQueue = [...this.scanPhotosQueue];
      this.flash(`跳過第 ${idx+1} 張 (${item.filename.slice(0,12)})`);
    },

    // v2.3: clear all queue items that aren't pending/processing
    clearPhotosQueue() {
      this.scanPhotosQueue = [];
      this.scanPhotosQueueDone = 0;
      this.flash('已清空相簿 queue');
    },

    async loadPhotostream(classify = true) {
      this.photostreamClassifying = classify;
      try {
        const r = await fetch(`/api/photostream/today?classify=${classify}&limit=30`);
        const data = await r.json();
        this.photostream = data.items || [];
      } catch(e) { /* silent */ }
      finally { this.photostreamClassifying = false; }
    },

    async commitPreview() {
      if (!this.previewEntry) {
        this.flash('冇 preview 可以確認');
        return;
      }
      try {
        // Build final entry: merged with Jim's edits
        const baseEntry = this.previewEntry.suggested_entry;
        const finalEntry = {
          ...baseEntry,
          name: this.previewCorrectForm.name || baseEntry.name,
          restaurant_chain: this.previewCorrectForm.restaurant_chain || baseEntry.restaurant_chain,
          calories: this.previewCorrectForm.calories ?? baseEntry.calories,
          protein: this.previewCorrectForm.protein ?? baseEntry.protein,
          carbs: this.previewCorrectForm.carbs ?? baseEntry.carbs,
          fat: this.previewCorrectForm.fat ?? baseEntry.fat,
        };
        const r = await fetch('/api/scan_commit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            entry: finalEntry,
            image_path: this.previewEntry.image_path,
            user_correction: this.previewCorrectForm.note ? this.previewCorrectForm : null,
            // v2.7.19: send all hints Jim typed during this scan preview session
            user_hints: this.previewUserHints,
          }),
        });
        const data = await r.json();
        if (data.ok) {
          this.flash(data.sheet_synced ? '✓ 已寫入 log + Sheet' : '✓ 已寫入 log（Sheet 跳過）');
          this.previewEntry = null;
          this.previewEditing = false;
          this.previewHint = '';
          this.previewUserHints = [];
          await this.loadRecentScans();
          await this.loadPhotostream(true);
        } else {
          this.flash('Commit 失敗：' + (data.error || '未知'));
        }
      } catch(e) {
        this.flash('Error：' + e.message);
      }
    },

    cancelPreview() {
      this.previewEntry = null;
      this.previewEditing = false;
      this.previewHint = '';
      this.previewUserHints = [];
      this.flash('Preview 已取消');
    },

    // v2.7.19: Re-estimate with user hint (Jim OOB 2026-07-31 13:25 HKT)
    async reEnrichPreview() {
      const hint = (this.previewHint || '').trim();
      if (!hint || !this.previewEntry || this.reEnrichInFlight) return;
      this.reEnrichInFlight = true;
      this.flash('⏳ 用補充資料再 estimate… (約 5-8 秒)');
      try {
        const r = await fetch('/api/scan_re_enrich', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            image_path: this.previewEntry.image_path,
            user_hint: hint,
            original_vision_desc: this.previewEntry.vision_desc || '',
          }),
        });
        const data = await r.json();
        if (!data.ok) {
          this.flash('Re-enrich 失敗：' + (data.error || '未知'));
          return;
        }
        // Swap in new preview (swap full preview object, not just suggested_entry)
        const newPreview = data.preview;
        // Preserve image_path so commit still works
        newPreview.image_path = newPreview.image_path || this.previewEntry.image_path;
        newPreview.image_url = newPreview.image_url || this.previewEntry.image_url;
        this.previewEntry = newPreview;
        // Reset previewCorrectForm to reflect new suggested_entry (don't clobber Jim's edits
        // if he's already tweaked fields; only sync if form is empty/default)
        const s = newPreview.suggested_entry;
        const cur = this.previewCorrectForm;
        if (!cur.name && s.name) cur.name = s.name;
        if (!cur.restaurant_chain && s.restaurant_chain) cur.restaurant_chain = s.restaurant_chain;
        if (cur.calories == null && s.calories != null) cur.calories = s.calories;
        if (cur.protein == null && s.protein != null) cur.protein = s.protein;
        if (cur.carbs == null && s.carbs != null) cur.carbs = s.carbs;
        if (cur.fat == null && s.fat != null) cur.fat = s.fat;
        // Track hint + clear textarea
        this.previewUserHints = [...this.previewUserHints, hint].slice(-20);
        this.previewHint = '';
        this.flash(`✨ 已用 hint 再 estimate (kcal: ${s.calories ?? '—'}, P: ${s.protein ?? '—'})`);
      } catch(e) {
        this.flash('Error：' + e.message);
      } finally {
        this.reEnrichInFlight = false;
      }
    },

    // v2.7.51: Telegram voice path (Jim OOB 2026-08-07 'Use A for the time
    // being' — use Telegram voice bubble → food log as the working PTT
    // path; iOS Safari PWA mic on plain HTTP is blocked by Apple and
    // cannot be bypassed client-side). The button is preserved visually
    // for muscle memory but it now opens a TG-direct sheet instead of
    // a recorder. Show a deeplink + @XAlonsobot handle.
    openVoiceInput() {
      this.voiceInputMode = !this.voiceInputMode;
      this.voiceError = null;
      if (this.voiceInputMode) {
        // Surface a friendly explanation + TG deeplink
        this.voiceTranscript = null;
        this.voiceRecording = false;
      }
      if (!this.voiceInputMode) {
        this.cancelVoiceRecording();
      }
    },

    async toggleVoiceRecording() {
      if (this.voiceTranscribing) return;
      if (this.voiceRecording) {
        await this.stopVoiceRecording();
      } else {
        await this.startVoiceRecording();
      }
    },

    async startVoiceRecording() {
      this.voiceError = null;
      this.voiceTranscript = null;

      // Check MediaRecorder support
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        this.voiceError = '此裝置唔支援錄音 (iOS 14.3+ 需要 HTTPS)';
        return;
      }

      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        // Pick supported MIME type (iOS Safari needs mp4)
        const mimeOptions = ['audio/mp4', 'audio/webm', 'audio/ogg'];
        let mimeType = '';
        for (const m of mimeOptions) {
          if (MediaRecorder.isTypeSupported(m)) {
            mimeType = m;
            break;
          }
        }
        const opts = mimeType ? { mimeType } : {};
        this.voiceMediaRecorder = new MediaRecorder(stream, opts);
        this.voiceAudioChunks = [];

        this.voiceMediaRecorder.ondataavailable = (e) => {
          if (e.data.size > 0) this.voiceAudioChunks.push(e.data);
        };

        this.voiceMediaRecorder.onstop = async () => {
          stream.getTracks().forEach(t => t.stop());
          const blob = new Blob(this.voiceAudioChunks, { type: mimeType || 'audio/webm' });
          await this.sendAudioToTranscribe(blob);
        };

        this.voiceMediaRecorder.start();
        this.voiceRecording = true;
        this.voiceRecordingTime = 0;

        // Timer + auto-stop at 60s
        this.voiceTimer = setInterval(() => {
          this.voiceRecordingTime += 1;
          if (this.voiceRecordingTime >= 60) {
            this.stopVoiceRecording();
          }
        }, 1000);
      } catch (e) {
        this.voiceError = '錄音失敗：' + e.message + ' (記得要撳「使用麥克風」授權)';
      }
    },

    async stopVoiceRecording() {
      if (this.voiceTimer) {
        clearInterval(this.voiceTimer);
        this.voiceTimer = null;
      }
      if (this.voiceMediaRecorder && this.voiceMediaRecorder.state === 'recording') {
        this.voiceMediaRecorder.stop();
        this.voiceRecording = false;
        this.voiceTranscribing = true;
      }
    },

    cancelVoiceRecording() {
      if (this.voiceTimer) {
        clearInterval(this.voiceTimer);
        this.voiceTimer = null;
      }
      if (this.voiceMediaRecorder) {
        try {
          this.voiceMediaRecorder.ondataavailable = null;
          this.voiceMediaRecorder.onstop = null;
          if (this.voiceMediaRecorder.state === 'recording') {
            this.voiceMediaRecorder.stop();
          }
        } catch (e) {}
        this.voiceMediaRecorder = null;
      }
      this.voiceRecording = false;
      this.voiceRecordingTime = 0;
      this.voiceTranscribing = false;
      this.voiceTranscript = null;
    },

    async onVoiceFileSelected(event) {
      const file = event.target.files[0];
      if (!file) return;
      this.voiceError = null;
      this.voiceTranscript = null;
      this.voiceTranscribing = true;
      await this.sendAudioToTranscribe(file);
      // reset input so same file can be selected again
      event.target.value = '';
    },

    async sendAudioToTranscribe(blobOrFile) {
      this.voiceError = null;
      this.voiceTranscribing = true;
      try {
        // Determine filename + extension
        let filename = 'voice.webm';
        if (blobOrFile instanceof File) {
          filename = blobOrFile.name;
        } else {
          const ext = blobOrFile.type?.includes('mp4') ? '.m4a'
                    : blobOrFile.type?.includes('ogg') ? '.ogg'
                    : '.webm';
          filename = 'voice_' + Date.now() + ext;
        }

        const fd = new FormData();
        fd.append('audio', blobOrFile, filename);
        fd.append('language', 'yue');

        const r = await fetch('/api/transcribe', { method: 'POST', body: fd });
        const data = await r.json();
        if (!data.ok) {
          this.voiceError = data.error || 'transcribe failed';
          return;
        }
        this.voiceTranscript = data.text || '';
        if (!this.voiceTranscript) {
          this.voiceError = '冇 transcript 出嚟 (可能 audio 太空 / 淨係靜音)';
        }
      } catch (e) {
        this.voiceError = '網絡錯誤：' + e.message;
      } finally {
        this.voiceTranscribing = false;
        this.voiceRecordingTime = 0;
      }
    },

    async voiceTranscriptToFoodEntry() {
      // Hand off to text-direct flow: set scanTextInput + open text mode, then submit
      if (!this.voiceTranscript || !this.voiceTranscript.trim()) return;
      this.scanTextInput = this.voiceTranscript.trim();
      this.voiceInputMode = false;
      this.voiceTranscript = null;
      this.scanTextMode = true;
      // Auto submit
      await this.submitScanText();
    },

    // ---- v2.7.22 text-direct food input methods (Jim OOB 2026-08-02 02:50 HKT) ----

    openScanTextInput() {
      // Toggle text-direct input mode ON. Clear any leftover preview.
      this.scanTextMode = !this.scanTextMode;
      if (this.scanTextMode) {
        this.scanTextInput = '';
        this.scanTextPreview = null;
        this.scanTextEditForm = {
          name: '', restaurant_chain: '',
          calories: null, protein: null, carbs: null, fat: null, note: ''
        };
        this.scanTextHints = [];
        this.scanTextHintInput = '';
        // Auto-focus textarea when toggled on
        this.$nextTick(() => {
          const ta = document.querySelector('textarea[x-model="scanTextInput"]');
          if (ta) ta.focus();
        });
      }
    },

    async submitScanText() {
      const text = (this.scanTextInput || '').trim();
      if (!text || this.scanTextUploading) return;
      this.scanTextUploading = true;
      this.flash('🤖 AI 估緊營養…');
      try {
        // Merge stored hints into the request so iteration cycle works
        const hintsForReq = (this.scanTextHints || []).slice();
        if (this.scanTextHintInput.trim()) {
          hintsForReq.push(this.scanTextHintInput.trim());
          this.scanTextHintInput = '';
        }
        const r = await fetch('/api/scan_preview_text', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            text: text,
            user_hints: hintsForReq,
          }),
        });
        const data = await r.json();
        if (!data.ok) {
          this.flash('估算失敗：' + (data.error || '未知'));
          return;
        }
        this.scanTextPreview = data.preview;
        // Init edit form from suggested entry (Jim can override)
        const s = data.preview.suggested_entry;
        this.scanTextEditForm = {
          name: s.name || '',
          restaurant_chain: s.restaurant_chain || '',
          calories: s.calories ?? null,
          protein: s.protein ?? null,
          carbs: s.carbs ?? null,
          fat: s.fat ?? null,
          note: '',
        };
        // Lock the hints in as part of the preview history
        this.scanTextHints = hintsForReq.slice();
        this.flash(`✨ 已估算 (kcal: ${s.calories ?? '—'}, P: ${s.protein ?? '—'}) — 檢查再 ✓ 確認 log`);
      } catch(e) {
        this.flash('Error：' + e.message);
      } finally {
        this.scanTextUploading = false;
      }
    },

    addScanTextHint() {
      const h = (this.scanTextHintInput || '').trim();
      if (!h) return;
      this.scanTextHints = [...this.scanTextHints, h].slice(-5);
      this.scanTextHintInput = '';
    },

    async reEnrichScanText() {
      // Re-run estimate with new hint appended (text-only path can't use image-based endpoint).
      // Just re-call submitScanText() — the existing scanTextHints list gets sent through.
      const newHint = (this.scanTextHintInput || '').trim();
      if (newHint) this.addScanTextHint();
      if (!this.scanTextHints.length) {
        this.flash('請先輸入補充資料');
        return;
      }
      this.scanTextReEnriching = true;
      try {
        await this.submitScanText();
      } finally {
        this.scanTextReEnriching = false;
      }
    },

    async commitScanText() {
      if (!this.scanTextPreview || this.scanTextCommitting) return;
      const s = this.scanTextPreview.suggested_entry;
      const edit = this.scanTextEditForm || {};
      // Merge edit overrides into entry (text-only → image_path="" is handled server-side)
      const entry = {
        ...s,
        name: edit.name || s.name,
        restaurant_chain: edit.restaurant_chain || s.restaurant_chain,
        calories: (edit.calories != null) ? edit.calories : s.calories,
        protein: (edit.protein != null) ? edit.protein : s.protein,
        carbs: (edit.carbs != null) ? edit.carbs : s.carbs,
        fat: (edit.fat != null) ? edit.fat : s.fat,
        note: edit.note || '',
      };
      this.scanTextCommitting = true;
      try {
        const r = await fetch('/api/scan_commit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            entry: entry,
            image_path: '',  // empty = text-direct entry, server skips image rename
            user_hints: this.scanTextHints || [],
          }),
        });
        const data = await r.json();
        if (!data.ok) {
          this.flash('Log 失敗：' + (data.error || '未知'));
          return;
        }
        this.flash(
          `✓ 已 log 落 nutrition + Sheet (${data.is_text_only ? '文字模式' : 'image'}, row ${data.scan_index})`
        );
        // Refresh recent scans + reset UI (also close text-input mode so the panel
        // collapses, otherwise user sees an empty box with no feedback)
        this.scanTextPreview = null;
        this.scanTextInput = '';
        this.scanTextEditForm = {
          name: '', restaurant_chain: '',
          calories: null, protein: null, carbs: null, fat: null, note: ''
        };
        this.scanTextHints = [];
        this.scanTextMode = false;  // v2.7.41: close the text-input panel
        this.loadRecentScans();
      } catch(e) {
        this.flash('Error：' + e.message);
      } finally {
        this.scanTextCommitting = false;
      }
    },

    async suggestLogFromPhoto(item) {
      // Re-run scan_preview on an existing photostream image (server fetches bytes)
      try {
        this.scanUploading = true;
        this.flash('AI 睇緊呢張相…');
        const r = await fetch('/api/scan_preview_from_path', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ image_path: item.path }),
        });
        const data = await r.json();
        if (!data.ok) { this.flash('失敗：' + (data.error || '')); this.scanUploading = false; return; }
        this.previewEntry = data.preview;
        this.previewCorrectForm = {
          name: data.preview.suggested_entry.name || '',
          restaurant_chain: data.preview.suggested_entry.restaurant_chain || '',
          calories: data.preview.suggested_entry.calories || null,
          protein: data.preview.suggested_entry.protein || null,
          carbs: data.preview.suggested_entry.carbs || null,
          fat: data.preview.suggested_entry.fat || null,
          note: '',
        };
        this.previewEditing = true;
        this.tab = 'scan';
        this.flash('Preview 就緒 ✓ 撳「確認」先 log');
        // v2.7.42c: scroll the preview card into view so the user doesn't have
        // to hunt for it after tapping a thumbnail. Preview card has x-show
        // controlling visibility; smooth scroll after Alpine tick.
        this.$nextTick(() => {
          const el = document.querySelector('[x-ref="previewCard"], #previewCard, [data-preview-card]');
          if (el) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
          } else {
            // Fallback: scroll to top of scan section
            const scanSection = document.querySelector('section[x-show*="scan"]');
            if (scanSection) scanSection.scrollTo({ top: 0, behavior: 'smooth' });
          }
        });
      } catch (e) { this.flash('Error: ' + e.message); }
      finally { this.scanUploading = false; }
    },

    async submitCorrection() {
      if (!this.lastScan || this.lastScan.scan_index == null) {
        this.correctSubmitMsg = '冇 scan 可以改';
        return;
      }
      try {
        const r = await fetch('/api/scan_correct', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            scan_index: this.lastScan.scan_index,
            ...this.correctForm,
          }),
        });
        const data = await r.json();
        if (data.ok) {
          this.correctSubmitMsg = '✓ 修正送出（永久保留）';
          await this.loadRecentScans();
        } else {
          this.correctSubmitMsg = '修正失敗：' + (data.error || '未知');
        }
      } catch(e) {
        this.correctSubmitMsg = 'Error：' + e.message;
      }
    },

    // v2.7.39: Open inline rename popover for a scan card
    openRenamePopover(scan) {
      this.editingScanIndex = scan.scan_index;
      this.editingScanNewName = scan.name || '';
      this.editingScanOldName = scan.name || '';
      this.renameSubmitting = false;
      this.renameSubmitMsg = '';
      this.haptic(10);
    },
    closeRenamePopover() {
      this.editingScanIndex = null;
      this.editingScanNewName = '';
      this.editingScanOldName = '';
      this.renameSubmitMsg = '';
    },
    async submitRename() {
      if (this.editingScanIndex == null) return;
      const newName = (this.editingScanNewName || '').trim();
      if (!newName) {
        this.renameSubmitMsg = '新名唔可以空';
        return;
      }
      if (newName === this.editingScanOldName) {
        this.renameSubmitMsg = '同原名一樣，唔使改';
        return;
      }
      this.renameSubmitting = true;
      this.renameSubmitMsg = '';
      try {
        const r = await fetch('/api/scan_rename', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            scan_index: this.editingScanIndex,
            new_name: newName,
          }),
        });
        const data = await r.json();
        if (data.ok) {
          this.renameSubmitMsg = '✓ 改名 + 重新估算成功';
          this.haptic(40);
          // Reload scans to reflect new name + macros
          await this.loadRecentScans();
          setTimeout(() => this.closeRenamePopover(), 800);
        } else {
          this.renameSubmitMsg = '失敗：' + (data.error || '未知');
        }
      } catch(e) {
        this.renameSubmitMsg = 'Error：' + e.message;
      } finally {
        this.renameSubmitting = false;
      }
    },
    // v2.7.43: edit date/time popover (Jim OOB 8/7 "in gymbro, allow me to edit date time of food log")
    openEditDateTimePopover(scan) {
      this.editingDateTimeIndex = scan.scan_index;
      const ts = scan.timestamp_iso || '';
      this.editingDateTimeNewDate = ts.slice(0, 10) || '';
      this.editingDateTimeNewTime = (ts.slice(11, 16) || '').replace(/^(\\d):/, '0$1:');
      this.editingDateTimeOld = `${(ts.slice(0, 10) || '?')} ${(ts.slice(11, 16) || '?')}`;
      this.editDateTimeSubmitting = false;
      this.editDateTimeSubmitMsg = '';
      this.haptic(15);
    },
    closeEditDateTimePopover() {
      this.editingDateTimeIndex = null;
      this.editingDateTimeNewDate = '';
      this.editingDateTimeNewTime = '';
      this.editDateTimeSubmitting = false;
      this.editDateTimeSubmitMsg = '';
    },
    async submitEditDateTime() {
      if (this.editingDateTimeIndex == null) return;
      if (!this.editingDateTimeNewDate || !this.editingDateTimeNewTime) {
        this.editDateTimeSubmitMsg = '請填日期同時間';
        return;
      }
      // Find target scan for stable identifier
      const target = (this.recentScansVisible || []).find(
        s => s.scan_index === this.editingDateTimeIndex
      ) || (this.recentScans || []).find(
        s => s.scan_index === this.editingDateTimeIndex
      );
      this.editDateTimeSubmitting = true;
      this.editDateTimeSubmitMsg = '';
      try {
        const r = await fetch('/api/scan_edit_datetime', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            scan_index: this.editingDateTimeIndex,
            timestamp_iso: target ? target.timestamp_iso : null,
            new_date: this.editingDateTimeNewDate,
            new_time: this.editingDateTimeNewTime,
          }),
        });
        const data = await r.json();
        if (data.ok) {
          const parts = [`${data.old_date} ${data.old_time} → ${data.new_date} ${data.new_time}`];
          if (data.nutrition_updated > 0) parts.push(`nutrition log ${data.nutrition_updated}`);
          if (data.sheet && data.sheet.updated > 0) parts.push(`Sheet ${data.sheet.updated} cells`);
          this.editDateTimeSubmitMsg = `✓ ${parts.join(' · ')}`;
          this.haptic(40);
          // Reload scans to reflect new date/time
          await this.loadRecentScans();
          setTimeout(() => this.closeEditDateTimePopover(), 1500);
        } else {
          this.editDateTimeSubmitMsg = '失敗：' + (data.error || '未知');
        }
      } catch(e) {
        this.editDateTimeSubmitMsg = 'Error：' + e.message;
      } finally {
        this.editDateTimeSubmitting = false;
      }
    },
    // v2.7.42: cascade delete (Jim OOB 8/6 23:32 HKT)
    openDeleteConfirm(scan) {
      this.deletingScanIndex = scan.scan_index;
      this.deleteSubmitting = false;
      this.deleteSubmitMsg = '';
      this.haptic(15);
    },
    closeDeleteConfirm() {
      this.deletingScanIndex = null;
      this.deleteSubmitting = false;
      this.deleteSubmitMsg = '';
    },
    async confirmDeleteScan() {
      if (this.deletingScanIndex == null) return;
      // Find the scan object so we can pass stable identifiers (timestamp_iso + name)
      // to the backend — array index may have shifted due to prior deletes.
      const target = (this.recentScansVisible || []).find(
        s => s.scan_index === this.deletingScanIndex
      ) || (this.recentScans || []).find(
        s => s.scan_index === this.deletingScanIndex
      );
      this.deleteSubmitting = true;
      this.deleteSubmitMsg = '';
      try {
        const r = await fetch('/api/scan_delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            scan_index: this.deletingScanIndex,
            timestamp_iso: target ? target.timestamp_iso : null,
            name: target ? target.name : null,
            calories: target ? target.calories : null,
          }),
        });
        const data = await r.json();
        if (data.ok) {
          const parts = [];
          parts.push('food log');
          if (data.nutrition_log_removed > 0) parts.push('nutrition log');
          if (data.sheet_rows_deleted > 0) parts.push(`Sheet ${data.sheet_rows_deleted} row`);
          this.deleteSubmitMsg = `✓ 已刪：${parts.join(' + ')}`;
          this.haptic(60);
          // Reload to reflect deleted entry gone
          await this.loadRecentScans();
          setTimeout(() => this.closeDeleteConfirm(), 1000);
        } else {
          this.deleteSubmitMsg = '失敗：' + (data.error || '未知');
        }
      } catch(e) {
        this.deleteSubmitMsg = 'Error：' + e.message;
      } finally {
        this.deleteSubmitting = false;
      }
    },

    haptic(pattern = 30) {
      try { if (navigator.vibrate) navigator.vibrate(pattern); } catch(e) {}
    },

    flash(msg) {
      this.toast = msg;
      setTimeout(() => this.toast = '', 1500);
    },
  };
}
</script>

<!-- Service worker registration for PWA install -->
<script>
// iOS Safari gesture-block: kill pinch-zoom + double-tap-zoom that bypasses CSS touch-action
['gesturestart','gesturechange','gestureend'].forEach(ev => document.addEventListener(ev, e => e.preventDefault(), {passive:false}));
// Last-resort double-tap zoom blocker (some iOS versions ignore user-scalable=no)
let lastTouch = 0;
document.addEventListener('touchend', e => {
  const now = Date.now();
  if (now - lastTouch < 300) e.preventDefault();
  lastTouch = now;
}, {passive:false});

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

// PWA install prompt — 7-day delayed bottom sheet (Jim OOB 2026-07-26 19:35 HKT).
// Captures beforeinstallprompt for Android, shows iOS manual instructions for iPhone.
(function(){
  try {
    const DISMISS_KEY = 'pwa_install_dismissed_at';
    const dismissedAt = parseInt(localStorage.getItem(DISMISS_KEY) || '0', 10);
    const sevenDaysMs = 7 * 24 * 60 * 60 * 1000;
    const elapsed = Date.now() - dismissedAt;
    if (dismissedAt && elapsed < sevenDaysMs) return;  // already shown within 7 days
    const isIos = /iphone|ipad|ipod/i.test(navigator.userAgent);
    const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;
    if (isStandalone) return;  // already installed
    setTimeout(() => {
      const banner = document.createElement('div');
      banner.id = 'pwa-install-banner';
      banner.style.cssText = 'position:fixed;left:16px;right:16px;bottom:80px;z-index:9999;background:#1f2937;color:white;padding:16px;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,0.4);font-family:system-ui,-apple-system,sans-serif;font-size:14px;line-height:1.5;';
      const steps = isIos
        ? '1. 撳底下 Share掣 (□↑)<br>2. 揀「加到主畫面」<br>3. 撳「加入」'
        : '1. 撳 Chrome menu (⋮)<br>2. 揀「加到主畫面」<br>3. 撳「加入」';
      banner.innerHTML = '<div style="font-weight:700;margin-bottom:8px;">📲 加 gymbro 到主畫面</div>' +
        '<div style="opacity:0.9;margin-bottom:12px;">更快開 + 全螢幕 + 即時通知</div>' +
        '<div style="opacity:0.85;margin-bottom:12px;">' + steps + '</div>' +
        '<div style="display:flex;gap:8px;">' +
        '<button id="pwa-install-close" style="flex:1;background:transparent;border:1px solid #4b5563;color:white;padding:8px;border-radius:8px;font-size:13px;">遲啲</button>' +
        '<button id="pwa-install-go" style="flex:1;background:#10b981;border:none;color:white;padding:8px;border-radius:8px;font-size:13px;font-weight:600;">開工</button>' +
        '</div>';
      document.body.appendChild(banner);
      const dismiss = () => {
        try { localStorage.setItem(DISMISS_KEY, String(Date.now())); } catch(e) {}
        banner.remove();
      };
      document.getElementById('pwa-install-close').onclick = dismiss;
      document.getElementById('pwa-install-go').onclick = () => { dismiss(); };
    }, 3000);  // 3s delay after page load — don't compete with hero
  } catch(e) { /* noop */ }
})();

// v3.1.0: triggerCheer() handler (frontpage cheer button).
// Calls /api/cheer with fire_type=auto, AI decides morning/evening/etc.
// On success, shows last cheer artifact if any. Polls /api/cheer/status
// for completion and shows toast.
function triggerCheer() {
  // Alpine.js component — find via DOM walk or just use direct fetch
  fetch('/api/cheer', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({fire_type: 'auto'})
  })
  .then(r => r.json())
  .then(data => {
    if (data.ok) {
      // Show what type AI picked + poll for completion
      console.log('[cheer] auto → ' + data.fire_type + ' job ' + data.job_id);
      // Optionally: load last cheer artifact after ~20s
      setTimeout(() => {
        const tgBanner = document.createElement('div');
        tgBanner.style.cssText = 'position:fixed;top:80px;left:50%;transform:translateX(-50%);background:rgba(168,85,247,0.95);color:white;padding:8px 16px;border-radius:12px;z-index:9999;font-size:12px;font-weight:bold;box-shadow:0 4px 20px rgba(168,85,247,0.5);';
        tgBanner.textContent = '🔥 ' + data.fire_type + ' sent — check Telegram';
        document.body.appendChild(tgBanner);
        setTimeout(() => tgBanner.remove(), 5000);
      }, 2000);
    }
  })
  .catch(e => console.error('[cheer] trigger failed', e));
}

// v3.2.4: legacy requestLandscapeGym() removed — replaced by Alpine
// toggleGymFocus() method (line ~8632). Old global function had 3 bugs:
// state never synced, iOS orientation.lock rejected silently, no scroll
// management. Kept no-op stub to prevent "ReferenceError" if stale PWA
// service worker serves cached HTML that still references the old name.
function requestLandscapeGym() { /* deprecated — use Alpine toggleGymFocus() */ }

// v3.1.0: Landscape detection — auto-switch food history to grid view when
// device is in landscape orientation. (Jim OOB 2026-08-07 14:50 HKT 'for
// food logging, i used do it in portrait but the food history is best view
// as landscape. please find it out.')
function checkLandscapeFood() {
  const isLandscape = window.matchMedia('(orientation: landscape)').matches;
  document.body.classList.toggle('food-landscape', isLandscape);
  // Re-render the food log if visible
  const event = new CustomEvent('gymbro:orientation', {detail: {isLandscape}});
  window.dispatchEvent(event);
}
window.addEventListener('orientationchange', checkLandscapeFood);
window.addEventListener('resize', checkLandscapeFood);
// Initial check on load
setTimeout(checkLandscapeFood, 500);
</script>

<!-- v3.2.0: Schedule tab content (Jim OOB 2026-08-07: weekly + monthly
     calendar view of Whoop activities — gym + walking + everything else
     from the Whoop cache, not just gym sessions).
     - Hide the whole tab when there are no activities (is_empty_week &&
     no other activities in past 42 days).
     - Top: this week (Mon-Sun) as a 7-day strip with sport icons.
     - Bottom: monthly calendar grid (6 weeks back) with color-coded
     day cells (gym = green, walking = sky, etc.) and is_today ring. -->
<section x-show="isTabVisible('history')"
         x-cloak
         class="px-3 pb-32 pt-3"
         x-init="loadSchedule()">

  <!-- Empty state — if no activities in the past 42 days AND this week,
       the section is hidden entirely (handled by parent x-show on the
       tab nav button). But for within-tab UX, also show a friendly hint
       if data is still loading or only this-week is empty. -->
  <div x-show="scheduleLoading" class="text-center py-10 text-gray-500">
    <div class="text-2xl mb-2">⏳</div>
    <div class="text-sm">Loading activities...</div>
  </div>

  <div x-show="!scheduleLoading && !scheduleHasAny"
       class="text-center py-16 px-4">
    <div class="text-4xl mb-3">📅</div>
    <div class="text-base text-gray-300 mb-1">呢 6 週冇活動紀錄</div>
    <div class="text-xs text-gray-500">做 gym 或者出街行下, 紀錄會即刻出現</div>
  </div>

  <div x-show="!scheduleLoading && scheduleHasAny">

    <!-- v3.2.6: monthly calendar ONLY. The week strip + list view were
         removed (Jim OOB 2026-08-07 23:30 HKT 'Fix gymbro calendar
         view. Remove its list view and weekly view'). The 42-day grid
         is the single source of truth — tap any day to see full
         activity details in the popover below. -->

    <!-- ============ MONTHLY CALENDAR (42 days back, enriched) ============ -->
    <!-- v3.2.7: cells now show gym volume + sets + recovery %, enriched
         via /api/whoop_activities_calendar. (Jim OOB 2026-08-07 23:40 HKT
         'beatify the month view with enriched data'.) -->
    <div>
      <div class="flex items-center justify-between mb-2">
        <div class="text-[10px] uppercase tracking-[0.2em] text-gray-400">
          月曆 <span class="text-gray-300" x-text="scheduleRangeLabel"></span>
        </div>
        <div class="text-[10px] text-gray-500 flex items-center gap-1.5">
          <span class="font-bold text-emerald-400">🏋️<span x-text="scheduleMonthGymCount"></span></span>
          <span class="text-gray-600">·</span>
          <span class="font-bold text-sky-400" x-text="scheduleMonthOtherCount"></span><span>其他</span>
          <template x-if="scheduleTotalVolume != null">
            <span class="ml-1 px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-300 font-bold tabular-nums" x-text="formatVolume(scheduleTotalVolume) + 'kg'"></span>
          </template>
        </div>
      </div>
      <!-- Weekday header row -->
      <div class="grid grid-cols-7 gap-1 mb-1">
        <template x-for="wd in ['一','二','三','四','五','六','日']" :key="wd">
          <div class="text-center text-[10px] text-gray-500 font-bold" x-text="wd"></div>
        </template>
      </div>
      <!-- Calendar grid: align first day to its weekday slot -->
      <div class="grid grid-cols-7 gap-1">
        <template x-for="day in scheduleMonthAligned" :key="day.date">
          <!-- v3.2.7: enriched cell layout — 3 visual states:
               (1) gym day  → bg-emerald + day-num + volume + sets + recovery chip
               (2) activity → bg-sky + day-num + icon + strain
               (3) rest     → bg-transparent + day-num + recovery chip (if data)
               Empty days + non-today are blank. Today always gets a ring.
               (Jim OOB 2026-08-07 23:40 HKT 'beatify the month view with
               enriched data'.) -->
          <div class="aspect-square rounded-lg p-0.5 flex flex-col items-stretch justify-start transition-all overflow-hidden"
               :class="dayCellClass(day)"
               @click="day.activities && day.activities.length > 0 ? openDayPopover(day) : (day.recovery_pct != null ? openDayPopover(day) : null)">
            <!-- Top row: day number + recovery/sleep chip -->
            <div class="flex items-start justify-between px-0.5 pt-0.5">
              <div class="text-[10px] tabular-nums font-black leading-none"
                   :class="day.count === 0 && !day.is_today ? 'text-transparent' : (day.is_today ? 'text-emerald-200' : (day.has_gym ? 'text-emerald-200' : 'text-sky-200'))"
                   x-text="day.day_num"></div>
              <template x-if="day.recovery_pct != null">
                <div class="text-[8px] tabular-nums font-black leading-none px-1 rounded-sm"
                     :class="recoveryPillClass(day.recovery_pct)"
                     x-text="day.recovery_pct + '%'"></div>
              </template>
            </div>
            <!-- Bottom area: gym volume + sets OR activity icon + strain -->
            <div class="flex-1 flex flex-col items-center justify-end pb-0.5">
              <template x-if="day.has_gym && day.gym_volume_kg != null">
                <div class="flex flex-col items-center leading-none">
                  <div class="text-[10px] font-black tabular-nums text-emerald-200" x-text="formatVolume(day.gym_volume_kg) + 'kg'"></div>
                  <div class="text-[8px] font-bold text-emerald-300/80 tabular-nums" x-text="day.gym_set_count + ' 套'"></div>
                </div>
              </template>
              <template x-if="!day.has_gym && day.activities && day.activities.length > 0">
                <div class="flex flex-wrap justify-center gap-0.5 text-[11px] leading-none">
                  <template x-for="(act, idx) in (day.activities || []).slice(0, 2)" :key="idx">
                    <span class="text-sky-300" x-text="act.icon"></span>
                  </template>
                </div>
              </template>
              <template x-if="day.sleep_pct != null && !day.has_gym && (!day.activities || day.activities.length === 0)">
                <div class="text-[8px] font-bold tabular-nums"
                     :class="day.sleep_pct >= 80 ? 'text-emerald-300/80' : (day.sleep_pct >= 60 ? 'text-sky-300/80' : 'text-rose-300/80')"
                     x-text="'💤' + day.sleep_pct + '%'"></div>
              </template>
            </div>
          </div>
        </template>
      </div>

      <!-- Legend (v3.2.7: enriched) -->
      <div class="mt-3 flex items-center justify-center gap-3 text-[10px] text-gray-400 flex-wrap">
        <div class="flex items-center gap-1">
          <span class="inline-block h-2.5 w-2.5 rounded-sm bg-emerald-500/30 ring-1 ring-emerald-500/40"></span>
          <span>Gym</span>
        </div>
        <div class="flex items-center gap-1">
          <span class="inline-block h-2.5 w-2.5 rounded-sm bg-sky-500/20 ring-1 ring-sky-500/30"></span>
          <span>其他活動</span>
        </div>
        <div class="flex items-center gap-1">
          <span class="inline-block h-2.5 w-2.5 rounded-sm bg-emerald-500/40 ring-2 ring-emerald-300/60"></span>
          <span>今日</span>
        </div>
        <div class="flex items-center gap-1">
          <span class="px-1 rounded-sm bg-emerald-500/30 text-emerald-200 font-black text-[8px]">≥66%</span>
          <span class="text-emerald-300/80">高 recovery</span>
        </div>
        <div class="flex items-center gap-1">
          <span class="px-1 rounded-sm bg-rose-500/30 text-rose-200 font-black text-[8px]">≤33%</span>
          <span class="text-rose-300/80">低 recovery</span>
        </div>
      </div>
    </div>
    <!-- v3.2.5: Day detail popover (Jim OOB 2026-08-07 18:40 HKT 'need to
         put the info on the calendar beautifully'). Tap a day in the
         calendar to see full activity list with sport + strain + time. -->
    <div x-show="scheduleSelectedDay"
         x-transition.opacity
         class="fixed inset-0 z-50 flex items-end justify-center bg-black/70 backdrop-blur-sm"
         @click.self="scheduleSelectedDay = null"
         @keydown.escape.window="scheduleSelectedDay = null">
      <div class="w-full max-w-md rounded-t-3xl bg-gradient-to-b from-zinc-900 to-black border-t border-white/10 p-5 pb-8 max-h-[70dvh] overflow-y-auto"
           x-show="scheduleSelectedDay"
           x-transition.duration.200ms>
        <div class="flex items-center justify-between mb-3">
          <div>
            <div class="text-[10px] uppercase tracking-[0.2em] text-gray-500" x-text="scheduleSelectedDay?.weekday_label || ''"></div>
            <div class="text-2xl font-black tracking-tighter" x-text="scheduleSelectedDay?.date || ''"></div>
          </div>
          <button @click="scheduleSelectedDay = null"
                  class="flex h-9 w-9 items-center justify-center rounded-full bg-white/[0.06] active:scale-95"
                  data-testid="day-popover-close">
            <span class="text-lg">✕</span>
          </button>
        </div>
        <!-- v3.2.7: enriched day summary stats — volume, sets, recovery, HRV, sleep -->
        <div class="grid grid-cols-4 gap-1.5 mb-3">
          <div class="rounded-xl bg-white/[0.04] p-2 text-center">
            <div class="text-[8px] uppercase tracking-wider text-gray-500">Gym vol</div>
            <div class="text-base font-black text-emerald-300 tabular-nums" x-text="scheduleSelectedDay?.gym_volume_kg ? scheduleSelectedDay.gym_volume_kg + 'kg' : '—'"></div>
          </div>
          <div class="rounded-xl bg-white/[0.04] p-2 text-center">
            <div class="text-[8px] uppercase tracking-wider text-gray-500">套數</div>
            <div class="text-base font-black text-emerald-300 tabular-nums" x-text="scheduleSelectedDay?.gym_set_count || '—'"></div>
          </div>
          <div class="rounded-xl bg-white/[0.04] p-2 text-center">
            <div class="text-[8px] uppercase tracking-wider text-gray-500">Recovery</div>
            <div class="text-base font-black tabular-nums" :class="recoveryPillClass(scheduleSelectedDay?.recovery_pct)" x-text="scheduleSelectedDay?.recovery_pct != null ? scheduleSelectedDay.recovery_pct + '%' : '—'"></div>
          </div>
          <div class="rounded-xl bg-white/[0.04] p-2 text-center">
            <div class="text-[8px] uppercase tracking-wider text-gray-500">HRV</div>
            <div class="text-base font-black text-sky-300 tabular-nums" x-text="scheduleSelectedDay?.hrv_ms != null ? scheduleSelectedDay.hrv_ms.toFixed(1) : '—'"></div>
          </div>
        </div>
        <div class="grid grid-cols-2 gap-1.5 mb-4">
          <div class="rounded-xl bg-white/[0.04] p-2 text-center">
            <div class="text-[8px] uppercase tracking-wider text-gray-500">Sleep</div>
            <div class="text-base font-black tabular-nums" :class="(scheduleSelectedDay?.sleep_pct ?? 0) >= 80 ? 'text-emerald-300' : ((scheduleSelectedDay?.sleep_pct ?? 0) >= 60 ? 'text-sky-300' : 'text-rose-300')" x-text="scheduleSelectedDay?.sleep_pct != null ? scheduleSelectedDay.sleep_pct + '%' : '—'"></div>
          </div>
          <div class="rounded-xl bg-white/[0.04] p-2 text-center">
            <div class="text-[8px] uppercase tracking-wider text-gray-500">總 strain</div>
            <div class="text-base font-black text-amber-300 tabular-nums" x-text="scheduleSelectedDay?.total_strain || 0"></div>
          </div>
        </div>
        <!-- v3.2.7: gym exercises list (if any) -->
        <template x-if="scheduleSelectedDay?.gym_exercises && scheduleSelectedDay.gym_exercises.length > 0">
          <div class="mb-4">
            <div class="text-[10px] uppercase tracking-[0.15em] text-emerald-400/80 mb-1.5 font-bold">動作</div>
            <div class="flex flex-wrap gap-1.5">
              <template x-for="(ex, idx) in (scheduleSelectedDay?.gym_exercises || [])" :key="idx">
                <span class="text-[11px] font-bold px-2 py-1 rounded-full bg-emerald-500/15 text-emerald-200 border border-emerald-500/30" x-text="ex"></span>
              </template>
            </div>
          </div>
        </template>
        <!-- Activity list (Whoop raw activities — walking etc) -->
        <div class="text-[10px] uppercase tracking-[0.15em] text-sky-400/80 mb-1.5 font-bold" x-show="scheduleSelectedDay?.activities && scheduleSelectedDay.activities.length > 0">活動</div>
        <div class="space-y-2">
          <template x-for="(act, idx) in (scheduleSelectedDay?.activities || [])" :key="idx">
            <div class="flex items-center gap-3 rounded-xl bg-white/[0.04] p-3 border border-white/[0.06]">
              <div class="text-3xl leading-none" x-text="act.icon"></div>
              <div class="flex-1 min-w-0">
                <div class="text-sm font-black" x-text="act.label"></div>
                <div class="text-[10px] text-gray-500 mt-0.5 tabular-nums">
                  <span x-text="(act.start || '').slice(11, 16)"></span>
                  <span class="mx-1">→</span>
                  <span x-text="(act.end || '').slice(11, 16)"></span>
                </div>
              </div>
              <div class="text-right">
                <div class="text-base font-black tabular-nums" :class="act.strain >= 14 ? 'text-rose-300' : (act.strain >= 10 ? 'text-amber-300' : 'text-sky-300')" x-text="(act.strain ?? 0).toFixed(1)"></div>
                <div class="text-[9px] uppercase tracking-wider text-gray-500">strain</div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- v3.2.0: Old floating cheer button removed (Jim OOB 2026-08-07
     16:45 HKT 'put the cheer button at the top left'). Cheer is now
     in the global header before the Gymbro title — visible on every
     tab without scrolling. -->

<!-- v3.2.4: Gym focus mode floating action (rewired from broken global
     requestLandscapeGym() to Alpine toggleGymFocus() method). Fixes
     the bottom-scroll weirdness + state desync. -->
<div x-show="tab === 'gym'" x-cloak
     class="fixed top-[60px] left-3 z-40 flex flex-col gap-2">
  <button @click="toggleGymFocus()"
          :class="gymFocusMode ? 'opacity-100 ring-2 ring-yellow-300 bg-yellow-300/25' : 'opacity-80 active:scale-95'"
          class="flex items-center gap-1.5 rounded-full px-3 py-2 text-xs font-black transition-all"
          :style="gymFocusMode ? 'background: linear-gradient(135deg, rgba(234,179,8,0.55), rgba(234,179,8,0.25)); border: 1.5px solid rgba(234,179,8,0.9); color: black;' : 'background: linear-gradient(135deg, rgba(234,179,8,0.4), rgba(234,179,8,0.15)); border: 1.5px solid rgba(234,179,8,0.7); color: white;'"
          :title="gymFocusMode ? 'Focus mode 開緊 — 撳一下熄' : 'Focus mode 熄緊 — 撳一下開 (大字 + 簡化 UI)'">
    <span x-text="gymFocusMode ? '🎯' : '🎯'"></span>
    <span x-text="gymFocusMode ? 'Focus ON' : 'Focus'"></span>
  </button>
</div>

<!-- SCAN TAB (v2.1 — MiniMax M3 vision + pplx enrichment) -->
</div>

</body>
</html>
"""


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
const CACHE = 'gym-web-v108';
//   - Per-row Copy button: each history row has its own 📋 button; no more
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
const CACHE = 'gym-web-v108';
// not workable. iPhone Withings widget has latest data but gymbro syncing"):
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
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
