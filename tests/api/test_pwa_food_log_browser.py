"""Headless browser test: load the gymbro PWA, find 海南雞 in the rendered DOM.

Jim OOB 2026-08-11 'i saw it quickly but then disappeared' — verify the
entry is actually present in the rendered food log, not just the API.
"""
import sys
from playwright.sync_api import sync_playwright

BASE_URL = "http://localhost:7000/"


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 390, "height": 844},
                                  is_mobile=True, has_touch=True)
        page = ctx.new_page()
        page.goto(BASE_URL, wait_until="networkidle", timeout=20000)

        # Wait for food log to render (recentScansGrouped populates after fetch)
        page.wait_for_timeout(3000)

        # Find all DOM nodes containing 海南雞
        matches = page.locator("text=/海南雞/").all()
        print(f"DOM matches for '海南雞': {len(matches)}")
        for i, m in enumerate(matches):
            try:
                txt = m.inner_text().strip()[:60]
                vis = m.is_visible()
                print(f"  [{i}] visible={vis}  text={txt!r}")
            except Exception as e:
                print(f"  [{i}] error: {e}")

        # Also dump the food log area
        print()
        print("=== food log area snapshot ===")
        try:
            section = page.locator("text=/食物紀錄/").first
            if section.is_visible():
                # Get the parent container text
                parent = section.locator("xpath=ancestor::div[3]")
                print(parent.inner_text()[:2000])
        except Exception as e:
            print(f"snapshot error: {e}")

        page.screenshot(path="/tmp/pwa_food_log.png", full_page=True)
        print("\nscreenshot saved → /tmp/pwa_food_log.png")
        browser.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)