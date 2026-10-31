"""
tests/qa/test_startup_check.py — v3.3.71 container 啟動自檢嘅 regression 測試

Frimpong 2026-10-31。背景見 `gym_web/startup_check.py` 嘅 module docstring。

呢個 suite 嘅作用:**令 container mount 嘅 silent failure 變成 loud failure**。

三個 mount 陷阱(全部係靜默嘅,`/api/health` 原本睇唔出):
  1. 單檔 bind mount + root-owned parent dir → atomic write 100% 失敗
  2. mount source 唔存在 → Docker 自動 mkdir 成 root-owned **目錄** → app 讀唔到
  3. image 內 baked code 同行緊 code 版本唔一致

⚠️ **全部 read-only。** 唔會寫入 Jim 個 Google Sheet,唔會改任何 state 檔內容。
    write probe 寫嘅係自己嘅 `.gymbro_wprobe.<pid>` temp 檔,跟即刪。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gym_web import startup_check as sc  # noqa: E402


# ---------- write permissions ----------------------------------------------

def test_write_permissions_reports_every_target():
    """每個要 atomic-write 嘅目標檔都要有獨立 entry,唔可以得個總數。"""
    res = sc.check_write_permissions()
    assert set(res["targets"]) == set(sc.CHECK_TMP_TARGETS)
    for target, entry in res["targets"].items():
        # 三個 capability 要分開講 —— 佢哋 fail 嘅原因唔同,修法都唔同
        assert "tmp_creatable" in entry, f"{target} 冇報 tmp_creatable"
        assert "target_writable" in entry, f"{target} 冇報 target_writable"
        assert "atomic_write_ok" in entry, f"{target} 冇報 atomic_write_ok"


def test_write_permissions_separates_tmp_vs_target_capability():
    """關鍵 invariant:target 可寫 ≠ tmp 可建立。

    Container 單檔 bind mount 嘅典型症狀就係 target_writable=True 但
    tmp_creatable=False —— 如果個 check 只睇 os.access(target, W_OK),
    就會報「OK」而 app 實際上 atomic write 全部失敗。
    """
    res = sc.check_write_permissions()
    for target, entry in res["targets"].items():
        if entry.get("tmp_creatable") and entry.get("target_writable"):
            assert entry["atomic_write_ok"] is True
        else:
            assert entry["atomic_write_ok"] is False, (
                f"{target}: 兩個 capability 唔齊全就唔應該報 atomic_write_ok")


def test_write_permissions_probe_does_not_leave_temp_files():
    """write probe 唔可以留低垃圾檔喺 state 目錄。"""
    before = set(p.name for p in Path("/home/work/.hermes").glob(".gymbro_wprobe.*"))
    sc.check_write_permissions()
    after = set(p.name for p in Path("/home/work/.hermes").glob(".gymbro_wprobe.*"))
    assert before == after, f"write probe 留低 temp 檔: {after - before}"


def test_atomic_write_capable_count_matches_targets():
    res = sc.check_write_permissions()
    assert res["atomic_write_total"] == len(sc.CHECK_TMP_TARGETS)
    assert 0 <= res["atomic_write_capable"] <= res["atomic_write_total"]
    assert res["ok"] == (res["atomic_write_capable"] == res["atomic_write_total"])


# ---------- state file types ----------------------------------------------

def test_state_files_detect_wrong_type():
    """mount source 唔存在 → Docker mkdir 成目錄 → app 讀唔到但唔報錯。

    呢個 check 一定要能夠分辨 file vs dir,唔可以淨係 check exists()。
    """
    res = sc.check_state_files()
    for path, kind in sc.CHECK_STATE_FILES:
        entry = res["files"][path]
        assert entry["expected"] == kind
        assert entry["actual"] in ("file", "dir", "other")
        expected_ok = entry["exists"] and entry["actual"] == kind
        assert entry["ok"] == expected_ok, f"{path} ok flag 唔啱"


def test_nutrition_log_json_must_be_a_file():
    """呢個係實際踩到嘅 bug:host 上係 root-owned 空目錄。

    呢個 test 喺 host 跑會 FAIL(因為 host 真係有問題),
    喺修好之後會 PASS —— 係刻意嘅 regression guard。
    """
    p = Path("/home/work/.hermes/nutrition_log.json")
    if not p.exists():
        pytest.skip("nutrition_log.json 唔存在(未被 mount)—— 冇嘢好 assert")
    assert p.is_file(), (
        "nutrition_log.json 係目錄唔係檔案 —— Docker 見到 mount source 唔存在"
        "就自動 mkdir 咗,app 讀佢會撞 IsADirectoryError 被 broad except 吞掉,"
        "→ silent failure。要 rm 個目錄 + 喺 host 重建真檔案。"
    )


# ---------- credentials -----------------------------------------------------

def test_credentials_reports_missing_files():
    res = sc.check_credentials()
    assert "files" in res and "google_token" in res
    for f, entry in res["files"].items():
        assert "exists" in entry and "is_file" in entry and "size" in entry


def test_google_token_structural_check():
    """Google token 唔止要存在,仲要有 refresh 所需嘅 field。"""
    res = sc.check_credentials()
    gt = res["google_token"]
    if not gt.get("exists"):
        pytest.skip("google_token.json 唔存在")
    if "parse_error" in gt:
        pytest.fail(f"google_token.json parse 失敗: {gt['parse_error']}")
    assert "has_required_fields" in gt
    if gt.get("has_required_fields"):
        assert not gt.get("missing_fields")


# ---------- version consistency --------------------------------------------

def test_version_consistency_reports_running_version():
    res = sc.check_version_consistency()
    # /app/gym_web.py 喺 container 存在;喺 host 可能唔存在 → skip
    if res.get("running_version") is None and res.get("ok") is None:
        pytest.skip("搵唔到 /app/gym_web.py(喺 host 跑)")
    assert "running_version" in res


# ---------- report shape ----------------------------------------------------

def test_run_startup_checks_report_shape():
    """Report 結構一定要穩定 —— 監控 / healthcheck 靠佢。"""
    rep = sc.run_startup_checks(include_network=False)
    for key in ("checks", "ok", "failed_checks", "degraded", "summary", "elapsed_sec"):
        assert key in rep, f"report 缺少 {key}"
    for name in ("write_permissions", "state_files", "credentials", "version_consistency"):
        assert name in rep["checks"], f"report.checks 缺少 {name}"
        assert "ok" in rep["checks"][name], f"check {name} 冇 'ok' 欄位"
        assert "detail" in rep["checks"][name], f"check {name} 冇 'detail' 欄位"


def test_run_startup_checks_fast_mode_skips_network():
    """fast=1 唔可以行對外 call(token refresh / Sheet 讀 / Redis)。"""
    rep = sc.run_startup_checks(include_network=False)
    assert "redis" not in rep["checks"]
    assert "sheet_token" not in rep["checks"]


def test_format_report_is_human_readable():
    rep = sc.run_startup_checks(include_network=False)
    text = sc.format_report(rep)
    assert "startup-check" in text
    assert rep["summary"] in text
    # 每個 check 都要有一行,唔可以得總結
    for name in rep["checks"]:
        assert name in text, f"format_report 漏咗 {name}"


def test_failed_check_is_reported_not_swallowed():
    """關鍵 invariant:有 check 壞咗,report.ok 一定係 False。

    防止將 fail 變成 degraded 又當 OK —— 嗰種係最陰險嘅 bug 來源。
    """
    rep = sc.run_startup_checks(include_network=False)
    hard_failed = [k for k in ("write_permissions", "state_files", "credentials")
                   if rep["checks"][k].get("ok") is False]
    if hard_failed:
        assert rep["ok"] is False, f"壞咗嘅 check {hard_failed} 但 report.ok 仍係 True"
        assert set(rep["failed_checks"]) >= set(hard_failed)
        assert "FAILED" in rep["summary"]
    else:
        assert rep["ok"] in (True, False)


def test_check_functions_never_raise():
    """任何一個 check 都唔可以拋 exception —— 否則 startup 會炸。"""
    for fn in (sc.check_write_permissions, sc.check_credentials,
               sc.check_state_files, sc.check_version_consistency):
        res = fn()
        assert isinstance(res, dict), f"{fn.__name__} 唔係 dict"
        assert "ok" in res, f"{fn.__name__} 冇 'ok'"
        assert "detail" in res, f"{fn.__name__} 冇 'detail'"


# ---------- no production pollution ----------------------------------------

def test_startup_check_never_writes_to_sheet():
    """靜態保證:startup_check 模組內冇任何 Sheets append / write call。

    只准讀。呢個 test 係防止日後有人加 write 落呢個模組。
    """
    src = Path(sc.__file__).read_text()
    # Sheets append endpoint = 唯一嘅寫入路徑
    assert "values/" in src  # 有讀 sheet(合理)
    for forbidden in (":append", "valueInputOption=USER_ENTERED", "_sheet_append_rows",
                      "_sheet_write_guard", "append?valueInputOption"):
        assert forbidden not in src, (
            f"startup_check.py 出現 {forbidden!r} —— 呢個模組必須係 read-only,"
            "唔可以寫入 Jim 個 production Sheet")
