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

# ---------- Constants ----------
WORKOUT_LOG = Path("/home/work/.whoop_workout_log.json")
HKT = timezone(timedelta(hours=8))
PORT = 7000
HOST = "0.0.0.0"

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
    """Return today's daily motivation image (or None if not yet generated)."""
    today = today_iso()
    img_path = Path("/home/work/.hermes/image_cache") / f"gymbro_{today}.png"
    if img_path.exists() and img_path.stat().st_size > 50000:
        return jsonify({"image_url": f"/img/gymbro_{today}.png", "date": today})
    return jsonify({"image_url": None, "date": today})


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
    """Latest Whoop recovery score (single number 0-100, or None)."""
    d = _safe_read_json(WHOOP_CACHE)
    recs = d.get("recovery", []) if isinstance(d, dict) else []
    for r in recs:
        score = (r.get("score") or {})
        val = score.get("recovery_score")
        if val is not None and r.get("score_state") == "SCORED":
            return int(round(float(val)))
    return None


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
    """Delete a past date's session from WORKOUT_LOG. Refuses to delete today."""
    data = request.get_json(force=True)
    date = (data.get("date") or "").strip()
    if not date:
        return jsonify({"error": "date required"}), 400
    if date == today_iso():
        return jsonify({"error": "cannot delete today — use cancel button"}), 400
    log = load_log()
    if date not in log:
        return jsonify({"error": f"date {date} not found"}), 404
    del log[date]
    save_log(log)
    return jsonify({"ok": True, "deleted": date})


@app.route("/img/<path:filename>")
def serve_image(filename):
    return send_from_directory("/home/work/.hermes/image_cache", filename)


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
    box-shadow: 0 0 12px rgba(255,255,255,0.4);
    border-top: 2px solid var(--uber-text);
  }
  .tab-inactive { color: var(--uber-grey-4); }
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
          <div class="min-w-0 pr-24">
            <div class="text-[9px] uppercase tracking-[0.2em] text-gray-300">Today</div>
            <div class="quote-line line-clamp-2 text-sm font-medium" x-text="quote"></div>
          </div>
        </div>
        <div x-show="streak > 0" class="streak-badge absolute right-2 top-2 z-20 shadow-lg shadow-black/40">
          <span class="text-yellow-300">🔥</span>
          <span x-text="`${streak} day${streak === 1 ? '' : 's'}`"></span>
        </div>
        <!-- Top-left: Whoop recovery % (single number, minimal) -->
        <div x-show="recovery !== null" class="absolute left-2 top-2 z-20 flex items-center gap-1 rounded-full border border-white/15 bg-black/55 px-2 py-0.5 text-[10px] font-bold text-emerald-300 backdrop-blur">
          <span>💚</span><span x-text="`${recovery}%`"></span>
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
      </div>

      <!-- Current set: exercise + weight + reps + intensity in one compact row -->
      <div x-show="currentExercise" class="glass mb-2 flex h-16 items-center gap-3 rounded-2xl px-3 shadow-lg shadow-black/20">
        <div class="min-w-0 flex-1">
          <div class="truncate text-base font-black tracking-tight" x-text="currentExercise"></div>
          <div class="mt-0.5 text-xs text-gray-400" x-text="currentSet ? `Set ${currentSet.set}` : 'Set 1'"></div>
        </div>
        <div class="whitespace-nowrap text-xl font-black tracking-tight" x-text="displayWeight"></div>
        <div class="whitespace-nowrap text-base font-bold text-gray-300" x-text="`${displayReps}×`"></div>
        <div class="rounded-full border border-white/10 bg-white/5 px-2 py-1 text-[9px] font-bold uppercase tracking-wider" :class="intensityColor" x-text="intensityLabel"></div>
      </div>

      <!-- Exercise name input (only if no exercise yet) -->
      <div x-show="!currentExercise" class="mt-1">
        <div class="mb-2 text-center text-sm font-semibold text-gray-400">Choose an exercise</div>
        <div class="mb-2 grid grid-cols-3 gap-2">
          <template x-for="(ex, idx) in quickPicks" :key="ex">
            <button class="tap fade-up min-w-0 truncate rounded-xl border border-white/15 bg-white/[0.08] px-2 py-2 text-sm font-semibold backdrop-blur"
                    :style="`animation-delay: ${idx * 50}ms`"
                    @click="pickExercise(ex)" x-text="ex"></button>
          </template>
        </div>
        <input class="!py-2.5 text-base" type="text" placeholder="或輸入 custom" x-model="exerciseInput" @keyup.enter="customExercise()" />
      </div>

      <!-- Weight + reps steppers share one 80px row. Tap is fine control; hold is coarse control. -->
      <div x-show="currentExercise" class="mb-2 grid h-20 grid-cols-2 gap-2">
        <div class="glass grid grid-cols-[2.5rem_1fr_2.5rem] items-center rounded-2xl p-1.5">
          <button class="tap flex h-10 w-10 flex-col items-center justify-center rounded-full bg-white/10 font-bold"
                  @pointerdown.prevent="startStep('weight', -1)" @pointerup.prevent="endStep('weight', -1)"
                  @pointerleave="cancelStep()" @pointercancel="cancelStep()">
            <span class="text-base leading-none">−3</span><span class="mt-0.5 text-[8px] text-gray-400">hold −5</span>
          </button>
          <div class="min-w-0 text-center">
            <span class="text-3xl font-black tracking-tighter" x-text="weight"></span><span class="ml-0.5 text-xs text-gray-400">kg</span>
          </div>
          <button class="tap flex h-10 w-10 flex-col items-center justify-center rounded-full bg-white/10 font-bold"
                  @pointerdown.prevent="startStep('weight', 1)" @pointerup.prevent="endStep('weight', 1)"
                  @pointerleave="cancelStep()" @pointercancel="cancelStep()">
            <span class="text-base leading-none">+3</span><span class="mt-0.5 text-[8px] text-gray-400">hold +5</span>
          </button>
        </div>
        <div class="glass grid grid-cols-[2.5rem_1fr_2.5rem] items-center rounded-2xl p-1.5">
          <button class="tap flex h-10 w-10 flex-col items-center justify-center rounded-full bg-white/10 font-bold"
                  @pointerdown.prevent="startStep('reps', -1)" @pointerup.prevent="endStep('reps', -1)"
                  @pointerleave="cancelStep()" @pointercancel="cancelStep()">
            <span class="text-base leading-none">−3</span><span class="mt-0.5 text-[8px] text-gray-400">hold −5</span>
          </button>
          <div class="min-w-0 text-center">
            <span class="text-3xl font-black tracking-tighter" x-text="reps"></span><span class="ml-0.5 text-xs text-gray-400">×</span>
          </div>
          <button class="tap flex h-10 w-10 flex-col items-center justify-center rounded-full bg-white/10 font-bold"
                  @pointerdown.prevent="startStep('reps', 1)" @pointerup.prevent="endStep('reps', 1)"
                  @pointerleave="cancelStep()" @pointercancel="cancelStep()">
            <span class="text-base leading-none">+3</span><span class="mt-0.5 text-[8px] text-gray-400">hold +5</span>
          </button>
        </div>
      </div>

      <!-- Sticky action dock: always ends above the fixed 64px tab bar. -->
      <div x-show="currentExercise" class="sticky bottom-[72px] z-40 mt-auto pb-2 pt-2">
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
                  :class="{'saving': saving}" @click="logSet()"
                  x-text="saving ? 'Saving…' : `✓ LOG SET ${currentSet ? currentSet.set : 1}`">
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
        <button class="text-xs text-gray-400 underline tap" @click="loadHistory()" x-show="!loadingHistory">↻ Refresh</button>
        <span class="text-xs text-gray-400" x-show="loadingHistory">Loading…</span>
      </div>
      <div x-show="loadingHistory && history.length === 0" class="text-gray-500 text-center py-12">Loading history…</div>
      <div x-show="!loadingHistory && history.length === 0" class="text-gray-500 text-center py-12">No sessions yet — go log some 🔥</div>
      <template x-for="row in history" :key="row.date">
        <div class="bg-white/5 backdrop-blur border border-white/10 rounded-2xl p-4 mb-3 relative fade-up"
             :class="row.date === today ? 'ring-2 ring-yellow-400/50' : ''">
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

  <!-- Bottom Tab Bar -->
  <nav class="fixed bottom-0 left-0 right-0 z-50 border-t border-white/10 bg-black/90 pb-[env(safe-area-inset-bottom)] backdrop-blur-2xl">
    <div class="flex h-16 justify-around">
      <button class="flex-1 py-2" :class="tab === 'set' ? 'tab-active' : 'tab-inactive'" @click="tab = 'set'">
        <div class="text-lg font-black leading-5">✓</div>
        <div class="mt-0.5 text-[11px]">Set</div>
      </button>
      <button class="flex-1 py-2" :class="tab === 'workout' ? 'tab-active' : 'tab-inactive'" @click="tab = 'workout'">
        <div class="text-lg font-black leading-5">📊</div>
        <div class="mt-0.5 text-[11px]">Workout</div>
      </button>
      <button class="flex-1 py-2" :class="tab === 'history' ? 'tab-active' : 'tab-inactive'" @click="tab = 'history'">
        <div class="text-lg font-black leading-5">📋</div>
        <div class="mt-0.5 text-[11px]">History</div>
      </button>
      <button class="flex-1 py-2" :class="tab === 'end' ? 'tab-active' : 'tab-inactive'" @click="tab = 'end'">
        <div class="text-lg font-black leading-5">🏁</div>
        <div class="mt-0.5 text-[11px]">End</div>
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
    pressTimer: null,
    pressHandled: false,
    quote: '努力唔會辜負你',
    quoteBank: ['努力唔會辜負你', '今日破 PR!', '肌肉記得晒', '每次一公斤', '收檔先贏', '慢慢嚟', '穩住', '加油'],
    quickPicks: ['BB Bench Press','Leg Press','Low Row (Cable)','DB OHP','DB Shoulder Raise','Lat Pulldown','Squat'],

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
      // Pull today's motivation image (non-blocking)
      try {
        const imgRes = await fetch('/api/today_image');
        const imgData = await imgRes.json();
        if (imgData && imgData.image_url) {
          this.motivationImage = imgData.image_url;
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
      // Initial warm-up if first time
      const prev = this.session.exercises.filter(e => e.exercise === name);
      if (!prev.length) {
        this.weight = 20;
        this.reps = 10;
        this.intensity = 'warm-up';
      } else {
        const last = prev[prev.length - 1];
        this.weight = (last.weight_kg || 20) + 5;  // warm-up ramp
        this.reps = 10;
        this.intensity = prev.length < 2 ? 'warm-up' : (prev.length < 4 ? 'working' : 'burn-out');
      }
      this.haptic();
      this.flash(`Exercise: ${name}`);
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
        if (kind === 'weight') this.bumpWeight(direction * 5);
        else this.bumpReps(direction * 5);
        this.pressHandled = true;
        this.pressTimer = null;
      }, 500);
    },

    endStep(kind, direction) {
      if (this.pressTimer) clearTimeout(this.pressTimer);
      if (!this.pressHandled) {
        if (kind === 'weight') this.bumpWeight(direction * 3);
        else this.bumpReps(direction * 3);
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

    async logSet() {
      if (!this.currentExercise) return;
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
          // Auto-advance: warm-up ramp +5 kg, then maintain
          const sets = this.session.exercises.filter(e => e.exercise === this.currentExercise);
          const last = sets[sets.length - 1];
          if (sets.length < 3) {
            this.weight = +(this.weight + 2.5).toFixed(1);
            this.intensity = sets.length < 2 ? 'warm-up' : 'working';
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

    async loadHistory() {
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
        this.flash('Session ended ✓');
      } catch(e) { this.flash('Error: ' + e.message); }
      this.saving = false;
    },

    async resetSession() {
      this.endSummary = null;
      this.currentExercise = '';
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
const CACHE = 'gym-web-v3';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  e.respondWith(
    caches.open(CACHE).then(cache =>
      cache.match(e.request).then(cached => cached || fetch(e.request).then(res => {
        if (e.request.method === 'GET' && res.ok) cache.put(e.request, res.clone());
        return res;
      }).catch(() => cached))
    )
  );
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
