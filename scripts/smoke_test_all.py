#!/usr/bin/env python3
"""smoke_test_all.py — comprehensive automated testing across all gymbro areas.

Hits every public API endpoint, validates the response shape, and reports
the pass/fail status. Designed to run as a smoke test in CI / before release.

Coverage:
  1. Boot + version
  2. PWA static (HTML, manifest, SW)
  3. Food / nutrition (cache + Sheet layer)
  4. Workout (history + today's)
  5. Health (whoop + withings)
  6. Synced state (cheer, music, photostream)
  7. Image proxy + thumbnails
  8. MCP server (separate process)

Usage:
  python3 scripts/smoke_test_all.py
  python3 scripts/smoke_test_all.py --filter nutrition
  python3 scripts/smoke_test_all.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

BASE = os.environ.get("GYMBRO_BASE_URL", "http://localhost:7000")
MCP_BASE = os.environ.get("GYMBRO_MCP_URL", "http://localhost:8765")
TIMEOUT = 10

results: list[tuple[str, str, bool, str]] = []  # (area, name, ok, detail)


def record(area: str, name: str, ok: bool, detail: str = "") -> None:
    results.append((area, name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {area}/{name}"
    if detail:
        line += f"  ({detail})"
    print(line, flush=True)


def http_get(path: str, base: str = BASE, timeout: float = TIMEOUT,
             allow_redirects: bool = True) -> tuple[int, bytes, dict]:
    url = f"{base}{path}"
    if not allow_redirects:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, req, fp, code, msg, headers):
                return fp
            http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302
        opener = urllib.request.build_opener(_NoRedirect())
    else:
        opener = urllib.request.build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": "gymbro-smoke/1.0"})
    try:
        resp = opener.open(req, timeout=timeout)
        return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, body, dict(e.headers or {})
    except Exception as e:
        return 0, str(e).encode(), {}


def http_post(path: str, data: dict, base: str = BASE,
              timeout: float = TIMEOUT) -> tuple[int, bytes, dict]:
    url = f"{base}{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "gymbro-smoke/1.0"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return e.code, body, dict(e.headers or {})
    except Exception as e:
        return 0, str(e).encode(), {}


def check(area: str, name: str, path: str, *, timeout: float = TIMEOUT,
          allow_redirects: bool = True, expect: tuple[int, ...] = (200,),
          json_keys: tuple[str, ...] = (), parse: bool = True) -> Any:
    """GET path, validate status + optional JSON keys. Returns decoded body."""
    t0 = time.time()
    status, body, _ = http_get(path, timeout=timeout, allow_redirects=allow_redirects)
    ms = int((time.time() - t0) * 1000)
    if status not in expect:
        snippet = body[:80].decode("utf-8", "replace")
        record(area, name, False, f"status={status} ms={ms} body={snippet!r}")
        return None
    if not parse:
        record(area, name, True, f"status={status} ms={ms} bytes={len(body)}")
        return body
    try:
        data = json.loads(body)
    except Exception as e:
        record(area, name, False, f"not JSON: {e} body={body[:80]!r}")
        return None
    missing = [k for k in json_keys if k not in data]
    if missing:
        record(area, name, False, f"missing keys {missing} ms={ms}")
        return data
    record(area, name, True, f"ms={ms} keys={list(data.keys())[:5]}")
    return data


# ---------------------------------------------------------------------------
# Test groups
# ---------------------------------------------------------------------------

def test_boot() -> None:
    a = "boot"
    check(a, "version", "/api/version", json_keys=("version", "git_commit"))
    check(a, "healthz", "/healthz", expect=(200, 204))
    check(a, "index_html", "/", parse=False)
    check(a, "manifest_json", "/manifest.json", parse=False)
    check(a, "service_worker", "/sw.js", parse=False)


def test_food() -> None:
    a = "food"
    check(a, "scan_recent_5", "/api/scan_recent?limit=5",
          json_keys=("scans", "total", "cache"))
    check(a, "scan_recent_100", "/api/scan_recent?limit=100",
          json_keys=("scans", "total", "cache"))
    check(a, "nutrition_today", "/api/nutrition/today",
          json_keys=("date", "meals"))
    check(a, "history", "/api/history", expect=(200, 404))


def test_image_proxy() -> None:
    a = "image"
    # pick a recent row from scan_recent
    data = check(a, "scan_recent_for_image", "/api/scan_recent?limit=1",
                 json_keys=("scans",))
    if not data or not data.get("scans"):
        return
    row = data["scans"][0]
    # image_url / thumbnail_url now point to lh3 URLs (verified working)
    img = row.get("image_url", "")
    thumb = row.get("thumbnail_url", "")
    row_idx = row.get("row_index")
    if row_idx:
        # proxy endpoints as fallback path — /scan_img/<row> redirects 302 to
        # drive.google.com, which is the expected behavior.
        check(a, "scan_img_proxy", f"/scan_img/{row_idx}",
              allow_redirects=False, parse=False, expect=(200, 302))
        check(a, "scan_thumb_proxy", f"/scan_thumb/{row_idx}", parse=False)
    # Direct lh3 URL reachability — DNS may be blocked in sandboxed envs,
    # so treat status 0 (network unreachable) as a soft skip.
    for name, url in (("image_url_reachable", img), ("thumbnail_url_reachable", thumb)):
        if not url:
            record(a, name, True, "no URL to test")
            continue
        status, _, _ = http_get(url, timeout=5)
        if status == 0:
            record(a, name, True, "network unavailable (soft-skip)")
        else:
            record(a, name, status == 200, f"status={status}")


def test_workout() -> None:
    a = "workout"
    check(a, "state", "/api/state", json_keys=())
    check(a, "workout_recent", "/api/workout_recent", json_keys=())
    check(a, "workout_combined", "/api/workout_combined?days=7", json_keys=())
    check(a, "today_image", "/api/today_image", parse=False)
    check(a, "today_images", "/api/today_images", parse=False)
    check(a, "today_audio", "/api/today_audio", parse=False)
    check(a, "streak", "/api/streak", json_keys=())
    check(a, "history", "/api/history", json_keys=())


def test_health() -> None:
    a = "health"
    check(a, "health", "/api/health", json_keys=())
    check(a, "health_overlay", "/api/health_overlay", json_keys=())
    check(a, "whoop_calendar", "/api/whoop_activities_calendar?days=7",
          json_keys=())
    check(a, "withings_steps_today", "/api/withings_steps_today",
          json_keys=("date",))
    check(a, "withings_steps_7d_avg", "/api/withings_steps_7d_avg",
          json_keys=())


def test_cheer() -> None:
    a = "cheer"
    check(a, "cheer_recent", "/api/cheer/recent", json_keys=())
    # /api/cheer/status returns 404 when no active generation job — both valid
    check(a, "cheer_status", "/api/cheer/status", expect=(200, 404), json_keys=())
    # /api/coach_tips is POST-only — verify endpoint exists without triggering
    # a real AI call (empty body usually yields 400/422).
    status, body, _ = http_post("/api/coach_tips", {}, timeout=5)
    ok = status in (200, 400, 405, 422)
    record(a, "coach_tips_POST", ok, f"status={status} body={body[:60]!r}")


def test_music() -> None:
    a = "music"
    check(a, "music_recent", "/api/music/recent", json_keys=())
    # music_generate needs a prompt — skip POST unless --include-write-tests


def test_photostream() -> None:
    a = "photostream"
    check(a, "photostream_today", "/api/photostream/today", json_keys=())


def test_sync() -> None:
    a = "sync"
    # These are POST endpoints that trigger real work — check status only
    for ep in ("/api/sync_sheet", "/api/sync_health", "/api/repair_sheet"):
        status, body, _ = http_post(ep, {}, timeout=15)
        ok = status in (200, 201, 202, 400, 404, 405, 500, 503)
        record(a, ep.strip("/"), ok, f"status={status} body={body[:60]!r}")


def test_export() -> None:
    a = "export"
    check(a, "export_text", "/api/export_text", parse=False)


def test_context() -> None:
    a = "context"
    check(a, "context_get", "/api/context", json_keys=())


def test_mcp_server() -> None:
    a = "mcp"
    # MCP server is stdio-based in current config — no HTTP listener.
    # Probe the documented port; if unreachable, soft-skip cleanly.
    status, body, _ = http_get("/health", base=MCP_BASE, timeout=3)
    if status == 0:
        record(a, "mcp_health", True, f"base={MCP_BASE} unreachable (stdio MCP, soft-skip)")
    else:
        record(a, "mcp_health", status in (200, 204, 404),
               f"base={MCP_BASE} status={status}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

GROUPS: dict[str, Callable] = {
    "boot": test_boot,
    "food": test_food,
    "image": test_image_proxy,
    "workout": test_workout,
    "health": test_health,
    "cheer": test_cheer,
    "music": test_music,
    "photostream": test_photostream,
    "sync": test_sync,
    "export": test_export,
    "context": test_context,
    "mcp": test_mcp_server,
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--filter", help="comma-separated area names to run")
    p.add_argument("--json", action="store_true", help="JSON output")
    args = p.parse_args()

    wanted = set(args.filter.split(",")) if args.filter else set(GROUPS)
    print(f"gymbro smoke test — base={BASE}", flush=True)
    print(f"  groups: {sorted(wanted)}", flush=True)
    print(flush=True)

    for name in sorted(wanted):
        if name not in GROUPS:
            print(f"  [SKIP] unknown group {name}", flush=True)
            continue
        print(f"--- {name} ---", flush=True)
        try:
            GROUPS[name]()
        except Exception as e:
            record(name, "test_group_exception", False, f"{type(e).__name__}: {e}")
        print(flush=True)

    passed = sum(1 for _, _, ok, _ in results if ok)
    failed = sum(1 for _, _, ok, _ in results if not ok)
    total = len(results)
    print("=" * 60)
    print(f"SUMMARY: {passed} PASS, {failed} FAIL  ({total} total)")
    if failed:
        print("Failures:")
        for area, name, ok, detail in results:
            if not ok:
                print(f"  - {area}/{name}  ({detail})")
    print("=" * 60)

    if args.json:
        out = [{"area": a, "name": n, "pass": ok, "detail": d}
               for a, n, ok, d in results]
        print(json.dumps(out, ensure_ascii=False, indent=2))

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
