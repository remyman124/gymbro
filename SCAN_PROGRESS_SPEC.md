# Gymbro 食物相片上載 + AI 識別進度條 — 實作規格 (v3.3.24e)

> ⚠️ **呢份係 v3.3.24 時代嘅設計文件,部分內容已過時。**
> 2026-10-21 更新:Perplexity 同 APiyI 供應商已完全移除(v3.3.30 / v3.3.34),
> 而家淨返 MiniMax 一家;掃描亦已改成**上載完即刻 return、AI 喺背景行**
> (v3.3.33)。當中講「MiniMax + APiyi + PPLX」嘅 pipeline 描述屬歷史記錄。

**Owner:** Alonso (tech leads: Frimpong / Torres / Isak)
**日期:** 2026-10-20
**來源:** Jim OOB — 「Gymbro food image upload and recognising. Pls add progress bar of the upload and ai recognition」+「And when multiple upload, I want gymbro to handle the upload first. Ai recognition can be done later」

---

## 0. 現狀 (唔好重做)

| 位置 | 現況 | 問題 |
|------|------|------|
| `templates/index.html:894-895` | 有一條 `scanProgress` bar | **假進度** — `setInterval` 每 400ms +5%,頂到 85% freeze,response 到先跳 100 |
| `templates/index.html:3756` `onScanFile` | `fetch('/api/scan_preview')` | `fetch` **冇 upload progress event** |
| `templates/index.html:3898` `onScanPhotosPicked` | 逐張 sequential `fetch` 上傳+識別綑埋一齊 | Jim 要求:**先全部上傳,識別後做** |
| `gym_web.py:7646` `api_scan_preview` | 單一 request 做晒上傳+EXIF+MiniMax+APiyi+PPLX+multi+Drive+Sheet commit | 總共 20-40 秒,期間完全冇進度 |

## 1. 目標架構:兩階段

```
Phase 1  UPLOAD   (快,真實 byte 進度)   → /api/scan_upload      (multipart, 多檔)
Phase 2  RECOGNISE (慢,真實 stage 進度)   → /api/scan_recognize   (JSON, 立即返 job_id)
                                            /api/scan_progress/<job_id>  (poll)
```

Jim 原文:**「when multiple upload, I want gymbro to handle the upload first. Ai recognition can be done later」**
→ Phase 1 必須**全部**上傳完先開始 Phase 2。

---

## 2. Backend 規格 (Frimpong 負責) — `gym_web.py`

### 2.1 `POST /api/scan_upload`

- **Content-Type:** `multipart/form-data`
- **欄位:** `images` — 可多個檔案 (Jim 一次揀多張相)。為咗容錯,**同時**接受 `image` (單張) 呢個欄位名。
- **行為:** 只做「收檔 + 存檔」,**完全唔做 AI**。
  1. 逐個檔案寫入 `SCAN_CACHE_DIR`,檔名 `upload_{YYYYMMDD_HHMMSS}_{i}.jpg` (保留原副檔名如果係 jpg/png)
  2. 抽 EXIF 拍攝時間 (重用現有 `_extract_exif_capture_time`) → 存 `capture_iso` / `capture_hhmm`
  3. 造縮圖 (重用現有 `_make_thumb` / `_thumb_path_for` / `_thumb_url_for`)
- **大小上限:** 每張 10 MB (沿用現有 `api_scan_preview` 嘅 `10 * 1024 * 1024`)
- **回應 (JSON, HTTP 200):**
```json
{
  "ok": true,
  "uploads": [
    {
      "client_index": 0,
      "filename": "IMG_1234.jpg",
      "path": "/home/work/projects/gymbro/scan_cache/upload_20261020_143012_0.jpg",
      "image_url": "/scan_img/upload_20261020_143012_0.jpg",
      "thumbnail_url": "/scan_thumb/upload_20261020_143012_0.jpg",
      "size_kb": 2341,
      "capture_iso": "2026-10-20",
      "capture_hhmm": "13:05"
    }
  ]
}
```
- **`path` 係 server-side absolute path**,Phase 2 會用。**必須**做 safety check:Phase 2 只准讀 `SCAN_CACHE_DIR` 同 `/home/work/.hermes/image_cache/` 之下嘅路徑(現有 `api_scan_preview_from_path` 已有呢個 pattern,照抄)。

### 2.2 `POST /api/scan_recognize`

- **Content-Type:** `application/json`
- **Body:** `{"paths": ["/abs/path/a.jpg", "/abs/path/b.jpg"], "client_indexes": [0, 1]}`
  - `client_indexes` 選填,對返 Phase 1 每張嘅 index
- **行為:** **立即**建立 job,背景 thread 開始**順序**處理(唔好平行 — MiniMax 有 rate limit),HTTP 200 即刻返:
```json
{ "ok": true, "job_id": "scanjob_20261020_143015_a1b2c3", "total": 2 }
```
- **每張圖**跑一次現有嘅完整識別 pipeline (MiniMax vision → APiyi vision 2nd opinion → PPLX enrich → APiyi nutrition → APiyi multi-dish → brand override → Drive upload → 縮圖 → Sheet commit + cache refresh)。

### 2.3 `GET /api/scan_progress/<job_id>`

**每 400-800ms poll 一次。** 回應:
```json
{
  "ok": true,
  "job_id": "scanjob_...",
  "status": "running",              // running | completed | failed
  "stage": "recognising",           // queued | uploading_not_needed | vision | second_opinion | enrich | multi | drive | commit | done
  "stage_label": "AI 睇緊第 2 張相…",  // 繁中,俾前端直接顯示
  "pct": 45,                          // 0-100 整體進度
  "current_index": 1,                // 而家處理緊第幾張 (0-based)
  "total": 2,
  "results": [                        // 每張完成後 append 一個
    { "client_index": 0, "ok": true, "name": "白飯配煎雞扒", "calories": 720, "protein": 42,
      "committed": [ {"name":"白飯配煎雞扒","calories":720,"protein":42,"grade":"B","sheet_row":"Sheet1!A123","sheet_ok":true,"scan_index":123} ],
      "preview": { ...同現有 /api/scan_preview 嘅 preview 一樣... },
      "image_url": "/scan_img/scan_20261020_143012.jpg", "thumbnail_url": "..." }
  ],
  "errors": [ { "client_index": 1, "error": "..." } ],
  "elapsed_s": 12.4
}
```

**進度 stage 定義 (每張圖嘅 8 個 stage,每個 stage 佔 12.5%):**

| pct 範圍 | stage | stage_label (繁中) | 對應 code |
|---|---|---|---|
| 0-12 | `vision` | `AI 睇緊相…` | `_minimax_vision()` 前 |
| 12-25 | `second_opinion` | `第二個 AI 交叉檢查…` | `_apiyi_vision_analyze()` 前 |
| 25-37 | `enrich` | `估算營養…` | `_pplx_enrich()` 前 |
| 37-50 | `enrich2` | `交叉核對營養值…` | `_apiyi_nutrition_enrich()` 前 |
| 50-62 | `multi` | `分析有幾多餸…` | `_apiyi_nutrition_enrich_multi()` 前 |
| 62-75 | `brand` | `對照品牌營養表…` | `_lookup_brand_macros()` 前 |
| 75-87 | `drive` | `上載到雲端…` | `_upload_to_drive()` 前 |
| 87-100 | `commit` | `寫入 Google Sheet…` | `_append_to_sheet_nutrition()` 前 |

**整體 pct** = `(current_index * 100 + stage_pct) / total`

### 2.4 Job registry

```python
SCAN_JOBS = {}
SCAN_JOBS_LOCK = threading.Lock()

def _scan_job_set(job_id, **kwargs):
    with SCAN_JOBS_LOCK:
        cur = SCAN_JOBS.get(job_id, {})
        cur.update(kwargs)
        cur["updated_at"] = time.time()
        SCAN_JOBS[job_id] = cur
```
- Job 做完之後**保留 10 分鐘**再清(避免前端遲啲 poll 唔到)。可以用 `time.time() - updated_at > 600` 判斷。
- **每次更新 MUST print 進度** (`flush=True`),方便 debug。
- **唔准** 用 `except: pass` — 全部要 `print(f"[scan-job] {job_id} ... failed: {e}", flush=True)`

### 2.5 Refactor 要求

`api_scan_preview` (line 7646) 嘅邏輯要抽成一個**可重用**嘅 module-level function,俾 job worker 同現有 endpoint 共用。**但唔可以改變現有 `/api/scan_preview` 嘅行為** (唔少 code path 靠佢)。

建議做法:
```python
def _scan_preview_core(img_bytes: bytes, img_path_hint: str = "") -> dict:
    """由 api_scan_preview 抽出。回傳原本嗰個 jsonify({...}) 嘅 dict。"""
    # ...原本 line 7658-7979 嘅邏輯,最後 return { ... } 而唔係 return jsonify({...})
```
然後:
```python
@app.route("/api/scan_preview", methods=["POST"])
def api_scan_preview():
    ...讀 request.files['image']...
    result = _scan_preview_core(img_bytes)
    return jsonify(result)
```

**驗收:** 改完之後,現有 `/api/scan_preview` 嘅 response JSON **必須同改之前一模一樣**(逐個 key 對)。Isak 會做呢個 regression test。

---

## 3. Frontend 規格 (Torres 負責) — `templates/index.html`

### 3.1 真實上傳進度 (XHR,唔用 fetch)

`fetch()` 冇 upload progress。改用 `XMLHttpRequest`:
```js
function uploadWithProgress(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) {
        onProgress(Math.round((e.loaded / e.total) * 100), e.loaded, e.total);
      }
    };
    xhr.onload = () => {
      try { resolve(JSON.parse(xhr.responseText)); }
      catch (err) { reject(new Error('bad JSON: ' + xhr.responseText.slice(0,200))); }
    };
    xhr.onerror = () => reject(new Error('network error'));
    xhr.onabort = () => reject(new Error('aborted'));
    xhr.send(formData);
  });
}
```

### 3.2 兩階段 flow (`onScanPhotosPicked` 重寫)

```
1. 揀 N 張相
2. 建立 queue UI (全部 status='uploading')
3. PHASE 1 — UPLOAD:
   逐張 (或並行,建議最多 3 張並行) 上傳去 /api/scan_upload
   → 進度條顯示「上傳緊 2/5 · 43%」+ 真實 byte %
   → 全部上傳完先進入 Phase 2
4. PHASE 2 — RECOGNISE:
   POST /api/scan_recognize {paths:[...]} → job_id
   → 每 700ms GET /api/scan_progress/<job_id>
   → 進度條顯示「AI 睇緊第 2 張 · 識食物中…」+ server 返嘅 stage_label
   → 收到 results 就逐張更新 queue item
5. 全部完成 → refresh food log + flash 摘要
```

### 3.3 UI 設計

Queue card 內要見到**兩條 bar**(或一條 bar 分兩段)。Jim 見到嘅要清晰:

```
┌─────────────────────────────────────────┐
│ 📸 上傳緊 2/5 相                         │
│ ▓▓▓▓▓▓▓▓░░░░░░░░  43%   1.2 MB / 2.8 MB │  ← 真實 byte 進度
├─────────────────────────────────────────┤
│ 🤖 AI 識別緊 (等緊上傳完成)               │
│ ░░░░░░░░░░░░░░░  0%   準備緊…            │  ← 灰色,等 Phase 1 完
└─────────────────────────────────────────┘
```

Phase 2 進行中:
```
┌─────────────────────────────────────────┐
│ ✅ 上傳完成 5/5                          │
├─────────────────────────────────────────┤
│ 🤖 AI 睇緊第 3 張相… (識食物中)          │
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░  62%  3/5            │
└─────────────────────────────────────────┘
```

**要求:**
- 兩條 bar 用**唔同顏色**(上傳 = 藍/emerald,AI = amber/purple),唔好分唔到
- 每條 bar 都要有**文字 stage label**(唔淨係得個 bar)
- Bar 要有 `transition-all` 但**唔好太慢**(200-300ms),否則 stage 跳得太快睇唔到
- 保留現有 `scanProgress` / `scanUploading` 變數嘅兼容(其他 code 可能引用),但新邏輯用新變數:
  - `scanUploadPct` (0-100, 真實)
  - `scanUploadCurrent` (第幾張)
  - `scanUploadTotal`
  - `scanUploadBytesLabel` (例: "1.2 MB / 2.8 MB")
  - `scanAiPct` (0-100, server 返)
  - `scanAiStageLabel`
  - `scanAiCurrent` / `scanAiTotal`
  - `scanPhase` ('idle' | 'uploading' | 'recognising' | 'done')
- 單張上傳 (`onScanFile`) 都要用新 XHR + 新 bar
- **唔好整 Telegram button** (Jim 硬性要求)
- **唔好用英文 narrative term**(Jim OOB 2026-10-20): 用「上傳緊」「AI 睇緊」「識別緊」,唔好寫 "Uploading..." / "Recognizing..."

### 3.4 錯誤處理
- 上傳失敗 → 該張 status='failed',**繼續**上傳其他張
- 識別失敗 → 該張入 `errors[]`,queue item status='failed'
- 網絡斷 → 顯示清楚嘅錯誤訊息,唔好靜靜哋死

---

## 4. QA 規格 (Isak 負責)

寫 Playwright 或 pure-Python HTTP test,驗證:

1. **Upload 先過識別** — 用 2 張真實食物相,assert `/api/scan_upload` 喺 <5 秒內返 (唔做 AI),然後 `/api/scan_recognize` 先至啟動
2. **Job progress 有效** — poll `/api/scan_progress/<job_id>`,assert `pct` 單調遞增 (唔可以跌),`stage` 會變,`results` 逐張出
3. **Regression** — `/api/scan_preview` response JSON keys 同改之前**完全一致**(Frimpong 改之前先 dump 一份 baseline 落 `/home/work/projects/gymbro/scan_preview_baseline.json`)
4. **Frontend 渲染** — Playwright 開 `http://127.0.0.1:7000/`,確認 progress bar DOM 存在,`x-text` 有 stage label
5. **冇回歸** — 跑 `pytest` (如果 suite 存在),記住基線有 4 個 pre-existing env-dependent failure,**baseline-diff** 先算 regression

---

## 5. 共通約束

- **Gymbro 啟動命令(唔准改):** `PYTHONPATH="" /usr/bin/python3 gym_web.py` — 唔加 `PYTHONPATH=""` 會繼承 alonso 嘅 3.14 venv,PIL `_imaging.so` 會 ImportError
- **唔好重啟 alonso gateway** — 會斬斷現有 session
- **Sheet API `UNFORMATTED_VALUE` 返序號唔返字串** — 日期 46088 / 時間 0.5,`cache.py` 已有 `_format_serial_date()` / `_format_serial_time()` 修正,唔好改返
- **12 個營養欄位一個都不能少** (Jim 硬性要求)
- **菜名唔准係 generic**(例: "食物" / "未識別菜式" / "呢張相顯示…") — 沿用現有 `_sanitise_dish_name`
- **唔准 `except: pass`** — 全部要 print 埋
- **唔准寫假進度** — 進度必須反映真實上傳 byte 或真實 server stage
- **Reply 用繁體中文廣東話**

## 6. 交付

每個 tech lead 寫一份報告落:
- Frimpong → `/home/work/knowledge/raw/misc/2026-10-20-gymbro-scan-progress-backend.md`
- Torres → `/home/work/knowledge/raw/misc/2026-10-20-gymbro-scan-progress-frontend.md`
- Isak → `/home/work/knowledge/raw/misc/2026-10-20-gymbro-scan-progress-qa.md`

報告要寫:改咗咩檔案、diff 摘要、實測輸出、已知問題。
