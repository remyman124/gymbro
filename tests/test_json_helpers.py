"""Tests for the JSON helper contract — atomic write semantics and
graceful read failure. These guard against the silent-corruption class of
bugs that the v3.x codebase is prone to (113 `except Exception` clauses).
"""

import json
import os
from pathlib import Path

from gym_web.core import safe_read_json, safe_write_json


def test_safe_write_replaces_atomically(tmp_path: Path):
    """Re-write an existing file and verify the tmp is cleaned up."""
    target = tmp_path / "data.json"
    target.write_text('{"old": 1}')
    assert safe_write_json(target, {"new": 2}) is True
    assert json.loads(target.read_text()) == {"new": 2}
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == [], f"tmp file leaked: {leftover}"


def test_safe_read_handles_missing_directory(tmp_path: Path):
    """Read against a non-existent parent dir — must return default, not raise."""
    missing = tmp_path / "no_such_dir" / "x.json"
    assert safe_read_json(missing, default=[]) == []


def test_safe_read_handles_permission_denied(tmp_path: Path, monkeypatch):
    """Simulate a file that exists but is unreadable. Must not raise to caller."""
    target = tmp_path / "locked.json"
    target.write_text('{"a": 1}')

    def boom(*_a, **_kw):
        raise PermissionError("denied")

    monkeypatch.setattr("builtins.open", boom)
    assert safe_read_json(target, default={"fallback": True}) == {"fallback": True}