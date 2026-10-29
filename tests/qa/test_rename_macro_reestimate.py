"""v3.3.37 regression — 改名一定要重新估算營養。

2026-10-22 實測故障 (Jim OOB):
    「The food recognition is not wrong. It recognized as 白飯 only.
      When i change the name to 海南雞白飯 the macro detail is unchanged」

真因(兩個 endpoint 一樣錯):
    new_kcal, new_p, new_c, new_f = old_kcal, old_p, old_c, old_f   # 舊值做預設
    ...
    if new_kcal <= 0:      # ← AI 層先會套用 → 舊值 > 0 → AI 永遠唔生效
    → 白飯(260 kcal)改名做海南雞白飯:名變咗,kcal/P/C/F 一模一樣。

修法:抽 `_reestimate_macros_for_name()`(4 層鏈:explicit → brand → AI → history),
     兩個 endpoint 都由 0 起步行鏈,全部失敗先回落舊值(source=kept_previous)。

測試策略:用 AST 抽 helper 出嚟自己 exec(唔使 import 成個 Flask app),
再注入 mock 依賴,驗證 4 層優先次序 + 全部失敗嘅行為。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO = Path("/home/work/projects/gymbro")
GYM_WEB = REPO / "gym_web.py"


def _load_helper():
    """抽 `_reestimate_macros_for_name` 出嚟,注入 mock 依賴後 exec。"""
    src = GYM_WEB.read_text()
    tree = ast.parse(src)
    node = next(
        (n for n in tree.body
         if isinstance(n, ast.FunctionDef) and n.name == "_reestimate_macros_for_name"),
        None,
    )
    assert node is not None, "搵唔到 _reestimate_macros_for_name"
    ns: dict = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(GYM_WEB), "exec"), ns)
    fn = ns["_reestimate_macros_for_name"]

    def _install(explicit=None, brand=None, ai=None, history=None):
        ns["_extract_explicit_macros"] = lambda _n: (explicit or {})
        ns["_lookup_brand_macros"] = lambda _n: (brand or {})
        ns["_minimax_nutrition_enrich"] = lambda _t: (ai or "")
        ns["_safe_parse_nutrition_json"] = lambda s: (json.loads(s) if s else None)
        ns["_lookup_known_macros"] = lambda _n, **_kw: (history or {})

    return fn, _install


@pytest.fixture()
def helper():
    fn, install = _load_helper()
    install()  # 預設:全部依賴都失敗
    return fn, install


AI_HENAN = json.dumps({"calories": 520, "protein": 32, "carbs": 58, "fat": 16})


# ───────────────────────────── 4 層鏈優先次序 ─────────────────────────────
def test_ai_layer_applies_for_renamed_dish(helper):
    """核心 case:AI 層一定要生效(舊 bug 就係永遠唔生效)。"""
    fn, install = helper
    install(ai=AI_HENAN)
    out = fn("海南雞白飯")
    assert out["kcal"] == 520
    assert out["protein"] == 32
    assert out["source"] == "ai"


def test_explicit_numbers_beat_ai(helper):
    fn, install = helper
    install(explicit={"calories": 180, "protein": 30, "carbs": 5, "fat": 4}, ai=AI_HENAN)
    out = fn("30g protein shake")
    assert out["kcal"] == 180
    assert out["protein"] == 30
    assert out["source"] == "explicit"


def test_brand_table_beats_ai(helper):
    fn, install = helper
    install(brand={"kcal": 120, "protein": 24, "carbs": 3, "fat": 1, "_source": "brand_table"},
            ai=AI_HENAN)
    out = fn("Quest Bar PB")
    assert out["kcal"] == 120
    assert out["source"] == "brand"


def test_history_is_last_resort(helper):
    fn, install = helper
    install(history={"kcal": 260, "protein": 4, "carbs": 47, "fat": 3, "_source": "sheet_history"})
    out = fn("白飯")
    assert out["kcal"] == 260
    assert out["source"] == "history"


def test_all_layers_fail_returns_none_source(helper):
    """全部層失敗 → 交返俾 caller 回落舊值(唔會自己變 0)。"""
    fn, install = helper
    install()
    out = fn("海南雞白飯")
    assert out["kcal"] == 0
    assert out["source"] == "none"


def test_ai_failure_does_not_raise(helper):
    """AI 死咗唔可以拋 exception(要安全回落)。"""
    fn, install = helper
    install(ai="")           # 空回應
    out = fn("海南雞白飯")
    assert out["source"] == "none"

    ns_explode = {}

    def _boom(_t):
        raise RuntimeError("MiniMax 502")

    fn2, install2 = _load_helper()
    install2(ai="")
    fn2.__globals__["_minimax_nutrition_enrich"] = _boom
    out2 = fn2("海南雞白飯")
    assert out2["kcal"] == 0
    assert out2["source"] == "none"
    assert ns_explode == {}


# ───────────────────────────── 源碼層保險 ─────────────────────────────
def test_rename_no_longer_seeds_new_macros_from_old_values():
    """舊嗰行「由舊值起步」嘅 seed 一定要只喺 fallback 入面出現。"""
    src = GYM_WEB.read_text()
    seed = "new_kcal, new_p, new_c, new_f = old_kcal, old_p, old_c, old_f"
    assert src.count(seed) == 1, \
        "舊 seed 行出現多過一次(如果 > 1,起手嗰次又會鎖死 AI 層)"
    helper_pos = src.index("_reestimate_macros_for_name(new_name, restaurant")
    seed_pos = src.index(seed)
    assert seed_pos > helper_pos, "seed 行仲喺 helper 之前 → 仍然由舊值起步"
    # 唯一嗰次必須喺「全部層失敗 → 回落舊值」嘅 guard 入面
    # v3.3.66:fallback 改成兩層(partial / 全失敗)—— seed 行只可以出現喺
    # 「全部層都失敗」嗰個 else 入面。
    guard = "    if new_kcal <= 0:\n        if (new_p or new_c or new_f):"
    assert guard in src, "fallback guard 唔見咗"
    assert src.index(guard) < seed_pos, "seed 行唔喺 fallback guard 入面"


def test_rename_has_kept_previous_fallback():
    src = GYM_WEB.read_text()
    assert 'macros_source = "kept_previous"' in src, "缺 kept_previous 回落"
    assert "kept previous macros" in src


def test_scan_correct_recalculates_when_name_changed():
    src = GYM_WEB.read_text()
    assert "name_changed = bool(sup_name) and sup_name != (row.name or \"\")" in src
    assert "(row.kcal <= 0 or name_changed)" in src, \
        "scan_correct 仍然淨係喺 row.kcal <= 0 先重估 → 改名唔會更新營養"


def test_scan_correct_uses_shared_helper():
    src = GYM_WEB.read_text()
    assert "_reestimate_macros_for_name(sup_name" in src, \
        "scan_correct 未用共用 helper(會漏 AI 層)"


def test_rename_response_reports_macros_source():
    src = GYM_WEB.read_text()
    assert '"macros_source": macros_source' in src
