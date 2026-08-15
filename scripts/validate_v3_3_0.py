#!/usr/bin/env python3
"""validate_v3_3_0.py — end-to-end smoke test for the v3.3.0 architecture.

Validates:
  1. Pre-flight:  gym_web is up, /api/version reports 3.3.0
  2. Cache stats: /api/scan_recent surfaces the in-memory NutritionCache
  3. Recent scans: cache count matches the actual Google Sheet row count
  4. Today's nutrition: /api/nutrition/today returns a non-empty `meals`
  5. Photo proxy:   /scan_img/<row_index> + /scan_thumb/<row_index> respond 200/302
  6. Image URL contract: scan dict has image_url + thumbnail_url keyed by row_index
  7. Hydration resilience: kill+restart gym_web, wait 8s, assert count unchanged

Usage:
  python3 /home/work/projects/gymbro/scripts/validate_v3_3_0.py

Exit codes:
  0  — all PASS
  1  — at least one FAIL (run log emitted to stderr)
  2  — pre-flight failed (server unreachable / wrong version)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants — mirror gym_web/core.py
# ---------------------------------------------------------------------------
HKT = timezone(timedelta(hours=8))
BASE_URL = os.environ.get("GYMBRO_BASE_URL", "http://localhost:7000")
EXPECTED_VERSION = "3.3.0"
SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
SHEET_TAB = "Nutrition"
SHEET_RANGE = f"{SHEET_TAB}!A1:K"
TOKEN_PATH = Path("/home/work/.hermes/google_token.json")
GYM_WEB_CMD = ["python3", "/home/work/projects/gymbro/gym_web.py"]
GYM_WEB_LOG = "/tmp/gym_web_validate.log"
HYDRATION_WAIT_S = 8
POST_KILL_WAIT_S = 3

# ---------------------------------------------------------------------------
# Tiny test harness
# ---------------------------------------------------------------------------
results: list[tuple[str, bool, str]] = []  # (name, ok, detail)


def _record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    marker = "PASS" if ok else "FAIL"
    line = f"[{marker}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line, flush=True)


def http_get(path: str, timeout: float = 60.0, allow_redirects: bool = False):
    """GET BASE_URL + path. Returns (status, body_bytes, headers).

    `allow_redirects=False` so we can observe the 302 from /scan_img/<row>.
    `urllib` follows redirects by default — we disable that via a custom opener.
    """
    url = f"{BASE_URL}{path}"
    opener = urllib.request.build_opener(
        urllib.request.HTTPRedirectHandler()  # noop; we override
    )
    # Simpler: build a request, set a non-following opener via NoRedirection.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def http_error_302(self, req, fp, code, msg, headers):  # noqa: N802
            return fp
        http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    if not allow_redirects:
        opener = urllib.request.build_opener(_NoRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": "gymbro-validate/1.0"})
    try:
        resp = opener.open(req, timeout=timeout)
        return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        # Treat HTTP errors as a status we can inspect.
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, body, dict(e.headers or {})
    except Exception as e:
        return 0, str(e).encode(), {}


# ---------------------------------------------------------------------------
# Sheet reader (mirrors gym_web._sheet_read_nutrition_rows)
# ---------------------------------------------------------------------------
def refresh_access_token() -> str:
    tok = json.loads(TOKEN_PATH.read_text())
    data = urllib.parse.urlencode({
        "client_id": tok["client_id"],
        "client_secret": tok["client_secret"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    access = resp["access_token"]
    tok["token"] = access
    TOKEN_PATH.write_text(json.dumps(tok, indent=2))
    return access


def fetch_sheet_rows() -> list[list[str]]:
    """Returns list of row arrays; first row is the header. Mirrors
    gym_web._sheet_read_nutrition_rows — same URL, same auth."""
    access = refresh_access_token()
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
        f"{SHEET_RANGE}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
    return body.get("values", [])


def sheet_data_row_count(rows: list[list[str]]) -> int:
    """Mirror NutritionCache.hydrate skip rules: drop header + drop blank rows."""
    count = 0
    for offset, cells in enumerate(rows):
        if offset == 0:
            continue
        padded = (list(cells) + [""] * 11)[:11]
        date, name = padded[0].strip(), padded[3].strip()
        if not date and not name:
            continue
        count += 1
    return count


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def check_preflight() -> bool:
    status, body, _ = http_get("/api/version")
    if status != 200:
        _record("1. preflight /api/version 200", False, f"status={status}")
        return False
    try:
        data = json.loads(body)
    except Exception as e:
        _record("1. preflight /api/version JSON", False, str(e))
        return False
    ver = data.get("version", "")
    ok = ver == EXPECTED_VERSION
    _record(
        "1. preflight /api/version",
        ok,
        f"version={ver!r} expected={EXPECTED_VERSION!r}",
    )
    return ok


def check_cache_stats() -> dict | None:
    status, body, _ = http_get("/api/scan_recent?limit=5")
    if status != 200:
        _record("2. cache stats reachable", False, f"status={status}")
        return None
    try:
        data = json.loads(body)
    except Exception as e:
        _record("2. cache stats JSON", False, str(e))
        return None
    cache = data.get("cache") or {}
    ok = (
        isinstance(cache.get("rows"), int)
        and cache.get("hydrated") is True
    )
    _record(
        "2. cache stats hydrated",
        ok,
        f"rows={cache.get('rows')} hydrated={cache.get('hydrated')}",
    )
    return data


def check_recent_vs_sheet(recent_data: dict | None) -> int:
    """Compare cache scan count to actual Sheet row count.
    Cache total = all cached rows (incl. failed scans); we filter out
    the failed-name bucket the same way /api/scan_recent does."""
    if not recent_data:
        _record("3. recent vs sheet count", False, "no recent_data")
        return 0
    cache_total = recent_data.get("total", 0)
    cache_filtered = recent_data.get("filtered", 0)
    cache_successful = cache_total - cache_filtered

    try:
        rows = fetch_sheet_rows()
    except Exception as e:
        _record("3. recent vs sheet count", False, f"sheet fetch failed: {e}")
        return 0
    sheet_count = sheet_data_row_count(rows)
    _record(
        "3. recent vs sheet count",
        cache_successful == sheet_count,
        f"cache_successful={cache_successful} sheet={sheet_count} "
        f"(cache_total={cache_total} filtered={cache_filtered})",
    )
    return cache_successful


def check_nutrition_today() -> None:
    status, body, _ = http_get("/api/nutrition/today")
    if status != 200:
        _record("4. /api/nutrition/today 200", False, f"status={status}")
        return
    try:
        data = json.loads(body)
    except Exception as e:
        _record("4. /api/nutrition/today JSON", False, str(e))
        return
    meals = data.get("meals") or []
    ok = isinstance(meals, list) and len(meals) > 0
    _record(
        "4. /api/nutrition/today meals non-empty",
        ok,
        f"date={data.get('date')} meal_count={data.get('meal_count')} "
        f"len(meals)={len(meals)}",
    )


def check_photo_proxy(recent_data: dict | None) -> None:
    if not recent_data or not recent_data.get("scans"):
        _record("5. /scan_img/<row> present", False, "no scans")
        _record("5. /scan_thumb/<row> present", False, "no scans")
        _record("6. image_url + thumbnail_url contract", False, "no scans")
        return

    # Pick the most recent scan; PWA exposes row_index in the dict.
    scans = recent_data["scans"]
    first = scans[0]
    row_index = first.get("row_index") or first.get("scan_index")
    if row_index is None:
        _record("5. row_index present in scan dict", False, "missing field")
        return
    _record("5. row_index present in scan dict", True, f"row_index={row_index}")

    # /scan_img/<row> — 302 redirect to drive.google.com is acceptable.
    status_img, _, hdr_img = http_get(f"/scan_img/{row_index}", allow_redirects=False)
    ok_img = status_img in (200, 302)
    detail_img = f"status={status_img} location={hdr_img.get('Location', '')[:80]}"
    _record("5. /scan_img/<row> 200/302", ok_img, detail_img)

    # /scan_thumb/<row> — proxies bytes (200) or 302 if proxying disabled.
    status_thumb, _, hdr_thumb = http_get(f"/scan_thumb/{row_index}", allow_redirects=False)
    ok_thumb = status_thumb in (200, 302)
    detail_thumb = f"status={status_thumb}"
    _record("5. /scan_thumb/<row> 200/302", ok_thumb, detail_thumb)

    # Image URL contract: scan dict should expose image_url + thumbnail_url.
    img_url = first.get("image_url", "")
    thumb_url = first.get("thumbnail_url", "")
    legacy_ok = img_url.startswith("/scan_img/") or (
        img_url.startswith("http") and "drive.google.com" in img_url
    )
    # Drive URLs end with =s220-c for the thumbnail hint — proxy version uses
    # the row-index path. Accept either.
    thumb_ok = thumb_url.startswith("/scan_thumb/") or (
        thumb_url.startswith("http") and "drive.google.com" in thumb_url
    )
    ok = bool(img_url) and bool(thumb_url) and legacy_ok and thumb_ok
    _record(
        "6. image_url + thumbnail_url contract",
        ok,
        f"image_url={img_url[:60]!r} thumbnail_url={thumb_url[:60]!r}",
    )


def check_hydration_resilience(baseline_count: int) -> None:
    """Kill gym_web, restart it, wait HYDRATION_WAIT_S, assert count unchanged."""
    # Find PID
    try:
        out = subprocess.check_output(
            ["ps", "aux"], text=True,
        )
    except Exception as e:
        _record("7. find gym_web PID", False, str(e))
        return
    pids: list[str] = []
    for line in out.splitlines():
        if "gym_web.py" in line and "grep" not in line:
            parts = line.split()
            if len(parts) >= 2:
                pids.append(parts[1])
    if not pids:
        _record("7. find gym_web PID", False, "no gym_web.py process found")
        return
    _record("7. find gym_web PID", True, f"pids={','.join(pids)}")

    # SIGTERM (not SIGKILL) — graceful shutdown
    for pid in pids:
        try:
            os.kill(int(pid), 15)  # SIGTERM
        except ProcessLookupError:
            pass
        except Exception as e:
            _record(f"7. kill -TERM {pid}", False, str(e))
            return
    _record(f"7. kill -TERM {','.join(pids)}", True, "SIGTERM sent")

    # Wait for the process to release the port
    time.sleep(POST_KILL_WAIT_S)

    # Verify it really died
    out2 = subprocess.check_output(["ps", "aux"], text=True)
    still_alive = [
        line.split()[1]
        for line in out2.splitlines()
        if "gym_web.py" in line and "grep" not in line
    ]
    if still_alive:
        _record("7. process gone after SIGTERM", False, f"still alive: {still_alive}")
        return
    _record("7. process gone after SIGTERM", True, "")

    # Restart via start_new_session so the child survives our exit and is
    # detached from our process group / controlling terminal.
    try:
        subprocess.Popen(
            GYM_WEB_CMD,
            stdout=open(GYM_WEB_LOG, "ab"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        _record("7. restart gym_web", False, str(e))
        return
    _record("7. restart gym_web", True, f"spawned {' '.join(GYM_WEB_CMD)}")

    # Wait for hydration
    print(f"  ... waiting {HYDRATION_WAIT_S}s for hydration ...", flush=True)
    time.sleep(HYDRATION_WAIT_S)

    # Wait for /api/version to become reachable (server boot)
    deadline = time.time() + 30
    version_ok = False
    while time.time() < deadline:
        status, body, _ = http_get("/api/version", timeout=3)
        if status == 200 and EXPECTED_VERSION in body.decode("utf-8", "ignore"):
            version_ok = True
            break
        time.sleep(1)
    if not version_ok:
        _record("7. server back up after restart", False, f"see {GYM_WEB_LOG}")
        return
    _record("7. server back up after restart", True, "")

    # Re-query /api/scan_recent?limit=200
    status, body, _ = http_get("/api/scan_recent?limit=200")
    if status != 200:
        _record("7. /api/scan_recent after restart", False, f"status={status}")
        return
    try:
        data = json.loads(body)
    except Exception as e:
        _record("7. /api/scan_recent JSON after restart", False, str(e))
        return
    cache = data.get("cache") or {}
    total_after = data.get("total", 0)
    filtered_after = data.get("filtered", 0)
    successful_after = total_after - filtered_after
    hydrated = cache.get("hydrated") is True
    ok = hydrated and successful_after == baseline_count
    _record(
        "7. hydration resilience (count unchanged after restart)",
        ok,
        f"baseline={baseline_count} after={successful_after} "
        f"hydrated={hydrated}",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    print(f"v3.3.0 architecture validator")
    print(f"  base_url = {BASE_URL}")
    print(f"  sheet    = {SHEET_ID} / {SHEET_TAB}")
    print()

    if not check_preflight():
        print()
        print("SUMMARY: 1/1 FAIL — preflight failed; aborting.")
        return 2

    print()
    recent_data = check_cache_stats()
    print()
    baseline_count = check_recent_vs_sheet(recent_data)
    print()
    check_nutrition_today()
    print()
    check_photo_proxy(recent_data)
    print()
    check_hydration_resilience(baseline_count)
    print()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print("=" * 60)
    print(f"SUMMARY: {passed} PASS, {failed} FAIL  ({len(results)} total)")
    if failed:
        print("Failures:")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}  ({detail})")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())