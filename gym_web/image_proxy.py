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

import io
import json
import logging
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Optional

from flask import Blueprint, Response, redirect
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

THUMB_MAX_SIDE = 480
THUMB_JPEG_QUALITY = 82
THUMB_CACHE_MAX = 100                      # LRU bound
DRIVE_FETCH_TIMEOUT = 8                    # seconds
USER_AGENT = "gymbro-image-proxy/1.0"
DRIVE_PUBLIC_HOST = "drive.google.com"

# ---------------------------------------------------------------------------
# Per-row Drive-URL cache (read-through).
# /api/scan_recent populates this after each scan so the image endpoints
# don't re-fetch the Sheet for every image render.
# ---------------------------------------------------------------------------

_url_cache: "dict[int, Optional[str]]" = {}
_url_cache_lock = threading.Lock()


def remember_drive_url(row_index: int, drive_url: Optional[str]) -> None:
    """Called by /api/scan_recent (or any writer) to seed the URL cache."""
    with _url_cache_lock:
        _url_cache[row_index] = drive_url


def get_drive_url(row_index: int) -> Optional[str]:
    with _url_cache_lock:
        return _url_cache.get(row_index)


def invalidate_drive_url(row_index: int) -> None:
    with _url_cache_lock:
        _url_cache.pop(row_index, None)


def invalidate_thumb(row_index: int) -> None:
    """Drop the cached thumbnail bytes for one row."""
    with _thumb_cache._lock:
        _thumb_cache._data.pop(row_index, None)


def shift_thumb_cache(row_index: int) -> None:
    """After deleting Sheet row R, every row > R has its row_index shifted
    down by 1. The thumbnail-bytes LRU cache is keyed by row_index, so its
    entries must move down by 1 as well — otherwise /scan_thumb/<new_idx>
    would return a JPEG that originally belonged to a different row.
    """
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _DriveFetchError(Exception):
    pass


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


def _resolve_drive_url(row_index: int) -> Optional[str]:
    """Cache-first lookup of the Drive URL for a row."""
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
            headers={"Cache-Control": "public, max-age=86400"},
        )

    return redirect(drive_url, code=302)


@bp.route("/scan_thumb/<int:row_index>")
def scan_thumb(row_index: int) -> Response:
    cached = _thumb_cache.get(row_index)
    if cached is not None:
        return Response(
            cached,
            mimetype="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    drive_url = _resolve_drive_url(row_index)
    if not drive_url:
        return Response(b"", status=404)

    try:
        raw = _fetch_drive_bytes(drive_url)
        thumb = _make_thumbnail(raw)
    except _DriveFetchError as e:
        log.warning("drive fetch failed (thumb) row=%s: %s", row_index, e)
        return Response(b"upstream fetch failed", status=502)
    except Exception as e:
        log.exception("thumb gen failed row=%s: %s", row_index, e)
        return Response(b"thumb generation failed", status=502)

    _thumb_cache.put(row_index, thumb)
    return Response(
        thumb,
        mimetype="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
