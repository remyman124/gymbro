"""E2E — v3.3.54 日結 JPG「逐頁下載」(唯讀,唔碰 Sheet)。

驗證:
  1. footer 冇「✕ 關閉」掣(第二個按鈕已移除)。
  2. 右上角 ✕ 仲喺度(仲閂得到)。
  3. 每頁一個下載掣,數量 == pageTotal。
  4. 撳其中一個掣 → **只發一個** request(唔會一次過載多張)。
  5. 撳第 2 頁嘅掣 → request 係 page=2(唔係 page=1)。
  6. 檔名帶 _p2。
"""
import json
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
        card_calls = []
        page.on("request", lambda r: card_calls.append(r.url)
                if "/api/nutrition/card" in r.url else None)

        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # PWA install banner 會攔截 click → 先收埋
        page.evaluate("""() => {
            const b = document.getElementById('pwa-install-banner');
            if (b) { b.style.display = 'none'; b.remove(); }
        }""")

        # 開 modal
        page.locator("button:visible:has-text('下載日結')").first.click(force=True)
        page.wait_for_timeout(4000)

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

        # 3. 下載掣數量 == pageTotal
        dl_btns = page.locator("button:has-text('⬇')")
        n = dl_btns.count()
        check("E04 下載掣數量 == pageTotal", n == total, f"buttons={n} pageTotal={total}")

        labels = page.evaluate("""() => Array.from(document.querySelectorAll('button'))
            .map(b => (b.textContent||'').trim())
            .filter(t => t.includes('⬇'))""")
        print("   labels:", labels)
        check("E05 每個掣都標明頁碼",
              all(("第" in t and "頁" in t) or ("下載 JPG" in t) for t in labels),
              str(labels))

        # 4/5. 撳第 2 頁嘅掣 → 只有一個 request,而且係 page=2
        if total >= 2:
            before = len(card_calls)
            page.locator("button:has-text('⬇ 第 2 頁')").first.click(force=True)
            page.wait_for_timeout(4000)
            new = card_calls[before:]
            # 排除 modal 開啟時嘅背景 refresh(只計撳掣之後)
            check("E06 撳一下只發一個 request", len(new) == 1, f"{len(new)} 個: {new}")
            if new:
                check("E07 request 係 page=2", "page=2" in new[0], new[0][:110])

            # 下載事件
            dl_name = ""
            try:
                with page.expect_download(timeout=15000) as di:
                    page.locator("button:has-text('⬇ 第 2 頁')").first.click(force=True)
                d = di.value
                dl_name = d.suggested_filename
            except Exception as e:
                dl_name = f"(no download event: {type(e).__name__})"
            print("   suggested_filename:", dl_name)
            check("E08 下載檔名帶 _p2", "_p2" in dl_name, dl_name)
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
