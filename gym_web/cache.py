"""gym_web/cache.py — in-memory cache for food/nutrition entries.

Architecture (v3.3.0):
  - Google Sheet (id ...Oag, tab "Nutrition", cols A:K) is the SINGLE source of
    truth. The Sheet writer (`gym_web._append_to_sheet_nutrition`) and the
    hydration reader (`gym_web._read_sheet_nutrition`) live in gym_web.py; this
    module is a pure in-memory layer on top.
  - On startup, the cache is EMPTY. `hydrate(access_token)` pulls all rows from
    the Sheet and builds indexes; PWA requests that arrive before hydration
    completes should 503 (caller checks `is_ready()`).
  - Reads: O(1) for /api/scan_recent (slice of by_recent), /api/food_history
    (by_date[date]), and /api/scan_get/<id> (by_id[id]).
  - Writes: Sheet-first via the injected `sheet_writer`. On success the new
    row is inserted into all indexes. `refresh_one(row_index)` runs after each
    write as a defensive re-fetch (the Sheet write may reorder rows).
  - Refresh: `start_background_refresh(period_s)` spawns a daemon thread that
    calls `refresh_full()` every N seconds (default 60s). Manual calls to
    `refresh_full()` / `refresh_one(i)` are also exposed for /api endpoints.
  - Thread-safe: a single `threading.RLock` guards all index mutations and
    iteration. Reads hold the lock briefly; writes hold it for the duration
    of index updates only — Sheet I/O happens OUTSIDE the lock.

NO file persistence — restart = empty cache until hydration finishes.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from .core import NUTRITION_SHEET_ID, NUTRITION_TAB_NAME, now_hkt

# ---------- Constants ----------
SHEET_ID = NUTRITION_SHEET_ID
TAB = NUTRITION_TAB_NAME
RANGE_ALL = f"{TAB}!A1:K1000"  # 11 cols A-K; 1000-row soft cap
# v3.3.4: empty string, not a placeholder URL. The PWA template uses
# `<template x-if="scan.image_url">` (line 862 of templates/index.html)
# to skip the <img> element entirely when there's no image — Jim OOB
# 2026-08-07 14:15 HKT 'for those without image, don't show any image'.
# Returning a non-existent /static/img/placeholder_food.png was the
# cause of the "many broken image" report on 2026-08-15.
PLACEHOLDER_IMG = ""
DEFAULT_REFRESH_S = 60

# ---------- Sheet column indices (0-based within a row list) ----------
COL_DATE, COL_TIME, COL_MEAL, COL_NAME, COL_RESTAURANT = 0, 1, 2, 3, 4
COL_KCAL, COL_P, COL_C, COL_F, COL_NOTES, COL_IMG = 5, 6, 7, 8, 9, 10
EXPECTED_COLS = 11

# ---------- Row dataclass ----------
@dataclass
class NutritionRow:
    """One nutrition entry, mirroring Sheet row + PWA-friendly derived fields."""
    row_index: int            # 1-based sheet row index (header = 1, first data = 2)
    entry_id: str             # stable id = f"row-{row_index}" (sheet rows never recycle)
    date: str                 # "YYYY-MM-DD"
    time: str                 # "HH:MM"
    meal: str                 # 早/午/晚/宵夜/snack...
    name: str                 # dish name (餐名)
    restaurant: str           # 餐廳/連鎖
    kcal: float
    p: float                  # protein g
    c: float                  # carbs g
    f: float                  # fat g
    notes: str
    drive_image_url: str      # raw column K (may be "")

    # ---- derived ----
    dt: datetime = field(default=None)            # parsed date+time, for sorting
    image_url_for_pwa: str = ""                  # K or placeholder
    thumbnail_url: str = ""                      # = image_url_for_pwa (Drive thumbs via =s220)
    _coach_comment: Optional[dict] = field(default=None, repr=False)

    @classmethod
    def from_sheet_row(cls, row_index: int, cells: list[str]) -> "NutritionRow":
        """cells = list of 11 strings (A..K). Defensive: missing/short → ""."""
        cells = (cells + [""] * EXPECTED_COLS)[:EXPECTED_COLS]
        date = cells[COL_DATE].strip()
        time = cells[COL_TIME].strip()
        try:
            dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        except ValueError:
            dt = None
        try:
            kcal = float(cells[COL_KCAL] or 0)
        except ValueError:
            kcal = 0.0
        try:
            p = float(cells[COL_P] or 0)
        except ValueError:
            p = 0.0
        try:
            c = float(cells[COL_C] or 0)
        except ValueError:
            c = 0.0
        try:
            f = float(cells[COL_F] or 0)
        except ValueError:
            f = 0.0
        drive = cells[COL_IMG].strip()
        if drive:
            # Drive public URL is e.g. 'https://drive.google.com/uc?export=view&id=<FILE_ID>'
            # v3.3.4: switch back to drive.google direct URLs. The lh3
            # thumbnail proxy (lh3.googleusercontent.com/d/<id>=s480) returned
            # 404 for tiny Drive files (e.g. 555-byte stub JPEGs uploaded when
            # the AI scan produced a placeholder instead of a real photo) —
            # Google's image CDN rejects non-image files at any size param.
            # drive.google.com is reliable for everything, even tiny uploads.
            # Build canonical drive.google URLs for image + thumb (same URL
            # since Drive doesn't have separate thumbnail caching).
            file_id = ""
            if "id=" in drive:
                file_id = drive.split("id=", 1)[1].split("&", 1)[0].split("#", 1)[0]
            elif "/d/" in drive:
                # legacy shape like https://drive.google.com/file/d/<id>/view
                file_id = drive.split("/d/", 1)[1].split("/", 1)[0]
            if file_id:
                canonical = f"https://drive.google.com/uc?export=view&id={file_id}"
                img_for_pwa = canonical
                thumb = canonical
            else:
                img_for_pwa = drive
                thumb = drive
        else:
            # v3.3.4: empty (no broken /static/img/placeholder_food.png URL).
            # Template `<template x-if="scan.image_url">` skips the <img>.
            img_for_pwa = PLACEHOLDER_IMG
            thumb = PLACEHOLDER_IMG
        return cls(
            row_index=row_index,
            entry_id=f"row-{row_index}",
            date=date, time=time,
            meal=cells[COL_MEAL].strip(),
            name=cells[COL_NAME].strip(),
            restaurant=cells[COL_RESTAURANT].strip(),
            kcal=kcal, p=p, c=c, f=f,
            notes=cells[COL_NOTES].strip(),
            drive_image_url=drive,
            dt=dt,
            image_url_for_pwa=img_for_pwa,
            thumbnail_url=thumb,
        )

    def to_pwa_dict(self) -> dict:
        """Shape consumed by /api/scan_recent and /api/food_history.

        v3.3.0: PWA template expects the historical scan_log fields
        (image_url, thumbnail_url, scan_index, timestamp_iso, etc.). Fields
        no longer persisted after the file-cache removal (coach_comment,
        vision_short, user_corrections) are returned with empty defaults
        — the template's `<template x-if=...>` guards handle missing data.

        v3.3.4: image_url + thumbnail_url point to LOCAL proxy routes
        (/scan_img/<row> + /scan_thumb/<row>) instead of drive.google.com
        direct URLs. Drive was 403-ing ~15% of files when the browser
        fired many parallel <img> requests (anti-bot rate limit). The
        local proxy fetches once, caches in-memory LRU, and serves from
        the same origin — no per-request Drive round-trip.
        """
        ts_iso = self.dt.isoformat() if self.dt else ""
        if self.dt:
            # Sheet rows are HKT — append +08:00 for ISO compatibility
            ts_iso = f"{ts_iso}+08:00" if "+" not in ts_iso else ts_iso
        return {
            # Identity
            "entry_id": self.entry_id,
            "scan_index": self.row_index,
            "row_index": self.row_index,
            # Core fields (Sheet)
            "date": self.date,
            "time": self.time,
            "time_label": self.time,             # alias used by some templates
            "timestamp_iso": ts_iso,
            "meal": self.meal,
            "meal_type": self.meal,              # legacy alias
            "name": self.name,
            "meal_name": self.name,              # legacy alias
            "restaurant": self.restaurant,
            "restaurant_chain": self.restaurant, # legacy alias
            # Macros
            "calories": self.kcal,
            "kcal": self.kcal,
            "protein": self.p,
            "carbs": self.c,
            "fat": self.f,
            # 12-field extras (Sheet doesn't persist micros — zero defaults)
            "fiber": 0.0,
            "sugar": 0.0,
            "sodium": 0.0,
            "sat_fat": 0.0,
            "trans_fat": 0.0,
            "vit_c": 0.0,
            "iron": 0.0,
            "calcium": 0.0,
            # Notes
            "notes": self.notes,
            "note": self.notes,                  # alias
            # v3.3.4: ALL images route through the local /scan_thumb/ proxy.
            # /scan_img/ would 302-redirect to drive.google.com directly,
            # which 403's ~15% of files when many parallel <img> requests
            # hit Drive's anti-bot rate limit. /scan_thumb/ stays in-LRU,
            # proxies bytes via image_proxy.bp, never redirects, browser
            # sees only localhost. The /scan_img/ URL is preserved for
            # the full-resolution tap-to-open modal (still via 302).
            "image_url": self.image_url_for_pwa if not self.drive_image_url
                        else f"/scan_thumb/{self.row_index}",
            "thumbnail_url": self.thumbnail_url if not self.drive_image_url
                             else f"/scan_thumb/{self.row_index}",
            "drive_image_url": self.drive_image_url,
            "image_path": "",                    # local path no longer used
            "is_text_only": not bool(self.drive_image_url),
            # Dropped after file-cache removal — empty defaults keep PWA happy
            "coach_comment": {},                 # populated by get_or_compute_coach_comment
            "vision_short": "",
            "user_corrections": [],
            "shared": False,
            "is_shared_meal": False,
        }

    def get_or_compute_coach_comment(self, coach_fn: Callable[["NutritionRow"], dict]) -> dict:
        """Option (b): cache per-row after first compute. Thread-safe via cache lock."""
        if self._coach_comment is None:
            self._coach_comment = coach_fn(self)
        return self._coach_comment


# ---------- Cache ----------
class NutritionCache:
    """Thread-safe in-memory cache. One instance per process (Flask module-global)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._hydrated = False
        self._hydrating = False
        self._last_hydrated_at: Optional[datetime] = None
        # Indexes
        self.by_row: dict[int, NutritionRow] = {}        # sheet_row_index -> Row
        self.by_id: dict[str, int] = {}                  # entry_id -> row_index
        self.by_date: dict[str, list[int]] = {}          # "YYYY-MM-DD" -> [row_index]
        self.by_recent: list[int] = []                   # row_index, sorted date+time DESC
        # Background refresher handle
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()

    # ---- status ----
    def is_ready(self) -> bool:
        with self._lock:
            return self._hydrated

    def last_hydrated_at(self) -> Optional[str]:
        with self._lock:
            return self._last_hydrated_at.isoformat() if self._last_hydrated_at else None

    # ---- hydration ----
    def hydrate(self, fetch_all_rows: Callable[[], list[list[str]]]) -> int:
        """Idempotent. `fetch_all_rows` returns list of 11-col cell lists.
        Safe to call repeatedly (e.g. on Sheet schema drift). Returns rows loaded.
        """
        with self._lock:
            self._hydrating = True
        try:
            rows_raw = fetch_all_rows() or []  # Sheet I/O OUTSIDE lock
            with self._lock:
                self.by_row.clear()
                self.by_id.clear()
                self.by_date.clear()
                self.by_recent.clear()
                # First row is header — skip it (row_index 1 in Sheet)
                loaded = 0
                tmp_recent: list[tuple[datetime, int]] = []
                for offset, cells in enumerate(rows_raw):
                    if offset == 0:
                        continue
                    sheet_row = offset + 1  # 1-based, header is row 1
                    row = NutritionRow.from_sheet_row(sheet_row, cells)
                    if not row.date and not row.name:
                        continue  # skip blank rows
                    self.by_row[sheet_row] = row
                    self.by_id[row.entry_id] = sheet_row
                    self.by_date.setdefault(row.date, []).append(sheet_row)
                    if row.dt:
                        tmp_recent.append((row.dt, sheet_row))
                    elif row.date:
                        # v3.3.1: legacy rows have date but no time. Use
                        # midnight as the sort key so they still appear
                        # in by_recent (at the end of their date).
                        try:
                            tmp_recent.append((datetime.strptime(row.date, "%Y-%m-%d"), sheet_row))
                        except ValueError:
                            pass
                tmp_recent.sort(key=lambda t: t[0], reverse=True)
                self.by_recent = [ri for _, ri in tmp_recent]
                self._hydrated = True
                self._last_hydrated_at = now_hkt()
                loaded = len(self.by_row)
                return loaded
        finally:
            with self._lock:
                self._hydrating = False

    # ---- reads (O(1) for typical PWA queries) ----
    def get_recent(self, limit: int = 100) -> list[NutritionRow]:
        with self._lock:
            idxs = self.by_recent[:limit]
            return [self.by_row[i] for i in idxs]

    def get_by_date(self, date: str) -> list[NutritionRow]:
        with self._lock:
            idxs = list(self.by_date.get(date, []))
            # within a date, sort by time DESC
            idxs.sort(key=lambda i: self.by_row[i].time, reverse=True)
            return [self.by_row[i] for i in idxs]

    def get_by_id(self, entry_id: str) -> Optional[NutritionRow]:
        with self._lock:
            ri = self.by_id.get(entry_id)
            return self.by_row.get(ri) if ri is not None else None

    def get_by_row(self, row_index: int) -> Optional[NutritionRow]:
        with self._lock:
            return self.by_row.get(row_index)

    def find_by_signature(self, date: str, time_short: str, calories: int) -> Optional[NutritionRow]:
        """Find a row by (date, HH:MM, kcal) signature — replaces the legacy
        food_scan_log.json dedup logic. Returns the first matching row, or None.
        """
        target_cal = int(calories or 0)
        with self._lock:
            for ri in self.by_date.get(date, []):
                row = self.by_row.get(ri)
                if row is None:
                    continue
                if row.time[:5] != time_short[:5]:
                    continue
                if int(row.kcal) == target_cal:
                    return row
        return None

    def stats(self) -> dict:
        with self._lock:
            return {
                "rows": len(self.by_row),
                "dates": len(self.by_date),
                "hydrated": self._hydrated,
                "last_hydrated_at": self.last_hydrated_at(),
            }

    # ---- writes ----
    def commit(self, entry: dict, sheet_writer: Callable[[dict], dict]) -> dict:
        """Sheet-first write. `entry` is the PWA dict (date, time, meal, name,
        restaurant, kcal, p, c, f, notes, drive_image_url). `sheet_writer` MUST
        perform the Sheet append/update and return {"ok": True, "row_index": N}.
        On Sheet success, the new row is inserted into all indexes.
        Returns the same shape sheet_writer returns, plus "entry": Row dict.
        """
        sheet_result = sheet_writer(entry)  # I/O OUTSIDE lock
        if not sheet_result.get("ok"):
            return sheet_result
        row_index = int(sheet_result["row_index"])
        # Build a full cells list (11 cols) from entry to keep indexes consistent
        cells = [
            entry.get("date", ""), entry.get("time", ""),
            entry.get("meal", ""), entry.get("name", ""),
            entry.get("restaurant", ""),
            str(entry.get("kcal", 0)), str(entry.get("protein", entry.get("p", 0))),
            str(entry.get("carbs", entry.get("c", 0))), str(entry.get("fat", entry.get("f", 0))),
            entry.get("notes", ""), entry.get("drive_image_url", ""),
        ]
        with self._lock:
            # Remove old version of this row_index if any (e.g. update path)
            self._evict(row_index)
            row = NutritionRow.from_sheet_row(row_index, cells)
            self._insert(row)
        sheet_result["entry"] = row.to_pwa_dict()
        return sheet_result

    # ---- invalidation ----
    def insert_row(self, row: NutritionRow) -> None:
        """Index a row we just wrote to the Sheet (avoid the round-trip of
        refresh_one). Caller is responsible for Sheet-write success."""
        with self._lock:
            self._evict(row.row_index)
            if row.date or row.name:
                self._insert(row)
                self._hydrated = True
                self._last_hydrated_at = now_hkt()

    def refresh_one(self, row_index: int, fetch_row: Callable[[int], list[str]]) -> bool:
        """Re-fetch a single row from Sheet and patch indexes. Returns True on update."""
        cells = fetch_row(row_index)  # I/O OUTSIDE lock
        with self._lock:
            if not cells or len(cells) < 1:
                self._evict(row_index)
                return False
            self._evict(row_index)
            row = NutritionRow.from_sheet_row(row_index, cells)
            if row.date or row.name:
                self._insert(row)
                return True
            return False

    def refresh_full(self, fetch_all_rows: Callable[[], list[list[str]]]) -> int:
        """Full re-hydrate. ~5s for 500 rows."""
        return self.hydrate(fetch_all_rows)

    # ---- background refresher ----
    def start_background_refresh(self, fetch_all_rows: Callable, period_s: int = DEFAULT_REFRESH_S) -> None:
        if self._refresh_thread and self._refresh_thread.is_alive():
            return  # already running
        self._stop_flag.clear()

        def _loop():
            while not self._stop_flag.wait(period_s):
                try:
                    self.refresh_full(fetch_all_rows)
                except Exception:
                    # swallow — next tick will retry; cache stays valid
                    pass

        t = threading.Thread(target=_loop, name="nutrition-cache-refresher", daemon=True)
        t.start()
        self._refresh_thread = t

    def stop_background_refresh(self) -> None:
        self._stop_flag.set()

    # ---- internal helpers (assume lock held) ----
    def _insert(self, row: NutritionRow) -> None:
        self.by_row[row.row_index] = row
        self.by_id[row.entry_id] = row.row_index
        self.by_date.setdefault(row.date, []).append(row.row_index)
        # Insert in sorted position by dt DESC
        dt = row.dt
        if dt is None:
            self.by_recent.append(row.row_index)
            return
        ins = 0
        for i, ri in enumerate(self.by_recent):
            other = self.by_row[ri]
            if other.dt and other.dt > dt:
                ins = i + 1
            else:
                break
        self.by_recent.insert(ins, row.row_index)

    def _evict(self, row_index: int) -> None:
        old = self.by_row.pop(row_index, None)
        if old is None:
            return
        self.by_id.pop(old.entry_id, None)
        dlist = self.by_date.get(old.date)
        if dlist is not None:
            try:
                dlist.remove(row_index)
            except ValueError:
                pass
            if not dlist:
                self.by_date.pop(old.date, None)
        try:
            self.by_recent.remove(row_index)
        except ValueError:
            pass


# ---------- Module singleton ----------
_cache: Optional[NutritionCache] = None
_cache_lock = threading.Lock()


def get_cache() -> NutritionCache:
    """Lazy singleton. gym_web.py imports this; first call creates the cache."""
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = NutritionCache()
        return _cache
