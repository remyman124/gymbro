"""v3.3.39 regression —「落 set」快路 + 補記過去一節 + 修 MCP 死 port。

2026-10-23 實測故障 (Jim):「落 set」
背景:本機 workout log 只有 7/19-8/28 嗰 10 日有 set,之後 60 日全部 0 組
     → app-tap log 死咗,但後端一直冇壞 → 問題係「冇快路」。

守:
  1. `/api/log_set` 支援選填 `date`(YYYY-MM-DD)→ 補記過去一節
  2. `set_n` 唔俾就自動遞增(同一動作喺嗰一節已做幾多組 + 1)
  3. **唔准再出現 `log[today_iso()] = session`** → 會令補記覆蓋今日 session
  4. MCP `log_meal_text` 唔准再 POST 去死 port 4280
  5. script 解析規則(中英夾雜 / 自重 / xN 展開 / 重量+多組次數)

全部測試都唔寫入任何本機 log 或 Google Sheet(純解析 + 靜態檢查)。
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

REPO = Path("/home/work/projects/gymbro")
GYM_WEB = REPO / "gym_web.py"
MCP = REPO / "gymbro_mcp_server.py"
SCRIPT = REPO / "scripts" / "log_sets_from_text.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("log_sets_from_text", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 1. script 存在 + 解析規則 ───────────────────────────────────────────────

def test_script_exists():
    assert SCRIPT.exists(), "搵唔到 scripts/log_sets_from_text.py"


@pytest.mark.parametrize("text,expected", [
    ("臥推 60x8 60x8", [("BB Bench Press", 60.0, 8), ("BB Bench Press", 60.0, 8)]),
    ("深蹲 80x10", [("Squat", 80.0, 10)]),
    ("肩推 40x10x3", [("DB OHP", 40.0, 10)] * 3),
    ("硬拉 120 5 5", [("Deadlift", 120.0, 5), ("Deadlift", 120.0, 5)]),
    ("引體上升 8", [("Pull-ups", None, 8)]),
    ("伏地挺身 15", [("Push-ups", None, 15)]),
    ("BB Bench Press 100x5", [("BB Bench Press", 100.0, 5)]),
])
def test_parse_formats(text, expected):
    mod = _load_script()
    got = [(p["exercise"], p.get("weight_kg"), p["reps"]) for p in mod.parse_text(text)]
    assert got == expected, f"{text!r} → {got},期望 {expected}"


def test_parse_multiple_exercises_comma_separated():
    mod = _load_script()
    got = [(p["exercise"], p.get("weight_kg"), p["reps"])
           for p in mod.parse_text("臥推 60x8, 深蹲 80x10")]
    assert got == [("BB Bench Press", 60.0, 8), ("Squat", 80.0, 10)]


def test_parse_bare_weight_uses_default_reps():
    mod = _load_script()
    got = [(p["exercise"], p.get("weight_kg"), p["reps"]) for p in mod.parse_text("臥推 60")]
    assert got == [("BB Bench Press", 60.0, 10)]


def test_parse_unknown_exercise_kept_verbatim():
    mod = _load_script()
    got = [p["exercise"] for p in mod.parse_text("波比跳 30")]
    assert got == ["波比跳"], "認唔到嘅動作應該原樣保留中文名"


def test_script_never_touches_sheet():
    """set log 只寫本機 log,唔准直接寫 Google Sheet。"""
    src = SCRIPT.read_text()
    for bad in ("gspread", "spreadsheets.googleapis", "sheets.googleapis",
                "values/Nutrition", "WORKOUT_LOG_TAB"):
        assert bad not in src, f"script 唔應該出現 {bad}"


# ── 2. /api/log_set 支援 date + set_n 自動遞增 ──────────────────────────────

def _gym_web_src():
    return GYM_WEB.read_text()


def test_log_set_accepts_date():
    src = _gym_web_src()
    assert 'data.get("date")' in src, "/api/log_set 應該支援 date 參數"
    assert 'date must be YYYY-MM-DD' in src, "date 格式錯誤要有明確 400 訊息"


def test_log_set_auto_increments_set_n():
    src = _gym_web_src()
    assert 'if data.get("set_n") is None:' in src, "set_n 唔俾就要自動遞增"
    assert 'e.get("exercise") == exercise' in src, "set_n 自動遞增要按同一動作計"


def test_no_hardcoded_today_key_write():
    """`log[today_iso()] = session` 會令補記覆蓋今日 session → 唔准再存在。"""
    src = _gym_web_src()
    assert "log[today_iso()] = session" not in src, "仲有硬寫 today_iso() 嘅寫入"
    assert 'log[session.get("date") or today_iso()] = session' in src


def test_get_or_create_session_accepts_date():
    tree = ast.parse(_gym_web_src())
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "get_or_create_session"), None)
    assert fn is not None
    args = [a.arg for a in fn.args.args]
    assert "session_date" in args, "get_or_create_session 要支援 session_date"


# ── 3. MCP:死 port 修好 ─────────────────────────────────────────────────────

def test_mcp_has_no_dead_port():
    """用 AST 睇真實賦值,唔好俾註釋/docstring 誤傷。"""
    tree = ast.parse(MCP.read_text())
    assigned = {
        t.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    assert "GYM_WEB_URL" not in assigned, "仲留住 GYM_WEB_URL 賦值(死 port 4280)"
    assert "GYM_WEB_BASE" in assigned
    src = MCP.read_text()
    assert 'GYM_WEB_BASE = "http://127.0.0.1:7000"' in src


def test_mcp_log_meal_text_uses_gym_web_base():
    tree = ast.parse(MCP.read_text())
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "log_meal_text"), None)
    assert fn is not None
    names = {n.id for n in ast.walk(fn)
             if isinstance(n, ast.Name)}
    assert "GYM_WEB_BASE" in names, "log_meal_text 要用 GYM_WEB_BASE"
    assert "GYM_WEB_URL" not in names, "log_meal_text 唔可以再用 GYM_WEB_URL"


def test_mcp_log_set_has_date_param():
    tree = ast.parse(MCP.read_text())
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "log_set"), None)
    assert fn is not None
    names = [a.arg for a in fn.args.args]
    assert "date" in names, "MCP log_set 要有 date 參數"


# ── 4. skill 存在 ───────────────────────────────────────────────────────────

def test_skill_exists():
    skill = Path("/home/work/.hermes/skills/fitness/gymbro-set-logging/SKILL.md")
    assert skill.exists(), "fitness/gymbro-set-logging skill 唔見咗"
