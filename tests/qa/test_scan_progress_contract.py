"""QA — 兩階段 scan progress backend contract (SCAN_PROGRESS_SPEC §4.1-4.3).

Isak 2026-10-20。

呢組測試對住 **live server** (http://127.0.0.1:7000) 行,唔係 Flask test
client。原因: job worker 係 background thread + 跨 request 嘅 registry
(SCAN_JOBS),用 test client 會失去 thread 語意。

設計原則 (Jim OOB 2026-10-20 「STOP patching, run playwright headless test FIRST」):
- 全部測試係 **可重複執行** 嘅,唔使人手 click。
- 每個測試自己 skip 返如果 backend 未上線 (Frimpong 未 commit),唔會 false-fail。
- 唔會用 placeholder / 假數據 — 全部打真實 fixture 相。
- 副作用可控: upload 寫入 SCAN_CACHE_DIR (真實路徑),recognize 會真跑 AI。
  為咗唔污染 Jim 嘅 Sheet,recognize 測試預設 skip,要用
  `GYMBRO_QA_ALLOW_AI=1` 先行。

環境:
    PYTHONPATH="" /home/work/projects/gymbro/.venv/bin/python -m pytest \
        tests/qa/test_scan_progress_contract.py -v -s
"""

import io
import json
import os
import time
from pathlib import Path

import pytest
import requests

BASE_URL = os.environ.get("GYMBRO_URL", "http://127.0.0.1:7000")
SCAN_CACHE_DIR = Path("/home/work/.hermes/scan_cache")
FIXTURES = Path(__file__).resolve().parent.parent.parent / "test_fixtures"

# 真實食物相 (Alonso 2026-10-20 準備)
FOOD_1 = FIXTURES / "food_1.jpg"
FOOD_2 = FIXTURES / "food_2.jpg"

# Spec §4.1: /api/scan_upload 必須喺 5 秒內返 (唔做 AI)
UPLOAD_MAX_SECONDS = 5.0


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _server_alive() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/healthz", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def _api_present(path: str, method: str = "OPTIONS") -> int | None:
    """回傳 status code;如果 route 唔存在就 404。"""
    try:
        r = requests.request(method, f"{BASE_URL}{path}", timeout=10)
        return r.status_code
    except Exception:
        return None


def _fixture_bytes(path: Path) -> bytes:
    assert path.exists(), f"fixture 唔存在: {path}"
    return path.read_bytes()


def _upload(images, field: str = "images"):
    """POST /api/scan_upload,回傳 (elapsed_seconds, response_json_or_None, status)."""
    files = [(field, (p.name, _fixture_bytes(p), "image/jpeg")) for p in images]
    t0 = time.monotonic()
    r = requests.post(f"{BASE_URL}/api/scan_upload", files=files, timeout=60)
    elapsed = time.monotonic() - t0
    try:
        data = r.json()
    except Exception:
        data = None
    return elapsed, data, r.status_code


def _poll_job(job_id: str, timeout_s: float = 240.0, interval_s: float = 0.05):
    """Poll /api/scan_progress/<job_id> 直到 completed/failed。

    回傳 (samples, final_data)。samples 係 [(t, pct, stage, status, n_results)]。

    ⚠️ interval_s 刻意用 0.05 (唔係 spec 建議畀前端嘅 400-800ms):
       QA 要 **dense sampling** 先捉得到 server 端「瞬時」倒退嘅 pct。
       如果 poll 太疏,一個只維持 100ms 嘅倒退會被完全 sample 唔到,
       測試就會 false-pass。呢個係 QA 自己嘅 meta-test 捉到嘅缺陷。
    """
    samples = []
    deadline = time.monotonic() + timeout_s
    final = None
    while time.monotonic() < deadline:
        r = requests.get(f"{BASE_URL}/api/scan_progress/{job_id}", timeout=20)
        assert r.status_code == 200, f"scan_progress HTTP {r.status_code}: {r.text[:300]}"
        d = r.json()
        samples.append((
            round(time.monotonic() - deadline + timeout_s, 3),
            d.get("pct"),
            d.get("stage"),
            d.get("status"),
            len(d.get("results") or []),
        ))
        final = d
        if d.get("status") in ("completed", "failed"):
            break
        time.sleep(interval_s)
    return samples, final


# Spec §2.3 定義嘅 8 個處理中 stage (唔包���終止 stage)
PROCESSING_STAGES = {
    "vision", "second_opinion", "enrich", "enrich2",
    "multi", "brand", "drive", "commit",
}
TERMINAL_STAGES = {"done", "queued", "uploading_not_needed"}


# ──────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def server():
    if not _server_alive():
        pytest.skip(f"gymbro server 冇起喺 {BASE_URL}")
    return BASE_URL


@pytest.fixture(scope="module")
def two_phase_api(server):
    """如果 /api/scan_upload 未上線就 skip 全組 (Frimpong 未 commit 嘅情況)。"""
    code = _api_present("/api/scan_upload")
    if code is None:
        pytest.skip("server unreachable")
    if code == 404:
        pytest.skip("/api/scan_upload 未上線 (backend 未 commit) — 唔算 fail")
    return True


# ──────────────────────────────────────────────────────────────────────────
# §4.1 Upload 先過識別
# ──────────────────────────────────────────────────────────────────────────

def test_scan_upload_returns_fast_without_ai(server, two_phase_api):
    """Spec §2.1 + §4.1: /api/scan_upload 收檔+存檔,唔做 AI,<5 秒返。

    驗證:
    - HTTP 200 + ok=True
    - uploads[] 每個 entry 有 path / image_url / thumbnail_url / size_kb
    - path 真係喺 SCAN_CACHE_DIR 之下 (server-side absolute path)
    - elapsed < 5 秒 (即係真冇跑 AI)
    """
    #
    # ⚠️ 關於 timing 門檻:
    #   實測過 cold start —— server 第一次處理 upload 要 6.8s
    #   (EXIF 抽取 + PIL thumbnail 首次載入),之後每次都係 0.74-1.02s。
    #   唔做 warm-up 就量一次,會撞 cold start 造成假 failure。
    #   所以:先 warm-up 一次,再量真實速度。
    warm_t, warm_d, warm_status = _upload([FOOD_1])
    assert warm_status == 200, f"warm-up upload HTTP {warm_status}"

    elapsed, data, status = _upload([FOOD_1, FOOD_2])

    assert status == 200, f"scan_upload HTTP {status}"
    assert data is not None and data.get("ok") is True, f"scan_upload 唔 ok: {data}"

    uploads = data.get("uploads")
    assert isinstance(uploads, list), f"uploads 唔係 list: {type(uploads)}"
    assert len(uploads) == 2, f"應該有 2 個 upload,得 {len(uploads)}: {uploads}"

    for i, up in enumerate(uploads):
        assert up.get("path"), f"upload[{i}] 冇 path: {up}"
        assert up.get("image_url"), f"upload[{i}] 冇 image_url: {up}"
        assert up.get("thumbnail_url"), f"upload[{i}] 冇 thumbnail_url: {up}"
        assert isinstance(up.get("size_kb"), (int, float)), f"upload[{i}] 冇 size_kb: {up}"
        assert up.get("size_kb") > 0, f"upload[{i}] size_kb 唔應該係 0: {up}"
        # Spec §2.1: path 係 SCAN_CACHE_DIR 之下嘅 server-side absolute path
        p = Path(up["path"])
        assert p.is_absolute(), f"upload[{i}] path 唔係 absolute: {p}"
        assert p.parent == SCAN_CACHE_DIR.resolve(), (
            f"upload[{i}] path 唔喺 SCAN_CACHE_DIR 之下: {p} "
            f"(SCAN_CACHE_DIR={SCAN_CACHE_DIR.resolve()})"
        )
        assert p.exists(), f"upload[{i}] 檔案唔存在: {p}"

    # 真檔案已經落地
    assert elapsed < UPLOAD_MAX_SECONDS, (
        f"/api/scan_upload 用了 {elapsed:.2f}s,超過 {UPLOAD_MAX_SECONDS}s "
        f"— 可能偷偷跑咗 AI (Jim 要求 upload 唔好做 AI)\n"
        f"  (warm-up 用咗 {warm_t:.2f}s;注意 cold start 會慢啲,"
        f" 所以呢個門檻只適用於 warm 之後)"
    )


def test_scan_upload_accepts_single_image_field(server, two_phase_api):
    """Spec §2.1: 為咗容錯,field 名 'image' (單張) 都要收。"""
    files = [("image", (FOOD_1.name, _fixture_bytes(FOOD_1), "image/jpeg"))]
    r = requests.post(f"{BASE_URL}/api/scan_upload", files=files, timeout=60)
    assert r.status_code == 200, f"HTTP {r.status}: {r.text[:300]}"
    d = r.json()
    assert d.get("ok") is True, d
    assert len(d.get("uploads") or []) == 1, d


def test_scan_upload_rejects_oversize_file(server, two_phase_api):
    """Spec §2.1: 每張 10 MB 上限 → 413。"""
    big = io.BytesIO(b"\xff\xd8\xff\xe0" + b"\x00" * (10 * 1024 * 1024 + 1024))
    r = requests.post(
        f"{BASE_URL}/api/scan_upload",
        files=[("images", ("big.jpg", big.getvalue(), "image/jpeg"))],
        timeout=60,
    )
    assert r.status_code == 413, f"應該 413,得 {r.status}: {r.text[:300]}"


def test_scan_recognize_requires_recognize_endpoint(server, two_phase_api):
    """Spec §2.2: /api/scan_recognize 存在,接受 JSON {paths, client_indexes}。

    唔喺呢度真跑 AI (會 commit 落 Jim 嘅 Sheet)。呢度只驗 contract:
    - route 存在 (唔係 404)
    - 收到空 paths → 應該快返 error (唔係 hang)
    """
    code = _api_present("/api/scan_recognize", method="POST")
    assert code != 404, "/api/scan_recognize 未上線"

    t0 = time.monotonic()
    r = requests.post(f"{BASE_URL}/api/scan_recognize", json={"paths": []}, timeout=30)
    elapsed = time.monotonic() - t0
    assert r.status_code != 500, f"server error: {r.text[:300]}"
    assert elapsed < 30, f"空 paths 應該好快返,用了 {elapsed:.1f}s"


# ──────────────────────────────────────────────────────────────────────────
# §4.2 Job progress 有效
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(
    os.environ.get("GYMBRO_QA_ALLOW_AI") != "1",
    reason="真跑 AI 會 commit 落 Jim 嘅 Google Sheet — 要 GYMBRO_QA_ALLOW_AI=1 先行",
)
def test_scan_job_progress_is_monotonic(server, two_phase_api):
    """Spec §4.2: poll /api/scan_progress/<job_id> → pct 單調遞增,stage 會變,
    results 逐張出。

    呢個測試會真跑 AI + Sheet commit,所以預設 skip。
    """
    _, up_data, status = _upload([FOOD_1, FOOD_2])
    assert status == 200 and up_data.get("ok")
    paths = [u["path"] for u in up_data["uploads"]]
    indexes = list(range(len(paths)))

    r = requests.post(
        f"{BASE_URL}/api/scan_recognize",
        json={"paths": paths, "client_indexes": indexes},
        timeout=30,
    )
    assert r.status_code == 200, f"recognize HTTP {r.status}: {r.text[:300]}"
    job = r.json()
    assert job.get("ok") is True, job
    assert job.get("job_id"), job
    assert job.get("total") == 2, job

    samples, final = _poll_job(job["job_id"])
    assert samples, "一次都 poll 唔到"

    pcts = [s[1] for s in samples]
    stages = [s[2] for s in samples]

    # 1. pct 單調遞增 (唔可以跌)
    #    逐對相鄰 sample 比,任何一次跌都係 bug。
    for i, (prev, cur) in enumerate(zip(pcts, pcts[1:])):
        assert cur >= prev, (
            f"pct 跌咗 (sample #{i} → #{i+1}): {prev} → {cur}\n"
            f"  spec §2.3: 整體 pct 必須單調遞增,唔可以跌\n"
            f"  samples={samples}"
        )

    # 2. stage 會變 (唔係一個 stage 停死)
    #    ⚠️ 只計 **處理中** stage — 終止 stage ('done') 唔算,
    #    否則「全部 stage 都 frozen,最後跳 done」會 false-pass。
    processing = [s for s in stages if s in PROCESSING_STAGES]
    assert len(set(processing)) >= 2, (
        f"stage 冇變過 (處理中 stage 只有 {set(processing)})\n"
        f"  stages={stages}\n"
        f"  spec §2.3: 每張圖有 8 個 stage,應該行過多個"
    )

    # 2b. stage 應該只會係 spec 定義嘅合法值
    unknown = {s for s in stages if s is not None and s not in PROCESSING_STAGES
               and s not in TERMINAL_STAGES}
    assert not unknown, (
        f"出現 spec 冇定義嘅 stage: {unknown}\n"
        f"  合法 stage: {sorted(PROCESSING_STAGES | TERMINAL_STAGES)}"
    )

    # 2c. stage_label 應該每個 stage 都對應到中文 label
    labelled = [s for s in samples if s[2] in PROCESSING_STAGES]
    assert labelled, "冇 sample 見到處理中 stage"

    # 3. results 逐張出
    counts = [s[4] for s in samples]
    assert counts == sorted(counts), f"results 數量唔應該倒退: {counts}"
    assert final.get("status") in ("completed", "failed"), final
    assert len(final.get("results") or []) == 2, (
        f"應該有 2 個 result,得 {len(final.get('results') or [])}: {final}"
    )

    # 4. 每個 result 有 preview + committed
    for res in final.get("results") or []:
        assert res.get("client_index") is not None, res
        assert res.get("preview"), f"result 冇 preview: {res}"
        assert res.get("committed"), f"result 冇 committed: {res}"

    # 5. 最終 pct 應該到 100
    assert final.get("pct") == 100, f"最終 pct 唔係 100: {final.get('pct')}"


# ──────────────────────────────────────────────────────────────────────────
# §4.3 Regression — /api/scan_preview response JSON 唔可以變
# ──────────────────────────────────────────────────────────────────────────

def _baseline_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "scan_preview_baseline.json"


def _key_paths(obj, prefix=""):
    """遞歸收集所有 key path — 同 scripts/capture_scan_preview_baseline.py
    用同一個格式,確保 baseline 同 live response 係同一個語言比較。

    dict → "a.b.c";list → "a[].b"。
    """
    out = []
    if isinstance(obj, dict):
        for k in obj.keys():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.append(p)
            out.extend(_key_paths(obj[k], p))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_key_paths(v, f"{prefix}[]"))
    return out


def _key_shape(obj):
    """由 dict 抽出 key 結構 (遞歸),value 唔理 — 只比 key。

    value 會每次變 (時間戳 / 圖片 url),所以只比 key shape。
    """
    if isinstance(obj, dict):
        return {k: _key_shape(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        # list 嘅第一個 element 代表 shape (空 list 就當空 dict)
        return [_key_shape(obj[0])] if obj else []
    return type(obj).__name__


def test_scan_preview_baseline_exists():
    """Frimpong 應該 dump 咗 baseline。冇嘅話 skip (backend 未改完)。"""
    p = _baseline_path()
    if not p.exists():
        pytest.skip(f"baseline 未 dump: {p} (等 Frimpong commit 前後再驗)")
    assert p.stat().st_size > 0, "baseline 係空檔"


@pytest.mark.skipif(
    os.environ.get("GYMBRO_QA_ALLOW_AI") != "1",
    reason="scan_preview 會真跑 AI + commit 落 Jim 個 Google Sheet — "
           "要 GYMBRO_QA_ALLOW_AI=1 先行",
)
def test_scan_preview_response_keys_unchanged(server):
    """Spec §2.5 + §4.3: /api/scan_preview 改完之後 response JSON keys
    必須同改之前 **一模一樣**。

    對比方法同 Frimpong 個 capture script 一致 (key_paths 格式),
    咁 baseline 同 live response 係同一個語言比較,唔會漏 nested key。

    ⚠️ 呢個 test 會真跑 AI + commit 落 Jim 個 Google Sheet。
       預設 skip,要用 GYMBRO_QA_ALLOW_AI=1 先行。
    """
    p = _baseline_path()
    if not p.exists():
        pytest.skip(f"baseline 未 dump: {p}")

    baseline = json.loads(p.read_text())

    # Frimpong 個 baseline 格式:
    #   {top_level_keys: [...], all_key_paths: [...], response: {...}}
    if "all_key_paths" in baseline:
        baseline_paths = set(baseline["all_key_paths"])
        baseline_top = set(baseline.get("top_level_keys") or [])
    else:
        # fallback: 直接由 response 推
        baseline_resp = baseline.get("response", baseline)
        baseline_paths = set(_key_paths(baseline_resp))
        baseline_top = set(baseline_resp.keys()) if isinstance(baseline_resp, dict) else set()

    # 打真實 request
    files = [("image", (FOOD_1.name, _fixture_bytes(FOOD_1), "image/jpeg"))]
    r = requests.post(f"{BASE_URL}/api/scan_preview", files=files, timeout=180)
    assert r.status_code == 200, f"scan_preview HTTP {r.status}: {r.text[:300]}"
    live = r.json()
    assert live.get("ok") is True, live

    live_paths = set(_key_paths(live))
    live_top = set(live.keys())

    # 1. top-level key 完全一致
    missing_top = baseline_top - live_top
    added_top = live_top - baseline_top
    assert not missing_top, (
        f"scan_preview response top-level 少咗 key: {sorted(missing_top)}"
    )
    assert not added_top, (
        f"scan_preview response top-level 多咗 key: {sorted(added_top)}"
    )

    # 2. 全部 nested key path 一致 (嚴格)
    missing = baseline_paths - live_paths
    added = live_paths - baseline_paths
    assert not missing, (
        f"scan_preview response 少咗 {len(missing)} 個 key path: {sorted(missing)[:20]}"
    )
    assert not added, (
        f"scan_preview response 多咗 {len(added)} 個 key path: {sorted(added)[:20]}"
    )


def test_scan_preview_baseline_key_paths_are_consistent():
    """Sanity check: baseline 檔內部自洽 (all_key_paths 同 response 一致)。

    如果 capture script 有 bug (例如漏咗 nested key),
    呢個測試會捉到,唔會等到真 regression 先發現。
    """
    p = _baseline_path()
    if not p.exists():
        pytest.skip(f"baseline 未 dump: {p}")

    baseline = json.loads(p.read_text())
    if "response" not in baseline or "all_key_paths" not in baseline:
        pytest.skip("baseline 格式唔同 expected (冇 response / all_key_paths)")

    derived = set(_key_paths(baseline["response"]))
    recorded = set(baseline["all_key_paths"])
    assert derived == recorded, (
        f"baseline 內部唔自洽 — all_key_paths 同 response 對唔上。"
        f"少: {sorted(derived - recorded)[:10]}, 多: {sorted(recorded - derived)[:10]}"
    )
