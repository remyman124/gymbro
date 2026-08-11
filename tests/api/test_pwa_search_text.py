"""Search for 海南雞 in the rendered DOM text."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844},
                        is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    pg.goto("http://localhost:7000/", wait_until="networkidle", timeout=20000)
    pg.wait_for_timeout(3000)

    # Look for '食物紀錄' and scroll to it
    sec = pg.locator("text=/食物紀錄/").first
    sec.scroll_into_view_if_needed()
    pg.wait_for_timeout(500)

    # Now check if 海南雞 appears anywhere in the page
    txt = pg.evaluate("document.body.innerText")
    print(f"body text length: {len(txt)}")
    print(f"contains '海南雞': {'海南雞' in txt}")
    print(f"contains '檸檬茶': {'檸檬茶' in txt}")
    print(f"contains 'Shake Shack': {'Shake Shack' in txt}")

    # Count occurrences of various dates
    for date in ["08/10", "08/09", "08/08", "08/07"]:
        c = txt.count(date)
        print(f"  {date}: {c} occurrences")

    # Get all unique dish-like strings by searching for known markers
    print("\n=== scanning for 海南 substring ===")
    if "海南" in txt:
        idx = txt.find("海南")
        while idx != -1:
            print(f"  found at {idx}: ...{txt[max(0,idx-30):idx+50]}...")
            idx = txt.find("海南", idx + 1)

    b.close()