#!/usr/bin/env python3
"""
Workout Formatter — Text rendering for workout logs.

Extracted from gym_web.py /api/export_text (Jim OOB 2026-07-22).
Source of truth: this module. /api/export_text is now a thin caller.
Single responsibility: turn structured rows into AI-friendly text formats.

Whoop AI paste reliability (root cause investigation):
- "Set 1" resets per exercise in sheet → Whoop collapses set boundaries.
- Emojis (💪📅🏋📊🎯), × multiplication sign, Unicode middle-dot (·)
  confused Whoop AI parser when interleaved with English/punctuation.

Two modes:
- whoop_text (DEFAULT, copyDay() default): plain text for AI ingestion.
  No emojis, no ×, no ·. Uses English labels ("Exercise:", "Set N",
  "Sets", "Total volume", "End"). Single decimal of reasoning ambiguity,
  stable whitespace boundaries.
- whoop_emoji: chat-AI friendly, with ⛹ / 📅 / 📊 markers. For Obsidian,
  WhatsApp, chat rooms where emojis are aesthetic not semantic.
"""

from typing import Iterable


# ---------- Public formats ----------

def render(rows: list[dict], fmt: str = "whoop_text", *,
           date_filter_label: str = "Workout",
           total_volume: float = 0.0,
           muscle_split: dict | None = None) -> str:
    """Dispatch to the right format renderer.

    rows: list of dicts with at least {"date", "exercise", "weight_kg",
           "reps", "set_n"}. Muscles split is computed upstream.
    fmt: "whoop_text" | "whoop_emoji" | "json" | "md"
    """
    if fmt == "whoop_text":
        return _render_whoop_text(rows, date_filter_label=date_filter_label,
                                  total_volume=total_volume,
                                  muscle_split=muscle_split or {})
    if fmt == "whoop_emoji":
        return _render_whoop_emoji(rows, date_filter_label=date_filter_label,
                                   total_volume=total_volume,
                                   muscle_split=muscle_split or {})
    raise ValueError(f"unknown fmt: {fmt!r}")


# ---------- Internal helpers ----------

def _fmt_weight(weight) -> str:
    """Format weight as '40kg' / '22.5kg' / 'BW' (bodyweight) — no decimals on whole numbers."""
    if not weight:
        return "BW"
    try:
        w = float(weight)
    except (TypeError, ValueError):
        return "BW"
    if w == int(w):
        return f"{int(w)}kg"
    return f"{w}kg"


def _group_by_date(rows: Iterable[dict]) -> dict:
    """Yield {date: [row, row, ...]} preserving source order within each date."""
    by = {}
    for r in rows:
        if not r or not r.get("date"):
            continue
        by.setdefault(r["date"], []).append(r)
    return by


# ---------- Whoop text (DEFAULT — pure ASCII / English / no markers) ----------

def _render_whoop_text(rows, *, date_filter_label, total_volume, muscle_split) -> str:
    """Plain text output. AI-friendly: no emojis, no Unicode bullets,
    no × multiplication sign. Absolute set numbering across full session,
    blank line between exercise groups, stable English labels.
    """
    lines = [
        f"Workout Log - {date_filter_label}",
        "",
    ]
    abs_set = 0
    prev_ex = None
    by_date = _group_by_date(rows)
    for date in sorted(by_date.keys(), reverse=True):
        date_rows = by_date[date]
        date_volume = round(sum((r.get("volume_kg") or 0) for r in date_rows), 1)
        lines.append(f"Date: {date}")
        lines.append(f"Sets: {len(date_rows)}")
        lines.append(f"Volume: {date_volume}kg")
        lines.append("")
        for r in date_rows:
            ex = (r.get("exercise") or "").strip()
            weight_str = _fmt_weight(r.get("weight_kg"))
            reps = r.get("reps", 0)
            # Exercise boundary detection: sheet rows reset Set 1 per exercise,
            # so a real new exercise shows up as a different exercise name.
            if ex and ex != prev_ex:
                if prev_ex is not None:
                    lines.append("")  # blank line between exercise groups
                lines.append(f"Exercise: {ex}")
                prev_ex = ex
            abs_set += 1
            sheet_set_n = r.get("set_n")
            sheet_label = f" (sheet set {sheet_set_n})" if sheet_set_n else ""
            lines.append(f"  Set {abs_set}{sheet_label}: {ex} - {weight_str}, {reps} reps")
        lines.append("")
        prev_ex = None  # reset between dates

    lines.append(f"Total sets: {len(rows)}")
    lines.append(f"Total volume: {round(total_volume, 1)}kg")
    if muscle_split:
        muscles = ", ".join(
            f"{k.upper()}={v}"
            for k, v in sorted(muscle_split.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"Muscle split: {muscles}")
    lines.append("")
    lines.append("End of workout log.")
    return "\n".join(lines)


# ---------- Whoop emoji (chat rooms / Obsidian / aesthetic mode) ----------

def _render_whoop_emoji(rows, *, date_filter_label, total_volume, muscle_split) -> str:
    """Emoji-style output. For human-readable chat copy, NOT for AI paste.
    Preserves the v18 visual format (📅 dates, 🏋 exercises, 💪 header).
    """
    lines = [
        f"💪 Workout Log — {date_filter_label}",
        "",
    ]
    abs_set = 0
    prev_ex = None
    by_date = _group_by_date(rows)
    for date in sorted(by_date.keys(), reverse=True):
        date_rows = by_date[date]
        date_volume = round(sum((r.get("volume_kg") or 0) for r in date_rows), 1)
        lines.append(f"📅 {date}  ·  {len(date_rows)} sets · {date_volume}kg volume")
        for r in date_rows:
            ex = (r.get("exercise") or "").strip()
            weight_str = _fmt_weight(r.get("weight_kg"))
            reps = r.get("reps", 0)
            if ex and ex != prev_ex:
                if prev_ex is not None:
                    lines.append("")
                lines.append(f"  🏋 {ex}")
                prev_ex = ex
            abs_set += 1
            sheet_set_n = r.get("set_n")
            sheet_label = f" (was {sheet_set_n})" if sheet_set_n else ""
            lines.append(f"  Set {abs_set}{sheet_label} · {weight_str} × {reps}")
        lines.append("")
        prev_ex = None
    lines.append(f"📊 Totals: {len(rows)} sets · {round(total_volume, 1)}kg volume")
    if muscle_split:
        muscles = " · ".join(
            f"{k.upper()} {v}"
            for k, v in sorted(muscle_split.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"🎯 Muscle split: {muscles}")
    lines.append("")
    lines.append("End of session.")
    return "\n".join(lines)
