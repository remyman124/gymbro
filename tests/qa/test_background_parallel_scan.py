"""
QA — v3.3.33 背景 AI 識別 + 平行 AI worker

Jim OOB 2026-10-21:
  > After upload, ai food recognition can be done in background
  > And ai work can be done in parallel to save queuing time too

呢組測試守兩件事:

1. **背景**:上傳完 → 開 job → 前端即刻返回。唔准再 `await` 個 poll
   (舊版 await 完先返回 → 上傳 bar 100% 咗但個 app 仲卡住,影相掣 disable 住)。

2. **平行**:server 端用 ThreadPoolExecutor 平行跑 AI stage。
   同時守住兩條安全線:
   - **Sheet 寫入必須 serialized** —— token refresh + idempotency store +
     trim pre-flight 唔係 concurrency-safe,平行會撞爛 golden copy。
   - **結果對應必須用 client_index** —— 平行之下 results 唔保證順序,
     用 array 位置對返會寫錯人嘅記錄。

全部係靜態 source 檢查 + 純函數測試,**唔會寫任何嘢落 production Sheet**。
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
TPL_PATH = REPO / "templates" / "index.html"
GW_PATH = REPO / "gym_web.py"


@pytest.fixture(scope="module")
def tpl():
    return TPL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def gw():
    return GW_PATH.read_text(encoding="utf-8")


def _js_body(tpl: str, needle: str) -> str:
    """抽出 `needle` 開始嗰個 function 嘅 body(粗略但夠用)。"""
    idx = tpl.find(needle)
    assert idx >= 0, f"搵唔到 {needle}"
    # 由 function 頭行搵到下一個同層 `},`
    m = re.compile(r"\n    \},").search(tpl, idx)
    assert m, f"{needle} 嘅 body 截唔到"
    return tpl[idx: m.start()]


# ──────────────────────────────────────────────────────────────────────────
# 1. 背景:前端唔准再 await 個 poll
# ──────────────────────────────────────────────────────────────────────────

def test_bg_recognition_does_not_await_poll(tpl):
    """`_scanStartBgRecognition` 唔准 await 個 poll —— 呢個就係「背景」。"""
    body = _js_body(tpl, "async _scanStartBgRecognition(")
    assert "await this._scanPollJob(" not in body, (
        "_scanStartBgRecognition 仲係 await 個 poll —— "
        "前端會block 住,唔算背景"
    )
    assert ".then(" in body and ".catch(" in body, (
        "背景 job 要用 .then()/.catch() 處理完成/失敗"
    )


def test_photos_picker_returns_before_recognition(tpl):
    """onScanPhotosPicked:開完 background job 就要 return,唔好等。"""
    body = _js_body(tpl, "async onScanPhotosPicked(")
    assert "await this._scanStartBgRecognition(" in body, (
        "onScanPhotosPicked 唔係用 background recognition"
    )
    # 唔准再直接 await 舊嘅 blocking helper
    assert "await this._scanRecognisePhase(" not in body, (
        "仲喺度 await blocking 版 _scanRecognisePhase"
    )
    # _scanStartBgRecognition 之後要有 return
    call_at = body.find("await this._scanStartBgRecognition(")
    after = body[call_at:]
    assert re.search(r"return;", after), (
        "開完 background job 之後要 return(唔好繼續 block 住)"
    )


def test_single_photo_path_is_background(tpl):
    """單張相(影相掣)都要背景 —— 唔准只有多張相先背景。"""
    body = _js_body(tpl, "async onScanFile(")
    assert "await this._scanStartBgRecognition(" in body, (
        "單張相路徑唔係 background"
    )
    assert "await this._scanRecognisePhase(" not in body, (
        "單張相路徑仲係 blocking"
    )


def test_camera_button_not_blocked_by_recognition(tpl):
    """相簿掣唔准因為 AI 背景識別而 disable。"""
    m = re.search(
        r":disabled=\"(scanUploading[^\"]*scanPhotosQueue\.some[^\"]*)\"",
        tpl,
    )
    assert m, "搵唔到相簿掣嘅 disabled expression"
    expr = m.group(1)
    assert "recognising" not in expr, (
        f"相簿掣仲會因為 'recognising' 狀態 disable:{expr}"
    )
    assert "'uploaded'" not in expr, (
        f"相簿掣仲會因為 'uploaded' 狀態 disable:{expr}"
    )
    assert "'pending'" in expr and "'uploading'" in expr, (
        f"但真係仲喺度上傳嘅時候要 disable:{expr}"
    )


def test_bg_state_present(tpl):
    """背景狀態變數一定要喺 Alpine state 度 declare。"""
    for key in ("scanQueueToken", "scanBgJobs", "scanBgDisplayJobId"):
        assert re.search(rf"^\s*{key}\s*:", tpl, re.MULTILINE), (
            f"Alpine state 冇 {key}"
        )


def test_queue_token_guard_present(tpl):
    """舊 background job 唔准寫入新 scan 嘅 queue(token guard)。"""
    body = _js_body(tpl, "async _scanPollJob(")
    assert "ownsQueue" in body, "_scanPollJob 冇 queue 身分檢查"
    assert "scanQueueToken" in tpl, "冇用 token 做 queue guard"
    # 新 scan 一定要 bump token
    assert re.search(r"this\.scanQueueToken\s*\+=\s*1", tpl), (
        "新 scan 唔會 bump token → 舊 job 會覆蓋新 queue"
    )


# ──────────────────────────────────────────────────────────────────────────
# 2. 平行:server 端
# ──────────────────────────────────────────────────────────────────────────

def test_scan_job_uses_thread_pool(gw):
    """_scan_run_job 要用 ThreadPoolExecutor 平行跑。"""
    body = _js_body_py(gw, "def _scan_run_job(")
    assert "ThreadPoolExecutor" in body, (
        "_scan_run_job 仲係 sequential —— 冇用 ThreadPoolExecutor"
    )
    assert "pool.submit(" in body, "ThreadPoolExecutor 冇用 submit"
    assert "workers > 1" in body, "冇 parallel / sequential 分支"


def _js_body_py(src: str, needle: str) -> str:
    idx = src.find(needle)
    assert idx >= 0, f"搵唔到 {needle}"
    m = re.compile(r"\ndef [a-zA-Z_]+\(").search(src, idx + len(needle))
    return src[idx: m.start()] if m else src[idx:]


def test_parallel_worker_count_configurable(gw):
    """平行 worker 數量要可以調(唔好 hardcode)。"""
    assert "_scan_parallel_workers" in gw, "冇 _scan_parallel_workers helper"
    assert "GYMBRO_SCAN_PARALLEL" in gw, "平行 worker 數唔可以調"
    m = re.search(r"def _scan_parallel_workers\(\).*?return max\(1, min\(8, n\)\)",
                  gw, re.DOTALL)
    assert m, "worker 數量要有上限(唔可以無限開線程)"


def test_sheet_write_is_serialised(gw):
    """🔴 安全線:production writer 必須 serialized。

    平行 AI worker 會同時叫 `_append_to_sheet_nutrition`。Token refresh +
    idempotency store + trim pre-flight 唔係 concurrency-safe。
    """
    assert "_serialise_sheet_write" in gw, "冇 sheet write 序列化機制"
    assert "@_serialise_sheet_write" in gw, (
        "_append_to_sheet_nutrition 冇掛序列化 decorator"
    )
    assert "_SHEET_WRITE_LOCK" in gw, "冇 process-wide sheet write lock"
    assert "threading.RLock()" in gw, (
        "要用 RLock(唔係 Lock)—— 避免將來互相叫時死鎖"
    )


def test_results_mapped_by_client_index(tpl):
    """🔴 安全線:結果對應要用 client_index,唔可以用 array 位置。

    平行模式下 results 唔保證順序 —— 用位置對返會寫錯人嘅記錄。
    """
    body = _js_body(tpl, "async _scanPollJob(")
    assert "res.client_index" in body or "_clientOf" in body, (
        "結果對應唔係用 client_index"
    )
    # 舊嘅位置式對應 `uploads[ri]` 唔應該再出現喺 results 對應
    assert not re.search(r"const up = uploads\[ri\];\s*\n\s*if \(!up\) return;",
                         body), (
        "仲用住位置式 uploads[ri] 對應 —— 平行模式會對錯相"
    )


def test_server_exposes_inflight(gw):
    """Server 要回 inflight 清單俾前端知道邊啲張仲喺度做。"""
    m = re.search(r"def api_scan_progress\(job_id: str\):(.*?)\n\n\n", gw, re.DOTALL)
    assert m, "搵唔到 api_scan_progress"
    body = m.group(1)
    assert '"inflight"' in body, "/api/scan_progress 冇回 inflight"
    assert '"completed"' in body, "/api/scan_progress 冇回 completed"


def test_pct_stays_monotonic_in_parallel(gw):
    """平行模式下 pct 必須單調遞增(high-water mark)。"""
    body = _js_body_py(gw, "def _scan_run_job(")
    assert "pct_high" in body, "平行模式冇 pct high-water mark"
    assert re.search(r"if new_pct > pct_high:", body), (
        "pct 冇用 high-water mark → 會倒退"
    )


def test_parallel_stage_labels_present(gw):
    """平行模式要有自己嘅 stage label(唔可以講「第 n 張」——會誤導)。"""
    assert "SCAN_STAGE_LABELS_PARALLEL" in gw, "冇平行版 stage label"
    assert "_scan_stage_label_parallel" in gw, "冇平行 label helper"
    m = re.search(r"def _scan_stage_label_parallel\(.*?SCAN_STAGE_LABELS_PARALLEL",
                  gw, re.DOTALL)
    assert m, "平行 label helper 唔係用 SCAN_STAGE_LABELS_PARALLEL"


def test_production_write_guard_still_inside_writer(gw):
    """v3.3.32 嘅統一寫入閘唔准被 v3.3.33 搞走。"""
    idx = gw.find("def _append_to_sheet_nutrition(")
    assert idx >= 0, "搵唔到 _append_to_sheet_nutrition"
    body = gw[idx: idx + 4000]
    assert "_sheet_write_guard(entry)" in body, (
        "_append_to_sheet_nutrition 唔再行統一寫入閘"
    )


def test_worker_counter_uses_nonlocal(gw):
    """🔴 `done_count += 1` 喺 worker closure 入面 → 一定要 nonlocal。

    2026-10-21 實測捉到:漏咗 nonlocal → UnboundLocalError →
    每一張相都變成 failed。
    """
    body = _js_body_py(gw, "def _scan_run_job(")
    i = body.find("def _process(idx: int, p: str):")
    assert i >= 0, "搵唔到 _process worker 函式"
    worker = body[i: i + 6000]
    assert "nonlocal done_count" in worker, (
        "_process 入面 `done_count += 1` 冇 nonlocal → UnboundLocalError"
    )


def test_job_persist_is_thread_safe(gw):
    """🔴 平行 worker 會同時寫 scan_jobs_state.json → 要有 persist lock。"""
    assert "_SCAN_JOBS_PERSIST_LOCK" in gw, "冇 job persist lock"
    body = _js_body_py(gw, "def _scan_jobs_persist(")
    assert "with _SCAN_JOBS_PERSIST_LOCK:" in body, (
        "_scan_jobs_persist 冇喺 persist lock 入面寫"
    )
    assert "with SCAN_JOBS_LOCK:" in body, (
        "_scan_jobs_persist 讀 SCAN_JOBS 冇喺 lock 入面影 snapshot "
        "(會拋 dictionary changed size during iteration)"
    )


def test_same_minute_same_calorie_not_deduped(gw):
    """🔴 同分鐘 + 同卡路里但唔同相 → 唔可以當重複(會漏寫 golden copy)。"""
    idx = gw.find("def _append_to_sheet_nutrition(")
    assert idx >= 0
    body = gw[idx: idx + 6000]
    assert "entry_img" in body, "dedup 冇比較相片 URL"
    assert "row_img != entry_img" in body, (
        "dedup 唔會因為相片唔同而放行 → 平行模式會漏寫行"
    )
