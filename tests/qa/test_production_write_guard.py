"""Production write guard — 防止 QA 測試污染 Jim 個 Nutrition golden copy.

Alonso 2026-10-20 HARD STOP: "Someone has pollute the production data.
Pls avoid doing so"

呢組測試係 **unit level** — 完全唔打 network、唔打 Google API、
唔掂 Jim 個 Sheet。純粹驗 guard 嘅邏輯。

受保護嘅嘢:
- `_write_mode()`        — production / dryrun / throwaway 解析
- `_target_sheet_id()`   — GYMBRO_SHEET_ID override
- `_is_production_target()` — 判定寫入目標係咪 live golden copy
- `_is_test_fixture_image()` — 用 content hash + dir marker 偵測 fixture
- `_assert_not_test_fixture()` — refuse commit
- `_append_to_sheet_nutrition()` dryrun 分支 — 唔會發任何 network request

Regression target: 2026-10-20 26 個 polluted rows (grid 656-681)。
"""

import importlib.util
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO / "test_fixtures"
PRODUCTION_SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"


@pytest.fixture(scope="module")
def gw():
    """Load gym_web.py by path (shadowed by the gym_web/ package)."""
    spec = importlib.util.spec_from_file_location(
        "gym_web_guard_test", REPO / "gym_web.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from a known env state."""
    monkeypatch.delenv("GYMBRO_WRITE_MODE", raising=False)
    monkeypatch.delenv("GYMBRO_SHEET_ID", raising=False)


# ──────────────────────────────────────────────────────────────────────────
# 1. Write mode resolution
# ──────────────────────────────────────────────────────────────────────────

def test_default_mode_is_production(gw):
    """No env var → production (real Jim usage still works)."""
    assert gw._write_mode() == "production"


def test_dryrun_mode(gw, monkeypatch):
    monkeypatch.setenv("GYMBRO_WRITE_MODE", "dryrun")
    assert gw._write_mode() == "dryrun"


def test_throwaway_mode(gw, monkeypatch):
    monkeypatch.setenv("GYMBRO_WRITE_MODE", "throwaway")
    assert gw._write_mode() == "throwaway"


def test_mode_is_case_insensitive(gw, monkeypatch):
    monkeypatch.setenv("GYMBRO_WRITE_MODE", "DryRun")
    assert gw._write_mode() == "dryrun"


def test_unknown_mode_falls_back_to_production(gw, monkeypatch):
    """Unknown value must NOT silently become a safe mode."""
    monkeypatch.setenv("GYMBRO_WRITE_MODE", "banana")
    assert gw._write_mode() == "production"


# ──────────────────────────────────────────────────────────────────────────
# 2. Target sheet resolution (throwaway redirect)
# ──────────────────────────────────────────────────────────────────────────

def test_target_defaults_to_production(gw):
    assert gw._target_sheet_id() == PRODUCTION_SHEET_ID
    assert gw._is_production_target() is True


def test_sheet_id_override_redirects(gw, monkeypatch):
    monkeypatch.setenv("GYMBRO_SHEET_ID", "1ThrowawayCopySheetId")
    assert gw._target_sheet_id() == "1ThrowawayCopySheetId"
    assert gw._is_production_target() is False


def test_blank_sheet_id_falls_back_to_production(gw, monkeypatch):
    monkeypatch.setenv("GYMBRO_SHEET_ID", "   ")
    assert gw._target_sheet_id() == PRODUCTION_SHEET_ID


# ──────────────────────────────────────────────────────────────────────────
# 3. Fixture detection — the backstop
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["food_1.jpg", "food_2.jpg"])
def test_synthetic_food_fixtures_are_detected(gw, name):
    """The synthetic food_1/food_2 fixtures must be recognised."""
    assert gw._is_test_fixture_image(FIXTURES / name) is True


def test_static_test_fixture_is_detected(gw):
    assert gw._is_test_fixture_image(FIXTURES / "static_test.jpg") is True


def test_fixture_detected_by_content_hash_outside_fixture_dir(gw, tmp_path):
    """Hash detection must work even when the file was copied elsewhere.

    This is the case that matters: a test that copies the fixture into a
    temp dir (or into scan_cache) is still committing a fixture.
    """
    src = FIXTURES / "food_1.jpg"
    copy = tmp_path / "looks_real.jpg"
    copy.write_bytes(src.read_bytes())
    assert gw._is_test_fixture_image(copy) is True


def test_real_photo_is_not_flagged(gw):
    """A genuine photo must NOT be blocked — only fixtures are."""
    real = Path("/home/work/.hermes/scan_cache/scan_20261020_221827.jpg")
    if not real.exists():
        pytest.skip("reference photo not present")
    assert gw._is_test_fixture_image(real) is False


def test_assert_raises_on_fixture(gw):
    with pytest.raises(ValueError, match="REFUSED"):
        gw._assert_not_test_fixture(FIXTURES / "food_1.jpg")


def test_assert_passes_on_non_fixture(gw, tmp_path):
    junk = tmp_path / "random.bin"
    junk.write_bytes(b"not a fixture")
    gw._assert_not_test_fixture(junk)  # must not raise


# ──────────────────────────────────────────────────────────────────────────
# 4. dryrun never touches the network — THE critical regression test
# ──────────────────────────────────────────────────────────────────────────

def test_dryrun_append_writes_nothing(gw, monkeypatch):
    """dryrun 模式下 _append_to_sheet_nutrition 唔會發任何 network call。

    呢個係最關鍵嘅 regression test — 2026-10-20 嘅污染就係呢個
    function 發出嘅 POST。
    """
    monkeypatch.setenv("GYMBRO_WRITE_MODE", "dryrun")

    def _boom(*a, **kw):
        raise AssertionError(
            "dryrun 模式下唔准發任何 network request！"
            "（呢個正正就係 2026-10-20 污染 production 嘅根因）")

    # Poison every network primitive the writer could use.
    monkeypatch.setattr(gw.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(gw.urllib.request, "Request", _boom)

    entry = {
        "date": "2026-10-20",
        "time": "22:19",
        "meal_type": "dinner",
        "name": "測試菜式",
        "calories": 500,
        "protein": 30,
        "carbs": 40,
        "fat": 10,
    }
    res = gw._append_to_sheet_nutrition(entry)

    assert res["ok"] is True
    assert res["range"] == "dryrun"
    assert res["skipped"] is True
    # Rule 4 — the would-have-written row list must be reportable.
    whw = res.get("would_have_written")
    assert isinstance(whw, list) and len(whw) == 1
    assert whw[0]["name"] == "測試菜式"
    assert whw[0]["calories"] == 500
    assert whw[0]["would_have_written"] is True
    # Must NOT be marked as synced — nothing was actually written.
    assert entry.get("sheet_synced") is False


def test_dryrun_drive_upload_is_skipped(gw, monkeypatch, tmp_path):
    """dryrun 唔會上載 Drive（避免 orphan Drive files）。"""
    monkeypatch.setenv("GYMBRO_WRITE_MODE", "dryrun")

    def _boom(*a, **kw):
        raise AssertionError("dryrun 唔准上載 Drive")

    monkeypatch.setattr(gw.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(gw.urllib.request, "Request", _boom)

    jpg = tmp_path / "x.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0 fake jpeg bytes")

    assert gw._upload_to_drive(jpg) == ""


def test_dryrun_scan_preview_core_does_not_commit(gw, monkeypatch, tmp_path):
    """End-to-end (no network): a fixture in dryrun produces a dryrun row,
    never a sheet append."""
    monkeypatch.setenv("GYMBRO_WRITE_MODE", "dryrun")

    calls = []

    def _spy(*a, **kw):
        calls.append(a)
        raise AssertionError("dryrun 唔准發 network request")

    monkeypatch.setattr(gw.urllib.request, "urlopen", _spy)

    entry = {
        "date": "2026-10-20", "time": "22:19", "name": "測試菜式",
        "calories": 500, "protein": 30, "carbs": 40, "fat": 10,
        "timestamp_iso": "2026-10-20T22:19:00+08:00",
    }
    res = gw._append_to_sheet_nutrition(entry)
    assert res["range"] == "dryrun"
    assert calls == []


def test_production_mode_still_allows_write(gw, monkeypatch):
    """Guard 唔會阻擋真實 usage — production 模式照寫。

    我哋唔想將 Jim 自己用嘅 path 都鎖死。
    """
    monkeypatch.setenv("GYMBRO_WRITE_MODE", "production")
    assert gw._write_mode() == "production"
    # A non-fixture image must pass the backstop.
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(b"\xff\xd8\xff\xe0 definitely not a registered fixture")
        tmp = f.name
    gw._assert_not_test_fixture(Path(tmp))
    os.unlink(tmp)


# ──────────────────────────────────────────────────────────────────────────
# 5. Fixture hygiene — the fixtures themselves must stay synthetic
# ──────────────────────────────────────────────────────────────────────────

def test_fixture_readme_states_the_rule(gw):
    """README 必須寫明「唔准用真食物相做 fixture」。"""
    readme = (FIXTURES / "README.txt").read_text(encoding="utf-8")
    assert "SYNTHETIC" in readme
    assert "NOT REAL FOOD" in readme or "NOT REAL" in readme


def test_food_fixtures_are_small_synthetic_images(gw):
    """Synthetic fixtures 細過 100KB — 真食物相會係 MB 級。

    呢個係 heuristic 檢查,唔係絕對保證,但捉到明顯違規。
    """
    for name in ("food_1.jpg", "food_2.jpg"):
        size = (FIXTURES / name).stat().st_size
        assert size < 100 * 1024, (
            f"{name} 係 {size} bytes — 咁大唔似 synthetic fixture。"
            "真食物相唔准放落 test_fixtures/"
        )
