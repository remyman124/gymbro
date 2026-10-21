#!/usr/bin/env python3
"""Meta-test — 驗證 pytest_baseline_diff.py 嘅判定邏輯真係啱。

Isak 2026-10-20。

背景: baseline-diff 係 QA 嘅「唔好誤報 regression」保險。如果佢自己
有 bug(例如將 flaky 當 regression),個 QA 交付就係假嘅。

做法: 直接 import 工具 module,monkeypatch 佢嘅 `run_pytest` 回傳
**合成 pytest 輸出**,咁就唔使真跑 pytest(快,而且唔受 suite 狀態影響)，
亦都唔使搞掂 stub interpreter 嘅問題。

用法:
    PYTHONPATH="" .venv/bin/python tests/qa/baseline_diff_meta_test.py
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

QA_DIR = Path(__file__).resolve().parent
SCRIPT = QA_DIR / "pytest_baseline_diff.py"

spec = importlib.util.spec_from_file_location("bd", SCRIPT)
bd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bd)

# 合成 pytest 輸出 (short summary 格式)
FAKE_OUTPUT = {
    "same": (
        "FAILED tests/api/test_x.py::test_a - boom\n"
        "ERROR tests/e2e/test_y.py::test_b\n"
    ),
    "new": (
        "FAILED tests/api/test_x.py::test_a - boom\n"
        "ERROR tests/e2e/test_y.py::test_b\n"
        "FAILED tests/NEW.py::test_boom - new regression\n"
    ),
    "gone": (
        "FAILED tests/api/test_x.py::test_a - boom\n"
    ),
    "clean": "",
    # ⚠️ 實測踩過:pytest 中途被 kill → 輸出淨係得 progress dots,
    #    冇 summary 行、冇 FAILED/ERROR 清單。如果唔 detect,
    #    diff 會收到空 set → 報「0 NEW regression」= 假陰性。
    "truncated": (
        ".......................F.....FFF......FF...\n"
    ),
}

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond)))
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""), flush=True)


def run_tool(baseline: dict, mode: str, monkeypatch_fp=None, complete=True):
    """用合成 pytest 輸出跑 baseline-diff,回傳 (exit_code, stdout)。"""
    real_run = bd.run_pytest
    real_fp = bd.fingerprint

    def fake_run(python, extra_args=None, timeout=1800):
        out = FAKE_OUTPUT[mode]
        failed, errored = bd.parse_results(out)
        rc = 1 if (failed or errored) else 0
        return rc, failed, errored, out, complete

    bd.run_pytest = fake_run
    if monkeypatch_fp:
        bd.fingerprint = lambda python: monkeypatch_fp
    try:
        with tempfile.TemporaryDirectory() as td:
            bl = Path(td) / "baseline.json"
            bl.write_text(json.dumps(baseline))
            argv = ["bd", "--baseline", str(bl)]
            old_argv = sys.argv
            sys.argv = argv
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    rc = bd.main()
            finally:
                sys.argv = old_argv
            return rc, buf.getvalue()
    finally:
        bd.run_pytest = real_run
        bd.fingerprint = real_fp


def main():
    print("=" * 68)
    print("  BASELINE-DIFF META-TEST — 驗證判定邏輯")
    print("=" * 68)

    base = {
        "note": "test",
        "failed": ["tests/api/test_x.py::test_a"],
        "errored": ["tests/e2e/test_y.py::test_b"],
        "fingerprint": {"python": "Python 3.11.15", "executable": "/x",
                        "platform": "linux"},
    }
    fp_same = {"python": "Python 3.11.15", "executable": "/x",
               "platform": "linux"}

    print("\n── 1. 只有 pre-existing failure → 唔算 regression (exit 0) ──")
    rc, out = run_tool(base, "same", monkeypatch_fp=fp_same)
    check("exit code == 0", rc == 0, f"got {rc}")
    check("顯示 SAME 計數 = 2", "pre-existing (SAME) : 2" in out)
    check("冇報 REGRESSION", "REGRESSION DETECTED" not in out)

    print("\n── 2. 出現新 failure → 報 REGRESSION (exit 1) ──")
    rc, out = run_tool(base, "new", monkeypatch_fp=fp_same)
    check("exit code == 1", rc == 1, f"got {rc}")
    check("列出新 failure", "tests/NEW.py::test_boom" in out)
    check("顯示 REGRESSION DETECTED", "REGRESSION DETECTED" in out)
    check("NEW 計數 = 1", "newly failing (NEW) : 1" in out)

    print("\n── 3. baseline 有、今次冇 → 報 GONE 但唔 fail (exit 0) ──")
    rc, out = run_tool(base, "gone", monkeypatch_fp=fp_same)
    check("exit code == 0", rc == 0, f"got {rc}")
    check("列為 GONE", "tests/e2e/test_y.py::test_b" in out)
    check("唔報 REGRESSION", "REGRESSION DETECTED" not in out)

    print("\n── 4. interpreter 唔同 → 出 WARNING ──")
    mismatched = dict(base)
    mismatched["fingerprint"] = {"python": "Python 3.13.7",
                                 "executable": "/other", "platform": "linux"}
    rc, out = run_tool(mismatched, "same",
                       monkeypatch_fp={"python": "Python 3.11.15",
                                       "executable": "/x", "platform": "linux"})
    check("出 WARNING", "WARNING" in out)
    check("提到 interpreter 唔同", "interpreter" in out and "唔同" in out)
    check("提到可比性問題", "可比" in out)

    print("\n── 5. baseline 完全冇 fingerprint → 唔會 crash ──")
    nofp = {k: v for k, v in base.items() if k != "fingerprint"}
    rc, out = run_tool(nofp, "same", monkeypatch_fp=fp_same)
    check("exit code == 0", rc == 0, f"got {rc}")
    check("唔會 exception", "Traceback" not in out)

    print("\n── 6. 全部 pass → exit 0 ──")
    rc, out = run_tool(base, "clean", monkeypatch_fp=fp_same)
    check("exit code == 0", rc == 0, f"got {rc}")
    check("顯示冇 regression", "冇 regression" in out)

    # ⚠️ 最關鍵嘅 case:pytest 中途被 kill(實測踩過)
    #    之前工具會收到空 set → 報「0 NEW regression」= 假陰性。
    print("\n── 7. pytest 被 kill / 輸出截斷 → 唔可以報「零 regression」 ──")
    rc, out = run_tool(base, "truncated", monkeypatch_fp=fp_same, complete=False)
    check("exit code == 2 (唔係 0)", rc == 2, f"got {rc}")
    check("冇報『冇 regression』", "冇 regression" not in out)
    check("冇報 REGRESSION DETECTED", "REGRESSION DETECTED" not in out)
    check("有明確話結果唔可信", "唔完整" in out or "唔可信" in out)
    check("有叫人手重跑", "重跑" in out)

    print("\n── 8. 截斷嘅輸出唔會被當成 GONE ──")
    rc, out = run_tool(base, "truncated", monkeypatch_fp=fp_same, complete=False)
    check("冇列出 GONE 清單", "已經好咗" not in out)

    # ── --from-file 模式 ──
    print("\n── 9. --from-file 模式:用已 capture 嘅輸出做比對 ──")
    import subprocess
    script = str(SCRIPT)

    def run_from_file(content: str, tmpname: str):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / tmpname
            f.write_text(content)
            bl = Path(td) / "baseline.json"
            bl.write_text(json.dumps(base))
            r = subprocess.run(
                [sys.executable, script, "--baseline", str(bl), "--from-file", str(f)],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "PYTHONPATH": ""},
            )
            return r.returncode, r.stdout

    rc, out = run_from_file(
        "FAILED tests/api/test_x.py::test_a - boom\n"
        "ERROR tests/e2e/test_y.py::test_b\n"
        "1 failed, 2 passed, 1 error in 10.00s\n", "complete.txt")
    check("完整輸出 → exit 0", rc == 0, f"got {rc}")
    check("有印 pytest summary", "pytest summary" in out)
    check("有印 parsed 數量", "parsed" in out)
    check("SAME 計數 = 2", "pre-existing (SAME) : 2" in out)
    check("NEW 計數 = 0", "newly failing (NEW) : 0" in out)

    rc, out = run_from_file("....F....FF.....\n", "trunc.txt")
    check("截斷輸出 → exit 2", rc == 2, f"got {rc}")
    check("有話唔完整", "唔完整" in out)
    check("冇報『冇 regression』", "冇 regression" not in out)

    # 實測踩過:全部 collection error 嗰種輸出,summary 行格式唔同
    # ("154 errors in 37.23s",冇 "failed"/"passed")。
    # 之前個寬鬆 regex 令佢 match 到,summary 顯示錯。
    rc, out = run_from_file(
        "ERROR tests/test_x.py::test_a - AttributeError\n"
        "ERROR tests/test_y.py::test_b - AttributeError\n"
        "2 errors in 3.10s\n", "errsonly.txt")
    # 注意:呢 2 條 error 唔喺 baseline 入面 → 應該報 NEW regression (exit 1),
    #       重點係「完整 run 有被識別」,唔係 exit 0。
    check("只有 errors 嘅輸出 → 當做完整 run 處理 (exit 1 = 有新 failure)",
          rc == 1, f"got {rc}")
    check("summary 正確顯示 errors-only", "2 errors in 3.10s" in out)
    check("冇誤報搵唔到 summary", "搵唔到" not in out)
    check("冇報『唔完整』", "唔完整" not in out)

    passed = sum(1 for _, ok in CHECKS if ok)
    total = len(CHECKS)
    print()
    print("=" * 68)
    print(f"  {passed}/{total} checks passed")
    if passed != total:
        for n, ok in CHECKS:
            if not ok:
                print(f"    ✗ {n}")
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
