#!/usr/bin/env python3
"""QA — 喺 **真實 browser** 入面驗 Torres 嘅前端改動 (唔靠 :7000 server).

Isak 2026-10-20。

點解要呢個?
  gym_web.py 喺 module import 時讀 template 入記憶體 (HTML_TEMPLATE),
  所以 :7000 個 server 要 restart 先見到 Torres 嘅新前端。
  QA 唔可以自己 restart (會斬斷 alonso session,而且 Frimpong 仲做緊)。

呢個 harness 起一個 **臨時 Flask server 喺另一個 port**,serve 磁碟上
最新嘅 `templates/index.html`,用 Playwright headless 開真實 browser 驗:

  - 兩條進度條 DOM 真係 render 咗 (唔係淨係喺 template 檔入面)
  - Alpine state 綁定真係行得 (scanPhase / scanUploadPct / scanAiPct)
  - 上傳 bar 真係有 byte 進度 (XHR onprogress → scanUploadPct)
  - AI bar 真係跟 server stage_label 郁
  - **Phase 1 全部上傳完先開始 Phase 2** (Jim 嘅核心要求) — 睇真實 network log
  - 零 console error

用法:
    PYTHONPATH="" /usr/bin/python3 tests/qa/browser_frontend_harness.py
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
from playwright.sync_api import sync_playwright  # noqa: E402

TEMPLATE = REPO / "templates" / "index.html"
FIXTURES = REPO / "test_fixtures"
PORT = int(os.environ.get("GYMBRO_QA_BROWSER_PORT", "7402"))

# ── Mock backend:照 Torres 報告 §7 嘅契約 ───────────────────────────────
CALLS = []
JOBS = {}
JOBS_LOCK = threading.Lock()

app = Flask(__name__)
app.jinja_env.auto_reload = True


@app.route("/")
def index():
    return TEMPLATE.read_text(encoding="utf-8")


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/static/<path:p>")
def static_files(p):
    f = REPO / "static" / p
    if f.exists():
        return f.read_bytes(), 200, {"Content-Type": "application/octet-stream"}
    return ("not found", 404)


@app.route("/manifest.json")
def manifest():
    return jsonify({"name": "Gymbro", "display": "standalone"})


@app.route("/sw.js")
def sw():
    return ("// noop", 200, {"Content-Type": "application/javascript"})


@app.route("/api/scan_upload", methods=["POST", "OPTIONS"])
def scan_upload():
    if request.method == "OPTIONS":
        return ("", 200)
    CALLS.append(("upload", time.time()))
    files = list(request.files.getlist("images")) or list(request.files.getlist("image"))
    uploads = []
    for i, f in enumerate(files):
        data = f.read()
        name = f"upload_qa_{int(time.time()*1000)}_{i}.jpg"
        uploads.append({
            "client_index": int(request.form.get("client_index", i)),
            "filename": f.filename or name,
            "path": f"/home/work/.hermes/scan_cache/{name}",
            "image_url": f"/scan_img/{name}",
            "thumbnail_url": f"/scan_thumb/{name}",
            "size_kb": max(1, len(data) // 1024),
            "capture_iso": "2026-10-20",
            "capture_hhmm": "13:05",
        })
    return jsonify({"ok": True, "uploads": uploads})


@app.route("/api/scan_recognize", methods=["POST"])
def scan_recognize():
    CALLS.append(("recognize", time.time()))
    data = request.get_json(silent=True) or {}
    paths = data.get("paths") or []
    job_id = f"scanjob_qa_{int(time.time()*1000)}"
    with JOBS_LOCK:
        JOBS[job_id] = {"total": len(paths), "poll": 0}
    return jsonify({"ok": True, "job_id": job_id, "total": len(paths)})


@app.route("/api/scan_progress/<job_id>")
def scan_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "no job"}), 404
        job["poll"] += 1
        n = job["poll"]
        total = job["total"]

    stages = [
        ("vision", "AI 睇緊相…", 10),
        ("second_opinion", "第二個 AI 交叉檢查…", 25),
        ("enrich", "估算營養…", 45),
        ("multi", "分析有幾多餸…", 65),
        ("drive", "上載到雲端…", 85),
        ("commit", "寫入 Google Sheet…", 96),
    ]
    idx = min((n - 1) // 2, total - 1)
    stage, label, pct = stages[min((n - 1) % len(stages), len(stages) - 1)]
    overall = int((idx * 100 + pct) / total)

    results = []
    done = min(n // 3, total)
    for k in range(done):
        results.append({
            "client_index": k, "ok": True, "name": f"測試菜式{k+1}",
            "calories": 500, "protein": 30,
            "committed": [{"name": f"測試菜式{k+1}", "calories": 500,
                           "protein": 30, "grade": "B", "sheet_row": "Sheet1!A1",
                           "sheet_ok": True, "scan_index": k + 1}],
            "preview": {"image_url": "", "thumbnail_url": "",
                        "suggested_entry": {"name": f"測試菜式{k+1}"}},
            "image_url": "", "thumbnail_url": "",
        })

    status = "completed" if n >= total * 3 + 2 else "running"
    return jsonify({
        "ok": True, "job_id": job_id, "status": status,
        "stage": stage, "stage_label": label, "pct": overall,
        "current_index": idx, "total": total,
        "results": results, "errors": [], "elapsed_s": round(n * 0.7, 1),
    })


@app.route("/api/state")
def state():
    return jsonify({"ok": True, "today": "2026-10-20"})


@app.route("/api/<path:p>")
def api_stub(p):
    return jsonify({"ok": True, "items": [], "scans": [], "data": []})


# ── Harness ────────────────────────────────────────────────────────────

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail else ""),
          flush=True)


def main():
    print("=" * 70)
    print("  BROWSER FRONTEND HARNESS — 真實 Chromium 驗 Torres 前端")
    print("=" * 70)

    t = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT, threaded=True,
                               use_reloader=False),
        daemon=True,
    )
    t.start()
    time.sleep(1.5)

    base = f"http://127.0.0.1:{PORT}"
    fixture = FIXTURES / "food_1.jpg"

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox"])
        # service_workers='block' 一定要加 — gymbro 有 sw.js,
        # service worker 會 bypass Playwright 嘅 page.route(),
        # 令所有 mock 靜靜哋失效 (實測: SW on → 0 route hits)
        ctx = b.new_context(viewport={"width": 393, "height": 852},
                            is_mobile=True, has_touch=True,
                            service_workers="block")
        pg = ctx.new_page()
        console_errors = []
        pg.on("console", lambda m: console_errors.append(m.text)
              if m.type == "error" else None)
        pg.on("pageerror", lambda e: console_errors.append(str(e)))

        pg.goto(f"{base}/", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_function(
            "() => document.querySelector('[x-data]') && window.Alpine !== undefined",
            timeout=15000,
        )
        pg.wait_for_timeout(1200)

        print("\n── A. 進度條 DOM 真係 render 咗 ──")
        check("[data-scan-progress] 存在",
              pg.locator("[data-scan-progress]").count() >= 1)
        check("[data-scan-upload-bar] 存在",
              pg.locator("[data-scan-upload-bar]").count() >= 1)
        check("[data-scan-ai-bar] 存在",
              pg.locator("[data-scan-ai-bar]").count() >= 1)
        check("[data-scan-stage-label] 存在",
              pg.locator("[data-scan-stage-label]").count() >= 1)

        print("\n── B. idle 狀態收埋 (唔阻 Jim 影相) ──")
        check("scanPhase='idle' 時 container 唔顯示",
              not pg.locator("[data-scan-progress]").first.is_visible())

        print("\n── C. 揀 3 張相 → 真實上傳 → 真實 Phase 2 ──")
        CALLS.clear()
        # 真實檔案入 input (multi)
        files = [str(fixture)] * 3
        pg.set_input_files("input[x-ref='scanPhotosInputEl']", files)

        # 等 Phase 1 完 (scanPhase → recognising 或 done)
        try:
            pg.wait_for_function(
                "() => { const r=document.querySelector('[x-data]');"
                " const st=r&&r._x_dataStack&&r._x_dataStack[0];"
                " return st && (st.scanPhase==='recognising'||st.scanPhase==='done'); }",
                timeout=25000,
            )
        except Exception:
            pass

        state = pg.evaluate("""() => {
            const r=document.querySelector('[x-data]');
            const st=r._x_dataStack[0];
            return {phase:st.scanPhase, upPct:st.scanUploadPct,
                    bytes:st.scanUploadBytesLabel, upTotal:st.scanUploadTotal,
                    aiPct:st.scanAiPct, aiStage:st.scanAiStageLabel,
                    aiTotal:st.scanAiTotal,
                    queue:(st.scanPhotosQueue||[]).map(q=>q.status)};
        }""")
        print(f"  state: {json.dumps(state, ensure_ascii=False)}")

        check("scanPhase 進入 recognising/done",
              state["phase"] in ("recognising", "done"), state["phase"])
        check("scanUploadPct 去到 100 (全部上傳完)",
              state["upPct"] == 100, f"upPct={state['upPct']}")
        check("scanUploadBytesLabel 有值",
              bool(state["bytes"]), str(state["bytes"]))
        check("scanUploadTotal == 3", state["upTotal"] == 3, str(state["upTotal"]))
        check("AI bar 有 stage label",
              bool(state["aiStage"]), str(state["aiStage"]))

        # 等完成
        try:
            pg.wait_for_function(
                "() => { const r=document.querySelector('[x-data]');"
                " const st=r._x_dataStack[0]; return st.scanPhase==='done'; }",
                timeout=40000,
            )
        except Exception:
            pass

        print("\n── D. Jim 核心要求:全部上傳完先開始識別 ──")
        uploads = [t for k, t in CALLS if k == "upload"]
        recog = [t for k, t in CALLS if k == "recognize"]
        check("有 upload request", len(uploads) >= 1, f"{len(uploads)} 個")
        check("有 recognize request", len(recog) >= 1, f"{len(recog)} 個")
        if uploads and recog:
            check("**最後一次 upload 早過第一次 recognize**",
                  max(uploads) < min(recog),
                  f"gap={min(recog)-max(uploads):.3f}s")
        else:
            check("**最後一次 upload 早過第一次 recognize**", False,
                  "call log 唔完整")

        final = pg.evaluate("""() => {
            const r=document.querySelector('[x-data]');
            const st=r._x_dataStack[0];
            return {phase:st.scanPhase, aiPct:st.scanAiPct,
                    aiStage:st.scanAiStageLabel,
                    queue:(st.scanPhotosQueue||[]).map(q=>({s:q.status, n:(q.committed||[]).length}))};
        }""")
        print(f"  final: {json.dumps(final, ensure_ascii=False)}")
        check("AI bar 去到 100", final["aiPct"] == 100, f"aiPct={final['aiPct']}")
        check("queue 全部 committed",
              all(q["s"] == "committed" for q in final["queue"]) and final["queue"],
              str([q["s"] for q in final["queue"]]))

        print("\n── E. 冇 console error ──")
        real_errors = [e for e in console_errors
                       if "favicon" not in e.lower() and "manifest" not in e.lower()]
        check("零 console error", not real_errors, str(real_errors[:3]))

        ctx.close()
        b.close()

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print()
    print("=" * 70)
    print(f"  {passed}/{total} checks passed")
    if passed != total:
        print("  FAILED:")
        for n, ok, d in CHECKS:
            if not ok:
                print(f"    ✗ {n}  {d}")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
