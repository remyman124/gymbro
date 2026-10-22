"""v3.3.36 regression — /scan_thumb 唔可以派舊圖,更加唔可以全部一樣。

2026-10-22 實測故障 (Jim OOB「昨日啲餐嘅縮圖全部一樣(錯)」):
  - Sheet column K (row 663/664/665) 已經指向 21:02-21:46 上載嘅新相
    (scan_20261021_210255.jpg / scan_20261021_214520.jpg,trashed=False)
  - 但 image proxy 個 row → Drive URL LRU 仲指向 17:04-17:09 嗰批
    **已經被 trash** 嘅舊檔 (scan_20261021_170451.jpg …)
  - Drive 對住 trashed 檔會回同一張「已掉進垃圾桶」placeholder
    → 三張 /scan_thumb byte 级一模一樣 (md5 f0c72585af72,39911 bytes)

修法(3 層):
  1. _resolve_drive_url() 先問 NutritionCache(定期 re-hydrate,先新鮮)
  2. thumb bytes 快取要記住「由邊個 URL 生成」(src map);URL 變就重生
  3. NutritionCache hydrate() 之後清走 URL LRU
"""
from __future__ import annotations

import sys
from collections import OrderedDict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pytest  # noqa: E402

from gym_web import image_proxy  # noqa: E402

FRESH = "https://drive.google.com/uc?id=FRESH_NEW_FILE"
STALE = "https://drive.google.com/uc?id=STALE_TRASHED_FILE"


class _FakeRow:
    def __init__(self, drive_image_url: str):
        self.drive_image_url = drive_image_url


class _FakeCache:
    def __init__(self, rows: dict):
        self._rows = rows

    def get_by_row(self, row_index: int):
        return self._rows.get(row_index)


@pytest.fixture(autouse=True)
def _clean():
    image_proxy.clear_url_cache()
    image_proxy._thumb_cache.clear()
    with image_proxy._thumb_src_lock:
        image_proxy._thumb_src.clear()
    yield
    image_proxy.clear_url_cache()
    image_proxy._thumb_cache.clear()
    with image_proxy._thumb_src_lock:
        image_proxy._thumb_src.clear()


def _patch_lru(monkeypatch, mapping):
    """Force image_proxy's URL LRU to an exact mapping."""
    lru = OrderedDict(mapping)
    monkeypatch.setattr(image_proxy, "_url_cache", lru)
    monkeypatch.setattr(image_proxy, "get_drive_url",
                        lambda ri: lru.get(ri))
    return lru


def _patch_nutrition(monkeypatch, rows):
    import gym_web.cache as gcache
    monkeypatch.setattr(gcache, "get_cache", lambda: _FakeCache(rows))


# ---------------------------------------------------------------- 1. resolve

def test_resolve_prefers_nutrition_cache_over_stale_lru(monkeypatch):
    """核心 bug: LRU 指向已 trashed 嘅舊檔 → 一定要畀 NutritionCache 贏。"""
    _patch_lru(monkeypatch, {663: STALE})
    _patch_nutrition(monkeypatch, {663: _FakeRow(FRESH)})
    assert image_proxy._resolve_drive_url(663) == FRESH


def test_resolve_falls_back_to_lru_when_cache_has_no_row(monkeypatch):
    _patch_lru(monkeypatch, {663: STALE})
    _patch_nutrition(monkeypatch, {})
    assert image_proxy._resolve_drive_url(663) == STALE


def test_resolve_updates_lru_after_cache_read(monkeypatch):
    lru = _patch_lru(monkeypatch, {663: STALE})
    _patch_nutrition(monkeypatch, {663: _FakeRow(FRESH)})
    image_proxy._resolve_drive_url(663)
    assert lru[663] == FRESH


def test_nutrition_lookup_failure_does_not_raise(monkeypatch):
    """cache 出事唔可以炸 image serving(必須 fallback)。"""
    import gym_web.cache as gcache

    def _boom():
        raise RuntimeError("cache down")

    monkeypatch.setattr(gcache, "get_cache", _boom)
    _patch_lru(monkeypatch, {663: STALE})
    assert image_proxy._resolve_drive_url(663) == STALE


# ------------------------------------------------------------ 2. clear/shift

def test_clear_url_cache_empties_lru(monkeypatch):
    lru = _patch_lru(monkeypatch, {663: STALE, 664: FRESH})
    image_proxy.clear_url_cache()
    assert len(lru) == 0


def test_shift_thumb_cache_shifts_src_map():
    with image_proxy._thumb_src_lock:
        image_proxy._thumb_src.clear()
    image_proxy._thumb_src_put(664, FRESH)
    image_proxy._thumb_src_put(665, STALE)
    image_proxy.shift_thumb_cache(663)
    with image_proxy._thumb_src_lock:
        src = dict(image_proxy._thumb_src)
    assert src.get(663) == FRESH      # 664 → 663
    assert src.get(664) == STALE      # 665 → 664
    assert 665 not in src              # 663 自己被 drop


def test_invalidate_thumb_drops_src_too():
    image_proxy._thumb_src_put(663, FRESH)
    image_proxy.invalidate_thumb(663)
    assert image_proxy._thumb_src_get(663) is None


# ------------------------------------------------------------- 3. scan_thumb

def _flask_client():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(image_proxy.bp)
    return app.test_client()


def test_scan_thumb_regenerates_when_url_changed(monkeypatch):
    """舊 bytes 對應舊 URL → URL 變咗就唔可以再派舊 bytes。"""
    _patch_lru(monkeypatch, {663: FRESH})
    _patch_nutrition(monkeypatch, {663: _FakeRow(FRESH)})

    fetched = []

    def _fake_fetch(url):
        fetched.append(url)
        return b"RAW-NEW"

    monkeypatch.setattr(image_proxy, "_fetch_drive_bytes", _fake_fetch)
    monkeypatch.setattr(image_proxy, "_make_thumbnail", lambda raw: b"THUMB-NEW:" + raw)

    # 先用舊 URL 快取產生一份 bytes
    image_proxy._thumb_cache.put(663, b"THUMB-OLD")
    image_proxy._thumb_src_put(663, STALE)

    client = _flask_client()
    resp = client.get("/scan_thumb/663")
    assert resp.status_code == 200
    assert resp.data == b"THUMB-NEW:RAW-NEW"
    assert fetched == [FRESH]


def test_scan_thumb_serves_cache_when_url_unchanged(monkeypatch):
    _patch_lru(monkeypatch, {663: FRESH})
    _patch_nutrition(monkeypatch, {663: _FakeRow(FRESH)})

    def _boom(url):
        raise AssertionError("唔應該重新抓 Drive")

    monkeypatch.setattr(image_proxy, "_fetch_drive_bytes", _boom)

    image_proxy._thumb_cache.put(663, b"THUMB-CACHED")
    image_proxy._thumb_src_put(663, FRESH)

    client = _flask_client()
    resp = client.get("/scan_thumb/663")
    assert resp.status_code == 200
    assert resp.data == b"THUMB-CACHED"


def test_scan_thumb_404_when_no_url(monkeypatch):
    import gym_web.core as gcore
    _patch_lru(monkeypatch, {})
    _patch_nutrition(monkeypatch, {})
    monkeypatch.setattr(gcore, "get_row_drive_url", lambda ri: None)
    client = _flask_client()
    resp = client.get("/scan_thumb/663")
    assert resp.status_code == 404


# -------------------------------------------------------------- 4. hydration

def test_hydrate_clears_url_lru(monkeypatch):
    """re-hydrate 之後 URL LRU 唔可以再跨快照存活。"""
    import gym_web.cache as gcache

    lru = _patch_lru(monkeypatch, {663: STALE})
    calls = []
    monkeypatch.setattr(image_proxy, "clear_url_cache",
                        lambda: (calls.append(1), lru.clear())[1])

    cache = gcache.NutritionCache()
    rows = [
        ["日期", "時間", "餐別", "菜名", "餐廳/連鎖", "kcal", "蛋白質(g)",
         "碳水(g)", "脂肪(g)", "備註", "圖片"],
        ["2026-10-21", "20:29", "晚餐", "粉紅色雞尾酒", "", "180", "1", "10", "2", "", FRESH],
    ]
    loaded = cache.hydrate(lambda: rows)
    assert loaded == 1
    assert calls, "hydrate() 之後一定要清 URL LRU"


def test_hydrate_keeps_row_drive_url(monkeypatch):
    """hydrate 出嚟嘅 row 一定要帶住 column K(新圖片 id)。"""
    import gym_web.cache as gcache
    _patch_lru(monkeypatch, {})
    cache = gcache.NutritionCache()
    rows = [
        ["日期", "時間", "餐別", "菜名", "餐廳/連鎖", "kcal", "蛋白質(g)",
         "碳水(g)", "脂肪(g)", "備註", "圖片"],
        ["2026-10-21", "20:29", "晚餐", "粉紅色雞尾酒", "", "180", "1", "10", "2", "", FRESH],
    ]
    cache.hydrate(lambda: rows)
    row = cache.get_by_row(2)
    assert row is not None
    assert row.drive_image_url == FRESH
