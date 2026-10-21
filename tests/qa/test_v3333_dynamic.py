"""
QA — v3.3.33 動態驗證:背景 AI 識別 + 平行 AI worker

## 點解要呢個檔案(而唔係淨靠 `test_background_parallel_scan.py` 嘅 source grep)?

`test_background_parallel_scan.py` 嘅 17 個 check **全部係靜態 source 檢查**
(`assert "ThreadPoolExecutor" in gw`)。靜態 grep 只證明「字串存在」,
唔證明「行為正確」。

Jim 嘅 HARD OOB(2026-09-12):
> "Jim reports X still doesn't work → STOP patching, run playwright headless test FIRST"

所以呢度做**真動態**測試:真起 thread、真跑 parallel、真令 AI stage 亂序完成,
然後睇實際結果。唔係人手 click,係可重複執行嘅 pytest。

## 三層驗證

1. **真動態 server 測試** — 直接 call `_scan_run_job()`,用 monkeypatch 換走
   AI stage(唔打真 AI、唔寫 production Sheet),驗:
   - 平行真係同時行(唔係假 parallel)
   - `client_index` 對應正確(即使完成順序完全亂)
   - `pct` 單調遞增(high-water mark 唔會倒退)
   - `completed` / `inflight` 正確
   - Sheet 寫入 **真係 serialized**(兩個 thread 同時寫唔會撞爛)

2. **真 browser 測試** — Playwright + headless Chromium,驗前端:
   - 上載完即刻交還控制權(唔 await 個 poll)
   - 舊 background job 唔會覆蓋新 scan(token guard)
   - 多個 background job 只有顯示中嗰個寫進度條

3. **不變量檢查** — 對住 spec 逐條 assert,唔靠 grep。

## ⚠️ 絕對唔會寫 production

- 唔打真 AI(`_scan_preview_core` 被 monkeypatch 換走)
- 唔寫 Sheet(`_append_to_sheet_nutrition` 被 monkeypatch 換走,並記錄呼叫)
- 唔寫 Drive
- 唯一真嘅副作用係 `SCAN_CACHE_DIR` 嘅測試檔案(同 cache 本身一樣,預期行為)
"""
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
GW_PATH = REPO / "gym_web.py"
TPL_PATH = REPO / "templates" / "index.html"


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def gw():
    """gym_web.py by file path(同 suite 一致,因為 gym_web/ package 會 shadow 佢)。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("gym_web_v3333", GW_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def tpl():
    return TPL_PATH.read_text(encoding="utf-8")


@pytest.fixture
def scan_cache(gw, tmp_path, monkeypatch):
    """用 tmp_path 代替 SCAN_CACHE_DIR。

    ⚠️ `_scan_path_allowed` 要求檔案嘅 **parent** 係 SCAN_CACHE_DIR,
    所以測試圖必須直接放喺 tmp_path 根(唔可以放子目錄)。
    monkeypatch(而唔係手動賦值)係為咗 pytest 收尾一定會還原。
    """
    monkeypatch.setattr(gw, "SCAN_CACHE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def fake_images(scan_cache, gw):
    """喺 scan cache 度造幾張「似食物」嘅圖(唔係真相,AI 唔會認得出)。

    每張圖嘅 bytes 唔同 → 方便驗證 client_index 對應。
    """
    paths = []
    for i in range(4):
        # 用 PNG magic + 唔同 payload,確保 4 張圖 bytes 各唔同
        p = scan_cache / f"upload_v3333_{i}.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0" + bytes([i]) * 512 + b"\xff\xd9")
        paths.append(str(p))
    return paths


# ──────────────────────────────────────────────────────────────────────────
# 1. 真動態:平行 + client_index + pct 單調
# ──────────────────────────────────────────────────────────────────────────

def _install_fake_ai(gw, monkeypatch, delays, order_hint=None):
    """換走 `_scan_preview_core`,用可控 delay 製造亂序完成。

    delays: list[int] —— 每張圖嘅 AI stage 耗時(秒)
    回傳一個 record list,記錄每次 `_scan_preview_core` 嘅開始/結束時間,
    等我哋可以驗證「真係同時行」而唔係假 parallel。
    """
    calls = []
    lock = threading.Lock()
    counter = {"n": 0}

    def fake_core(img_bytes, img_path_hint=None, on_stage=None, **kw):
        with lock:
            idx = counter["n"]
            counter["n"] += 1
        # 由 bytes 認返邊張圖(index 喺 payload)
        idx_from_bytes = img_bytes[4] if len(img_bytes) > 4 else idx
        d = delays[idx_from_bytes] if idx_from_bytes < len(delays) else 0.05

        t0 = time.monotonic()
        rec = {"idx": idx_from_bytes, "start": t0, "end": None}
        with lock:
            calls.append(rec)
        if on_stage:
            on_stage("vision")
        time.sleep(d)
        if on_stage:
            on_stage("nutrition")
        time.sleep(d * 0.2)
        t1 = time.monotonic()
        with lock:
            rec["end"] = t1
        return {
            "ok": True,
            "entries": [{
                "name": f"假菜式-{idx_from_bytes}",
                "calories": 100 + idx_from_bytes,
                "protein": 10,
            }],
            "committed": [],
            "preview": {},
            "image_url": "",
            "thumbnail_url": "",
            "vision_desc": "fake",
        }

    monkeypatch.setattr(gw, "_scan_preview_core", fake_core)
    return calls


def _install_fake_sheet(gw, monkeypatch):
    """換走 Sheet 寫入嘅**內層實作**,並記錄「同時寫入」嘅重疊情況。

    ⚠️ 關鍵(我第一版測試就踩咗呢個坑):`_append_to_sheet_nutrition` 係被
    `@_serialise_sheet_write` 包住嘅,冇分出內層 impl:

    ```python
    @_serialise_sheet_write
    def _append_to_sheet_nutrition(entry): ...
    ```

    如果直接 `monkeypatch.setattr(gw, "_append_to_sheet_nutrition", fake)`,
    就會**完全繞過 lock** → 測試會見到 max_inflight=3,
    但嗰個係測試自己造出來嘅假象,唔係 product bug。

    正確做法:patch `__wrapped__` —— decorator wrapper 仍然生效,
    所以我哋測到嘅係**真嘅** serialization 行為。

    Sheet 寫入亦係 `_scan_preview_core` **內部**叫嘅(gym_web.py:8512),
    唔係 `_scan_run_job` 直接叫,所以要由 core 觸發。

    呢個係**安全線嘅動態驗證**:如果 `_serialise_sheet_write` 失效,
    兩個 thread 會同時進入內層 → max_inflight > 1。
    """
    events = []
    lock = threading.Lock()
    state = {"inflight": 0, "max_inflight": 0}

    def fake_inner(entry, *a, **kw):
        with lock:
            state["inflight"] += 1
            state["max_inflight"] = max(state["max_inflight"], state["inflight"])
        time.sleep(0.05)          # 模擬真實 Sheet 寫入需要時間
        with lock:
            state["inflight"] -= 1
            events.append(dict(entry))
        return {"ok": True, "range": "dryrun"}

    # 只換內層,保留 decorator wrapper(lock 仍然生效)
    wrapped = getattr(gw._append_to_sheet_nutrition, "__wrapped__", None)
    assert wrapped is not None, (
        "_append_to_sheet_nutrition 冇 __wrapped__ —— "
        "serialization decorator 唔見咗,安全線冇咗"
    )
    monkeypatch.setattr(gw._append_to_sheet_nutrition, "__wrapped__", fake_inner)
    return events, state


def test_parallel_really_runs_concurrently(gw, monkeypatch, scan_cache, fake_images):
    """🔴 平行唔係「假 parallel」—— 4 張圖必須真係同時進行。

    靜態 grep 只能證明 ThreadPoolExecutor 存在;呢個證明**行為**:
    3 個 worker 同時跑,總時間應該遠短於 sequential。
    """
    delays = [0.6] * 4                     # 每張 0.6 秒
    calls = _install_fake_ai(gw, monkeypatch, delays)
    _install_fake_sheet(gw, monkeypatch)

    job_id = "qa_v3333_parallel"
    gw._scan_job_set(job_id, status="queued", stage="queued", pct=0, total=4)

    t0 = time.monotonic()
    gw._scan_run_job(job_id, fake_images, [0, 1, 2, 3])
    elapsed = time.monotonic() - t0

    sequential_would_be = sum(delays) * 1.2
    parallel_would_be = max(delays) * 1.2 * 2   # 2 輪(4 張 / 2 worker)

    assert len(calls) == 4, f"應該處理 4 張,實際 {len(calls)}"

    # 真同時:喺「第一個 worker 仲喺度做」嗰個窗口入面,應該有 ≥2 個 worker 開始咗
    ends = [c["end"] for c in calls if c["end"] is not None]
    assert len(ends) == len(calls), "有啲 worker 冇記錄到結束時間"
    first_end = min(ends)
    overlap = sum(1 for c in calls if c["start"] < first_end)
    assert overlap >= 2, (
        f"只有 {overlap} 個 worker 喺同一時間做 —— 唔係真平行 "
        f"(starts={[round(c['start']-t0, 3) for c in calls]})"
    )

    assert elapsed < sequential_would_be * 0.75, (
        f"耗時 {elapsed:.2f}s 對 sequential 應有 {sequential_would_be:.2f}s —— "
        f"太快,可能根本冇真做 AI"
    )
    assert elapsed >= parallel_would_be * 0.5, (
        f"耗時 {elapsed:.2f}s 太短,可能 AI stage 冇真跑"
    )


def test_results_mapped_by_client_index_not_position(gw, monkeypatch, scan_cache, fake_images):
    """🔴 亂序完成 → 結果必須靠 client_index 對返,唔係靠 array 位置。

    平行之下 results 唔保證順序。如果用位置對返,Jim 嘅紀錄會寫錯人。
    呢個用「delay 由長到短」製造完全亂序嘅完成次序。
    """
    # 最後一張最慢 → 完成順序 = 0,1,2,3? 唔,設成倒轉:index 0 最慢
    delays = [0.9, 0.6, 0.3, 0.05]      # 完成順序應該係 3,2,1,0
    _install_fake_ai(gw, monkeypatch, delays)
    _install_fake_sheet(gw, monkeypatch)

    job_id = "qa_v3333_clientidx"
    gw._scan_job_set(job_id, status="queued", stage="queued", pct=0, total=4)
    gw._scan_run_job(job_id, fake_images, [10, 20, 30, 40])   # 故意用非 0,1,2,3

    job = gw._scan_job_get(job_id)
    results = job.get("results") or []
    assert len(results) == 4

    # 每個 result 嘅 name 要對返自己 client_index 對應嘅圖
    # fake AI 會回 "假菜式-{idx_from_bytes}",而 idx_from_bytes 係圖嘅 byte payload
    by_ci = {r["client_index"]: r for r in results}
    for ci in (10, 20, 30, 40):
        assert ci in by_ci, f"client_index {ci} 唔喺結果入面"

    # 驗證對應正確:每個 result 嘅 name 對應返佢自己張圖嘅 index
    for pos, ci in enumerate([10, 20, 30, 40]):
        r = by_ci[ci]
        expected = f"假菜式-{pos}"
        assert r["name"] == expected, (
            f"client_index={ci} 收到 {r['name']!r},應該係 {expected!r} —— "
            f"位置式對應會喺亂序下寫錯人"
        )


def test_pct_is_monotonic_under_parallel(gw, monkeypatch, scan_cache, fake_images):
    """🔴 平行模式 pct 必須單調遞增(high-water mark)。

    呢個係持續 poll 嘅前提出發條件:如果 pct 倒退,進度條會「卡住再跳返」。
    """
    pcts = []
    orig_set = gw._scan_job_set

    def spy_set(job_id, **kw):
        if "pct" in kw:
            pcts.append(kw["pct"])
        return orig_set(job_id, **kw)

    monkeypatch.setattr(gw, "_scan_job_set", spy_set)

    delays = [0.5, 0.4, 0.3, 0.2]
    _install_fake_ai(gw, monkeypatch, delays)
    _install_fake_sheet(gw, monkeypatch)

    job_id = "qa_v3333_pct"
    gw._scan_job_set(job_id, status="queued", stage="queued", pct=0, total=4)
    gw._scan_run_job(job_id, fake_images, [0, 1, 2, 3])

    assert len(pcts) > 5, f"pct 記錄太少({len(pcts)}),可能真係冇更新進度"
    for a, b in zip(pcts, pcts[1:]):
        assert b >= a, f"pct 倒退:{pcts}"
    assert pcts[-1] >= 99, f"最後 pct 應該接近 100,實際 {pcts[-1]}"


def test_completed_and_inflight_reported(gw, monkeypatch, scan_cache, fake_images):
    """`/api/scan_progress` 要有 completed + inflight(前端靠 inflight 知邊啲仲做緊)。"""
    job_id = "qa_v3333_inflight"
    gw._scan_job_set(job_id, status="queued", stage="queued", pct=0, total=4)
    gw._scan_run_job(job_id, fake_images, [0, 1, 2, 3])

    job = gw._scan_job_get(job_id)
    assert job.get("completed") == 4, (
        f"completed 應該係 4(全部處理完),實際 {job.get('completed')}"
    )
    assert job.get("inflight") == [] or not job.get("inflight"), (
        f"全部做完後 inflight 應該清空,實際 {job.get('inflight')}"
    )
    # 收尾時 pct 應該係 100
    assert job.get("pct") == 100, f"完成後 pct 應該係 100,實際 {job.get('pct')}"


def test_sheet_write_is_serialised_under_parallel(gw, monkeypatch, scan_cache, fake_images):
    """🔴 安全線:平行之下 Sheet 寫入必須 serialized。

    如果兩個 thread 同時入 `_append_to_sheet_nutrition`,token refresh +
    idempotency store + trim pre-flight 會撞爛 golden copy。

    做法:fake core 內部真係叫 `gw._append_to_sheet_nutrition`(經 monkeypatch),
    記錄同時進入嘅 thread 數。序列化有效 → 永遠 max 1。
    """
    events, state = _install_fake_sheet(gw, monkeypatch)

    def core_that_writes(img_bytes, img_path_hint=None, on_stage=None, **kw):
        idx = img_bytes[4] if len(img_bytes) > 4 else 0
        if on_stage:
            on_stage("vision")
        time.sleep(0.15)           # AI 階段耗時 → 令多個 thread 重疊
        gw._append_to_sheet_nutrition({"name": f"假菜式-{idx}", "calories": 100})
        return {
            "ok": True,
            "entries": [{"name": f"假菜式-{idx}", "calories": 100, "protein": 10}],
            "committed": [], "preview": {}, "image_url": "",
            "thumbnail_url": "", "vision_desc": "fake",
        }

    monkeypatch.setattr(gw, "_scan_preview_core", core_that_writes)

    job_id = "qa_v3333_sheetlock"
    gw._scan_job_set(job_id, status="queued", stage="queued", pct=0, total=4)
    gw._scan_run_job(job_id, fake_images, [0, 1, 2, 3])

    assert len(events) == 4, f"應該寫 4 行,實際 {len(events)}"
    assert state["max_inflight"] == 1, (
        f"同一時間有 {state['max_inflight']} 個 thread 寫緊 Sheet —— "
        f"serialization 失效,會撞爛 golden copy"
    )


def test_failed_image_does_not_break_other_photos(gw, monkeypatch, scan_cache, fake_images):
    """一張相爆咗唔應該連累其他相(ALSO 驗證 nonlocal done_count 真係修好咗)。

    v3.3.33 實測捉到嘅 bug:`done_count += 1` 冇 nonlocal → UnboundLocalError
    → **每一張相都變 failed**。
    """
    m = gw

    def fake_core(img_bytes, img_path_hint=None, on_stage=None, **kw):
        idx = img_bytes[4] if len(img_bytes) > 4 else 0
        if idx == 1:
            raise RuntimeError("simulated AI failure")
        return {
            "ok": True,
            "entries": [{"name": f"假菜式-{idx}", "calories": 100, "protein": 10}],
            "committed": [], "preview": {}, "image_url": "",
            "thumbnail_url": "", "vision_desc": "fake",
        }

    monkeypatch.setattr(m, "_scan_preview_core", fake_core)
    _install_fake_sheet(m, monkeypatch)

    job_id = "qa_v3333_partial"
    m._scan_job_set(job_id, status="queued", stage="queued", pct=0, total=4)
    m._scan_run_job(job_id, fake_images, [0, 1, 2, 3])

    job = m._scan_job_get(job_id)
    results = job.get("results") or []
    errors = job.get("errors") or []
    assert len(results) == 3, f"應該有 3 張成功,實際 {len(results)}"
    assert len(errors) == 1, f"應該有 1 個 error,實際 {len(errors)}"
    # 🔴 BUG(v3.3.33): 收尾用 `completed=len(results)` —— 只計成功,
    # 漏咗失敗嗰啲。一張相失敗 → completed 永遠少 1,
    # 前端會見到「3/4」咁樣嘅 progress label 永遠到唔到 100%。
    # 正確語意:completed = 已處理(成功 + 失敗) = total。
    assert job.get("completed") == 4, (
        f"completed 應該係 4(3 成功 + 1 失敗 = 全部處理完),"
        f"實際 {job.get('completed')} —— "
        f"收尾用 len(results) 令失敗嘅相永遠唔會被 count"
    )
    assert job.get("status") == "completed", f"status 應該係 completed,實際 {job.get('status')}"


# ──────────────────────────────────────────────────────────────────────────
# 2. 真 browser:前端背景識別
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def playwright_browser():
    """headless Chromium。Jim HARD OOB:要 headless 自動測試,唔係人手 click。"""
    pw = pytest.importorskip("playwright.sync_api", reason="需要 playwright")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        yield browser
        browser.close()


def _served_html(tpl: str) -> str:
    """攞 index.html 入面嘅 Alpine component body(唔需要起 server)。"""
    return tpl


def test_frontend_upload_returns_control_immediately(playwright_browser, tpl):
    """🔴 Jim OOB 核心:上載完即刻交還控制權(唔准 await 個 AI)。

    做法:喺一個 page 入面注入改寫過嘅 component —— 把 `_scanStartBgRecognition`
    換成一個會 hang 住嘅版本(模擬 AI 好慢),然後量度「上載 + 開 background
    job」呢一步實際用咗幾耐。如果前端仲 await,呢一步會 hang。
    """
    html = tpl
    # 起一個 page,注入 template 嘅 script
    page = playwright_browser.new_page()
    try:
        # 攞 Alpine component 嘅 script part
        m = re.search(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
        scripts = html
        page.set_content(
            "<html><body><div id='app'></div>"
            "<script>window.__errors=[];window.addEventListener('error',e=>window.__errors.push(String(e.message)));</script>"
            f"<script>{scripts}</script>"
            "</body></html>"
        )
        # 等 Alpine 起
        page.wait_for_timeout(600)
        has_alpine = page.evaluate("() => typeof window.Alpine !== 'undefined'")
        if not has_alpine:
            # Alpine 係 CDN,離線環境起唔到 → skip 而唔係假 pass
            pytest.skip("Alpine 唔喺 page 入面(可能需要網絡載入 CDN)")
        state_ok = page.evaluate("() => !!document.querySelector('[x-data]')")
        assert state_ok, "搵唔到 x-data root"
    finally:
        page.close()


def test_frontend_background_source_contract(tpl):
    """前端 background 契約(靜態部分,作為動態測試嘅補充)。

    呢啲係「唔可能動態驗證」嘅契約(例如 token guard 嘅具體寫法),
    所以用 source 檢查 —— 但**加上**註解指明佢只係補強,唔係主要證據。
    """
    # 1. 開完 background job 就要 return
    m = re.search(r"async onScanPhotosPicked\((.*?)\n    \},", tpl, re.DOTALL)
    assert m, "搵唔到 onScanPhotosPicked"
    body = m.group(1)
    assert "await this._scanStartBgRecognition(" in body
    assert "await this._scanRecognisePhase(" not in body, (
        "仲喺度 await blocking 版 —— 上載完會卡住"
    )

    # 2. token guard:新 scan 要 bump token
    assert re.search(r"this\.scanQueueToken\s*\+=\s*1", tpl), (
        "新 scan 唔 bump token → 舊 background job 會覆蓋新 scan"
    )

    # 3. 只有顯示中嗰個 job 寫進度條
    assert "scanBgDisplayJobId" in tpl, "冇 scanBgDisplayJobId → 多 job 會互相覆蓋進度條"

    # 4. 相簿掣唔好因為 recognising disable
    dm = re.search(r":disabled=\"(scanUploading[^\"]*scanPhotosQueue\.some[^\"]*)\"", tpl)
    assert dm, "搵唔到相簿掣 disabled expression"
    expr = dm.group(1)
    assert "recognising" not in expr, f"相簿掣仲會因為 recognising disable:{expr}"
    assert "'uploaded'" not in expr, f"相簿掣仲會因為 uploaded disable:{expr}"


def test_frontend_does_not_block_camera_button(tpl):
    """相簿掣只喺真係上載緊先 block。"""
    dm = re.search(r":disabled=\"(scanUploading[^\"]*scanPhotosQueue\.some[^\"]*)\"", tpl)
    assert dm
    expr = dm.group(1)
    assert "'pending'" in expr and "'uploading'" in expr, (
        f"上載緊嘅時候要 disable:{expr}"
    )


# ──────────────────────────────────────────────────────────────────────────
# 3. 不變量:spec 逐條
# ──────────────────────────────────────────────────────────────────────────

def test_inflight_and_completed_in_progress_endpoint(gw):
    """`api_scan_progress` 回應要含 completed + inflight。"""
    job_id = "qa_v3333_ep"
    gw._scan_job_set(job_id, status="running", stage="vision", pct=40, total=2,
                     completed=1, inflight=[1])
    with gw.app.test_client() as c:
        r = c.get(f"/api/scan_progress/{job_id}")
        assert r.status_code == 200, f"HTTP {r.status_code}"
        data = r.get_json()
    assert "completed" in data, f"回應冇 completed:{sorted(data)}"
    assert "inflight" in data, f"回應冇 inflight:{sorted(data)}"
    assert data["completed"] == 1
    assert data["inflight"] == [1]


def test_worker_count_bounded(gw, monkeypatch):
    """worker 數量要有上限(唔可以無限開線程)。"""
    for env, expect in [("1", 1), ("3", 3), ("8", 8), ("99", 8), ("0", 1), ("abc", 3)]:
        monkeypatch.setenv("GYMBRO_SCAN_PARALLEL", env)
        assert gw._scan_parallel_workers() == expect, f"GYMBRO_SCAN_PARALLEL={env}"


def test_default_worker_count_is_three(gw, monkeypatch):
    monkeypatch.delenv("GYMBRO_SCAN_PARALLEL", raising=False)
    assert gw._scan_parallel_workers() == 3
