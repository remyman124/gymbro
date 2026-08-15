# scripts/validate_sheet_health.py — ongoing sheet health monitor

A read-only daily cron gate that validates the Nutrition Google Sheet against the
eight defect classes the parallel audit found in practice. Reuses the OAuth
helpers from `scripts/migrations/sheet_cleanup_v3_2_7_47.py` — plain stdlib only.

## What it checks

| Severity | Check | Why it matters |
|---|---|---|
| ERROR   | Row width > 14 cols             | The original catastrophe: a wide bottom row made `values.append` auto-detect a wide range, and 76 subsequent rows bled into cols O–AC. |
| ERROR   | Column A empty                  | Orphaned rows. |
| ERROR   | Column B not `HH:MM`            | ISO datetime (`2026-08-09T20:44:48+08:00`), single-digit hour (`8:30`), and Google serial-number artifacts (`0.8576388889`) all seen in practice. |
| ERROR   | Column A not `YYYY-MM-DD` / future / pre-2020 | Format and plausibility. |
| ERROR   | Column N malformed              | Must be a `drive.google.com` URL containing `id=`. A literal `"30"` was once found. |
| WARNING | Weak dish name (col D)          | Empty / `scan` / `Unknown` / `未識別菜式` / truncated at exactly 120 chars (AI prose bleeding into the dish-name cell). |
| WARNING | Numeric sanity                  | Non-numeric / negative macros; kcal outside [20, 3000]; `4P+4C+9F` off by >40%. |
| WARNING | Duplicates                      | Exact A–N dupes; same date+time+meal_name. |

**Errors fail the cron. Warnings log but do not** — otherwise the job cries wolf
forever over the 391 legacy `Jim Meals Log legacy` rows that will never satisfy
the soft checks.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Healthy (warnings OK) |
| 2 | At least one ERROR found |
| 3 | Fetch / auth failure |

## Usage

```bash
# Full audit (all 476 rows)
python3 scripts/validate_sheet_health.py

# Scope to recent pipeline output (skip legacy noise)
python3 scripts/validate_sheet_health.py --since 2026-08-01
python3 scripts/validate_sheet_health.py --last 14        # last 14 data rows

# Machine-readable
python3 scripts/validate_sheet_health.py --json

# Cron mode: silent on success, mail only on failure
python3 scripts/validate_sheet_health.py --since "$(date -u -d '14 days ago' +%F)" --quiet
```

## Cron wiring (daily, 06:00 HKT = 22:00 UTC prior day)

```cron
# gymbro: validate Nutrition sheet health daily at 06:00 HKT (22:00 UTC).
# Scans rows from the last 14 days only so legacy bulk-import noise doesn't
# drown the signal; uses --quiet so MAILTO only fires when something's wrong.
0 22 * * * cd /home/work/projects/gymbro && /usr/bin/python3 scripts/validate_sheet_health.py --since "$(date -u -d '14 days ago' +%F)" --quiet 2>&1
```

### Reading the output

- Exit `0` + nothing printed: sheet is healthy; nothing to do.
- Exit `2` + report: ERROR class failed. Open the report — each line lists
  `count=N  rows=[R1, R2, …]` (1-indexed; header is row 1) so you can jump
  straight to the offending rows in the sheet.
- Exit `3`: usually means the OAuth refresh token expired/rotated. Run
  `scripts/migrations/sheet_cleanup_v3_2_7_47.py` once interactively to
  refresh `/home/work/.hermes/google_token.json`.

The `--json` mode emits `{scope, total_rows, errors, checks: {...}}` —
the per-check lists include `row_num` + a `kind`/`value` discriminator, so
a downstream alerting layer can dedupe by `kind` and only page on new errors.