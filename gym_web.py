#!/usr/bin/env python3
"""
Jim's Gym Web App — port 7000
Uber-style mobile-first interface for gym set logging via Tailnet VPN.

Stack: Flask 3.1.3 + Tailwind CDN + Alpine.js
Bind: 0.0.0.0:7000 (Tailscale IP 100.114.66.125)
Persistence: /home/work/.whoop_workout_log.json[YYYY-MM-DD]
PWA: installable, wake-lock enabled
"""
import json
import os
import secrets
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
    """Push local WORKOUT_LOG entries to Google Sheet (idempotent)."""
    payload = request.get_json(silent=True) or {}
    target_date = payload.get("date")  # optional: sync one date, else all
    log = load_log()
    dates = [target_date] if target_date else sorted(log.keys())
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
            # Handle BOTH session shapes (Jim OOB 2026-07-19):
            #   - legacy / telegram-text-mode: ex.sets[] = [{n, reps, weight_kg/weight, time}, ...]
            #   - gym-web-tap flat shape: ex IS a set itself = {exercise, set, reps, weight_kg, time}
            sub_sets = ex.get("sets") if isinstance(ex.get("sets"), list) else None
            if sub_sets:
                for s in sub_sets:
                    set_n = s.get("n")
                    if set_n is None:
                        continue
                    if not _has_sheet_row(date, ex.get("name", ""), set_n):
                        rows_to_push.extend(_session_to_sheet_rows(date, {"exercises": [ex]}))
                    else:
                        skipped += 1
            else:
                # gym-web-tap: each ex entry IS a single set
                set_n = ex.get("set")
                if set_n is None:
                    continue
                if not _has_sheet_row(date, ex.get("exercise", ""), set_n):
                    rows_to_push.extend(_session_to_sheet_rows(date, {"exercises": [ex]}))
                else:
                    skipped += 1
        if rows_to_push:
            try:
                _sheet_append_rows(rows_to_push)
                added += len(rows_to_push)
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
      <h1 class="text-3xl font-black tracking-tighter">Gym</h1>
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
                  style="background: rgba(99,102,241,0.20); border: 1px solid rgba(99,102,241,0.40); color: #c7d2fe;"
                  @click="copyDay(row.date)"
                  :aria-label="`Copy ${row.date}`"
                  title="複製呢一日 workout log">📋</button>
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
          <div x-show="row.exercises.length" class="text-xs text-gray-400 mt-1 truncate" x-text="row.exercises.join(' · ')"></div>
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

      <div x-show="endSummary" class="my-6">
        <div class="text-[10px] uppercase tracking-[0.2em] font-bold text-emerald-400">✓ Session Ended</div>
        <pre class="text-sm text-gray-300 whitespace-pre-wrap mt-4" x-text="endSummary?.pyramid"></pre>
        <div class="mt-4 text-2xl font-black tracking-tight" x-text="`Total ${endSummary?.total_vol_kg}kg vol`"></div>
        <button class="primary-btn w-full py-4 text-lg tap mt-6" @click="resetSession()">New Session</button>
      </div>
    </section>
  </main>

  <!-- Bottom Tab Bar — 2x2 grid (Jim OOB 2026-07-19) -->
  <nav class="fixed bottom-0 left-0 right-0 z-50 border-t border-white/10 bg-black/90 pb-[env(safe-area-inset-bottom)] backdrop-blur-2xl">
    <div class="grid grid-cols-2 grid-rows-2 gap-x-1 gap-y-1 px-2 py-1.5">
      <button class="flex items-center justify-center gap-2 rounded-lg py-1.5 transition-all" :class="tab === 'set' ? 'tab-active' : 'tab-inactive'" @click="tab = 'set'">
        <span class="text-lg leading-none">✓</span><span class="text-xs font-bold">Set</span>
      </button>
      <button class="flex items-center justify-center gap-2 rounded-lg py-1.5 transition-all" :class="tab === 'workout' ? 'tab-active' : 'tab-inactive'" @click="tab = 'workout'">
        <span class="text-lg leading-none">📊</span><span class="text-xs font-bold">Workout</span>
      </button>
      <button class="flex items-center justify-center gap-2 rounded-lg py-1.5 transition-all" :class="tab === 'history' ? 'tab-active' : 'tab-inactive'" @click="goToTab('history')">
        <span class="text-lg leading-none">📋</span><span class="text-xs font-bold">History</span>
      </button>
      <button class="flex items-center justify-center gap-2 rounded-lg py-1.5 transition-all" :class="tab === 'end' ? 'tab-active' : 'tab-inactive'" @click="tab = 'end'">
        <span class="text-lg leading-none">🏁</span><span class="text-xs font-bold">End</span>
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
      } catch(e) { /* keep empty, gradient fallback shows */ }
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
      } catch(e) { this.flash('Error: ' + e.message); }
      this.saving = false;
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
const CACHE = 'gym-web-v21';
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
self.addEventListener('controllerchange', () => {
  if (typeof window !== 'undefined') window.location.reload();
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
