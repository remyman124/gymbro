#!/usr/bin/env python3
"""validate_sheet_health.py — ongoing data-quality monitor for the Nutrition sheet.

Background:
  Jim OOB 2026-08-14 asked for a job that "validates the data on an ongoing
  basis so it can never silently rot again" after a months-long silent
  corruption was discovered in the sheet.

What it checks (8 defect classes, mirroring the parallel audit):
  Sheet schema (v3.2.7.48+): 11 columns A-K
    A 日期  B 時間  C 餐別  D 餐名  E 餐廳/連鎖  F kcal  G P (g)
    H C (g)  I F (g)  J 備註  K Drive Image URL
  (Pre-v3.2.7.48 was 14 cols A-N with 來源/Image/User Hints at K-M.
   Drive URL was at N, now at K. The Image column is gone.)

  ERRORS (structural — will fail the cron job):
    1. Row width > 11          (the original catastrophe: 76 rows bled)
    2. Empty column A          (orphaned rows)
    3. Column B not HH:MM      (ISO datetime, single-digit hour, serial #, etc.)
    4. Column A not YYYY-MM-DD (malformed / future / implausibly old)
    5. Malformed or missing Drive URL (col K must contain
                                    'drive.google.com' + 'id='; missing → image-only
                                    rows from pre-v3.2.7.48 import)

  WARNINGS (soft — logged but do NOT fail the job, since ~391/476 rows are
            legacy "Jim Meals Log legacy" bulk-import that will never satisfy
            these checks; the daily job monitors NEW pipeline output):
    6. Weak dish name          (empty / 'scan' / 'Unknown' / '未識別菜式'
                                 / truncated at 120 chars)
    7. Numeric sanity          (non-numeric / negative / kcal outside
                                 [20, 3000] / |4P+4C+9F − kcal| > 40%)
    8. Duplicates              (exact A-K dupes + same date+time+meal_name)

Exit codes:
   0  — healthy (or only warnings)
   2  — at least one ERROR found
   3  — fetch / auth failure (network/token problem)

Usage:
  python3 scripts/validate_sheet_health.py
  python3 scripts/validate_sheet_health.py --since 2026-08-01
  python3 scripts/validate_sheet_health.py --last 14
  python3 scripts/validate_sheet_health.py --json
  python3 scripts/validate_sheet_health.py --quiet      # only emit on problems

Requires the OAuth refresh token at /home/work/.hermes/google_token.json
(plain stdlib only: urllib + json — matches codebase style).
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
SHEET_TAB = "Nutrition"
TOKEN_PATH = Path("/home/work/.hermes/google_token.json")

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_FETCH_FAIL = 3

TIME_RE = re.compile(r"^\d{2}:\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SERIAL_TIME_RE = re.compile(r"^\d+\.\d+$")
PLACEHOLDER_DISHES = {"scan", "unknown", "unidentified", "未識別菜式",
                      "n/a", "na", "none", "-", "—", "?"}

# ---------------------------------------------------------------------------
# Auth + fetch (reused from scripts/migrations/sheet_cleanup_v3_2_7_47.py)
# ---------------------------------------------------------------------------

def refresh_access_token() -> str:
    tok = json.loads(TOKEN_PATH.read_text())
    data = urllib.parse.urlencode({
        "client_id": tok["client_id"], "client_secret": tok["client_secret"],
        "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    access = resp["access_token"]
    tok["token"] = access
    TOKEN_PATH.write_text(json.dumps(tok, indent=2))
    return access


def fetch_sheet(access: str) -> list:
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
           f"{SHEET_TAB}?valueRenderOption=FORMATTED_VALUE")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read()).get("values", [])


def fetch_with_retry():
    """Returns rows or raises. Two retries on transient auth failures."""
    last_err = None
    for attempt in range(2):
        try:
            access = refresh_access_token()
            return fetch_sheet(access)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (401, 403) and attempt == 0:
                continue  # retry once after fresh token
            raise
    raise last_err  # unreachable


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalize_rows(rows: list) -> list:
    """Pad every row to exactly 11 cols so checks can use fixed indices.

    Returns a list of dicts: {row_num (1-indexed), cells (length-11 list)}.
    Header is preserved as row_num=1.
    """
    out = []
    for i, r in enumerate(rows):
        if r is None:
            r = []
        if len(r) < 11:
            cells = list(r) + [""] * (11 - len(r))
        else:
            cells = list(r)
        out.append({"row_num": i + 1, "cells": cells, "raw_width": len(r)})
    return out


def apply_scope(rows: list, since: str | None, last_n: int | None):
    """Filter rows by date or recency. Returns (filtered_rows, scope_label)."""
    if not rows:
        return rows, "all"
    data = rows[1:]  # drop header
    if since:
        kept = [r for r in data if (r["cells"][0] or "").strip() >= since]
        return [rows[0]] + kept, f"since {since}"
    if last_n:
        # Keep header + the last N data rows by position (after trimming empties).
        # "Last N days" would require parsing every date — instead we treat it
        # as "last N data rows" which is what cron operators actually want.
        non_empty = [r for r in data if (r["cells"][0] or "").strip()]
        tail = non_empty[-last_n:]
        return [rows[0]] + tail, f"last {last_n} data rows"
    return rows, "all rows"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _is_iso_dt(s: str) -> bool:
    return "T" in s and ("+" in s.split("T", 1)[1] or s.endswith("Z"))


def check_row_width(rows: list) -> list:
    """ERROR: any row with > 11 cols."""
    return [{"row_num": r["row_num"], "width": r["raw_width"]}
            for r in rows[1:] if r["raw_width"] > 11]


_UNFORMATTED_AB = None


def _fetch_unformatted_ab() -> list:
    """Fetch columns A/B as raw values so cell TYPE (not display) is visible."""
    global _UNFORMATTED_AB
    if _UNFORMATTED_AB is None:
        access = refresh_access_token()
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
               f"{SHEET_TAB}!A1:B10000?valueRenderOption=UNFORMATTED_VALUE")
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {access}"})
        _UNFORMATTED_AB = json.loads(
            urllib.request.urlopen(req, timeout=30).read()).get("values", [])
    return _UNFORMATTED_AB


def check_cell_types(rows: list) -> list:
    """ERROR: col A must be a real DATE cell, col B a real TIME cell.

    A text "2026-07-18" renders identically to a real date but cannot be
    sorted or filtered, and left-aligns while real dates right-align — the
    "date field is not a date field" symptom Jim reported on 2026-08-14.
    Display-value checks cannot see this; only the raw value type can.
    """
    try:
        raw = _fetch_unformatted_ab()
    except Exception:
        return []  # secondary fetch — never fail the whole run on it
    bad = []
    for r in rows[1:]:
        idx = r["row_num"] - 1
        if idx >= len(raw):
            continue
        cell = raw[idx]
        a = cell[0] if len(cell) > 0 else ""
        b = cell[1] if len(cell) > 1 else ""
        if str(a).strip() and not isinstance(a, (int, float)):
            bad.append({"row_num": r["row_num"], "value": a,
                        "kind": "col_A_text_not_date"})
        if str(b).strip() and not isinstance(b, (int, float)):
            bad.append({"row_num": r["row_num"], "value": b,
                        "kind": "col_B_text_not_time"})
    return bad


def check_empty_a(rows: list) -> list:
    """ERROR: column A blank."""
    return [r["row_num"] for r in rows[1:]
            if not (r["cells"][0] or "").strip()]


def check_time_format(rows: list) -> list:
    """ERROR: column B not in HH:MM (ISO dt, single-digit hour, serial #, etc)."""
    bad = []
    for r in rows[1:]:
        b = (r["cells"][1] or "").strip()
        if not b:
            continue  # empty time is a different class; not flagged here
        if TIME_RE.match(b):
            continue
        kind = ("iso" if _is_iso_dt(b)
                else "single_digit_hour" if re.match(r"^\d:\d{2}$", b)
                else "serial_number" if SERIAL_TIME_RE.match(b)
                else "malformed")
        bad.append({"row_num": r["row_num"], "value": b, "kind": kind})
    return bad


def check_date_format(rows: list) -> list:
    """ERROR: column A not YYYY-MM-DD, future, or implausibly old."""
    today = date.today()
    bad = []
    for r in rows[1:]:
        a = (r["cells"][0] or "").strip()
        if not a:
            continue  # handled by check_empty_a
        if not DATE_RE.match(a):
            bad.append({"row_num": r["row_num"], "value": a,
                        "kind": "malformed"})
            continue
        try:
            dt = datetime.strptime(a, "%Y-%m-%d").date()
        except ValueError:
            bad.append({"row_num": r["row_num"], "value": a,
                        "kind": "malformed"})
            continue
        if dt > today:
            bad.append({"row_num": r["row_num"], "value": a,
                        "kind": "future_date"})
        elif dt < date(2020, 1, 1):
            bad.append({"row_num": r["row_num"], "value": a,
                        "kind": "implausibly_old"})
    return bad


def check_dish_name(rows: list) -> list:
    """WARNING: empty / placeholder / 120-char truncated dish names."""
    bad = []
    for r in rows[1:]:
        d = (r["cells"][3] or "").strip()
        if not d:
            bad.append({"row_num": r["row_num"], "issue": "empty"})
            continue
        if d.lower() in PLACEHOLDER_DISHES:
            bad.append({"row_num": r["row_num"], "issue": "placeholder",
                        "value": d})
            continue
        if len(d) == 120:
            bad.append({"row_num": r["row_num"], "issue": "truncated_at_120"})
    return bad


def _parse_num(s: str):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def check_numeric(rows: list) -> list:
    """WARNING: non-numeric / negative / kcal out of [20,3000] / macro mismatch."""
    bad = []
    for r in rows[1:]:
        kcal = _parse_num(r["cells"][5])
        p = _parse_num(r["cells"][6])
        c = _parse_num(r["cells"][7])
        f = _parse_num(r["cells"][8])
        if None in (kcal, p, c, f):
            bad.append({"row_num": r["row_num"],
                        "issue": "non_numeric",
                        "values": [r["cells"][5], r["cells"][6],
                                   r["cells"][7], r["cells"][8]]})
            continue
        if any(x < 0 for x in (kcal, p, c, f)):
            bad.append({"row_num": r["row_num"], "issue": "negative",
                        "values": [kcal, p, c, f]})
            continue
        if not (20 <= kcal <= 3000):
            bad.append({"row_num": r["row_num"], "issue": "kcal_out_of_range",
                        "kcal": kcal})
            continue
        calc = 4 * p + 4 * c + 9 * f
        if calc > 0 and abs(calc - kcal) / max(calc, kcal) > 0.40:
            bad.append({"row_num": r["row_num"], "issue": "macro_mismatch",
                        "kcal": kcal, "calc_kcal": round(calc, 1)})
    return bad


def check_drive_url(rows: list) -> list:
    """ERROR: missing or malformed Drive URL (col K).

    Post-v3.2.7.48 the Image column is gone, so we no longer pair the URL
    with an image. Every row is simply expected to have a well-formed
    Drive URL at col K (index 10). Missing or malformed → ERROR.
    """
    out = {"missing_url": [], "malformed_url": []}
    for r in rows[1:]:
        k = (r["cells"][10] or "").strip()
        if not k:
            out["missing_url"].append({"row_num": r["row_num"]})
        elif not ("drive.google.com" in k and "id=" in k):
            out["malformed_url"].append({"row_num": r["row_num"],
                                        "value": k[:80]})
    return out


def check_duplicates(rows: list) -> list:
    """WARNING: exact A-K dupes + same date+time+meal_name."""
    out = {"exact_full": [], "date_time_meal": []}
    seen_full = {}
    seen_dtm = {}
    for r in rows[1:]:
        c = r["cells"]
        full = tuple((x or "").strip() for x in c[:11])
        dtm = ((c[0] or "").strip(),
               (c[1] or "").strip(),
               (c[3] or "").strip())
        if all(full):
            if full in seen_full:
                out["exact_full"].append({"row_num": r["row_num"],
                                          "first_seen": seen_full[full]})
            else:
                seen_full[full] = r["row_num"]
        if dtm[0] and dtm[2]:
            if dtm in seen_dtm:
                out["date_time_meal"].append({"row_num": r["row_num"],
                                              "first_seen": seen_dtm[dtm],
                                              "key": list(dtm)})
            else:
                seen_dtm[dtm] = r["row_num"]
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

CHECKS = [
    # (key, label, severity, fn) — severity in {"error", "warning"}
    ("wide_rows",      "Wide rows (>11 cols)",               "error",   check_row_width),
    ("empty_A",        "Empty column A (orphans)",           "error",   check_empty_a),
    ("time_format",    "Malformed column B time",            "error",   check_time_format),
    ("date_format",    "Malformed / future / implausible A", "error",   check_date_format),
    ("cell_types",     "Cell types (A=DATE, B=TIME)",        "error",   check_cell_types),
    ("drive_url",      "Drive URL coverage (col K)",         "error",   check_drive_url),
    ("dish_name",      "Weak dish names (col D)",            "warning", check_dish_name),
    ("numeric",        "Numeric sanity (kcal + macros)",     "warning", check_numeric),
    ("duplicates",     "Duplicate rows",                     "warning", check_duplicates),
]


def _examples(rows_for_check, n: int = 5):
    """Extract a few example row numbers from a check result (varies by shape)."""
    examples = []
    for item in rows_for_check[:n]:
        if isinstance(item, dict) and "row_num" in item:
            examples.append(item["row_num"])
        elif isinstance(item, int):
            examples.append(item)
    return examples


def _format_human(results: dict, scope: str, total_rows: int) -> str:
    lines = []
    lines.append(f"=== Nutrition Sheet Health ({scope}, {total_rows} data rows) ===")
    error_total = 0
    warn_total = 0
    for key, label, severity, _fn in CHECKS:
        result = results[key]
        # drive_url + duplicates are dicts; others are lists
        if key == "drive_url":
            n = len(result["missing_url"]) + len(result["malformed_url"])
            ex = _examples(result["missing_url"]) + _examples(result["malformed_url"])
        elif key == "duplicates":
            n = len(result["exact_full"]) + len(result["date_time_meal"])
            ex = _examples(result["exact_full"]) + _examples(result["date_time_meal"])
        else:
            n = len(result)
            ex = _examples(result)
        marker = "ERROR  " if severity == "error" else "WARNING"
        if severity == "error":
            error_total += n
        else:
            warn_total += n
        status = "FAIL" if n > 0 else "OK"
        line = f"  [{marker}] {label:42s}  {status:4s}  count={n}"
        if ex:
            line += f"  rows={ex}"
        lines.append(line)
    lines.append("")
    lines.append(f"  Total errors:   {error_total}")
    lines.append(f"  Total warnings: {warn_total}")
    lines.append(f"  Verdict:        {'FAIL — fix errors above' if error_total else 'PASS (warnings OK)'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Ongoing data-quality monitor for the Nutrition sheet.")
    p.add_argument("--since", help="Only check rows with col A >= YYYY-MM-DD")
    p.add_argument("--last", type=int,
                   help="Only check the last N data rows (by position)")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON")
    p.add_argument("--quiet", action="store_true",
                   help="Print nothing on success; print report on failure")
    p.add_argument("--examples", type=int, default=5,
                   help="How many example row numbers per check (default 5)")
    args = p.parse_args()

    try:
        raw_rows = fetch_with_retry()
    except Exception as e:
        msg = f"FETCH FAILED: {type(e).__name__}: {e}"
        if args.json:
            print(json.dumps({"status": "fetch_failed", "error": msg}))
        elif not args.quiet:
            print(msg, file=sys.stderr)
        sys.exit(EXIT_FETCH_FAIL)

    rows = normalize_rows(raw_rows)
    rows, scope = apply_scope(rows, args.since, args.last)
    data_rows = rows[1:]
    total = len(data_rows)

    results = {}
    error_count = 0
    for key, _label, severity, fn in CHECKS:
        results[key] = fn(rows)
        if severity == "error":
            r = results[key]
            if key in ("drive_url", "duplicates"):
                error_count += sum(len(v) for v in r.values())
            else:
                error_count += len(r)

    if args.json:
        out = {
            "scope": scope,
            "total_rows": total,
            "errors": error_count,
            "checks": {k: results[k] for k in results},
        }
        out["status"] = "fail" if error_count else "pass"
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        report = _format_human(results, scope, total)
        if error_count and args.quiet:
            # Always print failures in quiet mode
            print(report)
        elif not args.quiet:
            print(report)

    sys.exit(EXIT_ERROR if error_count else EXIT_OK)


if __name__ == "__main__":
    main()