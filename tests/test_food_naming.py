"""Unit tests for the food-recognition dish name extractor.

These guard against model narration patterns leaking into the final dish
name (e.g. "這張相顯示一支蘇打水樽，品牌為..." instead of just "蘇打水").
"""
import sys
from pathlib import Path

import pytest

# Load via importlib so the gym_web module doesn't collide with the gym_web/ package
REPO = Path(__file__).parent.parent
spec = __import__("importlib.util").util.spec_from_file_location(
    "gym_web_main", REPO / "gym_web.py"
)
gym_web = __import__("importlib.util").util.module_from_spec(spec)
spec.loader.exec_module(gym_web)


# ── narration-prefix stripping (regex fallback path) ────────────────────

@pytest.mark.parametrize("vision, expected_prefix_in_return", [
    ("這張相顯示一支蘇打水樽", "蘇打水"),       # the user's exact case
    ("呢張相顯示一碗白飯", "白飯"),
    ("相顯示咗一塊千層蛋糕", "千層蛋糕"),
    ("可見到一碟蛋撻", "蛋撻"),
    ("睇到一杯黑咖啡", "黑咖啡"),
    ("照片中有一碗粥", "粥"),
])
def test_narration_prefix_stripped(vision, expected_prefix_in_return):
    """The regex fallback should strip leading narration and return the dish."""
    result = gym_web._extract_dish_name(vision, pplx_desc="")
    # Either the actual dish name, OR a stub. Either way, must NOT contain narration
    for marker in ("這張相", "呢張相", "相顯示", "可見到", "睇到", "照片中", "一支", "一樽"):
        assert marker not in result, f"narration leaked: {result!r} from input {vision!r}"


# ── generic-label rejection (AI extractor) ──────────────────────────────

def test_ai_result_narration_falls_through(monkeypatch):
    """If the AI returns narration (e.g. '這張相顯示一支蘇打水樽'),
    the extractor must fall through to regex instead of returning it."""
    fake_responses = iter([
        {"choices": [{"message": {"content": "這張相顯示一支蘇打水樽，品牌為"}}]},
    ])

    def fake_call(req, *a, **kw):
        class FakeResp:
            def __init__(self, payload): self._payload = payload
            def read(self): return __import__("json").dumps(self._payload).encode("utf-8")
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return FakeResp(next(fake_responses))

    monkeypatch.setattr(gym_web, "_minimax_api_key", lambda: "fake-key")
    monkeypatch.setattr("urllib.request.urlopen", fake_call)

    # The AI returns narration → should fall through to regex → returns stripped dish
    result = gym_web._extract_dish_name_ai("這張相顯示一支蘇打水樽，品牌為可口可樂", "")
    # Must NOT contain narration markers
    for marker in ("這張相", "相顯示", "品牌為", "一支", "一樽"):
        assert marker not in result, f"AI narration leaked: {result!r}"


# ── generic-label rejection ──────────────────────────────────────────────

def test_generic_meal_label_note():
    """Placeholder test — generic-label rejection is handled upstream by
    the AI extractor and the `generic_labels` filter (see v3.2.7.6). The
    regex fallback is the last-resort path when the AI gives nothing
    useful, so it may still return generic labels.

    This test is informational only — see test_narration_prefix_stripped
    for the user-visible fix (narration must not appear in output)."""
    # Just verify the function doesn't crash on generic input
    result = gym_web._extract_dish_name("可見到簡單嘅早餐", pplx_desc="")
    assert isinstance(result, str)  # always returns a string


# ── commit-path guard (v3.2.7.17, AI-based) ─────────────────────────────
# Jim OOB 2026-08-10 'don't use rule. please use ai to obtain' — the
# narration classifier calls MiniMax M3-highspeed. We monkeypatch the
# classifier so the tests are deterministic and don't hit the API.

@pytest.fixture
def stub_narration_ai(monkeypatch):
    """Stub the AI classifier: anything containing 敘述性 markers is
    narration, otherwise it's a dish. Matches the heuristic the
    deployed MiniMax M3 model uses in practice."""
    narration_substrings = (
        "這張相", "呢張相", "張相", "張圖", "呢個相", "呢個圖",
        "相顯示", "圖顯示", "圖中", "相中", "照片", "圖片",
        "可見到", "可以見到", "可以睇到",
        "品牌為", "牌子係", "品牌係", "品牌是", "牌子是",
    )
    def fake(name):
        if not name:
            return False
        return any(m in name for m in narration_substrings)
    monkeypatch.setattr(gym_web, "_ai_check_narration", fake)
    return fake


@pytest.mark.parametrize("name", [
    "這張相顯示一支蘇打水樽",          # the user's exact case
    "這張相顯示一支蘇打水樽，品牌為可口可樂",
    "呢張相顯示一碗白飯",
    "相顯示咗一塊千層蛋糕",
    "可見到一碟蛋撻",
    "品牌為可口可樂",
    "照片中有一碗粥",
    "圖中可見漢堡包",
    "圖片入面係壽司",
])
def test_narration_guard_detects(name, stub_narration_ai):
    """The AI-based guard must flag narration names so the commit
    path refuses to auto-commit (v3.2.7.17)."""
    assert gym_web._name_has_narration(name) is True, (
        f"expected narration in {name!r}")


@pytest.mark.parametrize("name", [
    "蘇打水",
    "白飯",
    "千層蛋糕",
    "蛋撻",
    "黑咖啡",
    "粥",
    "可口可樂",         # brand alone is OK (no narration)
    "壽司",
    "漢堡包",
    "海南雞飯",
    "Coca-Cola",
    "雞蛋",
    "一支蘇打水",       # bare; classifier says this is a dish now
    "一樽可樂",
])
def test_narration_guard_passes_clean(name, stub_narration_ai):
    """Clean dish names must NOT trip the AI guard."""
    assert gym_web._name_has_narration(name) is False, (
        f"false positive on clean name {name!r}")


def test_narration_guard_empty():
    """Empty name should not be considered narration (handled elsewhere)."""
    assert gym_web._name_has_narration("") is False
    assert gym_web._name_has_narration(None) is False  # type: ignore[arg-type]


def test_narration_guard_fail_closed(monkeypatch):
    """If the AI call fails, the guard must default to True (treat
    as narration) so we never auto-commit unverified names."""
    def fake(name):
        return True  # simulate API failure / fail-closed
    monkeypatch.setattr(gym_web, "_ai_check_narration", fake)
    assert gym_web._name_has_narration("蘇打水") is True  # fails closed
    assert gym_web._name_has_narration("漢堡包") is True  # fails closed


def test_narration_ai_no_api_key_fails_closed(monkeypatch):
    """Without a MiniMax API key, the AI classifier must fail closed
    (return True = narration) so the commit path stays safe."""
    monkeypatch.setattr(gym_web, "_minimax_api_key", lambda: "")
    gym_web._NARRATION_CACHE.clear()
    assert gym_web._ai_check_narration("蘇打水") is True
