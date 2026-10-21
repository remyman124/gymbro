#!/usr/bin/env python3
"""由多個 pytest run 嘅輸出建 union baseline。

Isak 2026-10-20。

點解要 union?
  gymbro suite 有一批 flaky e2e test(全部係 `networkidle` timeout 競態 ——
  PWA 有持續 polling,networkidle 永遠唔 settle,10 秒 timeout 係博彩)。
  同一個 test 喺唔同 run 會時而 pass 時而 fail。

  如果 baseline 只記錄單次 run,flaky test 就會喺 A 次 run 之間
  「NEW 咗又 GONE 咗」→ 假 regression 警報。

  Union baseline:任何一次 run fail 過嘅 test,永遠當 pre-existing。
  → 唔會誤報,亦唔會漏報真 regression(真 regression 每次都 fail)。

用法:
    PYTHONPATH="" .venv/bin/python tests/qa/build_baseline.py \
        --out tests/qa/pytest_baseline.json /tmp/cap_run1.txt /tmp/cap_run2.txt
"""

import argparse
import json
import platform
import re
import sys
from pathlib import Path

QA_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = QA_DIR / "pytest_baseline.json"

NOTE = (
    "live server / shared Redis gymbro:overlay:health / stale gym-web-v121 assert"
    " / playwright networkidle timeout (gymbro PWA 有持續 polling,networkidle 唔會 settle)"
)


def parse(output: str):
    failed, errored = set(), set()
    for m in re.finditer(r"^FAILED\s+(\S+)", output, re.MULTILINE):
        failed.add(_norm(m.group(1)))
    for m in re.finditer(r"^ERROR\s+(\S+)", output, re.MULTILINE):
        errored.add(_norm(m.group(1)))
    for m in re.finditer(r"^(\S+::\S+)\s+FAILED", output, re.MULTILINE):
        failed.add(_norm(m.group(1)))
    for m in re.finditer(r"^(\S+::\S+)\s+ERROR", output, re.MULTILINE):
        errored.add(_norm(m.group(1)))
    return failed, errored


def _norm(tid: str) -> str:
    tid = tid.strip()
    if " - " in tid:
        tid = tid.split(" - ", 1)[0]
    return tid.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="pytest 輸出檔 (每個係一次 run)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--python", default=sys.executable,
                    help="產生呢啲 run 嘅 interpreter (記錄 fingerprint)")
    args = ap.parse_args()

    failed, errored = set(), set()
    per_run = []
    qa_leaks = []
    for path in args.runs:
        p = Path(path)
        if not p.exists():
            print(f"  ! 跳過(唔存在): {p}", file=sys.stderr)
            continue
        f, e = parse(p.read_text(errors="replace"))
        # ⚠️ 污染防護:如果個 capture 冇用 `--ignore=tests/qa`,
        #    入面會混入 QA 自己嘅測試 id。呢啲唔係 suite 嘅 pre-existing
        #    failure,放咗入 baseline 會污染比對(最壞情況:QA test 嘅
        #    失敗會被當成 suite 嘅 known failure,將來真 regression 都唔報)。
        qa_f = {t for t in f if t.startswith("tests/qa")}
        qa_e = {t for t in e if t.startswith("tests/qa")}
        if qa_f or qa_e:
            qa_leaks.append((p.name, len(qa_f | qa_e)))
            f -= qa_f
            e -= qa_e
        per_run.append((p.name, len(f | e)))
        failed |= f
        errored |= e

    if qa_leaks:
        print("  ⚠️  以下 capture 入面混入咗 tests/qa 自己嘅測試 id,已自動排除:")
        for name, n in qa_leaks:
            print(f"       {name}: {n} 條")
        print("      (提示: 產生 capture 嗰陣記得加 `--ignore=tests/qa`)")

    # ⚠️ 同一個 test 喺唔同 run 入面可能一次 FAILED、一次 ERROR
    #    (例如 flaky 嘅 e2e test)。Union 完之後要統一 ——
    #    同一個 id 唔應該同時出現喺 failed 同 errored。
    #    統一放去 failed(因為「FAIL 咗」語意上覆蓋「ERROR 咗」),
    #    避免 baseline-diff 計 SAME 時重複計數。
    overlap = failed & errored
    if overlap:
        print(f"  ⚠️  {len(overlap)} 個 test id 喺 failed 同 errored 都出現過 "
              f"(flaky),統一歸入 failed:")
        for t in sorted(overlap):
            print(f"       {t}")
        errored -= overlap

    ver = "unknown"
    try:
        import subprocess
        r = subprocess.run([args.python, "-V"], capture_output=True, text=True,
                           timeout=30)
        ver = (r.stdout or r.stderr or "").strip()
    except Exception:
        pass

    payload = {
        "note": NOTE,
        "failed": sorted(failed),
        "errored": sorted(errored),
        "fingerprint": {
            "python": ver,
            "executable": str(Path(args.python).resolve()),
            "platform": platform.platform(),
        },
        "runs_recorded": len(per_run),
        "runs_detail": [{"file": n, "not_passing": c} for n, c in per_run],
    }
    out = Path(args.out)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"baseline 已寫入: {out}")
    print(f"  failed={len(failed)}  errored={len(errored)}  "
          f"union={len(failed | errored)}")
    print(f"  runs: {per_run}")
    print(f"  fingerprint: {ver}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
