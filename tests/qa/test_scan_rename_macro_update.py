"""
v3.3.66 — 改名後營養唔更新 QA(Jim OOB 2026-10-29)

Jim OOB:「I uploaded today food. But after I changed the food name, the
nutrition info does not update.」

真實事故(2026-10-29):
  Jim 上載「朱古力蛋白粉」→ 改名做「朱古力蛋白粉，27 Gram蛋白質」→ 營養冇更新。

  Root cause chain(server log 實證):
    [macro-reestimate] AI failed (empty/unparsable) for '朱古力蛋白粉，27 Gram蛋白質'
    [macro-reestimate] history → 290 kcal for '朱古力蛋白粉，27 Gram蛋白質'

    1. `_extract_explicit_macros` 只識 "27g protein" → 捉唔到「27 Gram蛋白質」
       → 落去 AI 層
    2. `_minimax_nutrition_enrich` timeout=30s + 冇 retry + 冇關 thinking
       → MiniMax M3 跑 reasoning tokens 慢到爆 → TimeoutError
    3. fallback 去 history 層 → 攞返**同一行自己嘅舊值 290 kcal**
       → 睇落就似「改名完全冇更新營養」
    4. 前端仲用 ✓ 綠色講「改名成功」→ Jim 以為搞掂

呢組測試係 unit level + source level:
唔打 network、唔打 Google API、唔掂 Jim 個 Sheet、唔產生 TTS 成本。
"""

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
GYM_WEB = REPO / "gym_web.py"
TPL = REPO / "templates" / "index.html"

JIM_NAME = "朱古力蛋白粉，27 Gram蛋白質"


@pytest.fixture(scope="module")
def gw():
    spec = importlib.util.spec_from_file_location("gym_web_rename_qa", GYM_WEB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def src():
    return GYM_WEB.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────
# 1. explicit 層:捉到「27 Gram蛋白質」
# ─────────────────────────────────────────────────────────────
def test_explicit_parses_gram_protein_chinese(gw):
    out = gw._extract_explicit_macros(JIM_NAME)
    assert out.get("protein") == 27.0


@pytest.mark.parametrize("name,expect", [
    ("朱古力蛋白粉，27 Gram蛋白質", {"protein": 27.0}),
    ("朱古力蛋白粉，27 grams 蛋白質", {"protein": 27.0}),
    ("朱古力蛋白粉 27克蛋白質", {"protein": 27.0}),
    ("朱古力蛋白粉 27 g 蛋白質", {"protein": 27.0}),
    ("朱古力蛋白粉 27g protein", {"protein": 27.0}),
    ("燕麥片 40克碳水", {"carbs": 40.0}),
    ("杏仁 12克脂肪", {"fat": 12.0}),
])
def test_explicit_variants(gw, name, expect):
    out = gw._extract_explicit_macros(name)
    for k, v in expect.items():
        assert out.get(k) == v, f"{name} → {out}"


def test_explicit_no_false_positive_on_plain_name(gw):
    """普通名唔可以亂抽數(之前加咗中文 alias,唔可以抽錯)。"""
    out = gw._extract_explicit_macros("白飯")
    assert out == {} or not out.get("protein")
    out2 = gw._extract_explicit_macros("海南雞白飯")
    assert not out2.get("protein")


def test_explicit_does_not_parse_macro_word_before_number(gw):
    """「蛋白質每餐爭取 35 克」唔係食物名,唔應該當成 P=35。"""
    out = gw._extract_explicit_macros("蛋白質每餐爭取 35 至 45 克")
    assert out.get("protein") is None


# ─────────────────────────────────────────────────────────────
# 2. AI 層:唔會因為 timeout 而靜靜哋死
# ─────────────────────────────────────────────────────────────
def test_enrich_has_retry_and_longer_timeout(src):
    i = src.find("def _minimax_nutrition_enrich(")
    body = src[i:src.find("\ndef ", i + 1)]
    assert "for _attempt in range(3)" in body
    assert "timeout=75" in body
    # 註釋可以提舊 timeout,但實際 code 唔可以仲用 30s
    assert "timeout=30)" not in body and "timeout=30," not in body


def test_enrich_disables_thinking(src):
    """M3 開 thinking 慢到爆 → timeout → 靜靜哋死。"""
    i = src.find("def _minimax_nutrition_enrich(")
    body = src[i:src.find("\ndef ", i + 1)]
    assert '"thinking": {"type": "disabled"}' in body


def test_enrich_logs_failure_instead_of_silent(src):
    i = src.find("def _minimax_nutrition_enrich(")
    body = src[i:src.find("\ndef ", i + 1)]
    assert "[minimax_enrich] attempt" in body
    # 唔准 bare except pass
    assert "except Exception: pass" not in body


# ─────────────────────────────────────────────────────────────
# 3. history 層:唔可以攞返同一行自己嘅舊值
# ─────────────────────────────────────────────────────────────
def test_lookup_known_macros_supports_exclude_row(src):
    assert "def _lookup_known_macros(dish_name: str, exclude_row: int = 0)" in src
    i = src.find("def _lookup_known_macros(")
    body = src[i:src.find("\ndef ", i + 1)]
    assert "exclude_row" in body


def test_reestimate_passes_exclude_row(src):
    assert "def _reestimate_macros_for_name(dish_name: str, restaurant: str = \"\"," in src
    i = src.find("def _reestimate_macros_for_name(")
    body = src[i:src.find("\ndef ", i + 1)]
    assert "exclude_row=exclude_row" in body


def test_api_scan_rename_passes_scan_index(src):
    i = src.find("def api_scan_rename(")
    body = src[i:src.find("\ndef ", i + 1)]
    assert "exclude_row=scan_index" in body


# ─────────────────────────────────────────────────────────────
# 4. AI 唔可以覆蓋 explicit 數字
# ─────────────────────────────────────────────────────────────
def test_ai_does_not_override_explicit(src):
    i = src.find("def _reestimate_macros_for_name(")
    body = src[i:src.find("\ndef ", i + 1)]
    assert 'if a_p > 0 and out["protein"] <= 0: out["protein"] = a_p' in body
    assert "if a_p > 0: out[\"protein\"] = a_p" not in body


# ─────────────────────────────────────────────────────────────
# 5. Partial 情況:explicit 有數但 kcal 估唔到
# ─────────────────────────────────────────────────────────────
def test_partial_source_handled(src):
    assert '"explicit_partial"' in src
    i = src.find("def api_scan_rename(")
    body = src[i:src.find("\ndef ", i + 1)]
    assert "explicit_partial" in body
    assert "macros_warning" in body


def test_kept_previous_fallback_still_exists(src):
    """全部層失敗仍然唔可以把一行變 0。"""
    i = src.find("def api_scan_rename(")
    body = src[i:src.find("\ndef ", i + 1)]
    assert 'macros_source = "kept_previous"' in body
    assert "new_kcal, new_p, new_c, new_f = old_kcal, old_p, old_c, old_f" in body


# ─────────────────────────────────────────────────────────────
# 6. 前端提示:唔可以用綠色 ✓ 講「成功」但其實冇更新
# ─────────────────────────────────────────────────────────────
def test_frontend_warning_not_green_check():
    t = TPL.read_text(encoding="utf-8")
    assert "⚠️ 改名成功，但 AI 重新估算失敗" in t
    assert "✓ 改名成功（AI 重新估算失敗" not in t


def test_frontend_has_partial_message():
    t = TPL.read_text(encoding="utf-8")
    assert "已更新名裡面寫明嘅營養" in t


def test_frontend_amber_style():
    t = TPL.read_text(encoding="utf-8")
    assert "text-amber-300" in t


# ─────────────────────────────────────────────────────────────
# 7. Version
# ─────────────────────────────────────────────────────────────
def test_version_bumped(src):
    assert '__version__ = "3.3.70"' in src
    assert "gym-web-v174" in src
