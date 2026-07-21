#!/usr/bin/env python3
"""
Workout Formatter — Text rendering for workout logs.

Jim OOB 2026-07-22: Refactored after Whoop AI paste mis-interpreted set counts.
Root cause investigation:
  1. Sync sheet dedup bug: re-syncing would push duplicate set rows; net
     effect was inflated sheet. We dedupe by (date, exercise, set_n) tuple.
  2. Sheet format: each new exercise group resets sheet "set_n" to 1, so a
     single workout with N exercises has sheet_n restarting N times. Old
     output exposed sheet_n verbatim, which Whoop's parser could not
     disambiguate.
  3. Parser confusion: emoji headers (💪📅🏋📊🎯), × multiplication, and
     Unicode middle-dot bullets confused Whoop's AI segmenter.

Fix: whoop_text_v2 default uses ALL-CAPS keywords + "X OF Y" framing +
    empirical exercise group detection via set_n reset detection. This is
    unambiguous to any AI parser and reads naturally to humans.

Source of truth. Single responsibility: rows in → text out.
"""

from typing import Iterable


# ---------- Public formats ----------

def render(rows: list[dict], fmt: str = "whoop_text", *,
           date_filter_label: str = "Workout",
           total_volume: float = 0.0,
           muscle_split: dict | None = None) -> str:
    if fmt == "whoop_text" or fmt == "whoop_text_v2":
        return _render_whoop_text_v2(rows)
    if fmt == "whoop_emoji":
        return _render_whoop_emoji(rows, date_filter_label, total_volume, muscle_split or {})
    raise ValueError(f"unknown fmt: {fmt!r}")


# ---------- Dedup & grouping ----------

def dedupe(rows: list[dict]) -> list[dict]:
    """Remove duplicate rows by (date, exercise, set_n) key.
    Keep first occurrence; preserve order. Critical for sheet that has
    been re-synced multiple times."""
    seen = set()
    out = []
    for r in rows:
        if not r:
            continue
        d = (r.get("date"), r.get("exercise", ""), r.get("set_n"))
        if d in seen:
            continue
        seen.add(d)
        out.append(r)
    return out


def group_exercises(rows: list[dict]) -> list[dict]:
    """Split a session's flat row list into exercise groups.
    Group boundary detection: sheet restarts set_n at 1 for each new
    exercise, so whenever we see set_n <= previous set_n we infer a new
    exercise group. Time-ordered within group.
    """
    groups = []
    current = None  # {"name": str, "sets": [row, ...]}
    for r in rows:
        ex = (r.get("exercise") or "").strip()
        set_n = r.get("set_n") or 0
        if current is None:
            current = {"name": ex, "sets": [r]}
            continue
        # Boundary conditions: exercise name different, OR set_n reset
        if ex != current["name"]:
            groups.append(current)
            current = {"name": ex, "sets": [r]}
        elif set_n <= (current["sets"][-1].get("set_n") or 0):
            # Same exercise name but set_n dropped → new group of same ex
            # (rare; happens if user re-logs the same exercise block later)
            groups.append(current)
            current = {"name": ex, "sets": [r]}
        else:
            current["sets"].append(r)
    if current:
        groups.append(current)
    return groups


# ---------- Weight formatter ----------

def _fmt_weight(weight) -> str:
    if not weight:
        return "BW"
    try:
        w = float(weight)
    except (TypeError, ValueError):
        return "BW"
    if w == int(w):
        return f"{int(w)} kg"
    return f"{w} kg"


# ---------- whoop_text_v2 (DEFAULT for copyDay) ----------

def _render_whoop_text_v2(rows: list[dict]) -> str:
    """All-caps keywords + "X OF Y" framing. Empirically disambiguates
    for both Whoop AI parser and humans.

    Example output:
        WORKOUT LOG 2026-07-20
        TOTAL EXERCISES: 5
        TOTAL SETS: 38
        TOTAL VOLUME: 11150 kg

        EXERCISE 1 OF 5: BB BENCH PRESS
        SET 1 OF 5 FOR THIS EXERCISE: 40 kg x 10 reps
        SET 2 OF 5 FOR THIS EXERCISE: 40 kg x 10 reps
        ...

        EXERCISE 2 OF 5: LOW ROW (CABLE)
        SET 1 OF 3 FOR THIS EXERCISE: 25 kg x 10 reps
        ...

        END WORKOUT LOG
    """
    rows = dedupe(rows)
    total_sets = len(rows)
    total_volume = sum((r.get("volume_kg") or 0) for r in rows)

    # Group by date first; if only one date, skip the date header.
    by_date = {}
    for r in rows:
        by_date.setdefault(r.get("date"), []).append(r)
    dates = sorted(by_date.keys(), reverse=True)

    out = []
    for di, date in enumerate(dates):
        if date:
            out.append(f"WORKOUT LOG {date}")
        date_rows = by_date[date]
        groups = group_exercises(date_rows)
        out.append(f"TOTAL EXERCISES: {len(groups)}")
        out.append(f"TOTAL SETS: {len(date_rows)}")
        out.append(f"TOTAL VOLUME: {round(total_volume, 1)} kg")
        out.append("")

        for gi, g in enumerate(groups, start=1):
            gsets = g["sets"]
            out.append(f"EXERCISE {gi} OF {len(groups)}: {g['name'].upper()}")
            for si, s in enumerate(gsets, start=1):
                w = _fmt_weight(s.get("weight_kg"))
                reps = s.get("reps", 0)
                out.append(
                    f"SET {si} OF {len(gsets)} FOR THIS EXERCISE: {w} x {reps} reps"
                )
            out.append("")

        out.append("END WORKOUT LOG")
    return "\n".join(out)


# ---------- whoop_emoji (chat rooms / Obsidian / aesthetic legacy) ----------

def _render_whoop_emoji(rows, date_filter_label, total_volume, muscle_split) -> str:
    """Emoji-style output for human-readable chat copy, NOT for AI paste."""
    rows = dedupe(rows)
    total_volume = round(total_volume, 1)
    lines = [f"💪 Workout Log — {date_filter_label}", ""]
    abs_set = 0
    prev_ex = None
    by_date = {}
    for r in rows:
        by_date.setdefault(r.get("date"), []).append(r)
    for date in sorted(by_date.keys(), reverse=True):
        date_rows = by_date[date]
        date_volume = round(sum((r.get("volume_kg") or 0) for r in date_rows), 1)
        lines.append(f"📅 {date}  ·  {len(date_rows)} sets · {date_volume}kg volume")
        for r in date_rows:
            ex = (r.get("exercise") or "").strip()
            weight_str = _fmt_weight(s.get("weight_kg") if False else r.get("weight_kg")) if False else _fmt_weight(r.get("weight_kg"))
            reps = r.get("reps", 0)
            if ex and ex != prev_ex:
                if prev_ex is not None:
                    lines.append("")
                lines.append(f"  🏋 {ex}")
                prev_ex = ex
            abs_set += 1
            lines.append(f"  Set {abs_set} · {weight_str} × {reps}")
        lines.append("")
        prev_ex = None
    lines.append(f"📊 Totals: {len(rows)} sets · {total_volume}kg volume")
    if muscle_split:
        muscles = " · ".join(
            f"{k.upper()} {v}" for k, v in sorted(muscle_split.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"🎯 Muscle split: {muscles}")
    lines.append("")
    lines.append("End of session.")
    return "\n".join(lines)
