## v3.3.49 — 2026-10-27 — 修正:食物日期講錯 + WHOOP 指標唔咗入 prompt

Jim OOB 2026-10-27:「the voice was saying that i ate 6 food. but the food
was actually ytd not today」

### 三個 root cause + 修法

**1. 營養 fallback 闊到 24 個鐘** ❗
`_load_today_nutrition()` 嘅 late-night fallback 係「今日 0 餐 → 攞尋日」,
但**冇時間條件** → 下午兩點零餐都會攞返尋日 6 餐當今日 ❗
→ 收窄成 `NUTRITION_FALLBACK_MAX_AGE_H = 8`(淨係真係熬夜/啱啱過午夜先
fallback ✅),並用 row 自己嘅 date+time 判斷(`_latest_meal_age_hours` ✅)

**2. prompt 硬寫「今日飲食記錄 / 已 log N 餐」** ❗
LLM 見到「6 餐」就當係今日 → 而家 prompt 用 `day_label`(今日 / 尋日 / 前日 /
N 日前)+ 明確寫記錄日期,唔係今日就唔准用「今日」字眼 ✅

**3. `ctx["whoop"]` 被成個蓋走** ❗❗(最嚴重)
`_build_daily_context()` 原本 `ctx["whoop"] = {"steps": ...}` → 將
recovery / HRV / 睡眠 / 疲勞**全部抹走** ❗所以 prompt 見到嘅復原度係
None → narrative 講「身體數據未成功讀到 / 按紅燈處理」,但實際 recovery
係 73% ✅ → 改成 merge(只加 steps ✅)

### 其他
- 數字用阿拉伯數字(「2 kcal」唔准寫成「兩千卡」❗)✅
- food coach comment 都會標明日期 ✅
- 新增 `[cheer] ctx | rec=… | sleep=… | steps=… | meals=N(date/label)` log ✅
- SW cache `gym-web-v152` → `gym-web-v153` ✅、版本 3.3.49 ✅
- 測試:`tests/qa/test_nutrition_date_correctness.py`(8 個守門 ✅)

### 驗證
- `ctx` log:`rec=73.0 | sleep=7.88h | steps=3177 | meals=2(2026-10-27/今日)` ✅
- 新 narrative:「今日兩餐都係高蛋白低醣:14:07 冰黑咖啡配三文魚刺身同紅洋蔥絲,
  合計約252 kcal、蛋白質24克…」+「瞓咗7.88個鐘,表現78%;復原73.0%…綠燈」✅

## v3.3.48 — 2026-10-27 — 語音教練升級:兩位解說員 + 每個 cheer 必拉 WHOOP

Jim OOB 2026-10-27:「voice coach, can we switch to two voice commentators
that will not be too boring? Moreover, please pull whoop data once to get
the most update data in every cheer routine.」

### 1️⃣ 兩位解說員(commentators)嘅語音
- **A 聲(主講)**:MiniMax `Cantonese_crisp_news_anchor_vv2`(Jim locked choice ✅)
- **B 聲(副講)**:MiniMax `Cantonese_resonant_host_vv2`(另一把聲 ✅)
  B 聲掛咗 → edge-tts `zh-HK-HiuMaanNeural`(曉曼 ✅)做後備
- **點樣夾**:文本按句子切成 4 段(**順序保留 ✅**,唔會倒轉內容 ✅)
  → 單數段俾 A、雙數段俾 B → 一來一回,似兩個講評員輪流講 ✅
- **合併**:ffmpeg concat(段與段之間 0.35 秒停頓 ✅,似真實交接 ✅)
- **安全網**:任何一步失敗 → 回 `""` → dispatcher 自動轉單聲 → 再 fallback
  edge-tts(唔會靜靜哋死 ✅)

### 2️⃣ 每個 cheer routine 都拉一次最新 WHOOP
- `_run_whoop_pull_cached(force=True)` ✅ 喺每個 cheer job 開頭行 ✅
- 新增 `whoop_fresh` 旗標:睇 pull 完之後 cache mtime 係咪喺 5 分鐘內 ✅
  → 睇得見今次用嘅係咪真・最新數據 ✅
- 日誌:`[cheer] whoop pull | fresh=True | age=0.3s | rec=25 | sleep=4.37h ...` ✅

### 其他
- SW cache `gym-web-v151` → `gym-web-v152` ✅(舊 client 要換新 SW 先有 duo 同
  新 deep link ✅)
- 影片沿用 `cheer_{today}.mp3` → duo 語音自動入片 ✅
- 測試:`tests/qa/test_cheer_duo_voice.py`(12 個守門 ✅)

## v3.3.47 — 2026-10-27 — 推播深連結:撳通知直接入收件箱

Jim OOB 2026-10-27:「When I receive the notification, can I just click to
enter directly to inbox. I guess some kind of deep link」。

### 實作
- **深連結格式**:`/?tab=<tab>`(同時支援 `#<tab>`)✅
  允許 tab:`set` / `workout` / `scan` / `end` / `cheer` / `schedule` / `inbox` ✅
- **Service Worker `notificationclick`**(v150 → v151):
  - app 開住 → `focus()` + `postMessage({type:'navigate', url})` → **原地跳 tab**
    (唔再開新窗口,iOS 開新窗口會令人以為 app 爆咗)✅
  - app 冇開 → `openWindow(url)` → 啟動時讀深連結 ✅
- **前端**:`_deepLinkTab()` / `applyDeepLink()` / `_listenSwMessages()` ✅
  - `init()` 一開始就套用深連結(唔等 fetch 完)✅
  - 收 SW 嘅 `navigate` message → 即刻跳 tab ✅
  - URL 同步成 `?tab=...`(reload 都留喺同一個 tab)✅
- **推播 cron**(`scripts/gymbro_push_notify.py`)→ 推播 url 改成 `/?tab=inbox` ✅
- 測試:`tests/qa/test_inbox_api.py` 加深連結守門(共 17 個測試)✅

### 驗證
- Playwright(390×844):開 `/?tab=inbox` → inbox tab 即刻 active ✅、
  panel 顯示 ✅、訊息卡載入 ✅;開 `#inbox` 一樣 ✅
- 線上實測:推播 `url=/?tab=inbox` → **送達 1 部機** ✅、outbox 記低 url ✅
- QA 408 passed / 4 skipped ✅

## v3.3.46 — 2026-10-27 — 收件箱改成「Gymbro 推播」

Jim OOB 2026-10-27:「Just the message push from gymbro. Not our conversation.」
→ 收件箱唔再顯示 Hermes Telegram 對話,改為顯示 **Gymbro 自己推出去嘅訊息**。

### 改動
- 新增 `gym_web/outbox.py` — 出站訊息紀錄(append-only JSON,最多 500 條,
  store = `/home/work/.hermes/gymbro_outbox.json`)
  - `log_message()` / `list_messages()` / `stats()`
  - kind: `push`(Web Push 推播) / `cheer`(打氣訊息)
  - **只記錄,永遠唔會阻礙原本嘅推播**(寫 log 失敗一律 swallow + print)
- `POST /api/push/send` → 每次推播都記低(送咗幾多部機 / 失敗原因 /
  「冇 subscription」)
- cheer pipeline 完成 → 記低 cheer 文字 + 有冇語音 / 圖
- `/api/inbox/messages` + `/api/inbox/stats` → 改讀 outbox
  (參數 `kind` = all / push / cheer,取代原本嘅 dir)
- 前端:tab 改名「📥 推播收件箱」,filter 改成 全部 / 📲 推播 / 🔥 Cheer,
  卡片顯示 標題 + 內容 + ✅已送出 / ⚠️失敗 + 送達部機數
- **刪除** `gym_web/inbox.py`(Hermes state.db 對話讀取層)
- `tests/qa/test_inbox_api.py` 改寫成 outbox guard(15 個測試)
- sw.js cache v149 → v150

### 安全
- outbox 只寫自己嘅 store,**唔讀** Hermes state.db(唔再係對話收件箱)
- 完全唔掂 Google Sheet / food-scan / vision 路徑

## v3.3.45 — 2026-10-27 — 訊息收件箱(Telegram 歷史訊息)⚠️ 已被 v3.3.46 取代

> Jim 澄清:收件箱要嘅係「Gymbro 推出去嘅訊息」,唔係對話紀錄。
> 呢版讀 Hermes `state.db` 嘅對話讀取層已喺 v3.3.46 刪走。


Jim OOB 2026-10-27:「In gymbro, there should be an inbox showing all the
historical messages received」。

### 新增
- `gym_web/inbox.py` — 收件箱讀取層(**唯讀** `state.db`)
  - `list_messages()` — 分頁(before cursor)+ 方向 filter + FTS 全文搜索
    (FTS 壞咗自動退返 LIKE)
  - `stats()` — 收到 / 發出 / 總數 / session 數 / 最後一條時間
  - 過濾:role='tool'(工具輸出)、系統注入式 user 訊息(壓縮摘要 /
    gateway / skill 提示)、display_kind = internal_notification / hidden /
    failed_turn
  - 去重:同一段內容重複寫入過(session fork / resume)→ 只留最新嗰條
  - OOB 包裝拆封 → 顯示 Jim 原本嘅說話
- `GET /api/inbox/messages` — 參數 `limit` / `before` / `dir`(all|in|out)/
  `q`;fail-soft(DB 掛咗 → 200 + 空結果)
- `GET /api/inbox/stats`
- 前端新增第 5 個 tab **📥 收件箱**(tab bar 由 4 欄變 5 欄):搜尋框、
  方向 filter、訊息卡(↓收到 / ↑發出 + 時間)、載入更多
- `tests/qa/test_inbox_api.py` — 12 個守門測試(唯讀 / 過濾 / 去重 /
  分頁 / 搜索 / 前端 + 路由)

### 安全
- 收件箱**只讀** `state.db`(SQLite `mode=ro`),唔會寫入任何訊息
- **完全唔掂** Google Sheet / food-scan / vision 路徑
- 測試用自己造嘅 tmp SQLite fixture,唔讀唔寫生產 state.db

## v3.3.44 — 2026-10-26 — 手機 PWA 強化:Web Push + 定位 + 背景同步(離線隊列)

規格:`docs/MOBILE_PWA_SPEC_v3.3.44.md`(唯一真實來源)。branch:`feature/v3.3.44-mobile-pwa`。

### 1. 新增檔案
- `gym_web/push.py` — Web Push 工具:VAPID 金鑰讀取、subscription 儲存、發送
  - VAPID 金鑰喺 `/home/work/.hermes/gymbro_vapid.json`(repo 之外,mode 0600),
    **private key 唔會入 git / 唔會入 log**
  - ⚠️ 實測發現嗰個檔案入面嘅 `public_key`(43 字元)**同 private_key 對唔上**
    (由 private key 推導出嚟嘅係 87 字元)。所以 `vapid_public_key()` 會**由
    private key 推導**出正確嘅 public key,並且喺 log 度出一次警告 ——
    唔推導嘅話每次 push 都會 401/403。`/home/work/.hermes/gymbro_vapid.json`
    應該重新產生。
  - subscription store:`GYMBRO_PUSH_STORE`(預設
    `/home/work/.hermes/gymbro_push_subs.json`),endpoint 做 key,原子寫
- `gym_web/geo.py` — 位置工具:位置紀錄(上限 200 條)、Nominatim 反向地理編碼
  (24 小時 cache,3 秒 timeout,失敗回傳空字串,**唔會編造地名**)、
  haversine 距離、最近一筆有 label 嘅紀錄
- `tests/qa/test_mobile_pwa_api.py` — 20 個測試(store 一律 override 去 tmp_path,
  **唔會**寫入真實 `/home/work/.hermes/gymbro_*.json`,亦**完全唔掂** Google Sheet)

### 2. 新增 7 個 endpoint(全部 fail-soft,唔會 500)
| Route | 用途 |
|---|---|
| `GET /api/push/vapid_public_key` | 畀 browser `pushManager.subscribe()` 用 |
| `POST /api/push/subscribe` | 儲存 subscription |
| `POST /api/push/unsubscribe` | 刪除 subscription |
| `GET /api/push/status` | subscription 數量 + VAPID 是否 ready |
| `POST /api/push/send` | 群發通知;push service 回 404/410 → 自動剷走失效 subscription |
| `POST /api/geo/log` | 記低位置 + 反向地理編碼 + 距離上次幾遠 |
| `GET /api/geo/recent` | 最近 n 筆位置 |

輸入驗證:經緯度範圍、endpoint ≤ 2048 字元、title/body ≤ 200 字元、payload ≤ 4 KB。
**任何 response 都唔會包含 VAPID private key。**

### 3. Service Worker(純 additive,fetch/caching 邏輯一律冇改)
- `push` → 顯示通知;`notificationclick` → focus 現成 client 或者開新 window
- `message` → `SKIP_WAITING` / `FLUSH_QUEUE`
- `sync`(tag `gymbro-queue`)→ 自動重試排隊嘅 POST
- IndexedDB(`gymbro-offline` / store `queue`)離線隊列:2xx 就刪,失敗就
  `attempts+1`,超過 5 次就丟棄;完成後更新 app badge

### 4. 前端(`templates/index.html`)
- Header 加咗兩個 44×44 按鈕:🔔(通知開關:已開啟 / 未開啟 / 已封鎖)同 📍(記位置)
- iOS 16.4+ 一定要加咗主畫面嘅 PWA 先有 Web Push → 唔係 standalone 就提示
  「請先用 Safari 分享 → 加入主畫面,再開通知」;非 secure context 就直接話
  「需要 HTTPS」
- 通知流程:`Notification.requestPermission()` → `pushManager.subscribe()` →
  `POST /api/push/subscribe`
- 定位流程:`getCurrentPosition` → `POST /api/geo/log` → 顯示返地標同距離;
  權限被拒 / 逾時 用文字講,**唔會**彈 `alert()` / `confirm()`
- `apiPost(url, body, {queueable:true})` wrapper:`/api/log_set`、
  `/api/cancel_last_set`、`/api/end_session`、`/api/whoop_refresh`、
  `/api/scan_preview_text` 全部行入離線隊列;network error → 寫 IndexedDB +
  `reg.sync.register('gymbro-queue')`(冇 Sync API 就靠 `online` event +
  SW `FLUSH_QUEUE`)
  - ⚠️ `scan_commit`(**唔可以** queue 住 —— 圖片上傳太大,而且係 Sheet 寫入路徑)
- `manifest.json` 補咗 `id` / `scope` / `lang` / `dir` / `categories` +
  2 個 shortcuts(影相記錄 → `/?action=scan`、落 set → `/?action=logset`)
- `<main>` 底部 padding 改成 `calc(env(safe-area-inset-bottom) + 5rem)`,
  iPhone home indicator 唔會遮住最後一行

### 5. ⚠️ 一定要用 HTTPS
Web Push / Geolocation / Service Worker 全部要求 secure context。
Jim 手機上面要用 **`https://alonso.tail9e2b74.ts.net/`**(tailscale serve),
舊嘅 `http://100.114.66.125:7000/` 冇 TLS,呢三樣全部唔會 work。

### 6. 驗證
- `py_compile gym_web.py gym_web/push.py gym_web/geo.py` ✅
- `pytest tests/qa/test_mobile_pwa_api.py` → 20 passed ✅
- Service Worker 同前端 inline script 用 `node --check` 驗過語法 ✅

---

## v3.3.42 — 2026-10-25 — 語音改用 MiniMax(全局 + gymbro + 9am cheer)

Jim 2026-10-25「Yes. Change to minimax for voice」。

### 1. 全域 Hermes TTS
- `hermes config set tts.provider minimax`
  (`tts.minimax.model = speech-02-hd`,`voice_id = Cantonese_crisp_news_anchor_vv2`,
  `language = Chinese,Yue`)
- edge-tts WanLungNeural 保留做**後備**(Microsoft 每 4–8 週輪換 token → 403 會再嚟)

### 2. gymbro cheer dispatcher 翻轉
- `_synthesize_cheer_voice()`:由 edge-first 改成 **MiniMax-first、edge-tts 後備**
- ⚠️ 舊碼喺 v3.3.24c(2026-10-17)已經漂移成 edge-first,但 docstring 仍然寫
  「MiniMax primary」→ **docstring 同 code 會唔一致,永遠睇實際 call order**
- docstring / 註釋同步更新,寫低三個 OOB 嘅歷史(09-04 edge → 10-17 MiniMax → 10-17 edge → 10-25 MiniMax)

### 3. 9am cheer cron job(`5731dd241e82`)
- STEP 3:由 edge-tts CLI 改成直接打 `POST /v1/t2a_v2`(speech-02-hd +
  `Cantonese_crisp_news_anchor_vv2`),輸出 `/home/work/.hermes/audio_cache/cheer_voice_today.mp3`;
  MiniMax 報錯先做 edge-tts 後備
- STEP 5:**唔再 sendVoice、唔再 upload 去 uguu.se**(Rule 79:Telegram 一律唔送音訊),
  改為喺文字報告寫低語音檔案路徑同時長

### 4. 順手修咗「語音文字被清空」嘅老 bug
- `_humanize_for_voice()` Layer 3(開頭審計)見到第一句有英文就整句丟咗 → **單句短文會被清成空字串**
  → MiniMax 收空字串 → `code 2013 invalid params, empty field` → 靜靜哋跌返去 edge-tts
- 修法:dispatcher 保留 humanize 前嘅原文,一旦被清空就還原(兩道防護:humanize 前 + EN 審計後)
- 實測:純中文 / 有 ASCII / 真實 cheer 全文 三種都真係行 MiniMax,2013 錯誤零新增 ✅

### 5. 驗證
- `text_to_speech`(無 provider 參數 → 跟 config)→ MiniMax 出貨:486 字 → 107KB OGG、18.1 秒 ✅
- QA:hygiene 組頭先報 fail 係因為「server 仲跑緊 3.3.40 / 未 commit」→ commit + 部署後重跑

### 5. 同步更新
- skill `hermes-tts-edge-wanlung-2026-08-30`(改為 MiniMax primary)、`media/minimax-tts`
- 記憶:全域 `tts.provider=minimax`

## v3.3.41 — 2026-10-25 — 修 /api/nutrition/today 回退日期 + 快取序列號轉換

(每日健康巡檢 job 自己搵到嘅兩個真 bug,已 commit `c4a08f8` / `1a5d019`)
- `/api/nutrition/today`:回退日期會被標成今日 → 修正
- `gym_web/cache.py`:欄位感知序列號轉換(kcal ≥ 1000 被當日期 → 0 卡)
- 注意:呢兩次 commit **冇** bump `__version__`(仍然 3.3.40),所以 v3.3.42 一并補返

## v3.3.40 — 2026-10-23/24 — 修 workout log key/date 錯位 + 原子寫 + 永久 QA 守衛

(commit `9eee9f0`)

## v3.3.39 — 2026-10-23 — 落 set 快路 + 補記過去一節 + 修死 port

Jim 2026-10-23 20:0x「落 set」→ 由「3 個 session 全部 0 組」查起,
發現 **app-tap log 自 2026-08-28 起就冇新 set**(本機 log 只有 7/19-8/28 嗰 10 日有),
但後端一直冇壞 → 問題係「冇快路」而唔係「寫入壞」。

### 1. 新增:落 set 快路
- `scripts/log_sets_from_text.py` — 一句廣東話/中英夾雜 → 多組 set:
  `臥推 60x8 60x8, 深蹲 80x10` → BB Bench Press 60kg×8 ×2 + Squat 80kg×10
- 支援 `WxR` / `W x R` / `W R` / `x8` / `WxRxN` / 淨數字(重量+多組次數)/自重動作
- 中文動作名自動對應 app 目錄 30 個預設動作;對唔到 → 原樣保留中文名
- `--dry-run` 只解析唔寫入;`--date YYYY-MM-DD` 補記過去一節
- 只寫本機 `/home/work/.whoop_workout_log.json`,**唔掂生產 Sheet**

### 2. `/api/log_set` 升級
- 新增選填 `date`(YYYY-MM-DD)→ 補記過去一節(格式錯 → 400)
- `set_n` 唔俾 → **自動遞增**(同一動作喺嗰一節已做幾多組 + 1)
- **修 bug**:原本 `log[today_iso()] = session` 會令補記嘅 session 寫錯 key
  → 覆蓋今日 session。改用 `log[session.get("date") or today_iso()]`(3 處)

### 3. 修 MCP 死 port
- `gymbro_mcp_server.py` 嘅 `log_meal_text` 一直 POST 去 `GYM_WEB_URL=:4280`
  (冇 process 監聽)→ 每次都 connection-refused。改用 `GYM_WEB_BASE=:7000`
- 移除已死嘅 `GYM_WEB_URL` 常量;docstring 同步改返 MiniMax-only

### 4. 版本
- `__version__` 3.3.38 → 3.3.39;skil `fitness/gymbro-set-logging` 新增

### 驗證
- 補記測試:2026-10-05 set 1 → set 2 自動遞增 ✅;今日 session 0 組(冇被污染)✅
- script 端到端:3/3 組寫入 2026-10-06 ✅;測試日全部清走 ✅
- 錯誤 `date` → 400 ✅

---

## v3.3.38 — 2026-10-22 — 修「縮圖又全部一樣」(系統性修法)+ Alpine 本地化

Jim OOB 2026-10-22 20:1x:
「The thumbnail are the same again. Pls fix the thumbnail and the systemic problem.」
+ OOB「Do a deeper research to see what's going on」

### 1. 深查證據(逐層排除,唔係猜)
| 層 | 檢查 | 結果 |
|---|---|---|
| 伺服器 byte | 24 行 `/scan_thumb` 逐個 md5 | **24/24 全部唔同** ✅ 唔係渲染 bug |
| Sheet column K | 重複 id / trashed / 唔存在 | 只有 2026-08 一對舊重複;**冇一行指向 trashed 檔** ✅ |
| API payload | scan_index / image_url / thumbnail_url / Alpine `:key` | **全部唯一** ✅ 唔係資料重複 |
| 前端 DOM | 每行 `img.src` | **全部唔同** ✅ 唔係 template 綁錯 |
| HTTP cache | `Cache-Control` | `no-store` ✅ 唔係瀏覽器 HTTP 快取 |
| SW | `/scan_thumb/*` 規則 | network-first ✅ |
| **前端 `loading="lazy"`** | 實測 20 張縮圖 | **0/20 發出過 request**(naturalWidth=0,complete=false)❌ |
| 改成 `loading="eager"` | 同一頁重測 | **20/20 全部載入** ✅ |

**真正根因(兩個,都係「系統性」)**
1. **`loading="lazy"` 嘅食物歷史縮圖從來冇 load 過** → 每一行顯示同一個
   空白/失敗框 → Jim 見到「縮圖一樣」。實測加 `?bust=` 都無用(仍然 0 載入),
   證明唔係快取問題,而係 lazy 本身根本冇發 request。
2. **`/scan_thumb/<row_index>` 呢條 URL 唔係 immutable** —— 刪一行 → row 移位;
   換相 → 檔案換咗;但 URL 冇變 → 客戶端(手機瀏覽器 / iOS 圖片解碼快取)
   可以繼續顯示舊圖。同一類「縮圖一樣」會無限復發 —— 呢個就係
   v3.3.36 修完之後仍然「again」嘅原因。

### 2. 修法(5 層)
1. **內容定址 URL**:`?v=<drive file id 嘅短 hash>`(`gym_web/cache.py`
   `_content_version()`)。同一個檔 → 同一個 v;檔案換咗 / row 移位 → v 變
   → URL 變 → 客戶端**唔可能**再顯示舊圖。
2. **Cache header 分兩種**:帶 `?v=` → `public, max-age=31536000, immutable`
   (內容定址可以永久快取);舊 URL → 照舊 `no-store`。
   `after_request` 加 `X-Gymbro-Immutable: 1` 放行,唔好被 no-store 蓋走。
3. **trashed 守衛**:派圖之前查 Drive 檔案係咪已被 trash(30 分鐘快取,
   fail-open)。trashed → **404**,唔再派 Drive 嗰張「垃圾桶」placeholder
   (2026-10-21 三張縮圖 byte 级一樣嘅元兇)。
4. **前端移除 `loading="lazy"`**(食物歷史 + 掃描預覽),改 `decoding="async"`。
   伺服器有 100 行 LRU,回應 1.8ms,唔需要 lazy。
5. **SW cache 版本 144 → 145** → 手機上面所有舊快取( 包括 2026-10-21
   嗰批 placeholder)一次過清走;新規則:`?v=` 內容定址 → cache-first,
   冇 `?v=` → 照舊 network-first。

### 3. 順手修嘅系統性問題
- **Alpine 由 unpkg CDN 改為本地託管**(`/js/alpine-3.17.5.min.js`,CDN 留後備)。
  CDN 慢/被擋 → Alpine 唔起 → 成個 app 空 → **所有縮圖同一個空框**;
  亦係 QA `frontend` group 隨機 timeout 嘅真正原因(實測:重跑即綠)。
- **Google access token 加 45 分鐘記憶體快取** —— 之前每次 Drive 查詢都做一次
  OAuth refresh,餐牌 JPG 拉 7 張縮圖慢 3-4 秒;快取後由 8.5s → 2.8ms。
- **餐牌 JPG 解析 row index 要剝走 `?v=`**(否則卡上縮圖會消失)。
- 新增唯讀診斷工具 `scripts/check_thumb_integrity.py`:
  逐行查 column K 重複 / trashed / 唔存在 + 逐行拉縮圖比對 byte。

### 4. 驗證
- 新 regression `tests/qa/test_thumb_url_integrity.py`(19 passed)+ QA 組 `thumbintegrity`
- 伺服器:`/scan_thumb/669?v=0dcfec79ba` → 200 immutable;冇 `?v=` → no-store
- **20 張縮圖逐張 md5:20/20 唯一,0 重複組**
- 瀏覽器實測:20/20 縮圖載入成功、每行 md5 唔同、Alpine 由本地載入、SW 已接管

## v3.3.37 — 2026-10-22 — 修「改名之後 macro detail 冇變」

Jim OOB 2026-10-22 14:0x:
「The food recognition is not wrong. It recognized as 白飯 only.
 When i change the name to 海南雞白飯 the macro detail is unchanged」

### 1. 實測證據(唔係猜)
- Sheet row 668(2026-10-22 13:35)已經改名做「海南雞白飯」,
  備註 = `scan_food rename` → **改名功能確實行過**
- 但 `kcal/P/C/F = 260 / 6 / 56 / 17` —— 同 AI 原本認到嘅「白飯」**一模一樣**
  (260 kcal、得 6g 蛋白 = 一碟淨白飯,冇雞)
- 真相:海南雞白飯應該 ~500 kcal / 33g 蛋白
- 即係:**名改咗,營養冇重估**

### 2. 根因:兩個 endpoint 都用錯咗「未設定」嘅判斷
`/api/scan_rename` 同 `/api/scan_correct` 都有同一個 pattern:

```python
new_kcal, new_p, new_c, new_f = old_kcal, old_p, old_c, old_f   # ← 舊值做預設
...
if new_kcal <= 0:      # ← AI 層先會套用
    new_kcal = ai_kcal
```

因為預設已經填咗**舊值**(`> 0`),`new_kcal <= 0` 永遠唔成立 →
**AI 重新估算層根本冇行過**。功能(v2.7.39 為 Jim 2026-08-06
「白切雞 → Hai nan chicken rice」而寫)一直係壞嘅,只要舊行有營養值。

`/api/scan_correct` 更加絕:`row.kcal <= 0` 先重估 → 有數就唔郁。

### 3. 修法
1. 抽 `_reestimate_macros_for_name(name, restaurant)`(新 helper):
   **由 0 起步**行 4 層鏈 —— explicit(名內數字)→ brand(品牌表)→
   **AI(MiniMax)**→ history(Sheet 歷史)。回傳 `source` 講明邊層贏。
2. `/api/scan_rename` 同 `/api/scan_correct` 都用呢條鏈;
   `scan_correct` 額外加咗「名真係改咗先重估」(`name_changed`),
   淨係加餐廳/備註唔會無謂行 AI。
3. **全部層都失敗 → 保留原本 macros**(`macros_source = "kept_previous"`),
   唔會把一行變 0;前端亦都會講「AI 重新估算失敗,保留原本營養」。
4. 順手修:重複 rename 令備註愈堆愈長(`scan_food rename rename`)。

### 4. 驗證
- 新增 `tests/qa/test_rename_macro_reestimate.py`(11 項,AST 抽 helper + mock 4 層)
- 實測 row 668 重新估算:
  `260 kcal / P6` → **`500 kcal / P33 / C55 / F15`**(`source = ai`)✅
- Sheet 讀返核實:`海南雞白飯 | 500 | 33 | 55 | 15`✅
- 2026-10-22 營養卡重新生成(152KB)✅

## v3.3.36 — 2026-10-22 — 修「昨日啲餐嘅縮圖全部一樣」

Jim OOB 2026-10-22 01:1x:
「i just logged on gymbro, the thumbnail for ytd meal are all the same
(incorrect). please fix it」

### 1. 實測證據(唔係猜)
`/scan_thumb/659..665` 逐張抓落嚟比對 byte:
- 659/660/661/662 → 4 張唔同 ✅
- **663 / 664 / 665 → md5 一模一樣(`f0c72585af72`,39911 bytes)** ❌

再查落去:
- Sheet `column K`(真相):row 663/664/665 → `scan_20261021_210255.jpg`
  (21:02 上載)、`scan_20261021_214520.jpg`(21:46 上載),**trashed=False** ✅
- `/scan_img/663..665` 派嘅 id → `scan_20261021_170451.jpg`、
  `scan_20261021_170759.jpg`(17:04-17:09 上載),**trashed=True** ❌
- Drive 對住 **trashed** 檔會回同一張「已掉進垃圾桶」placeholder
  → 3 張縮圖 byte 级一樣。

### 2. 根因: image proxy 個 URL LRU 唔識跟住 column K 變
`gym_web/image_proxy.py` 有一個 row → Drive URL 嘅記憶體 LRU
(`_url_cache`)。**但佢只喺 miss 時讀一次 Sheet,之後就唔再核對。**
今日 17:04-17:09 Jim 上載咗一批相(之後被 trash 換走),URL LRU 記低咗
舊 id;21:02-21:46 新相入咗 Sheet,但 LRU 冇更新 →
`/scan_thumb` 繼續去 Drive 攞已被 trash 嘅檔 → placeholder → 全部一樣。
另外 `NutritionCache` 有 background refresher 定期 re-hydrate
(所以 app 自己嘅 JSON 一直顯示啱嘅 id),**但 image proxy 唔知呢件事**。

### 3. 修法(3 層)
1. **`_resolve_drive_url()` 先問 `NutritionCache`**(定期 re-hydrate,先新鮮),
   再問 URL LRU,最後先直讀 Sheet。cache 壞咗唔會炸 image serving
   (fallback + `log.warning`)。
2. **thumb bytes 快取記住「由邊個 URL 生成」**(`_thumb_src`):URL 一變就
   重新生成,唔再照派舊 bytes。`_thumb_src` 會跟住 `shift_thumb_cache()`
   一齊 shift(而且獨立於 bytes LRU,唔會被 early-return 吞掉)。
3. **`NutritionCache.hydrate()` 之後清走 URL LRU**(`clear_url_cache()`):
   一次新快照唔應該有舊快照嘅 row → id 對應。

### 4. 驗證
- `/scan_thumb/659..665` → **7 張全部唔同**(修正前 5 張,3 張重複)✅
- `/scan_img/663..665` → id 同 Sheet `column K` **逐個一致** ✅
- 每日 JPG 卡(`/api/nutrition/card?date=2026-10-21`)重生後 7 餐 2 頁 ✅
- 新增 `tests/qa/test_thumb_url_freshness.py`(12 個測試,全綠),
  已加入 `run_qa.sh` 嘅 `thumbfresh` 組。

### 5. 冇碰生產資料
全部係讀取 + 記憶體快取修正;冇改 Sheet、冇改 Drive、冇刪任何檔案。

---

## v3.3.35 — 2026-10-21 — 修「WHOOP band off」假警報 + 統一 as-of

Jim OOB 2026-10-21 20:0x:
「Why gymbro is saying my whoop band off?!!! The as of date, make it same as
data as of data」

### 1. 「WHOOP band off」係假警報 —— 推論已實測推翻
舊邏輯:`band_off = source_age_min > 6h`,即係用 cycle `updated_at` 嘅年齡
去推斷「band 除咗身」。**呢個推論係錯嘅**:
- 連續 8 個 cycle 嘅 `updated_at` 全部落喺朝早 **07:59-08:34 HKT**,
  連「31147 步 / strain 18.1 / maxHR 163」嗰日都一樣 → 佢係「每日朝早
  寫一次記錄」嘅 record-level stamp,**同 band 有冇戴完全無關**;
- 今日 11:22 / 11:45 / 11:46 三次 live pull,`step_count` 由 **1042 → 1159
  → 1342** 一路升,而 `updated_at` 一直企喺 **08:14:53**。
→ 舊邏輯**每晚 08:30 之後一定會報「band off」**,無論 band 有冇戴。

而家改成講真話:**個步數連續 >3h 冇郁過** → badge 寫「步數 X 個鐘冇增加」,
唔再亂指住個 band 嘅狀態;`band_off` 保留喺 API(兼容),但加咗
`band_off_basis: "steps_frozen"` 講明新語意。

### 2. 同一份資料兩個「as of」→ 統一
- `/api/health` 用 `_meta.sync_time_epoch` → 報 **65.74 分鐘**;
- app 用檔案 mtime → 報「as of 19:51」。
根因:`_meta` 係一個**已經唔存在嘅 script**(`whoop_pull_inline.py`)留低嘅
遺物,之後冇人再改過 → 永遠停喺舊時間。
而家:
- `whoop_nutrition.py --sync` **每次寫入都重新蓋章**
  `sync_time_hkt / sync_time_utc / sync_time_epoch / refreshed_at`;
- `/api/health` 嘅 `whoop_cache_age_minutes` 以**檔案 mtime** 為準;
- **`data_as_of` = 個步數最後一次變化嘅時間**(data 嘅真正 as-of),
  所以 app 唔會再出現「as of 19:51」同時又話「band off」咁自相矛盾。
  個數郁嘅時候 as-of ≈ 而家;個數停咗 as-of 就企喺真實停低嗰刻。

### 3. 新增欄位
`/api/whoop_steps`:`pulled_at`、`last_change_at`、`last_change_known`、
`steps_frozen_min`、`band_off_basis`;
`/api/health_overlay`:`steps_band_off_basis`、`steps_frozen_min`;
MCP `get_health_overlay`:`steps_band_off_basis`、`steps_frozen_min`、
`steps_last_change_at`。

### 4. 步數變化史
`whoop_nutrition.py --sync` 每次 pull 都喺 `_meta.steps_history` 記低
`{ts, steps, date}`(保留 96 條 ≈ 4 日),下游由佢計「最後一次變化」。

### 5. 跟進 v3.3.36 —— 步數歷史只記「真變化」+ 唔好畀第三個 writer wipe 咗

`whoop_pull_inline.py`(第三個 writer,9am/9pm 幾個 cron 都會跑)以前**覆寫**
成個 cache → 20:16 實測:跑完 `_meta.steps_history` 只剩 3 條,而且真正嘅
「變化邊界」唔見咗 → as-of 退化返做「最後一次 pull 時間」(就係 v3.3.31 嗰個
Jim 投訴嘅講大話版本)。而家佢 merge + 保留 + 延伸 history。

另外 `whoop_nutrition.py --sync` 改成**只喺個數真係變咗嘅時候**先記一條:
如果每次 pull 都記,96 條只夠 8 個鐘(app 每 5 分鐘 pull 一次)→ 真正嘅變化
邊界會被剪走,as-of 會慢慢飄向「而家」。只記變化 → 96 條 = 96 次真變化,
「最後一次變化」長期保住,as-of 企喺真實停低嗰刻(08:14 就一直 08:14)。

### 6. 測試
- `tests/qa/test_whoop_freshness.py`(新,6 個 test):舊推論已死、
  凍結先會 flag、as-of = 最後一次變化、pull age / source age 分開、
  writer 每次都重新蓋章、health age 用 mtime。
- `tests/api/test_whoop_steps.py`:2 個**舊有失敗**順手修好
  (v3.3.31 改咗 `age_min` 語意但測試冇跟),另外加咗 3 個新 test。

---

## v3.3.34 — 2026-10-21 — 拎走 Perplexity,淨返 MiniMax 一家

Jim OOB 2026-10-21:「Remove perplexity. keep minimax only」

**點解拎走**:Perplexity 條 key 2026-10-21 額度爆咗
(`{"type":"insufficient_quota","message":"You exceeded your current quota"}`),
HTTP 401。而且每次 scan 都要多等一個 provider 先交叉核對 —— 救唔到任何嘢。

### 1. 供應商鏈
```
之前: MiniMax-text-01 → Perplexity sonar
而家: MiniMax-text-01            (淨一個)
```
`/api/health` 嘅 `vision_pool` 而家係 `["minimax-text-01"]`,
`api_keys_present` 冇 `pplx` 咗。

### 2. 刪走嘅 function(淨 -281 行)
| function | 原本做乜 | 而家 |
|---|---|---|
| `_perplexity_vision()` | 第 2 個視覺供應商 | 刪走 |
| `_pplx_enrich()` | 12 個營養 field 嘅第 1 份 estimate | 刪走 → 淨 MiniMax 一份 |
| `_pplx_check_narration()` | MiniMax 死時嘅 narration 判斷後備 | 刪走 → fail closed |
| `_pplx_extract_dish_name()` | MiniMax 死時嘅菜名抽取後備 | 刪走 → 空 + 硬攔截 |
| `_pplx_api_key()` | 讀 `PPLX_API_KEY` | 刪走 → **唔會再有任何 HTTP 請求打去 api.perplexity.ai** |

### 3. cheer 嘅 3 個 web-search context 唔會失效
原本新聞 / 基金 fact / 利物浦賽程靠 Perplexity sonar-pro 上網。拎走之後
新增:
- `_brave_web_search(query, count)` —— Brave 網頁搜尋(key 已經有)
- `_cheer_web_fact(search_query, ask, ...)` —— Brave 搜尋 + MiniMax 撮一句

仍然守住「**唔准 fabricate**」:搜尋冇結果就回 `""`(cheer 少一塊 context,
好過亂講);MiniMax 只准用搜尋結果入面出現過嘅事。

### 4. coach tips
第一遍(form cue + progression)由 Perplexity sonar-pro 改用 MiniMax-M3,
第二遍(render 做 cheer 教練訊息)不變。

### 5. 2nd opinion:剩返同一個 model 行第二輪
chain 本身用 **soft preference**(唔係 hard exclusion),所以剩 1 個供應商
時 2nd opinion 仍然出到 —— 但佢係「同一個 model 問兩次(唔同 prompt)」,
**唔係兩個獨立意見**。總好過冇第二輪(詳細描述可以帶出第一輪漏咗嘅細節)。

### 6. QA
- `tests/qa/test_ai_provider_fallback.py` 重寫成 MiniMax-only 版(14 check),
  新增兩條硬 guard:
  - `gym_web.py` 唔准再有任何 `api.perplexity.ai` 呼叫
  - 唔准再讀 `PPLX_API_KEY`
  - 結構性 guard:prompt 砌好之後嘅每個 `return ""` **必須有 log**
    (冇後備 = 失敗一定要響,唔可以靜靜哋吞掉)
- `tests/qa/test_vision_failure_guard.py` 加 `test_perplexity_fully_removed`
- `tests/qa/test_generic_name_guard.py` 嘅 pool 斷言改成「淨 MiniMax 一家」
- 新增 `tests/qa/test_repo_hygiene.py`(10 check,pytest 風格):唔准 detached
  HEAD、唔准孤兒 commit、核心 code 檔要同已提交版本一致、版本號一致、
  唔准多餘 worktree、寫入閘同平行安全網仍然在、唔准有 Perplexity 依賴
- 前端 5 處「pplx + MiniMax」字眼改成「AI」

**QA:14/14 組 PASS,meta 35/35。**

### 跟進修正(同一個 v3.3.34 發布窗口)

- **`9c18089` / `b8cbecd`(Torres)**:前端「✓ 已寫入 log」badge 之前淨 gate 喺
  `item.preview`,所以 server 回 `ok=false`(Rule 25 攔截、相片冇入賬)嗰陣,
  張卡會**同時**見到紅色「失敗」pill + 綠色「✓ 已寫入 log」—— 等於話「扮成成功」。
  而家每個 `item.status`(`committed` / `failed` / `skipped`)都有自己嘅 badge。
- **`e0d6356`**:修兩個 QA flaky/過嚴斷言
  - `test_no_test_pollution.py` 之前淨喺 token 欄**缺失**時先 refresh,可能攞到
    **已過期**嘅 token → Sheets API 401 → 誤報「有污染」(實際 production 乾淨)。
    而家會睇 `expires_at`,過期即 refresh,401 都會再 refresh 重試,原子寫回 token 檔。
  - `test_repo_hygiene.py` 原本要求核心 code 檔唔可以有未提交改動 —— 但 repo 係
    多個 agent 共用(Torres / Frimpong 會同時改檔),工作區有改動係正常狀態。
    改成比對「**行緊嘅 server 版本 == 工作區版本**」,真正要保證嘅嘢。

### ⚠️ 單一供應商 = 單點故障(要知風險)
睇 `fitness/jim-image-recognition` Rule 28。現況:
- MiniMax 死 → 掃描被 rule-2 / rule-25 **硬攔截**(唔會寫「未識別菜式」
  入表,Jim 見到失敗提示而唔係壞資料)✅
- 但成個食物 log 會停,第二個意見冇咗,營養 estimate 冇咗 median-merge
- 想加返第二個 provider → 改 `_VISION_PROVIDER_POOL` 一行就得

## v3.3.33 — 2026-10-21 — AI 背景識別 + 平行 AI worker(上載完即刻可以繼續用)

Jim OOB 2026-10-21:
> After upload, ai food recognition can be done in background
> And ai work can be done in parallel to save queuing time too

### 起因 — 兩層「等」

**第一層(前端)**:舊流程 `await this._scanRecognisePhase(...)` —— 上載 bar 100% 咗,
但個 app 仲要等**全部 AI 做完**先返回。相機掣 disable 住,Jim 三十秒乜都做唔到。

**第二層(server)**:`_scan_run_job` 刻意 sequential(舊註釋寫住
「MiniMax rate-limit if run in parallel」)。但 AI stage 全部 network-bound
(MiniMax 視覺 → 第二意見 → 營養估算 → 交叉核對 → 品牌表 → Drive 上載),
一張相要 60-120 秒。4 張相 = 排隊 4-8 分鐘。

### 1. 背景識別(前端)

* 新增 `_scanStartBgRecognition()`:開完 job **即刻 return**,唔再 await 個 poll。
* 舊 background job 用 `scanQueueToken` 判斷仲屬唔屬於當前 scan ——
  舊 job 唔會覆蓋新 scan 嘅 queue(舊版靠 array reference,但
  `this.scanPhotosQueue = [...]` 每次都換新 array → 根本唔 work)。
* 多個背景 job 並行時,只有「顯示中」嗰個 job 寫進度條(`scanBgDisplayJobId`)。
* 搞掂之後 flash 通知 + 自動 refresh 食物記錄,Jim 唔使手動 reload。
* 相簿掣唔再因為 `recognising` / `uploaded` 狀態 disable —— 淨係真係仲喺度
  上傳(`pending` / `uploading`)先 block。

### 2. 平行 AI worker(server)

* `_scan_run_job` 改用 `ThreadPoolExecutor`,預設 3 個 worker
  (`GYMBRO_SCAN_PARALLEL` 可調,上限 8)。
* 進度百分比改用 high-water mark —— 單調遞增,唔會倒退。
* stage label 改用平行版(`排緊 AI 隊…(已完成 1/4)`)——
  唔會再講「第 n 張」而實際上有三張同時進行緊。
* `/api/scan_progress` 新增 `completed` + `inflight`(邊啲張仲喺度做)。

### 3. 🔴 兩條安全線(平行唔代表可以亂寫)

* **Sheet 寫入仍然 serialized** —— 新增 process-wide `_SHEET_WRITE_LOCK`
  (`threading.RLock`)+ `@_serialise_sheet_write` decorator,掛喺
  `_append_to_sheet_nutrition()`。Token refresh、idempotency store 讀改寫、
  trim pre-flight 全部唔係 concurrency-safe,平行會撞爛 golden copy。
  AI 係慢嘅部分,Sheet 寫入只係 1-2 秒 → 整體快 N 倍而資料唔會花。
* **結果對應用 client_index,唔用 array 位置** —— 平行之下 results 唔保證
  順序,舊嘅 `uploads[ri]` 位置對應會寫錯人嘅記錄。前端 + server 兩邊都改咗。
* **同分鐘 + 同卡路里唔等於重複** —— 舊 dedup 會當同一碟 → 漏寫一行落 golden
  copy。而家加多一重:兩行都有相片 URL 而且唔同 → 一定唔係重複,照寫。

### 4. 實測捉到嘅 3 個 bug(唔跑真實 AI 係捉唔到)

| Bug | 症狀 | 修法 |
|---|---|---|
| `done_count += 1` 冇 `nonlocal` | `UnboundLocalError` → **每一張相都變 failed** | 加 `nonlocal done_count` |
| `_scan_jobs_persist` 固定 tmp 檔名 + 喺 lock 外面讀 dict | 兩個 worker 寫爛同一個 tmp;讀 dict 途中被改 → `dictionary changed size during iteration` | 獨立 `_SCAN_JOBS_PERSIST_LOCK` + 喺 `SCAN_JOBS_LOCK` 下面影 snapshot + 唯一 tmp 名 |
| 舊 dedup 當「同分鐘同卡路里」= 重複 | 平行模式兩碟嘢啱啱好同卡路里 → **漏寫一行** | dedup 加相片 URL 比對 |

### 5. ⚠️ 順帶發現:Perplexity key 額度用完

實測時 log 見到:
```
[vision] Perplexity sonar failed: HTTPError: HTTP Error 401: Unauthorized
{"error":{"message":"You exceeded your current quota, add credits at
 https://console.perplexity.ai/project/billing.","type":"insufficient_quota"}}
```
`/home/work/.hermes/.env` 嘅 `PPLX_API_KEY` 而家**冇額度**。
Vision pool 會自動由 MiniMax 頂上(實測 `minimax-text-01 served after 1 failure(s)`),
即係**唔會壞**,但交叉核對同第二意見冇咗 —— 12 個營養欄而家只靠 MiniMax 單邊。
需要 Jim 去 console 加 credits 或者換新 key。

### 6. 實測 A/B(dryrun 模式,零 production 影響)

用 Jim 自己 2026-10-21 15:42 嗰批 4 張午餐相(真實食物相)行真實 AI:

```
                        ┌──────────────┬──────────────┬─────────┐
                        │ 模式         │ 4 張相行費時  │ 結果    │
                        ├──────────────┼──────────────┼─────────┤
                        │ 排隊(舊)     │ 308.9s(5:09) │ 4 張    │
                        │ 平行(新,3 個) │ 147.4s(2:27) │ 4 張    │
                        └──────────────┴──────────────┴─────────┘
→ 快 2.1 倍(慳 161.5 秒),0 errors
```

**關鍵佐證**:平行模式嘅 results 順序係 `0, 2, 1, 3`(**亂序**!)——
如果仲用舊嘅 `uploads[ri]` 位置對返,會寫錯人嘅記錄。
今次改用 `client_index` 對返,4 張全部對啱。


### 7. QA

新增 `tests/qa/test_background_parallel_scan.py`(17 個**靜態** check)——
背景唔准 block、平行 worker 存在、Sheet 寫入必須 serialized、
結果必須用 client_index 對返、`nonlocal` 陷阱、persist lock、
同卡路里唔可以當重複、統一寫入閘未被搞走。

### 8. Frimpong 動態測試再捉到 3 個問題(全部已修)

Frimpong 收到 A2A 通知後,自己寫咗 `tests/qa/test_v3333_dynamic.py`
(12 個**真動態**測試:真起 thread、真跑 parallel、真令 AI stage 亂序完成)。
佢捉到我自己冇捉到嘅 3 個問題:

| # | 問題 | 症狀 | 修法 |
|---|---|---|---|
| 1 | **進度百分比公式搞錯 scale** | `((done_count + stage_frac) / total * 100)` —— `stage_frac` 係 0-100 嘅 stage 百分比,唔係張數。`stage_frac=12` 計出 600% → bar 即刻跳到 99% 然後**成個 job 停喺 99%**(實測 0 → 99 喺 2.8 秒,然後 pin 住 110 秒)。呢個正正係 Jim 禁止嘅「假進度」。 | 改用 SPEC §2.3 公式:`raw = (done_count * 100 + stage_frac) / total` —— 完成嘅相每張計 100,進行緊嘅 stage 貢獻**一張**相嘅 0-100 切片 |
| 2 | **decorator 唔可以被測試換內層** | `@_serialise_sheet_write` 直接叫 closure 嘅 `fn`,QA 換唔走內層實作 → 動態驗證「序列化有效」做唔到 | wrapper 經 `getattr(_wrapped, "__wrapped__", fn)` 間接叫內層(預設行為完全一樣) |
| 3 | **`completed` 漏計失敗嘅相** | 收尾用 `completed=len(results)`(只計成功)→ 一張相失敗,前端個「(已完成 x/y)」label **永遠到唔到 100%** | 改用 `completed=done_count`(已處理 = 成功 + 失敗) |

> 呢個係「靜態 source 檢查」嘅盲區:我嗰 17 個 check 全部係 `assert "ThreadPoolExecutor" in gw`
> 呢類字串檢查 —— 證明到「有呢段碼」,證明唔到「行得到」。
> Fimpong 嘅動態測試先捉到上面 3 個。

新增 `tests/qa/test_v3333_dynamic.py`(12 個**動態** check,Frimpong 寫)——
真起 thread、真跑 parallel、真令 AI stage 亂序完成:
平行真係同時行、client_index 對應、pct 單調遞增、completed/inflight 正確、
**Sheet 寫入真係 serialized**(兩個 thread 同時寫唔會撞爛)、
一張相爆咗唔會連累其他相、前端上載完即刻交還控制權。


## v3.3.32 — 2026-10-21 — 文字輸入流程大修:餐廳自動填、食物相優先、品牌 cover

Jim OOB 2026-10-21:
> I want you to fix the manual food input workflow. It has been very buggy.
> Can you fill in the restaurant (in best effort basis) based on what I inputted.
> Moreover, the food image capture should be related to the food. Secondary
> option is the restaurant. Sometimes, I would input Protein drinks and brand.
> You research it online and put the brand cover as food image

### 起因 — production 餐廳欄 98% 係 AI 嘅半句說話
實測 `Nutrition` tab(1645 行):餐廳欄只有 21 個真店名,198 個係 AI 廢話
```
難以辨認餐廳 ×45   描述可靠識別餐廳 ×13   睇唔到餐廳 ×12   冇見到餐廳 ×12
一間香港連鎖餐廳 ×10   相片可靠識別餐廳 ×5   所以餐廳 ×2
「如果你係做餐廳」「碟炸蔬菜係喺餐廳」「楚具體係邊間餐廳」「景係一個露天餐廳」
```
原因:舊流程由 AI 直接填 `restaurant_guess`,**冇任何消毒步驟**就寫入
golden copy。

### 1. 餐廳自動填(優先次序固定,全部有 source)
新增 `gym_web/food_context.py`(新模組):
```
品牌 → 文字提到 → 上次食過同一碟(歷史索引) → AI 猜(經消毒) → 留空
```
* **品牌**:自然寫法「Optimum Nutrition Gold Standard」而唔再係淨收
  「on gold standard」縮寫。alias 層把 `optimum nutrition`→`Optimum Nutrition`、
  `my protein`→`MyProtein`、`muscle tech`→`MuscleTech`、
  `quest nutrition`→`Quest Nutrition`、`dymatize iso 100`→`Dymatize`、
  `iso 100`→`Dymatize` 正規化。
* **文字提到**:輸入「星巴克拿鐵」→ 餐廳「星巴克」。
* **歷史索引**:同一碟菜上次食過嘅店。相似度**收緊咗**(見下)。
* **AI 猜**:經消毒器,AI 廢話 → 留空(唔會發明)。

### 2. 餐廳名消毒器
`sanitise_restaurant_name()` 攔截:AI 自我描述(難以/可靠/描述/相片/未見到…)、
不肯定假設(一間/任何/應該/所以/如果/邊間/連鎖/屬於…)、散文式(睇落/好似/以下為/
採用/判定/足夠/典型…)、泛用場所字尾(餐廳/連鎖/茶餐廳/小店,只限 ≤6 字,
**避免誤殺「新羅寶韓國餐廳」**)、純場所類型(居酒屋/酒樓 — 完全一樣先算)、
純菜系(韓式燒肉 / Hainanese chicken rice shop)。
> 全部規則由 production 實測垃圾值反推,唔係憑空想像。

### 3. 食物相優先 → 餐廳相次選 → 品牌 cover
新增 `gym_web/food_image.py`(新模組):
* **食物相優先**:用「最終菜名 + 最終餐廳」砌 query(餐廳名已喺菜名入面就唔重複 prepend)。
* **餐廳相次選**:排喺最後,只有搵唔到食物先用。
* **品牌 cover**:蛋白飲品/蛋白棒 → 先查官方網域 og:image,再 fallback 官網直連
  `https://www.{domain}/`。實測「Optimum Nutrition Gold Standard」攞到官方產品圖
  `optimumnutrition.com/cdn/shop/files/US_GSW_2LB_EXTMLKCHOC_FOP.png`。
* **相關性驗證**:每張候選相都要經視覺驗證先至用;舊版淨係攞 Brave 第一個結果
  (實測舊版「Optimum Nutrition Gold Standard」→ iStock stock 圖)。
* **stock 網封鎖**:istockphoto / shutterstock / getty 唔會被 download。
* **query 唔重複**:「星巴克拿鐵」唔會變成「星巴克 星巴克拿鐵」。

### 4. 統寫入閘 `_sheet_write_guard`(唯一 production writer)
**最關鍵嘅修正**。實測 2026-10-21 13:52(HKT):Jim 自己 13:16 影嘅午餐盒相,
AI 認唔出,相片路徑嘅 **scan job 自動 commit** 直接寫咗入 golden copy:
```
餐名 =「黑金圓形餐盒內食物（無法識別）」  餐廳 =「描述可靠識別餐廳」  營養 = 0/0/0/0
```
相片路徑**唔經** `/api/scan_commit`,所以之前放喺 commit endpoint 嘅消毒器
完全冇覆蓋到。而家防線放喺 `_append_to_sheet_nutrition`:
* 泛用 / 認唔出嘅名(未識別/無法識別/無法辨認/睇唔到/唔知係咩…)→ **唔寫**
* AI 廢話餐廳 → **清空**
* 4 大營養素全 0 → 先試 Sheet 歷史 median 補,補唔到就 **唔寫**
  (白開水/無糖茶等真係 0 卡嘅嘢除外)

### 5. 文字輸入穩定性
* **0 kcal 重試**:MiniMax 偶爾回傳空/壞 JSON(實測「星巴克拿鐵」一次 0 kcal),
  而家重試一次,再失敗就由 Sheet 歷史 median 補,唔會靜靜哋寫 0 入表。
* **品牌產品名要穩定**:同一個「Optimum Nutrition Gold Standard」AI 一次出
  「金標乳清蛋白粉」(啱),另一次出「雞胸肉」(完全唔啱)。而家品牌產品用返
  Jim 自己打嘅字,除非 AI 個名夠具體。
* **產品但冇品牌 → 餐廳留空**:唔會亂填另一個品牌(實測舊版「whey protein
  chocolate」被填成「Quest Nutrition」,但 Jim 根本冇講過 Quest)。

### 6. 歷史相似度收緊
舊 fuzzy 門檻係「重疊 bigram 數 ≥ 2」→ **「乳清蛋白粉」會撞到
「Dim sum dinner (8 dishes, Jim 60%)」**(實測 score 2.0 剛好過門檻)。
而家三級:完全一樣 1.00 / 一個係另一個嘅子字串 0.95 / 英文 token 重疊 ≥2 且
bigram ≥0.5。「chocolate cake」亦唔會再撞「chocolate peanut butter bar」。

### 7. 前端
* **一句 toast**(舊版連續三句,最後一句會蓋走「搵到食物相」嘅訊息):
  `✨ 已估算 (kcal 720, P 28) · 食物相✅已驗證(img-global.cpcdn.com) — 檢查再 ✓ log`
* 餐廳來源標籤:🏪 餐廳來源: 品牌 / 你輸入提到 / 上次食過同一碟 / AI 猜
* 相片標籤:🖼 品牌封面 / 餐廳相 / 食物相 · 來源 · ✅驗證相符 / ⚠未驗證 · query

### 8. Production 資料清理(已備份,可 100% 還原)
* 刪走 2026-10-21 13:16 嗰行 AI 廢話(0 kcal + 無法識別 + 餐廳廢話)。
* 清走 198 行歷史 AI 廢話餐廳值(只動 column E,其他欄一律唔掂)。
  備份:`scan_cache/restaurant_cleanup_backup.json`
* 清理後:**餐廳欄垃圾 0 行、『未識別菜式』0 行**,剩低 21 個相異值全部係真店名。

### 9. 測試
* `tests/qa/test_manual_food_input.py`(新,145 個測試)—— 消毒器 / 品牌 alias /
  歷史索引 / 圖片策略 / 統一寫入閘 / 前端 toast 數量
* `tests/qa/check_production_clean.py`(新)—— production 污染檢查
* `tests/qa/clean_restaurant_column.py`(新)—— 餐廳欄清理(預設 dryrun)
* QA suite:**241 passed / 4 skipped**

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
