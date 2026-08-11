"""Count entries per date group in the rendered food log DOM."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844},
                        is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    pg.goto("http://localhost:7000/", wait_until="networkidle", timeout=20000)
    pg.wait_for_timeout(3500)

    # Use Alpine state to inspect what's actually being grouped
    info = pg.evaluate("""() => {
        const root = document.querySelector('[x-data]');
        const data = Alpine.$data(root);
        return {
            grouped: data.recentScansGrouped.map(g => ({
                date: g.date,
                date_label: g.date_label,
                count: g.count,
                total_kcal: g.total_kcal,
                items: g.items.map(s => ({
                    name: s.name,
                    kcal: s.calories,
                    ts: (s.timestamp_iso || '').slice(0, 16),
                    scan_index: s.scan_index,
                }))
            })),
        };
    }""")

    print("=== Alpine recentScansGrouped ===")
    for g in info["grouped"]:
        print(f"\n{g['date']}  ({g['date_label']})  count={g['count']}  total_kcal={g['total_kcal']}")
        for s in g["items"]:
            marker = " ←" if "海南雞" in s["name"] else ""
            print(f"  scan#{s['scan_index']}  {s['ts']}  kcal={s['kcal']:>5}  {s['name']!r}{marker}")

    b.close()