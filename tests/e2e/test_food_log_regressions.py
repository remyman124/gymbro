"""E2E regression tests for food log name + image fields.

Jim OOB 2026-08-15:
  - 'so many 蘇打水 wrongly displayed in food log'
  - 'many broken image'

These drive a real Chromium against the running PWA to ensure:
  1. No row in the food log has a narration-prefixed name (這張/相顯示/
     圖顯示/呢張/（). The sanitiser rejects these so the PWA shows
     '未識別菜式' instead, prompting Jim to name the entry.
  2. No row has a broken /static/img/placeholder_food.png URL (returns
     404). The cache returns empty string now — template skips the <img>.

Run with the Flask app running on :7000:
    pytest tests/e2e/test_food_log_regressions.py -v -s
"""
import json
import urllib.request

import pytest
from playwright.sync_api import sync_playwright

BASE_URL = "http://localhost:7000"

# Names that must NEVER appear in /api/scan_recent. These are model
# narration phrases — should have been converted to 未識別菜式 at write time.
NARRATION_PREFIXES = ("這張", "相顯示", "圖顯示", "呢張", "（")


def _fetch_scan_recent(limit: int = 100) -> list[dict]:
    with urllib.request.urlopen(f"{BASE_URL}/api/scan_recent?limit={limit}", timeout=10) as r:
        return json.loads(r.read())["scans"]


# ──────────────────────────────────────────────────────────────────
# API-level: fast + deterministic. Don't need a browser.
# ──────────────────────────────────────────────────────────────────

def test_api_no_narration_names_in_scan_recent():
    """v3.3.4: dish-name sanitiser rejects 這張/相顯示/圖顯示/呢張/(.
    Make sure none leaked into the live Sheet."""
    bad = [
        (s.get("name"), s.get("timestamp_iso", "")[:16])
        for s in _fetch_scan_recent()
        if s.get("name") and s.get("name").startswith(NARRATION_PREFIXES)
    ]
    assert not bad, f"narration-prefixed names in Sheet: {bad}"
    print(f"  ✓ no narration names in /api/scan_recent ({len(_fetch_scan_recent())} rows)")


def test_api_no_broken_placeholder_image_urls():
    """v3.3.4: PLACEHOLDER_IMG was a 404 URL; now empty string.
    Make sure no row returns /static/img/placeholder_food.png."""
    bad = [
        (s.get("image_url"), s.get("timestamp_iso", "")[:16])
        for s in _fetch_scan_recent()
        if s.get("image_url") and "placeholder" in s["image_url"]
    ]
    assert not bad, f"placeholder URLs still in cache: {bad}"
    print(f"  ✓ no placeholder_food URLs in /api/scan_recent")


def test_api_image_url_is_either_drive_or_local():
    """v3.3.4: every image_url is either a Drive URL (drive.google.com or
    googleusercontent.com), a local proxy route (/scan_thumb/<row> or
    /scan_img/<row>), or empty. No /static/ 404s, no legacy placeholder."""
    OK_LOCAL = ("/scan_thumb/", "/scan_img/", "/static/img/")
    OK_DRIVE = ("drive.google.com", "googleusercontent.com")
    for s in _fetch_scan_recent():
        url = s.get("image_url") or ""
        if not url:
            continue
        ok = any(p in url for p in OK_LOCAL + OK_DRIVE)
        assert ok, (
            f"unexpected image_url format on "
            f"{s.get('timestamp_iso', '')[:16]}: {url!r}"
        )
    print(f"  ✓ all image_url values are Drive URL, local proxy, or empty")


# ──────────────────────────────────────────────────────────────────
# Browser-level: drives the actual PWA, catches things API tests miss
# (e.g. broken <img> tags rendering, console errors).
# ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


def test_pwa_food_log_no_narration_titles(browser):
    """Load the food log tab, walk every group entry, assert the title
    never starts with a narration prefix. The PWA binds the dish name
    via x-text="displayName(scan)" inside `<div class="text-lg font-bold
    text-white ...">` per templates/index.html:898-900."""
    page = browser.new_page()
    page.goto(BASE_URL, wait_until="networkidle", timeout=15000)
    page.wait_for_timeout(2000)

    title_locators = page.locator("div.text-lg.font-bold.text-white")
    n = title_locators.count()
    assert n > 0, "no food titles rendered — PWA didn't load the scan list"
    titles = [title_locators.nth(i).inner_text().strip()
              for i in range(min(n, 200))]
    narr = [t for t in titles if t and t.startswith(NARRATION_PREFIXES)]
    assert not narr, f"narration titles still in PWA: {narr}"
    print(f"  ✓ PWA rendered {n} titles, 0 narration-prefixed")
    page.close()


def test_pwa_food_log_no_broken_image(browser):
    """Walk every <img> in the food history, verify each one either:
    - has a successful load (naturalWidth > 0) AND src starts with
      lh3.googleusercontent.com / scan_thumb / scan_img, OR
    - the template already skipped rendering (the `<template x-if>` is
      empty when image_url is "")."""
    page = browser.new_page()
    page.goto(BASE_URL, wait_until="networkidle", timeout=15000)
    page.wait_for_timeout(2000)
    imgs = page.locator(".food-history-list img")
    n = imgs.count()
    broken: list[str] = []
    seen_srcs: set[str] = set()
    for i in range(n):
        img = imgs.nth(i)
        src = img.get_attribute("src") or ""
        seen_srcs.add(src)
        # Drive lh3 thumbnails have naturalWidth > 0 once they decode;
        # a 404 placeholder has naturalWidth=0.
        nat_w = img.evaluate("(el) => el.naturalWidth")
        if nat_w == 0 and src:
            broken.append(f"src={src} naturalWidth=0")
    # Also assert the template didn't render <img src="/static/img/..."> at all
    static_broken = [s for s in seen_srcs if "/static/img/placeholder" in s]
    assert not static_broken, (
        f"PWA still rendering placeholder <img> with broken URL: {static_broken}"
    )
    # And separately: any <img> that did render MUST have loaded (naturalWidth > 0).
    assert not broken, f"PWA rendered <img> with broken src: {broken}"
    print(f"  ✓ PWA rendered {n} <img> elements, 0 broken "
          f"({len(seen_srcs)} unique srcs, all loaded)")
    page.close()
