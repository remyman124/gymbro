#!/usr/bin/env python3
"""Repo 衛生檢查(v3.3.33 — 2026-10-21 detached HEAD 事故之後加)。

2026-10-21 事故:做 A/B 對照時喺主 working tree 跑 `git checkout <commit>`,
`sed`/merge 之後 HEAD 變咗 detached,新 commit 冇掛喺任何 branch 上 ——
任何人 `git checkout main` 就會令佢變孤兒 commit(只靠 reflog 救)。
呢個 QA 組專門捉呢類狀態。

跑法:直接 python3 tests/qa/test_repo_hygiene.py
"""
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/work/projects/gymbro")

PASS, FAIL = "PASS", "FAIL"
results = []


def git(*args, cwd=REPO):
    r = subprocess.run(["git", *args], cwd=str(cwd),
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def check(name, ok, detail=""):
    results.append((name, PASS if ok else FAIL, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f" — {detail}" if detail else ""))
    return ok


print("== repo 衛生檢查 ==")

# 1. 唔可以喺 detached HEAD
code, out, _ = git("branch", "--show-current")
check("唔喺 detached HEAD 狀態", out not in ("", "HEAD"),
      f"branch={out or '(detached)'}")

# 2. HEAD 必須有 branch 名(唔可以 "(HEAD detached from ...)")
code, out, _ = git("status", "--porcelain=v1", "-b")
check("status 顯示 branch 身份(唔係 detached)", out.startswith("## ") and
      "detached" not in out.splitlines()[0], out.splitlines()[0][:80] if out else "")

# 3. 至少有一個本地 branch 指向 HEAD 個 commit(唔好孤兒 commit)
code, head_sha, _ = git("rev-parse", "HEAD")
code, branches, _ = git("branch", "--format=%(refname:short) %(objectname)")
pointing = [b.split()[0] for b in branches.splitlines()
            if len(b.split()) == 2 and b.split()[1] == head_sha]
check("有 branch 指向 HEAD 個 commit(唔係孤兒)", bool(pointing),
      f"HEAD={head_sha[:7]} → {pointing or '冇!'}")

# 4. 核心 code 檔唔可以有未提交改動
#    (唔好禁止其他檔案 —— screenshot 之類可以自由變動)
dirty_core = []
for f in ("gym_web.py", "templates/index.html", "gym_web/food_context.py",
          "gym_web/food_image.py", "tests/qa/run_qa.sh"):
    code, out, _ = git("status", "--porcelain", "--", f)
    if out.strip():
        dirty_core.append(f)
check("核心 code 檔同已提交版本一致", not dirty_core,
      f"有改動: {dirty_core}" if dirty_core else "")

# 5. 已提交版本號 == 檔案內 __version__
code, out, _ = git("show", "HEAD:gym_web.py")
committed_ver = ""
for line in out.splitlines():
    if line.strip().startswith("__version__"):
        committed_ver = line.split("=", 1)[1].strip().strip('"').strip("'")
        break
live_ver = ""
for line in (REPO / "gym_web.py").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("__version__"):
        live_ver = line.split("=", 1)[1].strip().strip('"').strip("'")
        break
check("工作區版本號 == 已提交版本號", committed_ver == live_ver and bool(live_ver),
      f"committed={committed_ver} live={live_ver}")

# 6. 唔可以留低多餘 worktree(會令人搞錯邊個係現行 code)
code, wt, _ = git("worktree", "list")
wt_lines = [l for l in wt.splitlines() if l.strip()]
check("冇多餘 git worktree", len(wt_lines) <= 1,
      f"{len(wt_lines)} 個: {[l.split()[0] for l in wt_lines]}")

# 7. 統一寫入閘仍然喺 writer 入面(唔准被 v3.3.33 改動搞走)
src = (REPO / "gym_web.py").read_text(encoding="utf-8")
check("production writer 仍然有 _sheet_write_guard",
      "_sheet_write_guard(" in src and
      "def _append_to_sheet_nutrition" in src)

# 8. 平行 worker 嘅兩個關鍵鎖仍然存在
check("平行安全網齊全(_SHEET_WRITE_LOCK + _SCAN_JOBS_PERSIST_LOCK)",
      "_SHEET_WRITE_LOCK" in src and "_SCAN_JOBS_PERSIST_LOCK" in src)

# 9. Frimpong 修正過嘅進度公式同 completed 語意仍然喺度
check("進度公式正確(done_count*100 + stage_frac)",
      "done_count * 100" in src or "done_count*100" in src)
check("completed 用 done_count(唔係 len(results))",
      "completed=done_count" in src)

failed = [r for r in results if r[1] == FAIL]
print(f"\n  {len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    print("  ❌ FAILED: " + ", ".join(f[0] for f in failed))
    sys.exit(1)
print("  ✅ 全部通過")
