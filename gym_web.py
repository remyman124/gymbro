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
__version__ = "2.5.1"


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
    Falls back to most recent cache entry so Jim always sees his latest weigh-in."""
    d = _safe_read_json(WITHINGS_CACHE)
    body = d.get("body") if isinstance(d, dict) else None
    if isinstance(body, dict) and body.get("weight_kg"):
        return body
    # Fallback: most recent weight from `weight_today` or any cached body reading
    for key in ("weight_kg", "fat_pct", "fat_kg", "muscle_kg", "hydration_pct", "bone_kg"):
        if body and body.get(key) is not None:
            return body  # at least some field populated
    # Last resort: try direct measurement lookup from Withings
    try:
        import subprocess, json, re
        # Try wider windows until we find a reading
        for window in (7, 14, 30, 90):
            r = subprocess.run(
                ["python3", "/home/work/.hermes/skills/withings/withings.py", "body", str(window)],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0 and r.stdout.strip():
                # Parse CLI table — first data line is most recent
                for line in r.stdout.strip().splitlines():
                    parts = line.split()
                    if len(parts) >= 3 and re.match(r"\d{4}-\d{2}-\d{2}", parts[0]):
                        try:
                            return {
                                "date": parts[0],
                                "weight_kg": float(parts[1]),
                                "fat_pct": float(parts[2]),
                            }
                        except (ValueError, IndexError):
                            continue
                break  # exit loop if subprocess worked
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


@app.route("/api/health_overlay")
def api_health_overlay():
    """Single endpoint for the hero overlay.
    - Top-left: Whoop recovery %
    - Top-right: Withings weight kg + fat % (latest reading, drives Jim's goal)
    """
    return jsonify({
        "recovery": _recovery_pct(),
        "weight_kg": _withings_weight(),
        "fat_pct": _withings_fat_pct(),
        "weight_date": (_withings_body_latest() or {}).get("date"),
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
        "name": "Gym · Jim",
        "short_name": "GymBro",
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


def _pplx_enrich(dish_desc: str) -> str:
    """Call pplx sonar-pro for nutrition enrichment of described dishes.

    Prompt focuses on chain/brand lookup + standard portion + kcal/P/C/F
    per item, structured answer (no marketing copy).
    """
    api_key = _pplx_api_key()
    if not api_key:
        return "（PPLX 金鑰未設定）"
    prompt = (
        f"由以下香港/廣東話食物描述：\n\n「{dish_desc}」\n\n"
        "幫我做兩件事：\n"
        "1. 識別每樣菜式所屬嘅餐廳/連鎖/品牌（如：沙嗲王、KFC、大家樂、太興、添好運等），"
        "列出每樣嘅 standard portion / 標準份量同每份大概嘅卡路里、蛋白質、碳水、脂肪。\n"
        "2. 如有 brand-specific nutrition 數據（例如 KFC 雞件卡路里），用嗰啲 official 數。"
        "如無 brand-specific 數，請用一般常見 portion。\n\n"
        "用繁體中文、表格或 bullet 列明。唔好講餐廳裝修、唔好建議其他餐廳。"
    )
    payload = {
        "model": "sonar-pro",
        "messages": [
            {"role": "system", "content": "你係香港連鎖餐廳 nutrition 查詢助手。用事實同官方數據回答，唔好幻想。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1400,
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


def _detect_shared_meal(dish_desc: str) -> bool:
    """Heuristic — detect if dish description suggests shared meal."""
    shared_indicators = [
        "兩人份", "二人份", "分享", "share", "套餐", "二人餐", "二人",
        "set menu", "family", "set for two", "二人用", "二人套餐",
        "set  for", "二人用套餐",
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


def _append_to_sheet_nutrition(entry: dict) -> dict:
    """Mirror entry to Google Sheet Nutrition tab (sheetId 474877075).
    Returns {"ok": bool, "range": str} — silent on quota/error."""
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
        # Append row to Nutrition tab
        SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
        row_data = [
            entry.get("date", today_iso()),
            f"{entry.get('date', today_iso())}T{entry.get('time', now_iso().split('T')[-1][:5])}:00+08:00",
            entry.get("meal_type", "meal"),
            entry.get("meal_name", entry.get("name", "scan"))[:120],
            entry.get("restaurant_chain", ""),
            str(entry.get("calories", 0)),
            str(entry.get("protein", 0)),
            str(entry.get("carbs", 0)),
            str(entry.get("fat", 0)),
            entry.get("note", "scan_food"),
            entry.get("source", "vision+pplx"),
            "",
        ]
        body = {"values": [row_data], "majorDimension": "ROWS"}
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Nutrition:append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        return {"ok": True, "range": resp.get("updates", {}).get("updatedRange", "?")}
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

    # 2. pplx enrichment
    pplx_desc = _pplx_enrich(vision_desc)

    # 3. Heuristic share + macros hint (vision often gives totals)
    shared = _detect_shared_meal(vision_desc + " " + pplx_desc)
    jim_ratio = 0.60 if shared else 1.00

    # Defaults — keep simple (downstream corrections refine)
    estimate_match = re.search(r"約?\s*(\d{3,4})\s*[kK]?[cC]al|大約\s*(\d{3,4})\s*千卡", vision_desc + pplx_desc)
    if estimate_match:
        raw_kcal = int(estimate_match.group(1) or estimate_match.group(2))
    else:
        raw_kcal = 0  # No reliable estimate — log with 0, user can correct
    jim_kcal = round(raw_kcal * jim_ratio)

    p_match = re.search(r"蛋白質[約大概]*\s*(\d+)\s*[gk]克?", vision_desc + pplx_desc)
    raw_p = int(p_match.group(1)) if p_match else 0
    jim_p = round(raw_p * jim_ratio)

    # 4. Build entry
    now_hkt_dt = datetime.now(timezone(timedelta(hours=8)))
    entry = {
        "date": today_iso(),
        "time": now_hkt_dt.strftime("%H:%M"),
        "timestamp_iso": now_iso(),
        "meal_type": "scan",
        "meal_name": f"scan_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}",
        "name": vision_desc[:200],
        "vision_raw_desc": vision_desc,
        "pplx_enrichment": pplx_desc,
        "restaurant_chain": "",  # user/correction can fill
        "share_with_wife": ("Jim 60% / 小寶 40% (auto-applied)" if shared else "Jim 100% (solo)"),
        "is_shared_meal": shared,
        "calories": jim_kcal,
        "protein": jim_p,
        "carbs": 0,
        "fat": 0,
        "raw_kcal_estimate": raw_kcal,
        "raw_p_estimate": raw_p,
        "source": "v2.1-scan (minimax-m3 + pplx-sonar-pro)",
        "models_used": ["minimax-m3", "pplx-sonar-pro"],
        "confidence": "single-pass vision+pplx (Jim can correct via /api/scan_correct)",
        "notion_synced": False,
        "image_saved_to": "",  # filled below
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
    scan_log.append({
        "scan_index": scan_index,
        "timestamp_iso": entry["timestamp_iso"],
        "name": entry["name"],
        "calories": entry["calories"],
        "protein": entry["protein"],
        "shared": entry["is_shared_meal"],
        "image_path": str(img_path),
        "image_url": f"/scan_img/{img_filename}",
        "restaurant_chain": entry["restaurant_chain"],
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
    """
    limit = int(request.args.get("limit", 5))
    scan_log = _load_scan_log()
    # v2.4: drop failed scans (name/vision_short contain Vision failed marker)
    def _is_failed_scan(s):
        n = str(s.get("name", "")) + " " + str(s.get("vision_short", ""))
        return ("失敗" in n or "NameError" in n or "failed" in n.lower())
    successful = [s for s in scan_log if not _is_failed_scan(s)]
    recent = successful[-limit:][::-1]
    return jsonify({"scans": recent, "total": len(scan_log), "filtered": len(scan_log) - len(successful)})


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

    # 2. pplx enrichment
    pplx_desc = _pplx_enrich(vision_desc)

    # 3. Build preview entry (NOT written yet)
    shared = _detect_shared_meal(vision_desc + " " + pplx_desc)
    jim_ratio = 0.60 if shared else 1.00

    kcal_match = re.search(r"約?\s*(\d{3,4})\s*[kK]?[cC]al|大約\s*(\d{3,4})\s*千卡", vision_desc + pplx_desc)
    raw_kcal = int((kcal_match.group(1) or kcal_match.group(2)) if kcal_match else 0)
    p_match = re.search(r"蛋白質[約大概]*\s*(\d+)\s*[gk]克?", vision_desc + pplx_desc)
    raw_p = int(p_match.group(1)) if p_match else 0
    jim_kcal = round(raw_kcal * jim_ratio)
    jim_p = round(raw_p * jim_ratio)

    # Try to extract restaurant chain from vision or pplx (heuristic: first capitalised phrase)
    chain_match = re.search(r"([\u4e00-\u9fff]{2,6}(?:王|軒|亭|餐廳|食堂|廚|小店|屋|樓))", vision_desc + pplx_desc)
    restaurant_guess = chain_match.group(1) if chain_match else ""

    preview = {
        "preview_id": f"pv_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}",
        "image_path": str(img_path),
        "image_url": f"/scan_img/{img_filename}",
        "vision_desc": vision_desc,
        "vision_short": vision_desc[:300],
        "pplx_short": pplx_desc[:500],
        "suggested_entry": {
            "date": today_iso(),
            "time": now_hkt_dt.strftime("%H:%M"),
            "meal_type": "scan",
            "name": vision_desc[:120],
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
    pplx_desc = _pplx_enrich(vision_desc)

    shared = _detect_shared_meal(vision_desc + " " + pplx_desc)
    jim_ratio = 0.60 if shared else 1.00
    kcal_match = re.search(r"約?\s*(\d{3,4})\s*[kK]?[cC]al|大約\s*(\d{3,4})\s*千卡", vision_desc + pplx_desc)
    raw_kcal = int((kcal_match.group(1) or kcal_match.group(2)) if kcal_match else 0)
    p_match = re.search(r"蛋白質[約大概]*\s*(\d+)\s*[gk]克?", vision_desc + pplx_desc)
    raw_p = int(p_match.group(1)) if p_match else 0
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
            "name": vision_desc[:120],
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


@app.route("/api/scan_commit", methods=["POST"])
def api_scan_commit():
    """Jim OOB 2026-07-23 22:42: 'all food logging should be preview and allow me to confirm before logging'.

    Receives the (possibly edited) suggested_entry + image_path from /api/scan_preview.
    ONLY NOW writes to nutrition_log.json + Google Sheet.

    If user_corrections are submitted (correction_form), they're appended permanently.
    """
    data = request.get_json(silent=True) or {}
    entry = data.get("entry", {})
    image_path = data.get("image_path", "")
    user_correction = data.get("user_correction")  # optional dict

    if not entry or not image_path:
        return jsonify({"ok": False, "error": "missing entry or image_path"}), 400

    img_path = Path(image_path)
    if not img_path.exists():
        return jsonify({"ok": False, "error": "image not found"}), 404

    now_iso_str = now_iso()
    entry["timestamp_iso"] = now_iso_str
    entry["source"] = "v2.2-scan (minimax-m3 + pplx-sonar-pro, Jim confirmed)"
    entry["models_used"] = ["minimax-m3", "pplx-sonar-pro"]
    entry["confidence"] = "Jim-confirmed preview"
    entry["notion_synced"] = False
    entry["image_saved_to"] = str(img_path)
    entry["user_correction"] = None

    # Append to nutrition log
    _append_to_nutrition_log(entry)
    sheet_result = _append_to_sheet_nutrition(entry)

    # Rename preview_*.jpg → scan_*.jpg
    now_hkt_dt = datetime.now(timezone(timedelta(hours=8)))
    final_name = f"scan_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}.jpg"
    final_path = SCAN_CACHE_DIR / final_name
    try:
        img_path.rename(final_path)
        image_url = f"/scan_img/{final_name}"
    except Exception:
        final_path = img_path
        image_url = f"/scan_img/{img_path.name}"

    # Append to scan_log
    scan_log = _load_scan_log()
    scan_index = len(scan_log)
    scan_log.append({
        "scan_index": scan_index,
        "timestamp_iso": now_iso_str,
        "name": entry.get("name", "scan"),
        "calories": entry.get("calories", 0),
        "protein": entry.get("protein", 0),
        "shared": entry.get("is_shared_meal", False),
        "image_path": str(final_path),
        "image_url": image_url,
        "restaurant_chain": entry.get("restaurant_chain", ""),
        "vision_short": entry.get("vision_raw_desc", "")[:120],
        "user_corrections": [user_correction] if user_correction else [],
    })
    _save_scan_log(scan_log)

    return jsonify({
        "ok": True,
        "scan_index": scan_index,
        "entry": entry,
        "sheet_synced": sheet_result.get("ok", False),
        "sheet_range": sheet_result.get("range", ""),
    })


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


def _run_whoop_pull_cached() -> dict:
    """Run whoop_pull.py if cache is stale (>2h old) OR pulled recently failed.
    Returns the latest Whoop data dict (cycles/recovery/sleep/workouts bare lists)."""
    now_ts = datetime.now().timestamp()
    if WHOOP_CACHE_PATH.exists():
        try:
            cache_mtime = WHOOP_CACHE_PATH.stat().st_mtime
            data = json.loads(WHOOP_CACHE_PATH.read_text())
            age_hr = (now_ts - cache_mtime) / 3600
            if age_hr < 2 and data.get("cycles"):
                return data
        except Exception:
            pass
    # Run whoop_pull.py
    try:
        result = _sp.run([sys.executable, str(WHOOP_PULL_SCRIPT)],
                          capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and WHOOP_CACHE_PATH.exists():
            return json.loads(WHOOP_CACHE_PATH.read_text())
    except Exception:
        pass
    if WHOOP_CACHE_PATH.exists():
        try:
            return json.loads(WHOOP_CACHE_PATH.read_text())
        except Exception:
            pass
    return {"cycles": [], "recovery": [], "sleep": [], "workouts": []}


def _extract_whoop_metrics(whoop: dict) -> dict:
    """Pull the headline metrics from Whoop V2 cache (Defensive parsing per Rule 34 pitfall)."""
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

    return {
        "recovery_pct": score.get('recovery_score'),
        "recovery_state": score.get('score_state'),
        "hrv_ms": score.get('hrv_rmssd_milli'),
        "rhr_bpm": score.get('resting_heart_rate'),
        "spo2_pct": score.get('spo2_percentage'),
        "skin_temp_c": score.get('skin_temp_celsius'),
        "sleep_id": (latest_sleep or {}).get('id'),
        "sleep_bed_hr": round(sleep_ss.get('total_in_bed_time_milli', 0) / 3600000, 2),
        "sleep_rem_min": round(sleep_ss.get('total_rem_sleep_time_milli', 0) / 60000, 1),
        "sleep_sws_min": round(sleep_ss.get('total_slow_wave_sleep_time_milli', 0) / 60000, 1),
        "sleep_perf_pct": ((latest_sleep or {}).get('score') or {}).get('sleep_performance_percentage'),
        "sleep_eff_pct": ((latest_sleep or {}).get('score') or {}).get('sleep_efficiency_percentage'),
        "today_workout_count": len(today_workouts),
        "cycle_id": (latest_cycle or {}).get('id'),
        "strain": (latest_cycle or {}).get('score', {}).get('strain'),
    }


def _synthesize_cheer_text(metrics: dict, fire_type: str = "manual") -> str:
    """Call pplx sonar-pro to synthesize detailed 8-section cheer text per
    cheer-routine Rule 22.

    Jim OOB 2026-07-23 17:35 HKT: voice script needs FULL detail. Now prompts
    pplx for 600-900 字 with all sections expanded.
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

    # Detail-rich prompt — explicit 8 sections with concrete numbers + insights
    prompt = f"""幫我寫一段 100% 繁中廣東話嘅{fire_type_zh}，教練加管家口吻，唔好有英文字。
語句要自然、口語化，每段都要有具體數字同實際建議，唔好空洞。

以下係今日已拉返嘅真實數據，務必全部用晒喺 cheer 內：
- 復原指數：{rec}% ({rec_status_zh}, 區間 {rec_state})
- 心跳變異：{hrv} 毫秒
- 靜止心跳：{rhr} 下/分鐘
- 血氧：{spo2}%
- 噉晚瞓：{sleep_hr} 個鐘頭（REM {sleep_rem} 分鐘、深層瞓 {sleep_sws} 分鐘、表現指數 {sleep_perf}%、效率 {sleep_eff}%）
- 今日已經做：{workout_n} 個 session
- 昨日疲勞度：{strain}
- Cycle ID：{cycle_id}
- HKT 時間：{hkt_str}

必須包含以下 8 個 section，每個 section 都要有 detail + 教練建議：
§1 打招呼 + HKT 時間 + 朝早/夜晚/即場 時段呼應
§2 Whoop 復原詳細解讀：四個核心數字（復原%、HRV、RHR、SPO2）每個講一個 insight，復原區間代表咩意思，今日適合咩強度 ({rec_advice_zh})
§3 睡眠評估：噉晚瞓咗幾多個鐘、深層瞓 REM 分鐘、表現指數、效率，每個講教練點睇。如果深層瞓少過 90 分鐘，要明確建議噉晚早瞓 + 鎂補充
§4 今日健身檢討：已經做咗 N 個 session，每個 session 嘅訓練容量、組數、重量分佈。教練建議點樣調整強度
§5 營養 + 水分建議：今日蛋白質目標、碳水比例、水份目標，根據訓練強度調整。教練具體建議食咩、食幾多
§6 噉晚恢復計劃：包括瞓前 routine（伸展、鎂、甘胺酸）、房溫、瞓幾多個鐘、手機距離床鋪
§7 明日預覽：根據今日復原 + 訓練 + 睡眠，建議明日做咩類型訓練、強度、注意事項
§8 收尾打氣：用純中文 closing（不要 Bon voyage），唔好講英文

格式要求：
- 全程用 paragraph prose，唔好用 list / bullet / table / **bold** headers
- 大量使用粵語助詞：嘅/啦/咗/嗰/咁/吖/囉/嘢 — 目標密度 ≥8 個 per 100 字
- 每個 section 之間用 `\\n\\n` 分隔（會喺 voice 階段轉成「。 」自然過渡）
- 長度：600-900 字，**唔好壓縮、唔好遺留數字**
- 唔好 fabricate 任何數字，全部用上面提供嘅真實數據

**嚴禁使用以下英文字**（會破壞 TTS 嘅廣東話韻律 — 必須用中文代替）：
- 常用動詞：keep, base, plan, use, using, treat, check, monitor, tracking, trend, stable, fact, matters, feel, felt, feeling, OK, ok, make sure
- 時間相關：time, times, hr, hrs, min, sec
- 訓練術語：session, workout, set, rep, drill, plate, bar, spot, lift, push, rest, PR, RM, build, bulk, cut, RPE, HIIT, squat, bench, deadlift, press, curl, row, lat pulldown, pullup
- 健康指標：HRV, SpO2, RHR, RPE, REM, N1, N2, N3, deep sleep, light sleep, awake, strain, recovery, level, range, target, delta, score, state, status
- 顏色狀態：YELLOW, GREEN, RED（或 yellow/green/red）
- 品牌 / 應用：Jim, Google, Whoop, Novotel, Wanchai, app, PC, phone, tab
- 單位：kg, lb, oz, g, kcal, ms, bpm
- 收尾：Bon voyage, Welcome home, Good luck, Good night
- 縮寫：e.g., i.e., vs, via, FYI, ASAP, P.S., OK
- 敬稱：Dr., Mr., Mrs., Ms.

凡係以上任何一個英文字，都必須改用括號內或相應嘅中文表示。寫嘅時候直接用中文，唔好諗住用英文再翻譯。

開始寫啦："""
    payload = {
        "model": "sonar-pro",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2400,
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


def _synthesize_cheer_voice(text: str) -> str:
    """Generate Edge-TTS WanLung voice MP3 from cheer text.

    Jim OOB 2026-07-23 17:35 HKT: voice was too short and lacked detail.
    Strategy (v2.5.1):
    - NO truncation (was capping at 280 chars, killing §3-§5 detail). Now full
      text up to 2000 chars (edge-tts safety).
    - Convert section breaks (\\n) into comma-separated clause continuations so
      WanLung reads naturally instead of stopping at line breaks.
    - Inject 5-7 intonation transitions ("下一節係..." / "教練建議係咁..." /
      "再講下...") between sections so each section gets a clear breath
      pause instead of running-on.
    - 100% 中文 enforcement (Rule 26 + Rule 37): zh-replace + audit loop,
      2 retries before fallback to detailed ~280 字 fallback script.
    - Timeout 45 → 90s for longer scripts.
    - NO Telegram 55s cap (Rule 32) — gymbro PWA has no upper bound on voice
      duration; Jim wants full data/insights/recommendation in the bubble.

    Returns file path or '' on failure.
    """
    try:
        # Step 1: zh-replace (first pass)
        voice_text = _voice_zh_replace(text)
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
            # Use a richer fallback (~280 字) — still better than the old 50-char stub
            voice_text = (
                "今朝好占姆。我係你嘅 AI 教練加管家，依家同你做個完整健康摘要啦。"
                "第一，Whoop 復原指數、心跳變異、靜止心跳、血氧呢四個核心數字影響你今日嘅訓練容量，"
                "教練建議根據復原區間決定強度；綠燈可以做高強度，黃燈做中強度，紅燈轉低強度或休息。"
                "第二，噉晚瞓嘅時長、深層瞓比例、表現指數決定你嘅恢復速度，"
                "如果深層瞓少過一個鐘頭，教練建議噉晚瞓前做半個鐘頭伸展，避免飲酒，食多啲蛋白質。"
                "第三，今日已經做咗幾多個 session，每個 session 嘅總重量、總組數、總次數寫入 Google Sheet 嗰度。"
                "教練建議每次收操後做五分鐘 foam roll，幫助筋膜放鬆。"
                "第四，營養嗰邊，蛋白質目標要夠、碳水要適量、脂肪要健康。"
                "教練建議今日總蛋白質最少一百五十克，水份最少兩公升半。"
                "第五，噉晚嘅收尾。講到尾，保持恆常、安全、逐步加上去就係最好嘅策略。"
                "祝你今早日順，旅途愉快。"
            )

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
        return str(out_mp3) if out_mp3.exists() else ""
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
    """Run cheer pipeline in background thread."""
    try:
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id] = {"status": "running", "step": "whoop_pull", "started_at": now_iso()}

        # 1. Whoop pull
        whoop = _run_whoop_pull_cached()
        metrics = _extract_whoop_metrics(whoop)
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({"step": "text_gen", "metrics": metrics})

        # 2. Text
        text = _synthesize_cheer_text(metrics, fire_type)
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({"step": "voice_gen", "text": text})

        # 3. Voice
        voice_path = _synthesize_cheer_voice(text)
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({"step": "image_gen", "voice_path": voice_path})

        # 4. Image
        context = f"{fire_type}_{int(time.time())}"
        image_path = _generate_cheer_image(context)
        with CHEER_JOBS_LOCK:
            CHEER_JOBS[job_id].update({"step": "done", "image_path": image_path})

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


@app.route("/api/cheer", methods=["POST"])
def api_cheer_trigger():
    """v2.5: Trigger a cheer fire from inside gymbro PWA.

    Jim OOB 2026-07-23: "Can copy all the cheer routine stuff into gymbro?".

    Returns immediately with {job_id}; pipeline runs in background thread
    (Whoop pull → pplx text → Edge TTS → MiniMax image → cheer_artifacts +
    audio_cache + image_cache sync).

    Poll /api/cheer/status?job_id=... for progress.
    """
    data = request.get_json(silent=True) or {}
    fire_type = data.get("fire_type", "manual")
    if fire_type not in ("morning", "evening", "manual"):
        fire_type = "manual"

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
        })
    return jsonify({"ok": True, "status": job["status"], "step": job.get("step"), "started_at": job.get("started_at")})


@app.route("/api/cheer/recent", methods=["GET"])
def api_cheer_recent():
    """v2.5: Return last N cheer fires (default 3) for cheer tab hero card."""
    limit = int(request.args.get("limit", 3))
    log = _load_cheer_log()
    recent = log[-limit:][::-1]
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
<title>Gym · Jim</title>
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
</style>
</head>
<body x-data="gymApp()" x-init="init()">

  <!-- Top Bar -->
  <header class="sticky top-0 z-50 border-b border-white/10 bg-black/[0.85] px-4 py-2 backdrop-blur-xl">
    <div class="flex items-center justify-between gap-2">
      <h1 @click="onBrandTap()" class="text-3xl font-black tracking-tighter cursor-pointer select-none active:opacity-60 transition-opacity" style="-webkit-user-select: none; -webkit-tap-highlight-color: transparent;">Gym</h1>
      <div class="flex flex-col items-end leading-tight">
        <span class="text-sm font-bold text-emerald-300 tabular-nums" x-text="clockStr"></span>
        <span class="text-[10px] uppercase tracking-[0.2em] text-gray-400" x-text="sessionDateStr"></span>
      </div>
    </div>
  </header>

  <!-- Toast -->
  <div class="toast" x-show="toast" x-text="toast" x-transition.opacity></div>

  <!-- Tab Content -->
  <main class="px-4 pb-20 pt-2">

    <!-- SET TAB (default) -->
    <section x-show="tab === 'set'" class="flex min-h-[calc(100dvh-14rem)] flex-col" x-cloak>

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
                  :class="{'saving': saving, 'opacity-50 cursor-not-allowed': cooldownUntil && Date.now() < cooldownUntil}"
                  :disabled="saving || (cooldownUntil && Date.now() < cooldownUntil)"
                  @click="logSet()"
                  x-text="(cooldownUntil && Date.now() < cooldownUntil) ? `⏳ REST ${cooldownRemaining}s` : (saving ? 'Saving…' : `✓ LOG SET ${currentSet ? currentSet.set : 1}`)">
          </button>
        </div>
      </div>
    </section>

    <!-- WORKOUT / PYRAMID TAB -->
    <section x-show="tab === 'workout'">
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

    <!-- HISTORY TAB -->
    <section x-show="tab === 'history'">
      <div class="flex items-baseline justify-between my-3">
        <div class="text-[10px] uppercase tracking-[0.2em] text-gray-400">Recent Sessions</div>
        <button class="text-xs text-gray-400 underline tap" @click="refreshHistory(true)" x-show="!loadingHistory">↻ Refresh</button>
        <span class="text-xs text-gray-400" x-show="loadingHistory">Loading…</span>
      </div>

      <!-- Jim OOB 2026-07-21: Removed date-range chips + global Copy.
           New pattern: ONE copy button per row in history list.
           Always copies that single day's data. No date-range. -->

      <div x-show="loadingHistory && history.length === 0" class="text-gray-500 text-center py-12">Loading history…</div>
      <div x-show="!loadingHistory && history.length === 0" class="text-gray-500 text-center py-12">No sessions yet — go log some 🔥</div>
      <template x-for="row in history" :key="row.date">
        <div class="bg-white/5 backdrop-blur border border-white/10 rounded-2xl p-4 mb-3 relative fade-up"
             :class="row.date === today ? 'ring-2 ring-yellow-400/50' : ''">
          <button class="absolute top-2 right-11 w-8 h-8 rounded-full flex items-center justify-center text-lg tap"
                  :style="copyingDate === row.date
                            ? 'background: rgba(250,204,21,0.25); border: 1px solid rgba(250,204,21,0.5); color: #fde68a; animation: spin 1s linear infinite;'
                            : 'background: rgba(99,102,241,0.20); border: 1px solid rgba(99,102,241,0.40); color: #c7d2fe;'"
                  :class="copyInFlight ? 'opacity-60 cursor-wait' : ''"
                  :disabled="copyInFlight"
                  @click="copyDay(row.date)"
                  :aria-label="copyingDate === row.date ? `Copying ${row.date}` : `Copy ${row.date}`"
                  :title="copyingDate === row.date ? 'Copying…' : '複製呢一日 workout log'"
                  x-text="copyingDate === row.date ? '⏳' : '📋'">📋</button>
          <button class="absolute top-2 right-2 w-8 h-8 rounded-full flex items-center justify-center text-lg tap"
                  style="background: rgba(239,68,68,0.20); border: 1px solid rgba(239,68,68,0.40); color: #fca5a5;"
                  @click="deleteSession(row.date)"
                  :aria-label="`Delete ${row.date}`"
                  title="Delete session">🗑</button>
          <div class="flex items-baseline gap-2 mb-1 pr-10">
            <div class="text-2xl font-black tracking-tight" x-text="row.date"></div>
            <span x-show="row.date === today"
                  class="text-[10px] uppercase tracking-[0.15em] font-bold px-2 py-0.5 rounded-full"
                  style="background: rgba(255,214,10,0.15); color: var(--gold); border: 1px solid rgba(255,214,10,0.35);">Today</span>
            <span x-show="row.completed"
                  class="text-[10px] uppercase tracking-[0.15em] font-bold px-2 py-0.5 rounded-full text-emerald-400"
                  style="background: rgba(16,185,129,0.12); border: 1px solid rgba(16,185,129,0.30);">✓ Done</span>
          </div>
          <div class="text-sm text-gray-300">
            <span class="font-bold" x-text="`${row.sets} set${row.sets === 1 ? '' : 's'}`"></span>
            <span class="text-gray-500"> · </span>
            <span class="font-bold" x-text="`${row.total_vol_kg}kg vol`"></span>
            <span x-show="row.exercises.length" class="text-gray-500" x-text="` · ${row.exercises.length} exercise${row.exercises.length === 1 ? '' : 's'}`"></span>
          </div>
          <div x-show="row.exercises.length" class="text-xs text-gray-400 mt-1 truncate" x-text="row.exercises.join(' · ')"</div>
        </div>
      </template>
    </section>


    <!-- SCAN TAB (v2.1 — MiniMax M3 vision + pplx enrichment) -->
    <section x-show="tab === 'scan'" x-cloak class="px-4 pb-32 pt-3">
      <div class="text-[10px] uppercase tracking-[0.2em] text-emerald-400 mb-2 text-center font-bold">掃描食物 / 餐單</div>
      <div class="text-xs text-gray-400 text-center mb-4">影相 → 自動記錄卡路里、蛋白質、餐廳</div>

      <!-- v2.3: two file inputs — (1) live camera + (2) iPhone photo stream picker (multiple) -->
      <input type="file" accept="image/*" capture="environment" @change="onScanFile($event)" x-ref="scanInputEl" style="display:none">
      <input type="file" accept="image/*" multiple @change="onScanPhotosPicked($event)" x-ref="scanPhotosInputEl" style="display:none">

      <!-- Big tap-to-scan card — opens live camera -->
      <button @click="$refs.scanInputEl.click()"
              :disabled="scanUploading"
              class="w-full rounded-2xl py-6 px-4 mb-3 transition-all active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed"
              style="background: linear-gradient(135deg, rgba(16,185,129,0.18), rgba(255,255,255,0.05)); border: 1.5px dashed rgba(16,185,129,0.55);">
        <div class="text-5xl mb-2" x-text="scanUploading ? '⏳' : '📸'"></div>
        <div class="text-base font-bold text-emerald-300" x-text="scanUploading ? 'AI 睇緊你張相…' : '撳呢度影相 / 揀圖'"></div>
        <div class="text-[10px] text-gray-400 mt-1" x-show="!scanUploading">食物、收據、外賣單都影得</div>
      </button>

      <!-- v2.3: iPhone photo stream picker — opens Photos app for multi-select (server cache independent) -->
      <button @click="$refs.scanPhotosInputEl.click()"
              :disabled="scanUploading || scanPhotosQueue.length > 0"
              class="w-full rounded-2xl py-4 px-4 mb-4 transition-all active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed"
              style="background: rgba(255,255,255,0.04); border: 1.5px solid rgba(16,185,129,0.35);">
        <div class="flex items-center justify-center gap-2">
          <div class="text-3xl">📷</div>
          <div class="text-left">
            <div class="text-sm font-bold text-emerald-300">iPhone 相簿（多選）</div>
            <div class="text-[10px] text-gray-400 mt-0.5">直接開 Photos app 揀幾張食物相，每張 AI 逐張 preview 確認</div>
          </div>
        </div>
      </button>

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
      <div x-show="previewEntry" class="rounded-2xl bg-yellow-500/10 backdrop-blur border-2 border-yellow-400/40 p-4 mb-4" x-cloak>
        <div class="text-[10px] uppercase tracking-[0.15em] text-yellow-300 mb-2 font-bold">⚠️ 預覽 — 未 log，請確認</div>
        <img :src="previewEntry?.image_url" class="w-full rounded-xl mb-3 max-h-48 object-cover bg-black/40">
        <div class="text-sm text-white mb-2" x-text="previewEntry?.vision_short || ''"></div>
        <div class="flex items-baseline gap-3 text-xs text-gray-300 mb-3">
          <span><span class="text-emerald-300 font-bold" x-text="previewCorrectForm.calories ?? 0"></span> kcal</span>
          <span><span class="text-emerald-300 font-bold" x-text="previewCorrectForm.protein ?? 0"></span> P</span>
          <span x-show="previewEntry?.suggested_entry?.is_shared_meal" class="text-yellow-300 font-bold">👥 60/40 share</span>
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

      <!-- v2.2 PHOTOSTREAM strip — today's photos with food/non-food classification -->
      <template x-if="photostream.length > 0">
        <div class="mb-4">
          <div class="flex items-center justify-between mb-2">
            <div class="text-[10px] uppercase tracking-[0.15em] text-gray-400 font-bold">今日相片
              <span class="text-emerald-300" x-text="`(${photostream.filter(p => p.classification?.is_food && !p.already_logged).length} 建議 log)`"></span>
            </div>
            <button @click="loadPhotostream(true)" class="text-[10px] text-emerald-300" :disabled="photostreamClassifying">
              <span x-text="photostreamClassifying ? '分類中...' : '↻ 重分類'"></span>
            </button>
          </div>
          <div class="grid grid-cols-3 gap-2">
            <template x-for="item in photostream" :key="item.path">
              <div class="relative rounded-xl overflow-hidden border" :class="item.classification?.is_food ? (item.already_logged ? 'border-white/10 opacity-50' : 'border-emerald-400/60') : 'border-white/10'">
                <img :src="item.url" class="w-full h-20 object-cover" loading="lazy">
                <div class="absolute inset-x-0 bottom-0 bg-black/70 text-[9px] p-1 leading-tight">
                  <template x-if="item.classification?.is_food && !item.already_logged">
                    <div class="text-emerald-300 font-bold" x-text="item.classification?.suggested_name?.slice(0, 14) || '食物'"></div>
                  </template>
                  <template x-if="item.classification?.is_food && item.already_logged">
                    <div class="text-gray-400">✓ 已 log</div>
                  </template>
                  <template x-if="item.classification && !item.classification.is_food">
                    <div class="text-gray-400">非食物</div>
                  </template>
                  <template x-if="!item.classification">
                    <div class="text-gray-500">— 分類中 —</div>
                  </template>
                </div>
                <template x-if="item.classification?.is_food && !item.already_logged">
                  <button @click="suggestLogFromPhoto(item)" class="absolute top-1 right-1 bg-emerald-500 text-black text-[10px] px-1.5 py-0.5 rounded font-bold active:scale-95">AI log 呢張</button>
                </template>
              </div>
            </template>
          </div>
        </div>
      </template>

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

      <!-- Recent scans (last 5) — v2.4 filters out failed uploads -->
      <div class="flex items-baseline justify-between mb-2">
        <div class="text-[10px] uppercase tracking-[0.15em] text-gray-400 font-bold">最近 5 個 scan</div>
        <div x-show="recentScansFiltered > 0" class="text-[10px] text-gray-500">
          過濾咗 <span class="text-yellow-300 font-bold" x-text="recentScansFiltered"></span> 條 failed upload
        </div>
      </div>
      <template x-if="recentScans.length === 0">
        <div class="text-xs text-gray-500 text-center py-6">未有 scan 紀錄</div>
      </template>
      <template x-for="scan in recentScans" :key="scan.scan_index">
        <div class="rounded-xl bg-white/[0.04] backdrop-blur border border-white/10 p-3 mb-2">
          <div class="flex gap-3 items-center">
            <img :src="scan.image_url" class="w-16 h-16 rounded-lg object-cover bg-black/40" loading="lazy">
            <div class="flex-1 min-w-0">
              <div class="text-xs text-white truncate" x-text="scan.name || scan.vision_short || '—'"></div>
              <div class="flex items-baseline gap-2 text-[11px] text-gray-400 mt-0.5">
                <span><span class="text-emerald-300 font-bold" x-text="scan.calories || 0"></span> kcal</span>
                <span><span class="text-emerald-300 font-bold" x-text="scan.protein || 0"></span> P</span>
                <span x-show="scan.shared" class="text-yellow-300">👥</span>
                <span x-show="(scan.user_corrections || []).length > 0" class="text-sky-300" x-text="`✏ ${(scan.user_corrections || []).length}`"></span>
              </div>
              <div class="text-[10px] text-gray-500" x-text="scan.timestamp_iso || ''"></div>
            </div>
          </div>
        </div>
      </template>
    </section>


    <!-- END TAB -->
    <section x-show="tab === 'end'" x-cloak>
      <div class="text-center my-6">
        <div class="text-[10px] uppercase tracking-[0.2em] text-gray-400">End Session</div>
        <h2 class="text-4xl font-black tracking-tighter mt-2">收檔時間</h2>
        <p class="text-gray-400 mt-2">收尾寫入 Google Sheet + Whoop log</p>
      </div>

      <div x-show="!endSummary">
        <div class="my-6">
          <label class="text-[10px] uppercase tracking-[0.2em] text-gray-400 mb-2 block">RPE (1-10)</label>
          <input type="number" min="1" max="10" placeholder="例: 7" x-model.number="endRPE" />
        </div>
        <button class="primary-btn w-full py-6 text-2xl tap mt-8 glow-ready" @click="endSession()" :class="{'saving': saving}">🏁 END SESSION</button>
        <div class="text-xs text-gray-500 text-center mt-3">Telegram 同步 ON by default (Jim 7/19 config)</div>
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
    <section x-show="tab === 'cheer'" x-cloak class="px-4 pb-32 pt-3">
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

      <!-- Recent fires (last 3) -->
      <div class="text-[10px] uppercase tracking-[0.15em] text-gray-400 mb-2 font-bold">最近 fires</div>
      <template x-if="cheerRecent.length === 0">
        <div class="text-xs text-gray-500 text-center py-6">未有 cheer 紀錄</div>
      </template>
      <template x-for="(fire, idx) in cheerRecent" :key="fire.fire_id || idx">
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
    </section>

  </main>

  <!-- Bottom Tab Bar — 2x2 grid (Jim OOB 2026-07-19) -->
  <nav class="fixed bottom-0 left-0 right-0 z-50 border-t border-white/10 bg-black/90 pb-[env(safe-area-inset-bottom)] backdrop-blur-2xl">
    <div class="grid grid-cols-3 grid-rows-2 gap-x-1 gap-y-1 px-2 py-1.5">
      <button class="flex items-center justify-center gap-2 rounded-lg py-1.5 transition-all" :class="tab === 'set' ? 'tab-active' : 'tab-inactive'" @click="tab = 'set'">
        <span class="text-lg leading-none">✓</span><span class="text-xs font-bold">Set</span>
      </button>
      <button class="flex items-center justify-center gap-2 rounded-lg py-1.5 transition-all" :class="tab === 'workout' ? 'tab-active' : 'tab-inactive'" @click="tab = 'workout'">
        <span class="text-lg leading-none">📊</span><span class="text-xs font-bold">Workout</span>
      </button>
      <button class="flex items-center justify-center gap-2 rounded-lg py-1.5 transition-all" :class="tab === 'history' ? 'tab-active' : 'tab-inactive'" @click="goToTab('history')">
        <span class="text-lg leading-none">📋</span><span class="text-xs font-bold">History</span>
      </button>
      <button class="flex items-center justify-center gap-2 rounded-lg py-1.5 transition-all" :class="tab === 'scan' ? 'tab-active' : 'tab-inactive'" @click="tab = 'scan'" style="background:rgba(16,185,129,0.18);box-shadow:inset 0 0 0 1px rgba(16,185,129,0.45);">
        <span class="text-lg leading-none">🍽️</span><span class="text-xs font-bold">Scan</span>
      </button>
      <button class="flex items-center justify-center gap-2 rounded-lg py-1.5 transition-all" :class="tab === 'end' ? 'tab-active' : 'tab-inactive'" @click="tab = 'end'">
        <span class="text-lg leading-none">🏁</span><span class="text-xs font-bold">End</span>
      </button>
      <button class="flex items-center justify-center gap-2 rounded-lg py-1.5 transition-all" :class="tab === 'cheer' ? 'tab-active' : 'tab-inactive'" @click="openCheerTab()" style="background:rgba(168,85,247,0.18);box-shadow:inset 0 0 0 1px rgba(168,85,247,0.5);">
        <span class="text-lg leading-none">🔥</span><span class="text-xs font-bold">Cheer</span>
      </button>
    </div>
  </nav>

  <script>
function gymApp() {
  return {
    tab: 'set',
    sessionDateStr: '',
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
    copyRange: 7,
    copyInFlight: false,
    copyingDate: null,  // Jim OOB 2026-07-22: per-row "⏳" spinner during copy.
    // Jim OOB 2026-07-21: 30s resting cooldown after LOG SET (prevents accidental double-tap)
    cooldownUntil: null,
    cooldownRemaining: 0,
    cooldownInterval: null,
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
    recentScansFiltered: 0,  // v2.4: count of failed scans skipped by filter
    // v2.5 cheer tab (Jim OOB 2026-07-23 "Can copy all the cheer routine stuff into gymbro?")
    cheerLatest: null,        // latest fire (object from /api/cheer/recent[0])
    cheerRecent: [],          // last 3 fires list
    cheerFiring: false,       // button disabled while pipeline runs
    cheerProgress: '',        // human-readable progress string
    cheerPct: 0,              // progress bar 0-100
    cheerJobId: null,         // current job_id being polled
    cheerLastFire: null,      // last-completed fire (full state from /api/cheer/status)
    cheerPollTimer: null,     // setInterval handle for status polling
    correctForm: { name: '', restaurant_chain: '', calories: null, protein: null, carbs: null, fat: '', note: '' },
    correctSubmitMsg: '',
    // v2.2 features (Jim OOB 2026-07-23 22:42 HKT)
    photostream: [],           // today's images with optional classification
    photostreamClassifying: false,
    scanPhotosQueue: [],       // v2.3: queue of preview entries from multi-photo iPhone picker
    scanPhotosQueueDone: 0,    // v2.3: how many previews fetched (out of scanPhotosQueue.length)
    previewEntry: null,        // current scan preview pending Jim confirmation
    previewEditing: false,     // toggle edit-mode for preview fields
    previewCorrectForm: { name: '', restaurant_chain: '', calories: null, protein: null, carbs: null, fat: null, note: '' },
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
      // Rotate quote every 4s
      setInterval(() => {
        const next = this.quoteBank[Math.floor(Math.random() * this.quoteBank.length)];
        if (next !== this.quote) this.quote = next;
      }, 4000);
      this.haptic();
    },

    tickClock() {
      const d = new Date();
      const hh24 = d.getHours();
      const mm = String(d.getMinutes()).padStart(2, '0');
      const ss = String(d.getSeconds()).padStart(2, '0');
      const hh12 = ((hh24 + 11) % 12) + 1;
      const ampm = hh24 < 12 ? 'AM' : 'PM';
      // Digital clock with blinking colon + AM/PM (Jim OOB 2026-07-19)
      this.clockStr = `${String(hh12).padStart(2,'0')}:${mm}:${ss} ${ampm}`;
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
      if (this.cooldownUntil && Date.now() < this.cooldownUntil) {
        const sec = Math.ceil((this.cooldownUntil - Date.now()) / 1000);
        this.flash(`等 ${sec}s 後先可以再 log set`);
        return;
      }
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
          // Jim OOB 2026-07-21: 30s resting cooldown after log
          this.cooldownUntil = Date.now() + 30000;
          this.cooldownRemaining = 30;
          if (this.cooldownInterval) clearInterval(this.cooldownInterval);
          this.cooldownInterval = setInterval(() => {
            const remaining = Math.max(0, Math.ceil((this.cooldownUntil - Date.now()) / 1000));
            this.cooldownRemaining = remaining;
            if (remaining <= 0) {
              clearInterval(this.cooldownInterval);
              this.cooldownInterval = null;
              this.cooldownUntil = null;
              this.cooldownRemaining = 0;
              this.flash('Rest done · ready 下一組');
            }
          }, 1000);
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
      } catch(e) { this.flash('Error: ' + e.message); }
      this.saving = false;
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

    async loadRecentScans() {
      try {
        const r = await fetch('/api/scan_recent?limit=5');
        const data = await r.json();
        this.recentScans = data.scans || [];
        this.recentScansFiltered = data.filtered || 0;
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
    async loadCheerRecent() {
      try {
        const r = await fetch('/api/cheer/recent?limit=3');
        const data = await r.json();
        const fires = data.fires || [];
        this.cheerRecent = fires;
        this.cheerLatest = fires[0] || null;
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
          // Running — update progress label
          const stepMap = {
            whoop_pull: 'WHOOP 拉緊數據…',
            text_gen: 'pplx 寫緊 cheer 文字…',
            voice_gen: 'Edge-TTS 整緊 WanLung 語音…',
            image_gen: 'MiniMax 整緊勵志圖…',
          };
          this.cheerProgress = stepMap[data.step] || `${data.status}: ${data.step}`;
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
          }),
        });
        const data = await r.json();
        if (data.ok) {
          this.flash(data.sheet_synced ? '✓ 已寫入 log + Sheet' : '✓ 已寫入 log（Sheet 跳過）');
          this.previewEntry = null;
          this.previewEditing = false;
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
      this.flash('Preview 已取消');
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
</script>

</body>
</html>
"""


# ---------- Service worker for PWA ----------
SERVICE_WORKER = """
// Jim OOB 2026-07-21: bump to v18 + activate-time nuclear flush of OLD
// caches (v17 and earlier). SW skipWaiting + clients.claim ensure iPhone PWA
// picks up the new SW on next reload. controllerchange handler below forces
// auto-reload so the user sees fresh HTML without manual pull-to-refresh.
// v18 changes (Jim OOB 2026-07-21):
//   - Per-row Copy button: each history row has its own 📋 button; no more
//     date-range chips. /api/export_text now accepts ?date=YYYY-MM-DD for
//     single-day export (legacy ?days=N still works).
//   - 30s REST cooldown after LOG SET: prevents accidental double-tap from
//     inflating set count. Button shows ⏳ REST ${cooldownRemaining}s and
//     is disabled during cooldown. After 30s, button returns to ✓ LOG SET.
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
const CACHE = 'gym-web-v28';
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
