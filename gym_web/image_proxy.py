"""
gym_web/image_proxy.py

Replacement endpoints for the file-cache-based image serving in gym_web.py.

Removes:
    GET /scan_img/<path:filename>     # served from /home/work/.hermes/scan_cache/
    GET /scan_thumb/<path:filename>   # served from same dir, generated on miss

Adds:
    GET /scan_img/<int:row_index>     # proxied (or redirected) from Google Drive
    GET /scan_thumb/<int:row_index>   # 480px max-side JPEG, LRU-cached in memory

The Sheet row layout is unchanged: column K (index 10) holds the Drive URL.
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import logging
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Optional

from flask import Blueprint, Response, redirect, request
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

THUMB_MAX_SIDE = 480
THUMB_JPEG_QUALITY = 82
# v3.3.23al: bound thumb LRU at 100 rows. With ~50KB/thumb, 100 = 5MB
# total — fits comfortably in memory even on small VPS instances.
THUMB_CACHE_MAX = 100
DRIVE_FETCH_TIMEOUT = 8                    # seconds
USER_AGENT = "gymbro-image-proxy/1.0"
DRIVE_PUBLIC_HOST = "drive.google.com"
# v3.3.23al: cap Drive URL cache too. Previously unbounded dict —
# could grow to thousands of entries on long-running instances, leaking
# memory. LRU bound 600 (~3 weeks of daily meals) is plenty.
URL_CACHE_MAX = 600

# ---------------------------------------------------------------------------
# Per-row Drive-URL cache (read-through, LRU-bounded).
# /api/scan_recent populates this after each scan so the image endpoints
# don't re-fetch the Sheet for every image render.
# v3.3.23al: bounded OrderedDict — drops oldest entries past URL_CACHE_MAX.
# ---------------------------------------------------------------------------

_url_cache: "OrderedDict[int, Optional[str]]" = OrderedDict()
_url_cache_lock = threading.Lock()

# v3.3.36 (2026-10-22, Jim OOB「昨日啲餐嘅縮圖全部一樣(錯)」):
# row → Drive URL **會變** —— 重新上載 / 換圖 / 清理會令舊 Drive 檔被
# trash,而 Sheet column K 已經指向新檔。thumb bytes 快取淨係記 row_index,
# 唔記「呢啲 bytes 係由邊個 URL 生成」→ 舊 bytes 會喺新 URL 下繼續派出去。
# 而 Drive 對住 trashed 檔會回同一張「已掉進垃圾桶」placeholder
# → 幾張縮圖一模一樣(2026-10-21 實測:row 663/664/665 三張 byte 级一樣)。
_thumb_src: "OrderedDict[int, str]" = OrderedDict()
_thumb_src_lock = threading.Lock()


def _thumb_src_get(row_index: int) -> Optional[str]:
    with _thumb_src_lock:
        return _thumb_src.get(row_index)


def _thumb_src_put(row_index: int, url: str) -> None:
    with _thumb_src_lock:
        if row_index in _thumb_src:
            _thumb_src.move_to_end(row_index)
        _thumb_src[row_index] = url
        while len(_thumb_src) > THUMB_CACHE_MAX:
            _thumb_src.popitem(last=False)


def clear_url_cache() -> None:
    """v3.3.36: drop every cached row → Drive URL mapping.

    Called when the Nutrition cache re-hydrates from the Sheet: a fresh
    snapshot may have different row indices or a different column K, so
    the old mapping must not survive it.
    """
    with _url_cache_lock:
        _url_cache.clear()


def remember_drive_url(row_index: int, drive_url: Optional[str]) -> None:
    """Called by /api/scan_recent (or any writer) to seed the URL cache."""
    with _url_cache_lock:
        if row_index in _url_cache:
            _url_cache.move_to_end(row_index)
        _url_cache[row_index] = drive_url
        # Enforce LRU bound
        while len(_url_cache) > URL_CACHE_MAX:
            _url_cache.popitem(last=False)


def get_drive_url(row_index: int) -> Optional[str]:
    with _url_cache_lock:
        if row_index in _url_cache:
            _url_cache.move_to_end(row_index)
            return _url_cache[row_index]
        return None


def invalidate_drive_url(row_index: int) -> None:
    with _url_cache_lock:
        _url_cache.pop(row_index, None)


def invalidate_thumb(row_index: int) -> None:
    """Drop the cached thumbnail bytes for one row."""
    with _thumb_cache._lock:
        _thumb_cache._data.pop(row_index, None)
    with _thumb_src_lock:
        _thumb_src.pop(row_index, None)


def shift_thumb_cache(row_index: int) -> None:
    """After deleting Sheet row R, every row > R has its row_index shifted
    down by 1. The thumbnail-bytes LRU cache is keyed by row_index, so its
    entries must move down by 1 as well — otherwise /scan_thumb/<new_idx>
    would return a JPEG that originally belonged to a different row.
    """
    # v3.3.36: the src-URL map is keyed by row_index too → must shift with
    # the bytes cache, but INDEPENDENTLY of it (the bytes LRU may have been
    # evicted while the src map still holds entries).
    with _thumb_src_lock:
        new_src = OrderedDict()
        for k, v in _thumb_src.items():
            if k > row_index:
                new_src[k - 1] = v
            elif k != row_index:           # k < row_index stays; row_index itself is dropped
                new_src[k] = v
        while len(new_src) > THUMB_CACHE_MAX:
            new_src.popitem(last=False)
        _thumb_src.clear()
        _thumb_src.update(new_src)

    with _thumb_cache._lock:
        if not _thumb_cache._data:
            return
        # Snapshot and rebuild — _BoundedBytesCache is OrderedDict-backed.
        new_data = OrderedDict()
        for k, v in _thumb_cache._data.items():
            if k > row_index:
                new_data[k - 1] = v
            elif k != row_index:           # k < row_index stays; row_index itself is dropped
                new_data[k] = v
        # Cap to capacity if shift added many entries
        while len(new_data) > _thumb_cache._cap:
            new_data.popitem(last=False)
        _thumb_cache._data = new_data


# ---------------------------------------------------------------------------
# Bounded LRU thumbnail cache (bytes, not PIL objects — JPEG is ready to serve)
# ---------------------------------------------------------------------------

class _BoundedBytesCache:
    def __init__(self, capacity: int) -> None:
        self._cap = capacity
        self._data: "OrderedDict[int, bytes]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: int) -> Optional[bytes]:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def put(self, key: int, value: bytes) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self._data[key] = value
            else:
                self._data[key] = value
                if len(self._data) > self._cap:
                    self._data.popitem(last=False)    # FIFO evict oldest

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_thumb_cache = _BoundedBytesCache(THUMB_CACHE_MAX)


# v3.3.23al: MODULE-LEVEL shared ThreadPoolExecutor for Drive fetches.
# Previously each scan_thumb call created its own ThreadPoolExecutor
# with max_workers=1, which is effectively serial. With 8 parallel
# JPG render calls each waiting 8s for Drive timeout, total = 48s
# (misleading — looks like "sequential scan_thumb calls"). Sharing
# one pool with 8 workers means all 8 Drive fetches run in parallel
# and complete in ~2s even with cold cache.
_DRIVE_FETCH_WORKERS = 8
_DRIVE_FETCH_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=_DRIVE_FETCH_WORKERS,
    thread_name_prefix="drive-fetch",
)


# ------------------------------------------------------------------
# Bounded LRU JPG-card cache (keyed by date string)
# v3.3.23al (Jim OOB 2026-09-27 'Pls fix all of them. Not just quick
# win'): disk cache alone isn't enough — /api/nutrition/card still
# runs PIL even when the file exists (then send_file reads from disk).
# Adding in-memory LRU so the second+ request in the same session
# is served from RAM (~10ms vs 150ms). Capacity 14 ≈ ~2 weeks of
# cached JPGs (each ~200KB → ~3MB total).
# ------------------------------------------------------------------

JPG_CACHE_MAX = 14
_jpg_cache: "OrderedDict[str, bytes]" = OrderedDict()
_jpg_cache_lock = threading.Lock()


def _jpg_cache_get(date_str: str) -> "Optional[bytes]":
    with _jpg_cache_lock:
        if date_str not in _jpg_cache:
            return None
        _jpg_cache.move_to_end(date_str)
        return _jpg_cache[date_str]


def _jpg_cache_put(date_str: str, jpg_bytes: bytes) -> None:
    with _jpg_cache_lock:
        if date_str in _jpg_cache:
            _jpg_cache.move_to_end(date_str)
            _jpg_cache[date_str] = jpg_bytes
        else:
            _jpg_cache[date_str] = jpg_bytes
            if len(_jpg_cache) > JPG_CACHE_MAX:
                _jpg_cache.popitem(last=False)


def _jpg_cache_invalidate(date_str: str) -> None:
    """v3.3.23al: invalidate on scan_edit/correct/rename/delete so
    stale JPGs don't show after edits. Called by api_scan_commit etc.

    v3.3.23av (Jim OOB 2026-10-12 'the edited 兩個鮮竹卷 is not updated
    in jpg download'): also clear multi-page variants (_p1, _p2, ...).
    api_nutrition_card stores per-page entries as 'date_p1', 'date_p2' so
    we must purge all of them, not just the base date key.
    """
    with _jpg_cache_lock:
        _jpg_cache.pop(date_str, None)
        # Also drop any per-page variants (date_p1, date_p2, ...)
        stale = [k for k in list(_jpg_cache.keys()) if k.startswith(f"{date_str}_p")]
        for k in stale:
            _jpg_cache.pop(k, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _DriveFetchError(Exception):
    pass


# ---------------------------------------------------------------------------
# v3.3.38 (2026-10-22, Jim OOB「The thumbnail are the same again. Pls fix the
# thumbnail and the systemic problem」)—— 系統性修法兩件套:
#
# (A) 內容定址 cache header
#     /scan_thumb/<row>?v=<hash> 係「內容定址」URL:同一個 v = 同一張相。
#     → 可以永久快取(public, max-age=31536000, immutable),客戶端唔會再
#       因為本地快取而顯示舊圖。
#     冇 ?v= 嘅舊 URL → 唔可以快取(no-store),因為 row_index 對應嘅相會變。
#
# (B) trashed 守衛
#     Drive 對住已被 trash 嘅檔會回**同一張**「已掉進垃圾桶」placeholder JPEG。
#     只要多行指向已被 trash 嘅檔 → 啲縮圖 byte 级一模一樣(2026-10-21 實測)。
#     → 派圖之前先查 trashed 狀態,trashed 就回 404(唔派錯圖)。
# ---------------------------------------------------------------------------

IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
NO_STORE_CACHE_CONTROL = "no-store, no-cache, must-revalidate, max-age=0"

# trashed 狀態快取:file_id → (is_trashed, fetched_at_epoch)
_TRASHED_TTL_S = 1800
_trashed_cache: "OrderedDict[str, tuple]" = OrderedDict()
_trashed_lock = threading.Lock()


def _file_id_from_drive_url(drive_url: str) -> str:
    """Extract the bare Drive file id from any of the URL shapes we store."""
    if not drive_url:
        return ""
    if "id=" in drive_url:
        return drive_url.split("id=", 1)[1].split("&", 1)[0].strip()
    if "/d/" in drive_url:
        return drive_url.split("/d/", 1)[1].split("/", 1)[0].strip()
    return ""


def _drive_file_trashed(drive_url: str) -> Optional[bool]:
    """Is the Drive file behind this URL already in the trash?

    Cached (30 min TTL) so a thumbnail render costs one metadata call, not
    one per request. Returns None when we can't tell (API/token error) —
    callers must fail OPEN (serve the image) in that case.
    """
    import time as _time
    fid = _file_id_from_drive_url(drive_url)
    if not fid:
        return None
    now = _time.time()
    with _trashed_lock:
        hit = _trashed_cache.get(fid)
        if hit and (now - hit[1]) < _TRASHED_TTL_S:
            _trashed_cache.move_to_end(fid)
            return hit[0]
    try:
        from gym_web.core import drive_file_is_trashed   # local import avoids cycles
        state = drive_file_is_trashed(fid)
    except Exception as e:
        log.warning("trashed check failed id=%s: %s", fid, e)
        return None
    if state is None:
        return None            # unknown → fail open, don't cache
    with _trashed_lock:
        _trashed_cache[fid] = (state, now)
        _trashed_cache.move_to_end(fid)
        while len(_trashed_cache) > 500:
            _trashed_cache.popitem(last=False)
    return state


def _img_headers(content_addressed: bool) -> dict:
    """Cache headers for image responses (v3.3.38)."""
    if content_addressed:
        return {"Cache-Control": IMMUTABLE_CACHE_CONTROL, "X-Gymbro-Immutable": "1"}
    return {"Cache-Control": NO_STORE_CACHE_CONTROL}


def _fetch_drive_bytes(drive_url: str) -> bytes:
    """GET the Drive image. Raises _DriveFetchError on failure."""
    req = urllib.request.Request(drive_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=DRIVE_FETCH_TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise _DriveFetchError(f"{type(e).__name__}: {e}") from e


def _make_thumbnail(raw: bytes) -> bytes:
    """PIL downscale to max-side 480px, JPEG q=82. Re-encode always.

    v3.3.7 (Jim OOB 2026-08-19 'last two photos rotated'): apply EXIF
    orientation so portrait iPhone photos render right-side up. Without
    this, a 1600x2133 portrait photo with EXIF=6 gets thumbnailed to
    480x360 with the pixels still in the rotated raw state — display
    shows it 90° off.
    """
    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        im.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=THUMB_JPEG_QUALITY, optimize=True)
        return buf.getvalue()


def _nutrition_drive_url(row_index: int) -> Optional[str]:
    """Row → Drive URL from the in-memory NutritionCache.

    v3.3.36: 之前 `_resolve_drive_url` 只信自己個 URL 快取,而嗰個快取**唔會**
    跟住 Sheet column K 嘅變化(重新上載 / 換圖 → 舊 Drive 檔被 trash)。
    NutritionCache 係由 background refresher 定期 re-hydrate 嘅,先佢新鮮。

    2026-10-21 實測故障:row 663/664/665 嘅 Sheet column K 已經指向
    21:02-21:46 上載嘅新相(未 trashed),但 URL 快取仲係 17:04-17:09 嗰批
    已被 trash 嘅舊 id → /scan_thumb 派 Drive 嘅「已掉進垃圾桶」placeholder
    → 三張縮圖 byte 级一模一樣。
    """
    try:
        from gym_web.cache import get_cache      # local import avoids cycles
        row = get_cache().get_by_row(row_index)
        if row is None:
            return None
        u = (row.drive_image_url or "").strip()
        return u or None
    except Exception as e:   # never break image serving on a cache hiccup
        log.warning("nutrition drive-url lookup failed row=%s: %s", row_index, e)
        return None


def _resolve_drive_url(row_index: int) -> Optional[str]:
    """v3.3.36: freshest source first.

    1. NutritionCache (in-memory, re-hydrated from the Sheet on a timer) —
       this is the truth the app's own JSON is showing.
    2. The per-row URL LRU (fast path, kept in sync with #1).
    3. A direct single-row Sheet read (last resort).
    """
    url = _nutrition_drive_url(row_index)
    if url:
        remember_drive_url(row_index, url)   # keep the LRU in sync
        return url
    url = get_drive_url(row_index)
    if url is not None:
        return url or None
    # Lazy backfill: ask the Sheet once if we missed the cache.
    from gym_web.core import get_row_drive_url   # local import avoids cycles
    url = get_row_drive_url(row_index)
    remember_drive_url(row_index, url)
    return url


# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------

bp = Blueprint("scan_image_proxy", __name__)


@bp.route("/scan_img/<int:row_index>")
def scan_img(row_index: int) -> Response:
    drive_url = _resolve_drive_url(row_index)
    if not drive_url:
        return Response(b"", status=404)

    # v3.3.38: trashed 檔唔可以派(會派 Drive 嘅「垃圾桶」placeholder)。
    if _drive_file_trashed(drive_url) is True:
        log.info("scan_img row=%s: drive file trashed — 404 (唔派 placeholder)", row_index)
        return Response(b"", status=404, headers=_img_headers(False))

    # v3.3.38: 內容定址 URL → immutable;舊 URL → no-store
    headers = _img_headers(bool(request.args.get("v")))

    # Default: 302 redirect. Cheap, masks Drive URL only via Referer.
    # Switch to proxy by setting GYMBRO_PROXY_IMAGES=1 in env.
    import os
    if os.environ.get("GYMBRO_PROXY_IMAGES") == "1":
        try:
            data = _fetch_drive_bytes(drive_url)
        except _DriveFetchError as e:
            log.warning("drive fetch failed row=%s: %s", row_index, e)
            return Response(b"upstream fetch failed", status=502)
        return Response(
            data,
            mimetype="image/jpeg",
            headers=headers,
        )

    resp = redirect(drive_url, code=302)
    for k, v in headers.items():
        resp.headers[k] = v
    return resp


def thumb_bytes(row_index: int) -> Optional[bytes]:
    """Same as scan_thumb() but returns raw JPEG bytes and needs NO request
    context.

    v3.3.62 (2026-10-28): `scan_thumb()` 讀 `request.args`(v3.3.38 內容定址)
    → 喺 ThreadPoolExecutor worker thread 入面行會 raise
    "Working outside of request context" → PDF 嘅相片全部畫唔到。
    `copy_current_request_context` 都救唔到(Flask 3 contextvar 跨 Context)。

    呢個版本淨係做「攞 bytes」,唔掂 request → 邊度都行得。
    """
    drive_url = _resolve_drive_url(row_index)
    if not drive_url:
        return None
    cached = _thumb_cache.get(row_index)
    if cached is not None and _thumb_src_get(row_index) == drive_url:
        return cached
    if _drive_file_trashed(drive_url) is True:
        log.info("thumb_bytes row=%s: drive file trashed — skip", row_index)
        return None
    try:
        raw = _DRIVE_FETCH_POOL.submit(_fetch_drive_bytes, drive_url).result(timeout=8)
        thumb = _make_thumbnail(raw)
    except concurrent.futures.TimeoutError:
        log.warning("drive fetch timeout (thumb_bytes) row=%s", row_index)
        return None
    except Exception as e:
        log.warning("drive fetch failed (thumb_bytes) row=%s: %s", row_index, e)
        return None
    _thumb_cache.put(row_index, thumb)
    _thumb_src_put(row_index, drive_url)
    return thumb


@bp.route("/scan_thumb/<int:row_index>")
def scan_thumb(row_index: int) -> Response:
    # v3.3.36: 先解 Drive URL,再決定用唔用 cache —— 同一個 row_index 嘅
    # URL 會變(重新上載/換圖),舊 bytes 唔可以照派。
    drive_url = _resolve_drive_url(row_index)
    if not drive_url:
        return Response(b"", status=404)

    # v3.3.38: 內容定址 URL → immutable;舊 URL → no-store
    headers = _img_headers(bool(request.args.get("v")))

    cached = _thumb_cache.get(row_index)
    if cached is not None and _thumb_src_get(row_index) == drive_url:
        return Response(
            cached,
            mimetype="image/jpeg",
            headers=headers,
        )
    if cached is not None:
        log.info("thumb cache stale for row=%s (url changed) — regenerating", row_index)

    # v3.3.38: trashed 檔唔可以派(Drive 會回「垃圾桶」placeholder → 啲縮圖
    # 全部一模一樣)。fail open:查唔到就照派。
    if _drive_file_trashed(drive_url) is True:
        log.info("scan_thumb row=%s: drive file trashed — 404 (唔派 placeholder)", row_index)
        return Response(b"", status=404, headers=headers)

    # v3.3.23al (Jim OOB 2026-09-27 'Pls fix all of them. Not just quick
    # win'): parallel Drive fetch with timeout. Multiple scan_thumb
    # calls (e.g. JPG card rendering 8 meals in parallel) used to be
    # blocked behind this serial network call. Each fetch can be 1-2s
    # on cold cache, multiplied by 8 meals = 8-16s. With concurrent
    # futures, all 8 fetch in parallel and complete in 1-2s total.
    #
    # IMPORTANT: use a MODULE-LEVEL ThreadPoolExecutor (not per-call!)
    # because nested ThreadPoolExecutor instances are serial — each
    # outer worker waits for its own single-worker inner pool. This
    # caused 50-second renders when 8 meals each had to wait 8s for
    # Drive timeout. Sharing the module pool fixes this.
    import concurrent.futures as _cf_scan
    try:
        raw = _DRIVE_FETCH_POOL.submit(_fetch_drive_bytes, drive_url).result(timeout=8)
        thumb = _make_thumbnail(raw)
    except concurrent.futures.TimeoutError:
        log.warning("drive fetch timeout (thumb) row=%s", row_index)
        return Response(b"drive fetch timeout", status=504)
    except _DriveFetchError as e:
        log.warning("drive fetch failed (thumb) row=%s: %s", row_index, e)
        return Response(b"upstream fetch failed", status=502)
    except Exception as e:
        log.exception("thumb gen failed row=%s: %s", row_index, e)
        return Response(b"thumb generation failed", status=502)

    _thumb_cache.put(row_index, thumb)
    _thumb_src_put(row_index, drive_url)
    return Response(
        thumb,
        mimetype="image/jpeg",
        headers=headers,
    )
