"""Regression tests for /api/scan_delete — Jim OOB 2026-08-15
'i try to delete the redundant water but it cant'.

Bug 1 (v3.3.5): `get_cache()` was renamed at import time (to
`_get_nutrition_cache`) but the 7 call sites in gym_web.py still used
the bare name — all deletes returned 500 NameError.

Bug 2 (v3.3.5): the legacy matcher used a 3-min time tolerance and
T-separated parsing, so deleting row 443 (09:11) also matched row 442
(09:08) for 蘇打水 entries — over-deletion.

Fix: prefer direct row_index delete via
`_sheet_delete_nutrition_rows_by_indices([scan_index])` — a single-element
list targeting exactly the requested row. Fall back to a 0-tolerance
matcher only if the direct delete reports 0 rows deleted.

These tests run entirely against the Flask test client with a faked
NutritionCache + faked Sheet delete — they do NOT touch the live
Google Sheet and do NOT bind :7000.
"""
from dataclasses import dataclass
from unittest.mock import MagicMock


# ── Fake cache that satisfies api_scan_delete's lookup + evict surface ──

class _FakeLock:
    """RLock-compatible context manager for `with cache._lock:`."""
    def __enter__(self): return self
    def __exit__(self, *a): return False


@dataclass
class _FakeRow:
    """Just the attributes api_scan_delete reads (.date/.time/.kcal/.name)."""
    row_index: int
    date: str
    time: str
    kcal: int
    name: str


class _FakeCache:
    """Minimal stand-in for NutritionCache for api_scan_delete."""
    def __init__(self, rows: dict[int, dict]) -> None:
        self.by_row = {i: _FakeRow(row_index=i, **spec)
                       for i, spec in rows.items()}
        self._lock = _FakeLock()

    def get_by_row(self, row_index: int):
        return self.by_row.get(row_index)

    def _evict(self, row_index: int) -> None:
        self.by_row.pop(row_index, None)

    def _delete_with_shift(self, row_index: int) -> None:
        """Matches v3.3.5+ NutritionCache._delete_with_shift: evict row R,
        then decrement every higher row_index by 1 (both dict key and the
        row's own .row_index field)."""
        self.by_row.pop(row_index, None)
        for k in sorted(self.by_row):
            if k > row_index:
                row = self.by_row.pop(k)
                row.row_index = k - 1
                self.by_row[k - 1] = row


# ── Tests ────────────────────────────────────────────────────────────────

def test_scan_delete_returns_200_not_500(client, gym_web_module, monkeypatch):
    """v3.3.5 fix: previously returned 500 NameError because `get_cache`
    was renamed but call sites still used the bare name."""
    fake_cache = _FakeCache({443: {
        "date": "2026-08-15", "time": "09:11", "kcal": 0, "name": "蘇打水",
    }})
    sheet_mock = MagicMock(return_value={"ok": True, "deleted": 1, "errors": []})
    monkeypatch.setattr(gym_web_module, "get_cache", lambda: fake_cache)
    monkeypatch.setattr(gym_web_module,
                        "_sheet_delete_nutrition_rows_by_indices", sheet_mock)

    r = client.post("/api/scan_delete", json={
        "scan_index": 443,
        "timestamp_iso": "2026-08-15T09:11:00+08:00",
        "name": "蘇打水", "calories": 0,
    })

    assert r.status_code == 200, r.data
    d = r.get_json()
    assert d["ok"] is True, f"delete not ok: {d}"
    assert d["sheet_rows_deleted"] == 1, d


def test_scan_delete_exact_row_no_overdelete(client, gym_web_module, monkeypatch):
    """v3.3.5 fix: legacy matcher had 3-min tolerance and matched adjacent
    蘇打水 entries. New path uses row_index directly and calls the Sheet
    deleter with EXACTLY [scan_index] — never [scan_index, scan_index+1].

    Bug condition: two 蘇打水 entries on the same date within minutes
    (09:08 + 09:11). Legacy matcher would delete BOTH. New code targets
    only the requested row_index.
    """
    fake_cache = _FakeCache({
        442: {"date": "2026-08-15", "time": "09:08", "kcal": 0, "name": "蘇打水"},
        443: {"date": "2026-08-15", "time": "09:11", "kcal": 0, "name": "蘇打水"},
    })
    sheet_mock = MagicMock(return_value={"ok": True, "deleted": 1, "errors": []})
    monkeypatch.setattr(gym_web_module, "get_cache", lambda: fake_cache)
    monkeypatch.setattr(gym_web_module,
                        "_sheet_delete_nutrition_rows_by_indices", sheet_mock)

    r = client.post("/api/scan_delete", json={
        "scan_index": 442,
        "timestamp_iso": "2026-08-15T09:08:00+08:00",
        "name": "蘇打水", "calories": 0,
    })

    # 1) HTTP success
    assert r.status_code == 200, r.data
    d = r.get_json()
    assert d["ok"] is True

    # 2) Sheet deleter called with EXACTLY [442] — legacy would be [442, 443].
    assert sheet_mock.call_count == 1, (
        f"sheet deleter should be called exactly once, got {sheet_mock.call_count}"
    )
    call_args, _ = sheet_mock.call_args
    assert call_args[0] == [442], (
        f"sheet deleter must receive exactly [442]; legacy 3-min matcher "
        f"would have also matched 443 → got {call_args[0]}"
    )

    # 3) Response reports exactly 1 deletion.
    assert d["sheet_rows_deleted"] == 1, d


def test_scan_delete_missing_scan_index_returns_400(client):
    """Input guard — scan_index must be present and an int ≥ 2."""
    r = client.post("/api/scan_delete", json={"name": "x"})
    assert r.status_code == 400
    d = r.get_json()
    assert "scan_index" in (d.get("error") or "") or d.get("ok") is False


def test_scan_delete_reindexes_higher_rows(client, gym_web_module, monkeypatch):
    """After deleting row R, rows > R must shift down by 1 in the cache so
    the next delete/edit on those rows targets the correct Sheet row.

    Pre-existing bug: `_evict` only removed the row, leaving all higher
    by_row keys stale. Fixed by v3.3.5+ `_delete_with_shift`.
    """
    fake_cache = _FakeCache({
        441: {"date": "2026-08-09", "time": "09:00", "kcal": 100, "name": "A"},
        442: {"date": "2026-08-09", "time": "09:08", "kcal": 0, "name": "蘇打水"},
        443: {"date": "2026-08-09", "time": "09:11", "kcal": 0, "name": "蘇打水"},
        444: {"date": "2026-08-09", "time": "09:14", "kcal": 200, "name": "B"},
    })
    sheet_mock = MagicMock(return_value={"ok": True, "deleted": 1, "errors": []})
    monkeypatch.setattr(gym_web_module, "get_cache", lambda: fake_cache)
    monkeypatch.setattr(gym_web_module,
                        "_sheet_delete_nutrition_rows_by_indices", sheet_mock)

    # Delete row 442 (蘇打水 at 09:08). After reindex:
    #   441 stays at 441 (A)
    #   442 is gone
    #   443 → 442 (蘇打水 at 09:11)
    #   444 → 443 (B)
    r = client.post("/api/scan_delete", json={
        "scan_index": 442,
        "timestamp_iso": "2026-08-09T09:08:00+08:00",
        "name": "蘇打水", "calories": 0,
    })
    assert r.status_code == 200, r.data

    # 441 unchanged
    a = fake_cache.get_by_row(441)
    assert a is not None and a.name == "A" and a.row_index == 441
    # 442 is now the surviving 蘇打水 at 09:11 (was 443, decremented)
    s = fake_cache.get_by_row(442)
    assert s is not None and s.name == "蘇打水" and s.time == "09:11" and s.row_index == 442, (
        f"key 442 should hold the shifted 蘇打水@09:11; got {s}"
    )
    # 443 is now B (was 444, decremented)
    b = fake_cache.get_by_row(443)
    assert b is not None and b.name == "B" and b.row_index == 443, (
        f"key 443 should hold the shifted B; got {b}"
    )
    # 444 is now empty
    assert fake_cache.get_by_row(444) is None, (
        f"key 444 should be empty after reindex; got {fake_cache.get_by_row(444)}"
    )


def test_scan_delete_unknown_scan_index_returns_404(client, gym_web_module, monkeypatch):
    """Cache lookup guard — row_index not present in cache → 404."""
    monkeypatch.setattr(gym_web_module, "get_cache", lambda: _FakeCache({}))
    r = client.post("/api/scan_delete", json={"scan_index": 999999})
    assert r.status_code == 404
    assert r.get_json().get("ok") is False
