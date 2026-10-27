"""E2E — v3.3.60 日結 JPG「逐頁下載」:直接 <a href> 即刻落檔(唯讀,唔碰 Sheet)。

驗證:
  1. footer 冇「✕ 關閉」掣(第二個按鈕已移除)。
  2. 右上角 ✕ 仲喺度(仲閂得到)。
  3. 每頁一個下載連結,數量 == pageTotal,而且係 **<a href>**(唔係 <button>)。
  4. 撳其中一個連結 → **只觸發一個下載事件**,而且係 page=2(唔係 page=1)。
  5. 檔名帶 _p2。
  6. 🔴 **下載係即刻開始**(click → download event < 2.5 秒),
     唔係等 fetch → blob → objectURL → click 嗰條慢路徑。
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("GYMBRO_TEST_BASE", "http://127.0.0.1:7000")

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def pick_date(min_meals=6):
    """搵最近一個餐數夠多嘅日期(唔好寫死 —— 日子轉咗就 0 餐)。"""
    today = dt.date.today()
    for back in range(0, 8):
        d = (today - dt.timedelta(days=back)).isoformat()
        try:
            with urllib.request.urlopen(
                    f"{BASE}/api/nutrition/card_url?date={d}&per_page=5", timeout=30) as rr:
                data = json.loads(rr.read().decode())
        except Exception:
            continue
        if (data.get("meal_count") or 0) >= min_meals:
            return d, data.get("meal_count") or 0
    return None, 0


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
        #    load **之前**就 dismiss 佢(唔好靠事後 remove,會撞時間窗口)。
        page.add_init_script(
            "try { localStorage.setItem('pwa_install_dismissed_at', String(Date.now())); } catch(e) {}"
        )
        card_calls = []
        page.on("request", lambda r: card_calls.append(r.url)
                if "/api/nutrition/card" in r.url else None)
        downloads = []
        page.on("download", lambda d: downloads.append(d))

        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # PWA install banner 會攔截 click → 先收埋
        page.evaluate("""() => {
            const b = document.getElementById('pwa-install-banner');
            if (b) { b.style.display = 'none'; b.remove(); }
        }""")

        # ⚠️ 唔好靠「撳今日嘅下載日結」—— 今日可能一餐都未食。
        target_date, meals = pick_date(6)
        check("E00 搵到有 ≥6 餐嘅日期", target_date is not None, f"{target_date} ({meals} 餐)")
        if target_date is None:
            browser.close()
            print("=== 0/1 passed ===")
            return 1
        print("   用緊日期:", target_date)

        page.evaluate("(d) => Alpine.$data(document.body).showFoodCardPreview(d)", target_date)
        page.wait_for_timeout(5000)

        live = "() => Alpine.$data(document.body)"
        st = page.evaluate(live + ".foodCardPreview")
        total = st.get("pageTotal") or 1
        print("   pageTotal =", total, "| pageNum =", st.get("pageNum"))
        check("E01 modal 開住", st.get("visible") is True, json.dumps(st)[:100])

        # 1. footer 冇關閉掣
        footer_close = page.locator("button:has-text('✕ 關閉')").count()
        check("E02 footer 冇「✕ 關閉」掣", footer_close == 0, f"count={footer_close}")

        # 2. 右上角 ✕ 仲喺度
        hdr = page.locator("[data-testid='food-card-close']").count()
        check("E03 右上角 ✕ 仲喺度", hdr == 1, f"count={hdr}")

        # 3. 下載連結數量 == pageTotal(而且係 <a>)
        links = page.locator("a:has-text('⬇')")
        n = links.count()
        check("E04 下載連結數量 == pageTotal", n == total, f"links={n} pageTotal={total}")
        n_btn = page.locator("button:has-text('⬇')").count()
        check("E04b 下載掣係 <a href>(即刻落檔,唔係 JS blob)", n_btn == 0, f"button={n_btn}")

        hrefs = page.evaluate(r"""() => Array.from(document.querySelectorAll('a'))
            .filter(a => (a.textContent||'').includes('⬇'))
            .map(a => ({href: a.getAttribute('href'), dl: a.getAttribute('download'),
                        target: a.getAttribute('target'),
                        text: (a.textContent||'').trim().replace(/\s+/g,' ')}))""")
        print("   hrefs:", hrefs)
        check("E05 每個連結都標明頁碼 + 指向 /api/nutrition/card",
              all(("第" in h["text"] and "頁" in h["text"]) or ("下載 JPG" in h["text"])
                  for h in hrefs)
              and all("/api/nutrition/card" in (h["href"] or "") for h in hrefs),
              str([h["text"] for h in hrefs])[:220])
        check("E05c 每個連結都有 download 屬性(檔名一致)",
              all((h["dl"] or "").startswith("gymbro_foodlog_") for h in hrefs),
              str([h["dl"] for h in hrefs])[:220])
        # 🔴 v3.3.61:唔可以有 target=_blank(iOS 開新分頁蓋住 App → 落完檔返唔到)
        check("E05d 下載連結冇 target=_blank(唔會開新分頁)",
              all(not h["target"] for h in hrefs),
              str([h["target"] for h in hrefs])[:120])

        # v3.3.61:關閉掣要有可見文字,而且喺 viewport 內
        close_btn = page.locator("[data-testid='food-card-close']")
        cb_text = (close_btn.inner_text() or "").strip() if close_btn.count() else ""
        cb_rect = close_btn.bounding_box() if close_btn.count() else None
        check("E05e 關閉掣有可見文字「關閉」", "關閉" in cb_text, repr(cb_text))
        check("E05f 關閉掣喺 viewport 內(短屏都撳得到)",
              cb_rect is not None and cb_rect["y"] >= 0
              and cb_rect["y"] + cb_rect["height"] <= 844 + 1,
              str(cb_rect))
        check("E05b 每個連結帶對應 page 參數",
              all(f"page={i + 1}" in (h["href"] or "") for i, h in enumerate(hrefs)),
              str([h["href"] for h in hrefs])[:220])

        # 4/5/6. 撳第 2 頁嘅連結 → 只觸發一個下載,而且即刻
        if total >= 2:
            before_calls = len(card_calls)
            before_dl = len(downloads)
            t0 = time.time()
            try:
                with page.expect_download(timeout=20000) as di:
                    page.locator("a:visible:has-text('⬇ 第 2 頁')").first.click(force=True)
                d = di.value
                elapsed = time.time() - t0
                dl_name = d.suggested_filename
                dl_url = d.url
            except Exception as e:
                elapsed = time.time() - t0
                dl_name = f"(no download event: {type(e).__name__})"
                dl_url = ""
            page.wait_for_timeout(1500)
            new_dl = downloads[before_dl:]
            print(f"   suggested_filename: {dl_name}  ({elapsed:.2f}s)")
            check("E06 撳一下只觸發一個下載", len(new_dl) == 1, f"{len(new_dl)} 個")
            check("E07 下載 URL 係 page=2", "page=2" in (dl_url or ""), (dl_url or "")[:120])
            check("E08 下載檔名帶 _p2", "_p2" in dl_name, dl_name)
            # 🔴 即刻性:click → download event 應該好快(唔使等 fetch+blob)
            check("E09 下載即刻開始(< 2.5s,唔係等 blob 路徑)", elapsed < 2.5, f"{elapsed:.2f}s")
        else:
            print("   (單頁日,跳過 page=2 測試)")

        browser.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {passed}/{len(results)} passed ===")
    for nm, ok, d in results:
        if not ok:
            print(f"  FAIL {nm} {d}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
