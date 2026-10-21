"""
v3.3.34 — 供應商鏈 regression 測試(MiniMax-only 版)

2026-10-21 Jim OOB:「Remove perplexity. keep minimax only」

原本呢個檔係 v3.3.29 Isak 嘅「AI 依賴單點故障」測試,建基於
MiniMax + Perplexity 兩家供應商。而家 Perplexity 拎走,呢啲測試要改成
**單一供應商**嘅現實:

  • 供應商池 = [minimax-text-01] 淨一個
  • 冇後備 = 失敗一定要「響」,唔可以靜靜哋寫壞資料
    (Rule 28:AI 依賴單點故障清零)
  • MiniMax 死 → 菜名攞唔到 → 掃描被 Rule 25 硬攔截(唔會寫「未識別菜式」)
  • 2nd opinion 會行返同一個 model(唔同 prompt)—— 唔係「兩個獨立意見」,
    但總好過冇第二輪

另外加咗一個新 guard:**gym_web.py 內唔准再有任何 Perplexity 呼叫**。
呢個正正就係 Jim 今次要求嘅可執行版本。
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
        "gym_web_v3334_test", REPO / "gym_web.py")
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
                       minimax_raises=None, fail_after=None):
    """Fake MiniMax (唯一供應商). Returns a call-counter dict.

    `minimax_reply`  → the content MiniMax returns (dict body)
    `minimax_raises` → exception MiniMax raises (simulates a dead key)
    `fail_after`     → 第 N 次呼叫之後開始失敗(用嚟模擬「第一個意見成功、
                        第二個意見失敗」)

    ⚠️ v3.3.34:Perplexity 已經拎走,所以 fake 遇到任何非 MiniMax 嘅 URL
    會直接 AssertionError —— 呢個就係「殘留 Perplexity 呼叫」嘅即時警報。
    """
    calls = {"minimax": 0}

    def fake_urlopen(req, timeout=None):
        url = getattr(req, "full_url", "") or ""
        body = (req.data.decode() if getattr(req, "data", None) else "")
        if "perplexity" in url:
            raise AssertionError(
                f"v3.3.34:唔准再打 Perplexity!但有程式叫咗:{url}")
        if "minimax" in url:
            calls["minimax"] += 1
            if fail_after is not None and calls["minimax"] > fail_after:
                raise OSError("HTTP Error 429: Too Many Requests")
            if minimax_raises:
                raise OSError(minimax_raises)
            if minimax_reply is not None:
                return FakeResp({"choices": [
                    {"message": {"content": minimax_reply}}]})
            raise OSError("no minimax reply configured")
        raise AssertionError(f"unexpected URL in fake: {url}")

    monkeypatch.setattr(gw.urllib.request, "urlopen", fake_urlopen)
    return calls


VISION_DESC = "牛扒配焗薯、芝士煙肉,配西蘭花同車厘茄。"


# ──────────────────────────────────────────────────────────────────────
# v3.3.34 新 guard — 唔准再有 Perplexity 呼叫
# ──────────────────────────────────────────────────────────────────────

def test_no_perplexity_http_call_left_in_source():
    """gym_web.py 唔准再有任何 Perplexity HTTP endpoint。

    Jim 2026-10-21:「Remove perplexity. keep minimax only」。
    淨係註解可以提,任何 `api.perplexity.ai` 都要冇。
    """
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    bad = []
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "api.perplexity.ai" in line or "perplexity.ai" in line:
            bad.append((i, stripped[:90]))
    assert not bad, (
        "v3.3.34:仲有 Perplexity 呼叫留喺 code 度(Jim 要求拎走):\n"
        + "\n".join(f"  line {i}: {l}" for i, l in bad))


def test_no_perplexity_key_read_left_in_source():
    """唔准再讀 PPLX_API_KEY(連 health 都唔應該報呢個 key)。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    code = [l for l in src.splitlines()
            if "PPLX_API_KEY" in l and not l.strip().startswith("#")]
    assert not code, (
        "v3.3.34:仲有 code 讀 PPLX_API_KEY:\n"
        + "\n".join("  " + l.strip()[:90] for l in code))


def test_provider_pool_is_minimax_only(gw):
    """供應商池淨返 MiniMax 一家。"""
    pool = gw._VISION_PROVIDER_POOL
    names = [p[0] for p in pool]
    assert names == ["minimax-text-01"], (
        f"供應商池應該淨返 MiniMax 一家,實際 {names}")
    assert not any("perplexity" in n.lower() for n in names)
    assert not any("pplx" in str(p[1]).lower() for p in pool), (
        f"供應商池唔應該再指向 Perplexity function:{pool}")


def test_perplexity_vision_function_is_gone(gw):
    """`_perplexity_vision` 應該完全唔存在(唔係「disable」,係「拎走」)。"""
    assert not hasattr(gw, "_perplexity_vision"), (
        "_perplexity_vision 仲喺度 —— 應該刪走")
    for gone in ("_pplx_enrich", "_pplx_check_narration",
                 "_pplx_extract_dish_name", "_pplx_api_key"):
        assert not hasattr(gw, gone), f"{gone} 仲喺度 —— 應該刪走"


# ──────────────────────────────────────────────────────────────────────
# 單一供應商:失敗一定要響,唔可以靜靜哋寫壞資料
# ──────────────────────────────────────────────────────────────────────

def test_dish_name_think_truncation_returns_empty(gw, monkeypatch):
    """MiniMax 半死(think block 截斷)→ 返回空。

    v3.3.34:冇後備供應商,所以呢度唔會再「另一家接手」——
    但**唔可以靜靜哋返回空**:抽取器必須有 log,而且上游會被
    Rule 25 硬攔截(唔會寫「未識別菜式」入表)。
    """
    calls = _install_fake_http(
        gw, monkeypatch,
        # 未閉合嘅 think block:有「進思考」但冇 closed tag
        minimax_reply="進思考...呢張相顯示一碟食物",
    )
    gw._NARRATION_CACHE.clear()

    result = gw._extract_dish_name_ai(VISION_DESC)

    assert result == "", (
        f"MiniMax 半死應該返回空(等上游硬攔截),實際 {result!r}")
    assert calls["minimax"] >= 1, "根本冇叫過 MiniMax"


def test_dish_name_all_providers_dead_returns_empty(gw, monkeypatch):
    """唯一供應商死 → 返回空(唔可以亂估一個菜名)。

    反向 guard:唔係「乜都試」,係「試唔到就認」。
    """
    _install_fake_http(
        gw, monkeypatch,
        minimax_raises="HTTP Error 401: Unauthorized",
    )
    gw._NARRATION_CACHE.clear()

    result = gw._extract_dish_name_ai(VISION_DESC)
    assert result == "", f"供應商死應該返回空,實際 {result!r}"


def test_every_early_return_in_extractor_is_logged(gw):
    """結構性 guard:prompt 砌好之後嘅每個 `return ""` 都必須有 log。

    v3.3.34 嘅新常規:**冇後備供應商 = 失敗一定要響**。
    呢個測試用 source code 結構代替逐個情境手動測 ——
    任何未來新加嘅早退都會被捉到,唔使靠人記得。
    """
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.startswith("def _extract_dish_name_ai("))
    end = next(i for i in range(start + 1, len(lines))
               if lines[i].startswith("def ")
               and not lines[i].startswith("def _extract_dish_name_ai("))
    body = lines[start:end]

    unguarded = []
    for i, line in enumerate(body):
        if line.strip() != 'return ""':
            continue
        # 行號 <= prompt 砌好之前 → exempt(冇 prompt 就冇嘢可以叫供應商)
        ctx = "\n".join(body[max(0, i - 30):i + 1])
        if "prompt = (" not in ctx:
            continue
        window = "\n".join(body[max(0, i - 12):i + 1])
        if "print(" not in window:
            unguarded.append((start + i + 1, body[max(0, i - 2)].strip()))

    assert not unguarded, (
        "v3.3.34:冇後備供應商,early return 一定要有 print(唔可以靜靜哋"
        "吞掉失敗):\n"
        + "\n".join(f"  line {ln}: {ctx}" for ln, ctx in unguarded))


# ──────────────────────────────────────────────────────────────────────
# 2nd opinion 唔可以覆蓋主力意見嘅供應商記錄
# ──────────────────────────────────────────────────────────────────────

def test_second_opinion_does_not_overwrite_primary_status(gw, monkeypatch):
    """2nd opinion 唔可以覆蓋主力意見嘅供應商記錄。

    v3.3.29 嘅 bug:before the fix, opinion#1=minimax-text-01,
    opinion#2=perplexity-sonar, 最終 _VISION_STATUS=perplexity-sonar
    → /api/health 報錯供應商。
    v3.3.34:兩個意見都係 MiniMax,但「唔可以覆蓋」嘅規則一樣要守。
    """
    def _no_network(req, timeout=None):
        raise AssertionError(f"唔准打真實 network:{getattr(req, 'full_url', '')}")

    monkeypatch.setattr(gw.urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(gw, "_minimax_vision_text01",
                        lambda b64, prompt: "MINIMAX 描述")

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
    assert d2, "2nd opinion 應該仍然出到(剩返同一供應商)"

    assert gw._VISION_STATUS.get("provider_key") == "minimax-text-01", (
        "2nd opinion 覆蓋咗主 dict,"
        f"最終 provider_key={gw._VISION_STATUS.get('provider_key')!r}"
        " → /api/health 會報錯供應商")
    assert gw._VISION_STATUS_2ND.get("provider_key") == "minimax-text-01", (
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
# 主力失敗唔可以被第 2 個意見掩蓋(rule-2 寫入保護)
# ──────────────────────────────────────────────────────────────────────

def test_primary_failure_not_masked_by_second_opinion(gw, monkeypatch):
    """主力意見死 + 第 2 個意見成功 → 主狀態必須仍然係 failed。

    v3.3.29 BUG 3:第 2 個意見成功會把主 dict 由 "failed" 改成 "ok"
    → rule-2 guard 唔觸發 → 「未識別菜式」寫得入 golden copy。
    v3.3.34:兩個意見都係 MiniMax,所以要用「第一次失敗、第二次成功」
    嘅呼叫序列去模擬。
    """
    state = {"n": 0}

    def fake_provider(b64, prompt):
        state["n"] += 1
        return "" if state["n"] == 1 else "MINIMAX 描述"

    monkeypatch.setattr(gw, "_minimax_vision_text01", fake_provider)
    gw._record_vision_status("none", "unknown")
    gw._VISION_2ND_OPINION = False

    # opinion #1 → 失敗
    d1 = gw._minimax_vision("B64", "prompt")
    assert d1 == "", "opinion#1 應該失敗"
    assert gw._VISION_STATUS.get("state") == "failed", (
        "opinion#1 死 → 主狀態必須係 failed,"
        f"實際 {gw._VISION_STATUS.get('state')!r}")

    # opinion #2 → 成功
    d2 = gw._vision_second_opinion("B64", "prompt")
    assert d2, "opinion#2 應該成功"

    assert gw._VISION_STATUS.get("state") == "failed", (
        "2nd opinion 成功把主狀態改成 ok,"
        f"實際 {gw._VISION_STATUS.get('state')!r} → rule-2 guard 會被繞過,"
        "「未識別菜式」會寫入生產表")
    assert gw._VISION_STATUS_2ND.get("state") == "ok"


def test_provider_failure_during_second_opinion_does_not_poison_primary(
        gw, monkeypatch):
    """2nd opinion 期間嘅 provider 失敗唔可以污染主 dict。

    provider 層 (_minimax_vision_text01) 失敗時會直接呼叫
    `_record_vision_status(failed)`。修正前呢個寫入主 dict → 主力意見
    明明成功,狀態卻變 "failed"。

    ⚠️ 呢個測試刻意**唔** monkeypatch provider 函數本身 —— 咁樣就會
    繞過 provider 函數內部嘅 except 分支,即係測唔到要測嘅嘢。
    要 patch 嘅係底層 urlopen,令真實 provider 函數行到佢嘅 except。
    """
    # 第一次呼叫(意見#1)成功,之後全部 429。
    def fake_urlopen(req, timeout=None):
        url = getattr(req, "full_url", "") or ""
        if "perplexity" in url:
            raise AssertionError(f"唔准打 Perplexity:{url}")
        if "minimax" in url:
            FakeResp.__call_count = getattr(FakeResp, "__call_count", 0) + 1
            if FakeResp.__call_count > 1:
                raise OSError("HTTP Error 429: Too Many Requests")
            return FakeResp({"choices": [{"message": {
                "content": "MINIMAX 描述"}}]})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(gw.urllib.request, "urlopen", fake_urlopen)

    gw._record_vision_status("none", "unknown")
    gw._VISION_2ND_OPINION = False

    # opinion #1:MiniMax 成功
    d1 = gw._minimax_vision("B64", "prompt")
    assert d1, "opinion#1 應該由 MiniMax 服務"
    assert gw._VISION_STATUS.get("provider_key") == "minimax-text-01"

    # opinion#2:MiniMax 429 → provider 層 except 觸發 → chain 冇得救
    d2 = gw._vision_second_opinion("B64", "prompt")
    assert not d2, "opinion#2 應該失敗(供應商 429)"

    assert gw._VISION_STATUS.get("provider_key") == "minimax-text-01", (
        "2nd opinion 期間嘅 provider 失敗污染咗主 dict,"
        f"provider_key={gw._VISION_STATUS.get('provider_key')!r}")
    assert gw._VISION_STATUS.get("state") == "ok", (
        "2nd opinion 期間 provider 失敗把主狀態改成 failed,"
        f"實際 {gw._VISION_STATUS.get('state')!r}")
    assert gw._VISION_STATUS_2ND.get("state") == "failed", (
        "2nd opinion 自己嘅狀態應該記錄喺 _VISION_STATUS_2ND,"
        f"實際 {gw._VISION_STATUS_2ND.get('state')!r}")


# ──────────────────────────────────────────────────────────────────────
# narration 分類 —— 冇後備,必須 fail closed
# ──────────────────────────────────────────────────────────────────────

def test_narration_classifier_fails_closed_when_minimax_dead(gw, monkeypatch):
    """MiniMax 死 → narration 分類 fail closed(當 narration,拒絕自動 commit)。

    v3.3.29 呢度原本係「MiniMax 死 → Perplexity 接手」。Perplexity 拎走
    之後,唯一選擇係 fail closed —— 寧願唔好自動 commit,都唔好存垃圾。
    """
    calls = _install_fake_http(
        gw, monkeypatch,
        minimax_raises="HTTP Error 401: Unauthorized",
    )
    gw._NARRATION_CACHE.clear()

    is_narration = gw._ai_check_narration("芝士牛扒焗薯")

    assert calls["minimax"] >= 1, "根本冇叫過 MiniMax"
    assert is_narration is True, (
        "MiniMax 死 → 必須 fail closed(當 narration),"
        f"實際 is_narration={is_narration}")


def test_narration_classifier_fails_closed_when_everything_dead(gw, monkeypatch):
    """全部 AI 死 → fail closed(當作 narration,拒絕自動 commit)。"""
    _install_fake_http(
        gw, monkeypatch,
        minimax_raises="HTTP Error 401: Unauthorized",
    )
    gw._NARRATION_CACHE.clear()

    assert gw._ai_check_narration("芝士牛扒焗薯") is True, (
        "所有 AI 都死 → 必須 fail closed(當 narration),"
        "寧願唔好自動 commit,都唔好存垃圾")


def test_second_opinion_soft_preference_still_yields_second_opinion(
        gw, monkeypatch):
    """只剩 1 個供應商時,第 2 個意見仍然要出到。

    v3.3.29 原本用 hard exclusion → 剩 1 個供應商時 2nd opinion 冇嘢用。
    soft preference 修好咗,而家淨返 1 個供應商 —— 呢個測試更加重要。
    """
    monkeypatch.setattr(gw, "_minimax_vision_text01",
                        lambda b64, prompt: "MINIMAX 描述")

    gw._record_vision_status("none", "unknown")
    gw._VISION_2ND_OPINION = False

    gw._minimax_vision("B64", "prompt")
    d2 = gw._vision_second_opinion("B64", "prompt")

    assert d2, (
        "只剩 1 個供應商時 2nd opinion 應該仍然出到(用返同一供應商)——"
        "hard exclusion 會令呢度變空")
