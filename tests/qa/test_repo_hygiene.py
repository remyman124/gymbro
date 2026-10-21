"""Repo 衛生檢查(v3.3.33 — 2026-10-21 detached HEAD 事故之後加)。

2026-10-21 事故:做 A/B 對照時喺主 working tree 跑 `git checkout <commit>`,
`sed`/merge 之後 HEAD 變咗 detached,新 commit 冇掛喺任何 branch 上 ——
任何人 `git checkout main` 就會令佢變孤兒 commit(只靠 reflog 救)。
呢個 QA 組專門捉呢類狀態。

跑法:pytest tests/qa/test_repo_hygiene.py
"""
import subprocess
from pathlib import Path

import pytest

REPO = Path("/home/work/projects/gymbro")


def git(*args, cwd=REPO):
    r = subprocess.run(["git", *args], cwd=str(cwd),
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def test_not_in_detached_head():
    """唔可以喺 detached HEAD 狀態。"""
    code, out, _ = git("branch", "--show-current")
    assert out not in ("", "HEAD"), (
        f"跌咗入 detached HEAD 狀態(branch={out or 'empty'})—— "
        "即刻 `git checkout main` 救返,唔好 commit 落去")


def test_status_shows_branch_identity():
    """status 第一行要顯示 branch 身份,唔係 detached。"""
    code, out, _ = git("status", "--porcelain=v1", "-b")
    first = out.splitlines()[0] if out else ""
    assert out.startswith("## "), f"status 第一行唔似 branch 行:{first!r}"
    assert "detached" not in first, f"跌咗入 detached HEAD:{first!r}"


def test_head_is_not_an_orphan_commit():
    """至少有一個本地 branch 指向 HEAD 個 commit。"""
    code, head_sha, _ = git("rev-parse", "HEAD")
    assert head_sha, "攞唔到 HEAD"
    code, branches, _ = git("branch", "--format=%(refname:short) %(objectname)")
    pointing = [b.split()[0] for b in branches.splitlines()
                if len(b.split()) == 2 and b.split()[1] == head_sha]
    assert pointing, (
        f"HEAD={head_sha[:7]} 冇任何 branch 指向佢 —— 呢個係孤兒 commit,"
        "任何人 checkout 個 branch 就會消失")


def test_core_code_files_match_committed_version():
    """核心 code 檔唔可以有未提交改動。

    (唔好禁止其他檔案 —— screenshot 之類可以自由變動。)
    """
    dirty = []
    for f in ("gym_web.py", "templates/index.html", "gym_web/food_context.py",
              "gym_web/food_image.py", "tests/qa/run_qa.sh"):
        code, out, _ = git("status", "--porcelain", "--", f)
        if out.strip():
            dirty.append(f)
    assert not dirty, (
        f"核心 code 檔有未提交改動:{dirty} —— "
        "server serve 嘅嘢同已提交版本唔一致")


def test_version_matches_committed_version():
    """工作區 __version__ == 已提交版本嘅 __version__。"""
    code, out, _ = git("show", "HEAD:gym_web.py")

    def _ver(text):
        for line in text.splitlines():
            if line.strip().startswith("__version__"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
        return ""

    committed = _ver(out)
    live = _ver((REPO / "gym_web.py").read_text(encoding="utf-8"))
    assert committed, "攞唔到已提交版本號"
    assert committed == live, (
        f"工作區版本號 {live} != 已提交版本號 {committed} —— "
        "server 可能 serve 住舊 code")


def test_no_leftover_worktrees():
    """唔可以留低多餘 worktree(會令人搞錯邊個係現行 code)。"""
    code, wt, _ = git("worktree", "list")
    lines = [l for l in wt.splitlines() if l.strip()]
    assert len(lines) <= 1, (
        f"有多餘 git worktree:{[l.split()[0] for l in lines]} —— "
        "對照完要 `git worktree remove`")


def test_write_guard_still_in_place():
    """統一寫入閘仍然喺 writer 入面(唔准被改動搞走)。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    assert "_sheet_write_guard(" in src, "寫入閘唔見咗"
    assert "def _append_to_sheet_nutrition" in src, "production writer 唔見咗"


def test_parallel_safety_locks_present():
    """平行 worker 嘅兩個關鍵鎖仍然存在。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    assert "_SHEET_WRITE_LOCK" in src, "Sheet 寫入鎖唔見咗"
    assert "_SCAN_JOBS_PERSIST_LOCK" in src, "job 持久化鎖唔見咗"


def test_pct_formula_and_completed_semantics():
    """Frimpong 修正過嘅進度公式同 completed 語意仍然喺度。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    assert "done_count * 100" in src or "done_count*100" in src, (
        "進度公式唔啱 —— 會令進度條跳 99% 然後 pin 住(假進度)")
    assert "completed=done_count" in src, (
        "completed 用返 len(results) —— 一張相失敗就永遠到唔到 100%")


def test_no_perplexity_dependency():
    """Perplexity 已拎走(Jim OOB 2026-10-21)—— 唔准黐返。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    code_only = "\n".join(l for l in src.splitlines()
                          if not l.strip().startswith("#"))
    for dead in ("_perplexity_vision", "_pplx_enrich", "_pplx_check_narration",
                 "_pplx_extract_dish_name", "_pplx_api_key"):
        assert dead not in code_only, f"仲有 Perplexity 依賴:{dead}"
    assert "api.perplexity.ai" not in code_only, "仲有 Perplexity endpoint call"
