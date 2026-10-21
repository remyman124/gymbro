#!/usr/bin/env python3
"""Meta-test harness — 驗證 QA contract 測試 **真係有效** (唔係假 pass).

Isak 2026-10-20。

問題: Frimpong 嘅 backend 未上線,`test_scan_progress_contract.py` 全部會 skip。
skip 唔等於 pass —— 我要證明嗰啲測試一旦 backend 上線就真係會捉到 bug。

做法: 起一個 **mock gymbro server**,照 SCAN_PROGRESS_SPEC §2 實作三個 endpoint,
故意喺指定位植入指定 bug,然後行同一套 QA 測試,驗證:
  - 正確實作 → 全部 pass
  - 每個植入 bug → 對應測試 fail

用法:
    PYTHONPATH="" /usr/bin/python3 tests/qa/mock_backend_harness.py
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from flask import Flask, jsonify, request  # noqa: E402
from werkzeug.datastructures import FileStorage  # noqa: E402

SCAN_CACHE_DIR = Path("/home/work/.hermes/scan_cache")
FIXTURES = REPO / "test_fixtures"

# ── 可注入嘅 bug (module global,測試用 monkeypatch 改) ────────────────
BUGS = {
    "none": None,
    "slow_upload": False,      # upload 拖慢 → 違反 <5s
    "pct_regress": False,      # pct 會倒退 → 違反單調
    "stage_frozen": False,     # stage 唔變 → 違反 stage 會變
    "no_results": False,       # results 唔出
    "wrong_path": False,       # path 唔喺 SCAN_CACHE_DIR
}

JOBS = {}
JOBS_LOCK = threading.Lock()

STAGES = [
    ("vision", "AI 睇緊相…", 0),
    ("second_opinion", "第二個 AI 交叉檢查…", 12),
    ("enrich", "估算營養…", 25),
    ("enrich2", "交叉核對營養值…", 37),
    ("multi", "分析有幾多餸…", 50),
    ("brand", "對照品牌營養表…", 62),
    ("drive", "上載到雲端…", 75),
    ("commit", "寫入 Google Sheet…", 87),
]

app = Flask(__name__)


def _job_set(job_id, **kw):
    with JOBS_LOCK:
        cur = JOBS.get(job_id, {})
        cur.update(kw)
        cur["updated_at"] = time.time()
        JOBS[job_id] = cur


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/api/scan_upload", methods=["POST", "OPTIONS"])
def scan_upload():
    if request.method == "OPTIONS":
        return ("", 200)
    if BUGS["slow_upload"]:
        time.sleep(6.0)

    files = list(request.files.getlist("images")) or list(request.files.getlist("image"))
    if not files:
        return jsonify({"ok": False, "error": "no image"}), 400

    uploads = []
    now = time.strftime("%Y%m%d_%H%M%S")
    for i, f in enumerate(files):
        data = f.read()
        if len(data) > 10 * 1024 * 1024:
            return jsonify({"ok": False, "error": "image too large"}), 413
        name = f"upload_{now}_{i}.jpg"
        if BUGS["wrong_path"]:
            path = Path("/tmp/gymbro_qa_wrong_dir") / name
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path = SCAN_CACHE_DIR / name
        path.write_bytes(data)
        uploads.append({
            "client_index": int(request.form.get("client_index", i)),
            "filename": f.filename or name,
            "path": str(path),
            "image_url": f"/scan_img/{name}",
            "thumbnail_url": f"/scan_thumb/{name}",
            "size_kb": max(1, len(data) // 1024),
            "capture_iso": time.strftime("%Y-%m-%d"),
            "capture_hhmm": time.strftime("%H:%M"),
        })
    return jsonify({"ok": True, "uploads": uploads})


@app.route("/api/scan_recognize", methods=["POST"])
def scan_recognize():
    data = request.get_json(silent=True) or {}
    paths = data.get("paths") or []
    indexes = data.get("client_indexes") or list(range(len(paths)))
    if not paths:
        return jsonify({"ok": False, "error": "no paths"}), 400

    job_id = f"scanjob_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid():x}"
    total = len(paths)
    _job_set(job_id, status="running", stage="queued", pct=0, total=total,
             current_index=0, results=[], errors=[], stage_label="排隊中…",
             started=time.time())

    def worker():
        results = []
        for idx, p in enumerate(paths):
            for stage, label, base_pct in STAGES:
                if BUGS["stage_frozen"]:
                    # 只 freeze stage 名,label + pct 照樣行
                    # (咁 QA 測試就係因為「處理中 stage 只有一個」而 fail,
                    #  而唔係因為 pct 跌 —— 針對性測試 stage 變化)
                    stage = "vision"
                stage_pct = base_pct
                overall = int((idx * 100 + stage_pct) / total)
                if BUGS["pct_regress"] and stage == "enrich":
                    overall = 1  # 倒退 (瞬時,維持一個 stage 嘅時間)
                _job_set(job_id, stage=stage, stage_label=label, pct=overall,
                         current_index=idx,
                         elapsed_s=round(time.time() - JOBS.get(job_id, {}).get("started", time.time()), 2))
                time.sleep(0.12)
            if not BUGS["no_results"]:
                results.append({
                    "client_index": indexes[idx] if idx < len(indexes) else idx,
                    "ok": True,
                    "name": "測試菜式",
                    "calories": 500,
                    "protein": 30,
                    "committed": [{
                        "name": "測試菜式", "calories": 500, "protein": 30,
                        "grade": "B", "sheet_row": "Sheet1!A1",
                        "sheet_ok": True, "scan_index": 1,
                    }],
                    "preview": {"image_url": "", "thumbnail_url": "",
                                "suggested_entry": {"name": "測試菜式"}},
                    "image_url": "", "thumbnail_url": "",
                })
                _job_set(job_id, results=list(results))
        _job_set(job_id, status="completed", stage="done", stage_label="完成", pct=100)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "total": total})


@app.route("/api/scan_progress/<job_id>")
def scan_progress(job_id):
    with JOBS_LOCK:
        cur = dict(JOBS.get(job_id) or {})
    if not cur:
        return jsonify({"ok": False, "error": "no such job"}), 404
    return jsonify({
        "ok": True,
        "job_id": job_id,
        "status": cur.get("status"),
        "stage": cur.get("stage"),
        "stage_label": cur.get("stage_label"),
        "pct": cur.get("pct"),
        "current_index": cur.get("current_index", 0),
        "total": cur.get("total", 0),
        "results": cur.get("results", []),
        "errors": cur.get("errors", []),
        "elapsed_s": cur.get("elapsed_s", 0),
    })


# ── harness ────────────────────────────────────────────────────────────

def run_with_bug(bug: str, port: int):
    """起 mock server (指定 bug),跑 QA contract 測試,回傳 result。"""
    import subprocess
    BUGS["none"] = None
    BUGS["slow_upload"] = (bug == "slow_upload")
    BUGS["pct_regress"] = (bug == "pct_regress")
    BUGS["stage_frozen"] = (bug == "stage_frozen")
    BUGS["no_results"] = (bug == "no_results")
    BUGS["wrong_path"] = (bug == "wrong_path")

    t = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, threaded=True,
                               use_reloader=False),
        daemon=True,
    )
    t.start()
    time.sleep(1.5)

    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env["GYMBRO_URL"] = f"http://127.0.0.1:{port}"
    env["GYMBRO_QA_ALLOW_AI"] = "1"  # 開埋 AI 相關嘅測試
    proc = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/qa/test_scan_progress_contract.py",
         "-p", "no:randomly", "-q", "--tb=no", "-rf"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=600,
    )
    out = proc.stdout + proc.stderr
    failed = sorted(set(
        m.group(1) for m in __import__("re").finditer(r"^FAILED\s+(\S+)", out, __import__("re").MULTILINE)
    ))
    return proc.returncode, failed, out


CASES = [
    # (bug, 預期會 fail 嘅測試)
    ("none", []),
    ("slow_upload", ["test_scan_upload_returns_fast_without_ai"]),
    ("wrong_path", ["test_scan_upload_returns_fast_without_ai"]),
    ("pct_regress", ["test_scan_job_progress_is_monotonic"]),
    ("stage_frozen", ["test_scan_job_progress_is_monotonic"]),
    ("no_results", ["test_scan_job_progress_is_monotonic"]),
]


def main():
    print("=" * 70)
    print("  META-TEST — 驗證 QA contract 測試真係有效")
    print("=" * 70)
    ok = True
    port = 7311
    for bug, expect_fail in CASES:
        port += 1
        rc, failed, out = run_with_bug(bug, port)
        got = [f for f in failed if "test_scan_preview" not in f]
        print(f"\n▶ bug='{bug}'  pytest rc={rc}")
        print(f"  failed tests: {got or '(none)'}")
        print(f"  expected    : {expect_fail or '(none)'}")
        hit = any(any(e in f for f in got) for e in expect_fail)
        miss = [e for e in expect_fail if not any(e in f for f in got)]
        if bug == "none":
            if got:
                print(f"  ❌ 正確實作應該全部 pass,但 fail 咗")
                ok = False
            else:
                print("  ✅ 正確實作 → 全部 pass")
        else:
            if hit and not miss:
                print(f"  ✅ 捉到植入嘅 bug")
            else:
                print(f"  ❌ 漏捉: 預期 {expect_fail},實際 {got}")
                ok = False

    print()
    print("=" * 70)
    print("  ✅ META-TEST 全部通過 — QA 測試真係有效"
          if ok else "  ❌ META-TEST 有問題")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
