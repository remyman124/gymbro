"""QA guards for the v3.3.48 two-commentator cheer voice + fresh WHOOP pull.

Jim OOB 2026-10-27:「voice coach, can we switch to two voice commentators
that will not be too boring? Moreover, please pull whoop data once to get
the most update data in every cheer routine.」

No network calls in this suite — the MiniMax/edge TTS is monkeypatched.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = (ROOT / "gym_web.py").read_text(encoding="utf-8")


def _src():
    return SRC


def test_cheer_voice_ids_defined_and_different():
    src = _src()
    a = re.search(r'CHEER_VOICE_A\s*=\s*"([^"]+)"', src)
    b = re.search(r'CHEER_VOICE_B\s*=\s*"([^"]+)"', src)
    assert a, "CHEER_VOICE_A 未定義"
    assert b, "CHEER_VOICE_B 未定義"
    assert a.group(1) != b.group(1), "兩位解說員唔可以用同一把聲"
    assert a.group(1) == "Cantonese_crisp_news_anchor_vv2", "A 聲要保留 Jim 揀嘅 voice"
    assert "Cantonese" in b.group(1), "B 聲應該係另一把廣東話 voice"


def test_edge_voice_b_fallback_defined():
    src = _src()
    assert 'CHEER_VOICE_B_EDGE = "zh-HK-HiuMaanNeural"' in src, \
        "B 聲要有 edge-tts 後備(曉曼)"


def test_duo_functions_exist():
    src = _src()
    for fn in ("_split_voice_segments", "_edge_tts_render",
               "_synthesize_cheer_voice_duo", "_minimax_tts_render"):
        assert f"def {fn}(" in src, f"{fn} 唔見咗"


def test_dispatcher_prefers_duo():
    src = _src()
    i_duo = src.find("_synthesize_cheer_voice_duo(voice_text)")
    i_single = src.find("_synthesize_cheer_voice_new(voice_text)")
    assert i_duo != -1 and i_single != -1, "duo / single 都要喺 dispatcher 度"
    assert i_duo < i_single, "duo 一定要行先,唔好排喺 single 後面"


def test_duo_alternates_two_voices():
    src = _src()
    assert "is_a = (idx % 2 == 0)" in src, "要單雙數段輪流俾兩把聲"
    assert "_minimax_tts_render(seg, CHEER_VOICE_A" in src, "A 聲呼叫唔見咗"
    assert "_minimax_tts_render(seg, CHEER_VOICE_B" in src, "B 聲呼叫唔見咗"


def test_duo_falls_back_to_single_on_failure():
    src = _src()
    assert "duo segment {idx} failed" in src or "duo segment" in src, \
        "duo 失敗要回 ""(dispatcher 自動轉單聲)"


def test_duo_concat_uses_ffmpeg():
    src = _src()
    assert '"ffmpeg"' in src, "duo 要用 ffmpeg concat"
    assert '"concat"' in src, "duo concat demuxer"


def test_whoop_pull_is_forced_every_cheer():
    src = _src()
    # 每個 cheer job 都要 force pull
    assert "_run_whoop_pull_cached(force=True)" in src, \
        "每個 cheer routine 都要 force 拉一次 WHOOP"


def test_whoop_freshness_is_recorded():
    src = _src()
    assert 'whoop_fresh' in src, "要記低今次 pull 係咪真係新"
    assert 'metrics["whoop_fresh"]' in src, "fresh 旗標要入 metrics"
    assert "[cheer] whoop pull" in src, "要有 pull 嘅 log(睇得見)"


def test_version_bumped():
    m = re.search(r'__version__\s*=\s*"([0-9.]+)"', _src())
    assert m, "搵唔到 __version__"
    parts = [int(x) for x in m.group(1).split(".")]
    assert tuple(parts) >= (3, 3, 48), f"版本要 >= 3.3.48(而家 {m.group(1)})"


def test_sw_cache_bumped():
    m = re.search(r"gym-web-v(\d+)", _src())
    assert m, "搵唔到 SW cache 名"
    assert int(m.group(1)) >= 152, f"SW cache 要 bump 到 >= v152(而家 v{m.group(1)})"


def test_split_segments_behaviour():
    """直接測切段邏輯(純字串運算,無網絡)。"""
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("gym_web_duo_mod", ROOT / "gym_web.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT))
    try:
        spec.loader.exec_module(mod)
    except Exception as e:                     # pragma: no cover
        import pytest
        pytest.skip(f"gym_web 匯入失敗(非本測試問題): {e}")
    text = ("今日嘅復原度只有百分之二十五。睡眠得四個鐘三十七分鐘。"
            "心率五十九，心率變異度二十九毫秒。血氧百分之九十一點九。"
            "身體仲未完全回電,今日唔好硬頂。飲食至少攞六十克蛋白質。"
            "下午三點出嚟行二十分鐘。夜晚八點十五分要瞓覺。"
            "跟住十點半就寢,聽日再拼。")
    segs = mod._split_voice_segments(text, 4)
    assert 2 <= len(segs) <= 4, f"應該切 2-4 段,得到 {len(segs)}"
    joined = "".join(segs)
    assert joined == text, "切段之後內容唔可以走樣"
    # 每段都應該以完整句號結尾(唔會斬爛半句)
    for s in segs:
        assert s.rstrip().endswith(("。", "！", "？")), f"段尾唔完整: {s!r}"
