"""
v3.3.65 — 廣東話守門員 QA(Jim OOB 2026-10-29)

背景:
  「The cheer you generated contains mandarin. Pls fix it. And always refer to
   gymbro to generate」
  「I can only listen cantonese not mandarin! this is serious」

  診斷:
    1. 聲線本身係粵語(MiniMax Cantonese_crisp_news_anchor_vv2 /
       edge zh-HK-HiuMaanNeural)→ 問題係**文字稿**寫成書面普通話
       (「照亮」「準備好」「兌現成力量」⋯⋯)。
    2. 晨間 cheer 稿用咗在床時間(9.82h)當實瞓時間(實際 5.35h)。
    3. raw float(0.33847734)+ 阿拉伯日期(10 月 27 日)→ TTS 讀出嚟
       機械又似普通話。
    4. MCP `trigger_cheer_pipeline` 舊版只係拎 payload 叫 Alonso 自己
       寫稿 → 自己寫嘅稿唔會經過守門員 → 普通話漏出嚟。

呢組測試係 **unit level + source-level** — 唔打 network、唔打 Google API、
唔掂 Jim 個 Sheet、唔產生任何 TTS 成本。
"""

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
GYM_WEB = REPO / "gym_web.py"
CANTO = REPO / "gym_web" / "cheer_cantonese.py"
MCP = REPO / "gymbro_mcp_server.py"

# 2026-10-29 晨間 cheer 真實出嘅壞稿(節錄,保留原文特徵)
BAD_CHEER = (
    "朝早陽光灑入窗邊，你今日由黃燈狀態起步：復原指數 61.0%，心跳變異 37.6 毫秒，"
    "靜止心跳 55 下/分鐘。頭兩句畫面，就係光線慢慢照亮睡房，再望住手錶上仍然有進步空間嘅數字。\n\n"
    "噉晚瞓咗 9.82 個鐘，時間其實夠，但表現只有 39.0%；疲勞度 0.33847734，"
    "代表你唔算攰到散，卻未完全充返電。連日做嘢要保留，唔好一醒就靠意志力硬頂。\n\n"
    "尋日 2026-10-28 你 log 咗 8 餐，合共約 2138 kcal。"
)

GOOD_CHEER = (
    "朝早 10:44，窗外已經醒晒，你個身仲未收工，床頭嗰 9.82 個鐘只係假裝瞓過。"
    "實際得 5.34 個鐘，中間醒咗 4.45 個鐘，睡眠表現得 39%，今日個底線自然未補返。\n\n"
    "復原指數 61%，黃燈；心跳變異 37.6 毫秒，靜止心跳 55 下，"
    "心跳本身平，但個神經狀態仲未完全回穩。今日 0 餐，一口嘢都未食，唔好等到攰先補。"
)


@pytest.fixture(scope="module")
def canto():
    spec = importlib.util.spec_from_file_location("cheer_cantonese_qa", CANTO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gw_src():
    return GYM_WEB.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────
# 1. 驗證器
# ─────────────────────────────────────────────────────────────
def test_bad_cheer_is_rejected(canto):
    """2026-10-29 真實壞稿一定要被驗證器捉到(唔可以放行)。"""
    a = canto.audit(BAD_CHEER)
    assert a["ok"] is False
    assert a["issues"], "壞稿一定要有 issue"
    # 至少要捉到英文 token(gym/log/session)同普通話書面詞
    joined = " ".join(a["issues"])
    assert "英文字" in joined or "普通話" in joined


def test_good_cantonese_passes(canto):
    """真·廣東話稿一定要通過。"""
    a = canto.audit(GOOD_CHEER)
    assert a["ok"] is True, f"好稿被誤殺: {a['issues']}"
    assert a["density"] >= 4.0
    assert a["en_leaks"] == []


def test_mandarin_blacklist_hits(canto):
    """普通話書面詞一定要命中(逐個試)。"""
    for phrase in ("然而", "此外", "因此", "趨於", "有所提升", "值得注意的是"):
        a = canto.audit(f"今日狀態{phrase}穩定,繼續保持。")
        assert a["ok"] is False, f"「{phrase}」應該被捉到"
        assert any("普通話" in i for i in a["issues"])


def test_particle_ratio_detects_mandarin(canto):
    """「的/了/是/在」過多 → 判書面普通話。"""
    mandarin = ("今日的復原指數有所提升整體狀態趨於穩定建議保持規律作息 "
                "需要注意的是今日尚未記錄訓練這是應該的安排是必須的")
    a = canto.audit(mandarin)
    assert a["ok"] is False
    assert a["ratios"]["de"] > 0


def test_en_leak_detected(canto):
    a = canto.audit("今日做咗 3 個 session,仲有 log 咗 8 餐,去咗 gym。")
    assert a["ok"] is False
    assert set(a["en_leaks"]) >= {"session", "log", "gym"}


def test_allowed_units_not_flagged(canto):
    """kcal/kg 等單位唔算英文漏(數字單位要保留)。"""
    a = canto.audit("今日食咗 2138 kcal,蛋白質 152 克,體重 77.4 公斤。")
    assert a["en_leaks"] == []
    assert a["ok"] is True


# ─────────────────────────────────────────────────────────────
# 2. 決定式重寫
# ─────────────────────────────────────────────────────────────
def test_rewrite_converts_mandarin_phrases(canto):
    fixed = canto.rewrite("今日狀態然而保持穩定,此外需要注意的是時間其實夠。")
    assert "然而" not in fixed
    assert "此外" not in fixed
    assert "不過" in fixed
    assert "另外" in fixed


def test_rewrite_converts_english_terms(canto):
    fixed = canto.rewrite("你做咗 3 個 session,仲有 log 咗 8 餐。")
    assert "session" not in fixed
    assert "log" not in fixed


def test_rewrite_improves_or_equal(canto):
    """重寫後 issues 唔應該變多。"""
    before = canto.audit(BAD_CHEER)
    after = canto.audit(canto.rewrite(BAD_CHEER))
    assert len(after["issues"]) <= len(before["issues"])


# ─────────────────────────────────────────────────────────────
# 3. 數字整理(TTS 唔可以讀 raw float / 阿拉伯日期)
# ─────────────────────────────────────────────────────────────
def test_tidy_numbers_rounds_raw_float(canto):
    t = canto.tidy_numbers("疲勞度 0.33847734,強度 12.8509。")
    assert "0.33847734" not in t
    assert "12.8509" not in t
    assert "0.34" in t


def test_tidy_numbers_strips_point_zero_percent(canto):
    assert canto.tidy_numbers("復原 61.0%") == "復原 61%"


def test_tidy_numbers_converts_dates_to_chinese(canto):
    t = canto.tidy_numbers("尋日 2026-10-28 做嘢,10 月 27 日 13:14 桑拿")
    assert "2026-10-28" not in t
    assert "10 月 27 日" not in t
    assert "十月" in t and "號" in t


def test_tidy_numbers_leaves_plain_numbers(canto):
    t = canto.tidy_numbers("步數 5171 步,蛋白質 152 克,2138 kcal")
    assert "5171" in t and "152" in t and "2138" in t


# ─────────────────────────────────────────────────────────────
# 4. Prompt 有硬規則
# ─────────────────────────────────────────────────────────────
def test_system_block_has_cantonese_rules(canto):
    blk = canto.system_block()
    assert "廣東話" in blk or "粵語" in blk
    assert "普通話" in blk
    # 一定要有 few-shot 標準答案(比淨係講規則有效)
    assert "標準答案" in blk
    # 黑名單要真係入咗 prompt
    assert "然而" in blk and "趨於" in blk


def test_gym_web_wires_system_block(gw_src):
    assert "from gym_web import cheer_cantonese as _canto" in gw_src
    assert "_canto.system_block()" in gw_src


# ─────────────────────────────────────────────────────────────
# 5. gym_web 嘅接線
# ─────────────────────────────────────────────────────────────
def test_narrative_has_audit_retry_loop(gw_src):
    assert "_canto.audit(text)" in gw_src
    assert "_call_minimax(user_prompt + fb)" in gw_src
    assert "_cheer_mandarin_judge" in gw_src


def test_voice_path_has_guard(gw_src):
    """TTS 入面嘅文字一定要過守門員(最後防線)。"""
    i = gw_src.find("def _synthesize_cheer_voice(")
    assert i > 0
    body = gw_src[i:gw_src.find("\ndef ", i + 1)]
    assert "_canto.audit" in body
    assert "_canto.rewrite" in body
    assert "_canto.tidy_numbers" in body


def test_fallback_cheer_has_no_english_leaks(canto, gw_src):
    """真跑 fallback 模板,睇輸出有冇英文漏(只准 Jim / HKT)。"""
    import importlib.util as _il
    spec = _il.spec_from_file_location("gym_web_fb_qa", GYM_WEB)
    mod = _il.module_from_spec(spec)
    spec.loader.exec_module(mod)
    metrics = {
        "recovery": 61, "hrv": 37.6, "rhr": 55, "spo2": 95.8,
        "sleep_bed_hr": 9.82, "sleep_actual_hr": 5.34, "sleep_awake_hr": 4.45,
        "sleep_perf_pct": 39, "sleep_debt_hr": 2.13, "strain": 1.47,
        "steps": 0, "kcal": 0, "protein": 0, "carbs": 0, "fat": 0,
        "workout_n": 0, "recent_workouts_zh": "",
    }
    out = mod._cheer_fallback_text(metrics, "morning")
    assert out and len(out) > 30
    words = set(re.findall(r"[A-Za-z]+", out))
    assert words <= {"Jim", "HKT"}, f"模板有英文漏: {words}"
    # 同一份稿要過得廣東話驗證器
    a = canto.audit(out)
    assert a["ok"] is True, f"fallback 稿唔過驗證: {a['issues']}"


def test_sleep_actual_hours_exposed(gw_src):
    """在床時間 ≠ 實瞓時間 —— 兩個欄位都要有。"""
    assert '"sleep_actual_hr"' in gw_src
    assert '"sleep_awake_hr"' in gw_src
    i = gw_src.find("def _daily_coaching_narrative_prompt(")
    body = gw_src[i:gw_src.find("\ndef ", i + 1)]
    assert "實際瞓咗" in body
    assert "中途醒咗" in body


def test_audit_log_written(gw_src):
    assert "cheer_cantonese_audit.log" in gw_src


def test_audit_artifact_written(gw_src):
    assert "cheer_audit.json" in gw_src


def test_version_bumped(gw_src):
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', gw_src)
    assert m, "搵唔到 __version__"
    ver = tuple(int(x) for x in m.groups())
    assert ver >= (3, 3, 65), f"version 要 >= 3.3.65, 而家 {ver}"
    assert re.search(r"gym-web-v1(6\d|7\d)", gw_src)


# ─────────────────────────────────────────────────────────────
# 6. MCP:cheer 一律經 gymbro 生成(Jim「always refer to gymbro to generate」)
# ─────────────────────────────────────────────────────────────
def test_mcp_trigger_cheer_delegates_to_gymbro():
    src = MCP.read_text(encoding="utf-8")
    i = src.find("def trigger_cheer_pipeline(")
    assert i > 0
    body = src[i:src.find("\n@mcp.tool()", i)]
    # 必須觸發 gymbro 自己條 pipeline
    assert '"/api/cheer"' in body
    assert '"/api/cheer/status"' in body
    # 舊版「叫 Alonso 自己寫稿」嘅指示一定要冇咗
    assert "Alonso then writes the cheer" not in body
    assert "Write 8-section cheer text" not in body
    # 一定要攞廣東話審核結果
    assert "cantonese_audit" in body
    assert "cheer_audit.json" in body


# ─────────────────────────────────────────────────────────────
# 7. 語言裁判(Jim OOB 升級:自己寫嘅稿唔會經守門員)
# ─────────────────────────────────────────────────────────────
def test_mandarin_judge_exists(gw_src):
    assert "def _cheer_mandarin_judge(" in gw_src
    i = gw_src.find("def _cheer_mandarin_judge(")
    body = gw_src[i:gw_src.find("\ndef ", i + 1)]
    assert "普通話" in body and "粵語" in body
    # 裁判唔好阻住出稿(掛咗要 fallback)
    assert "judge failed" in body
