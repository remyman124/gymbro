# Gymbro v3.3.44 — Mobile PWA 強化規格(Push / Geolocation / Background Sync)

> 交付者:Frimpong(Claude Code CLI)。Alonso 負責 HTTPS、VAPID 金鑰、重啟同最終驗證。
> **本規格係唯一真實來源。唔准發明 API 名字、唔准改 food-scan / Sheet 寫入路徑。**

## 0. 現況(已核實,唔使再猜)

| 項目 | 實情 |
|---|---|
| Repo | `/home/work/projects/gymbro`,branch `main`,git commit `43d4ed8`,版本 `3.3.43` |
| Monolith | `gym_web.py` = 16,297 行 / 773 KB |
| UI | `templates/index.html` = 321 KB(Alpine.js + Tailwind,全部 inline) |
| Service Worker | `gym_web.py:16011` 有一個 Python 字串常數 `SERVICE_WORKER = """..."""`,`@app.route("/sw.js")`(行 16254)直接吐返佢 |
| Manifest | `@app.route("/manifest.json")`(行 2707),已經有 name/short_name/start_url/display=standalone/theme_color/icons(192/512/apple-touch-icon 喺 `static/`) |
| 執行方式 | `cd /home/work/projects/gymbro && env PYTHONPATH='' PYTHONUNBUFFERED=1 GYMBRO_SCAN_PARALLEL=3 /usr/bin/python3 gym_web.py`(tmux session `gymbro`,log → `/home/work/.hermes/logs/gymbro_v3.3.43.log`) |
| Python | `/usr/bin/python3` = 3.13.7,`pywebpush 2.5.0` **已經裝好**(import OK),`cryptography` 50.0.1,`flask` 3.1.3,`requests` 2.33.0,`pytest` 9.1.1 |
| 公開 HTTPS | `https://alonso.tail9e2b74.ts.net/`(tailscale serve → `http://127.0.0.1:7000`)✅ 已通,HTTP/2 200 |
| 舊 HTTP | `http://100.114.66.125:7000/`(Tailscale IP,無 TLS)—— **Web Push / Geolocation / Service Worker 喺呢個 origin 唔會 work**,所以 Jim 手機上要改用 HTTPS 網址 |
| VAPID 金鑰 | `/home/work/.hermes/gymbro_vapid.json`(mode 0600,repo 之外)→ `{"public_key": "<base64url>", "private_key": "<base64url>"}` **private key 絕對唔准入 git** |
| 已知 app 內文檔 | `templates/index.html` 行 5783 已經有 `navigator.serviceWorker.register('/sw.js')`;index.html 行 5 已有 `viewport-fit=cover`;已經有「PWA install nudge banner」(7 日 localStorage gate,platform-aware)—— **唔好重複造一個** |

### 為咗做到 push / geo / background sync,必須 HTTPS
Service Worker、Web Push、Geolocation 全部要求 secure context。HTTPS 已經由 tailscale serve 提供 ✅,前端唔使自己搞 certificate。

---

## 1. 交付物 A — `gym_web/push.py`(新檔)

純函式、import 時唔可以有副作用、唔好因為 pywebpush 缺失就炸 import。

```python
VAPID_PATH = "/home/work/.hermes/gymbro_vapid.json"

def load_vapid() -> dict | None      # 讀上面個 JSON;任何錯誤 → return None(唔好 raise)
def vapid_public_key() -> str        # base64url public key;冇就 return ""
def save_subscription(sub: dict) -> dict   # 寫入 push subs store,endpoint 做 key,atomic write
def list_subscriptions() -> list[dict]
def remove_subscription(endpoint: str) -> bool
def send_push(sub: dict, payload: str, ttl: int = 86400, urgency: str = "normal") -> tuple[bool, str]
    # 用 pywebpush.webpush(subscription_info=sub, data=payload, vapid_private_key=..., vapid_public_key=..., ttl=ttl, urgency=urgency)
    # 失敗要 return (False, str(e)) —— 唔好 raise 出去炸 caller
```

儲存位置(可以喺 runtime 用 env override,方便測試):
- `GYMBRO_PUSH_STORE` env → 預設 `/home/work/.hermes/gymbro_push_subs.json`
- 格式:`{"subs": [{"endpoint": "...", "keys": {"p256dh": "...", "auth": "..."}, "created": "<iso>", "last_ok": <ts>, "last_error": ""}]}`

寫入用 `gym_web/core.py` 嘅 `safe_write_json`(已有 atomic write + fail-open)。**唔好自己寫 `except: pass`**。

---

## 2. 交付物 B — `gym_web/geo.py`(新檔)

```python
GEO_STORE_ENV = "GYMBRO_GEO_STORE";  預設 /home/work/.hermes/gymbro_geo.json

def log_location(lat, lng, accuracy=None, context="manual", label="") -> dict
    # 記錄 {ts, lat, lng, accuracy, context, label};上限 200 條(丟最舊)
def recent_locations(n=20) -> list[dict]
def reverse_geocode(lat, lng) -> str
    # best-effort:Nominatim https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=..&lon=..
    # timeout 3 秒,User-Agent 必須有(例如 "gymbro-pwa/1.0 (jim@local)")
    # 結果 cache 24 小時;失敗 → return ""(絕對唔准 fabricate 地名)
def haversine_km(a_lat, a_lng, b_lat, b_lng) -> float
def nearest_saved(lat, lng) -> dict | None   # 最近一筆有 label 嘅紀錄 + 距離
```

---

## 3. 交付物 C — `gym_web.py` 新增 route(純 additive)

全部喺現有 PWA route 附近(行 2707 嘅 `pwa_manifest` 附近或檔尾都行,但要易搵)。每個 route 都要 fail-soft:任何例外都 `print(...)` 出 traceback,然後 return `jsonify({"ok": False, "error": "..."})`,**唔好 500 死機**。

| Route | Method | Body / Query | Response |
|---|---|---|---|
| `/api/push/vapid_public_key` | GET | — | `{"ok": true, "public_key": "<base64url>"}` |
| `/api/push/subscribe` | POST | `{"endpoint": str, "keys": {"p256dh": str, "auth": str}}` | `{"ok": true, "count": n}` |
| `/api/push/unsubscribe` | POST | `{"endpoint": str}` | `{"ok": true, "removed": bool}` |
| `/api/push/status` | GET | — | `{"ok": true, "count": n, "vapid_ready": bool}` |
| `/api/push/send` | POST | `{"title": str, "body": str, "url": str = "/", "tag": str = "gymbro"}` | `{"ok": true, "sent": int, "failed": int, "errors": [str]}` |
| `/api/geo/log` | POST | `{"lat": float, "lng": float, "accuracy": float?, "context": str?}` | `{"ok": true, "label": str, "distance_km": float?}` |
| `/api/geo/recent` | GET | `?n=20` | `{"ok": true, "locations": [...]}` |

規則:
- **驗證輸入**:`lat` 必須 -90..90、`lng` 必須 -180..180,否則 `{"ok": false, "error": "bad coords"}`。
- `endpoint` 長度上限 2048;`title`/`body` 上限 200 字;`payload` 唔好超過 4 KB。
- `/api/push/send` 收到 push service 回 404/410 → 順手剷走嗰條 subscription(失效嘅 subscription 清走)。
- 任何 response 都**唔可以**包含 VAPID private key。
- `/api/push/send` 冇 subscription → `{"ok": true, "sent": 0}`(唔好 error)。
- 呢啲 route 喺 tailnet-only host 上,但都要有基本長度/型別檢查。

---

## 4. 交付物 D — Service Worker(`gym_web.py` 嘅 `SERVICE_WORKER` 常數)

**只准 ADDITIVE,唔准改現有 fetch/caching 邏輯**(`v137` 已經係 `/api/*` network-first,唔好搞砸)。改動:

1. `push` event:
```js
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { title: 'Gymbro', body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'Gymbro', {
    body: d.body || '', icon: d.icon || '/static/gymbro_icon-192.png',
    badge: '/static/gymbro_icon-192.png', tag: d.tag || 'gymbro',
    data: { url: d.url || '/' }, renotify: false
  }));
});
```
2. `notificationclick`: focus 現有 client(如果 url 相同)否則 `openWindow(url || '/')`。
3. `message` event:`{type:'SKIP_WAITING'}` → `self.skipWaiting()`;`{type:'FLUSH_QUEUE'}` → `flushQueue()`。
4. `sync` event(tag `gymbro-queue`)→ `e.waitUntil(flushQueue())`。
5. IndexedDB helper(`gymbro-offline` / store `queue`,keyPath `id`):
   - `queueRequest(record)`、`readQueue()`、`deleteQueueItem(id)`
   - `flushQueue()`:逐條 `fetch(url, {method, headers, body})` → 2xx 就刪;失敗就 `attempts+1`,超過 5 次就丟棄;完成後更新通知 badge 計數(`navigator.setAppBadge` 有就用)。
6. **唔好** register 任何外部 script/CDN。

---

## 5. 交付物 E — 前端 `templates/index.html`(additive)

Alpine.js 風格,唔好引入新 framework。mobile-first,tap target ≥ 44px,唔好用 `alert()`(用 app 現有 toast / inline 文字)。

### 5.1 通知(Push)
- Header 加一個細按鈕 `🔔`(state:`已開啟` / `未開啟` / `已封鎖`)。
- 流程:`Notification.requestPermission()` → `navigator.serviceWorker.ready` → `pushManager.subscribe({userVisibleOnly:true, applicationServerKey: <base64url → Uint8Array>})` → `POST /api/push/subscribe`。
- iOS 注意:iOS 16.4+ 先支援 Web Push,而且**必須係加咗到主畫面嘅 PWA**;所以喺 iOS 判斷出嚟而未 standalone 顯示時,提示「請先用 Safari 分享 → 加入主畫面,再開通知」。
- 唔好喺非 secure context(`!window.isSecureContext`)時亂試,直接顯示「需要 HTTPS」。
- `applicationServerKey` 要 base64url → Uint8Array(`Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')), c => c.charCodeAt(0))`)。

### 5.2 位置(Geolocation)
- 加一個 `📍` 按鈕(同 header 嗰排):`navigator.geolocation.getCurrentPosition(success, error, {enableHighAccuracy:false, timeout:10000, maximumAge:60000})` → `POST /api/geo/log {lat, lng, accuracy, context:'manual'}` → 顯示返 label(例如「諾富特酒店 · 距離上次 0.3 公里」)。
- 權限被拒 / 逾時 → 用文字講「定位失敗:權限被拒 / 逾時」,**唔准彈 confirm()**。
- 定位係**用戶主動撳**先執行(唔好自動攞)。

### 5.3 離線隊列 + Background Sync
- 將以下 POST 經過一個 wrapper `apiPost(url, body, {queueable:true})`:
  - `/api/log_set`(落 set)
  - `/api/cancel_last_set`
  - `/api/end_session`
  - `/api/whoop_refresh`
  - 食物文字輸入嗰條 endpoint(自己 grep 返,例如 `/api/scan_text` 或 `/api/manual_food`)—— 搵唔到就 skip,唔准亂加。
- network error(`TypeError: Failed to fetch` / offline)→ 寫入 IndexedDB queue + `navigator.serviceWorker.ready.then(reg => reg.sync.register('gymbro-queue'))`(冇 Sync API 就靠 `window.addEventListener('online')` + SW `message FLUSH_QUEUE`)→ toast「已離線,已排隊,恢復網絡後自動同步」。
- 成功回來之後順手 flush 一次舊 queue。
- **唔好** queue 住 scan_commit(圖片上傳太大)。

### 5.4 PWA manifest(`/manifest.json` route)
- 補 `id`("/")、`scope`("/")、`lang`("zh-HK")、`dir`("ltr")、`categories`、以及 `shortcuts`(2 個:`影相記錄` → `/?action=scan`、`落 set` → `/?action=logset`,各自 `icons` 用現有 `gymbro_icon-192.png`)。
- 唔好刪現有 field。

### 5.5 手機 layout
- 檢查 header/底部有冇 `env(safe-area-inset-bottom)`;冇就喺最外層 container 加 `padding-bottom: calc(env(safe-area-inset-bottom) + <現有值>)`,唔好整走現有 spacing。
- 唔好改現有 Alpine state 結構(其他 tab 靠 `x-data` 綁)。

---

## 6. 交付物 F — 測試(`tests/qa/`)

新增 `tests/qa/test_mobile_pwa_api.py`(跟現有 pytest 慣例,`from gym_web...` / app test client 寫法照抄 `tests/qa/test_set_logging.py` 或 `test_manual_food_input.py` 嘅做法):

- `GET /api/push/vapid_public_key` → 200 + `public_key` 非空(或至少 `ok` true)
- `POST /api/push/subscribe` 假 subscription → 200,之後 `GET /api/push/status` count == 1
- `POST /api/push/send`(假 endpoint,唔會真送到)→ 200,`ok` true,`sent` 0,**唔准 raise**
- `POST /api/geo/log` 合法座標 → 200 + label 欄位存在;非法座標(lat=999)→ ok false
- `GET /api/geo/recent` → 200 + `locations` 係 list
- **測試要用 monkeypatch 覆蓋 store 路徑**(用 `GYMBRO_PUSH_STORE` / `GYMBRO_GEO_STORE` env override 去 tmp_path),**唔准寫入真實 `/home/work/.hermes/gymbro_*.json`**
- ⛔ **測試絕對唔准寫入生產 Google Sheet `Nutrition` tab**(見 `tests/qa/test_no_test_pollution.py`)。任何 push/geo 測試都唔好觸碰 Sheet API。

---

## 7. 交付物 G — 文件

- `CHANGELOG.md` 加 v3.3.44 一段(寫清 push / geo / background sync / HTTPS URL)。
- `README.md` 加「手機使用」小節:建議用 `https://alonso.tail9e2b74.ts.net/` 加到 iPhone 主畫面(Web Push 需要)。
- `gym_web.py` 入面版本字串 bump 去 `3.3.44`(搵返現時 `3.3.43` 嘅位置,只改嗰個)。

---

## 8. Guardrails(違反 = 任務失敗)

1. ⛔ **唔准重啟 server**(Alonso 會自己重啟並做 5-step verification)。
2. ⛔ **唔准改任何 food-scan / vision / Sheet 寫入路徑**(`/api/scan_commit`、`/api/scan_preview`、`_append_to_sheet_nutrition`、`_minimax_vision`、`_extract_dish_name` 一律唔好掂)。
3. ⛔ **唔准寫入生產 Google Sheet**(任何測試資料、任何 row)。
4. ⛔ **唔准把 VAPID private key 寫入 repo / commit / log**。
5. ⛔ **唔准 `except: pass`** —— 每個 handler 都要 print 出 traceback(`print(f"[push] ... failed: {e}", flush=True)`)。
6. ⛔ 唔准改 `SERVICE_WORKER` 嘅 fetch/caching 部分。
7. ⛔ 唔准 `git push`(唔准 push 到 remote)。
8. ✅ 喺新 branch `feature/v3.3.44-mobile-pwa` 做,commit 落去,但唔好 merge。
9. ✅ 收工前一定要行:
   ```bash
   cd /home/work/projects/gymbro
   env PYTHONPATH='' /usr/bin/python3 -m py_compile gym_web.py gym_web/push.py gym_web/geo.py
   env PYTHONPATH='' /usr/bin/python3 -m pytest tests/qa -q
   ```
   (pytest 可能要 `-x` 收窄;如果 suite 本身有既存失敗,報告時要講清楚邊啲係既存、邊啲係你整出嚟)

## 9. 交報告格式

最後回覆要包含:
1. 改咗 / 新增嘅檔案清單(逐個一句話講做咗咩)
2. `git diff --stat`
3. `py_compile` + `pytest` 嘅實際輸出(唔准只寫「測試通過」)
4. 新 endpoint 嘅 curl 測試輸出(用 Flask test client 或者 `curl` 打 `127.0.0.1:7000` —— 注意:server 仲係舊 version,所以**唔好**打 7000,改用 test client 或者起一個 port 7777 嘅臨時 instance,測完 kill 掉)
5. 有冇做唔到嘅部分 + 原因
