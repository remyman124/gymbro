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

Transport: stdio (launched by Hermes via `python3 gymbro_mcp_server.py`).
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── Paths ─────────────────────────────────────────────────────────────────────
HOME = Path("/home/work")
WORKOUT_LOG = HOME / ".whoop_workout_log.json"
WHOOP_CACHE = HOME / ".whoop_data_latest.json"
WITHINGS_CACHE = HOME / ".withings_latest_cache.json"
JIM_CONTEXT = HOME / ".jim_context.json"
NUTRITION_LOG = Path("/home/work/.hermes/nutrition_log.json")
CHEER_ARTIFACTS = Path("/home/work/.hermes/cheer_artifacts")


def _safe_read_json(path: Path, default=None):
    if default is None:
        default = {}
    try:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default


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


def _load_today_nutrition() -> dict:
    """Read /home/work/.hermes/nutrition_log.json and return today's meals
    + summary stats. Mirrors gym_web._load_today_nutrition.
    """
    out = {"meals": [], "totals": {"kcal": 0.0, "P": 0.0, "C": 0.0, "F": 0.0}, "meal_count": 0, "last_meal_ts": None}
    if not NUTRITION_LOG.exists():
        return out
    try:
        log = json.loads(NUTRITION_LOG.read_text())
    except Exception:
        return out
    meals = (log or {}).get("meals") or []
    today = _hkt_today()
    today_meals = []
    for m in meals:
        if not isinstance(m, dict):
            continue
        m_date = m.get("date") or ""
        if not m_date:
            ts = m.get("timestamp") or m.get("logged_at")
            if ts and isinstance(ts, str) and today in ts:
                m_date = today
        if m_date != today:
            continue
        today_meals.append(m)
    today_meals.sort(key=lambda m: m.get("time") or m.get("timestamp") or "")
    out["meals"] = today_meals
    out["meal_count"] = len(today_meals)
    for m in today_meals:
        try:
            out["totals"]["kcal"] += float(m.get("calories") or 0)
            out["totals"]["P"] += float(m.get("protein") or 0)
            out["totals"]["C"] += float(m.get("carbs") or 0)
            out["totals"]["F"] += float(m.get("fat") or 0)
        except (TypeError, ValueError):
            pass
    out["totals"] = {k: round(v, 1) for k, v in out["totals"].items()}
    if today_meals:
        last = today_meals[-1]
        out["last_meal_ts"] = f"{last.get('date', today)}T{last.get('time', '00:00')}:00+08:00"
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
    """Return current health snapshot: Whoop recovery, Withings weight/fat/steps."""
    whoop = _safe_read_json(WHOOP_CACHE, {})
    withings = _safe_read_json(WITHINGS_CACHE, {})

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

    body = withings.get("body") or {}
    steps_block = withings.get("steps") or {}
    return json.dumps({
        "fetched_at": _hkt_now_iso(),
        "whoop_recovery_pct": recovery,
        "withings_weight_kg": body.get("weight_kg"),
        "withings_fat_pct": body.get("fat_pct"),
        "withings_weight_date": body.get("date"),
        "withings_steps_today": steps_block.get("steps"),
        "withings_distance_km_today": steps_block.get("distance_km"),
        "withings_steps_date": steps_block.get("date"),
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
    """Return today's meals + totals (kcal, P, C, F) from nutrition_log.json.

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


if __name__ == "__main__":
    mcp.run(transport="stdio")
