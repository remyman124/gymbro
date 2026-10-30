"""
v3.3.68 — Rule 74 語音格式 + 結構旁白 QA(2026-10-30)

2026-10-30 10:07 嗰稿嘅實測問題:
  - 「12:12」「08:31」「4:30」「12:30」→ 冒號時間入 voice(Rule 74 禁止)
  - 「-0.40%」→ 負號入 voice
  - 「第二段講返你嘅身體狀態,」「今朝第一句就想同你講」→ 模型自爆稿嘅結構
"""
import importlib.util
import re
from pathlib import Path

REPO = Path("/home/work/projects/gymbro")
CC_PATH = REPO / "gym_web" / "cheer_cantonese.py"
GW_PATH = REPO / "gym_web.py"


def _load():
    spec = importlib.util.spec_from_file_location("cc_v368", CC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_colon_time_becomes_cantonese():
    cc = _load()
    out = cc.tidy_numbers("英國時間 4:30 開波,香港時間 12:30。")
    assert "4:30" not in out and "12:30" not in out, out
    assert "四點半" in out, out
    assert "十二點半" in out, out
    assert not re.search(r"\d{1,2}:\d{2}", out), out


def test_colon_time_with_minutes():
    cc = _load()
    out = cc.tidy_numbers("12:12 做咗 60.5 分鐘,08:31 食咗嘢。")
    assert "十二點十二分" in out, out
    assert "八點三十一分" in out, out
    assert not re.search(r"\d{1,2}:\d{2}", out), out


def test_negative_number_no_minus_sign():
    cc = _load()
    out = cc.tidy_numbers("匯率變動係 -0.40%。")
    assert "-" not in out, out
    assert "負 0.4%" in out, out


def test_trailing_zero_decimal_trimmed():
    cc = _load()
    out = cc.tidy_numbers("升 0.50%,跌 0.40%。")
    assert "0.50" not in out and "0.40" not in out, out
    assert "0.5%" in out and "0.4%" in out, out


def test_structural_meta_removed():
    cc = _load()
    out = cc.rewrite("第二段講返你嘅身體狀態,復原指數 49%。今朝第一句就想同你講:加油。")
    assert "第二段" not in out, out
    assert "第一句" not in out, out
    assert "復原指數 49%" in out, out


def test_no_colon_time_in_real_cheer_artifact():
    """已出嘅 artifact 唔准有冒號時間(守住 Rule 74)。"""
    import glob, os
    # 只查最新嗗個 artifact(舊嘅係修正前出嘅,唔應該阻住 build)
    arts = sorted(glob.glob("/home/work/.hermes/cheer_artifacts/cheer_*/cheer_text.txt"),
                  key=os.path.getmtime)[-1:]
    assert arts, "搵唔到 artifact"
    for p in arts:
        txt = open(p, encoding="utf-8").read()
        assert not re.search(r"\d{1,2}:\d{2}", txt), f"{p} 有冒號時間"


def test_version_and_cache():
    src = GW_PATH.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    assert tuple(int(x) for x in m.groups()) >= (3, 3, 69)
    assert "gym-web-v173" in src
    assert "gym-web-v171" not in src


def test_prompt_has_structural_and_time_rules():
    src = GW_PATH.read_text(encoding="utf-8")
    assert "唔准自爆稿嘅結構" in src, "prompt 缺少結構旁白規則"
    assert "時間唔准用冒號" in src, "prompt 缺少時間格式規則"
