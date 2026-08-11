"""Dump the entire rendered food log DOM as text."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844},
                        is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    pg.goto("http://localhost:7000/", wait_until="networkidle", timeout=20000)
    pg.wait_for_timeout(3500)

    sec = pg.locator("text=/食物紀錄/").first
    sec.scroll_into_view_if_needed()
    pg.wait_for_timeout(500)

    # Dump entire body innerText
    txt = pg.evaluate("document.body.innerText")
    print("=== ENTIRE BODY INNER TEXT ===")
    print(txt)
    print("=== END ===")

    # Also check the recentScans array via Alpine
    n = pg.evaluate("""(function() {
        const root = document.querySelector('[x-data]');
        if (!root) return 'no x-data root';
        const data = Alpine.$data(root);
        return {
            scans_total: data.recentScans ? data.recentScans.length : null,
            scans_visible: data.recentScansVisible ? data.recentScansVisible.length : null,
            scans_filtered: data.recentScansFiltered,
            grouped_dates: data.recentScansGrouped ? data.recentScansGrouped.map(g => g.date + ' (' + g.count + ')') : null,
            first_3_scans: data.recentScans ? data.recentScans.slice(0, 3).map(s => s.name + ' @ ' + s.timestamp_iso) : null,
        };
    })()""")
    print("\n=== Alpine state ===")
    print(n)

    b.close()