"""Scroll through the entire PWA food log and report every entry name found.

Jim OOB 2026-08-11 'i saw it briefly but then disappeared' — verify the
entry is actually present in the rendered food log by scrolling the
whole page and collecting every name we see.
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
        page.wait_for_timeout(2500)

        # Find the food log section by its header
        food_log = page.locator("text=/食物紀錄/").first
        food_log.wait_for(state="visible", timeout=10000)
        print("food log section found")

        # Scroll to it
        food_log.scroll_into_view_if_needed()
        page.wait_for_timeout(500)

        # Now scroll the main scroll container progressively and collect
        # every food card name we encounter.
        names_seen = set()
        names_with_count = {}
        last_y = -1
        for step in range(0, 5000, 200):
            page.evaluate(f"window.scrollTo(0, {step})")
            page.wait_for_timeout(80)
            # Grab all elements that look like dish names
            for locator in page.locator("div.text-base, div.text-sm, div.text-xs").all():
                try:
                    txt = locator.inner_text().strip()
                    # heuristic: short text, no newlines
                    if 2 <= len(txt) <= 40 and "\n" not in txt:
                        # dedup incremental
                        names_seen.add(txt)
                        names_with_count[txt] = names_with_count.get(txt, 0) + 1
                except Exception:
                    pass
            if step == last_y:
                break
            last_y = step

        # Filter to plausible dish names (no header strings, no UI chrome)
        keywords = ["飯", "雞", "茶", "水", "包", "豆", "麵", "堡", "沙律",
                    "豆腐", "米", "蝦", "魚", "牛", "豬", "蛋", "芝士", "多士",
                    "薯條", "Coke", "Burger", "Shake Shack", "Nestea", "蘇打",
                    "四季", "炒蛋", "奶油", "海南", "檸檬"]
        dish_names = sorted({n for n in names_seen
                            if any(k in n for k in keywords)
                            and not n.startswith(("/", "<", "{"))})

        print(f"\n=== dish names found ({len(dish_names)}) ===")
        for n in dish_names:
            count = names_with_count.get(n, 0)
            marker = " ← HAINAN JI" if "海南雞" in n else ""
            print(f"  [{count:3}x] {n}{marker}")

        print()
        hainan = [n for n in dish_names if "海南雞" in n]
        if hainan:
            print(f"✓ 海南雞 visible: {hainan}")
        else:
            print("✗ 海南雞 NOT found in rendered DOM")
            # Dump the page text to see what's actually there
            body_text = page.evaluate("document.body.innerText")
            print("\n=== body text (first 3000 chars) ===")
            print(body_text[:3000])

        page.screenshot(path="/tmp/pwa_food_log_scrolled.png", full_page=True)
        browser.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)