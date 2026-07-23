# Changelog

All notable changes to gymbro are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [2.0.0] — 2026-07-23

### 🎉 Major Release — Stable Single-User Workout Tracking PWA

First stable major release after 22 incremental feature commits (v1 → v22 SW cache versions).
Consolidates 7 weeks of iterative development into a coherent release surface.

### Added
- **Workout Formatter module** (`workout_formatter.py`) — extracted from `gym_web.py` with multiple output formats:
  - `whoop_text` (default, AI-chat-friendly pure ASCII)
  - `whoop_emoji` (visual emphasis for human reading)
  - `whoop_text_v2` (ALL-CAPS `WORKOUT LOG / EXERCISE X OF Y / SET X OF Y` — Whoop AI ingest canonical)
  - `json` (structured dump)
  - `md` (markdown formatting)
- Per-row 📋 Copy button on history (no date-range chips; per-day granularity)
- 30-second REST cooldown on LOG SET button (`⏳ REST 30s` countdown)
- Cycle motivation image button on hero dashboard
- Apple-style icon set (180/192/512/favicon.ico)
- `/api/workout_combined` endpoint — supplements Whoop `/developer/v2/activity/workout` with Google Sheet `Workouts` tab data
- `/api/repair_sheet` endpoint — cleanup of duplicate Sheet rows
- Live `_today_images` / `_today_audio` endpoints for cheer-routine delivery
- Stepper `tap = ±1` / `hold = ±10` for weight; `tap = ±1` / `hold = ±5` for reps (Rule 7/18 default reps=10)

### Changed
- **Sync_robust_dedup**: dedup key hardened from `(date, exercise, set_n)` → `(date, exercise, set_n, time_iso)` — eliminates duplicate-row accumulation on repeated syncs (verified 7/20 + 7/21 reps)
- **Sheet number format**: Set/Reps/Weight/Volume columns enforced NUMBER type (was getting formatted as text)
- **Stepper behavior**: confirmed `+5` weight warm-up ramp removed (verified 7/19 commit `82fc8ce`)
- **Copy format default**: stripped emojis `💪📅🏋📊🎯`, `×`, `·`, `(was N)` for chat-AI token efficiency
- **PWA service worker discipline**: rigorous `gym-web-v3 → v22` cache versioning across every modification

### Codified Persistent Rules (memory + skill)
- Gym default reps = 10 (Jim OOB 2026-07-18)
- Sheet sync walkable path (Google token refresh + Sheets v4 REST API since `gws` CLI unavailable)
- Workout share 40% rate (PERSISTENT)
- Whoop pre-pipeline MANDATORY before any cheer or cron display
- Never fabricate specific numbers / names / future events (Rule 24)
- Service Worker cache bump MANDATORY on every deploy

### Fixed
- Sheet duplicate accumulation on repeated syncs (38 rows → 14 actual 7/20, 57 → 19 actual 7/21)
- `gym-web` ParseError caused by JS string literal escape sequences (Pitfall AA — fixed via ES6 template literals)
- Workouts tab `Workouts!A19:L23` 404 on fixed-range PUT (always use `values.append`)
- `vision_analyze` refusing valid JPEGs (fallback to direct MiniMax M3 curl)

### Verification (7/23)
- gym-web Flask live: port 7000 PID 800221
- `/healthz` 200 OK over HTTP
- Tailscale hostname `alonso` reachable from iPhone iOS Safari
- Cloudflared tunnel `hermes-alonso` serves Hermit on port 3010 (not 7000)
- `.whoop_workout_log.json` persists across server restarts
- Default reps=10 ✓

## [1.0.0] — 2026-06-09

### Initial Release
- Flask + Tailwind + Alpine.js Uber-look mobile-first PWA
- Workouts logging with stepper UI (weight, reps, sets)
- Google Sheet `Jim Workouts Log` integration (sheetId `1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag`)
- Whoop recovery overlay via `/api/health_overlay`
- iOS Tailscale support
