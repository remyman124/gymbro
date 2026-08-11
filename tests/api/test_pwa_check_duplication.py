"""Find all 海南雞 occurrences in the rendered PWA DOM with surrounding context."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844},
                        is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    pg.goto("http://localhost:7000/", wait_until="networkidle", timeout=20000)
    pg.wait_for_timeout(3500)

    # Find every 海南雞 occurrence with its parent context
    occurrences = pg.evaluate("""() => {
        const results = [];
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            if (node.nodeValue.includes('海南雞')) {
                // Get parent card text
                let p = node.parentElement;
                for (let i = 0; i < 6 && p; i++) {
                    if (p.classList && (p.classList.contains('rounded-2xl') || p.classList.contains('mb-2'))) {
                        break;
                    }
                    p = p.parentElement;
                }
                results.push({
                    raw: node.nodeValue.trim(),
                    parent_text: p ? p.innerText.trim().slice(0, 200) : '(no parent)',
                    parent_class: p ? p.className : '(none)',
                });
            }
        }
        return results;
    }""")

    print(f"=== 海南雞 occurrences in DOM: {len(occurrences)} ===")
    for i, occ in enumerate(occurrences):
        print(f"\n[{i}] raw={occ['raw']!r}")
        print(f"    parent_class={occ['parent_class'][:80]}")
        print(f"    parent_text={occ['parent_text'][:200]!r}")

    b.close()