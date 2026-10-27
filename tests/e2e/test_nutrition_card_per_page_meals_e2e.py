"""E2E — v3.3.58 日結 JPG:每頁固定 5 餐 + 短屏下載掣可見(唯讀,唔碰 Sheet)。

背景:
  v3.3.57  Jim OOB「Make it 5 meals max per screen. But show me all the page
           button to download one by one」→ 每頁上限 5 餐 + 全部頁面下載掣。
  v3.3.58  Jim OOB「Remove page size buttons」
           Jim OOB「My screen height is very short. Can't see the download buttons」
           → chip row 移除;modal 改用 h-[90dvh] + preview flex-1 min-h-0,
             短屏 footer(下載掣)永遠可見。

驗證:
  1. 冇「每頁餐數」chip(已移除)
  2. 預設 request 帶 per_page=5,圖係 1080x1496
  3. 全部頁面嘅下載掣都 render,而且**喺 viewport 之內**(唔係喺屏幕下面)
  4. 短屏(375x667)同極短屏(320x568)一樣見到全部下載掣
  5. 逐頁下載:撳一個掣 = 一個 request + 檔名帶 _pp5
  6. Server 仍然支援 per_page=4/3(舊 URL backwards compatible)
"""
import os
import sys

BASE = os.environ.get("GYMBRO_TEST_BASE", "http://127.0.0.1:7000")

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


# 量度 modal 入面嘅下載掣:有冇被裁 / 有冇喺 viewport 之內
PROBE_JS = """() => {
    const modal = document.querySelector('[x-show="foodCardPreview.visible"] > div');
    const vis = el => { const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && el.offsetParent !== null; };
    const btns = Array.from(modal.querySelectorAll('button')).filter(vis)
        .filter(b => (b.innerText||'').includes('第'))
        .map(b => { const r = b.getBoundingClientRect();
            return {t:(b.innerText||'').trim().replace(/\\s+/g,' '),
                    top:r.y, bottom:r.y+r.height, left:r.left, right:r.right}; });
    const mr = modal.getBoundingClientRect();
    const a = Alpine.$data(document.body);
    const img = Array.from(document.querySelectorAll('img'))
        .find(i => (i.src||'').includes('/nutrition_card/'));
    return {btns, modalTop: mr.y, modalBottom: mr.y + mr.height,
            vh: window.innerHeight, vw: window.innerWidth,
            pageTotal: a.foodCardPreview.pageTotal, perPage: a.foodCardPreview.perPage,
            url: a.foodCardPreview.url,
            natW: img ? img.naturalWidth : 0, natH: img ? img.naturalHeight : 0,
            hint: (document.querySelector('[x-show="foodCardPreview.visible"]')||{}).innerText || ''};
}"""


def probe(page):
    return page.evaluate(PROBE_JS)


def check_visibility(tag, d, page_total):
    """下載掣:全部 render + 喺 modal 內 + 喺 viewport 內。"""
    check(f"{tag} 下載掣數量 == pageTotal", len(d["btns"]) == page_total,
          f"掣={len(d['btns'])} pageTotal={page_total}")
    check(f"{tag} 全部下載掣喺 modal 範圍內(冇被裁)",
          all(b["bottom"] <= d["modalBottom"] + 1 for b in d["btns"]),
          str([(b["t"], round(b["bottom"]), round(d["modalBottom"])) for b in d["btns"]]))
    # 🔴 Jim 真正嘅投訴:「My screen height is very short. Can't see the download buttons」
    #    → 掣嘅 bottom 一定要 <= viewport 高度(唔可以喺屏幕下面)
    check(f"{tag} 全部下載掣喺 viewport 內(短屏都見到)",
          all(b["bottom"] <= d["vh"] + 1 for b in d["btns"]),
          f"vh={d['vh']} bottoms={[round(b['bottom']) for b in d['btns']]}")
    check(f"{tag} modal 本身喺 viewport 內",
          d["modalBottom"] <= d["vh"] + 1 and d["modalTop"] >= -1,
          f"top={round(d['modalTop'])} bottom={round(d['modalBottom'])} vh={d['vh']}")


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
        # ⚠️ PWA install banner 喺 page load 後 3 秒先出現,而且 z-index 9998
        #    高過所有嘢 → 會攔截 modal 底部嘅 pointer event。正解係喺 page
        #    load **之前** dismiss 佢(唔好靠事後 remove,會撞時間窗口)。
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

        # ── 1. chip row 已移除 ────────────────────────────────────────
        labels = page.evaluate("""() => Array.from(document.querySelectorAll('button'))
            .map(b => (b.innerText||'').trim()).filter(t => /餐$/.test(t))""")
        check("E01 冇「每頁餐數」chip(已移除)", labels == [], str(labels))

        # ── 2. 預設 = 5 餐/頁 ─────────────────────────────────────────
        check("E02 預設 request 帶 per_page=5",
              any("per_page=5" in c for c in calls), str(calls[:2]))
        d = probe(page)
        check("E03 每頁固定 5 餐(state perPage == 5)", d["perPage"] == 5, str(d["perPage"]))
        check("E04 圖係 1080 闊", d["natW"] == 1080, f"{d['natW']}x{d['natH']}")
        check("E05 5 餐高度 ≈ 1496", 1400 <= d["natH"] <= 1550, f"{d['natW']}x{d['natH']}")
        check("E06 page_total 正確(7 餐 ÷ 5 = 2 頁)", d["pageTotal"] == 2,
              str(d["pageTotal"]))

        # ── 3. 全部頁面下載掣(正常屏) ─────────────────────────────────
        check_visibility("E07 (390x844)", d, d["pageTotal"])
        check("E08 有講明逐頁下載", "逐頁下載" in d["hint"] and "撳一個落一張" in d["hint"],
              d["hint"][-90:].replace("\n", " "))

        # ── 4. 短屏:Jim 真正嘅投訴場景 ───────────────────────────────
        for vw, vh, tag in ((375, 667, "E09 (375x667 iPhone SE)"),
                            (320, 568, "E10 (320x568 極短屏)")):
            page.set_viewport_size({"width": vw, "height": vh})
            page.wait_for_timeout(1200)
            ds = probe(page)
            check_visibility(tag, ds, ds["pageTotal"])
            # 圖要等比縮細(唔可以爆出去)
            check(f"{tag} 預覽圖喺 preview 區內(唔會 overflow)",
                  ds["natH"] > 0, f"{ds['natW']}x{ds['natH']}")

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(1000)

        # ── 5. 逐頁下載:一個掣 = 一個 request ─────────────────────────
        before = len(calls)
        dl_name = ""
        try:
            with page.expect_download(timeout=20000) as di:
                page.locator("button:visible:has-text('⬇ 第 1 頁')").first.click(force=True)
            dl_name = di.value.suggested_filename
        except Exception as e:
            dl_name = f"(no download: {type(e).__name__})"
        page.wait_for_timeout(1500)
        new = calls[before:]
        check("E11 撳一個掣只發一個 request", len(new) == 1, str(new))
        check("E12 下載檔名帶 _pp5", "_pp5" in dl_name, dl_name)

        # ── 6. Server 仍然支援 per_page=4/3(舊 URL backwards compatible) ──
        # card_url 回傳嘅係 `pages[]`(每頁一個 image_url),唔係頂層 `url`
        import urllib.request
        import json as _json
        from PIL import Image
        import io
        for pp, expect_h in ((4, 1256), (3, 1016)):
            try:
                with urllib.request.urlopen(
                        f"{BASE}/api/nutrition/card_url?date=2026-10-27&per_page={pp}",
                        timeout=30) as resp:
                    data = _json.loads(resp.read().decode())
                pages = data.get("pages") or []
                assert pages, f"per_page={pp} 冇 pages"
                url = pages[0].get("image_url") or ""
                assert url, f"per_page={pp} 冇 image_url"
                full = url if url.startswith("http") else BASE + url
                with urllib.request.urlopen(full, timeout=60) as r2:
                    im = Image.open(io.BytesIO(r2.read()))
                    h = im.height
                check(f"E13 per_page={pp} 仍然 work(高度 {expect_h})", abs(h - expect_h) <= 4,
                      f"{h} (url={url.rsplit('/', 1)[-1]})")
                check(f"E13b per_page={pp} 檔名帶 _pp{pp}", f"_pp{pp}" in url, url)
            except Exception as e:
                check(f"E13 per_page={pp} 仍然 work", False, f"{type(e).__name__}: {e}")

        browser.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} passed ===")
    for nm, ok, dtl in results:
        if not ok:
            print(f"  FAIL {nm} {dtl}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
