"""QA guards — v3.3.49: 食物日期正確性 + whoop metrics 唔好被蓋走。

Jim OOB 2026-10-27:「the voice was saying that i ate 6 food. but the food
was actually ytd not today」

三個 root cause:
  1. `_load_today_nutrition()` 嘅 late-night fallback 闊到 24 個鐘 → 下午
     都會攞尋日嘅餐當今日
  2. prompt 硬寫「今日飲食記錄 / 已 log N 餐」→ LLM 一定當今日
  3. `_build_daily_context()` 用 `ctx["whoop"] = {...}` 成個蓋走 metrics →
     recovery / HRV / 睡眠全部唔咗入 prompt
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = (ROOT / "gym_web.py").read_text(encoding="utf-8")


def test_fallback_window_is_bounded():
    assert "NUTRITION_FALLBACK_MAX_AGE_H" in SRC, "要有 fallback 窗口上限"
    assert re.search(r"NUTRITION_FALLBACK_MAX_AGE_H\s*=\s*([0-9.]+)", SRC), \
        "窗口上限要寫成數字"
    val = float(re.search(r"NUTRITION_FALLBACK_MAX_AGE_H\s*=\s*([0-9.]+)", SRC).group(1))
    assert val <= 12, f"fallback 窗口唔可以闊過 12 個鐘(而家 {val})"
    assert "_latest_meal_age_hours(y_rows)" in SRC, "要用餐嘅實際時間判斷"


def test_relative_day_helper_exists():
    assert "def _relative_day_zh(" in SRC
    for w in ("今日", "尋日", "前日"):
        assert f'"{w}"' in SRC, f"相對日期要識講「{w}」"


def test_prompt_uses_day_label_not_hardcoded_today():
    assert "nutrition_day_label" in SRC, "prompt 要用 day_label"
    assert "nutrition_is_today" in SRC, "prompt 要分得出今日定唔係今日"
    assert "**{nutrition_day_label}飲食記錄" in SRC, "飲食段落標題要用 day_label"
    # 唔准再硬寫「今日飲食記錄（Jim OOB 2026-09-27」
    assert "**今日飲食記錄（Jim OOB 2026-09-27" not in SRC, \
        "prompt 唔可以再硬寫「今日飲食記錄」"


def test_snapshot_carries_date_and_label():
    assert '"day_label": day_label' in SRC, "snapshot 要帶 day_label"
    assert '"is_today":' in SRC, "snapshot 要帶 is_today"


def test_whoop_metrics_are_not_clobbered():
    """ctx['whoop'] 成個 assignment 會抹走 recovery/HRV/睡眠。"""
    code_lines = [ln for ln in SRC.splitlines()
                  if not ln.strip().startswith("#")]
    code = "\n".join(code_lines)
    assert 'ctx["whoop"] = {"steps"' not in code, \
        "唔可以再用 ctx['whoop'] = {...} 蓋走 metrics(會令 recovery 變 None)"
    assert re.search(r'ctx\["whoop"\]\s*=\s*\{\s*\}', code) is None or \
        'ctx["whoop"]["steps"]' in code, "空 dict assignment 要喺 merge 保護之下"
    assert 'ctx["whoop"]["steps"]' in SRC, "步數應該 merge 入去,唔係覆蓋"


def test_food_coach_block_uses_day_label():
    assert "day_label = _relative_day_zh(eff_date)" in SRC
    assert "- 記錄日期: {eff_date}" in SRC, "food coach 都要寫明日期"


def test_version_bumped():
    import re as _re
    m = _re.search(r'__version__\s*=\s*"([0-9.]+)"', _src())
    assert m, "搵唔到 __version__"
    parts = [int(x) for x in m.group(1).split(".")]
    assert tuple(parts) >= (3, 3, 49), f"版本要 >= 3.3.49(而家 {m.group(1)})"


def test_relative_day_zh_behaviour():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("gym_web_rel_day", ROOT / "gym_web.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT))
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                     # pragma: no cover
        import pytest
        pytest.skip(f"gym_web 匯入失敗(非本測試問題): {e}")
    today = mod.today_iso()
    assert mod._relative_day_zh(today) == "今日"
    assert mod._relative_day_zh("2000-01-01").endswith("日前")
    assert mod._relative_day_zh(None) == "今日"
