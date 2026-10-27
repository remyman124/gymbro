"""E2E — v3.3.53 日結 JPG「矮身版」toggle(唯讀,唔碰 Sheet)。

只做 GET(預覽圖),唔觸發任何 scan / commit → 唔會污染生產表。
目標:確認後端 `?compact=1` 仍然 work(矮身版渲染、獨立 cache、獨立檔名)。

v3.3.59 (Jim OOB「Remove the toggle row ... such that I can see the button
download button」):「檔案細啲」toggle row 已由 modal **移除** —— 佢喺底部
佔 ~42pt,真機短屏會將下載掣推落視窗外。後端仍然支援 compact=1(舊 URL
唔會壞),所以本測試改用 API 直接驗證,唔再靠 UI toggle。
"""
import json
import os
import re
import sys

BASE = os.environ.get("GYMBRO_TEST_BASE", "http://127.0.0.1:7000")
DATE = os.environ.get("GYMBRO_TEST_DATE", "2026-10-27")

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        # 呢部機 RAM 比較緊(實測 499MB free),要加 --disable-dev-shm-usage
        # 同 --disable-gpu,唔加會 "Target crashed"。
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-software-rasterizer", "--single-process"],
        )
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            service_workers="block",   # 唔要 SW 攔截,只要真 network
        )
        page = ctx.new_page()
        # ⚠️ PWA install banner 係 page load 後 3 秒先出現,而且 z-index 9998
        #    高過所有嘢 → 會攔截 modal 底部嘅 pointer event(實測撳落去
        #    完全冇反應)。最乾淨嘅做法係喺 page load **之前**就 dismiss 佢,
        #    唔好靠事後 remove(會撞時間窗口)。
        page.add_init_script(
            "try { localStorage.setItem('pwa_install_dismissed_at', String(Date.now())); } catch(e) {}"
        )

        calls = []

        def on_req(req):
            if "/api/nutrition/card" in req.url:
                calls.append(req.url)

        page.on("request", on_req)
        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        # ⚠️ PWA install banner 會攔截 pointer events(實測:click 60 秒都撳唔到)
        #    → 撳任何嘢之前一定要先收埋佢。
        page.evaluate("""() => {
            const b = document.getElementById('pwa-install-banner');
            if (b) { b.style.display = 'none'; b.remove(); }
        }""")
        page.wait_for_timeout(300)

        # 開日結 modal
        # ⚠️ 一定要用 `:visible` —— 頁面有 3 個「📸下載日結」掣,部分喺
        #    隱藏 tab 入面(x-show → display:none 但**仍然喺 DOM**)。
        #    count() > 0 唔代表睇得到,撳落去會撳中隔籬嘢。
        opened = False
        el = page.locator("button:visible:has-text('下載日結')").first
        if el.count() > 0:
            el.click(force=True)
            opened = True
        page.wait_for_timeout(3500)
        check("E01 modal 打得開", opened, f"opened={opened}")

        # ⚠️ `window.gymApp()` 係 **factory function** —— 叫佢會攞返一個全新嘅
        #    初始 state 嘅物件(唔係 live Alpine data)。
        #    要讀 live state 要用 `Alpine.$data(document.body)`
        #    (x-data="gymApp()" 喺 <body>)。
        live = "() => Alpine.$data(document.body)"
        st = page.evaluate(live + ".foodCardPreview")
        check("E01b modal 狀態係開住", st.get("visible") is True, json.dumps(st)[:120])

        # ── v3.3.59:「檔案細啲」toggle row 已由 modal 移除 ─────────────
        # Jim OOB「Remove the toggle row ... such that I can see the button
        # download button」—— 佢喺底部佔 ~42pt,真機短屏會將下載掣推落視窗外。
        # 後端 `?compact=1` 仍然支援(舊 URL 唔會壞),所以以下改用 API 直接驗證。
        has_toggle = page.locator("button:visible:has-text('檔案細啲')").count() > 0
        check("E02 「檔案細啲」toggle row 已移除", not has_toggle, f"has_toggle={has_toggle}")

        # 直接打 API 攞 compact 版,量真實像素高度
        dims = page.evaluate("""async () => {
            const r = await fetch('/api/nutrition/card?date=__D__&compact=1', {cache:'no-store'});
            const b = await r.blob();
            const bmp = await createImageBitmap(b);
            const r2 = await fetch('/api/nutrition/card?date=__D__', {cache:'no-store'});
            const b2 = await r2.blob();
            const bmp2 = await createImageBitmap(b2);
            return {cw: bmp.width, ch: bmp.height, cs: b.size,
                    pw: bmp2.width, ph: bmp2.height, ps: b2.size};
        }""".replace("__D__", DATE))
        print("   dims:", json.dumps(dims, ensure_ascii=False)[:300])
        check("E03 compact 版高度 ≤ 1280", dims.get("ch", 9999) <= 1280,
              f"{dims.get('cw')}x{dims.get('ch')}")
        check("E04a compact 版細過原版", dims.get("ch", 9999) < dims.get("ph", 0),
              f"{dims.get('ch')} < {dims.get('ph')}")
        check("E04b compact 版檔案細過原版", dims.get("cs", 999999) < dims.get("ps", 0),
              f"{dims.get('cs')} < {dims.get('ps')}")

        # 原始尺寸版仍然要用得 (未 toggle 嘅話)
        plain = page.evaluate("""async () => {
            const r = await fetch('/api/nutrition/card?date=__D__', {cache:'no-store'});
            const b = await r.blob();
            const bmp = await createImageBitmap(b);
            return {w: bmp.width, h: bmp.height, size: b.size};
        }""".replace("__D__", DATE))
        check("E05 原尺寸版仍然正常", plain.get("h", 0) > 1280,
              f"{plain.get('w')}x{plain.get('h')} {plain.get('size')}B")

        # 下載檔名要帶 _compact
        cdl = page.evaluate("""async () => {
            const r = await fetch('/api/nutrition/card?date=__D__&compact=1', {cache:'no-store'});
            return r.headers.get('content-disposition') || '';
        }""".replace("__D__", DATE))
        check("E06 Content-Disposition 帶 _compact", "_compact" in cdl, cdl[:80])

        # compact 唔可以覆蓋原版 cache
        import urllib.request
        def head(u):
            req = urllib.request.Request(BASE + u, method="GET")
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        full_bytes = head(f"/api/nutrition/card?date={DATE}")
        comp_bytes = head(f"/api/nutrition/card?date={DATE}&compact=1")
        check("E07 兩個版本唔同檔", full_bytes != comp_bytes,
              f"{len(full_bytes)}B vs {len(comp_bytes)}B")
        check("E08 矮身版細過原版", len(comp_bytes) < len(full_bytes),
              f"{len(comp_bytes)} < {len(full_bytes)}")

        browser.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} passed ===")
    for n, ok, d in results:
        if not ok:
            print(f"  FAIL {n} {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
