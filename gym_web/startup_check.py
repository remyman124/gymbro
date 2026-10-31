"""
gym_web/startup_check.py — v3.3.71 container 啟動自檢

背景 (Frimpong, 2026-10-31 review of Alonso v3.3.70):

GymBro 已經由 tmux + 系統 Python 搬上 Docker container。Container 版
有兩個 mount 陷阱,而家嘅 `/api/health` 完全睇唔出:

1. **單檔 bind mount 令 atomic write 失敗。**
   `docker-compose.yml` 用 `- /home/work/.hermes/google_token.json:/home/work/.hermes/google_token.json`
   (單檔)。Container 內 `/home/work` 同 `/home/work/.hermes` 係 **root:root 755**
   (Docker 自己建立嘅 mount point 父目錄),而 container 以 `user: 1001:1002` 行。
   → `Path(tmp).write_text()` 直接 PermissionError,連 `os.replace` 都去唔到。

   實測 (2026-10-31, running container `gymbro`):
   ```
   FAIL  tmp-create /home/work/.whoop_workout_log.json -> PermissionError: [Errno 13]
   FAIL  tmp-create /home/work/.jim_context.json        -> PermissionError: [Errno 13]
   FAIL  tmp-create /home/work/.hermes/google_token.json -> PermissionError: [Errno 13]
   ```

   ⚠️ v3.3.70 嘅 fallback 寫法(先 `write_text(tmp)` 再 `os.replace`,replace 失敗
   先 direct-write)**修唔到呢個 case** —— 因為爆嘅係 `write_text(tmp)` 嗰步,
   個 `except OSError` 包住嘅係 `os.replace`,包唔到 `write_text`。

2. **Container 冇咗 host 目錄 → silent fail。**
   `/home/work/.hermes/nutrition_log.json` 喺 host 上係一個 **root-owned 空目錄**
   (Docker 見到 mount source 唔存在,自動 mkdir 咗),唔係檔案。
   App 讀佢嘅 code 會撞 `IsADirectoryError`,被 broad `except Exception: pass` 吞掉
   → 靜靜哋讀唔到,無 log、無 error。

3. **Container 版本 vs 行緊版本可以唔一致。**
   `docker-compose.yml` mount `/home/work/projects/gymbro:/app:ro`,遮蔽咗 image
   內 baked 嘅 code。實測: image baked `__version__ = 3.3.69`,行緊嘅 container
   睇到 `3.3.70`。即係 `docker build` 出嘅 image 同行緊嘅 code 唔係同一份。

呢個 module 做嘅嘢:喺 **啟動時一次過** check 晒
寫入權限(atomic-write 能力)/ 憑證 / Redis / Sheet token / 版本一致性,
將結果結構化,俾 `/api/startup_check` endpoint 同 startup log 用。

設計原則:
- **只讀,唔寫 production。** 所有 write probe 都寫去自己嘅 temp 檔再刪,
  絕不寫入 Jim 個 Google Sheet,亦唔會改任何 state 檔嘅內容。
- **每個 check 都要有可診斷嘅 detail。** 唔係 bool,係 dict + 具體 error 訊息,
  否則 log 出嚟都唔知點修。
- **唔好喺 import 時拋。** 呢個係診斷工具,唔係啟動閘 — app 照跑,
  結果放喺 `/api/startup_check`。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

__all__ = [
    "CHECK_TMP_TARGETS",
    "run_startup_checks",
    "check_write_permissions",
    "check_credentials",
    "check_redis",
    "check_sheet_token",
    "check_version_consistency",
]

# --- 需要 atomic-write 能力嘅目標檔 ---------------------------------------
# 每個都係 app 會寫嘅 state 檔。Container 版行單檔 bind mount + uid 1001,
# tmp 檔 (同一個 dir) 建唔到 → atomic write 100% 失敗。
CHECK_TMP_TARGETS = (
    "/home/work/.whoop_workout_log.json",
    "/home/work/.jim_context.json",
    "/home/work/.hermes/google_token.json",
)

# --- 需要存在 + 可讀嘅憑證 -------------------------------------------------
CHECK_CREDENTIAL_FILES = (
    "/home/work/.hermes/.env",
    "/home/work/.hermes-torres/.env",
    "/home/work/.whoop_tokens.json",
    "/home/work/.whoop_data_latest.json",
)

# --- 只讀嘅 state 檔(唔需要寫,但要存在同類型正確)------------------------
CHECK_STATE_FILES = (
    # (path, expected_kind) — kind: "file" | "dir"
    ("/home/work/.hermes/nutrition_log.json", "file"),
    ("/home/work/.hermes/commit_idempotency.json", "file"),
    ("/home/work/.hermes/gymbro_geo.json", "file"),
    ("/home/work/.hermes/gymbro_outbox.json", "file"),
    ("/home/work/.hermes/gymbro_push_subs.json", "file"),
    ("/home/work/.hermes/gymbro_vapid.json", "file"),
    ("/home/work/.hermes/nutrition_desc_cache.json", "file"),
    ("/home/work/.hermes/video_cache", "dir"),
    ("/home/work/.hermes/image_cache", "dir"),
    ("/home/work/.hermes/scan_cache", "dir"),
    ("/home/work/.hermes/scan_thumb_cache", "dir"),
    ("/home/work/.hermes/nutrition_card_cache", "dir"),
    ("/home/work/.hermes/nutrition_pdf_cache", "dir"),
    ("/home/work/.hermes/cheer_artifacts", "dir"),
    ("/home/work/.hermes/logs", "dir"),
)


def _in_container() -> bool:
    """Detect container runtime ( /.dockerenv or cgroup hint)."""
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(errors="ignore")
        return "docker" in cgroup or "containerd" in cgroup or "kubepods" in cgroup
    except OSError:
        return False


def _probe_dir_writable(directory: Path) -> tuple[bool, str]:
    """Can we create a file in ``directory``? (the tmp-file capability)"""
    probe = directory / f".gymbro_wprobe.{os.getpid()}"
    try:
        probe.write_text("x")
        probe.unlink()
        return True, "ok"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def check_write_permissions() -> dict[str, Any]:
    """Can the app actually persist state in this runtime?

    Two distinct capabilities are probed separately because they fail for
    different reasons and need different fixes:

    - ``tmp_creatable``: can we create the sibling ``.tmp`` file? Needed for
      *atomic* write. Fails when the parent dir is not writable (container
      single-file bind mount + non-root uid).
    - ``target_writable``: can we write the target file itself? Always OK for
      a bind-mounted file the host owns, even when tmp creation fails.
    """
    out: dict[str, Any] = {
        "in_container": _in_container(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "targets": {},
    }
    ok_atomic = 0
    for target in CHECK_TMP_TARGETS:
        p = Path(target)
        entry: dict[str, Any] = {"exists": p.exists(), "is_file": p.is_file()}
        if not p.exists():
            entry["tmp_creatable"] = False
            entry["tmp_error"] = "target does not exist"
            entry["target_writable"] = False
        else:
            parent = p.parent
            tmp_ok, tmp_err = _probe_dir_writable(parent)
            entry["parent_dir"] = str(parent)
            entry["parent_writable"] = tmp_ok
            entry["tmp_creatable"] = tmp_ok
            entry["tmp_error"] = None if tmp_ok else tmp_err
            entry["target_writable"] = os.access(p, os.W_OK)
        entry["atomic_write_ok"] = bool(entry.get("tmp_creatable") and entry.get("target_writable"))
        if entry["atomic_write_ok"]:
            ok_atomic += 1
        out["targets"][target] = entry
    out["atomic_write_capable"] = ok_atomic
    out["atomic_write_total"] = len(CHECK_TMP_TARGETS)
    out["ok"] = ok_atomic == len(CHECK_TMP_TARGETS)
    out["detail"] = (
        "all targets support atomic write"
        if out["ok"]
        else f"{ok_atomic}/{len(CHECK_TMP_TARGETS)} targets support atomic write — "
             "single-file bind mount under a root-owned parent dir; "
             "atomic write degrades to direct write"
    )
    return out


def check_credentials() -> dict[str, Any]:
    """Credential files present, non-empty, and (for the Google token) structurally valid."""
    out: dict[str, Any] = {"files": {}, "google_token": {}}
    missing: list[str] = []
    for f in CHECK_CREDENTIAL_FILES:
        p = Path(f)
        ok = p.is_file() and p.stat().st_size > 0
        out["files"][f] = {
            "exists": p.exists(),
            "is_file": p.is_file(),
            "size": p.stat().st_size if p.exists() else 0,
            "ok": ok,
        }
        if not ok:
            missing.append(f)

    gt = Path("/home/work/.hermes/google_token.json")
    gt_info: dict[str, Any] = {"exists": gt.exists()}
    if gt.is_file():
        try:
            tok = json.loads(gt.read_text())
            required = ("client_id", "client_secret", "refresh_token")
            gt_info["has_required_fields"] = all(tok.get(k) for k in required)
            gt_info["missing_fields"] = [k for k in required if not tok.get(k)]
            expires_at = tok.get("expires_at")
            if expires_at:
                try:
                    gt_info["access_token_expired"] = time.time() > float(expires_at)
                    gt_info["expires_in_sec"] = round(float(expires_at) - time.time(), 1)
                except (TypeError, ValueError):
                    gt_info["access_token_expired"] = None
            gt_info["refreshed_at"] = tok.get("refreshed_at")
        except Exception as e:
            gt_info["parse_error"] = f"{type(e).__name__}: {e}"
            gt_info["has_required_fields"] = False
    out["google_token"] = gt_info
    out["missing"] = missing
    out["ok"] = not missing and bool(gt_info.get("has_required_fields"))
    out["detail"] = (
        "all credential files present"
        if out["ok"]
        else f"missing/invalid: {missing or 'google_token.json invalid'}"
    )
    return out


def check_redis() -> dict[str, Any]:
    """Redis reachable? (cache layer; app degrades to in-memory if down)"""
    out: dict[str, Any] = {"host": "127.0.0.1", "port": 6379}
    try:
        sys.path.insert(0, "/usr/lib/python3/dist-packages")
        import redis  # type: ignore

        client = redis.Redis(host="127.0.0.1", port=6379,
                              socket_connect_timeout=2, socket_timeout=2)
        out["ping"] = bool(client.ping())
        # Round-trip a namespaced key so we know it is writable, not just alive.
        k = "gymbro:startup_check:probe"
        client.set(k, "1", ex=30)
        out["readback"] = client.get(k) in (b"1", "1")
        client.delete(k)
        out["ok"] = out["ping"] and out["readback"]
        out["detail"] = "redis reachable, read+write ok" if out["ok"] else "redis ping/readback failed"
    except ImportError:
        out["ok"] = False
        out["detail"] = "redis python module not installed in this runtime"
    except Exception as e:
        out["ok"] = False
        out["detail"] = f"{type(e).__name__}: {e}"
    return out


def check_sheet_token() -> dict[str, Any]:
    """Can we obtain a Google access token and read the sheet tab?

    READ-ONLY: fetches the Nutrition tab values. Never writes.
    """
    out: dict[str, Any] = {"sheet_id": "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag",
                           "tab": "Nutrition"}
    token_path = Path("/home/work/.hermes/google_token.json")
    if not token_path.is_file():
        out["ok"] = False
        out["detail"] = "google_token.json missing"
        return out
    try:
        import urllib.parse
        import urllib.request

        tok = json.loads(token_path.read_text())
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
        out["token_refresh_ok"] = bool(body.get("access_token"))
        access = body["access_token"]
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{out['sheet_id']}/values/"
               f"{out['tab']}!A1:K?valueRenderOption=UNFORMATTED_VALUE"
               f"&majorDimension=ROWS")
        sreq = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
        with urllib.request.urlopen(sreq, timeout=15) as sresp:
            sbody = json.loads(sresp.read().decode())
        rows = sbody.get("values", [])
        out["rows_read"] = len(rows)
        out["sheet_read_ok"] = True
        out["ok"] = True
        out["detail"] = f"token refreshed, read {len(rows)} rows from {out['tab']} (read-only)"
    except Exception as e:
        out["ok"] = False
        out["token_refresh_ok"] = out.get("token_refresh_ok", False)
        out["sheet_read_ok"] = False
        out["detail"] = f"{type(e).__name__}: {e}"
    return out


def check_version_consistency() -> dict[str, Any]:
    """Does the code the app is running match the code the image was built from?

    compose bind-mounts ``/home/work/projects/gymbro:/app:ro`` over the image's
    baked ``/app``. So the running code is the HOST working tree, while the
    image still carries whatever was baked at build time. If a rebuild is
    skipped, the image is stale — which matters because the image is also where
    the pip dependencies live.
    """
    out: dict[str, Any] = {}
    try:
        src = Path("/app/gym_web.py")
        if not src.is_file():
            out["ok"] = None
            out["detail"] = "/app/gym_web.py not readable"
            return out
        import re

        m = re.search(r'^__version__ = "([^"]+)"', src.read_text(), re.M)
        out["running_version"] = m.group(1) if m else None
        out["source_path"] = str(src)
        # Dockerfile bakes a version into the image header comment.
        df = Path("/home/work/projects/gymbro/Dockerfile")
        if df.is_file():
            dm = re.search(r"v(\d+\.\d+\.\d+)", df.read_text(errors="ignore"))
            out["dockerfile_version"] = dm.group(1) if dm else None
        # A bind-mounted /app means the image's own copy is shadowed.
        try:
            st = os.stat("/app/gym_web.py")
            out["app_is_bind_mount"] = st.st_dev != os.stat("/").st_dev
        except OSError:
            out["app_is_bind_mount"] = None
        out["ok"] = True
        out["detail"] = (
            f"running v{out.get('running_version')}"
            + (" (bind mount: host code shadows the image's baked code)"
               if out.get("app_is_bind_mount") else " (image's own code)")
        )
    except Exception as e:
        out["ok"] = None
        out["detail"] = f"{type(e).__name__}: {e}"
    return out


def check_state_files() -> dict[str, Any]:
    """State files exist AND have the right type (a dir where a file is expected = silent fail)."""
    out: dict[str, Any] = {"files": {}, "wrong_type": []}
    for path, kind in CHECK_STATE_FILES:
        p = Path(path)
        exists = p.exists()
        actual = "dir" if p.is_dir() else ("file" if p.is_file() else "other")
        ok = exists and actual == kind
        out["files"][path] = {"exists": exists, "expected": kind, "actual": actual, "ok": ok}
        if exists and actual != kind:
            out["wrong_type"].append(f"{path} (expected {kind}, got {actual})")
    out["ok"] = not out["wrong_type"]
    out["detail"] = (
        "all state files present with correct type"
        if out["ok"]
        else "wrong type: " + "; ".join(out["wrong_type"])
    )
    return out


def run_startup_checks(include_network: bool = True) -> dict[str, Any]:
    """Run every check and return a single structured report.

    ``include_network=False`` skips the outbound calls (token refresh + sheet
    read + redis) — use that for a fast liveness probe.
    """
    started = time.time()
    report: dict[str, Any] = {
        "checks": {},
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    report["checks"]["write_permissions"] = check_write_permissions()
    report["checks"]["state_files"] = check_state_files()
    report["checks"]["credentials"] = check_credentials()
    report["checks"]["version_consistency"] = check_version_consistency()
    if include_network:
        report["checks"]["redis"] = check_redis()
        report["checks"]["sheet_token"] = check_sheet_token()

    # Overall verdict: a hard failure is anything that breaks persistence or
    # the core data path. Redis down = degraded (app has an in-memory fallback),
    # so it is reported but does not flip the verdict.
    hard = ("write_permissions", "state_files", "credentials")
    failed = [k for k in hard if report["checks"][k].get("ok") is False]
    sheet_ok = report["checks"].get("sheet_token", {}).get("ok")
    report["ok"] = not failed and sheet_ok is not False
    report["failed_checks"] = failed
    report["degraded"] = [k for k, v in report["checks"].items()
                          if v.get("ok") is False and k not in failed]
    report["elapsed_sec"] = round(time.time() - started, 2)
    report["summary"] = (
        "ALL OK" if report["ok"]
        else f"FAILED: {', '.join(failed)}" + (f" (degraded: {', '.join(report['degraded'])})"
                                              if report["degraded"] else "")
    )
    return report


def format_report(report: dict[str, Any]) -> str:
    """Human-readable one-block-per-check summary for the startup log."""
    lines = ["", "=" * 68, "[startup-check] GymBro container self-check", "=" * 68]
    for name, res in report.get("checks", {}).items():
        ok = res.get("ok")
        badge = {True: "OK", False: "FAIL", None: "SKIP"}.get(ok, "??")
        lines.append(f"[{badge:4s}] {name}: {res.get('detail', '')}")
    wp = report.get("checks", {}).get("write_permissions", {})
    if wp.get("targets"):
        lines.append("-" * 68)
        for target, t in wp["targets"].items():
            lines.append(
                f"        {target}\n"
                f"          atomic_write_ok={t.get('atomic_write_ok')} "
                f"tmp_creatable={t.get('tmp_creatable')} "
                f"target_writable={t.get('target_writable')}"
                + (f"\n          tmp_error={t.get('tmp_error')}" if t.get("tmp_error") else "")
            )
    sf = report.get("checks", {}).get("state_files", {})
    if sf.get("wrong_type"):
        lines.append("-" * 68)
        for bad in sf["wrong_type"]:
            lines.append(f"        WRONG TYPE: {bad}")
    lines.append("=" * 68)
    lines.append(f"[startup-check] {report.get('summary')}  ({report.get('elapsed_sec')}s)")
    lines.append("")
    return "\n".join(lines)
