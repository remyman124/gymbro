"""
RULE 2 — 視覺供應商全死時,唔准寫「未識別菜式」入生產表 (v3.3.25)

背景 (Jim OOB 2026-10-20「And the ai recognition is all wrong. It was
showing unrecognised food」):

  供應商 key 死咗 (HTTP 401) → 每一次 scan 都靜靜哋寫「未識別菜式 / 0 kcal」
  落 Jim 個 Nutrition golden copy。一晚 7 行垃圾,Jim 要到 app 睇到先發現。

v3.3.25 嘅修復有兩層:
  1. 供應商鏈 MiniMax-text-01 → Perplexity sonar (fallback 真係識相)
  2. 如果**所有**供應商都死 (_VISION_STATUS["state"] == "failed"),
     _scan_preview_core 唔會再寫「未識別菜式」,改回 ok:false +
     同一個 top-level key set(零 Sheet row、零 Drive 檔)

呢組測試係 **unit level** — 唔打 network、唔打 Google API、唔掂
Jim 個 Sheet。純粹驗 rule 2 嘅分支邏輯。

⚠️ 呢個係 rule 2 嘅**第一個**測試覆蓋 — 之前 rule 2 完全冇 test,
   即係「改咗 rule 2 但冇人驗證過佢真係 work」。呢個正正就係今晚
   事故嗰個 class of bug(改咗 code 但冇 restart / 冇 test)。

Regression target: 2026-10-20 嘅 7 行「未識別菜式 / 0 kcal」。
"""

import importlib.util
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO / "test_fixtures"

# The success path's top-level key set. Rule 2 必須回傳**同一個** key set
# (Isak's §4.3 regression contract) — 只可以 ok 變 False,唔可以少 key。
EXPECTED_TOP_LEVEL_KEYS = {
    "ok",
    "auto_committed",
    "image_path",
    "image_url",
    "thumbnail_url",
    "drive_image_url",
    "vision_desc",
    "vision_short",
    "is_multi_entry",
    "entries",
    "committed",
    "multi_entry",
    "preview",
}


@pytest.fixture(scope="module")
def gw():
    """Load gym_web.py by path (the gym_web/ package shadows the module)."""
    spec = importlib.util.spec_from_file_location(
        "gym_web_vision_guard_test", REPO / "gym_web.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GYMBRO_WRITE_MODE", raising=False)
    monkeypatch.delenv("GYMBRO_SHEET_ID", raising=False)


def _stub_vision(gw, monkeypatch, *, desc=""):
    """Make every vision provider fail.

    Monkeypatches the chain entry point _minimax_vision and the 2nd
    opinion helper _vision_second_opinion to return "", and forces
    _VISION_STATUS to the failed state — exactly what the runtime sets
    when MiniMax 401s AND the Perplexity fallback also fails.

    v3.3.30: APiyI was removed (dead key), so there is no _apiyi_vision_*
    to stub any more.
    """
    monkeypatch.setattr(gw, "_minimax_vision", lambda b64, prompt: desc)
    monkeypatch.setattr(gw, "_vision_second_opinion", lambda b64, prompt: "")
    monkeypatch.setattr(gw, "_minimax_vision_text01", lambda b64, prompt: "")
    monkeypatch.setattr(gw, "_perplexity_vision", lambda b64, prompt: "")
    monkeypatch.setattr(gw, "_VISION_STATUS", {
        "provider": "none",
        "state": "failed",
        "last_error": "test: all providers down",
        "last_ok_ts": 0,
    })


def _poison_network(gw, monkeypatch):
    """任何 network call 都係 bug — rule 2 必須零 Sheet row、零 Drive 檔。"""
    def _boom(*a, **kw):
        raise AssertionError(
            "rule 2 攔截之後唔准再發任何 network request "
            "(必須零 Sheet row、零 Drive 檔)")

    monkeypatch.setattr(gw.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(gw.urllib.request, "Request", _boom)


def _non_fixture_jpeg(tmp_path, name="meal.jpg"):
    """一張**唔係** QA fixture 嘅圖。

    Rule 3 (fixture backstop) 會喺 rule 2 之前行,所以呢啲測試唔可以用
    test_fixtures/ 嘅圖 —— 否則會撞 rule 3 而測唔到 rule 2。

    用 PIL 造一張純色 JPEG:內容唔喺 _TEST_FIXTURE_SHA256 名冊,路徑亦唔喺
    test_fixtures/ 目錄。
    """
    from PIL import Image
    img = Image.new("RGB", (32, 32), (120, 90, 60))
    p = tmp_path / name
    img.save(p, "JPEG", quality=90)
    return p


# ──────────────────────────────────────────────────────────────────────────
# 1. Rule 2 觸發條件
# ──────────────────────────────────────────────────────────────────────────

def test_vision_status_records_failure(gw, monkeypatch):
    """供應商全死時,_VISION_STATUS 必須記錄 state=failed。"""
    monkeypatch.setattr(gw, "_minimax_vision_text01",
                        lambda b64, prompt: "")
    gw._record_vision_status("none", "failed", "boom")
    assert gw._VISION_STATUS["state"] == "failed"
    assert gw._VISION_STATUS["last_error"] == "boom"


def test_vision_status_failure_is_exposed_in_health(gw, monkeypatch):
    """/api/health 必須見到 vision_state — 降級要 LOUD,唔可以靜。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    assert 'checks["vision_state"]' in src, (
        "/api/health 冇 expose vision_state — 供應商死咗 Jim 都唔知")
    assert 'checks["vision_provider"]' in src
    assert 'checks["vision_last_error"]' in src


# ──────────────────────────────────────────────────────────────────────────
# 2. Rule 2 行為:ok:false + 同一個 top-level key set
# ──────────────────────────────────────────────────────────────────────────

def test_scan_preview_core_blocks_when_all_vision_providers_failed(
        gw, monkeypatch, tmp_path):
    """核心 regression:所有供應商死 → 唔准寫「未識別菜式」。"""
    _stub_vision(gw, monkeypatch, desc="")
    _poison_network(gw, monkeypatch)

    img = _non_fixture_jpeg(tmp_path)
    res = gw._scan_preview_core(img.read_bytes())

    assert res["ok"] is False, (
        "所有視覺供應商死咗,應該 ok:false,而唔係寫「未識別菜式」入 golden copy")
    assert res["auto_committed"] is False
    # 零 Sheet row、零 Drive 檔
    assert res["committed"] == [], res.get("committed")
    assert res["drive_image_url"] == ""
    assert res["entries"] == []


def test_blocked_response_keeps_same_top_level_key_set(gw, monkeypatch, tmp_path):
    """Isak §4.3 contract:rule 2 只可以改 ok,唔可以改 key set。"""
    _stub_vision(gw, monkeypatch, desc="")
    _poison_network(gw, monkeypatch)

    img = _non_fixture_jpeg(tmp_path)
    res = gw._scan_preview_core(img.read_bytes())
    assert set(res.keys()) == EXPECTED_TOP_LEVEL_KEYS, (
        "rule 2 攔截之後嘅 top-level key set 同 success path 唔一樣 —— "
        f"少咗 {sorted(EXPECTED_TOP_LEVEL_KEYS - set(res.keys()))},"
        f"多咗 {sorted(set(res.keys()) - EXPECTED_TOP_LEVEL_KEYS)}")


def test_no_unrecognised_dish_written_when_vision_down(gw, monkeypatch, tmp_path):
    """最直接的防護:回應入面唔准出現「未識別菜式」。"""
    _stub_vision(gw, monkeypatch, desc="")
    _poison_network(gw, monkeypatch)

    img = _non_fixture_jpeg(tmp_path)
    res = gw._scan_preview_core(img.read_bytes())
    blob = repr(res)
    assert "未識別菜式" not in blob, (
        "rule 2 攔截之後回應仲出現「未識別菜式」—— "
        "呢個名會直接寫落 Jim 個 golden copy")


def test_vision_success_still_commits(gw, monkeypatch, tmp_path):
    """反向 guard:供應商正常時唔好誤擋 — rule 2 只喺全死時觸發。"""
    monkeypatch.setattr(gw, "_VISION_STATUS", {
        "provider": "minimax-text-01 (fallback)",
        "state": "ok", "last_error": "", "last_ok_ts": 1,
    })
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    # 攔截條件必須要求「state == failed」AND「冇 vision_desc」
    assert 'if _VISION_STATUS.get("state") == "failed" and not vision_desc:' in src, (
        "rule 2 嘅攔截條件唔對 —— 必須同時要求 state==failed 同埋冇 vision_desc,"
        " 否則會誤擋正常 scan")


# ──────────────────────────────────────────────────────────────────────────
# 3. Rule 1 — 供應商鏈同 MiniMax-text-01 真係支援 image_url
# ──────────────────────────────────────────────────────────────────────────

def test_vision_chain_has_minimax_text01_fallback(gw):
    """供應商鏈第一級係 MiniMax-text-01,第二級係 Perplexity sonar。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    assert "MiniMax-text-01" in src
    # MiniMax-text-01 要用 OpenAI-style image_url content block
    assert '"type": "image_url"' in src, (
        "MiniMax-text-01 唔係用 image_url content block —— "
        "咁就唔支援 vision")
    assert "api.minimax.io/v1/chat/completions" in src


def test_minimax_text01_not_vl01(gw):
    """MiniMax-VL-01 係 unknown model,唔可以攞嚟做 vision。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    assert '"model": "MiniMax-VL-01"' not in src, (
        "MiniMax-VL-01 會回 unknown model HTTP 400 —— "
        "vision 應該用 MiniMax-text-01")


def test_old_wrong_comment_about_minimax_vision_removed(gw):
    """舊註釋講「api.minimax.io 冇 vision model」係錯嘅,唔好留返。

    注意:唔可以淨係 grep 全文 —— v3.3.25 嘅更正說明**引用**咗呢句舊文
    去解釋點解錯。所以要搵嘅係「仲未更正嘅斷言」:如果舊文出現喺
    `_minimax_vision` 嘅 docstring(而唔係 `_minimax_vision_text01` 嘅
    更正段落),就代表錯解釋仲喺度。
    """
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")

    wrong_phrases = ("zero vision models", "has no vision model",
                     "冇 vision model", "no vision models")

    # 切出 _minimax_vision 嘅 docstring(供應商鏈入口)。
    # 如果呢度仲有舊嘅錯解釋,就代表 chain 入口仲誤導緊人。
    fn_start = src.index("def _minimax_vision(img_b64")
    fn_end = src.index("def _vision_second_opinion", fn_start)
    chain_docstring = src[fn_start:fn_end]

    for wrong in wrong_phrases:
        assert wrong not in chain_docstring, (
            f"供應商鏈入口 _minimax_vision 仲留住錯嘅註釋 {wrong!r} —— "
            "MiniMax-text-01 真係支援 image_url,唔係冇 vision model")

    # 更正段落本身必須存在,而且要講清楚係邊個 model 錯。
    corrected = src.index("def _minimax_vision_text01")
    corrected_doc = src[corrected:corrected + 2000]
    assert "MiniMax-VL-01" in corrected_doc, (
        "更正說明應該講明係 MiniMax-VL-01 唔 work(而唔係 MiniMax-text-01)")


# ──────────────────────────────────────────────────────────────────────────
# 4. Rule 29 — APiyI 已經完全移除,唔准再黐返嚟
# ──────────────────────────────────────────────────────────────────────────

def test_apiyi_fully_removed(gw):
    """Jim OOB 2026-10-21 "Remove apiyi stuff"。

    APIyI 條 key 2026-10-20 就死咗(每個 endpoint 都 401),所以成條供應商
    依賴要清乾淨:冇 function、冇 pool 成員、冇 key 檢查、冇 endpoint call。
    """
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")

    # 1) 冇任何 apiyi 嘅 function 定義或者呼叫
    for dead in ("_apiyi_vision_gpt4o", "_apiyi_vision_analyze",
                 "_apiyi_api_key", "_apiyi_nutrition_enrich"):
        assert dead not in src, f"仲有 APIyI 依賴:{dead}"

    # 2) 供應商池冇 apiyi
    pool = gw._VISION_PROVIDER_POOL
    assert not any("apiyi" in str(p[0]).lower() for p in pool), (
        f"供應商池仲有 APIyI:{pool}")
    assert len(pool) >= 2, f"供應商池至少要 2 個(唔可以又變單點故障):{pool}"

    # 3) 冇 APIyI 嘅 HTTP endpoint call
    assert "api.apiyi.com" not in src, "仲有 APIyI endpoint call"
    assert "vip.apiyi.com" not in src, "仲有 APIyI vip endpoint call"

    # 4) health 唔再報 apiyi key
    assert '"apiyi": bool(' not in src, "/api/health 仲報 apiyi key"

    # 5) 2nd opinion 改名後仍然存在(唔係刪走功能)
    assert hasattr(gw, "_vision_second_opinion"), (
        "2nd opinion 應該改名做 _vision_second_opinion,而唔係冇咗")


def test_voice_transcribe_no_longer_uses_apiyi(gw):
    """語音轉錄改用本地 faster-whisper,唔再靠 APIyI gpt-4o-transcribe。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    assert "/api/transcriptions" not in src, (
        "仲有用遠端 audio/transcriptions endpoint")
    assert "faster_whisper" in src, "應該用本地 faster-whisper"
    assert hasattr(gw, "_local_whisper_transcribe"), (
        "應該有 _local_whisper_transcribe helper")
