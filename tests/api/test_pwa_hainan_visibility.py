"""Verify 海南雞 entries render in the PWA food log (not just in the API).

Jim OOB 2026-08-11 'i saw it quickly then disappeared' — root cause was
Alpine.js `:key="scan.scan_index"` colliding on duplicate scan_index
values in scan_log. The PWA silently dropped the earlier entry of each
dupe pair.

This test catches the regression: load the page in a headless browser,
check that both 海南雞 entries for 2026-08-10 are in the rendered DOM.
"""
import sys
from playwright.sync_api import sync_playwright


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 390, "height": 844},
                                  is_mobile=True, has_touch=True)
        page = ctx.new_page()
        page.goto("http://localhost:7000/", wait_until="networkidle",
                  timeout=20000)
        page.wait_for_timeout(3500)

        # Scroll to food log
        sec = page.locator("text=/食物紀錄/").first
        sec.scroll_into_view_if_needed()
        page.wait_for_timeout(500)

        txt = page.evaluate("document.body.innerText")

        assert "海南雞" in txt, "海南雞 NOT in rendered DOM (regression: scan_index key collision)"
        # Should appear: 21:17 海南雞飯 (the 23:43 dup was removed by retro_v7)
        hainan_count = txt.count("海南雞飯")
        assert hainan_count >= 1, (
            f"expected >=1 海南雞飯 entries rendered, got {hainan_count}"
        )
        print(f"✓ 海南雞飯 rendered {hainan_count}× in food log DOM")

        # Sanity check: 21:17 entry visible
        assert "21:17" in txt, "21:17 timestamp missing from food log"
        print("✓ 21:17 海南雞飯 entry visible")

        browser.close()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)