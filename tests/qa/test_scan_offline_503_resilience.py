"""QA — v3.3.51: SW 合成 503 唔可以令前端誤報 scan 失敗。

背景 (2026-10-27, Jim OOB「Got an 503 error after uploaded the food image」):

實測證據:
  * server access log 喺 06:50:51–06:51:04 UTC 期間,每一次
    GET /api/scan_progress/<job_id> 都係 **200**,從來冇 503。
  * tailscaled log 同一時段:`TLS handshake error … connection reset by peer`
    → Jim 嘅 iPhone 隧道 reset → poll 嘅 *response* 喺返程跌咗。
  * Service Worker 嘅 `/api/` 分支喺 fetch() reject 時會**自造**一個
    HTTP 503 `{"error":"offline"}`。
  * 前端舊版 `if (!r.ok) throw` → 顯示「查 AI 進度失敗 HTTP 503」。
  * 但 server 個 job 一路跑到 `done pct=100 status=completed`,仲成功寫入
    咗 Sheet(「牛油果炒蛋碗」330 kcal,14:52:34 HKT)。

即係話:Jim 見到「失敗」,但個 scan 其實成功入咗賬 —— 呢個係最差嘅
failure mode(假陰性 + 冇自癒)。

呢個 suite 守住 5 件事:
  1. SW 每一個自造嘅 503 都要帶 `X-Gymbro-Offline: 1` header + body
     `offline:true`(前端靠佢分得清「網絡閃斷」同「真 server 503」)。
  2. 前端 poll 迴圈見到 offline 503 唔可以 throw —— 個 job 仲喺 server
     跑緊,應該繼續 poll。
  3. POST /api/scan_recognize 必須冪等(client_token)—— 否則重試會開兩個
     job,同一張相寫入 Sheet 兩次。
  4. SW cache 版本要 bump(v154 → v155),否則 Jim 個舊 SW 唔會換新。
  5. 要有 recovery path:重新接管仲喺 server 跑緊嘅 job + 重新識別掣。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "gym_web.py"
TPL = ROOT / "templates" / "index.html"


def _web_text():
    return WEB.read_text(encoding="utf-8")


def _tpl_text():
    return TPL.read_text(encoding="utf-8")


def _sw_source():
    """Extract the Service Worker JS (it's embedded in gym_web.py)."""
    txt = _web_text()
    m = re.search(r"SERVICE_WORKER_JS\s*=\s*r?['\"]{3}(.*?)['\"]{3}", txt, re.S)
    if m:
        return m.group(1)
    # Fallback: the SW lives in a big template literal.
    m = re.search(r"self\.addEventListener\('fetch'.*?const CACHE = 'gym-web-v\d+';", txt, re.S)
    return m.group(0) if m else txt


# ---------------------------------------------------------------------------
# 1. SW 合成 503 必須自我標記
# ---------------------------------------------------------------------------

def test_sw_fabricated_503_carries_offline_marker():
    """Every SW-synthesised 503 must be identifiable as an offline blip."""
    sw = _sw_source()
    assert sw, "could not locate the Service Worker source"

    # Every synthetic 503 in the SW must carry the marker header.
    synth_503s = re.findall(r"new Response\((?:[^()]|\([^()]*\))*?status:\s*503[^;]*?\)\)", sw, re.S)
    assert synth_503s, "no synthetic 503 found in the Service Worker"

    unmarked = [s for s in synth_503s if "X-Gymbro-Offline" not in s]
    assert not unmarked, (
        "these synthetic 503s are missing the X-Gymbro-Offline marker, so the "
        "frontend cannot tell an offline blip from a real server 503:\n%s"
        % "\n---\n".join(unmarked)
    )


def test_sw_offline_body_has_offline_flag():
    """The synthetic 503 body must also carry offline:true as a fallback.

    Some fetch paths strip custom headers (and a cached/serialised Response
    may not expose them), so the JSON body is the second signal.
    """
    sw = _sw_source()
    assert "offline:true" in sw, "synthetic 503 body must include offline:true"


def test_sw_cache_version_bumped():
    """SW cache must be bumped or Jim's installed PWA keeps the old worker."""
    sw = _sw_source()
    m = re.search(r"const CACHE = 'gym-web-v(\d+)';", sw)
    assert m, "could not find the SW cache version"
    assert int(m.group(1)) >= 155, (
        "SW cache is still v%s — the installed iOS PWA would keep the old "
        "Service Worker and never receive the offline marker" % m.group(1)
    )


# ---------------------------------------------------------------------------
# 2. 前端 poll 唔可以因為 offline 503 而當 job 死咗
# ---------------------------------------------------------------------------

def test_frontend_poll_does_not_throw_on_offline_503():
    """The poll loop must treat a SW-fabricated 503 as transient."""
    tpl = _tpl_text()
    assert "X-Gymbro-Offline" in tpl, (
        "the frontend never reads the X-Gymbro-Offline marker — it will still "
        "treat an offline blip as a dead job"
    )

    # The old bug: `if (!r.ok) throw new Error('查 AI 進度失敗 HTTP ' + r.status)`
    assert "if (!r.ok) throw new Error('查 AI 進度失敗 HTTP ' + r.status);" not in tpl, (
        "the poll loop still throws on any non-ok response — an offline blip "
        "will be reported as a failed scan"
    )

    assert "MAX_NET_BLIPS" in tpl, "the poll loop must tolerate consecutive blips"


def test_frontend_poll_handles_fetch_rejection():
    """A thrown fetch (no response at all) must also be treated as transient."""
    tpl = _tpl_text()
    assert re.search(r"_offline\s*=\s*true", tpl), (
        "the poll loop must handle fetch() rejection as an offline blip"
    )


def test_frontend_bg_failure_does_not_mark_photo_failed_on_network_blip():
    """A network blip must not paint the photo red / report a failed scan."""
    tpl = _tpl_text()
    assert "rescanUploadedPhotos" in tpl, "the recovery re-scan action is missing"
    # The failed-state branch must keep the uploaded status for network blips.
    assert re.search(r"_keepUpload\s*\?\s*'uploaded'\s*:\s*'failed'", tpl), (
        "a network blip must keep the photo in the 'uploaded' state instead of "
        "marking it 'failed'"
    )


# ---------------------------------------------------------------------------
# 3. scan_recognize 必須冪等(否則重試會重複寫入 Sheet)
# ---------------------------------------------------------------------------

def test_scan_recognize_is_idempotent():
    """POST /api/scan_recognize must reuse the job for a repeated token."""
    web = _web_text()
    assert "client_token" in web, (
        "/api/scan_recognize does not accept a client_token — a retry after an "
        "offline blip would start a SECOND job and write the meal twice"
    )
    assert "_scan_idem_lookup" in web and "_scan_idem_store" in web, (
        "the idempotency helpers are missing"
    )

    # The lookup must actually be wired into the route.
    route = re.search(
        r"def api_scan_recognize\(\).*?return jsonify\(\{\"ok\": True, \"job_id\"",
        web, re.S,
    )
    assert route, "could not locate api_scan_recognize"
    body = route.group(0)
    assert "_scan_idem_lookup" in body, (
        "api_scan_recognize never calls _scan_idem_lookup — the token is "
        "accepted but ignored, so retries still double-write"
    )


def test_frontend_sends_client_token():
    """The frontend must send a stable client_token so retries are safe."""
    tpl = _tpl_text()
    assert "_scanClientToken" in tpl, "the client token helper is missing"
    assert "client_token" in tpl, "the client token is not sent to the server"
    # And it must be derived from the paths (stable across retries).
    assert re.search(r"_scanClientToken\(paths\)", tpl), (
        "the client token must be derived from the uploaded paths so a retry "
        "produces the same token"
    )


def test_frontend_recognise_post_retries_offline():
    """The recognise POST must retry on an offline 503."""
    tpl = _tpl_text()
    assert "_scanStartRecogniseJob" in tpl, (
        "the recognise POST is not wrapped in an offline-tolerant helper"
    )
    # Both call sites must go through the helper.
    assert tpl.count("_scanStartRecogniseJob(") >= 3, (
        "both _scanRecognisePhase and _scanStartBgRecognition must use "
        "_scanStartRecogniseJob"
    )


# ---------------------------------------------------------------------------
# 4/5. Recovery path
# ---------------------------------------------------------------------------

def test_server_exposes_active_jobs_endpoint():
    """The frontend needs a way to re-attach to a surviving server-side job."""
    web = _web_text()
    assert '@app.route("/api/scan_jobs/active"' in web, (
        "the /api/scan_jobs/active endpoint is missing — after a reload the "
        "frontend cannot re-attach to a job that is still running"
    )


def test_frontend_reattaches_on_boot():
    """The app must pick up a running job on boot."""
    tpl = _tpl_text()
    assert "reattachActiveScanJobs" in tpl, "the boot-time re-attach is missing"
    assert "/api/scan_jobs/active" in tpl, "the frontend never queries the active-jobs endpoint"


def test_rescan_button_exists_in_ui():
    """There must be a real button Jim can tap to retry."""
    tpl = _tpl_text()
    assert "@click=\"rescanUploadedPhotos()\"" in tpl, (
        "the rescan button is not wired into the UI"
    )


# ---------------------------------------------------------------------------
# 回歸守衛:呢次修正唔准改壞其他嘢
# ---------------------------------------------------------------------------

def test_production_write_paths_untouched():
    """The v3.3.51 fix must not touch the food-scan / Sheet write paths.

    (Constraint: /api/scan_commit, /api/scan_preview, _append_to_sheet_nutrition,
    _minimax_vision, _extract_dish_name are off-limits.)
    """
    web = _web_text()
    for fn in ("_append_to_sheet_nutrition", "_minimax_vision", "_extract_dish_name"):
        # These must still exist (not renamed/removed).
        assert f"def {fn}" in web, f"{fn} disappeared — that is out of scope for this fix"
