# Changelog

All notable changes to gymbro are documented here.

## [2.2.0] — 2026-07-23

### 🎯 AI-driven personal coach pipeline (Jim OOB 2026-07-23 22:42 HKT)

Three coordinated features for Jim's daily workflow:

1. **Photostream auto-suggest** — scans today's image_cache + scan_cache files, classifies each via MiniMax vision (food vs not-food), flags photos that haven't been logged yet with 「AI log 呢張」 button for one-tap scan.
2. **Preview / confirm** — every food log now goes through a preview step (`POST /api/scan_preview` returns suggested entry, `POST /api/scan_commit` writes only after Jim taps 確認). Auto-fills kcal/P/chain from vision, Jim can edit any field, then taps 確認.
3. **Activity coach tips window** — after END SESSION, pplx sonar-pro generates per-exercise form cues + progression tips; MiniMax synthesizes into Traditional Chinese cheer-style summary (≤250 字). Cached by (date, exercises).

#### New endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/photostream/today` | GET | Today's photos, optional `?classify=true` runs MiniMax food detection |
| `/api/scan_preview` | POST | Take image → run vision + pplx → return suggested entry (NO LOG) |
| `/api/scan_preview_from_path` | POST | Same but on existing photostream image (server-side path) |
| `/api/scan_commit` | POST | Receive (possibly edited) entry → write to log + Sheet (PREVIEW-ONLY enforcement) |
| `/api/coach_tips` | POST | pplx + MiniMax for session exercises → Traditional Chinese coach cues |

#### Frontend changes

- Scan tab now shows a 3-column grid of today's photos (`今日相片 (N 建議 log)`). Tap 「AI log 呢張」 → re-runs vision → preview card appears.
- Preview card (yellow border) shows vision description + estimated macros + edit form + two buttons: 「取消」 / 「✓ 確認 log」. NO auto-log until 確認 tapped.
- END tab now auto-fetches coach tips after session ends (~60s for pplx + MiniMax). Shows loading state, then form cues + progression tips + raw pplx output (collapsible).
- All scan/photostream interactions use the same file → preview → commit pattern.

#### Defaults chosen (Jim OOB 「You decide for me」 applied consistently)

| Decision | Choice |
|---|---|
| Preview mode default | ON for ALL new scans |
| Confirm button label | `✓ 確認 log` (large emerald) |
| Cancel button label | `取消` |
| Coach tips format | Summary ≤250字 + raw pplx collapsible |
| Coach tips cache | Per (date, exercises tuple) |
| Photostream classify cache | Per (path + mtime) — re-classify only on file change |

#### Files Touched

- `gym_web.py` (5 new endpoints, photostream + preview + coach tips UI, scan flow refactored to preview-first)
- `workout_formatter.py` (version bump)
- `CHANGELOG.md` (this entry)
- Service Worker: `gym-web-v24 → v25`
- `__version__`: `2.1.0 → 2.2.0`

#### Verification (verified 2026-07-23 22:51 HKT)

- `node --check` on rendered gymApp() JS — clean (32571 chars)
- Playwright iPhone 393×852 — 10 photostream items render, scan tab loads clean
- Real preview flow: 沙嗲王 screenshot → 9 dishes parsed → suggested kcal=72 / protein=0 → Jim edits to 1850 kcal / 85P / chain=沙嗲王 → commit → Sheet `Nutrition!A10:L10` ✓ → scan_index=6 with `user_corrections: [{note: ...}]`
- Coach tips real test: pplx NSCA-CSCS perspective + MiniMax synthesis → Traditional Chinese output `### 教練總結訊息 (1) 今日表現 (2) 動作 Cue (3) 下次 Progression Tip`
- Cached on second call (avoid re-running pplx + MiniMax)

---

### 🍽️ Food Scan Feature — PWA-side camera + MiniMax M3 vision + pplx enrichment (Jim OOB 2026-07-23 22:26 HKT)

First mini-minor feature release. Jim OOB：「Version will be able to scan food or food receipt to capture. Using MiniMax image recognition and pplx search」.

#### Added

- **3 new API endpoints**:
  - `POST /api/scan_food` — Receive iPhone camera / file-picker image, runs MiniMax M3 vision for dish/portion/chain detection, pplx sonar-pro for brand-specific nutrition enrichment, applies 60/40 share silently if shared dish detected, appends to `nutrition_log.json[meals]` and mirrors to Google Sheet `Nutrition` tab.
  - `GET /api/scan_recent?limit=N` — Last N scans with thumbnail (for dashboard overlay).
  - `POST /api/scan_correct` — Jim corrections re-feed back; appended as `user_corrections[]` to scan entry. **NO TRIMMING** — corrections permanent (Jim OOB 2026-07-23 22:30 HKT).
  - `GET /scan_img/&lt;filename&gt;` — serve scanned images from `/home/work/.hermes/scan_cache/`.

- **Scan cache directory**: `/home/work/.hermes/scan_cache/` (auto-created)
- **Scan log**: `/home/work/.hermes/food_scan_log.json` (per-scan index + thumbnail)
- **Hidden file input with `capture="environment"`** — iOS Safari opens camera directly, file picker fallback if camera denied.

#### Frontend UI (3x2 bottom-nav)

- Added `🍽️ Scan` tab (emerald-tinged) + `📷 鏡頭` quick-trigger button
- Big tap-to-scan card with progress bar + upload state
- Last scan summary card with 60/40 share indicator + correction form (✏️ collapsed details)
- Recent 5 scans strip with thumbnail + macros + edit-count badge
- Auto-loads scan history on page init

#### NO TRIMMING Guarantee (Jim OOB 2026-07-23 22:30 HKT)

All `user_corrections[]` entries are **permanent** — no cron trims, no age expiry, no retention policy. Corrections accumulate indefinitely to support model retraining + Jim's audit needs.

#### Defaults chosen (Jim OOB 「You decide for me」 2026-07-23 22:35 HKT)

| Open question | Decision |
|---|---|
| UI placement | **Both** — Scan tab + Hero quick-trigger `📷 鏡頭` |
| iOS camera permission fallback | **Auto** — `<input capture="environment">` natively handles: grants → camera, denies → file picker |
| Pplx enrichment prompt | **Single-pass** — focus on chain/brand lookup + per-dish standard portion |
| Correction retention | **Permanent** (NO TRIMMING) — Jim OOB explicit |

#### Files Touched

- `gym_web.py` (4 new endpoints, scan section HTML, scan Alpine state, scan methods in gymApp)
- `workout_formatter.py` (version bump comment)
- `CHANGELOG.md` (this entry)
- Service Worker cache: `gym-web-v23 → v24`

#### Verification

- `node --check` on rendered HTML gymApp() JS — clean
- Playwright headless iPhone viewport 393×852 — all 6 nav tabs render, scan tab opens, file input renders with `capture="environment"`
- Real scan test with 沙嗲王 online-order screenshot → vision parsed 9 dishes, pplx enrichment returned brand-detection honesty, shared=True heuristic fired, `Nutrition!A8:L8` row appended to Sheet, scan_index=4 incremented, image saved to `scan_cache/scan_20260723_*.jpg`

---

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
