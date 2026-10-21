# Gymbro Scan-Progress QA — 測試套件

Isak 2026-10-20 · 對應 `SCAN_PROGRESS_SPEC.md` §4

## 快速開始

```bash
# 全套 QA (約 2 分鐘)
bash /home/work/projects/gymbro/tests/qa/run_qa.sh

# 只跑某一組
bash /home/work/projects/gymbro/tests/qa/run_qa.sh --only template
bash /home/work/projects/gymbro/tests/qa/run_qa.sh --only contract
bash /home/work/projects/gymbro/tests/qa/run_qa.sh --only frontend

# 加埋會真跑 AI 嘅測試 (會 commit 落 Jim 個 Google Sheet!)
bash /home/work/projects/gymbro/tests/qa/run_qa.sh --allow-ai
```

## 點解唔直接 `pytest`?

兩個環境陷阱:

1. **`PYTHONPATH=""` 一定要加** — 否則會繼承 alonso 個 3.14 venv,
   PIL `_imaging.so` ImportError。
2. **gymbro `.venv` 一開始冇 playwright** — QA 已裝 `playwright==1.58.0`
   入 venv。chromium browser 已經喺 `~/.cache/ms-playwright/chromium-1208`,
   唔使重新 download。

所以正確 command:

```bash
cd /home/work/projects/gymbro
PYTHONPATH="" .venv/bin/python -m pytest tests/qa/ -v
```

## 檔案結構

| 檔案 | 對應 spec | 類型 | 需 server? | 需 AI? |
|---|---|---|---|---|
| `test_scan_progress_template.py` | §3 (frontend 靜態) | template 靜態層 | ❌ | ❌ |
| `test_server_freshness.py` | §4.4 (server 版本) | HTTP | ✅ | ❌ |
| `test_scan_progress_contract.py` | §4.1-4.3 (backend) | HTTP | ✅ | 部分 opt-in |
| `test_scan_progress_frontend.py` | §4.4 (browser 動態) | Playwright | ✅ | ❌ (用 route mock) |
| `pytest_baseline_diff.py` | §4.5 (baseline-diff) | 工具 | ✅ | ❌ |
| `build_baseline.py` | §4.5 (union baseline) | 工具 | ❌ | ❌ |
| `baseline_diff_meta_test.py` | §4.5 (驗證工具) | meta-test | ❌ | ❌ |
| `mock_backend_harness.py` | §4.1-4.3 (驗證測試) | meta-test | 自己起 | ❌ |
| `browser_frontend_harness.py` | §4.4 (驗證測試) | meta-test | 自己起 (7402) | ❌ |
| `run_qa.sh` | — | runner | ✅ | 選項 |

### Layer A — template 靜態層 (`test_scan_progress_template.py`)

直接讀 `templates/index.html`。唔使 server、秒級完成。
驗 Torres 嘅改動確實喺檔案入面:DOM hook、Alpine state 變數契約、
XHR 上傳進度、Phase 1 → Phase 2 嘅次序、冇假進度、冇英文 narrative term、
冇 Telegram button、transition duration 合理。

**用途**: Torres 每次 commit 都可以即刻跑,唔使等 gym_web restart。

### Layer B — live browser 動態層 (`test_scan_progress_frontend.py`)

Playwright headless 開真實 PWA (`http://127.0.0.1:7000/`),驗進度條喺
browser 入面真係 render 咗、真係識郁。

用 `page.route()` 攔截 `/api/scan_upload` 同 `/api/scan_progress/*`,
所以 **唔會真跑 AI、唔會 commit 落 Sheet**。

## Baseline-diff (spec §4.5)

gymbro suite 有多個 **pre-existing, env-dependent** failure
(live server、shared Redis `gymbro:overlay:health`、stale `gym-web-v121` assert)。
baseline-diff 先算 regression,唔會誤報。

```bash
# 產生 / 更新 baseline
PYTHONPATH="" .venv/bin/python tests/qa/pytest_baseline_diff.py --write

# 對比 (exit 1 = 有 regression)
PYTHONPATH="" .venv/bin/python tests/qa/pytest_baseline_diff.py --pytest-args --ignore=tests/qa

# 模式 B:用已經 capture 好嘅 pytest 輸出做比對(唔重跑 suite,慳記憶體)
PYTHONPATH="" .venv/bin/python tests/qa/pytest_baseline_diff.py --from-file /tmp/verify_run2.txt
```

**兩種模式嘅分別:**

| | 模式 A | 模式 B (`--from-file`) |
|---|---|---|
| 會唔會跑 pytest | 會 | **唔會** |
| 記憶體 | 高(整套 suite + chromium) | 近乎零 |
| 幾時用 | 淨係做一次全面驗證 | 反覆比對、debug 工具邏輯 |
| 輸出唔完整 | 會 detect → exit 2 | 會 detect → exit 2 |

**建議工作流:** 先用模式 A capture 一次(記低輸出檔),之後用模式 B 反覆比對。
咁樣可以避開「跑多過一次就 OOM」嘅問題。

```bash

# 驗證工具自己嘅判定邏輯 (合成輸出,唔使真跑 pytest)
PYTHONPATH="" .venv/bin/python tests/qa/baseline_diff_meta_test.py
```

Output 分三類:

- **SAME** — baseline 已知嘅 pre-existing failure,唔算 regression
- **GONE** — baseline 有記錄但今次 pass (修好咗)
- **NEW** — 今次 fail 但 baseline 冇記錄 → **真 regression → exit 1**

### ⚠️ 兩個一定要知嘅陷阱 (都係實測撞到先加落嚟)

**A. Interpreter fingerprint — 唔好用唔同嘅 python 比**

baseline 檔會記低產生佢嗰陣用嘅 interpreter (`fingerprint.python`)。
如果今次用嘅 python 唔同 → 結果**唔完全可比**,工具會出
`⚠️ WARNING` 而唔會靜靜哋報一堆假 regression。

> 我第一次就係踩咗呢個坑:baseline 用 `/usr/bin/python3` (3.13.7) 做,
> 跟住用 `.venv/bin/python` (3.11.15) 跑 diff → 報 7 NEW / 7 GONE,
> 對稱互換 —— 典型嘅環境差異假警報,唔係真 regression。

**B. Flaky e2e test → 要用 union baseline**

`tests/e2e/*` 有一批 test 用 `wait_until="networkidle"` + 10 秒 timeout。
gymbro PWA 有持續 polling,`networkidle` 永遠唔 settle → 呢批 test
**時而 pass 時而 fail**(純競態)。單次 run 嘅 baseline 捉唔齊。

→ 用 `build_baseline.py` 建 **union baseline**:任何一次 run fail 過嘅 test,
永遠當 pre-existing。

```bash
# 先跑 N 次 pytest,輸出各自存檔
for i in 1 2; do
  PYTHONPATH="" .venv/bin/python -m pytest -p no:randomly \
    --continue-on-collection-errors -q --tb=no -rfE --ignore=tests/qa \
    > /tmp/cap_run$i.txt 2>&1
done

# 合併成 union baseline
PYTHONPATH="" .venv/bin/python tests/qa/build_baseline.py \
  --out tests/qa/pytest_baseline.json /tmp/cap_run1.txt /tmp/cap_run2.txt
```

**驗證呢啲 flaky 唔關今次改動事**:用 `git stash` 將 template 還原到
改動前,再跑同一批 test —— 一樣 fail → 證明係 pre-existing。

**C. Baseline 檔自己嘅 overlap**

同一個 flaky test 喺唔同 run 可能一次 FAILED、一次 ERROR。Union 完之後
同一個 id 會同時出現喺 `failed` 同 `errored` → 計 SAME 時重複計數。
`build_baseline.py` 會自動偵測並統一歸入 `failed`(已驗證:重建後
`sum == union`,overlap = 0)。`pytest_baseline_diff.py` 讀到有 overlap
都會出提示。

**D. Capture 污染防護**

如果產生 capture 嗰陣冇加 `--ignore=tests/qa`,capture 入面會混入 QA
自己嘅測試 id。`build_baseline.py` 會自動偵測並排除:

```
⚠️  以下 capture 入面混入咗 tests/qa 自己嘅測試 id,已自動排除:
       gymbro_baseline_raw.txt: 6 條
```

唔排走嘅話,QA 自己嘅測試失敗會被當成 suite 嘅 known failure,
將來真 regression 都唔報。

**E. Flaky test 嘅捕捉靠「唔同負載」**

有啲 test 喺 server 忙時 fail、server 閒置時 pass:

```python
# 連續跑 3 次 → 3 failed
# 隔開跑 5 次 → 5 passed
```

原因係 PWA 持續 polling,server 忙時 `networkidle` 喺 10 秒 timeout
內唔 settle。

→ 所以 baseline capture 唔應該全部喺「server 閒置」狀態下跑。
→ 理想做法:baseline capture 時**同時**跑其他嘢(製造負載),
或者分開「有負載」同「冇負載」兩種狀態各跑幾次。

實測:`test_schedule_tab_renders` 就係靠第 4 個 capture(撞正我
同時跑緊嘢)先捉到。union 由 34 → 35。

---

## ⚠️ 改 template 之後一定要 restart

`HTML_TEMPLATE` 喺 module import 時讀入記憶體,所以 **Torres 每次改
`templates/index.html` 都必須 restart `gym_web.py`**,新先生效。

實測見證(本次 session 內發生咗兩次):
```
15:01  server restart → 新進度條上線
15:32  Torres 又改 template (+8 chars: aiRewritePreview?.macros || {})
       → served 293181 vs disk 293189 → 又 stale
```

`test_server_freshness.py` 會捉到。注意:**唔好**用 `.strip()` 以外嘅
方法去「放寬」呢個檢查 —— 之前我因為 trailing newline 加咗 `.strip()`,
但 Torres 真正嘅內容改動差 8 個字元,仍然捉得到。

## 副作用控制

| 測試 | 副作用 | 控制方法 |
|---|---|---|
| `test_scan_upload_*` | 寫檔入 `SCAN_CACHE_DIR` | 安全 — scan cache 本來就係 cache |
| `test_scan_job_progress_is_monotonic` | 真跑 AI + commit 落 Sheet | `GYMBRO_QA_ALLOW_AI=1` 先行 |
| `test_scan_preview_response_keys_unchanged` | 真跑 AI + commit 落 Sheet | `GYMBRO_QA_ALLOW_AI=1` 先行 |
| `test_upload_bar_shows_real_byte_progress` | 冇 (route mock) | 安全 |
| `test_ai_bar_stage_label_is_populated` | 冇 (route mock) | 安全 |

**Fixture 相** (`/home/work/projects/gymbro/test_fixtures/`):
`food_1.jpg` (4.4 MB)、`food_2.jpg` (3.7 MB) — 真實食物相;
`static_test.jpg` — fallback。

**SCAN_CACHE_DIR** = `/home/work/.hermes/scan_cache/` (唔係 gymbro 目錄下面)。
路徑安全檢查測試用嘅係呢個真實路徑。

---

## 測試方法上嘅六個陷阱(實測撞到,唔睇就會寫錯測試)

### 1. Service worker 會 bypass `page.route()`

gymbro PWA 有 `sw.js`。**Service worker 會直接轉發 request,Playwright 嘅
route handler 永遠唔會觸發。**

實測:
```
# 預設(有 SW):
route hits: []

# service_workers='block':
route hits (sw blocked): ['OPTIONS']
sw controller: False
```

→ **所有 browser context 都要加 `service_workers="block"`**。
唔加嘅話:route mock 靜靜哋失效 → 測試以為「前端冇打 API」,
而真實係 mock 根本冇生效。假 failure 同假 pass 都有可能。

### 2. `page` fixture 要用 function scope

`scope="module"` 會令所有 test 共用同一個 page。前一個 test 觸發嘅
upload flow 仲喺 background 行,會覆寫下一個 test 嘅 Alpine state →
讀到唔屬於自己 set 嘅值 → 假 failure。

→ 用 `scope="function"`,每個 test 獨立 context。

### 3. Timing 門檻要避開 cold start

`/api/scan_upload` 第一次要 6.8s(EXIF 抽取 + PIL 首次載入),
之後每次 0.74-1.02s。

→ 量速度之前先 warm-up 一次,否則會撞 cold start 造成假 failure。

### 4. `networkidle` 永遠唔 settle

gymbro PWA 有持續 polling (`setInterval` 拉 `/api/state`、
`/api/withings_steps_today`、`/api/health_overlay` 等)。
Playwright 嘅 `wait_until="networkidle"` 會 **timeout**。

→ 全部用 `wait_until="domcontentloaded"` + 明確 `wait_for_function`。

呢個係 suite 入面大部分 pre-existing PWA failure/error 嘅主因之一。

### 5. ⚠️ 記憶體壓力會令 pytest 俾 OOM kill —— 造成假陰性

**呢個係最陰險嘅陷阱。** 呢部機得 8GB RAM,仲要行 `gym_web.py`
(900MB) + 多個 Hermes python 進程(每個 500MB) + Playwright chromium。

實測:
```
$ pytest tests/api tests/e2e
...                     ← 被 signal 9 終止 (SIGKILL = OOM killer)

Mem:  7935 total, 5888 used, 256 free
Swap: 8191 total, 4133 used
chromium orphan: 14 個, 1467 MB
```

**如果 pytest 冇跑完,`parse_results()` 收到空輸出 → diff 會報
「0 NEW regression」= 假陰性!**

`pytest_baseline_diff.py` 而家會自己 detect:
- 冇 pytest summary 行,或者 returncode 係 15/137/124/負數
  → 判定「跑唔完」→ 印警告 → `exit 2`,**唔報任何 regression 結論**。

**操作守則:**
- **唔好同時跑多個 pytest。** 我試過同時跑 QA 套件 + baseline-diff
  → 兩樣都 OOM。
- 跑之前先睇 `free -m`,`available` 少過 ~1.5GB 就等等。
- 跑完檢查有冇 orphan chromium 殘留:
  ```bash
  ps aux | grep -E "chrome-headless|headless_shell" | grep -v grep | wc -l
  ```
  如果有,確認係自己嘅 run 嘅先至清理 —— **唔好殺其他 agent 嘅 browser**。
- 睇 parent 判斷孤兒:
  ```bash
  ps -eo pid,ppid,etimes,rss,args | grep chrome | grep -v grep
  ```

**同一個 root cause 亦令 `gym_web.py` server 喺 AI job 期間死**
(見報告 Bug 3)—— 記憶體壓力,唔係 AI code 嘅問題。

### 6. `HTML_TEMPLATE` 喺 import 時讀入記憶體

```python
# gym_web.py:14490-14491
_HTML_TEMPLATE_PATH = Path(__file__).parent / "templates" / "index.html"
HTML_TEMPLATE = _HTML_TEMPLATE_PATH.read_text(encoding="utf-8")
```

`index()` 用 `render_template_string(HTML_TEMPLATE)`。

→ **改 `templates/index.html` 之後唔 restart server 就唔會生效。**
→ `test_server_freshness.py` 會偵測到並 fail,叫你去 restart。

Restart 指令 (只關 gym_web.py 自己,**唔好**重啟 alonso gateway):

```bash
cd /home/work/projects/gymbro
PYTHONPATH="" /usr/bin/python3 gym_web.py
```

⚠️ 比對 served vs disk template 時要 `.strip()` ——
Flask 嘅 `render_template_string` 會 trim 走 template 嘅尾隨換行,
唔 strip 會得到「served 291026 chars vs disk 291027 chars」嘅假陽性。
