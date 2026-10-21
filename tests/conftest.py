"""Shared fixtures for API integration tests.

CRITICAL: All tests that touch persistent state (workout log, nutrition log,
scan log, Whoop/Withings caches, Google Sheets) MUST go through these fixtures
to avoid polluting production data. The default autouse fixtures redirect
all file paths to temp files, so tests are safe by default.
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ──────────────────────────────────────────────────────────────────────────
# Module loading
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def gym_web_module():
    """Load gym_web.py by file path (it's shadowed by the gym_web/ package)."""
    repo = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "gym_web_main", repo / "gym_web.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="session")
def app(gym_web_module):
    """Flask app from the loaded module."""
    return gym_web_module.app


@pytest.fixture
def client(app):
    """Flask test client — no network, no actual server."""
    return app.test_client()


# ──────────────────────────────────────────────────────────────────────────
# File isolation — autouse so EVERY test is safe by default
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch, gym_web_module):
    """Redirect ALL persistent state to temp files. Never touches production.

    v3.3.2: removed NUTRITION_LOG_PATH + SCAN_LOG_PATH — Sheet + Drive are
    the canonical store now; no file-level persistence for food data.

    The patterns covered:
    - workout_log    → /home/work/.whoop_workout_log.json
    - whoop_cache    → /home/work/.whoop_data_latest.json
    - jim_context    → /home/work/.jim_context.json
    - google_token   → /home/work/.hermes/google_token.json

    v3.3.25 (Jim OOB 2026-10-20「Fully remove withings / Steps is
    whoop!」): `WITHINGS_CACHE` was DELETED from gym_web.py together with
    `gym_web/withings.py`. Patching it here raised
    `AttributeError: <module 'gym_web_main'> has no attribute
    'WITHINGS_CACHE'` at setup time — because this fixture is
    autouse=True it poisoned the ENTIRE suite (every test errored in
    setup, including tests that never touch Withings).
    """
    paths = {
        "WORKOUT_LOG": tmp_path / "workout_log.json",
        "WHOOP_CACHE": tmp_path / "whoop_cache.json",
        "JIM_CONTEXT": tmp_path / "jim_context.json",
        "GOOGLE_TOKEN_PATH": tmp_path / "google_token.json",
    }
    # Init each file with sane empty defaults
    paths["WORKOUT_LOG"].write_text("{}")
    paths["WHOOP_CACHE"].write_text(json.dumps({"recovery": {"records": []}, "workouts": []}))
    paths["JIM_CONTEXT"].write_text(json.dumps({"entries": {}}))
    paths["GOOGLE_TOKEN_PATH"].write_text(json.dumps({
        "client_id": "fake", "client_secret": "fake", "refresh_token": "fake"
    }))

    # Patch on the loaded module — only attributes that actually EXIST.
    # `monkeypatch.setattr` raises AttributeError for a missing module-level
    # constant, so a stale entry here takes down the whole suite.
    for attr, path in paths.items():
        if not hasattr(gym_web_module, attr):
            continue
        monkeypatch.setattr(gym_web_module, attr, path)

    return paths


# ──────────────────────────────────────────────────────────────────────────
# External service mocks — autouse so tests don't hit real APIs
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_api_keys(monkeypatch):
    """Provide deterministic API keys for any LLM call."""
    monkeypatch.setenv("PPLX_API_KEY", "test-pplx-key")
    monkeypatch.setenv("MINIMAX_API_KEY", "test-MiniMax-key")
    monkeypatch.setenv("APIYI_API_KEY", "test-apiyi-key")


@pytest.fixture(autouse=True)
def mock_google_sheets(monkeypatch, gym_web_module):
    """Mock Google Sheets token refresh so /api/sync_sheet doesn't hit real API."""
    monkeypatch.setattr(
        gym_web_module, "_get_google_access_token",
        lambda: "fake-google-access-token"
    )


@pytest.fixture(autouse=True)
def block_production_writes(monkeypatch, gym_web_module):
    """BLOCK ALL writes to Jim's production Nutrition tab + Drive.

    Jim OOB 2026-10-20: "Someone has pollute the production data. Pls
    avoid doing so" — the Nutrition tab is his GOLDEN COPY of the real
    food log.

    v3.3.25 incident (2026-10-20): `mock_google_sheets` above only faked
    the OAuth *token*, so `_append_to_sheet_nutrition` and
    `_upload_to_drive` still made REAL API calls with the real
    `/home/work/.hermes/google_token.json`. `tests/api/test_endpoints.py`
    posts a 1x1 base64 JPEG to `/api/scan_preview`, which auto-commits —
    that produced the 555-byte "未識別菜式 / 0 kcal" rows (sheet 656,
    660, 661, 662) and their orphan Drive files.

    Three independent layers, so a single missed mock can't leak:
      1. env var  → gym_web's own GYMBRO_WRITE_MODE=dryrun guard
      2. function mock → _append_to_sheet_nutrition returns a dryrun dict
      3. function mock → _upload_to_drive returns "" (no Drive file)
    """
    monkeypatch.setenv("GYMBRO_WRITE_MODE", "dryrun")

    _sheet_calls = []

    def _fake_append(entry):
        _sheet_calls.append(dict(entry))
        return {"ok": True, "range": "dryrun", "skipped": True,
                "would_have_written": [
                    {"name": entry.get("name", ""),
                     "calories": entry.get("calories", 0),
                     "would_have_written": True}]}

    monkeypatch.setattr(gym_web_module, "_append_to_sheet_nutrition", _fake_append)
    monkeypatch.setattr(gym_web_module, "_upload_to_drive", lambda *a, **kw: "")

    # Also stub the raw refresh helper — some paths read the real token
    # file directly instead of going through _get_google_access_token().
    monkeypatch.setattr(
        gym_web_module, "_refresh_google_access_token",
        lambda: "fake-google-access-token",
    )
    return _sheet_calls


@pytest.fixture
def captured_sheet_writes(block_production_writes):
    """Rows the test WOULD have written to the production sheet.

    Rule 4 (Alonso 2026-10-20): report a "would-have-written" row list
    instead of writing it. Tests that need to assert on the commit
    payload use this fixture rather than hitting the live sheet.
    """
    return block_production_writes


@pytest.fixture
def populated_whoop_cache(isolated_files):
    """Provide a stub Whoop cache with recovery + workout data."""
    isolated_files["WHOOP_CACHE"].write_text(json.dumps({
        "recovery": {"records": [
            {"score_state": "SCORED", "score": {"recovery_score": 72}}
        ]},
        "workouts": [
            {"sport_name": "weightlifting", "start": "2026-08-09T18:00:00Z",
             "end": "2026-08-09T19:00:00Z"},
            {"sport_name": "walking", "start": "2026-08-09T08:00:00Z",
             "end": "2026-08-09T08:30:00Z"},
        ],
    }))


# v3.3.25: `populated_withings_cache` REMOVED — Withings is gone from
# gymbro (Jim OOB 2026-10-20「Fully remove withings / Steps is whoop!」).
# `WITHINGS_CACHE` no longer exists on the module, so this fixture could
# only ever raise AttributeError. Steps now come from Whoop only; use
# `populated_whoop_cache` instead.


# v3.3.2: removed populated_scan_log + populated_nutrition_log fixtures —
# food data no longer lives in JSON files. Tests that depend on prior scan
# data should seed the in-memory NutritionCache directly via the gym_web
# module (see tests/api/test_food_log_visibility.py for the rewritten path).


# ── Redis isolation ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_shared_redis_cache():
    """Drop the shared `overlay:health` Redis entry around EVERY test.

    /api/health_overlay caches its merged payload in Redis for 60s, and that
    cache is a process-wide singleton (namespace "gymbro") SHARED WITH THE
    LIVE SERVER on port 7000. Without this, a test can read a payload the
    live server just wrote — production weight/recovery/steps instead of the
    fixture's values. That made `test_health_overlay_reads_caches` flaky
    (sometimes 48 = production recovery, sometimes None).

    The fix is a cache clear, not a mock: the endpoint's Redis path is
    exercised for real, and the test still asserts on the freshly built
    payload from the isolated fixture files.
    """
    try:
        from gym_web.redis_cache import get_redis_cache
        cache = get_redis_cache()
    except Exception:
        yield
        return
    cache.delete("overlay:health")
    yield
    cache.delete("overlay:health")
