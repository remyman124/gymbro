"""
RULE 25 (加固) — 泛用菜名就算 vision 正常都唔准入账 (v3.3.30)

背景 (2026-10-21, Jim OOB「Remove apiyi stuff」嘅後續發現):

  移除 APIyI 之後,我做咗多輪供應商鏈端到端測試,發現一個 v3.3.25
  冇覆蓋到嘅漏洞:

    **vision provider 好正常,但「抽菜名」失敗** → 名字跌返去
    「未識別菜式」→ 一樣照寫入 Jim 個 golden copy。

  v3.3.25 嘅防線只擋「供應商全死」(state == "failed" 而且冇
  vision_desc)。但呢個 case 係 vision_desc 有內容、AI 有答、有營養
  數字,淨係**名**抽出唔到 —— 防線就漏咗。

  實測:4 輪端到端測試入面,有 1 輪「未識別菜式」差啲就寫咗入去。
  呢個正正就係 2026-10-20 污染事故(26 行 + 7 行垃圾)嘅同一個簽名。

v3.3.30 嘅修復:
  1. `_GENERIC_DISH_NAMES_BLOCKLIST` —— 泛用名黑名單
  2. `_scan_preview_core` 入面,如果**全部** dish 都係泛用名 →
     ok:false + 零 Drive file + 零 Sheet row(同一個 top-level key set)
  3. 如果只有部分係泛用名 → 逐個剔走,淨係 commit 有名嗰啲
  4. 前端見到 ok=false 要話畀 Jim 聽,唔可以扮成「已入账」

呢組測試係 **unit level** — 唔打 network、唔打 Google API、唔掂
Jim 個 Sheet。純粹驗 rule 25 嘅分支邏輯同擋截位置。
"""

import importlib.util
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO / "test_fixtures"


@pytest.fixture(scope="module")
def gw():
    """Load gym_web.py by path (NOT as the `gym_web` package — that
    resolves to the gym_web/ directory, not gym_web.py)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    env.setdefault("GYMBRO_WRITE_MODE", "dryrun")
    os.environ["GYMBRO_WRITE_MODE"] = "dryrun"
    spec = importlib.util.spec_from_file_location(
        "gym_web_rule25", REPO / "gym_web.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ──────────────────────────────────────────────────────────────────────────
# 1. 黑名單
# ──────────────────────────────────────────────────────────────────────────

def test_generic_dish_name_is_blocklist(gw):
    """「未識別菜式」呢類泛用名一定要喺 blocklist 入面。"""
    bl = gw._GENERIC_DISH_NAMES_BLOCKLIST
    assert "未識別菜式" in bl
    assert "未識別" in bl
    assert "" in bl
    # 真實菜名唔可以喺黑名單入面
    for real in ("牛扒", "叉燒飯", "絲襪奶茶", "雞胸肉沙律"):
        assert real not in bl, f"真實菜名唔應該被封:{real}"


# ──────────────────────────────────────────────────────────────────────────
# 2. 擋截邏輯存在,而且位置啱
# ──────────────────────────────────────────────────────────────────────────

def test_generic_name_guard_exists_in_source(gw):
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    assert "_GENERIC_DISH_NAMES_BLOCKLIST" in src
    assert "BLOCKED commit — every dish name is a" in src


def test_guard_runs_before_drive_upload(gw):
    """攔截一定要早過 Drive 上傳 —— 否則會留低孤兒圖片。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    block_at = src.index("BLOCKED commit — every dish name is a")
    drive_at = src.index("upload_to_drive", block_at)
    assert block_at < drive_at, "攔截一定要早過 Drive 上傳"


def test_blocked_response_has_same_key_set(gw):
    """攔截回應嘅 top-level key set 要同成功路徑一致
    (Isak's §4.3 regression contract)。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    block_at = src.index("BLOCKED commit — every dish name is a")
    tail = src[block_at:block_at + 1400]
    for key in ("ok", "auto_committed", "image_path", "image_url",
                "thumbnail_url", "drive_image_url", "vision_desc",
                "vision_short", "is_multi_entry", "entries", "committed",
                "multi_entry", "preview"):
        assert f'"{key}"' in tail, f"攔截回應缺少 key:{key}"


# ──────────────────────────────────────────────────────────────────────────
# 3. 前端要話畀 Jim 聽
# ──────────────────────────────────────────────────────────────────────────

def test_frontend_tells_jim_when_blocked(gw):
    tpl = (REPO / "templates" / "index.html").read_text(encoding="utf-8")
    assert "res.ok === false" in tpl, "前端要處理 ok=false"
    assert "搵唔到菜名" in tpl, "要話畀 Jim 聽呢張相冇入账"
    # 唔可以仲照寫 auto_committed = true
    assert "item.auto_committed = false;" in tpl


# ──────────────────────────────────────────────────────────────────────────
# 4. Rule 29 — APIyI 已經完全移除,唔准再黐返嚟
# ──────────────────────────────────────────────────────────────────────────

def test_apiyi_fully_removed(gw):
    """Jim OOB 2026-10-21「Remove apiyi stuff」。

    APIyI 條 key 2026-10-20 就死咗(每個 endpoint 都 401),所以成條
    供應商依賴要清乾淨:冇 function、冇 pool 成員、冇 key 檢查、
    冇 endpoint call。
    """
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")

    for dead in ("_apiyi_vision_gpt4o", "_apiyi_vision_analyze",
                 "_apiyi_api_key", "_apiyi_nutrition_enrich"):
        assert dead not in src, f"仲有 APIyI 依賴:{dead}"

    pool = gw._VISION_PROVIDER_POOL
    assert not any("apiyi" in str(p[0]).lower() for p in pool), (
        f"供應商池仲有 APIyI:{pool}")
    assert len(pool) >= 2, f"供應商池至少要 2 個(唔可以又變單點故障):{pool}"

    assert "api.apiyi.com" not in src, "仲有 APIyI endpoint call"
    assert "vip.apiyi.com" not in src, "仲有 APIyI vip endpoint call"
    assert '"apiyi": bool(' not in src, "/api/health 仲報 apiyi key"

    # 2nd opinion 改名後仍然存在(唔係刪走功能)
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
