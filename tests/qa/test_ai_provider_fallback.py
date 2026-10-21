"""
v3.3.29 — AI 依賴單點故障嘅 regression 測試 (Isak 2026-10-21)

背景: Alonso 提交 v3.3.29 RULE 28 報告,話三個 AI 依賴(視覺 / 抽菜名 /
narration 分類)都加咗 Perplexity 後備,QA 8/8 全綠。我獨立驗證之後發現
**三個真 bug**,全部係「fallback 存在但行唔到」或者「狀態被蓋寫」:

  BUG 1 (line 5070) — `_extract_dish_name_ai` 嘅「進思考」早退冇 fallback。
    MiniMax 生存但半死(think block 被 max_tokens 截斷)→ 直接 `return ""`
    → Perplexity 抽取器永遠行唔到 → 完美描述變「未識別菜式」。
    Alonso 報告第 4 點話「fallback 要喺每個 return "" 之前行」,但呢個
    return 漏咗。PROVEN:Perplexity call count = 0。

  BUG 2 — `_VISION_STATUS` 被 2nd opinion 覆蓋。
    2nd opinion 用同一條 provider chain,chain 結束時寫死 `_VISION_STATUS`
    → /api/health 報嘅係第 2 個供應商,唔係主力嗰個。
    PROVEN: opinion#1=minimax-text-01, opinion#2=perplexity-sonar,
    最終 status=perplexity-sonar。

  BUG 3 (BUG 2 嘅更嚴重後果) — rule-2 寫入保護會被繞過。
    2nd opinion 期間 provider 失敗 → provider 層呼叫
    `_record_vision_status(failed)` → 寫入主 dict → 之後第 2 個供應商
    成功再寫 `ok` → 主 dict 變 "ok" → rule-2 guard
    (`state == "failed" and not vision_desc`) 唔會觸發 →
    「未識別菜式」寫得入 golden copy。呢個正正就係 v3.3.25 存在嘅原因。

呢組測試係 **unit level** — 唔打 network、唔打 Google API、唔掂
Jim 個 Sheet。用 fake provider 模擬各種死法。

Regression target: 2026-10-20 嘅「未識別菜式」事故 + v3.3.29 報告嘅
三個漏網。
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def gw():
    """Load gym_web.py by path (the gym_web/ package shadows the module)."""
    spec = importlib.util.spec_from_file_location(
        "gym_web_v3329_test", REPO / "gym_web.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeResp:
    """Minimal stand-in for urllib's HTTPResponse."""

    def __init__(self, body):
        self._b = body

    def read(self):
        return json.dumps(self._b).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake_http(gw, monkeypatch, *, minimax_reply=None,
                       minimax_raises=None, pplx_reply=None,
                       pplx_raises=None):
    """Fake the two remaining providers. Returns a call-counter dict.

    `minimax_reply`  → the content MiniMax returns (dict body)
    `minimax_raises` → exception MiniMax raises (simulates a dead key)
    `pplx_reply`     → the content Perplexity returns
    `pplx_raises`    → exception Perplexity raises

    v3.3.30: APIyI was removed from the chain (dead key), so there is no
    third provider to fake any more.
    """
    calls = {"minimax": 0, "pplx": 0}

    def fake_urlopen(req, timeout=None):
        url = getattr(req, "full_url", "") or ""
        body = (req.data.decode() if getattr(req, "data", None) else "")
        if "minimax" in url:
            calls["minimax"] += 1
            if minimax_raises:
                raise OSError(minimax_raises)
            if minimax_reply is not None:
                return FakeResp({"choices": [
                    {"message": {"content": minimax_reply}}]})
            raise OSError("no minimax reply configured")
        if "perplexity" in url:
            calls["pplx"] += 1
            if pplx_raises:
                raise OSError(pplx_raises)
            if pplx_reply is not None:
                return FakeResp({"choices": [
                    {"message": {"content": pplx_reply}}]})
            raise OSError("no pplx reply configured")
        raise AssertionError(f"unexpected URL in fake: {url}")

    monkeypatch.setattr(gw.urllib.request, "urlopen", fake_urlopen)
    return calls


VISION_DESC = "牛扒配焗薯、芝士煙肉,配西蘭花同車厘茄。"


# ──────────────────────────────────────────────────────────────────────
# BUG 1 — 「進思考」早退冇 fallback
# ──────────────────────────────────────────────────────────────────────

def test_dish_name_think_truncation_still_uses_perplexity(gw, monkeypatch):
    """MiniMax 半死(think block 截斷)→ Perplexity 必須接手。

    Alonso 報告第 4 點:「fallback 要喺每個 return "" 之前行」。
    呢個測試就係守住嗰個 return —— 之前佢直接 return ""。
    """
    calls = _install_fake_http(
        gw, monkeypatch,
        # 未閉合嘅 think block:有「進思考」但冇 closed tag
        minimax_reply="進思考...呢張相顯示一碟食物",
        pplx_reply="牛扒、芝士焗薯",
    )
    gw._NARRATION_CACHE.clear()

    result = gw._extract_dish_name_ai(VISION_DESC)

    assert calls["pplx"] >= 1, (
        "BUG 1 未修:MiniMax 回咗未閉合 think block,Perplexity 抽取器"
        "完全冇被叫過 → 上游會寫「未識別菜式」")
    assert result == "牛扒、芝士焗薯", (
        f"BUG 1 未修:fallback 應該保住菜名,實際返回 {result!r}")


def test_dish_name_all_providers_dead_returns_empty(gw, monkeypatch):
    """全部供應商死 → 返回空(唔可以亂估一個菜名)。

    反向 guard:fallback 唔係「乜都試」,係「試唔到就認」。
    """
    _install_fake_http(
        gw, monkeypatch,
        minimax_raises="HTTP Error 401: Unauthorized",
        pplx_raises="HTTP Error 401: Unauthorized",
    )
    gw._NARRATION_CACHE.clear()

    result = gw._extract_dish_name_ai(VISION_DESC)
    assert result == "", f"全部供應商死應該返回空,實際 {result!r}"


def test_every_empty_return_in_extractor_has_a_fallback(gw):
    """結構性 guard:每個 `return ""` 前面都要有 fallback。

    呢個係「用 source code 結構」代替「逐個情境手動測」——
    任何未來新加嘅早退都會被呢個測試捉到,唔使靠人記得。
    """
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.startswith("def _extract_dish_name_ai("))
    end = next(i for i in range(start + 1, len(lines))
               if lines[i].startswith("def ")
               and not lines[i].startswith("def _extract_dish_name_ai("))
    body = lines[start:end]

    # 呢啲 early return 係「prompt 都未砌好就冇嘢可抽」——
    # 唔需要 fallback(冇 prompt 就冇嘢可以叫供應商)。
    # 註釋掉嘅 docstring 行唔算。
    pre_prompt_exempt = []
    guarded = []
    for i, line in enumerate(body):
        if line.strip() != 'return ""':
            continue
        # 行號 <= prompt 砌好之前 → exempt
        ctx = "\n".join(body[max(0, i - 30):i + 1])
        if "prompt = (" not in ctx:
            pre_prompt_exempt.append(i)
            continue
        window = "\n".join(body[max(0, i - 15):i + 1])
        if "_pplx_extract_dish_name" not in window:
            guarded.append((start + i + 1, body[max(0, i - 2)].strip()))

    assert not guarded, (
        "BUG 1 未修 / 復發:_extract_dish_name_ai 有早退冇 fallback:\n"
        + "\n".join(f"  line {ln}: {ctx}" for ln, ctx in guarded)
        + "\n  → 供應商半死時,呢個 return 會令後備永遠行唔到。")


# ──────────────────────────────────────────────────────────────────────
# BUG 2 — 2nd opinion 覆蓋 _VISION_STATUS
# ──────────────────────────────────────────────────────────────────────

def test_second_opinion_does_not_overwrite_primary_status(gw, monkeypatch):
    """2nd opinion 唔可以覆蓋主力意見嘅供應商記錄。

    PROVEN 2026-10-21:before the fix, opinion#1=minimax-text-01,
    opinion#2=perplexity-sonar, 最終 _VISION_STATUS=perplexity-sonar
    → /api/health 報錯供應商。
    """
    # v3.3.30: APiyI 已移除 → chain 輪到 MiniMax 服務 opinion#1。
    # 擋住真實 network,確保測試唔會打出去。
    def _no_network(req, timeout=None):
        raise AssertionError(f"唔准打真實 network:{getattr(req, 'full_url', '')}")

    monkeypatch.setattr(gw.urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(gw, "_minimax_vision_text01",
                        lambda b64, prompt: "MINIMAX 描述")
    monkeypatch.setattr(gw, "_perplexity_vision",
                        lambda b64, prompt: "PERPLEXITY 描述")

    gw._record_vision_status("none", "unknown")
    gw._VISION_2ND_OPINION = False

    # opinion #1
    d1 = gw._minimax_vision("B64", "prompt")
    assert d1
    primary = dict(gw._VISION_STATUS)
    assert primary.get("provider_key") == "minimax-text-01", (
        f"opinion#1 應該由 minimax-text-01 服務,實際 {primary}")

    # opinion #2 — 落到 chain,排除 opinion#1 嘅供應商
    d2 = gw._vision_second_opinion("B64", "prompt")
    assert d2

    assert gw._VISION_STATUS.get("provider_key") == "minimax-text-01", (
        "BUG 2 未修:2nd opinion 覆蓋咗主 dict,"
        f"最終 provider_key={gw._VISION_STATUS.get('provider_key')!r}"
        " → /api/health 會報錯供應商")
    assert gw._VISION_STATUS_2ND.get("provider_key") == "perplexity-sonar", (
        "2nd opinion 應該記錄喺 _VISION_STATUS_2ND,"
        f"實際 {gw._VISION_STATUS_2ND.get('provider_key')!r}")


def test_health_exposes_both_opinions(gw):
    """/api/health 要同時見到兩個意見嘅供應商。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    for key in ('checks["vision_provider_key"]',
                'checks["vision_provider_key_2nd"]',
                'checks["vision_state_2nd"]'):
        assert key in src, f"/api/health 冇 expose {key}"


# ──────────────────────────────────────────────────────────────────────
# BUG 3 — rule-2 寫入保護被 2nd opinion 繞過
# ──────────────────────────────────────────────────────────────────────

def test_primary_failure_not_masked_by_second_opinion(gw, monkeypatch):
    """主力意見死 + 第 2 個意見成功 → 主狀態必須仍然係 failed。

    呢個係 BUG 3:before the fix, 第 2 個意見成功會把主 dict 由
    "failed" 改成 "ok" → rule-2 guard (`state == "failed" and not
    vision_desc`) 唔觸發 → 「未識別菜式」寫得入 golden copy。
    """
    # 主力鏈:全部供應商死
    monkeypatch.setattr(gw, "_minimax_vision_text01",
                        lambda b64, prompt: "")
    monkeypatch.setattr(gw, "_perplexity_vision",
                        lambda b64, prompt: "")
    # 但第 2 個意見:chain 有一個成功
    gw._record_vision_status("none", "unknown")
    gw._VISION_2ND_OPINION = False

    # opinion #1 → 全部死
    d1 = gw._minimax_vision("B64", "prompt")
    assert d1 == "", "opinion#1 應該失敗"
    assert gw._VISION_STATUS.get("state") == "failed", (
        "opinion#1 全死 → 主狀態必須係 failed,"
        f"實際 {gw._VISION_STATUS.get('state')!r}")

    # opinion #2 → 有一個供應商成功
    monkeypatch.setattr(gw, "_perplexity_vision",
                        lambda b64, prompt: "PERPLEXITY 描述")
    d2 = gw._vision_second_opinion("B64", "prompt")
    assert d2, "opinion#2 應該成功"

    assert gw._VISION_STATUS.get("state") == "failed", (
        "BUG 3 未修:2nd opinion 成功把主狀態改成 ok,"
        f"實際 {gw._VISION_STATUS.get('state')!r} → rule-2 guard 會被繞過,"
        "「未識別菜式」會寫入生產表")
    assert gw._VISION_STATUS_2ND.get("state") == "ok"


def test_provider_failure_during_second_opinion_does_not_poison_primary(
        gw, monkeypatch):
    """2nd opinion 期間嘅 provider 失敗唔可以污染主 dict。

    provider 層 (_minimax_vision_text01 / _perplexity_vision) 失敗時
    會直接呼叫 `_record_vision_status(failed)`。修正前呢個寫入主 dict
    → 主力意見明明成功,狀態卻變 "failed" → health 報假警,
    rule-2 guard 又可能誤擋正常 scan。

    ⚠️ 呢個測試刻意**唔** monkeypatch provider 函數本身 —— 咁樣就會
    繞過 provider 函數內部嘅 except 分支,即係測唔到要測嘅嘢。
    要 patch 嘅係底層 urlopen,令真實 provider 函數行到佢嘅 except。
    """
    # 真實 provider 函數行,只擋住 network。
    # perplexity → 429;minimax-text-01 → 成功。
    def fake_urlopen(req, timeout=None):
        url = getattr(req, "full_url", "") or ""
        if "perplexity" in url:
            raise OSError("HTTP Error 429: Too Many Requests")
        if "minimax" in url:
            return FakeResp({"choices": [{"message": {
                "content": "MINIMAX 描述"}}]})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(gw.urllib.request, "urlopen", fake_urlopen)

    gw._record_vision_status("none", "unknown")
    gw._VISION_2ND_OPINION = False

    # opinion #1:perplexity 真實失敗(429)→ provider 層 except 觸發 →
    # MiniMax 服務 → opinion#1 = minimax-text-01
    d1 = gw._minimax_vision("B64", "prompt")
    assert d1, "opinion#1 應該由 MiniMax 服務"
    assert gw._VISION_STATUS.get("provider_key") == "minimax-text-01"

    # opinion #2:chain;perplexity 429 → provider 層 except 觸發
    d2 = gw._vision_second_opinion("B64", "prompt")
    assert d2

    assert gw._VISION_STATUS.get("provider_key") == "minimax-text-01", (
        "BUG 3 未修:2nd opinion 期間嘅 provider 失敗污染咗主 dict,"
        f"provider_key={gw._VISION_STATUS.get('provider_key')!r} "
        "(應該仍然係 opinion#1 嘅 minimax-text-01)")
    assert gw._VISION_STATUS.get("state") == "ok", (
        "BUG 3 未修:2nd opinion 期間 provider 失敗把主狀態改成 failed,"
        f"實際 {gw._VISION_STATUS.get('state')!r}")
    assert gw._VISION_STATUS_2ND.get("state") == "ok", (
        "2nd opinion 自己嘅狀態應該記錄喺 _VISION_STATUS_2ND,"
        f"實際 {gw._VISION_STATUS_2ND.get('state')!r}")


# ──────────────────────────────────────────────────────────────────────
# 佐證 — Alonso 報告嗰啲真係 work 嘅部分
# ──────────────────────────────────────────────────────────────────────

def test_narration_classifier_falls_back_to_perplexity(gw, monkeypatch):
    """MiniMax 死 → narration 分類交畀 Perplexity。

    Alonso 報告第 2 點:「完美菜名『芝士牛扒焗薯』被判 narration 而被拒」。
    呢個測試確認修好咗。
    """
    calls = _install_fake_http(
        gw, monkeypatch,
        minimax_raises="HTTP Error 401: Unauthorized",
        pplx_reply="DISH",
    )
    gw._NARRATION_CACHE.clear()

    is_narration = gw._ai_check_narration("芝士牛扒焗薯")

    assert calls["pplx"] >= 1, "Perplexity 後備冇被叫"
    assert is_narration is False, (
        f"好菜名應該判定為 dish,實際 is_narration={is_narration}")


def test_narration_classifier_fails_closed_when_everything_dead(gw, monkeypatch):
    """全部 AI 死 → fail closed(當作 narration,拒絕自動 commit)。"""
    _install_fake_http(
        gw, monkeypatch,
        minimax_raises="HTTP Error 401: Unauthorized",
        pplx_raises="HTTP Error 401: Unauthorized",
    )
    gw._NARRATION_CACHE.clear()

    assert gw._ai_check_narration("芝士牛扒焗薯") is True, (
        "所有 AI 都死 → 必須 fail closed(當 narration),"
        "寧願唔好自動 commit,都唔好存垃圾")


def test_second_opinion_soft_preference_still_yields_second_opinion(
        gw, monkeypatch):
    """只剩 1 個供應商時,第 2 個意見仍然要出到。

    Alonso 報告第 3 點:hard exclusion → 剩 1 個供應商時 2nd opinion
    冇嘢用。soft preference 修好咗,呢個測試守住佢。
    """
    monkeypatch.setattr(gw, "_minimax_vision_text01",
                        lambda b64, prompt: "MINIMAX 描述")
    monkeypatch.setattr(gw, "_perplexity_vision",
                        lambda b64, prompt: "")

    gw._record_vision_status("none", "unknown")
    gw._VISION_2ND_OPINION = False

    gw._minimax_vision("B64", "prompt")
    d2 = gw._vision_second_opinion("B64", "prompt")

    assert d2, (
        "只剩 1 個供應商時 2nd opinion 應該仍然出到(用返同一供應商)——"
        "hard exclusion 會令呢度變空")
