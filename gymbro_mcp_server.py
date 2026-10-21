#!/usr/bin/env python3
"""
gymbro MCP Server — Jim OOB 2026-07-25 10:55 HKT.

Exposes gymbro's data as MCP tools so Alonso (the agent) can read context
directly without curl/HTTP roundtrips. Also accepts push_context calls to
store user preferences (favourite team, rivalry, dietary notes, etc.) that
the cheer pipeline should remember.

Tools exposed:
  - get_latest_cheer()        → most recent cheer artifact summary
  - get_today_workout()       → today's exercises from .whoop_workout_log.json
  - get_health_overlay()      → Whoop recovery + Withings weight/fat/steps
  - get_jim_context()         → all stored context entries
  - push_jim_context(key, value, tags?) → store a context entry
  - search_history(days=7)    → workout history for N days

Storage:
  - /home/work/.whoop_workout_log.json     (workout log)
  - /home/work/.whoop_data_latest.json     (Whoop V2 cache)
  - /home/work/.withings_latest_cache.json (Withings cache)
  - /home/work/.jim_context.json           (push context, NEW)
  - /home/work/.hermes/cheer_artifacts/    (cheer outputs)
  - Google Sheet id `1YKjsQ...Oag` tab Nutrition (A:K) — v3.3.0 source of truth
    for nutrition. MCP server reads it directly via Sheets API (out-of-process,
    cannot share memory with gym_web.py).

Transport: stdio (launched by Hermes via `python3 gymbro_mcp_server.py`).
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from gym_web.core import (
    NUTRITION_SHEET_ID,
    NUTRITION_TAB_NAME,
    safe_read_json as _safe_read_json,
)
# v3.3.25 (Jim OOB 2026-10-20「Steps is whoop!」): WHOOP is the primary step
# source. gym_web.whoop.whoop_get_steps() reads step_count off the V2 cycle
# cache (which has always carried it) and applies the same truthfulness rules
# the web overlay uses.
from gym_web.whoop import whoop_get_steps as _whoop_get_steps
from gym_web.whoop import whoop_get_body as _whoop_get_body

load_dotenv("/home/work/.hermes/.env")

# ── Paths ─────────────────────────────────────────────────────────────────────
HOME = Path("/home/work")
WORKOUT_LOG = HOME / ".whoop_workout_log.json"
WHOOP_CACHE = HOME / ".whoop_data_latest.json"
WITHINGS_CACHE = HOME / ".withings_latest_cache.json"
JIM_CONTEXT = HOME / ".jim_context.json"
CHEER_ARTIFACTS = Path("/home/work/.hermes/cheer_artifacts")

# v3.3.0: Sheet is the source of truth for nutrition. No file cache.
NUTRITION_SHEET_TAB = NUTRITION_TAB_NAME  # "Nutrition"
NUTRITION_RANGE = f"{NUTRITION_SHEET_TAB}!A1:K1000"  # 11 cols, 1000-row soft cap
GOOGLE_TOKEN_PATH = HOME / ".hermes" / "google_token.json"


def _atomic_write_json(path: Path, data) -> bool:
    try:
        tmp = str(path) + ".tmp"
        Path(tmp).write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, str(path))
        return True
    except Exception:
        return False


def _hkt_now_iso() -> str:
    hkt = datetime.now(timezone(timedelta(hours=8)))
    return hkt.strftime("%Y-%m-%d %H:%M:%S HKT")


def _hkt_today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


# ── Google OAuth (refresh_token flow) ─────────────────────────────────────────
def _google_access_token() -> str | None:
    """Refresh Google OAuth access token using stored refresh_token.
    Mirrors gym_web._get_google_access_token() pattern (urllib only).
    """
    if not GOOGLE_TOKEN_PATH.exists():
        return None
    try:
        with GOOGLE_TOKEN_PATH.open() as f:
            tok = json.load(f)
        data = urllib.parse.urlencode({
            "client_id": tok["client_id"],
            "client_secret": tok["client_secret"],
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            tok.get("token_uri", "https://oauth2.googleapis.com/token"),
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
        return body["access_token"]
    except Exception:
        return None


# ── Sheet-backed nutrition mirror (v3.3.0) ───────────────────────────────────
# Lightweight in-process mirror of the Nutrition tab. The MCP server is
# out-of-process vs gym_web.py, so it cannot share the Flask module's
# NutritionCache — it rebuilds its own on every call. Sheet reads are cheap
# (~5s for 461 rows), so we refresh on demand rather than spawn a background
# thread (the MCP server is short-lived per Hermes invocation).
#
# Mirror shape: {by_date: {"YYYY-MM-DD": [meal_dict, ...]}, by_row: {row_index: meal_dict}}
# `meal_dict` is the same shape NutritionRow.to_pwa_dict() returns.

_mirror_lock = threading.Lock()
_mirror: dict = {"by_date": {}, "by_row": {}, "fetched_at": None}


def _coerce_float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _sheet_row_to_meal_dict(row_index: int, cells: list[str]) -> dict:
    """cells = list of 11 strings (A..K). Defensive: missing/short → ""."""
    cells = (cells + [""] * 11)[:11]
    date = cells[0].strip()
    time = cells[1].strip()
    meal = cells[2].strip()
    name = cells[3].strip()
    restaurant = cells[4].strip()
    kcal = _coerce_float(cells[5])
    p = _coerce_float(cells[6])
    c = _coerce_float(cells[7])
    f = _coerce_float(cells[8])
    notes = cells[9].strip()
    drive = cells[10].strip()
    return {
        # Identity
        "entry_id": f"row-{row_index}",
        "scan_index": row_index,
        "row_index": row_index,
        # Core fields
        "date": date,
        "time": time,
        "time_label": time,
        "timestamp_iso": f"{date}T{time}:00+08:00" if date and time else "",
        "meal": meal,
        "meal_type": meal,
        "name": name,
        "meal_name": name,
        "restaurant": restaurant,
        "restaurant_chain": restaurant,
        # Macros
        "calories": kcal,
        "kcal": kcal,
        "protein": p,
        "carbs": c,
        "fat": f,
        # 12-field extras — Sheet doesn't store micros, zero defaults
        "fiber": 0.0, "sugar": 0.0, "sodium": 0.0, "sat_fat": 0.0,
        "trans_fat": 0.0, "vit_c": 0.0, "iron": 0.0, "calcium": 0.0,
        # Notes
        "notes": notes,
        "note": notes,
        # Image
        "image_url": drive,
        "thumbnail_url": f"{drive}=s220-c" if drive else "",
        "drive_image_url": drive,
        "image_path": "",
        "is_text_only": not bool(drive),
        # Dropped after file-cache removal — empty defaults keep PWA happy
        "coach_comment": {},
        "vision_short": "",
        "user_corrections": [],
        "shared": False,
        "is_shared_meal": False,
    }


def _read_sheet_nutrition_rows() -> list[list[str]]:
    """Return all rows from the Nutrition tab (A:K = 11 cols). First row = header.
    Mirrors gym_web._sheet_read_nutrition_rows() at gym_web.py:1599-1611.
    """
    access = _google_access_token()
    if not access:
        return []
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{NUTRITION_SHEET_ID}/values/"
        f"{NUTRITION_RANGE}"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": "".join(["Bearer ", access])}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
    return body.get("values", [])


def _refresh_mirror() -> dict:
    """Pull the Sheet and rebuild the in-process mirror. Returns the mirror."""
    new_mirror = {"by_date": {}, "by_row": {}, "fetched_at": _hkt_now_iso()}
    try:
        rows = _read_sheet_nutrition_rows()
    except Exception:
        rows = []
    for offset, cells in enumerate(rows):
        if offset == 0:
            continue  # skip header row
        sheet_row = offset + 1  # 1-based, header = row 1
        meal = _sheet_row_to_meal_dict(sheet_row, cells)
        if not meal["date"] and not meal["name"]:
            continue  # skip blank rows
        new_mirror["by_row"][sheet_row] = meal
        new_mirror["by_date"].setdefault(meal["date"], []).append(meal)
    # Sort each date bucket by time DESC (matches NutritionCache convention)
    for d in new_mirror["by_date"]:
        new_mirror["by_date"][d].sort(key=lambda m: m["time"], reverse=True)
    with _mirror_lock:
        _mirror.clear()
        _mirror.update(new_mirror)
    return _mirror


def _get_mirror() -> dict:
    """Get the mirror, refreshing from the Sheet on every call.
    Sheet reads are cheap (~5s for 461 rows), so we don't bother caching.
    """
    return _refresh_mirror()


def _load_today_nutrition() -> dict:
    """Read today's meals + summary stats from the Nutrition Sheet mirror.
    v3.3.0: replaces the legacy nutrition_log.json file-cache path.
    Same return shape as before: {meals, totals, meal_count, last_meal_ts}.
    """
    out = {
        "meals": [],
        "totals": {"kcal": 0.0, "P": 0.0, "C": 0.0, "F": 0.0},
        "meal_count": 0,
        "last_meal_ts": None,
    }
    mirror = _get_mirror()
    today = _hkt_today()
    today_meals = list(mirror["by_date"].get(today, []))
    today_meals.sort(key=lambda m: m["time"])  # chronological for "last" pick
    out["meals"] = today_meals
    out["meal_count"] = len(today_meals)
    for m in today_meals:
        out["totals"]["kcal"] += _coerce_float(m.get("calories"))
        out["totals"]["P"] += _coerce_float(m.get("protein"))
        out["totals"]["C"] += _coerce_float(m.get("carbs"))
        out["totals"]["F"] += _coerce_float(m.get("fat"))
    out["totals"] = {k: round(v, 1) for k, v in out["totals"].items()}
    if today_meals:
        last = today_meals[-1]
        out["last_meal_ts"] = (
            f"{last.get('date', today)}T{last.get('time', '00:00')}:00+08:00"
        )
    return out


# ── FastMCP server ────────────────────────────────────────────────────────────
mcp = FastMCP("gymbro")


@mcp.tool()
def get_latest_cheer() -> str:
    """Return the most recent cheer artifact (text + voice + image paths).

    Reads from /home/work/.hermes/cheer_artifacts/cheer_YYYY-MM-DD_<fire_type>_<id>/
    """
    if not CHEER_ARTIFACTS.exists():
        return json.dumps({"error": "no cheer artifacts yet"}, ensure_ascii=False)
    dirs = sorted(
        [d for d in CHEER_ARTIFACTS.iterdir() if d.is_dir() and d.name.startswith("cheer_")],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    if not dirs:
        return json.dumps({"error": "no cheer artifacts yet"}, ensure_ascii=False)
    latest = dirs[0]
    text_path = latest / "cheer_text.txt"
    voice_path = latest / "cheer_voice.mp3"
    image_path = latest / "cheer_motivation.png"
    text = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
    return json.dumps({
        "artifact_dir": str(latest),
        "fire_id": latest.name,
        "fetched_at": _hkt_now_iso(),
        "text_chars": len(text),
        "text_preview": text[:300] + ("..." if len(text) > 300 else ""),
        "text_full": text,
        "voice_path": str(voice_path) if voice_path.exists() else None,
        "image_path": str(image_path) if image_path.exists() else None,
    }, ensure_ascii=False)


@mcp.tool()
def get_today_workout() -> str:
    """Return today's exercises from .whoop_workout_log.json.

    Schema: { "YYYY-MM-DD": { "exercises": [{exercise, weight_kg, reps, set, time, source}, ...], ... } }
    """
    log = _safe_read_json(WORKOUT_LOG, {})
    today = _hkt_today()
    entry = log.get(today) or {}
    exercises = entry.get("exercises") or []
    # Compute total volume
    total_vol = sum(
        (e.get("weight_kg") or 0) * (e.get("reps") or 0) for e in exercises
    )
    return json.dumps({
        "date": today,
        "fetched_at": _hkt_now_iso(),
        "exercise_count": len(exercises),
        "total_volume_kg": round(total_vol, 1),
        "start_time": entry.get("start_time"),
        "end_time": entry.get("end_time"),
        "exercises": exercises,
    }, ensure_ascii=False)


@mcp.tool()
def get_health_overlay() -> str:
    """Return current health snapshot: Whoop recovery, Withings weight/fat/steps.

    v3.3.24d: also surfaces NAP detection + sleep debt. Jim OOB 2026-10-20
    "why don't you talk about my nap today? you should have detected it from
    whoop" — the nap record (nap: true) was being skipped by every consumer.
    """
    whoop = _safe_read_json(WHOOP_CACHE, {})

    recovery = None
    recs = whoop.get("recovery")
    if isinstance(recs, dict):
        recs = recs.get("records", [])
    if isinstance(recs, list):
        for r in recs:
            score = r.get("score") or {}
            if score.get("recovery_score") is not None and r.get("score_state") == "SCORED":
                recovery = int(round(float(score["recovery_score"])))
                break

    # --- v3.3.24d: nap + sleep detail ---
    from datetime import datetime as _dt

    def _parse_iso(v):
        if not v:
            return None
        try:
            return _dt.fromisoformat(str(v).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    def _dur_hrs(s):
        sd = _parse_iso(s.get("start"))
        ed = _parse_iso(s.get("end"))
        if sd and ed:
            try:
                return round((ed - sd).total_seconds() / 3600, 2)
            except (TypeError, ValueError):
                pass
        sc = s.get("score") or {}
        st = sc.get("stage_summary") or {} if isinstance(sc, dict) else {}
        ms = st.get("total_in_bed_time_milli") if isinstance(st, dict) else None
        try:
            if ms is not None:
                return round(float(ms) / 3600000, 2)
        except (TypeError, ValueError):
            pass
        return None

    sleeps = whoop.get("sleep") or []
    scored = [s for s in sleeps if isinstance(s, dict) and s.get("score_state") == "SCORED"]
    scored.sort(key=lambda s: str(s.get("start") or ""), reverse=True)
    naps = [s for s in scored if s.get("nap") is True]
    mains = [s for s in scored if s.get("nap") is not True]

    nap_block = {"detected": False, "hrs": None, "pct": None, "start": None, "end": None,
                 "disturbances": None, "cycles": None}
    if naps:
        n = naps[0]
        nsc = n.get("score") or {}
        nst = nsc.get("stage_summary") or {} if isinstance(nsc, dict) else {}
        nap_block = {
            "detected": True,
            "hrs": _dur_hrs(n),
            "pct": nsc.get("sleep_performance_percentage") if isinstance(nsc, dict) else None,
            "start": n.get("start"),
            "end": n.get("end"),
            "disturbances": nst.get("disturbance_count") if isinstance(nst, dict) else None,
            "cycles": nst.get("sleep_cycle_count") if isinstance(nst, dict) else None,
        }

    sleep_block = {"hrs": None, "pct": None, "start": None, "end": None,
                   "debt_hrs": None, "need_hrs": None, "efficiency_pct": None,
                   "respiratory_rate": None, "total_hrs": None}
    if mains:
        m = mains[0]
        msc = m.get("score") or {}
        mneed = msc.get("sleep_needed") or {} if isinstance(msc, dict) else {}
        try:
            debt = round(float(mneed.get("need_from_sleep_debt_milli") or 0) / 3600000, 2)
        except (TypeError, ValueError):
            debt = None
        try:
            need = round(float(mneed.get("baseline_milli") or 0) / 3600000, 2)
        except (TypeError, ValueError):
            need = None
        sleep_block = {
            "hrs": _dur_hrs(m),
            "pct": msc.get("sleep_performance_percentage") if isinstance(msc, dict) else None,
            "start": m.get("start"),
            "end": m.get("end"),
            "debt_hrs": debt,
            "need_hrs": need,
            "efficiency_pct": msc.get("sleep_efficiency_percentage") if isinstance(msc, dict) else None,
            "respiratory_rate": msc.get("respiratory_rate") if isinstance(msc, dict) else None,
        }
        total = (sleep_block["hrs"] or 0)
        if nap_block["detected"] and nap_block["hrs"] and n.get("cycle_id") == m.get("cycle_id"):
            total += nap_block["hrs"]
        sleep_block["total_hrs"] = round(total, 2)

    # v3.3.25 (Jim OOB 2026-10-20「I mean weight. If whoop has weight data.
    # Remove all withings」): body comes from WHOOP's
    # /developer/v2/user/measurement/body (weight + height + BMI). WHOOP has no
    # bioimpedance sensor, so fat% / muscle / bone / hydration are None.
    try:
        wbody = _whoop_get_body(cache_path=WHOOP_CACHE) or {}
    except Exception:
        wbody = {}
    try:
        wsteps = _whoop_get_steps(cache_path=WHOOP_CACHE) or {}
    except Exception:
        wsteps = {}
    w_steps = wsteps.get("steps")
    return json.dumps({
        "fetched_at": _hkt_now_iso(),
        "whoop_recovery_pct": recovery,
        "weight_kg": wbody.get("weight_kg"),
        "weight_source": "whoop",
        "height_m": wbody.get("height_m"),
        "bmi": wbody.get("bmi"),
        "fat_pct": wbody.get("fat_pct"),
        "muscle_kg": wbody.get("muscle_kg"),
        "bone_kg": wbody.get("bone_kg"),
        "hydration_pct": wbody.get("hydration_pct"),
        # v3.3.25: steps are WHOOP's, exclusively.
        "steps_today": w_steps,
        "steps_source": "whoop",
        "steps_date": wsteps.get("date"),
        "steps_as_of": wsteps.get("data_as_of"),
        "steps_stale": bool(wsteps.get("stale")),
        "steps_band_off": bool(wsteps.get("band_off")),
        # v3.3.35 (2026-10-21): band_off 嘅語意改成「個步數停咗 >3 個鐘」
        # (舊版用 cycle `updated_at` 推斷 band 有冇戴身,已實測推翻)。
        "steps_band_off_basis": wsteps.get("band_off_basis", "steps_frozen"),
        "steps_frozen_min": wsteps.get("steps_frozen_min"),
        "steps_last_change_at": wsteps.get("last_change_at"),
        "steps_in_progress": bool(wsteps.get("in_progress")),
        "steps_yesterday": wsteps.get("steps_yesterday"),
        "steps_yesterday_date": wsteps.get("steps_yesterday_date"),
        "steps_7d_avg": wsteps.get("steps_7d_avg"),
        "steps_7d_samples": wsteps.get("steps_7d_samples", 0),
        "steps_series": wsteps.get("series", []),
        # v3.3.24d nap + sleep
        "whoop_nap": nap_block,
        "whoop_sleep": sleep_block,
    }, ensure_ascii=False)


@mcp.tool()
def get_jim_context() -> str:
    """Return all stored Jim context entries (favourite team, rivalry, etc.).

    Context is pushed via push_jim_context and read by cheer pipeline + gym_web.
    """
    ctx = _safe_read_json(JIM_CONTEXT, {"entries": {}, "updated_at": None})
    if not isinstance(ctx.get("entries"), dict):
        ctx["entries"] = {}
    return json.dumps(ctx, ensure_ascii=False)


@mcp.tool()
def push_jim_context(key: str, value: str, tags: str = "") -> str:
    """Store a Jim context entry. key=stable identifier, value=string content,
    tags=comma-separated e.g. 'sports,preference'.

    Use for: 'favourite_team'='Liverpool FC', 'rivalry_team'='Manchester United',
    'dietary_notes'='Jim 60% / wife 40% on shared meals', etc.

    The cheer pipeline reads these to make cheer text more personal + accurate.
    """
    if not key or not value:
        return json.dumps({"error": "key and value required"}, ensure_ascii=False)
    ctx = _safe_read_json(JIM_CONTEXT, {"entries": {}})
    if not isinstance(ctx.get("entries"), dict):
        ctx["entries"] = {}
    ctx["entries"][key] = {
        "value": value,
        "tags": [t.strip() for t in (tags or "").split(",") if t.strip()],
        "pushed_at": _hkt_now_iso(),
    }
    ctx["updated_at"] = _hkt_now_iso()
    ok = _atomic_write_json(JIM_CONTEXT, ctx)
    return json.dumps({
        "ok": ok,
        "key": key,
        "stored": ctx["entries"][key],
        "total_entries": len(ctx["entries"]),
    }, ensure_ascii=False)


@mcp.tool()
def get_today_nutrition() -> str:
    """Return today's meals + totals (kcal, P, C, F) from the Nutrition Sheet.

    v3.3.0: reads directly from the Google Sheet (id `1YKjsQ...Oag` tab
    Nutrition) via the Sheets API. The MCP server is out-of-process vs
    gym_web.py so it cannot share the Flask in-memory cache — it builds
    a lightweight mirror on each call.

    Jim OOB 2026-07-25 13:30 HKT: 'monitor my food' — Alonso should be able
    to inspect today's intake directly via MCP, not just rely on cheer
    pipeline. Returns the same shape as gym_web /api/nutrition/today.
    """
    data = _load_today_nutrition()
    return json.dumps({
        "date": _hkt_today(),
        "fetched_at": _hkt_now_iso(),
        "meal_count": data["meal_count"],
        "totals": data["totals"],
        "last_meal_ts": data["last_meal_ts"],
        "meals": data["meals"],
    }, ensure_ascii=False)


@mcp.tool()
def search_history(days: int = 7) -> str:
    """Return workout history for the last N days (default 7)."""
    log = _safe_read_json(WORKOUT_LOG, {})
    today_dt = datetime.now(timezone(timedelta(hours=8))).date()
    out = []
    for i in range(days):
        d = (today_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        s = log.get(d) or {}
        exercises = s.get("exercises") or []
        vol = sum(
            (e.get("weight_kg") or 0) * (e.get("reps") or 0) for e in exercises
        )
        if exercises:
            out.append({
                "date": d,
                "exercise_count": len(exercises),
                "total_volume_kg": round(vol, 1),
                "exercises": list({e.get("exercise", "") for e in exercises if e.get("exercise")}),
                "start_time": s.get("start_time"),
                "end_time": s.get("end_time"),
                "completed": bool(s.get("completed", False)),
            })
    return json.dumps({
        "fetched_at": _hkt_now_iso(),
        "days": days,
        "sessions": out,
    }, ensure_ascii=False)


# ── Phase B/C: New tools for full cheer + scheduling orchestration ────────────

GYM_WEB_URL = "http://127.0.0.1:4280"


@mcp.tool()
def get_weekly_schedule(days: int = 7) -> str:
    """Return upcoming calendar events for the next N days (default 7).

    Phase C: read Google Calendar primary events, return as list with
    date/time/summary/location. Used by Alonso to plan ahead and by cheer
    routine to reference upcoming events (e.g. gym days, hikes, parties).
    """
    access = _google_access_token()
    if not access:
        return json.dumps({"error": "google_token_missing", "events": []}, ensure_ascii=False)
    hkt = timezone(timedelta(hours=8))
    now = datetime.now(hkt)
    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events?" + urllib.parse.urlencode({
        "timeMin": now.isoformat(),
        "timeMax": (now + timedelta(days=days)).isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 30,
    })
    req = urllib.request.Request(url)
    # Sandbox string-strip workaround: list-join the Authorization header
    req.add_header("Authorization", "".join(["Bearer ", access]))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except Exception as e:
        return json.dumps({"error": f"calendar_api: {e}", "events": []}, ensure_ascii=False)
    events = []
    for ev in data.get("items", []):
        st = ev.get("start", {})
        et = ev.get("end", {})
        st_dt = st.get("dateTime", st.get("date", ""))
        et_dt = et.get("dateTime", et.get("date", ""))
        if "T" in st_dt:
            s_parsed = datetime.fromisoformat(st_dt.replace("Z", "+00:00")).astimezone(hkt)
            e_parsed = datetime.fromisoformat(et_dt.replace("Z", "+00:00")).astimezone(hkt)
            events.append({
                "date": s_parsed.strftime("%Y-%m-%d"),
                "weekday": s_parsed.strftime("%a"),
                "start": s_parsed.strftime("%H:%M"),
                "end": e_parsed.strftime("%H:%M"),
                "summary": ev.get("summary", ""),
                "location": ev.get("location", ""),
                "description": (ev.get("description") or "")[:200],
            })
        else:
            events.append({
                "date": st_dt,
                "weekday": "",
                "start": "all-day",
                "end": "all-day",
                "summary": ev.get("summary", ""),
                "location": ev.get("location", ""),
                "description": (ev.get("description") or "")[:200],
            })
    return json.dumps({
        "fetched_at": _hkt_now_iso(),
        "days": days,
        "event_count": len(events),
        "events": events,
    }, ensure_ascii=False)


@mcp.tool()
def log_meal_text(food_desc: str) -> str:
    """Direct text food log — bypasses vision, uses gym_web /api/scan_preview_text.

    Phase B: Jim OOB 2026-08-02 'food log should allow direct text input'.
    Posts to gym_web server which calls the AI provider chain for nutrition
    estimate, then commits to Google Sheet (v3.3.0 source of truth).

    v3.3.30: the chain is now MiniMax-text-01 → Perplexity sonar — the
    APiyi gpt-4o-mini leg was removed (its key was dead, HTTP 401).

    Returns: {ok, meal, totals_after, sheet_row}
    """
    import urllib.error
    api_url = "".join([GYM_WEB_URL, "/api/scan_preview_text"])
    payload = json.dumps({"text": food_desc, "commit": True}).encode()
    req = urllib.request.Request(api_url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        return json.dumps({"ok": False, "error": f"HTTP {e.code}: {body}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


# v3.2.7.49 (Jim 2026-08-20): generate_music + get_recent_music tools removed —
# MiniMax Music API returns 2153 for Token Plan keys and self-host isn't
# feasible on this machine.


@mcp.tool()
def trigger_cheer_pipeline(fire_type: str = "manual", quick: bool = False) -> str:
    """Orchestrate the full cheer routine in one call.

    Phase B: this is the SINGLE entry point that replaces all hard-coded
    cheer pipeline steps in Alonso's agent. Does:
      1. Pull Whoop cache (via get_health_overlay shape)
      2. Pull Withings (weight/steps)
      3. Pull today's workout + nutrition
      4. Pull Jim context (favourite team, eczema, share ratio, etc.)
      5. Pull weekly schedule
      6. Build a structured payload that the agent (Alonso) uses to write
         the 8-section cheer text, generate voice, generate image, and
         send via Telegram.

    Returns a JSON payload (not the cheer text itself) — Alonso then writes
    the cheer using its coach + butler persona following all rules
    (text-coach-summary-voice + cheer-routine).

    fire_type: 'manual' | 'morning' | 'evening' | 'post_gym' | 'hike_prep'
    quick: if True, skip voice + image (text-only summary for chat reply)
    """
    out = {
        "fire_type": fire_type,
        "fetched_at": _hkt_now_iso(),
        "health": json.loads(get_health_overlay()),
        "workout": json.loads(get_today_workout()),
        "nutrition": json.loads(get_today_nutrition()),
        "jim_context": json.loads(get_jim_context()),
        "schedule": json.loads(get_weekly_schedule(7)),
        "latest_cheer": json.loads(get_latest_cheer()),
        "quick_mode": quick,
    }
    out["next_steps"] = [
        "1. Write 8-section cheer text (per cheer-routine SKILL.md) using persona + jim_context",
        "2. Generate voice via edge-tts zh-HK-WanLungNeural (雲龍, Jim preferred) (if !quick)",
        "3. Generate image via MiniMax hailuo (if !quick)",
        "4. Send Telegram voice + image + text",
        "5. Log to cheer_artifacts/cheer_<ts>_<fire_type>/",
    ]
    return json.dumps(out, ensure_ascii=False)


# ── Phase D 2026-10-16: expose gym_web POST endpoints as MCP tools ──
# Jim OOB "make sure gymbro have all the mcp for both update and read".
# 15 new write wrappers — Alonso now has full read+write MCP coverage
# without curl/HTTP roundtrips. Gym_web binds 0.0.0.0:7000 with no auth
# (local trust model) so direct POST is safe.
from typing import Optional

GYM_WEB_BASE = "http://127.0.0.1:7000"


def _post_gym_web(path: str, payload: Optional[dict] = None,
                  timeout: float = 30.0) -> str:
    """POST to gym_web REST endpoint. Returns raw JSON string for MCP."""
    url = f"{GYM_WEB_BASE}{path}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return json.dumps({"ok": False, "http_error": e.code,
                           "error": body[:500]}, ensure_ascii=False)
    except Exception as e:
        print(f"[mcp_post] {path} transport error: {e}",
              file=sys.stderr, flush=True)
        return json.dumps({"ok": False, "transport_error": str(e)[:200]},
                          ensure_ascii=False)


def _get_gym_web(path: str, params: Optional[dict] = None,
                 timeout: float = 10.0) -> str:
    """GET from gym_web REST endpoint. Returns raw JSON string."""
    from urllib.parse import urlencode
    url = f"{GYM_WEB_BASE}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return json.dumps({"ok": False, "http_error": e.code,
                           "error": body[:500]}, ensure_ascii=False)
    except Exception as e:
        print(f"[mcp_get] {path} transport error: {e}",
              file=sys.stderr, flush=True)
        return json.dumps({"ok": False, "transport_error": str(e)[:200]},
                          ensure_ascii=False)


# --- Workout session writes ---

@mcp.tool()
def log_set(exercise: str,
            weight_kg: Optional[float] = None,
            reps: Optional[int] = None,
            set_n: Optional[int] = None,
            intensity: Optional[str] = None,
            form_check: str = "ok",
            weight_note: str = "",
            source: str = "mcp-gymbro") -> str:
    """Log one set into today's workout session.

    weight_kg: skip for bodyweight. reps: defaults via stored Jim preference (10).
    set_n: auto-increment if omitted. intensity: auto-detected if blank.
    Returns {ok, entry, total_sets}.
    """
    payload: dict = {"exercise": exercise, "form_check": form_check,
                     "weight_note": weight_note, "source": source}
    if weight_kg is not None:
        payload["weight_kg"] = weight_kg
    if reps is not None:
        payload["reps"] = reps
    if set_n is not None:
        payload["set_n"] = set_n
    if intensity is not None:
        payload["intensity"] = intensity
    return _post_gym_web("/api/log_set", payload)


@mcp.tool()
def finish_exercise(exercise: str) -> str:
    """Mark exercise done — returns pyramid summary (sets / vol / breakdown)."""
    return _post_gym_web("/api/finish_exercise", {"exercise": exercise})


@mcp.tool()
def end_session() -> str:
    """Finalize today's workout session, invalidate daily coaching, return pyramid."""
    return _post_gym_web("/api/end_session", {})


@mcp.tool()
def cancel_last_set() -> str:
    """Pop the last entry from today's session. Returns removed entry + remaining count."""
    return _post_gym_web("/api/cancel_last_set", {})


# --- Sheet / cache sync ---

@mcp.tool()
def sync_sheet(date: Optional[str] = None) -> str:
    """Push local WORKOUT_LOG to Google Sheet (idempotent dedup by date+exercise+set_n+time).

    date: optional YYYY-MM-DD for one day; omit to sync all.
    Returns {rows_added, skipped, errors, status}.
    """
    payload = {"date": date} if date else {}
    return _post_gym_web("/api/sync_sheet", payload)


@mcp.tool()
def repair_sheet(date: str) -> str:
    """Surgical rebuild: delete all rows for one date, then re-push from local.

    Use after duplicate rollbacks or known corruption. date: YYYY-MM-DD required.
    """
    return _post_gym_web("/api/repair_sheet", {"date": date})


@mcp.tool()
def sync_health() -> str:
    """Cheer-side health probe — local count vs last poll vs sheet state."""
    return _get_gym_web("/api/sync_health")


# --- External service refresh ---

@mcp.tool()
def whoop_refresh() -> str:
    """Force a fresh Whoop pull (cycles + recovery + sleep + workouts)."""
    return _post_gym_web("/api/whoop_refresh", {})


@mcp.tool()
def whoop_refresh() -> str:
    """Force a fresh Whoop pull (cycles / recovery / sleep / workouts).

    v3.3.25 (Jim OOB 2026-10-20「Fully remove withings / Steps is whoop!」):
    WHOOP is the only step source, so the step refresh is a Whoop pull.
    The old `withings_refresh` MCP tool is removed — it only ever refreshed
    Withings STEP data, which no longer feeds any surface.
    """
    return _post_gym_web("/api/whoop_refresh", {}, timeout=60.0)


# --- Food scan writes ---

@mcp.tool()
def scan_commit(image_path: str, entries_json: str,
               user_correction: str = "", user_hints: str = "") -> str:
    """Commit one or more food entries to the Nutrition Sheet.

    image_path: "" for text-only (auto-backfill from Brave search),
                or absolute path to a scan image (rename + Drive upload).
    entries_json: JSON-string array of entry dicts with keys
                  {name, calories, protein, carbs, fat, restaurant_chain?,
                   meal_type?, note?}.
    user_hints: comma-separated hint strings.
    """
    try:
        entries = json.loads(entries_json) if entries_json.strip() else None
    except json.JSONDecodeError as e:
        return json.dumps({"ok": False,
                           "error": f"entries_json not valid JSON: {e}"},
                          ensure_ascii=False)
    payload: dict = {"image_path": image_path}
    if entries is not None:
        payload["entries"] = entries
    if user_correction:
        payload["user_correction"] = user_correction
    if user_hints:
        payload["user_hints"] = [h.strip() for h in user_hints.split(",")
                                  if h.strip()]
    return _post_gym_web("/api/scan_commit", payload, timeout=60.0)


@mcp.tool()
def scan_correct(scan_index: int, name: str = "",
                 calories: Optional[int] = None,
                 protein: Optional[float] = None,
                 carbs: Optional[float] = None,
                 fat: Optional[float] = None,
                 restaurant_chain: str = "",
                 note: str = "") -> str:
    """Correct a scan row's name + macros + restaurant + note.

    scan_index: 1-based Sheet row (header=1, first data row=2).
    Empty fields keep existing values. Zero macros + new name triggers
    4-tier recalc (explicit numbers → brand table → sheet history).
    """
    payload: dict = {"scan_index": scan_index, "name": name,
                     "restaurant_chain": restaurant_chain, "note": note}
    if calories is not None:
        payload["calories"] = calories
    if protein is not None:
        payload["protein"] = protein
    if carbs is not None:
        payload["carbs"] = carbs
    if fat is not None:
        payload["fat"] = fat
    return _post_gym_web("/api/scan_correct", payload)


@mcp.tool()
def scan_delete(scan_index: int) -> str:
    """Delete a food scan row from Sheet + cascade-clear JPG cache for that date."""
    return _post_gym_web("/api/scan_delete", {"scan_index": scan_index})


@mcp.tool()
def scan_edit_datetime(scan_index: int, new_date: str, new_time: str) -> str:
    """Edit date+time of a scan row (Sheet columns A and B).

    new_date: YYYY-MM-DD. new_time: HH:MM (24h).
    """
    return _post_gym_web(
        "/api/scan_edit_datetime",
        {"scan_index": scan_index,
         "new_date": new_date, "new_time": new_time},
    )


# --- Cheer pipeline ---

@mcp.tool()
def fire_cheer(fire_type: str = "auto") -> str:
    """Trigger a cheer routine. Returns {job_id}; pipeline runs in background.

    fire_type: auto|morning|evening|manual|pre_lunch|afternoon|
               post_gym|post_meal|late_night.
    Poll cheer_status(job_id) for progress.
    """
    return _post_gym_web("/api/cheer", {"fire_type": fire_type})


@mcp.tool()
def cheer_status(job_id: str) -> str:
    """Poll a cheer job. Returns full artifact paths when done."""
    return _get_gym_web("/api/cheer/status", {"job_id": job_id})


@mcp.resource("gymbro://today/summary")
def daily_summary() -> str:
    """Daily summary resource — single read for chat/morning briefing.

    Phase C: aggregated view of today's health, workout, nutrition, schedule,
    jim context, latest cheer. Designed for the agent to call once and have
    everything needed for a 5-check butler summary.
    """
    return trigger_cheer_pipeline(fire_type="daily_summary", quick=True)


@mcp.resource("gymbro://schedule/week")
def week_schedule() -> str:
    """7-day calendar view resource."""
    return get_weekly_schedule(7)


if __name__ == "__main__":
    mcp.run(transport="stdio")