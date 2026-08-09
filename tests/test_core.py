"""Tests for gym_web.core — guards the constants and helpers used across
both gym_web.py and gymbro_mcp_server.py.
"""

import json
from pathlib import Path

import pytest

from gym_web.core import (
    VERSION,
    detect_intensity,
    default_reps,
    safe_read_json,
    safe_write_json,
)


def test_version_is_string():
    """Version follows gymbro's MAJOR.MINOR.PATCH.MICRO scheme
    (e.g. 3.2.7.15). Accept any number of dot-separated numeric segments ≥ 2.
    """
    assert isinstance(VERSION, str)
    parts = VERSION.split(".")
    assert len(parts) >= 2
    assert all(p.isdigit() for p in parts)


def test_default_reps_is_ten():
    """Jim OOB 2026-07-18: default reps = 10. This is a persistent rule —
    a regression here breaks every default-set tap on the PWA.
    """
    assert default_reps() == 10


@pytest.mark.parametrize("set_n,expected", [
    (1, "warm-up"),
    (2, "warm-up"),
    (3, "working"),
    (4, "working"),
    (5, "burn-out"),
    (10, "burn-out"),
])
def test_detect_intensity_pyramid(set_n, expected):
    """Pyramid view color-codes each set based on intensity."""
    assert detect_intensity(set_n) == expected


def test_detect_intensity_custom_working_target():
    """Jim sometimes runs 5 working sets; set 6 should still be burn-out."""
    assert detect_intensity(5, working_target=5) == "working"
    assert detect_intensity(6, working_target=5) == "burn-out"


def test_safe_read_json_returns_default_when_missing(tmp_path: Path):
    missing = tmp_path / "nope.json"
    assert safe_read_json(missing) == {}
    assert safe_read_json(missing, default={"x": 1}) == {"x": 1}


def test_safe_read_json_returns_default_on_corrupt(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    assert safe_read_json(bad, default={"fallback": True}) == {"fallback": True}


def test_safe_read_json_happy_path(tmp_path: Path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"a": 1, "b": [2, 3]}))
    assert safe_read_json(good) == {"a": 1, "b": [2, 3]}


def test_safe_write_json_atomic(tmp_path: Path):
    """Atomic write: a tmp file is created then renamed. If we crash mid-write,
    the original file is untouched.
    """
    target = tmp_path / "out.json"
    target.write_text(json.dumps({"v": 1}))

    ok = safe_write_json(target, {"v": 2})
    assert ok is True
    assert json.loads(target.read_text()) == {"v": 2}
    assert not (tmp_path / "out.json.tmp").exists()


def test_safe_write_json_creates_parent_dirs(tmp_path: Path):
    target = tmp_path / "nested" / "deeper" / "out.json"
    assert safe_write_json(target, {"x": 1}) is True
    assert json.loads(target.read_text()) == {"x": 1}