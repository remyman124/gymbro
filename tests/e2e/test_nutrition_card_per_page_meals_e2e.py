"""E2E — v3.3.55 日結 JPG「每頁餐數」(6/4/3)選擇器(唯讀,唔碰 Sheet)。

驗證:
  1. 「每頁餐數」選擇器存在,有 6/4/3 三個 chip。
  2. 撳「4 餐」/「3 餐」→ request 帶 per_page=。
  3. 高度真係按比例縮短(1080 闊唔變,高度變)。
  4. page_total 跟住 per_page 變。
  5. 下載檔名帶 _pp3。
"""
import os
import sys

BASE = os.environ.get("GYMBRO_TEST_BASE", "http://127.0.0.1:7000")

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"])
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            service_workers="block",
            accept_downloads=True,
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
        page.on("request", lambda r: calls.append(r.url)
                if "/api/nutrition/card" in r.url else None)

        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        page.evaluate("""() => {
            const b = document.getElementById('pwa-install-banner');
            if (b) { b.style.display = 'none'; b.remove(); }
        }""")
        page.locator("button:visible:has-text('下載日結')").first.click(force=True)
        page.wait_for_timeout(4000)

        live = "() => Alpine.$data(document.body)"

        labels = page.evaluate("""() => Array.from(document.querySelectorAll('button'))
            .map(b => (b.innerText||'').trim()).filter(t => /餐$/.test(t))""")
        check("E01 有 5/4/3 三個「每頁餐數」chip", labels == ["5 餐", "4 餐", "3 餐"], str(labels))

        # v3.3.57: 每頁**上限 5 餐** —— 唔准有 6 或以上嘅選項
        nums = [int(t.replace(" 餐", "").strip()) for t in labels]
        check("E01b 每頁上限 5 餐(冇 6 或以上)",
              bool(nums) and max(nums) == 5, str(nums))

        heights = {}
        for pp, label in ((4, "4 餐"), (3, "3 餐"), (5, "5 餐")):
            before = len(calls)
            page.locator(f"button:has-text('{label}')").first.click(force=True)
            page.wait_for_timeout(4000)
            new = calls[before:]
            ok_q = any(f"per_page={pp}" in c for c in new)
            check(f"E02 per_page={pp} 有送落 server", ok_q, str(new[:2]))

            dims = page.evaluate("""() => {
                const a = Alpine.$data(document.body);
                const img = Array.from(document.querySelectorAll('img'))
                    .find(i => (i.src||'').includes('/nutrition_card/'));
                return {url: a.foodCardPreview.url, pageTotal: a.foodCardPreview.pageTotal,
                        natW: img ? img.naturalWidth : 0, natH: img ? img.naturalHeight : 0};
            }""")
            heights[pp] = dims
            print(f"   per_page={pp}: {dims['natW']}x{dims['natH']}  pages={dims['pageTotal']}")
            check(f"E03 per_page={pp} 圖量到高度", dims["natH"] > 0,
                  f"{dims['natW']}x{dims['natH']}")

        check("E04 闊度全部係 1080", all(h["natW"] == 1080 for h in heights.values()),
              str({k: v["natW"] for k, v in heights.items()}))
        check("E05 高度 4餐 < 5餐", heights[4]["natH"] < heights[5]["natH"],
              f"{heights[4]['natH']} < {heights[5]['natH']}")
        check("E06 高度 3餐 < 4餐", heights[3]["natH"] < heights[4]["natH"],
              f"{heights[3]['natH']} < {heights[4]['natH']}")
        check("E07 page_total 跟 per_page 變",
              heights[5]["pageTotal"] == 2 and heights[3]["pageTotal"] == 3,
              f"5餐→{heights[5]['pageTotal']}頁, 3餐→{heights[3]['pageTotal']}頁")

        # v3.3.57: 全部頁面嘅下載掣都要顯示喺 modal 底部,唔會藏喺第 2 頁
        dls = page.evaluate("""() => {
            const modal = document.querySelector('[x-show="foodCardPreview.visible"] > div');
            const vis = el => { const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && el.offsetParent !== null; };
            const btns = Array.from(modal.querySelectorAll('button')).filter(vis)
                .filter(b => (b.innerText||'').includes('第'))
                .map(b => { const r = b.getBoundingClientRect();
                    return {t:(b.innerText||'').trim().replace(/\\s+/g,' '),
                            y:r.y, bottom:r.y+r.height}; });
            const mr = modal.getBoundingClientRect();
            return {btns, modalBottom: mr.y + mr.height,
                    pageTotal: Alpine.$data(document.body).foodCardPreview.pageTotal,
                    hint: (document.querySelector('[x-show="foodCardPreview.visible"]')||{}).innerText || ''};
        }""")
        check("E09 下載掣數量 == pageTotal(全部頁面都見到)",
              len(dls["btns"]) == dls["pageTotal"],
              f"掣={len(dls['btns'])} pageTotal={dls['pageTotal']}")
        check("E10 全部下載掣都喺 modal 範圍內(冇被裁)",
              all(b["bottom"] <= dls["modalBottom"] + 1 for b in dls["btns"]),
              str([(b["t"], round(b["bottom"]), round(dls["modalBottom"])) for b in dls["btns"]]))
        check("E11 有講明逐頁下載", "逐頁下載" in dls["hint"] and "撳一個落一張" in dls["hint"],
              dls["hint"][-90:].replace("\n", " "))

        page.locator("button:has-text('3 餐')").first.click(force=True)
        page.wait_for_timeout(4000)
        dl_name = ""
        try:
            with page.expect_download(timeout=15000) as di:
                page.locator("button:has-text('⬇ 第 1 頁')").first.click(force=True)
            dl_name = di.value.suggested_filename
        except Exception as e:
            dl_name = f"(no download: {type(e).__name__})"
        print("   suggested_filename:", dl_name)
        check("E08 下載檔名帶 _pp3", "_pp3" in dl_name, dl_name)

        browser.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} passed ===")
    for nm, ok, d in results:
        if not ok:
            print(f"  FAIL {nm} {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
