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
  }
  * { -webkit-tap-highlight-color: transparent; }
  html, body { background: var(--uber-black); color: var(--uber-text); font-family: 'Inter', -apple-system, system-ui, sans-serif; overscroll-behavior: none; }
  body { padding-top: env(safe-area-inset-top); padding-bottom: env(safe-area-inset-bottom); }
  .tap { transition: transform 0.08s ease-out, background-color 0.15s; }
  .tap:active { transform: scale(0.97); }
  .primary-btn { background: var(--uber-text); color: var(--uber-black); border-radius: 4px; font-weight: 700; letter-spacing: 0.5px; }
  .pill { border-radius: 999px; }
  input[type="text"], input[type="number"] { background: var(--uber-grey-6); border: 0; padding: 14px 16px; border-radius: 4px; font-size: 16px; width: 100%; outline: none; }
  input[type="text"]:focus { background: var(--uber-grey-2); }
  .tab-active { color: var(--uber-text); border-bottom: 2px solid var(--uber-text); }
  .tab-inactive { color: var(--uber-grey-4); border-bottom: 2px solid transparent; }
  .num-btn { background: var(--uber-grey-6); color: var(--uber-black); border-radius: 4px; font-weight: 700; font-size: 24px; }
  .num-btn:active { background: var(--uber-grey-2); }
  .pyramid { display: flex; flex-direction: column; align-items: center; gap: 2px; }
  .pyramid-row { background: var(--uber-text); color: var(--uber-black); border-radius: 4px; padding: 8px 14px; font-weight: 600; min-width: 120px; text-align: center; }
  .pyramid-row.warm-up { opacity: 0.55; }
  .pyramid-row.working { font-weight: 900; }
  .pyramid-row.burn-out { background: var(--uber-green); color: var(--uber-text); }
  .hidden { display: none !important; }
  @keyframes pulse-fade { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
  .saving { animation: pulse-fade 1.2s ease-in-out infinite; }
  .toast {
    position: fixed; bottom: 100px; left: 50%; transform: translateX(-50%);
    background: var(--uber-text); color: var(--uber-black); padding: 12px 24px;
    border-radius: 24px; font-weight: 600; box-shadow: 0 4px 12px rgba(0,0,0,0.4);
    z-index: 100; transition: opacity 0.3s;
  }
</style>
</head>
<body x-data="gymApp()" x-init="init()">

  <!-- Top Bar -->
  <header class="sticky top-0 z-50 bg-black px-4 pt-3 pb-2">
    <div class="flex items-baseline justify-between">
      <h1 class="text-3xl font-black tracking-tight">Gym</h1>
      <span class="text-xs text-gray-400" x-text="sessionDateStr"></span>
    </div>
    <div class="mt-2 flex items-center gap-2">
      <span class="text-2xl font-black" x-text="currentExercise || '—'"></span>
      <span class="text-sm text-gray-400" x-text="currentSet ? `Set ${currentSet.set}/${currentSet.total}` : 'Tap to start'"></span>
    </div>
  </header>

  <!-- Toast -->
  <div class="toast" x-show="toast" x-text="toast" x-transition.opacity></div>

  <!-- Tab Content -->
  <main class="px-4 pt-4 pb-32 min-h-screen">

    <!-- SET TAB (default) -->
    <section x-show="tab === 'set'">
      <div class="text-center my-6">
        <div class="text-sm uppercase tracking-widest text-gray-400 mb-1">Current</div>
        <div class="text-6xl font-black my-2" x-text="displayWeight || '—'"></div>
        <div class="text-2xl text-gray-300 font-medium" x-text="displayReps ? `${displayReps} reps` : '—'"></div>
        <div class="mt-1 text-xs uppercase tracking-widest" :class="intensityColor" x-text="intensityLabel"></div>
      </div>

      <!-- Exercise name input (only if no exercise yet) -->
      <div x-show="!currentExercise" class="my-6">
        <label class="block text-sm uppercase tracking-widest text-gray-400 mb-2">Exercise</label>
        <div class="grid grid-cols-2 gap-2 mb-3">
          <template x-for="ex in quickPicks" :key="ex">
            <button class="pill bg-gray-100 text-black font-semibold py-3 tap" @click="pickExercise(ex)" x-text="ex"></button>
          </template>
        </div>
        <input type="text" placeholder="或輸入 custom" x-model="exerciseInput" @keyup.enter="customExercise()" />
      </div>

      <!-- Weight stepper -->
      <div x-show="currentExercise" class="my-4">
        <div class="text-sm uppercase tracking-widest text-gray-400 mb-2 text-center">Weight (kg)</div>
        <div class="flex items-center justify-between gap-3">
          <button class="num-btn w-14 h-14 tap" @click="bumpWeight(-5)">−5</button>
          <button class="num-btn w-14 h-14 tap" @click="bumpWeight(-2.5)">−2.5</button>
          <div class="flex-1 text-center text-4xl font-black" x-text="weight"></div>
          <button class="num-btn w-14 h-14 tap" @click="bumpWeight(2.5)">+2.5</button>
          <button class="num-btn w-14 h-14 tap" @click="bumpWeight(5)">+5</button>
        </div>
      </div>

      <!-- Reps stepper -->
      <div x-show="currentExercise" class="my-4">
        <div class="text-sm uppercase tracking-widest text-gray-400 mb-2 text-center">Reps (default 10)</div>
        <div class="flex items-center justify-between gap-3">
          <button class="num-btn w-14 h-14 tap" @click="bumpReps(-2)">−2</button>
          <button class="num-btn w-14 h-14 tap" @click="bumpReps(-1)">−1</button>
          <div class="flex-1 text-center text-4xl font-black" x-text="reps"></div>
          <button class="num-btn w-14 h-14 tap" @click="bumpReps(1)">+1</button>
          <button class="num-btn w-14 h-14 tap" @click="bumpReps(2)">+2</button>
        </div>
      </div>

      <!-- Big LOG button -->
      <div x-show="currentExercise" class="mt-8">
        <button class="primary-btn w-full py-6 text-2xl tap" :class="{'saving': saving}" @click="logSet()" x-text="saving ? 'Saving…' : `✓ Log Set ${currentSet ? currentSet.set : 1}`"></button>
      </div>

      <!-- Quick intensity tags -->
      <div x-show="currentExercise && hasWorkedAtLeastOneSet" class="mt-4 flex gap-2 justify-center">
        <button class="pill bg-gray-700 px-4 py-2 text-sm tap" @click="setIntensity('working')">🎯 Working</button>
        <button class="pill bg-gray-700 px-4 py-2 text-sm tap" @click="setIntensity('burn-out')">🔥 Burn-out</button>
        <button class="pill bg-gray-700 px-4 py-2 text-sm tap" @click="setIntensity('drop-set')">⚡ Drop</button>
        <button class="pill bg-gray-700 px-4 py-2 text-sm tap" @click="markPartial()">⚠️ Partial</button>
      </div>

      <!-- Same as last set -->
      <div x-show="currentExercise && lastSetForExercise" class="mt-3 text-center">
        <button class="text-sm text-gray-400 underline tap" @click="cloneLastSet()" x-text="`⬇ 上一組同 spec (${lastSetForExercise.weight_kg}kg × ${lastSetForExercise.reps})`"></button>
      </div>
    </section>

    <!-- WORKOUT / PYRAMID TAB -->
    <section x-show="tab === 'workout'">
      <div class="text-sm uppercase tracking-widest text-gray-400 my-2">Today's Pyramid</div>
      <template x-for="(ex, idx) in sessionGrouped" :key="ex.name">
        <div class="mb-6">
          <div class="text-xl font-bold mb-2" x-text="ex.name"></div>
          <div class="pyramid">
            <template x-for="entry in ex.entries" :key="entry.set">
              <div class="pyramid-row"
                   :class="entry.intensity"
                   x-text="`Set ${entry.set} · ${entry.weight_kg}kg × ${entry.reps} reps`">
              </div>
            </template>
          </div>
          <div class="text-xs text-gray-400 mt-2" x-text="`Sub-total: ${ex.vol}kg vol`"></div>
        </div>
      </template>
      <div x-show="!sessionGrouped.length" class="text-gray-500 text-center py-20">No sets logged yet</div>
    </section>

    <!-- HISTORY TAB -->
    <section x-show="tab === 'history'">
      <div class="text-sm uppercase tracking-widest text-gray-400 my-2">Recent Sessions</div>
      <div class="text-gray-500 text-center py-20">Pull previous days from /home/work/.whoop_workout_log.json</div>
    </section>

    <!-- END TAB -->
    <section x-show="tab === 'end'" x-cloak>
      <div class="text-center my-6">
        <div class="text-sm uppercase tracking-widest text-gray-400">End Session</div>
        <h2 class="text-3xl font-black mt-2">收檔時間</h2>
        <p class="text-gray-400 mt-2">收尾寫入 Google Sheet + Whoop log</p>
      </div>

      <div x-show="!endSummary">
        <div class="my-6">
          <label class="text-sm uppercase tracking-widest text-gray-400 mb-2 block">RPE (1-10)</label>
          <input type="number" min="1" max="10" placeholder="例: 7" x-model.number="endRPE" />
        </div>
        <button class="primary-btn w-full py-6 text-2xl tap mt-8" @click="endSession()" :class="{'saving': saving}">🏁 END SESSION</button>
        <div class="text-xs text-gray-500 text-center mt-3">Telegram 同步 ON by default (Jim 7/19 config)</div>
      </div>

      <div x-show="endSummary" class="my-6">
        <div class="text-sm uppercase tracking-widest text-green-400">✓ Session Ended</div>
        <pre class="text-sm text-gray-300 whitespace-pre-wrap mt-4" x-text="endSummary?.pyramid"></pre>
        <div class="mt-4 text-2xl font-black" x-text="`Total ${endSummary?.total_vol_kg}kg vol`"></div>
        <button class="primary-btn w-full py-4 text-lg tap mt-6" @click="resetSession()">New Session</button>
      </div>
    </section>
  </main>

  <!-- Bottom Tab Bar -->
  <nav class="fixed bottom-0 left-0 right-0 bg-black border-t border-gray-700 z-50" style="padding-bottom: env(safe-area-inset-bottom);">
    <div class="flex justify-around">
      <button class="flex-1 py-3 tab-active" :class="tab === 'set' ? 'tab-active' : 'tab-inactive'" @click="tab = 'set'">
        <div class="text-2xl font-black">✓</div>
        <div class="text-xs mt-0.5">Set</div>
      </button>
      <button class="flex-1 py-3" :class="tab === 'workout' ? 'tab-active' : 'tab-inactive'" @click="tab = 'workout'">
        <div class="text-2xl font-black">📊</div>
        <div class="text-xs mt-0.5">Workout</div>
      </button>
      <button class="flex-1 py-3" :class="tab === 'history' ? 'tab-active' : 'tab-inactive'" @click="tab = 'history'">
        <div class="text-2xl font-black">📋</div>
        <div class="text-xs mt-0.5">History</div>
      </button>
      <button class="flex-1 py-3" :class="tab === 'end' ? 'tab-active' : 'tab-inactive'" @click="tab = 'end'">
        <div class="text-2xl font-black">🏁</div>
        <div class="text-xs mt-0.5">End</div>
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
      this.haptic();
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
      return { 'warm-up': 'text-gray-400', 'working': 'text-white', 'burn-out': 'text-green-400', 'drop-set': 'text-yellow-400' }[this.intensity] || 'text-gray-400';
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
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
</script>

</body>
</html>
"""


# ---------- Service worker for PWA ----------
SERVICE_WORKER = """
const CACHE = 'gym-web-v1';
self.addEventListener('install', e => self.skipWaiting());
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
