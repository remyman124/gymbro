"""
PRODUCTION WRITE-MODE GUARD (gymbro v3.3.25, 2026-10-20)

Jim OOB:「Someone has pollute the production data. Pls avoid doing so」

The Nutrition tab is Jim's golden copy. gymbro gained three write modes so
the recognition pipeline is testable without ever touching it:

  GYMBRO_WRITE_MODE=production  (DEFAULT — real Jim usage, writes)
  GYMBRO_WRITE_MODE=dryrun      (QA/E2E — never writes, returns the row
                                 that WOULD have been written)
  GYMBRO_WRITE_MODE=throwaway   (writes to GYMBRO_SHEET_ID — a COPY of the
                                 tab — so the full pipeline can be exercised)

Plus a hard backstop: _assert_not_test_fixture() refuses any known QA
fixture (sha256 registry OR a test_fixtures/ path) even when the write
mode was left at production.

Run:  PYTHONPATH='' .venv/bin/python -m pytest tests/qa/test_write_mode_guard.py -v
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

PROJECT = Path("/home/work/projects/gymbro")
FIXTURE_DIR = PROJECT / "test_fixtures"
REAL_PHOTO = Path("/home/work/.hermes/scan_cache/scan_20261020_140937.jpg")


def _load_gym_web():
    """Load gym_web.py by path.

    There is BOTH a `gym_web/` package and a `gym_web.py` module in the
    project root; the package wins on `import gym_web`, so the module must
    be loaded explicitly (same trick the rest of the repo uses).
    """
    os.environ.setdefault("GYMBRO_WRITE_MODE", "dryrun")
    if str(PROJECT) not in sys.path:
        sys.path.insert(0, str(PROJECT))
    spec = importlib.util.spec_from_file_location(
        "gym_web_mod", str(PROJECT / "gym_web.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gym_web_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gw():
    return _load_gym_web()


def test_write_mode_defaults_and_recognises_all_three(gw, monkeypatch):
    for mode in ("production", "dryrun", "throwaway"):
        monkeypatch.setenv("GYMBRO_WRITE_MODE", mode)
        assert gw._write_mode() == mode
    # unknown mode must fail safe to production (and warn), never silently dry
    monkeypatch.setenv("GYMBRO_WRITE_MODE", "banana")
    assert gw._write_mode() == "production"


def test_dryrun_appends_nothing_to_the_sheet(gw, monkeypatch):
    """The Sheets append POST must never be issued in dryrun mode."""
    monkeypatch.setenv("GYMBRO_WRITE_MODE", "dryrun")
    posted = []
    real = gw.urllib.request.urlopen

    def spy(req, *a, **kw):
        url = str(getattr(req, "full_url", req))
        if "sheets.googleapis.com" in url and "/values/" in url and "append" in url:
            posted.append(url)
        return real(req, *a, **kw)

    monkeypatch.setattr(gw.urllib.request, "urlopen", spy)
    res = gw._append_to_sheet_nutrition({
        "date": "2026-10-20", "time": "23:59", "meal_type": "scan",
        "name": "QA DRYRUN MUST NOT BE WRITTEN",
        "calories": 123, "protein": 10, "carbs": 10, "fat": 5,
    })
    assert res.get("ok") is True
    assert res.get("range") == "dryrun"
    assert res.get("skipped") is True
    assert res.get("would_have_written"), "dryrun must report the would-be row"
    assert posted == [], f"dryrun still POSTed to the sheet: {posted}"


def test_production_mode_refuses_a_qa_fixture(gw, monkeypatch):
    """The hard backstop: even with write mode = production, a fixture is refused."""
    monkeypatch.setenv("GYMBRO_WRITE_MODE", "production")
    fixture = FIXTURE_DIR / "food_1.jpg"
    assert fixture.exists()
    assert gw._is_test_fixture_image(str(fixture)) is True
    with pytest.raises(ValueError):
        gw._assert_not_test_fixture(str(fixture))


def test_fixture_detector_does_not_false_positive_on_a_real_photo(gw):
    if not REAL_PHOTO.exists():
        pytest.skip(f"real photo missing: {REAL_PHOTO}")
    assert gw._is_test_fixture_image(str(REAL_PHOTO)) is False


def test_synthetic_fixtures_are_registered_by_content_hash(gw):
    """Every fixture on disk must be in the sha256 registry, by name too."""
    registered = gw._TEST_FIXTURE_SHA256
    for f in sorted(FIXTURE_DIR.glob("*.jpg")):
        assert gw._sha256_file(f) in registered, (
            f"{f.name} is not in _TEST_FIXTURE_SHA256 — a new fixture must be "
            f"registered or the content-hash backstop will not catch it")
