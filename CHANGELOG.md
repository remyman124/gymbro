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
