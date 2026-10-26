"""v3.3.44 — Geolocation helpers (location log + best-effort reverse geocode).

The location log is a small local JSON store (``GYMBRO_GEO_STORE``, default
``/home/work/.hermes/gymbro_geo.json``) capped at 200 entries.

Reverse geocoding is best-effort only: when Nominatim is unreachable the
label is simply empty. A place name is NEVER invented — the app would rather
show nothing than show something wrong.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from gym_web.core import safe_write_json

GEO_STORE_ENV = "GYMBRO_GEO_STORE"
DEFAULT_GEO_STORE = "/home/work/.hermes/gymbro_geo.json"

MAX_LOCATIONS = 200
GEOCODE_TTL_S = 24 * 60 * 60
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
# Nominatim's usage policy requires an identifying User-Agent.
USER_AGENT = "gymbro-pwa/1.0 (jim@local)"

HKT = timezone(timedelta(hours=8))

_LOCK = threading.Lock()


def geo_store_path() -> Path:
    """Resolve the geo store path (env-overridable for tests)."""
    return Path(os.environ.get(GEO_STORE_ENV) or DEFAULT_GEO_STORE)


def _now_iso() -> str:
    return datetime.now(HKT).isoformat()


# ---------- Store ----------


def _read_store() -> dict:
    path = geo_store_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"locations": [], "geocode_cache": {}}
    except Exception as e:
        print(f"[geo._read_store] cannot read {path}: "
              f"{type(e).__name__}: {e}", flush=True)
        return {"locations": [], "geocode_cache": {}}
    if not isinstance(data, dict):
        return {"locations": [], "geocode_cache": {}}
    if not isinstance(data.get("locations"), list):
        data["locations"] = []
    if not isinstance(data.get("geocode_cache"), dict):
        data["geocode_cache"] = {}
    return data


def _write_store(store: dict) -> bool:
    path = geo_store_path()
    ok = safe_write_json(path, store)
    if not ok:
        print(f"[geo._write_store] atomic write failed for {path}", flush=True)
    return ok


# ---------- Distance ----------


def haversine_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Great-circle distance in kilometres."""
    radius_km = 6371.0088
    p1 = math.radians(float(a_lat))
    p2 = math.radians(float(b_lat))
    dphi = math.radians(float(b_lat) - float(a_lat))
    dlam = math.radians(float(b_lng) - float(a_lng))
    h = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2)
    return 2 * radius_km * math.asin(math.sqrt(min(1.0, h)))


# ---------- Reverse geocoding ----------


def _label_from(payload: dict) -> str:
    """Turn a Nominatim response into a short human label."""
    if not isinstance(payload, dict):
        return ""
    address = payload.get("address")
    if isinstance(address, dict):
        parts: list[str] = []
        for key in ("name", "road", "neighbourhood", "suburb",
                    "city", "town", "village", "hamlet"):
            value = address.get(key)
            if isinstance(value, str) and value and value not in parts:
                parts.append(value)
        if parts:
            return " · ".join(parts[:3])
    display = payload.get("display_name")
    if isinstance(display, str) and display.strip():
        chunks = [c.strip() for c in display.split(",") if c.strip()]
        return " · ".join(chunks[:2])
    return ""


def reverse_geocode(lat: float, lng: float) -> str:
    """Best-effort place name for a coordinate. "" when unknown.

    Never fabricates: a failed lookup returns an empty string.
    """
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return ""
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return ""

    key = f"{lat:.5f},{lng:.5f}"
    with _LOCK:
        store = _read_store()
        cache = store["geocode_cache"]
        hit = cache.get(key)
        if isinstance(hit, dict) and hit.get("label"):
            age = time.time() - float(hit.get("ts") or 0)
            if age < GEOCODE_TTL_S:
                return str(hit["label"])

    query = urllib.parse.urlencode({
        "format": "jsonv2",
        "lat": f"{lat:.6f}",
        "lon": f"{lng:.6f}",
        "zoom": "18",
        "addressdetails": "1",
    })
    request = urllib.request.Request(
        f"{NOMINATIM_URL}?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[geo.reverse_geocode] lookup failed for {key}: "
              f"{type(e).__name__}: {e}", flush=True)
        return ""

    label = _label_from(payload)
    if not label:
        return ""

    with _LOCK:
        store = _read_store()
        store["geocode_cache"][key] = {"label": label, "ts": int(time.time())}
        _write_store(store)
    return label


# ---------- Location log ----------


def log_location(lat: float, lng: float, accuracy=None, context: str = "manual",
                 label: str = "") -> dict:
    """Append a location to the log. Raises ValueError on bad coords."""
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError) as e:
        raise ValueError("bad coords") from e
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        raise ValueError("bad coords")

    acc = None
    if accuracy is not None:
        try:
            acc = float(accuracy)
        except (TypeError, ValueError):
            acc = None

    record = {
        "ts": _now_iso(),
        "lat": lat,
        "lng": lng,
        "accuracy": acc,
        "context": str(context or "manual")[:32],
        "label": str(label or "")[:200],
    }
    with _LOCK:
        store = _read_store()
        locations = store["locations"]
        locations.append(record)
        if len(locations) > MAX_LOCATIONS:
            locations = locations[-MAX_LOCATIONS:]
        store["locations"] = locations
        _write_store(store)
    return record


def recent_locations(n: int = 20) -> list[dict]:
    """Most-recent-first list of stored locations."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 20
    n = max(1, min(n, MAX_LOCATIONS))
    with _LOCK:
        store = _read_store()
    locations = store["locations"]
    return list(reversed(locations[-n:]))


def nearest_saved(lat: float, lng: float) -> dict | None:
    """Nearest previously saved location that carries a label.

    Returns the record plus ``distance_km``, or None when there is nothing
    labelled to compare against.
    """
    best = None
    best_distance = None
    for record in recent_locations(MAX_LOCATIONS):
        label = str(record.get("label") or "")
        if not label:
            continue
        try:
            distance = haversine_km(lat, lng, record["lat"], record["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = record
    if best is None:
        return None
    out = dict(best)
    out["distance_km"] = round(float(best_distance), 4)
    return out
