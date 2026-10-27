# 🏋️ gymbro — Jim's Gym Logging Web App

> Uber-inspired mobile-first gym set logger with daily motivation

A minimalist, dark-themed gym set tracker that runs as a Flask web service on Jim's home server, accessible over Tailscale VPN from an iPhone on the bench. Tap, log, pyramid view, done.

Built around three ideas:

1. **Zero friction on the bench** — one-thumb logging between sets, no app store, just a PWA in the browser.
2. **Pyramid-style set tracking** — auto-classify each set as warm-up / working / burn-out so volume and intensity stay visible.
3. **Daily motivation** — a fresh AI-generated image each morning, served alongside the streak counter.

---

## 📱 手機使用(iPhone / Android)

> ⚠️ **一定要用 HTTPS 網址**:`https://alonso.tail9e2b74.ts.net/`
> (tailscale serve → `http://127.0.0.1:7000`)。
> 舊嘅 `http://100.114.66.125:7000/` 冇 TLS,**Web Push、定位、Service Worker
> 全部唔會 work** —— 係瀏覽器嘅硬性要求(secure context),唔係 app 嘅選擇。

**加入主畫面(建議做)**
1. iPhone:Safari 開 `https://alonso.tail9e2b74.ts.net/` → 分享 → **加入主畫面**
2. Android:Chrome 選單 → **安裝應用程式 / 加到主畫面**

**通知(Web Push,v3.3.44)**
- 撳 header 嘅 🔔 → 開通知
- iOS 16.4+:**必須係加咗到主畫面嘅 PWA** 先會 work(普通 Safari 分頁唔得)
- 收到通知撳一下 → 直接跳返 app

**記位置(v3.3.44)**
- 撳 header 嘅 📍 → 記低而家喺邊度,會顯示地標同「距離上次幾遠」
- 定位係**你主動撳先執行**,唔會自動攞

**離線使用(v3.3.44)**
- 落 set / 取消 set / 結束 session / 拉 WHOOP / 食物文字輸入,如果當下冇網絡,
  會自動排隊(IndexedDB),恢復網絡後由 Service Worker 自動重試
- 圖片上傳(`scan_commit`)**唔會**排隊 —— 相太大

**已知限制**
- `gymbro_vapid.json` 入面嘅 `public_key` 同 `private_key` 對唔上,
  app 已經喺 runtime 由 private key 推導正確嘅 public key(推導唔到就推)。
  建議重新產生一次嗰個檔案。

---

## ✨ Features

- **Four-tab mobile UI** — Log · Pyramid · History · You, inspired by Uber's bottom nav
- **Pyramid view** — visual stack of sets per exercise with warm-up / working / burn-out color coding
- **PWA install** — add to iPhone home screen, full-screen, no Safari chrome, wake-lock enabled
- **Daily motivation image** — `MiniMax image-01` generated each morning, cached locally, served via `/img/`
- **Streak tracking** — consecutive workout day counter with rest-day grace
- **One-tap default reps = 10** — calibrated to Jim's training plan
- **Tailscale-only access** — bound to `100.114.66.125:7000`, no public exposure
- **JSON file persistence** — `/home/work/.whoop_workout_log.json`, human-readable, version-controllable

---

## 🧰 Tech stack

| Layer | Tech |
|---|---|
| Backend | Flask 3.1.3 (Python 3.13) |
| Frontend | Tailwind CSS (CDN) + Alpine.js 3 |
| Image gen | MiniMax `image-01` model |
| Persistence | Local JSON file (no DB) |
| Transport | HTTPS via Tailscale mesh VPN |
| Deployment | systemd-less — runs as Jim's dev process on home server |

No build step. No npm. No database. Single `gym_web.py` file (~650 lines).

---

## 🚀 Quick start

```bash
# Install deps
pip install -r requirements.txt

# Run the server (binds 0.0.0.0:7000)
python3 gym_web.py
```

Then open:

- **Local:** http://localhost:7000
- **Tailscale (iPhone on bench):** http://100.114.66.125:7000

To enable daily motivation image generation, set:

```bash
export MINIMAX_API_KEY="sk-..."
```

…and run `scripts/gymbro_daily_image.py` once per morning (cron @ 06:00 HKT recommended).

---

## 📸 Screenshot

![Demo](docs/demo.png)

> Drop a phone screenshot at `docs/demo.png` after first deploy.

---

## 📁 Project layout

```
gymbro/
├── gym_web.py                         # Flask app (single file)
├── scripts/
│   └── gymbro_daily_image.py          # MiniMax image-01 daily motivation gen
├── docs/
│   ├── setup.md                       # Detailed install + run
│   └── architecture.md                # API + data flow
├── .github/
│   └── workflows/
│       └── python-ci.yml              # Syntax check on push to main
├── requirements.txt                   # Flask==3.1.3
├── LICENSE                            # MIT
└── README.md                          # You are here
```

---

## 🤝 Contributing

This is Jim's personal gym logger. PRs welcome for:

- Better set-detection algorithms (e.g. RPE-based intensity)
- Additional exercise presets (squat / bench / deadlift ramp defaults)
- Workout-volume charts
- Apple Health / Whoop integration

For everything else, fork it.

---

## 📄 License

MIT © Jim — see [LICENSE](LICENSE).

---

## v3.x feature additions (since these docs were last updated)

gymbro has grown well beyond the workout-only logger described above. The
single ~650-line `gym_web.py` mentioned in the old docs is now **~7,300 lines**
(the HTML template was extracted to `templates/index.html`) across two entry
points plus a package:

- `gym_web.py` — Flask web app (~50 routes) — workout logging + PWA shell
- `gymbro_mcp_server.py` — MCP server (stdio, for the Alonso agent)
- `workout_formatter.py` — Whoop-AI-friendly text rendering
- `gym_web/` package — gradual modular refactor (in progress)
- `templates/index.html` — extracted PWA shell (~3,800 lines)

### Added capabilities (v2.1 → v3.2.7)

- 🍽️ **Food scan** — PWA camera upload → MiniMax vision → AI nutrition enrichment → Google Sheet mirror(v3.3.34 起:供應商鏈淨返 MiniMax 一家,Perplexity / APiyI 已移除)
- 🤖 **AI coach comments** — Traditional Chinese, 4-section, A–F grade per scan
- 📊 **Whoop + Withings overlays** — recovery %, steps, body composition
- 📅 **Schedule tab** — Whoop activities on a monthly calendar
- 🎙️ **8-section cheer pipeline** — text + voice (edge-tts WanLung 雲龍) + image (MiniMax)
- 🔌 **MCP server** — `get_latest_cheer`, `get_today_workout`, `trigger_cheer_pipeline`
- 🔁 **Retro migrations** in `scripts/migrations/` — one-shot data fixes

### Project layout (current)

```
gymbro/
├── gym_web.py                  # Flask app (monolith, ~7.3k lines)
├── gymbro_mcp_server.py        # MCP server (stdio)
├── workout_formatter.py        # Text rendering
├── gym_web/                    # Gradual refactor package (in progress)
│   ├── core.py                 # Shared constants + helpers
│   ├── whoop.py                # Whoop cache reader
│   └── (withings.py removed)  # v3.3.26 — Withings retired; WHOOP is the only source
├── templates/
│   └── index.html              # PWA shell (extracted from gym_web.py)
├── scripts/
│   ├── gymbro_daily_image.py   # Daily motivation image cron
│   └── migrations/             # One-shot data migrations
├── tests/                      # Pytest suite
├── docs/
│   ├── setup.md
│   ├── architecture.md
│   └── v2.1-food-scan-design.md
├── requirements.txt            # Flask + mcp + requests + python-dotenv
├── requirements-dev.txt        # + pytest
└── .github/workflows/python-ci.yml
```
## 📥 推播收件箱(v3.3.46)

App 最底多咗一個 **📥 收件** tab —— 顯示 **Gymbro 自己推出去嘅訊息**
(唔係對話紀錄):

- **📲 推播** = Web Push 通知(訓練 / 瞓覺提醒等)
- **🔥 Cheer** = app 內產生嘅打氣訊息(文字 + 語音 + 圖)
- 每張卡顯示:標題、內容、送出結果(✅ 已送出 / ⚠️ 失敗)、送達幾多部機、時間(HKT)
- 撳搜尋框可以搜標題 / 內容;filter:全部 / 推播 / Cheer;仲有「載入更多」
- 紀錄 store:`/home/work/.hermes/gymbro_outbox.json`(最多 500 條)
- 記錄**唔會**阻礙原本嘅推播(寫 log 失敗一律吞掉)
- 完全唔掂 Google Sheet / 食相路徑

API:`GET /api/inbox/messages`(`limit` / `before` / `kind` / `q`)、
`GET /api/inbox/stats`。


### 語音教練(v3.3.48)
兩個解說員輪流講(A 聲 = `Cantonese_crisp_news_anchor_vv2`,B 聲 =
`Cantonese_resonant_host_vv2`),每個 cheer routine 開頭都會 force 拉一次
WHOOP(記低 `whoop_fresh` 旗標,睇得見啱唔啱新鮮)。

