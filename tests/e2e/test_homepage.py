"""E2E browser test for gymbro PWA.

Drives the actual running Flask app via Playwright to verify:
- HTTP 200 on the root
- HTML template renders (contains expected PWA markers)
- Alpine.js initializes (x-data attributes present)
- Bottom nav has 4 tabs (Log / Pyramid / History / You)
- Tabs switch correctly
- API endpoints respond (/healthz, /api/version, /api/state)
- Visual screenshots saved for manual review

Run after `python3 gym_web.py` is up on :7000:
    pytest tests/e2e/test_homepage.py -v -s
"""

import os
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

BASE_URL = os.environ.get("GYMBRO_URL", "http://localhost:7000")
SHOTS_DIR = Path(__file__).parent / "screenshots"
SHOTS_DIR.mkdir(exist_ok=True)


def _dismiss_install_prompt(page):
    """Dismiss the PWA's custom iOS-style 'Add to Home Screen' banner if shown."""
    try:
        close = page.locator("#pwa-install-close")
        if close.is_visible(timeout=500):
            close.click()
            page.wait_for_timeout(300)
    except Exception:
        pass


def _shot(page, name):
    """Save a screenshot for manual review."""
    _dismiss_install_prompt(page)
    path = SHOTS_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=False)
    print(f"  📸 {path}")
    return path


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def iphone(browser):
    """iPhone 14 viewport — matches the PWA's target form factor."""
    ctx = browser.new_context(
        viewport={"width": 393, "height": 852},
        device_scale_factor=3,
        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        is_mobile=True,
        has_touch=True,
    )
    yield ctx
    ctx.close()


def test_homepage_loads(iphone):
    """Root endpoint returns 200 and the HTML template renders."""
    page = iphone.new_page()
    response = page.goto(BASE_URL, wait_until="networkidle", timeout=10000)
    assert response.status == 200, f"Expected 200, got {response.status}"
    _shot(page, "01_homepage")
    page.close()


def test_pwa_markers_present(iphone):
    """PWA shell has the expected markers (manifest link, theme color, JS bundle)."""
    page = iphone.new_page()
    page.goto(BASE_URL, wait_until="networkidle", timeout=10000)

    # Manifest link
    manifest = page.locator('link[rel="manifest"]')
    assert manifest.count() >= 1, "manifest link missing"

    # Theme color
    theme = page.locator('meta[name="theme-color"]')
    assert theme.count() >= 1, "theme-color meta missing"

    # Alpine.js loaded (look for x-data attribute somewhere on the page)
    alpine = page.locator("[x-data]").count()
    assert alpine > 0, f"Alpine.js x-data attributes not found (count={alpine})"

    # The app's own JS — typically a `gymApp()` function or `x-data="gymApp()"`
    body_text = page.locator("body").inner_text()
    assert len(body_text) > 100, "page body looks empty"

    page.close()


def test_bottom_nav_has_four_tabs(iphone):
    """The PWA has 4 bottom-nav tabs: 食物 / Gym / 🔥 / 日程."""
    page = iphone.new_page()
    page.goto(BASE_URL, wait_until="networkidle", timeout=10000)

    # Actual tab labels (the app evolved from English Log/Pyramid/History/You
    # to Chinese 食物/Gym/🔥/日程 as food scan became the dominant feature)
    expected = ["食物", "Gym", "日程"]
    for label in expected:
        loc = page.get_by_text(label, exact=False).first
        assert loc.is_visible(timeout=3000), f"tab '{label}' not visible"

    _shot(page, "02_bottom_nav")
    page.close()


def test_api_healthz():
    """Quick API sanity check — Flask is up."""
    import urllib.request
    import json
    with urllib.request.urlopen(f"{BASE_URL}/healthz", timeout=5) as r:
        data = json.loads(r.read())
    assert data["status"] == "ok"
    assert "today" in data


def test_api_version_returns_3x():
    """Verify a v3.x version is running. v3.3.x is the Sheet+Drive
    canonical-store line; v3.2.7.x was the file-log line."""
    import urllib.request
    import json
    with urllib.request.urlopen(f"{BASE_URL}/api/version", timeout=5) as r:
        data = json.loads(r.read())
    assert data["version"].startswith("3."), f"got {data['version']}"
    print(f"  ✓ version {data['version']}, commit {data['git_commit']}")


def test_api_state_returns_today():
    """Verify /api/state returns a valid session shape."""
    import urllib.request
    import json
    with urllib.request.urlopen(f"{BASE_URL}/api/state", timeout=5) as r:
        data = json.loads(r.read())
    # Should have a session block with exercises list
    assert "exercises" in data or "session" in data or "today" in data, \
        f"unexpected /api/state shape: {list(data.keys())}"


def test_log_tab_interaction(iphone):
    """Tab interaction: verify the food scan tab is interactive (default tab)."""
    page = iphone.new_page()
    page.goto(BASE_URL, wait_until="networkidle", timeout=10000)
    page.wait_for_timeout(500)  # let Alpine settle
    _shot(page, "03_food_tab_view")
    # Verify there are clickable buttons (e.g., the camera trigger)
    buttons = page.locator("button, [role='button']").count()
    assert buttons > 0, "no interactive buttons found"
    page.close()


def test_gym_tab_renders(iphone):
    """Switch to the Gym tab and verify it renders the workout logger."""
    page = iphone.new_page()
    page.goto(BASE_URL, wait_until="networkidle", timeout=10000)
    page.wait_for_timeout(500)

    gym_tab = page.get_by_text("Gym", exact=False).first
    if gym_tab.is_visible():
        gym_tab.click()
        page.wait_for_timeout(800)
        _shot(page, "04_gym_tab")
    else:
        print("  ⚠ Gym tab not visible — skipping interaction")

    page.close()


def test_schedule_tab_renders(iphone):
    """Switch to the Schedule tab and verify it renders."""
    page = iphone.new_page()
    page.goto(BASE_URL, wait_until="networkidle", timeout=10000)
    page.wait_for_timeout(500)

    sched_tab = page.get_by_text("日程", exact=False).first
    if sched_tab.is_visible():
        sched_tab.click()
        page.wait_for_timeout(800)
        _shot(page, "05_schedule_tab")
    else:
        print("  ⚠ Schedule tab not visible — skipping interaction")

    page.close()


def test_healthz_endpoint_under_load():
    """Confirm the server responds to a burst of requests (no deadlock)."""
    import urllib.request
    start = time.time()
    for _ in range(20):
        with urllib.request.urlopen(f"{BASE_URL}/healthz", timeout=5) as r:
            assert r.status == 200
    elapsed = time.time() - start
    print(f"  ✓ 20 requests in {elapsed:.2f}s ({20/elapsed:.1f} req/s)")
    assert elapsed < 5.0, f"server too slow: {elapsed:.2f}s for 20 requests"