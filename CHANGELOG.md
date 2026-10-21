## v3.3.31 — 2026-10-21 — the "as of" time was lying; header clock used the device timezone

Jim OOB 2026-10-21:「In gymbro, the as of time is incorrect. This is showing
08:14 but should be at 11am」

### Root cause 1 — WHOOP's `updated_at` does not track the step count
The steps "as of HH:MM" stamp used the Whoop cycle record's `updated_at`.
Three live pulls on 2026-10-21:

```
11:22 -> step_count 1042, updated_at 08:14:53
11:45 -> step_count 1159, updated_at 08:14:53
11:46 -> step_count 1342, updated_at 08:14:53
```

The number was advancing in real time while `updated_at` stayed frozen — and
`recovery` / `activity/sleep` carried the *same* frozen stamp, so it is a
record-level stamp, not a "band last wrote" stamp. The app therefore claimed
the count was 3 hours old when it was seconds old.

**Fix**: `data_as_of` is now WHEN WE READ THE NUMBER — the cache file's mtime
(i.e. gymbro's last successful pull). The source stamp is kept as
`source_updated_at` (+ `source_age_min`) and still drives the band-off
heuristic. `/api/whoop_steps` exposes the new fields.

### Root cause 2 — the tap flash contradicted the stamp
Tapping the shoes widget flashed「✓ WHOOP 已更新 (11:20)」— 11:20 being the
*PULL* time — while the stamp right below read「as of 08:14」— the *DATA AGE*.
Two different meanings, side by side, so the time looked broken.

**Fix**: the flash now reports the truth after the pull:
「WHOOP 已拉,但雲端仲係 3小時30分前(手機未同步)」. When the data is fresh it
shows the source time, not the pull time.

### Root cause 3 — the header clock used the device timezone
`tickClock()` used `d.getHours()`, i.e. the *browser's* timezone. On a device
not set to HKT the header clock showed the wrong time entirely (a UTC browser
showed 03:22 for 11:22 HKT). The same pattern put the food log's「今日/昨日」
grouping and the schedule month picker on the device clock.

**Fix**: all of them now derive from `Asia/Hong_Kong` via `Intl.DateTimeFormat`,
plus a shared `hkTodayAndYesterday()` helper.

### Also
- The "as of" label now prints the data AGE (`as of 08:14 ⚠ 3小時29分前`) so a
  bare ⚠ can't be misread as "the app is showing the wrong time".
- `loadSteps()` silently fires a background WHOOP pull when the cache is older
  than 5 min, so the stamp tracks "now" instead of the 45-min cron cadence.
- `whoop_nutrition.py` now writes the cache ATOMICALLY (tmp + `os.replace`).
  The cache has three concurrent writers (45-min refresh cron, 9am sync cron,
  app tap-to-refresh); the old `open(path,'w')` truncated first, so a reader
  could see half-written JSON and the app would briefly show NO steps at all.

### Verified
- `/api/whoop_steps`: `data_as_of 11:46:09`, `source_updated_at 08:14:53`,
  `age_min 15`, `stale false`
- App header: `👟 1,378 as of 12:02 WHOOP 5.5K 12:03 10-21` (no ⚠)
- QA 10/10 PASS

## v3.3.30 — 2026-10-21 — APIyI removed + Rule 25 generic-name hard block

Jim OOB 2026-10-21:「Remove apiyi stuff」

### Why the removal was total
The APIyI key was probed on every endpoint and returned **HTTP 401
"Invalid token"** on all of them — gpt-4o, gpt-4o-mini, vip.apiyi.com,
vision, and audio/transcriptions. Not just the vision path: the whole key
was dead, including the voice transcription path.

### Removed / renamed
- Vision provider pool → `minimax-text-01` → `pplx sonar` (2 providers)
- Deleted `_apiyi_vision_gpt4o`, `_apiyi_api_key`
- `_apiyi_vision_analyze` → `_vision_second_opinion`
- `_apiyi_nutrition_enrich[_multi]` → `_minimax_nutrition_enrich[_multi]`
  (these already called MiniMax internally — the name was simply wrong)
- Variables: `apiyi_desc/parsed/text_desc/name_raw/enrichment/json_parsed/
  vision_desc` → `mm_*` / `minimax_*` / `opinion2_desc`
- `gym_web.py` now has **0** `apiyi` references; `models_used` and `source`
  labels cleaned; 2 template comments updated

### Voice transcription → local faster-whisper
`/api/voice/transcribe` used to call `audio/transcriptions` (routed through
the dead APIyI key). Now uses `_local_whisper_transcribe()`.

Three pitfalls, all verified:
- `av` must be `==15.1.0` (faster-whisper 1.2.1 needs `metadata_errors`;
  av ≥ 16 removed it → TypeError)
- `vad_filter=True` is mandatory (without it, sparse/silent audio returns
  garbage)
- `language='yue'` returns **ZERO segments** on the `small` checkpoint →
  use `'zh'` with a Cantonese-bias `initial_prompt`; output is Simplified,
  so it is converted to Traditional with opencc `s2twp`

Verified over HTTP: `ok=True`, `model=faster-whisper-small`, Cantonese output.

### Rule 25 hard block — the hole v3.3.25 left open
While running the provider-chain end-to-end test repeatedly, one run in
four produced「未識別菜式」**with a perfectly healthy vision provider** —
the description and macros were there, only the *name* extraction failed.
v3.3.25 only blocked "all providers dead", so that case still wrote a
placeholder name into Jim's golden copy — the exact signature of the
2026-10-20 pollution incident (26 rows + 7 rows of junk).

Now, in `_scan_preview_core`, **before** the Drive upload:
1. `_GENERIC_DISH_NAMES_BLOCKLIST` holds the placeholder names
2. Every dish generic → `ok:false`, zero Drive file, zero Sheet row, and the
   same top-level key set as the success path (Isak §4.3 contract)
3. Only some generic → those entries are dropped, the named ones commit
4. The frontend shows `status='failed'` and tells Jim the photo was not
   logged instead of pretending it was committed

Verified: forcing the name extractor to return「未識別菜式」→ fully blocked,
zero writes, key set identical; mixed names → the placeholder is dropped and
the real name is kept.

### QA
Suite is now **10 groups, all PASS** (new `rule25` group). The `vision` and
`aifallback` groups were updated for the removal — the stubbed symbols were
renamed, the test semantics are unchanged.

Production check: 2026-10-20 = 3 rows (Jim's real dinner), 2026-10-21 = **0
rows**,「未識別菜式」= 0 rows, Drive files created on/after 2026-10-21 = **0**.


## v3.3.26 — 2026-10-21 — WHOOP is the ONLY health source (Withings fully retired)

Jim OOB 2026-10-20:「Fully remove withings / Steps is whoop!」→ then
「I mean weight. If whoop has weight data. Remove all withings」

**The premise check that changed the plan:** WHOOP's body endpoint was recorded
as "404 / unavailable" in the pipeline doc — but that note tested the WRONG path.
Live verification 2026-10-21 found it:

```
GET /developer/v2/user/measurement/body      (scope read:body_measurement)
→ {"height_meter":1.778, "weight_kilogram":78.107, "max_heart_rate":182}
```

78.107 kg vs the Withings scale's 78.11 kg — same number, so the condition
Jim set ("if WHOOP has weight data") IS met and Withings can go.

### Removed
- `gym_web/withings.py` module (git rm)
- 6 Withings body helpers → replaced by `_body_latest/_body_weight/_body_fat_pct/
  _body_muscle_kg/_body_bone_kg/_body_hydration_pct` (WHOOP-backed)
- `/api/withings_steps_today`, `/api/withings_steps_7d_avg`, `/api/withings_refresh` → 404
- Withings pull from the cheer pipeline (`WITHINGS_PULL_SCRIPT` subprocess)
- MCP `withings_refresh` tool → `whoop_refresh`
- MCP payload keys `withings_weight_kg` / `withings_fat_pct` / `withings_weight_date` →
  `weight_kg` / `weight_source` / `height_m` / `bmi` / `fat_pct` / `muscle_kg` / `bone_kg` / `hydration_pct`
- `withings-token-refresh` + `Withings 30min refresh` cron jobs
- `~/.withings_latest_cache.json`, and the `withings` skill → `.archive/withings`

### Added
- `/developer/v2/user/measurement/body` in the WHOOP pull script (5 → **6/6 endpoints OK**)
- `whoop_get_body()` in `gym_web/whoop.py` → weight / height / BMI
- `whoop_get_steps()` → cycle `step_count`, in-progress cycle as today, yesterday,
  7-day average, series, `stale` / `band_off` / `in_progress` freshness
- `/api/whoop_steps` endpoint
- Front-end: WHOOP source badge, band-off warning, WHOOP tap-to-refresh

### Fixed (real bugs found on the way)
1. **Cache clobber** — `whoop_nutrition.py --sync` and `whoop_pull_inline.py` both
   write `~/.whoop_data_latest.json`; the nutrition sync wrote only 5 keys and
   silently DROPPED `body_measurement`, so the weight vanished every 9am cron.
   Both writers now MERGE.
2. **UTC/HKT day bug** — cycle `start` is UTC; slicing it gave the wrong day
   (up to 8 h off). Now converted to HKT, matching `whoop_get_steps()`.
3. **Redis cache leak into the test suite** — `/api/health_overlay` caches its
   payload in a process-wide Redis key SHARED WITH THE LIVE SERVER, so tests
   read production weight/recovery. Added an autouse conftest fixture that
   clears the key around every test.
4. **Timezone fallback bug** — `_date_of` referenced an un-imported name, so the
   HKT conversion always raised and silently fell back to the wrong UTC slice.

### Truthfulness rules now enforced
- `cycles[0]` (`end: null`) is TODAY's running total — never a previous day
- `updated_at` drives the `stale` / `band_off` flags; a stale record is shown
  as last-known, never as live
- When WHOOP has no step count the UI shows `—` + `steps_syncing`, never a
  Withings number and never yesterday's number

### Known gap
WHOOP has **no bioimpedance sensor** — fat% / muscle / bone / hydration have
no source now that the Withings BIA scale is retired. The UI renders `—`
rather than a fabricated 0.

# Changelog

All notable changes to gymbro are documented here.

## [3.3.15] — 2026-09-04

### 🎙️ Cheer voice: switch back to Edge-TTS WanLungNeural (雲龍) — Jim preferred (Jim OOB 2026-09-04「for gymbro, please use edge tts instead. wanlun voice is better than minimax tts」)

Per Jim OOB — v3.3.11→v3.3.14 MiniMax TTS path (Cantonese_crisp_news_anchor_vv2) demoted to **fallback only**. Primary cheer voice is again **Edge-TTS zh-HK-WanLungNeural (雲龍)**, Jim's preferred engine since 2026-08-30.

**Why WanLung over MiniMax**:
- Natural Microsoft HK Cantonese male voice — Jim says it sounds better than MiniMax synthetic output.
- Lower per-call cost (free via edge-tts CLI) vs MiniMax Token Plan quota (5.6 RPM, 1002 errors on burst).
- No `language_boost` / Jyutping workarounds needed — WanLung handles Cantonese out of the box.

**Changes**:
- `_synthesize_cheer_voice()` wrapper: now calls `_synthesize_cheer_voice_edge_tts(voice_text)` instead of `_synthesize_cheer_voice_new(voice_text)` (was MiniMax primary).
- `_synthesize_cheer_voice_edge_tts()`: voice flipped from `zh-HK-HiuMaanNeural` (曉曼) → `zh-HK-WanLungNeural` (雲龍). Error logging now includes stderr for debug.
- `_synthesize_cheer_voice_new()`: retained as fallback (Network/edge outage path) but no longer the primary entry. Docstring updated.
- `templates/index.html:voice_gen` label: 「Edge-TTS 整緊曉曼語音…」 → 「Edge-TTS 整緊雲龍語音…」
- `gymbro_mcp_server.py:trigger_cheer_pipeline` next_steps: voice comment flipped to WanLung.
- `README.md` cheer pipeline description: now mentions WanLung 雲龍 explicitly.
- Duration log `/tmp/cheer_durations.log` format: added `provider=edge-tts-WanLung` field for clearer audit trail.

**Rule 79 still enforced**: this function renders MP3 to `CHEER_AUDIO_CACHE/cheer_<date>.mp3` only. NO Telegram push from this layer. PWA cheer tab decides whether to embed voice bubble.

**Rate calibration**: WanLung 5.69 char/sec @ +0% (re-applied — was the baseline before v3.3.11). Target 130-160s audio for 600-720 字 sweet zone unchanged.

## [2.7.28] — 2026-08-04

### 🎨 Food log card: ALWAYS expanded (Jim OOB 2026-08-04 "show other nutrient info")

Per Jim OOB — v2.7.27 default-collapsed was wrong. The card should always show all nutrition (P/C/F grid) by default. No more tap-to-expand.

**Change**:
- Removed `x-data="{ open: false }"` per card.
- Removed the chevron ▸/▾ and the collapse hint.
- Card now shows: image + food name (large) + kcal + P/C/F grid (3 mini-cards) + timestamp + shared/corrections badges + note (if any).
- Layout tuned for one-glance reading: no hidden info.

## [2.7.29] — 2026-08-04

### ⚡ Food log: progressive scrolling (Jim OOB 2026-08-04 "list more and scroll a bit. progressive scrolling for loading performance")

Render performance + UX optimization for long food log lists.

**Changes**:
- New Alpine state: `recentScansVisible[]` (currently rendered), `recentScansPageSize=20`, `recentScansPageLoaded`, `scansLoadingMore`.
- `loadRecentScans()` fetches `limit=60` (3 pages worth) up front, slices to first 20 for initial render.
- `loadMoreScans()` triggered by click on "⬇ 拉落去載入更多" sentinel — appends next 20 to `recentScansVisible`.
- Template iterates `recentScansVisible` (not `recentScans`) — only renders what's visible.
- Bottom sentinel shows "✓ 已顯示全部 N 條紀錄" when all loaded.

**Why no infinite scroll?**: iOS Safari scroll event on `<html>` is jittery and breaks under `IntersectionObserver` patterns. Click-to-load is more reliable + same UX (scroll, see "load more", tap). Set up to be auto-scroll-triggered in future via `x-intersect` plugin.

**Frontend-only change**. SW cache v59 → v60.


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
