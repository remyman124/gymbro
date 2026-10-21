#!/usr/bin/env python3
"""pytest baseline-diff — 驗證 gymbro suite 冇 regression (SCAN_PROGRESS_SPEC §4.5).

Isak 2026-10-20。

背景: gymbro suite 有幾個 **pre-existing, env-dependent** failure ——
佢哋唔係本次改動造成,唔應該當 regression 報。但同時我哋又唔想
真 regression 被淹沒喺 noise 入面。

做法 (baseline-diff):
  1. 讀 baseline 檔 (tests/qa/pytest_baseline.json) 入面記低嘅
     「已知會 fail / error」嘅 test id 清單。
  2. 跑 pytest,攞今次嘅 FAILED / ERROR 清單。
  3. 對比:
       - NEW   = 今次 fail 但 baseline 冇記錄 → 真 regression → exit 1
       - GONE  = baseline 有記錄但今次 pass  → 修復咗 (只報告,唔 fail)
       - SAME  = 兩邊都有 → pre-existing,唔算 regression

⚠️ 三個關鍵設計 (全部係實測撞到問題之後加嘅):

  A. **Interpreter fingerprint**
     baseline 記低產生佢嗰陣用嘅 python 版本 + executable。
     如果今次用嘅 interpreter 同 baseline 唔同 → 結果**唔可比**,
     工具會出 WARNING 而唔係靜靜哋報一堆假 regression。
     (我第一次就係用 /usr/bin/python3 3.13 做 baseline,
      跟住用 .venv 3.11 跑 diff,結果 7 NEW / 7 GONE 對稱互換 —— 假警報。)

  B. **Union baseline (--write --merge)**
     一次 run 捉唔齊所有 flaky failure。`--merge` 會將新結果
     **union** 落舊 baseline:任何一次 run fail 過嘅 test,
     永遠當 pre-existing。咁 flaky 嘅 test 就唔會
     喺 A 次 run 之間「NEW 咗又 GONE 咗」。

  C. **Flaky 分類**
     如果一個 test 喺 baseline 期間出現過又消失過 → 標 FLAKY,
     唔當真 regression,只提示。

Baseline 檔點樣產生:
    # 第一次 (乾淨 baseline)
    PYTHONPATH="" .venv/bin/python tests/qa/pytest_baseline_diff.py --write
    # 之後 (吸收新 flaky,唔會蓋走舊記錄)
    PYTHONPATH="" .venv/bin/python tests/qa/pytest_baseline_diff.py --write --merge

跑法:
    PYTHONPATH="" .venv/bin/python tests/qa/pytest_baseline_diff.py
    PYTHONPATH="" .venv/bin/python tests/qa/pytest_baseline_diff.py --pytest-args --ignore=tests/qa
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
BASELINE_FILE = Path(__file__).resolve().parent / "pytest_baseline.json"

# 呢啲 test 係 env-dependent: 要 live server / Redis / 特定 sheet 狀態先跑得起,
# 唔應該當 regression。但佢哋**唔應該**被 skip 掉 —— skip 會掩蓋真問題,
# 所以 baseline-diff 只記錄,唔豁免。
ENV_DEPENDENT_NOTE = (
    "live server / shared Redis gymbro:overlay:health / stale gym-web-v121 assert"
    " / playwright networkidle timeout (gymbro PWA 有持續 polling,networkidle 唔會 settle)"
)

PYTEST_ARGS = [
    "-p", "no:randomly",
    "--continue-on-collection-errors",
    "-q", "--tb=no", "-rfE",
]


def run_pytest(python: str, extra_args=None, timeout: int = 1800):
    """跑 pytest,回傳 (returncode, failed_ids, error_ids, raw_output)。

    ⚠️ 完整性檢查:如果 pytest 被 kill(SIGTERM/OOM)或者冇跑到完,
       輸出會係「半截」—— 有進度點但冇 summary 行,亦冇 FAILED/ERROR
       清單。呢種情況**唔可以**當成「全部 pass」!
       實測踩過:pytest 中途被 kill(exit 15),output 淨係得 progress dots,
       parse_results 收到空 set,diff 就報「0 NEW regression」——
       即係**假陰性**,最危險嘅失敗模式。
       所以呢度要明確 detect,唔好靜靜哋當 pass。
    """
    cmd = [python, "-m", "pytest", *PYTEST_ARGS, *(extra_args or [])]
    env = dict(os.environ)
    env["PYTHONPATH"] = ""  # 唔加會 PIL _imaging ImportError
    print(f"[baseline-diff] $ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(
        cmd, cwd=str(REPO), env=env, timeout=timeout,
        capture_output=True, text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    failed, errored = parse_results(out)

    # ── 完整性判定 ──
    complete = has_summary_line(out)
    killed = proc.returncode < 0 or proc.returncode in (15, 137, 124)
    if not complete or killed:
        reason = []
        if killed:
            reason.append(f"pytest 被終止 (returncode={proc.returncode})")
        if not complete:
            reason.append("輸出冇 pytest summary 行(跑唔完)")
        print()
        print("!" * 68)
        print("  ❌ PYTEST RUN 唔完整 — 結果唔可信,唔可以當 regression 比對!")
        print("!" * 68)
        for r in reason:
            print(f"     • {r}")
        print(f"     • 收集到 {len(failed)} failed + {len(errored)} errored")
        print("     • 輸出最後 200 字元:")
        print("       " + out.strip()[-200:].replace("\n", "\n       "))
        print()
        print("     處理方法:人手重跑,或者用 --merge 模式再試。")
        print("     ⚠️ 絕對唔可以喺呢個狀態下報「零 regression」。")
        return proc.returncode, failed, errored, out, False

    return proc.returncode, failed, errored, out, True


def has_summary_line(output: str) -> bool:
    """pytest 完整跑完一定會有一行 summary。

    真實格式(全部都要 support):
      "12 failed, 115 passed, 13 errors in 309.56s (0:05:09)"
      "9 failed, 129 passed, 2 errors in 303.31s (0:05:03)"
      "154 errors in 37.23s"            ← 只有 errors(全部 collection error)
      "115 passed in 200.00s"           ← 全 pass
      "1 failed in 45.67s"
    """
    # ⚠️ 唔可以用 ".*\d+ passed.*" 呢類寬鬆 pattern:
    #    佢會 match 到輸出入面任何一行提到 "passed" 嘅嘢,
    #    令「搵唔到 summary」嘅情況永遠唔會觸發。
    #    要精準:行首數字 + (failed|passed|error) + 空格/逗號/結尾。
    return bool(re.search(
        r"^\d+\s+(failed|passed|error|errors|warnings?)\b",
        output, re.MULTILINE,
    ))


def parse_results(output: str):
    """從 pytest 輸出抽 FAILED / ERROR 清單。

    支援兩種格式:
      - short summary: "FAILED tests/foo.py::test_bar - reason"
      - verbose:       "tests/foo.py::test_bar FAILED"
    """
    failed, errored = set(), set()

    for m in re.finditer(r"^FAILED\s+(\S+)", output, re.MULTILINE):
        failed.add(_normalise(m.group(1)))
    for m in re.finditer(r"^ERROR\s+(\S+)", output, re.MULTILINE):
        errored.add(_normalise(m.group(1)))
    # verbose 模式
    for m in re.finditer(r"^(\S+::\S+)\s+FAILED", output, re.MULTILINE):
        failed.add(_normalise(m.group(1)))
    for m in re.finditer(r"^(\S+::\S+)\s+ERROR", output, re.MULTILINE):
        errored.add(_normalise(m.group(1)))

    return failed, errored


def _normalise(test_id: str) -> str:
    """統一格式:去掉 ' - reason' 後綴同空白。"""
    tid = test_id.strip()
    if " - " in tid:
        tid = tid.split(" - ", 1)[0]
    return tid.strip()


def fingerprint(python: str) -> dict:
    """記低 interpreter 身份 —— 唔同 interpreter 嘅結果唔可比。"""
    ver = "unknown"
    try:
        out = subprocess.run([python, "-V"], capture_output=True, text=True,
                             timeout=30)
        ver = (out.stdout or out.stderr or "").strip()
    except Exception:
        pass
    return {
        "python": ver,
        "executable": str(Path(python).resolve()) if os.path.sep in python else python,
        "platform": platform.platform(),
    }


def load_baseline(path: Path) -> dict:
    if not path.exists():
        return {"failed": [], "errored": [], "note": "", "fingerprint": {}}
    return json.loads(path.read_text())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable,
                    help="python interpreter (default: current)")
    ap.add_argument("--baseline", default=str(BASELINE_FILE))
    ap.add_argument("--write", action="store_true",
                    help="用今次結果覆蓋 baseline 檔")
    ap.add_argument("--merge", action="store_true",
                    help="union 落舊 baseline (唔會蓋走舊記錄) — 用嚟吸收 flaky")
    ap.add_argument("--pytest-args", default="", nargs=argparse.REMAINDER,
                    help="額外 pytest args (例: --pytest-args --ignore=tests/qa)")
    ap.add_argument("--from-file", default=None,
                    help="唔重跑 pytest,直接用呢個已 capture 嘅 pytest 輸出檔做比對。"
                         "用嚟避免重跑整套 suite 造成記憶體壓力(OOM)。")
    args = ap.parse_args()

    baseline_path = Path(args.baseline)
    extra = list(args.pytest_args or [])
    if extra and extra[0] == "--":
        extra = extra[1:]

    if args.from_file:
        # ── 模式 B:用已 capture 嘅輸出做比對 ──
        src = Path(args.from_file)
        if not src.exists():
            print(f"❌ 檔案唔存在: {src}", file=sys.stderr)
            return 2
        raw = src.read_text(errors="replace")
        failed, errored = parse_results(raw)
        current = failed | errored
        complete = has_summary_line(raw)
        rc = 0 if not current else 1
        print(f"[baseline-diff] 用已 capture 嘅輸出: {src}")
        if not complete:
            print()
            print("!" * 68)
            print("  ❌ 呢個 pytest 輸出唔完整(冇 summary 行) — 結果唔可信!")
            print("!" * 68)
            print("     處理方法:人手重跑一個完整嘅 suite,唔好喺呢個狀態下報結論。")
            return 2
    else:
        # ── 模式 A:自己跑 pytest ──
        rc, failed, errored, raw, complete = run_pytest(args.python, extra)
        current = failed | errored
    fp = fingerprint(args.python)

    print()
    print("=" * 68)
    print("  PYTEST BASELINE DIFF — gymbro suite")
    print("=" * 68)

    # ⚠️ 自我佐證:印出 pytest 自己嘅 summary 行。
    #    否則讀者分唔到「current 係空」係真 clean,定係 parser 壞咗
    #    靜靜哋漏咗所有 FAILED/ERROR(最陰險嘅假陰性)。
    # ⚠️ 同 has_summary_line 一樣,呢度都要精準 ——
    #    唔可以用寬鬆 pattern,否則會 match 到輸出入面任何提到
    #    "passed" 嘅行,令人以為搵到 summary。
    m = re.search(r"^\d+\s+(?:failed|passed|error|errors|warnings?)\b.*$",
                  raw, re.MULTILINE)
    summary = m.group(0).strip() if m else "(搵唔到 pytest summary 行 — 輸出格式可能變咗!)"
    print(f"pytest summary : {summary}")
    print(f"pytest exit    : {rc}")
    print(f"parsed         : {len(failed)} failed + {len(errored)} errored "
          f"= {len(current)} not-passing")
    if rc == 5:
        print("  ⚠️  pytest exit 5 = 冇收集到任何 test — 唔係「全部 pass」!")

    # ⚠️ 唔完整嘅 run → 直接 fail,唔好報比對結果。
    #    呢個係最重要嘅防護:空結果會被誤讀成「零 regression」。
    if not complete:
        print()
        print("❌ pytest 冇完整跑完,結果唔可信 — 唔會報 regression 比對。")
        print("   處理方法:人手重跑,或者用 --merge 模式再試。")
        print("   ⚠️ 絕對唔可以喺呢個狀態下報「零 regression」。")
        return 2

    if args.write:
        base = load_baseline(baseline_path)
        old_failed = set(base.get("failed") or [])
        old_errored = set(base.get("errored") or [])
        if args.merge:
            # union:任何一次 fail 過就當 pre-existing (吸收 flaky)
            all_failed = old_failed | failed
            all_errored = old_errored | errored
            added = len((failed | errored) - (old_failed | old_errored))
            mode = "merge (union with existing baseline)"
        else:
            all_failed, all_errored = failed, errored
            added = len(current)
            mode = "overwrite"

        payload = {
            "note": ENV_DEPENDENT_NOTE,
            "failed": sorted(all_failed),
            "errored": sorted(all_errored),
            "fingerprint": fp,
            "runs_recorded": int(base.get("runs_recorded", 0)) + 1,
        }
        baseline_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"[baseline-diff] baseline 已寫入 ({mode}): {baseline_path}")
        print(f"  failed={len(all_failed)}  errored={len(all_errored)}  "
              f"今次新增={added}")
        print(f"  fingerprint: {fp['python']}  ({fp['executable']})")
        return 0

    base = load_baseline(baseline_path)
    base_failed = set(base.get("failed") or [])
    base_errored = set(base.get("errored") or [])
    base_all = base_failed | base_errored

    new = sorted(current - base_all)          # ← 真 regression
    gone = sorted(base_all - current)         # ← 修好咗 / flaky 抖返
    same = sorted(current & base_all)          # ← pre-existing

    # ⚠️ baseline 檔本身可能有 overlap(同一 id 喺 failed 同 errored 都出現過,
    #    通常係 flaky test 喺唔同 run 表現唔同)。去重避免計數失真。
    base_overlap = base_failed & base_errored
    if base_overlap:
        print(f"  ⚠️  baseline 檔有 {len(base_overlap)} 個 id 重複出現"
              f"(failed ∩ errored) — 已去重。重建 baseline 建議用 "
              f"build_baseline.py(會自動統一)")

    print(f"baseline file : {baseline_path}")
    if base.get("note"):
        print(f"baseline note : {base['note']}")
    base_fp = base.get("fingerprint") or {}
    if base_fp:
        print(f"baseline env  : {base_fp.get('python', '?')} "
              f"({base_fp.get('executable', '?')})")
    print(f"current env   : {fp['python']} ({fp['executable']})")
    if base_fp and base_fp.get("python") and base_fp.get("python") != fp["python"]:
        print()
        print("  ⚠️  WARNING: baseline 同今次用嘅 interpreter **唔同**!")
        print("      → 結果唔完全可比,NEW/GONE 可能係環境差異而唔係真 regression。")
        print("      → 建議:用同一個 interpreter 重新產生 baseline "
              "(--write --merge)。")
    print()
    print(f"  pre-existing (SAME) : {len(same)}")
    print(f"  newly failing (NEW) : {len(new)}")
    print(f"  fixed       (GONE) : {len(gone)}")
    print()

    if same:
        print("── pre-existing failures (唔算 regression) ──")
        for t in same:
            print(f"   = {t}")
        print()

    if gone:
        print("── 已經好咗 / flaky 抖返 (baseline 有,今次冇) ──")
        for t in gone:
            print(f"   ✓ {t}")
        print()

    if new:
        print("── 新嘅 failure = REGRESSION ──")
        for t in new:
            print(f"   ✗ {t}")
        print()
        print("REGRESSION DETECTED — 上面的 test 之前係 pass,今次 fail。")
        print(f"pytest exit code: {rc}")
        return 1

    print("✅ 冇 regression — 所有 failure 都係 baseline 已知嘅 pre-existing。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
