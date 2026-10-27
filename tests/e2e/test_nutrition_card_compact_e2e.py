"""E2E — v3.3.53 日結 JPG「矮身版」toggle(唯讀,唔碰 Sheet)。

只做 GET(預覽圖),唔觸發任何 scan / commit → 唔會污染生產表。
目標:確認前端真係有 toggle、撳完會用 compact=1 重新載入、下載檔名帶 _compact。
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
        opened = False
        for sel in [
            "button:has-text('下載日結')",
            "text=下載日結",
            "button:has-text('下載')",
        ]:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    el.click()
                    opened = True
                    break
            except Exception:
                continue
        page.wait_for_timeout(3000)
        check("E01 modal 打得開", opened and page.locator("text=矮身版").count() > 0,
              f"opened={opened}")

        # ⚠️ `window.gymApp()` 係 **factory function** —— 叫佢會攞返一個全新嘅
        #    初始 state 嘅物件(唔係 live Alpine data)。
        #    要讀 live state 要用 `Alpine.$data(document.body)`
        #    (x-data="gymApp()" 喺 <body>)。
        live = "() => Alpine.$data(document.body)"
        st = page.evaluate(live + ".foodCardPreview")
        check("E01b modal 狀態係開住", st.get("visible") is True, json.dumps(st)[:120])

        # 搵 toggle
        toggle = page.locator("text=矮身版").first
        has_toggle = toggle.count() > 0
        check("E02 有「矮身版」toggle", has_toggle)

        if not has_toggle:
            browser.close()
            return 1

        # 撳 toggle → 應該重新 fetch(帶 compact=1)
        before = len(calls)
        toggle.click(force=True)
        page.wait_for_timeout(4000)
        after_calls = calls[before:]
        compact_calls = [c for c in after_calls if "compact=1" in c]
        check("E03 撳完 toggle 有帶 compact=1 嘅請求", len(compact_calls) > 0,
              f"{len(compact_calls)}/{len(after_calls)}")

        # 圖片實際高度(由 live state 嘅 url 抽,再量真實 img)
        dims = page.evaluate("""() => {
            const a = Alpine.$data(document.body);
            const url = a.foodCardPreview.url || '';
            const imgs = Array.from(document.querySelectorAll('img'))
                .filter(i => (i.src||'').includes('compact'));
            return {stateUrl: url, compact: a.foodCardPreview.compact,
                    imgs: imgs.map(i => ({src: i.src, w: i.naturalWidth, h: i.naturalHeight}))};
        }""")
        print("   dims:", json.dumps(dims, ensure_ascii=False)[:300])
        check("E04a live state compact = true", dims.get("compact") is True, str(dims.get("compact")))
        check("E04b 預覽 URL 指向 _compact 檔", "_compact" in (dims.get("stateUrl") or ""),
              (dims.get("stateUrl") or "")[:80])
        imgs = dims.get("imgs") or []
        if imgs:
            h = imgs[0]["h"]
            check("E04c 預覽圖高度 ≤ 1280", h <= 1280, f"h={h}")
        else:
            check("E04c 預覽圖搵唔到", False, str(imgs)[:120])

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
