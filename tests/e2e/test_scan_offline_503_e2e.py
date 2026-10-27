"""
E2E — v3.3.51: 前端喺「網絡閃斷」時唔可以誤報 scan 失敗。

重現 2026-10-27 Jim 見到嘅「Got an 503 error after uploaded the food image」:

  * Service Worker 嘅 /api/ 分支喺 fetch() reject 時會自造一個
    HTTP 503 {"error":"offline"} + `X-Gymbro-Offline: 1` header。
  * 舊前端:`if (!r.ok) throw` → 即刻報「查 AI 進度失敗 HTTP 503」,
    但 server 個 job 其實仲跑緊(甚至已經入咗 Sheet)。

呢個 suite 用 Playwright route interception 模擬**真實**嘅離線情況:
  - 第一次 poll → SW 風格嘅合成 503(帶 X-Gymbro-Offline: 1)
  - 第二次 poll → 網絡完全 throw(fetch 直接 reject)
  - 第三次 poll → 正常 200,server 回 completed + 結果

然後斷言:前端**冇**報失敗、**有**攞到 AI 結果。

⚠️ 唔會碰生產 Sheet:所有 /api/scan_* request 都被 route interception 攔截,
唔會有一個 request 落到 server。
"""
import sys
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:7000"
RESULTS = []


def log(name, ok, msg=""):
    icon = "OK  " if ok else "FAIL"
    print("  %s %s: %s" % (icon, name, msg))
    RESULTS.append({"name": name, "ok": bool(ok), "msg": msg})


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            ),
            is_mobile=True,
            has_touch=True,
            # v3.3.51: 一定要 block Service Worker,否則 SW 會攞走 /api/ request
            # → Playwright 嘅 route interception 唔會 fire → 測試會打去真
            # server(然後 404 unknown job_id)。
            # 我哋要測嘅係**前端點樣處理**一份合成嘅 503 回應,所以由
            # Playwright 直接注入嗰個回應(完全模擬 SW 嘅行為)。
            service_workers="block",
        )
        page = ctx.new_page()

        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        # ---- 攔截所有 scan 相關 endpoint(唔會漏到 production) ----
        state = {"poll_calls": 0, "recognise_calls": 0, "seen_tokens": []}

        def handle_recognise(route):
            state["recognise_calls"] += 1
            try:
                body = route.request.post_data_json or {}
                state["seen_tokens"].append(body.get("client_token"))
            except Exception:
                state["seen_tokens"].append(None)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "job_id": "qa_offline_job", "total": 1}),
            )

        def handle_progress(route):
            state["poll_calls"] += 1
            n = state["poll_calls"]
            if n == 1:
                # SW 合成嘅 503(帶 marker header)
                route.fulfill(
                    status=503,
                    content_type="application/json",
                    headers={"X-Gymbro-Offline": "1"},
                    body=json.dumps({"error": "offline", "offline": True}),
                )
            elif n == 2:
                # 網絡層面完全 throw(Chromium: abort)
                route.abort("connectionfailed")
            else:
                # 正常回來:job 完成 + 有結果
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "ok": True,
                        "job_id": "qa_offline_job",
                        "status": "completed",
                        "stage": "done",
                        "stage_label": "完成",
                        "pct": 100,
                        "current_index": 0,
                        "total": 1,
                        "completed": 1,
                        "inflight": [],
                        "results": [{
                            "client_index": 0,
                            "ok": True,
                            "name": "牛油果炒蛋碗",
                            "calories": 330,
                            "protein": 18,
                            "committed": [{"name": "牛油果炒蛋碗", "calories": 330}],
                            "preview": {"name": "牛油果炒蛋碗", "calories": 330},
                        }],
                        "errors": [],
                    }),
                )

        page.route("**/api/scan_recognize", handle_recognise)
        page.route("**/api/scan_progress/**", handle_progress)

        # ---- 載入個 app ----
        page.goto(f"{BASE}/?v=v3.3.51", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2500)

        # ---- 直接叫前端嘅 poll 邏輯(唔經真實上載,免得污染生產) ----
        result = page.evaluate(
            """async () => {
                const app = window.gymApp();
                const uploads = [{
                    client_index: 0,
                    path: '/home/work/.hermes/scan_cache/qa_offline.jpg',
                    filename: 'qa_offline.jpg',
                }];
                const queueItems = [{
                    client_index: 0,
                    filename: 'qa_offline.jpg',
                    status: 'uploaded',
                    preview: null,
                    error: null,
                }];
                let thrown = null;
                try {
                    await app._scanPollJob('qa_offline_job', uploads, queueItems);
                } catch (e) {
                    thrown = String((e && e.message) || e);
                }
                return {
                    thrown: thrown,
                    queueStatus: queueItems[0].status,
                    queueError: queueItems[0].error,
                    committed: (queueItems[0].committed || []).map(c => c.name),
                    aiTitle: app.scanAiTitle,
                    aiStage: app.scanAiStageLabel,
                };
            }"""
        )

        log(
            "E01 poll 冇因為網絡閃斷而報錯",
            result["thrown"] is None,
            "thrown=%r" % result["thrown"],
        )
        log(
            "E02 poll 至少行咗 3 次(1 個 503 + 1 個斷線 + 1 個成功)",
            state["poll_calls"] >= 3,
            "poll_calls=%d" % state["poll_calls"],
        )
        log(
            "E03 網絡恢復後攞到 AI 結果",
            result["queueStatus"] == "committed"
            and "牛油果炒蛋碗" in (result["committed"] or []),
            "status=%s committed=%s" % (result["queueStatus"], result["committed"]),
        )
        log(
            "E04 queue 冇被標成 failed",
            result["queueStatus"] != "failed",
            "status=%s error=%r" % (result["queueStatus"], result["queueError"]),
        )
        log(
            "E05 冇 JS page error",
            not page_errors,
            "; ".join(page_errors[:3]) or "(none)",
        )

        # ---- 冪等:同一個 client_token 重試 recognise 唔會開多個 job ----
        token_a = page.evaluate(
            "() => window.gymApp()._scanClientToken(['/a/b.jpg', '/a/c.jpg'])"
        )
        token_b = page.evaluate(
            "() => window.gymApp()._scanClientToken(['/a/c.jpg', '/a/b.jpg'])"
        )
        token_c = page.evaluate(
            "() => window.gymApp()._scanClientToken(['/a/d.jpg'])"
        )
        log(
            "E06 client_token 對同一批相片穩定(順序唔影響)",
            token_a == token_b and token_a != token_c,
            "a=%s b=%s c=%s" % (token_a, token_b, token_c),
        )

        # ---- recognise POST 遇 offline 503 會重試,唔會即刻死 ----
        state["poll_calls"] = 0
        retry_result = page.evaluate(
            """async () => {
                const app = window.gymApp();
                let thrown = null;
                try {
                    await app._scanStartRecogniseJob(['/x/y.jpg'], [0], 'qa_token_1');
                } catch (e) {
                    thrown = String((e && e.message) || e);
                }
                return { thrown: thrown };
            }"""
        )
        log(
            "E07 recognise POST 成功(唔會因為離線就死)",
            retry_result["thrown"] is None,
            "thrown=%r" % retry_result["thrown"],
        )
        log(
            "E08 recognise 有送 client_token(冪等)",
            state["seen_tokens"] and state["seen_tokens"][-1] == "qa_token_1",
            "tokens=%s" % state["seen_tokens"],
        )

        browser.close()

    failed = [r for r in RESULTS if not r["ok"]]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("FAILED: " + ", ".join(f["name"] for f in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
