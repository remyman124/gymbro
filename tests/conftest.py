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

    The patterns covered:
    - workout_log    → /home/work/.whoop_workout_log.json
    - nutrition_log  → /home/work/.hermes/nutrition_log.json
    - scan_log       → /home/work/.hermes/food_scan_log.json
    - whoop_cache    → /home/work/.whoop_data_latest.json
    - withings_cache → /home/work/.withings_latest_cache.json
    - jim_context    → /home/work/.jim_context.json
    - google_token   → /home/work/.hermes/google_token.json
    """
    paths = {
        "WORKOUT_LOG": tmp_path / "workout_log.json",
        "NUTRITION_LOG_PATH": tmp_path / "nutrition_log.json",
        "SCAN_LOG_PATH": tmp_path / "food_scan_log.json",
        "WHOOP_CACHE": tmp_path / "whoop_cache.json",
        "WITHINGS_CACHE": tmp_path / "withings_cache.json",
        "JIM_CONTEXT": tmp_path / "jim_context.json",
        "GOOGLE_TOKEN_PATH": tmp_path / "google_token.json",
    }
    # Init each file with sane empty defaults
    paths["WORKOUT_LOG"].write_text("{}")
    paths["NUTRITION_LOG_PATH"].write_text(json.dumps({"meals": []}))
    paths["SCAN_LOG_PATH"].write_text("[]")
    paths["WHOOP_CACHE"].write_text(json.dumps({"recovery": {"records": []}, "workouts": []}))
    paths["WITHINGS_CACHE"].write_text(json.dumps({"body": {}, "steps": {}}))
    paths["JIM_CONTEXT"].write_text(json.dumps({"entries": {}}))
    paths["GOOGLE_TOKEN_PATH"].write_text(json.dumps({
        "client_id": "fake", "client_secret": "fake", "refresh_token": "fake"
    }))

    # Patch on the loaded module
    for attr, path in paths.items():
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


@pytest.fixture
def populated_withings_cache(isolated_files):
    """Provide a stub Withings cache with body + steps data."""
    isolated_files["WITHINGS_CACHE"].write_text(json.dumps({
        "body": {"weight_kg": 75.2, "fat_pct": 18.5, "date": "2026-08-09"},
        "steps": {
            "today": {"steps": 548, "distance_km": 0.41, "calories": 13.9,
                      "date": "2026-08-09"},
            "yesterday": {"steps": 6048, "distance_km": 4.54, "calories": 281.1,
                          "date": "2026-08-08"},
        },
    }))


@pytest.fixture
def populated_scan_log(isolated_files):
    """Provide a stub scan log with 2 entries."""
    isolated_files["SCAN_LOG_PATH"].write_text(json.dumps([
        {"scan_index": 1, "date": "2026-08-09", "time": "12:00",
         "name": "白飯", "kcal": 117, "protein": 2.4, "carbs": 25.8, "fat": 0,
         "user_corrections": []},
        {"scan_index": 2, "date": "2026-08-09", "time": "13:00",
         "name": "凍咖啡", "kcal": 180, "protein": 4, "carbs": 30, "fat": 6,
         "user_corrections": [{"note": "less sugar"}]},
    ]))


@pytest.fixture
def populated_nutrition_log(isolated_files):
    """Provide today's nutrition_log with 2 meals."""
    isolated_files["NUTRITION_LOG"].write_text(json.dumps({
        "meals": [
            {"date": "2026-08-09", "time": "12:00", "name": "白飯",
             "calories": 117, "protein": 2.4, "carbs": 25.8, "fat": 0},
            {"date": "2026-08-09", "time": "13:00", "name": "凍咖啡",
             "calories": 180, "protein": 4, "carbs": 30, "fat": 6},
        ]
    }))
