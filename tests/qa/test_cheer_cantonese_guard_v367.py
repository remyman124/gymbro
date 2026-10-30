"""
v3.3.67 — 廣東話 cheer 守門員第二輪 QA(2026-10-30)

背景(實測事故):
  - 09:34 出嘅稿 audit ok=False,普通話詞「對齊」漏咗入 TTS。
    根因:MANDARIN_PHRASES 黑名單有 160 個字,但 MANDARIN_TO_CANTONESE
    譯法表只有 47 條 → rewrite() 根本改唔走「對齊」→ retry 用盡都 fail。
  - 同一稿出現「、 3 食物」—— 剝走英文 "Omega" 後留低孤兒數字同標點。
  - 審核唔過都照樣 publish(2026-10-30 09:34 就係咁)。

本檔案守住三樣嘢:
  1. 黑名單 ↔ 譯法表必須一一對應(唔准再漏)
  2. 英文清理唔准留低孤兒數字 / 空懸標點
  3. 審核唔過 → 唔准出稿,改用本地決定式廣東話模板
"""
import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path("/home/work/projects/gymbro")
CC_PATH = REPO / "gym_web" / "cheer_cantonese.py"
GW_PATH = REPO / "gym_web.py"


def _load_cc():
    spec = importlib.util.spec_from_file_location("cheer_cantonese_probe", CC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cc():
    return _load_cc()


# ─────────────────────────────────────────────────────────────
# 1. 黑名單 ↔ 譯法表完整性
# ─────────────────────────────────────────────────────────────
def test_every_blacklist_word_has_cantonese_mapping(cc):
    """每個黑名單字都要有譯法,否則 rewrite() 改唔走,audit 永遠 fail。"""
    missing = [p for p in cc.MANDARIN_PHRASES if p not in cc.MANDARIN_TO_CANTONESE]
    assert not missing, (
        f"{len(missing)} 個黑名單字冇譯法,rewrite() 改唔走: {missing[:12]}"
    )


def test_no_self_mapping(cc):
    """映射值 == 原字 = 改唔走(自我映射係 2026-10-30「對齊」同類事故)。"""
    self_map = [k for k, v in cc.MANDARIN_TO_CANTONESE.items() if k == v]
    assert not self_map, f"自我映射(改唔走): {self_map}"


def test_mapping_value_does_not_contain_source(cc):
    """譯法值唔可以包含原字,否則替換完仍然撞黑名單(例:呼應→相呼應)。"""
    # 只適用於黑名單入面嘅字(非黑名單嘅字,譯法含原字無害)
    bad = [k for k, v in cc.MANDARIN_TO_CANTONESE.items()
           if k in v and k != v and k in cc.MANDARIN_PHRASES]
    assert not bad, f"譯法值包含原字(替換完仍然 fail): {bad}"


def test_all_blacklist_words_rewrite_to_passing_audit(cc):
    """160 個黑名單字逐個入句子 → rewrite() 後 audit 一定要過。"""
    failures = []
    for phrase in cc.MANDARIN_PHRASES:
        src = f"今日{phrase}嘅情況好一般,唔算太差,做嘢照計數。"
        out = cc.rewrite(src)
        au = cc.audit(out)
        if not au["ok"]:
            failures.append((phrase, au["issues"]))
    assert not failures, f"{len(failures)} 個字改寫後仍然 fail: {failures[:6]}"


def test_blacklist_word_du_jiao_can_be_removed(cc):
    """2026-10-30 實際事故字:「對齊」一定要改得走。"""
    out = cc.rewrite("目標同價值對齊嘅分別好重要,聽日要留意。")
    assert "對齊" not in out, f"「對齊」仲喺度: {out}"
    assert cc.audit(out)["ok"], cc.audit(out)["issues"]


# ─────────────────────────────────────────────────────────────
# 2. 英文清理唔准留孤兒
# ─────────────────────────────────────────────────────────────
def test_omega3_becomes_chinese_without_orphan_digit(cc):
    """「Omega 3」→ 「奧米加三」(唔係剝走英文剩低孤兒「3」)。"""
    src = "濕疹活躍期間可以多啲深色蔬菜、Omega 3 食物,例如三文魚。"
    out = cc.rewrite(src)
    assert "奧米加三" in out, f"Omega-3 冇轉中文: {out}"
    assert "Omega" not in out
    assert not re.search(r"[、，,]\s*\d", out), f"仲有孤兒數字: {out}"
    assert cc.audit(out)["ok"], cc.audit(out)["issues"]


def test_no_orphan_number_after_english_strip(cc):
    """剝走未知英文 token 之後,唔准留低「、 3」呢類空懸殘渣。"""
    # 重點:英文 token 夾住數字(Omega-3 / GPT-4)要成個剝走,唔留數字;
    # 而「英文 + 空格 + 合法數量」唔可以被誤刪(會變成「食咗 個」)。
    cases = [
        ("多啲深色蔬菜、Omega 3 食物", r"[、，,]\s*\d"),
        ("試下 GPT-4 同 GPT 4 嘅分別", r"(?<![\u3400-\u9fff\d])4(?![\u3400-\u9fff])"),
        ("今日食咗 3 個模型試下", r"食咗\s*個"),
    ]
    for src, pat in cases:
        out = cc.rewrite(src)
        assert not re.search(pat, out), f"'{src}' → '{out}' 有孤兒數字"


def test_no_dangling_punctuation_after_strip(cc):
    """剝走英文之後唔准留低「、 」呢類空懸標點。"""
    out = cc.rewrite("今日食咗 Apple 個蘋果,同埋 Banana 條香蕉。")
    assert not re.search(r"[、，,]\s", out), f"空懸標點: {out}"
    assert not re.search(r"\s{2,}", out), f"多餘空格: {out}"


def test_english_brand_translated_not_dropped(cc):
    """已知品牌要譯成中文(唔係直接剝走,否則句子讀唔通)。"""
    out = cc.rewrite("用 Hugging Face 訓練模型,燒咗 200 kcal。")
    assert "開源模型平台" in out, out
    assert "kcal" in out, "允許嘅單位唔可以被剝走"


def test_allowed_units_survive(cc):
    for tok in ("Jim", "CIO", "HKT", "kcal", "kg"):
        out = cc.rewrite(f"今日{ tok }嘅狀態一般,繼續保持。")
        assert tok in out, f"允許嘅 token '{tok}' 被剝走咗: {out}"


# ─────────────────────────────────────────────────────────────
# 3. 審核唔過 → 唔准出稿
# ─────────────────────────────────────────────────────────────
def test_generation_pipeline_has_final_fallback_guard():
    """gym_web.py 一定要有「最終審核唔過 → 改用本地廣東話模板」嘅防線。"""
    src = GW_PATH.read_text(encoding="utf-8")
    assert "FINAL AUDIT FAILED" in src, "缺少最終審核防線"
    assert "_cheer_fallback_text(metrics, fire_type)" in src, "未接本地廣東話模板"
    assert "final_fallback" in src, "audit log 未記錄 fallback"


def test_fallback_text_is_pure_cantonese(cc):
    """fallback 模板本身一定要過 audit(否則出事時會讀普通話)。"""
    import ast
    src = GW_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_cheer_fallback_text":
            fn = node
            break
    assert fn is not None, "搵唔到 _cheer_fallback_text"
    # 喺一個隔離 namespace 執行佢
    ns = {"datetime": __import__("datetime").datetime,
          "timezone": __import__("datetime").timezone,
          "timedelta": __import__("datetime").timedelta}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<f>", "exec"), ns)
    metrics = {"recovery_pct": 68, "recovery_state": "SCORED", "hrv_ms": 52.3,
               "rhr_bpm": 54, "sleep_bed_hr": 8.1, "sleep_actual_hr": 7.4,
               "sleep_awake_hr": 0.7, "today_workout_count": 2,
               "sleep_perf_pct": 81}
    txt = ns["_cheer_fallback_text"](metrics, "morning")
    au = cc.audit(txt)
    assert au["ok"], f"fallback 唔係廣東話: {au['issues']}"


# ─────────────────────────────────────────────────────────────
# 4. 版本 / SW cache
# ─────────────────────────────────────────────────────────────
def test_version_bumped():
    src = GW_PATH.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', src)
    assert m, "搵唔到 __version__"
    ver = tuple(int(x) for x in m.groups())
    assert ver >= (3, 3, 68), f"版本要 >= 3.3.67, 而家 {ver}"


def test_service_worker_cache_bumped():
    src = GW_PATH.read_text(encoding="utf-8")
    assert "gym-web-v172" in src, "SW cache 要 bump 到 gym-web-v172"
    assert "gym-web-v170" not in src, "舊 cache 名仲喺度"
