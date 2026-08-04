# Changelog

All notable changes to gymbro are documented here.

## [2.7.27] — 2026-08-04

### 🎨 Food log card redesign: default collapsed, tap to expand (Jim OOB 2026-08-04 "cards larger, text too much")

**Before** (v2.7.26): each food scan card was a small rounded-xl with food name + kcal + P + timestamp + corrections count crammed inline. Hard to scan, eye-strain with too many fields.

**After** (v2.7.27):
- Card becomes `rounded-2xl p-4` (larger).
- Default shown: image (w-20 h-20) + food NAME (large, bold) + kcal + P + time + "撳落去睇詳細" hint.
- Tap (anywhere on the card) → expands inline with P/C/F grid (4 cards side-by-side), corrections badge, and the full note.
- Uses Alpine `x-data="{ open: false }"` per card + `x-show="open"` so collapse state is independent per entry.
- Chevron arrow rotates between ▸ and ▾ based on state.

**Compatibility**: works on all scan entries (text-only, image, multi-photo). No backend changes needed — same `/api/scan_recent` payload.

**Frontend-only change**. SW cache v58 → v59.


## [2.7.26] — 2026-08-04

### 🐛 Withings step widget: paired today + yesterday (Jim OOB 2026-08-04 09:55 HKT "show both yesterday and today in widget, today larger")

v2.7.25 fixed the "yesterday-as-today" bug but lost yesterday visibility. Jim asked for a paired widget instead: TODAY (large) + YESTERDAY (small) side-by-side.

**Change**:
- Backend: `_withings_yesterday()` helper pulls yesterday's daily commit. `/api/withings_steps_today` and `/api/health_overlay` now expose both `today` and `yesterday` dicts.
- Frontend: top-bar widget now shows `今 548` (large, color-graded) + `昨 6048` (small, gray) inside a single rounded-2xl pill. Yesterday hidden if missing.
- State: `stepsYesterday` field added to Alpine state. `loadSteps()` populates it from `data.yesterday.steps`.

**Verified live 2026-08-04 09:57 HKT**:
- `/api/withings_steps_today` → `{steps: 548, date: "2026-08-04", ..., yesterday: {steps: 6048, date: "2026-08-03"}}`
- `/api/health_overlay` → `steps_today: 548, steps_yesterday: 6048`
- Widget renders today (large) + yesterday (small) side-by-side

SW cache v57 → v58.


## [2.7.25] — 2026-08-04

### 🐛 Withings step count: REVERT yesterday-fallback — TODAY only (Jim OOB 2026-08-04 09:50 HKT "wait 6048 steps was ytd, not today")

**ROOT CAUSE**: v2.7.24 "latest known truth" semantics incorrectly returned yesterday's 6048 steps as TODAY's value when today's daily commit was missing. Jim called out: "wait 6048 steps was ytd, not today".

**Why v2.7.24 was wrong**: showing yesterday's final commit (which is what iPhone Withings widget does) means gymbro conflates "Apple Watch synced yesterday" with "today's steps". User wants TODAY's running total even if 0, or honest syncing.

**FIX — v2.7.25 TODAY-only semantics**:
1. Pull 7d of `getactivity` for context.
2. **ONLY today's record is truth**. Date-strict match.
3. If today record exists → use it (or intraday if intraday > dail).
4. If today record is missing → try intraday for fresh events.
5. If intraday also empty → return `syncing: true` (honest, do NOT show yesterday's number).
6. **NEVER fall back to yesterday's value.**

**Verified live 2026-08-04 09:50 HKT after restart**:
- Apple Watch eventually committed 548 steps for 2026-08-04 (after Jim OOB)
- `/api/withings_steps_today` v2.7.25 → `{date: "2026-08-04", steps: 548, distance_km: 0.41, calories: 13.9, _source: "today_commit"}` ✅
- Earlier (before 8/4 commit) → `{steps: null, syncing: true, _source: "no_today_record"}` ✅ — no more 6048 fabrication

**Compat**: iPhone Withings widget still shows 6048 (yesterday's final), gymbro now shows 548 (today's committed). UI now matches the user's mental model: "today's real number, not yesterday's".

SW cache v56 → v57.


## [2.7.24] — 2026-08-04

### 🐛 Withings step count: complete rewrite — "latest known truth" semantics (Jim OOB 2026-08-03 23:55 HKT "step count is way too buggy, not workable. iPhone widget has latest data but gymbro syncing")

**ROOT CAUSE**: v2.7.22/2.7.23 logic only fell back to intraday when `getactivity` returned NO today record. In practice, between HKT 00:00 and ~04:00, Withings' daily commit has NOT run yet for the new day, so `getactivity` returns yesterday's record + nothing for today. The widget showed "syncing" indefinitely, even though Apple Watch via HealthKit had already pushed yesterday's complete 6048 steps (which is exactly what the iPhone Withings widget shows).

**FIX — "latest known truth" semantics**:
1. Pull 7 days of `getactivity` records (not 1d — was too narrow).
2. Find the LATEST record with steps > 0 — this is the most-recent final daily commit from Apple Watch. Return it with its actual date. This matches what the iPhone Withings widget displays.
3. Cross-check with intraday for today's events. If intraday has more steps than the chosen daily record, use intraday (partial live data).
4. If today's daily commit exists with ≥100 steps, use it (real today commit, not stale baseline).
5. NEVER return `syncing: true` when we have a recent (within 7 days) successful daily commit. Even if the chosen record is yesterday's, that IS the truth — Apple Watch simply hasn't committed today yet.
6. If the latest record is ZERO steps (genuine rest day), honor it as truth.

**Verified live 2026-08-03 23:58 HKT (after restart)**:
- Apple Watch committed 6048 steps for 2026-08-03 (HKT today)
- iPhone Withings widget shows 6048
- gymbro `/api/withings_steps_today` (v2.7.24) → `{date: "2026-08-03", steps: 6048, distance_km: 4.54, calories: 281.1, _source: "latest_truth"}` ✅
- No more "syncing" indefinitely

**Use case preserved**: when today's daily commit lands later (e.g. 04:00+), `today_steps >= 100` activates and `chosen_source` becomes "today_commit". UI seamlessly updates from "latest_truth" → "today_commit" without any flicker.

**Replaces**:
- v2.7.22 first intraday fallback (only triggered when no today record)
- v2.7.23 wake-hour low-baseline fallback (caused 16-step → syncing false positive)

SW cache v55 → v56.


## [2.7.23] — 2026-08-03

### 🐛 Withings step count: 24h window truncation + low-baseline wake-hour fallback (Jim OOB 2026-08-03 14:00 HKT "step count is wrong")

Two root causes fixed in `_withings_steps_today()` + `_get_intraday_steps_today()`:

**Root cause #1 — 24h window silent truncation**: Withings `getintradayactivity` SILENTLY TRUNCATES earlier events when the window is < 24h. Empirical proof from 2026-08-03 14:00 HKT:
- 12h window: 0 entries (real events exist 8h ago)
- 16h window: 3 entries (real events 16h+ ago)
- 24h window: 7 entries (real events 24h+ ago)
- 48h window: 99 entries (full backfill)
- 72h window: 58 entries (sliding window cuts off again)

The 24h cap documented in Withings docs is misleading. FIX: use a **48h window then filter for `ts >= hkt_midnight_ts`**. This catches all of today's events even if Apple Watch pushed them hours ago.

**Root cause #2 — low-baseline daily commit masks sync lag**: When Apple Watch only committed a baseline (e.g. 16 steps from a 04:00 sync) into `getactivity` but real events haven't pushed yet, the daily record returns 16 steps — looks like truth but it's a stale baseline. The v2.7.22 intraday fallback only fired when `getactivity` returned NO today record, so this case was bypassed entirely.

**FIX — wake-hour + 50-step threshold**: After selecting the daily `chosen` record, if HKT is in waking hours (06:00-23:00) AND `steps < 100`, force an intraday cross-check:
- If intraday has more steps → use intraday (set `_source: "intraday_override"`)
- If both daily and intraday are < 50 steps → return `syncing: true` (Rule 24 NEVER FABRICATE) — Apple Watch truly hasn't synced since yesterday

**Verified live 2026-08-03 14:05 HKT after restart**:
- `/api/withings_steps_today` → `{steps: null, syncing: true, _source: "low_baseline_no_intraday"}` ✅
- `/api/health_overlay` → `steps_today: null, steps_syncing: true` ✅
- No fabrication of 16-step baseline as "today's truth" — opposite of v2.7.22 behavior

Risk: if Jim legitimately has < 50 steps by 14:00 HKT (e.g. sick day), widget will show "同步中" instead of true count. Acceptance: the alternative (showing 16) is indistinguishable from sync lag, and a sick day is a clear excuse to NOT expedite the widget. Production-safe.

SW cache v52 → v53.


## [2.7.20] — 2026-08-01

### 🎙️ Cheer voice duration sweet-zone patch (Jim OOB 2026-08-01 22:47 HKT)

Cheer routine voice over-runs: pplx produced 1649-1793 chars → voice 330-353s (target 150-200s). Reduce by tightening prompt length rule (780-960 chars STRICT MAX 960) + per-section CAPs (greeting 50-70, recovery 110, sleep 95, training 70, nutrition 80, routine 85, preview 50-70, closing 50) + max_tokens 2400 → 1400. Verified 2 fresh fires: 1015 chars / 203s + 1117 chars / 221s — within 200-220s target band (was 353s = 42% over-budget). SW cache v50 → v51.

## [2.7.19] — 2026-07-31

### 💬 Food scan re-estimate with user hint (Jim OOB 2026-07-31 13:25 HKT)

After scan preview returns, Jim can now type supplementary info (餐廳名 / 份量 / 醬汁 / 材料) and tap "🔄 用補充資料再 estimate" to re-run pplx + APiyi nutrition enrichment with hint as additional context. Hint appears as `用家補充資料：` block in the prompt. Each round-trip can iterate — Jim can re-hint multiple times before committing. All hints persisted in `entry.user_hints[]` + Google Sheet column M ("User Hints"). Backend: new `/api/scan_re_enrich` POST endpoint (image_path + user_hint → re-enriched preview). Frontend: purple-themed hint card in scan preview with textarea, char counter, re-enrich button, and hint history chips. SW cache v49 → v50.

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
