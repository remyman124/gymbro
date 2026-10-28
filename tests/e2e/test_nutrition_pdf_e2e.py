"""E2E — v3.3.62 營養日結 PDF:單一下載掣 + 落完自動閂返主介面。

Jim OOB 2026-10-27:
  「As we use pdf. Just one click download pdf and allow me to close the share
   such that I can be back to gymbro interface after download.」

驗證:
  1. modal 頂部只有**一粒**「⬇ 下載 PDF」掣(唔再有逐頁 JPG 掣)。
  2. 掣喺 preview 上面(短屏都見到)。
  3. 撳一下 → **一個**下載事件,檔名 gymbro_foodlog_<date>.pdf。
  4. 下載即刻開始(< 3 秒)。
  5. 落完 modal **自動閂咗** → 撳「關閉」iOS share sheet 就返返 GymBro。
  6. PDF 第 1 頁預覽圖 load 到。
  7. 右上角「✕ 關閉」仲喺度(撳得返)。
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


def pick_date(min_meals=1):
    """搵最近一個有餐嘅日期(唔好寫死 —— 日子轉咗就 0 餐)。"""
    today = dt.date.today()
    for back in range(0, 10):
        d = (today - dt.timedelta(days=back)).isoformat()
        try:
            with urllib.request.urlopen(
                    f"{BASE}/api/nutrition/pdf_info?date={d}", timeout=30) as rr:
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
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--single-process"])
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            service_workers="block",
            accept_downloads=True,
        )
        page = ctx.new_page()
        page.add_init_script(
            "try { localStorage.setItem('pwa_install_dismissed_at', String(Date.now())); } catch(e) {}"
        )
        downloads = []
        page.on("download", lambda d: downloads.append(d))
        pdf_calls = []
        page.on("request", lambda r: pdf_calls.append(r.url)
                if "/api/nutrition/pdf" in r.url else None)

        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        d, meals = pick_date()
        check("搵到有食物紀錄嘅日期", d is not None, f"{d} ({meals} 餐)")
        if d is None:
            browser.close()
            return 1

        # 收埋 PWA install banner(佢 z-index 9998 會攔 click)
        page.evaluate("""() => {
            const b = document.getElementById('pwa-install-banner');
            if (b) { b.style.display = 'none'; b.remove(); }
        }""")

        # 食物日結掣喺「營養」tab 入面,可能唔係 current tab →
        # 直接經 Alpine 開 modal(同之前嘅 E2E 一致)
        page.evaluate("(d) => Alpine.$data(document.body).showFoodCardPreview(d)", d)
        page.wait_for_timeout(3500)
        st = page.evaluate("() => Alpine.$data(document.body).foodCardPreview")
        check("modal 開住", st.get("visible") is True, json.dumps(st)[:140])
        check("PDF 狀態 ready", st.get("pdfReady") is True, str(st.get("pdfHint"))[:80])

        dl_btn = page.locator('[data-testid="food-card-pdf-download"]')
        check("有單一 PDF 下載掣", dl_btn.count() == 1, f"count={dl_btn.count()}")
        check("掣文字係『⬇ 下載 PDF』",
              "⬇ 下載 PDF" in (dl_btn.first.inner_text() if dl_btn.count() else ""))

        # 冇殘留逐頁 JPG 掣
        jpgs = page.locator("text=⬇ 下載 JPG").count()
        check("冇殘留逐頁 JPG 下載掣", jpgs == 0, f"count={jpgs}")

        # 掣喺 preview 上面
        try:
            btn_box = dl_btn.first.bounding_box()
            prev = page.locator("img[alt*='PDF 第 1 頁']").first
            prev_box = prev.bounding_box() if prev.count() else None
            ok = btn_box is not None and (prev_box is None or btn_box["y"] < prev_box["y"])
            check("PDF 掣喺 preview 上面(短屏見到)", ok,
                  f"btn_y={btn_box['y'] if btn_box else None} prev_y={prev_box['y'] if prev_box else None}")
        except Exception as e:
            check("PDF 掣喺 preview 上面(短屏見到)", False, str(e))

        # 預覽圖 load 到
        try:
            prev = page.locator("img[alt*='PDF 第 1 頁']").first
            if prev.count():
                nat = prev.evaluate("e => e.naturalWidth")
                check("PDF 第 1 頁預覽圖 load 到", nat and nat > 100, f"naturalWidth={nat}")
            else:
                check("PDF 第 1 頁預覽圖 load 到", False, "搵唔到 img")
        except Exception as e:
            check("PDF 第 1 頁預覽圖 load 到", False, str(e))

        # 撳下載掣
        n_before = len(downloads)
        t0 = time.time()
        try:
            dl_btn.first.click(timeout=8000)
        except Exception as e:
            check("撳得到 PDF 下載掣", False, str(e))
            browser.close()
            return 1
        # 等下載事件
        got = None
        for _ in range(40):
            if len(downloads) > n_before:
                got = downloads[-1]
                break
            page.wait_for_timeout(100)
        elapsed = time.time() - t0
        check("撳一下觸發下載", got is not None, f"{elapsed:.2f}s")
        if got is not None:
            fn = got.suggested_filename
            check("檔名係 gymbro_foodlog_<date>.pdf",
                  fn.startswith("gymbro_foodlog_") and fn.endswith(".pdf"), fn)
            check("下載即刻開始(< 3 秒)", elapsed < 3.0, f"{elapsed:.2f}s")
        check("淨係一個下載事件", len(downloads) - n_before == 1,
              f"n={len(downloads) - n_before}")

        # 落完自動閂 modal
        page.wait_for_timeout(1600)
        try:
            modal_hidden = page.locator('[data-testid="food-card-pdf-download"]').count() == 0 \
                or not page.locator('[data-testid="food-card-pdf-download"]').first.is_visible()
            check("落完自動閂咗 modal(返返主介面)", modal_hidden)
        except Exception as e:
            check("落完自動閂咗 modal(返返主介面)", False, str(e))

        # 重新打開 → 右上角 ✕ 關閉仲喺度
        try:
            page.evaluate("(d) => Alpine.$data(document.body).showFoodCardPreview(d)", d)
            page.wait_for_timeout(1500)
            close_btn = page.locator("[data-testid='food-card-close']").first
            cb_text = (close_btn.inner_text() or "").strip()
            check("右上角「✕ 關閉」仲喺度",
                  close_btn.count() > 0 and close_btn.is_visible(), repr(cb_text))
            check("關閉掣有可見文字", "關閉" in cb_text, repr(cb_text))
            if close_btn.count():
                bb = close_btn.bounding_box()
                check("關閉掣喺 viewport 內", bb is not None and bb["y"] < 844,
                      f"y={bb['y'] if bb else None}")
                close_btn.click(timeout=5000)
                page.wait_for_timeout(700)
                check("撳關閉可以閂返 modal",
                      not close_btn.is_visible() or close_btn.count() == 0)
        except Exception as e:
            check("右上角「✕ 關閉」仲喺度", False, str(e))

        browser.close()

    npass = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {npass}/{len(results)} PASS ===")
    for n, ok, d2 in results:
        if not ok:
            print(f"  FAIL: {n}  [{d2}]")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
