"""E2E — v3.3.64 營養日結:淨係兩粒掣(左 JPG / 右 PDF),唔使預覽,即刻分享。

Jim OOB 2026-10-28:
  「I want two buttons only. Left side is sharing a jpg. Right side is sharing
   an pdf. Both jpg and pdf have no page limit (continuous scrolling)」
  「No viewing. Just immediate share」

驗證:
  1. modal 入面淨係兩粒分享掣(JPG 掣喺 PDF 掣之前 = 左邊)。
  2. 冇預覽圖(冇「睇」呢一步)。
  3. 撳 JPG → navigator.share 收到 image/jpeg 嘅 File(URL 含 all=1)。
  4. 撳 PDF → navigator.share 收到 application/pdf 嘅 File(URL 含 continuous=1)。
  5. 兩個檔案都預先載好(iOS 要求 share 喺 user gesture 內同步叫)。
  6. 分享之後 modal **唔會**自動閂(用戶自己收埋 share sheet)。
  7. 右上角「✕ 關閉」仲喺度,撳得返。
"""
import datetime as dt
import json
import os
import sys
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
        # stub 原生 share(iOS 嘅 share sheet)
        page.add_init_script("""
            window.__shareCalls = [];
            try {
              Object.defineProperty(navigator, 'canShare', {
                configurable: true,
                value: (d) => !!(d && d.files && d.files.length)
              });
              Object.defineProperty(navigator, 'share', {
                configurable: true,
                value: async (data) => {
                  window.__shareCalls.push({
                    files: ((data && data.files) || []).map(f => ({
                      name: f.name, type: f.type, size: f.size
                    })),
                    title: (data && data.title) || ''
                  });
                }
              });
            } catch (e) {}
        """)
        reqs = []
        page.on("request", lambda r: reqs.append(r.url)
                if ("/api/nutrition/card" in r.url or "/api/nutrition/pdf" in r.url)
                else None)

        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        d, meals = pick_date()
        check("搵到有食物紀錄嘅日期", d is not None, f"{d} ({meals} 餐)")
        if d is None:
            browser.close()
            return 1

        page.evaluate("""() => {
            const b = document.getElementById('pwa-install-banner');
            if (b) { b.style.display = 'none'; b.remove(); }
        }""")

        page.evaluate("(d) => Alpine.$data(document.body).showFoodCardPreview(d)", d)
        page.wait_for_timeout(5000)
        st = page.evaluate("() => Alpine.$data(document.body).foodCardPreview")
        check("modal 開住", st.get("visible") is True, json.dumps(st)[:140])

        # ── 1. 兩粒掣 ──
        jpg_btn = page.locator('[data-testid="food-card-jpg-share"]')
        pdf_btn = page.locator('[data-testid="food-card-pdf-share"]')
        check("有 JPG 分享掣", jpg_btn.count() == 1, f"count={jpg_btn.count()}")
        check("有 PDF 分享掣", pdf_btn.count() == 1, f"count={pdf_btn.count()}")
        check("淨係得兩粒分享掣",
              jpg_btn.count() + pdf_btn.count() == 2,
              f"total={jpg_btn.count() + pdf_btn.count()}")
        if jpg_btn.count() and pdf_btn.count():
            jb = jpg_btn.first.bounding_box()
            pb = pdf_btn.first.bounding_box()
            check("JPG 掣喺左邊、PDF 掣喺右邊",
                  bool(jb and pb and jb["x"] < pb["x"]),
                  f"jpg_x={jb['x'] if jb else None} pdf_x={pb['x'] if pb else None}")
            check("兩粒掣同一行(唔會加高)",
                  bool(jb and pb and abs(jb["y"] - pb["y"]) < 8),
                  f"jpg_y={jb['y'] if jb else None} pdf_y={pb['y'] if pb else None}")
            check("兩粒掣喺 viewport 內(短屏都見到)",
                  bool(jb and pb and jb["y"] < 844 and pb["y"] < 844),
                  f"jpg_y={jb['y'] if jb else None}")

        # ── 2. 冇預覽圖 ──
        prev = page.locator("img[alt*='PDF 第 1 頁']")
        check("冇 PDF 預覽圖(唔使「睇」)", prev.count() == 0,
              f"count={prev.count()}")

        # ── 5. 預載 ──
        ready = page.evaluate(
            "() => Alpine.$data(document.body).foodCardPreview.readyKinds")
        check("兩個檔案都預先載好(iOS gesture 內同步 share)",
              "jpg" in (ready or []) and "pdf" in (ready or []),
              json.dumps(ready))

        # ── 3. 撳 JPG ──
        n0 = len(page.evaluate("() => window.__shareCalls") or [])
        try:
            jpg_btn.first.click(timeout=8000)
        except Exception as e:
            check("撳得到 JPG 分享掣", False, str(e))
            browser.close()
            return 1
        page.wait_for_timeout(1500)
        calls = page.evaluate("() => window.__shareCalls") or []
        check("撳 JPG 叫咗 navigator.share", len(calls) == n0 + 1,
              f"n={len(calls) - n0}")
        if len(calls) > n0:
            f0 = (calls[-1].get("files") or [{}])[0]
            check("share 嘅 JPG type 係 image/jpeg",
                  f0.get("type") == "image/jpeg", str(f0.get("type")))
            check("share 嘅 JPG 名係 gymbro_foodlog_<date>.jpg",
                  str(f0.get("name", "")).startswith("gymbro_foodlog_")
                  and str(f0.get("name", "")).endswith(".jpg"),
                  str(f0.get("name")))
            check("share 嘅 JPG 有內容", (f0.get("size") or 0) > 5000,
                  str(f0.get("size")))
        check("JPG 用咗 ?all=1(連續長圖,冇分頁)",
              any("all=1" in u for u in reqs),
              "; ".join(u.split("?")[-1] for u in reqs[-4:]))

        # ── 4. 撳 PDF ──
        n1 = len(page.evaluate("() => window.__shareCalls") or [])
        pdf_btn.first.click(timeout=8000)
        page.wait_for_timeout(1500)
        calls2 = page.evaluate("() => window.__shareCalls") or []
        check("撳 PDF 叫咗 navigator.share", len(calls2) == n1 + 1,
              f"n={len(calls2) - n1}")
        if len(calls2) > n1:
            f0 = (calls2[-1].get("files") or [{}])[0]
            check("share 嘅 PDF type 係 application/pdf",
                  f0.get("type") == "application/pdf", str(f0.get("type")))
            check("share 嘅 PDF 名係 gymbro_foodlog_<date>.pdf",
                  str(f0.get("name", "")).startswith("gymbro_foodlog_")
                  and str(f0.get("name", "")).endswith(".pdf"),
                  str(f0.get("name")))
            check("share 嘅 PDF 有內容", (f0.get("size") or 0) > 5000,
                  str(f0.get("size")))
        check("PDF 用咗 ?continuous=1(單頁長版,冇分頁)",
              any("continuous=1" in u for u in reqs),
              "; ".join(u.split("?")[-1] for u in reqs[-4:]))

        # ── 6. 分享完唔自動閂 ──
        still_open = page.locator(
            '[data-testid="food-card-pdf-share"]').count() > 0 and \
            page.locator('[data-testid="food-card-pdf-share"]').first.is_visible()
        check("分享完 modal 仲開住(用戶自己收埋 share sheet)", still_open)

        # ── 7. 關閉掣 ──
        close_btn = page.locator("[data-testid='food-card-close']").first
        check("右上角「✕ 關閉」仲喺度",
              close_btn.count() > 0 and close_btn.is_visible())
        cb_text = (close_btn.inner_text() or "").strip()
        check("關閉掣有可見文字", "關閉" in cb_text, repr(cb_text))
        if close_btn.count():
            bb = close_btn.bounding_box()
            check("關閉掣喺 viewport 內", bb is not None and bb["y"] < 844,
                  f"y={bb['y'] if bb else None}")
            close_btn.click(timeout=5000)
            page.wait_for_timeout(700)
            check("撳關閉可以閂返 modal",
                  not close_btn.is_visible() or close_btn.count() == 0)

        browser.close()

    npass = sum(1 for _, ok, _ in results if ok)
    print(f"\n=== {npass}/{len(results)} PASS ===")
    for n, ok, d2 in results:
        if not ok:
            print(f"  FAIL: {n}  [{d2}]")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
