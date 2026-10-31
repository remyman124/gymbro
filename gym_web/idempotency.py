"""Persistent idempotency key store for sheet writes.

v3.2.7.48: prevents duplicate Sheet appends from iOS PWA double-taps,
4-photo upload queue retries, or client-side retry storms (Jim OOB
2026-08-14 'audit found 30 duplicate rows').

Design contract:
- Backing file: /home/work/.hermes/commit_idempotency.json
- Bounded to most recent ~500 keys (drop oldest FIFO).
- MUST NEVER raise to the caller. On any IO/JSON error, fail OPEN
  (treat the key as new) so a corrupt store can never block a
  legitimate food log.
- Thread/loop safe enough for the single-process Flask app context;
  we accept the small race where two concurrent writes both pass the
  check — the in-sheet dedup downstream is the safety net.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Dict

# Reuse core helpers so atomic-write semantics + fail-open behavior stay
# consistent with the rest of gym_web.
from gym_web.core import safe_read_json as _safe_read_json
from gym_web.core import safe_write_json as _safe_write_json
# v3.3.71 (Frimpong 2026-10-31):read-modify-write 嘅跨 thread 鎖。
# 之前 `check_and_remember()` 冇 lock → load → check → modify → persist
# 四步之間可以畀另一個 thread 插隊。實測(8 thread 同一個 key):
#     returned True (would commit) : 8   <-- 應該係 1
#  即係 iOS PWA double-tap 會產生 8 個重複 Sheet row
#  (Jim OOB 2026-08-14「audit found 30 duplicate rows」嗰類問題)。
# 見 gym_web/filelock.py 嘅 docstring。
from gym_web.filelock import atomic_write_json as _atomic_write_json
from gym_web.filelock import lock_for as _lock_for

_STORE_PATH = Path("/home/work/.hermes/commit_idempotency.json")
_MAX_KEYS = 500
# Map key (str) -> epoch timestamp (float) when it was first seen.
_BLANK: Dict[str, float] = {}


def _now() -> float:
    return time.time()


def _load() -> Dict[str, float]:
    """Load the store. Returns a fresh dict on any failure."""
    data = _safe_read_json(_STORE_PATH, default={})
    if not isinstance(data, dict):
        return {}
    # Drop any non-numeric timestamps left over from a corrupted file.
    return {k: float(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, (int, float))}


def _persist(data: Dict[str, float]) -> None:
    """Persist the store.

    v3.3.71:用唯一 tmp 名(之前 `core.safe_write_json()` 用固定 `.tmp` 名,
    兩個 writer 會互相覆蓋同一個 tmp)。
    """
    _atomic_write_json(_STORE_PATH, data)


def _evict_oldest(data: Dict[str, float]) -> None:
    """Drop oldest entries so the store stays bounded to _MAX_KEYS."""
    if len(data) <= _MAX_KEYS:
        return
    # Sort by timestamp ascending; trim from the head.
    by_age = sorted(data.items(), key=lambda kv: kv[1])
    to_drop = len(data) - _MAX_KEYS
    for k, _ in by_age[:to_drop]:
        data.pop(k, None)


def check_and_remember(key: str) -> bool:
    """Return True if the key is NEW (caller should proceed with the
    commit); False if it was already seen within the bounded window.

    v3.3.71 (Frimpong):整個 load → check → modify → persist 喺 **同一把
    per-path lock** 入面做。以前冇 lock,兩個 thread 可以同時見到
    「key 未存在」→ 都返 True → 都 commit → 重複 Sheet row。
    實測修正前:8 個 thread 同一 key → 8 個 True(應該 1 個)。

    NEVER raises. On any internal failure, returns True (fail OPEN) so
    a corrupt store cannot block a legitimate food log. The in-sheet
    dedup in _append_to_sheet_nutrition remains the authoritative
    backstop.
    """
    try:
        if not key:
            # Empty key is never a legitimate identity — treat as new so
            # the caller proceeds and the in-sheet dedup can decide.
            return True
        with _lock_for(_STORE_PATH):
            store = _load()
            if key in store:
                return False
            store[key] = _now()
            _evict_oldest(store)
            _persist(store)
            return True
    except Exception:
        # Fail OPEN — never block a food log on a store IO error.
        return True


def seen(key: str) -> bool:
    """Convenience predicate. Returns True if the key has been recorded.
    Never raises."""
    try:
        return key in _load()
    except Exception:
        return False


def remember(key: str) -> None:
    """Convenience recorder. Never raises."""
    try:
        if not key:
            return
        with _lock_for(_STORE_PATH):
            store = _load()
            store[key] = _now()
            _evict_oldest(store)
            _persist(store)
    except Exception:
        pass
