#!/usr/bin/env python3
"""reconcile_drive_urls.py — fill column K (Drive Image URL) for rows that
were missed on initial commit.

Jim OOB 2026-08-14 (audit followup): the Nutrition sheet has rows whose
column K is empty or malformed (e.g. the literal "30" that bled in).
This script reconciles them.

Schema (v3.2.7.48+): 11 columns A-K. Drive Image URL is at K (index 10).
Pre-v3.2.7.48 was 14 cols A-N with the URL at N; the Image column (L) is
gone, so any "image-with-no-URL" pairing is now a simple "missing-URL-K".

Source priority (per row):
  1. /home/work/.hermes/drive_uploads_2026-08-09.json and _v2.json — already
     uploaded IDs from a one-off script. NO re-upload; just write URL.
  2. food_scan_log.json (covers 2026-07-24 → 2026-08-07 only) — entries
     may already carry drive_image_url from earlier auto-commit retries.
  3. Otherwise resolve the local JPEG by (date, time) → scan_YYYYMMDD_HHMMSS.jpg
     in /home/work/.hermes/scan_cache/ and upload.

A pending-uploads queue at /home/work/.hermes/drive_pending_uploads.json
is drained too — entries written there by _upload_to_drive when the upload
fails, so they can be retried later. This makes the pipeline fail-loud
instead of fail-silent.

Idempotent:
  - Rows whose K is already a well-formed drive.google.com URL are skipped.
  - Rows whose K is malformed (e.g. "30") are treated as "needs write".
  - Uploaded IDs from drive_uploads_2026-08-09*.json are NOT re-uploaded.
  - Re-running with --dry-run is safe (reads only).

Usage:
  python3 scripts/reconcile_drive_urls.py [--dry-run] [--use-snapshot PATH]

Options:
  --dry-run           Print plan only — no API writes, no Drive uploads.
                      Default for safety.
  --use-snapshot PATH Load sheet rows from a local JSON snapshot instead of
                      fetching live (offline analysis). Defaults to
                      /tmp/nutrition_sheet_current.json if present, else live.
  --rate N            Seconds between uploads (default 1.0).
  --yes               Skip confirmation prompt in live mode.

Pattern reused from scripts/migrations/sheet_cleanup_v3_2_7_47.py:
token refresh, fetch_sheet, batch_update, upload_to_drive. Stdlib only.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# --- Constants (mirror gym_web.py / sheet_cleanup_v3_2_7_47.py) ----------

HOME = Path("/home/work/.hermes")
SCAN_CACHE_DIR = HOME / "scan_cache"
SCAN_LOG_PATH = HOME / "food_scan_log.json"
TOKEN_PATH = HOME / "google_token.json"
DRIVE_UPLOADS_V1 = HOME / "drive_uploads_2026-08-09.json"
DRIVE_UPLOADS_V2 = HOME / "drive_uploads_2026-08-09_v2.json"
PENDING_QUEUE = HOME / "drive_pending_uploads.json"
DRIVE_ERROR_LOG = Path("/tmp/drive_upload_errors.log")
SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
SHEET_TAB = "Nutrition"
DEFAULT_SNAPSHOT = Path("/tmp/nutrition_sheet_current.json")
BACKUP_SNAPSHOT = Path("/tmp/nutrition_sheet_backup_20260814_064540.json")

# Drive URL regex — `id=...` is the load-bearing part. Catches "30", empty
# strings, garbage bleed-in values, etc.
DRIVE_URL_RE = re.compile(
    r"^https?://drive\.google\.com/.*[?&]id=[\w-]+", re.IGNORECASE
)

EXPECTED_COLS = 11  # columns A through K (v3.2.7.48+ schema)


# --- Helpers (adapted from sheet_cleanup_v3_2_7_47.py) ------------------

def _pad_row(r: list) -> list:
    """Pad a row to exactly 11 cols so checks can use fixed indices."""
    r = list(r) if r else []
    if len(r) < EXPECTED_COLS:
        return r + [""] * (EXPECTED_COLS - len(r))
    return r[:EXPECTED_COLS]


def refresh_access_token() -> str:
    """Refresh OAuth access token; returns "" on failure."""
    try:
        tok = json.loads(TOKEN_PATH.read_text())
        if not tok.get("refresh_token"):
            return ""
        data = urllib.parse.urlencode({
            "client_id": tok["client_id"],
            "client_secret": tok["client_secret"],
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        access = resp["access_token"]
        tok["token"] = access
        TOKEN_PATH.write_text(json.dumps(tok, indent=2))
        return access
    except Exception:
        return ""


def fetch_sheet(access: str) -> list:
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
        f"/values/{SHEET_TAB}?valueRenderOption=FORMATTED_VALUE"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read()).get("values", [])


def batch_update(access: str, edits: list, max_per_call: int = 500) -> int:
    n_done = 0
    for start in range(0, len(edits), max_per_call):
        chunk = edits[start:start + max_per_call]
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
            f"/values:batchUpdate?valueInputOption=USER_ENTERED"
        )
        body = {"valueInputOption": "USER_ENTERED", "data": chunk}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/json",
            },
        )
        urllib.request.urlopen(req, timeout=60).read()
        n_done += len(chunk)
    return n_done


def upload_to_drive(local_path: Path, access: str) -> str:
    """Upload JPEG to Drive and return the public URL. "" on failure."""
    if not local_path.exists():
        return ""
    try:
        boundary = uuid.uuid4().hex
        file_data = local_path.read_bytes()
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps({'name': local_path.name, 'mimeType': 'image/jpeg'})}\r\n"
            f"--{boundary}\r\n"
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode() + file_data + (f"\r\n--{boundary}--").encode()
        req = urllib.request.Request(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
            data=body,
            headers={
                "Authorization": f"Bearer {access}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            file_id = json.loads(r.read()).get("id")
        if not file_id:
            return ""
        # Make public-readable (best-effort).
        try:
            perm_req = urllib.request.Request(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
                data=json.dumps({"role": "reader", "type": "anyone"}).encode(),
                headers={
                    "Authorization": f"Bearer {access}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            urllib.request.urlopen(perm_req, timeout=10).read()
        except Exception:
            pass
        return f"https://drive.google.com/uc?export=view&id={file_id}"
    except Exception as e:
        # Best-effort log; never raise from here.
        try:
            with open(DRIVE_ERROR_LOG, "a") as _f:
                _f.write(
                    f"{time.time():.0f} | reconcile | {type(e).__name__}: {e}"
                    f" | path={local_path}\n"
                )
        except Exception:
            pass
        return ""


# --- Recorded-ID lookup -----------------------------------------------

def load_recorded_drive_urls() -> dict:
    """Return {(date, HHMM|name_lower): url} from the two one-off upload logs.

    The two JSON files have slightly different shapes — v1 keyed by upload
    label with metadata, v2 keyed by HHMMSS with food label. We map both
    into a flat table the sheet-side matcher can look up by
    (date, time-from-column-B) OR (date, dish-name-prefix).
    """
    # name_keys is separate so we can fall back to it when time doesn't match
    # (e.g. row 443 is 11:32 but its recorded file was named scan_20260809_144115.jpg).
    out: dict = {}
    name_keys: dict = {}  # (date, label_lower) → url — for substring match
    name_keys_short: dict = {}  # (date, label[:8].lower()) → url
    if DRIVE_UPLOADS_V1.exists():
        try:
            data = json.loads(DRIVE_UPLOADS_V1.read_text())
            for u in data.get("uploads", []):
                local = u.get("local", "")
                url = u.get("public_url", "")
                label = u.get("label", "")
                if not (local and url):
                    continue
                # The local filename encodes the date+time, e.g.
                # scan_20260809_144115.jpg or scan_20260809_165807.jpg.
                name = Path(local).name
                m = re.match(r"scan_(\d{8})_(\d{4,6})\.jpg$", name)
                if m:
                    date = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}"
                    hhmmss = m.group(2)
                    out[(date, hhmmss[:4])] = url
                    out[(date, hhmmss)] = url
                # Also index by remote_name which carries HHMMSS too.
                rn = u.get("remote_name", "")
                m2 = re.match(r"scan_(\d{8})_(\d{4,6})", rn)
                if m2:
                    date = f"{m2.group(1)[:4]}-{m2.group(1)[4:6]}-{m2.group(1)[6:8]}"
                    out[(date, m2.group(2)[:4])] = url
                    out[(date, m2.group(2))] = url
                # Index by (date, label_lower) — for substring match when sheet
                # column-B time doesn't match filename time (e.g. row 443 is
                # 11:32 but its recorded file was named scan_20260809_144115.jpg).
                if label and m:
                    name_keys[(date, label.lower())] = url
                    name_keys_short[(date, label[:8].lower())] = url
        except Exception as e:
            print(f"  WARN: failed to parse {DRIVE_UPLOADS_V1}: {e}")
    if DRIVE_UPLOADS_V2.exists():
        try:
            data = json.loads(DRIVE_UPLOADS_V2.read_text())
            for hhmmss, entry in data.items():
                url = entry.get("url", "")
                if not url:
                    continue
                name = entry.get("name", "")  # scan_20260809_165807.jpg
                food = entry.get("food", "")
                m = re.match(r"scan_(\d{8})_(\d{4,6})", name)
                if m:
                    date = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}"
                    out[(date, hhmmss)] = url
                    out[(date, hhmmss[:4])] = url
                    if food:
                        name_keys[(date, food.lower())] = url
                        name_keys_short[(date, food[:8].lower())] = url
        except Exception as e:
            print(f"  WARN: failed to parse {DRIVE_UPLOADS_V2}: {e}")
    # Merge name-keys as a fallback — they only win when no time match
    # exists, so we keep them in a parallel dict.
    out["__name_keys__"] = name_keys
    out["__name_keys_short__"] = name_keys_short
    return out


def load_scan_log_index() -> dict:
    """Return {(date, HHMM): drive_image_url} from food_scan_log.json.

    The log only covers 2026-07-24 → 2026-08-07 — out-of-range dates
    return no entry.
    """
    out: dict = {}
    if not SCAN_LOG_PATH.exists():
        return out
    try:
        log = json.loads(SCAN_LOG_PATH.read_text())
    except Exception:
        return out
    for e in log:
        d = e.get("date", "")
        url = e.get("drive_image_url", "")
        if not (d and url and DRIVE_URL_RE.match(url)):
            continue
        t = (e.get("time") or "")[:5]
        if t:
            out[(d, t)] = url
        # Also try HHMM from timestamp_iso.
        ts = e.get("timestamp_iso", "")
        m = re.search(r"T(\d{2}:\d{2})", ts)
        if m:
            out[(d, m.group(1))] = url
    return out


def resolve_local_image(date: str, time_str: str) -> Path:
    """Best-effort: scan_YYYYMMDD_HHMMSS.jpg or scan_YYYYMMDD_HHMM.jpg in scan_cache."""
    if not (date and time_str):
        return None
    date_compact = date.replace("-", "")
    # Column B may be HH:MM or HH:MM:SS — strip seconds.
    hhmmss = re.sub(r"[^0-9]", "", time_str)
    if len(hhmmss) == 4:
        hhmmss_padded = hhmmss + "00"
    elif len(hhmmss) == 6:
        hhmmss_padded = hhmmss
    else:
        return None
    candidates = [
        SCAN_CACHE_DIR / f"scan_{date_compact}_{hhmmss_padded}.jpg",
        SCAN_CACHE_DIR / f"scan_{date_compact}_{hhmmss}.jpg",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


# --- Pending queue ------------------------------------------------------

def load_pending_queue() -> list:
    """Read /home/work/.hermes/drive_pending_uploads.json.

    The queue is a JSON list of {ts, sheet_row, date, time, local_path,
    reason}. _upload_to_drive appends to it when the upload fails.
    """
    if not PENDING_QUEUE.exists():
        return []
    try:
        return json.loads(PENDING_QUEUE.read_text())
    except Exception:
        return []


def save_pending_queue(items: list):
    """Atomic write — tmp + rename to avoid half-written queue on crash."""
    tmp = PENDING_QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    tmp.replace(PENDING_QUEUE)


# --- Core: build the plan ---------------------------------------------

def _row_needs_reconciliation(r: list) -> bool:
    """True if this row's Drive URL (col K) is empty or malformed.

    Post-v3.2.7.48 the Image column is gone, so we no longer gate on
    "image-with-no-URL"; any missing or malformed K is a candidate.
    """
    r = _pad_row(r)
    K = (r[10] or "").strip()
    if not K:
        return True
    if not DRIVE_URL_RE.match(K):
        return True
    return False


def _normalize_date(s: str) -> str:
    return (s or "").strip()[:10]


def _normalize_time(s: str) -> str:
    """Strip ISO datetime to HH:MM."""
    if not s:
        return ""
    if "T" in s:
        s = s.split("T", 1)[1]
    return s[:5]


def build_plan(rows: list, recorded: dict, scan_log_idx: dict, queue: list):
    """Return (to_write, to_upload, unrecoverable, pending_drain).

    to_write     = list of (sheet_row, url) — URLs known, just need N written
    to_upload    = list of (sheet_row, local_path) — need a real upload
    unrecoverable = list of (sheet_row, reason) — no local image found
    pending_drain = list of (sheet_row, local_path) — from pending queue
    """
    to_write = []
    to_upload = []
    unrecoverable = []

    for i, r in enumerate(rows[1:], start=2):
        if not _row_needs_reconciliation(r):
            continue
        r = _pad_row(r)
        date = _normalize_date(r[0])
        time_short = _normalize_time(r[1])
        name = (r[3] or "").strip()

        # 1. Recorded drive URLs (one-off scripts) — try time first, then
        # fall back to dish-name match (handles row 443 where sheet column-B
        # time 11:32 doesn't match the recorded file's filename 144115).
        url = recorded.get((date, time_short))
        if not url:
            name_keys = recorded.get("__name_keys_short__", {})
            url = name_keys.get((date, name[:8].lower()))
            if not url:
                # Substring fallback: a label like "NOC 牛油果炒蛋多士"
                # should still match sheet "牛油果炒蛋多士". Try the longer
                # string contains the shorter (any direction).
                full_keys = recorded.get("__name_keys__", {})
                norm_name = name.lower().strip()
                for (kd, kn), u in full_keys.items():
                    if kd != date or not kn:
                        continue
                    if (norm_name and norm_name in kn) or kn in norm_name:
                        url = u
                        break
        if not url:
            url = scan_log_idx.get((date, time_short))
        if url:
            to_write.append((i, url, "recorded_id"))
            continue

        # 2. Resolve local file by (date, time).
        local = resolve_local_image(date, time_short)
        if local and local.exists():
            to_upload.append((i, local, date, time_short, name))
        else:
            unrecoverable.append((i, date, time_short, name, "no_local_file"))

    # Pending queue (independent of sheet scan; same upload helper applies).
    pending_drain = []
    for entry in queue:
        p = Path(entry.get("local_path", ""))
        if p.exists():
            pending_drain.append((entry.get("sheet_row"), p, entry))
        else:
            unrecoverable.append((
                entry.get("sheet_row") or 0,
                entry.get("date", ""),
                entry.get("time", ""),
                "(pending queue)",
                "pending_queue_missing_file",
            ))
    return to_write, to_upload, unrecoverable, pending_drain


# --- Reporting ---------------------------------------------------------

def print_plan(rows, recorded, scan_log_idx, queue, plan, dry_run):
    to_write, to_upload, unrecoverable, pending_drain = plan
    print("=== Dry-run summary ===")
    print(f"Total sheet rows: {len(rows) - 1}  (header + {len(rows) - 1} data)")
    print(f"Recorded drive URLs indexed: {len(recorded)}")
    print(f"food_scan_log URLs indexed: {len(scan_log_idx)}")
    print(f"Pending-queue entries: {len(queue)}")
    print()
    print(f"Rows recoverable from recorded IDs / scan_log: {len(to_write)}")
    if to_write:
        for sr, url, src in to_write:
            print(f"  row {sr:4} ← {url}  ({src})")
    print()
    print(f"Rows that need a real Drive upload: {len(to_upload)}")
    if to_upload:
        for sr, local, date, time_short, name in to_upload:
            print(f"  row {sr:4} | {date} {time_short} | {name[:30]:30} | {local.name}")
    print()
    print(f"Pending-queue entries to retry: {len(pending_drain)}")
    if pending_drain:
        for sr, local, entry in pending_drain:
            print(f"  row {sr} | {local.name} | reason={entry.get('reason', '?')}")
    print()
    print(f"Unrecoverable rows (no local file, no recorded ID): {len(unrecoverable)}")
    if unrecoverable:
        for tup in unrecoverable:
            print(f"  row {tup[0]:4} | {tup[1]} {tup[2]} | {tup[3][:30]:30} | reason={tup[4]}")
    print()
    if dry_run:
        print("[DRY-RUN: nothing was uploaded, nothing was written to the sheet]")
    return to_write, to_upload, unrecoverable, pending_drain


# --- Live run ----------------------------------------------------------

def run_live(plan, access: str, rate: float):
    to_write, to_upload, unrecoverable, pending_drain = plan
    print("=== LIVE RUN ===")

    # Write URLs we already know about (idempotent).
    n_written = 0
    if to_write:
        edits = [
            {"range": f"{SHEET_TAB}!K{sr}", "values": [[url]]}
            for sr, url, _src in to_write
        ]
        n_written = batch_update(access, edits)
        print(f"  ✓ wrote {n_written} known URLs to column K")

    # Upload files that genuinely need uploading.
    n_uploaded = 0
    new_pending = []
    for sr, local, date, time_short, name in to_upload:
        url = upload_to_drive(local, access)
        if url:
            edits = [{"range": f"{SHEET_TAB}!K{sr}", "values": [[url]]}]
            batch_update(access, edits)
            n_uploaded += 1
            print(f"  ✓ row {sr}: uploaded {local.name} → K{sr}")
        else:
            new_pending.append({
                "ts": time.time(),
                "sheet_row": sr,
                "date": date,
                "time": time_short,
                "local_path": str(local),
                "reason": "upload_failed",
            })
            print(f"  ✗ row {sr}: upload FAILED for {local.name}")
        time.sleep(rate)

    # Drain pending queue.
    n_drained = 0
    remaining_queue = []
    for entry in load_pending_queue():
        p = Path(entry.get("local_path", ""))
        sr = entry.get("sheet_row")
        if p.exists():
            url = upload_to_drive(p, access)
            if url:
                if sr:
                    batch_update(access, [{"range": f"{SHEET_TAB}!K{sr}", "values": [[url]]}])
                n_drained += 1
                print(f"  ✓ drained pending: {p.name} → K{sr or '?'}")
            else:
                remaining_queue.append(entry)
        else:
            remaining_queue.append(entry)
        time.sleep(rate)
    save_pending_queue(remaining_queue)

    # Append newly-failed rows to the queue.
    if new_pending:
        merged = load_pending_queue() + new_pending
        save_pending_queue(merged)
        print(f"  → added {len(new_pending)} new entries to pending queue")

    print()
    print(f"=== Live run complete ===")
    print(f"  URLs written (recorded): {n_written}")
    print(f"  URLs uploaded + written: {n_uploaded}")
    print(f"  Pending-queue drained:   {n_drained}")
    print(f"  Unrecoverable:           {len(unrecoverable)}")
    print(f"  Pending queue depth:     {len(remaining_queue)}")


# --- Main --------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Reconcile Drive Image URL (col K) for Nutrition sheet rows."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan only (default for safety).")
    parser.add_argument("--live", action="store_true",
                        help="Actually upload + write. DANGEROUS — also requires "
                             "the ALLOW_DRIVE_WRITES=1 env var as a safety guard.")
    parser.add_argument("--use-snapshot", type=str, default="",
                        help="Load sheet rows from a local JSON snapshot file.")
    parser.add_argument("--rate", type=float, default=1.0,
                        help="Seconds between uploads (default 1.0).")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt in live mode.")
    args = parser.parse_args()

    # Safety: default to dry-run. Live mode requires BOTH --live flag AND
    # ALLOW_DRIVE_WRITES=1 env var. This makes accidental Drive writes
    # effectively impossible from a regular shell.
    if args.live and os.environ.get("ALLOW_DRIVE_WRITES") != "1":
        print("ERROR: --live requires ALLOW_DRIVE_WRITES=1 in env.")
        print("       This is a safety guard against accidental Drive uploads.")
        sys.exit(2)
    dry_run = args.dry_run or not args.live  # default to safe

    print("=== Reconcile Drive URLs ===")
    print(f"Sheet: {SHEET_ID} (tab {SHEET_TAB})")
    print(f"Mode: {'DRY-RUN' if dry_run else 'LIVE'}")
    print()

    # Load recorded-ID + scan-log + queue tables.
    recorded = load_recorded_drive_urls()
    scan_log_idx = load_scan_log_index()
    queue = load_pending_queue()
    print(f"Indexed {len(recorded)} recorded Drive URLs, "
          f"{len(scan_log_idx)} scan_log URLs, "
          f"{len(queue)} pending-queue entries.")

    # Load sheet rows.
    rows = None
    snapshot_path = Path(args.use_snapshot) if args.use_snapshot else None
    if not snapshot_path and DEFAULT_SNAPSHOT.exists():
        snapshot_path = DEFAULT_SNAPSHOT
        print(f"Using snapshot: {snapshot_path}")
    if snapshot_path and snapshot_path.exists():
        try:
            data = json.loads(snapshot_path.read_text())
            rows = data.get("values", []) if isinstance(data, dict) else data
        except Exception as e:
            print(f"  WARN: failed to read snapshot {snapshot_path}: {e}")

    if rows is None:
        # Live fetch — only allowed in live mode (we still won't WRITE).
        if dry_run:
            print("  WARN: no snapshot found; cannot dry-run without reading the sheet.")
            print(f"  hint: pass --use-snapshot {BACKUP_SNAPSHOT} or "
                  f"{DEFAULT_SNAPSHOT}")
            sys.exit(1)
        access = refresh_access_token()
        if not access:
            print("ERROR: failed to refresh access token")
            sys.exit(1)
        rows = fetch_sheet(access)
        print(f"Fetched {len(rows)} rows live")
    print(f"Loaded {len(rows)} rows (header + {len(rows) - 1} data)")
    print()

    plan = build_plan(rows, recorded, scan_log_idx, queue)
    plan_tuple = print_plan(rows, recorded, scan_log_idx, queue, plan, dry_run)

    if dry_run:
        return

    # Live mode — confirm.
    if not args.yes:
        confirm = input("Proceed with LIVE run? [y/N] ")
        if confirm.lower() != "y":
            print("Aborted.")
            sys.exit(0)

    access = refresh_access_token()
    if not access:
        print("ERROR: failed to refresh access token")
        sys.exit(1)
    run_live(plan, access, args.rate)


if __name__ == "__main__":
    main()
