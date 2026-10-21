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


def test_deployed_server_matches_working_tree():
    """行緊嘅 server 版本 == 工作區 `__version__`。

    (原本呢個 check 係「核心 code 檔唔可以有未提交改動」,但係 repo 係
    多個 agent 共用 —— Torres / Frimpong 會同時改檔,工作區有未提交改動
    係正常狀態,唔應該令 QA 爆。真正要保證嘅係「server 跑緊嘅嘢同
    我哋以為嘅版本一致」,所以改用版本號比對。)
    """
    import json
    import urllib.request

    live = _ver((REPO / "gym_web.py").read_text(encoding="utf-8"))
    try:
        with urllib.request.urlopen("http://127.0.0.1:7000/api/health",
                                    timeout=8) as r:
            served = json.loads(r.read()).get("version")
    except Exception:
        pytest.skip("gymbro server 未喺 7000 行(唔喺呢部機測)")
    assert served == live, (
        f"server 跑緊 v{served},但工作區係 v{live} —— "
        "重啟 server 佢先會 serve 新 code")


def _ver(text):
    for line in text.splitlines():
        if line.strip().startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def test_version_matches_committed_version():
    """工作區 __version__ == 已提交版本嘅 __version__。"""
    code, out, _ = git("show", "HEAD:gym_web.py")
    committed = _ver(out)
    live = _ver((REPO / "gym_web.py").read_text(encoding="utf-8"))
    assert committed, "攞唔到已提交版本號"
    assert committed == live, (
        f"工作區版本號 {live} != 已提交版本號 {committed} —— "
        "server 可能 serve 住舊 code")


def test_no_leftover_worktrees():
    """唔可以喺 project 區內留低多餘 worktree。

    `/tmp` 下面嘅 worktree 係**規定做法**(A/B 對照唔准喺主 working tree
    checkout),所以唔算殘留;要捉嘅係 project 區內嘅 —— 嗰啲會令人搞錯
    邊個係現行 code。
    """
    code, wt, _ = git("worktree", "list")
    lines = [l for l in wt.splitlines() if l.strip()]
    stale = []
    for l in lines:
        path = l.split()[0] if l.split() else ""
        if not path or path == str(REPO):
            continue
        if path.startswith("/tmp/"):
            continue          # scratch A/B worktree —— 對照完要自己 remove
        stale.append(path)
    assert not stale, (
        f"project 區內有多餘 git worktree:{stale} —— "
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
