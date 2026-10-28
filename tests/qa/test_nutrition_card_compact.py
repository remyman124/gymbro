"""QA — v3.3.53: 日結 JPG「矮身版」(compact) — 高度降低,iPhone 友好。

Jim OOB 2026-10-27「My iPhone is duo now. Pls assist the jpg download image
with lower height」。

實測背景:2026-10-27 嘅日結卡係 1080 x 1736(237 KB)。喺 iPhone 上面
睇同存都唔方便 → 想要**更矮**嘅版本。

修法(唔重寫版面!):先用原本 1080px 闊嘅排版完整渲染,再等比縮到
目標高度(1280px)。零 layout 風險,但真係矮咗:
  原版  1080 x 1736  237 KB
  矮身版 796 x 1280  156 KB

呢個 suite 守住:
  1. compact 係 opt-in(預設一定要係原尺寸,唔可以靜靜哋改變大家
     已經習慣嘅排版)。
  2. 矮身版高度一定 ≤ CARD_COMPACT_MAX_H。
  3. 矮身版用**獨立** cache key + 獨立檔名 —— 唔可以覆蓋原尺寸版本
     (否則 Jim 撳一次矮身版就永久冇咗原版)。
  4. 前端 toggle + reload 流程完整。
  5. 下載檔名 / URL 要帶 _compact,唔可以撞原版。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "gym_web.py"
TPL = ROOT / "templates" / "index.html"


def _web():
    return WEB.read_text(encoding="utf-8")


def _tpl():
    return TPL.read_text(encoding="utf-8")


def _strip_comments(text):
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text


# ---------------------------------------------------------------------------
# 1. compact 係 opt-in
# ---------------------------------------------------------------------------

def test_compact_is_opt_in():
    """預設(無 compact 參數)一定要行原本嗰條路徑。"""
    web = _web()
    assert re.search(
        r'compact\s*=\s*\(request\.args\.get\("compact"\).*?in\s*\(\s*"1"', web, re.S
    ), "compact 必須由 query param 決定,唔可以預設開"


def test_render_default_is_not_compact():
    """_render_nutrition_card_pil 嘅 compact 預設值一定要係 False。"""
    web = _web()
    m = re.search(r"def _render_nutrition_card_pil\((.*?)\)\s*->", web, re.S)
    assert m, "搵唔到 _render_nutrition_card_pil"
    assert "compact: bool = False" in m.group(1), (
        "compact 預設值一定要 False:%s" % m.group(1)
    )


def test_non_compact_path_unchanged():
    """非 compact 時,輸出檔名一定要同原本一樣(唔加 suffix)。"""
    web = _web()
    assert re.search(r'_c_suffix\s*=\s*"_compact"\s*if\s+compact\s*else\s*""', web), (
        "要用 suffix 變數,唔好喺 non-compact 路徑加任何嘢"
    )


# ---------------------------------------------------------------------------
# 2. 高度上限
# ---------------------------------------------------------------------------

def test_compact_max_height_constant():
    """一定要有一個目標高度常數。"""
    web = _web()
    m = re.search(r"CARD_COMPACT_MAX_H\s*=\s*(\d+)", web)
    assert m, "搵唔到 CARD_COMPACT_MAX_H"
    assert 800 <= int(m.group(1)) <= 1600, (
        "目標高度 %s 唔合理(太矮睇唔清,太高就冇意思)" % m.group(1)
    )


def test_compact_downscaling_applied():
    """compact 一定要真係縮圖,唔係只係改個檔名。"""
    web = _web()
    assert re.search(r"if\s+compact\s*:", web), "搵唔到 compact 縮圖邏輯"
    assert re.search(r"img\.resize\(", web), "compact 版一定要做 resize"
    assert "Image.LANCZOS" in web, "resize 一定要用 LANCZOS(質素好過預設 NEAREST)"


def test_compact_uses_target_height_constant():
    """縮圖目標高度一定要用常數,唔可以 hardcode 另一個數。"""
    web = _web()
    assert "CARD_COMPACT_MAX_H" in web, "目標高度應該用常數"
    seg = web[web.index("if compact:"):web.index("if compact:") + 800]
    assert "CARD_COMPACT_MAX_H" in seg, (
        "縮圖邏輯內部要用 CARD_COMPACT_MAX_H,唔好再寫死另一個數"
    )


# ---------------------------------------------------------------------------
# 3. 獨立 cache / 檔名(唔可以覆蓋原尺寸版本)
# ---------------------------------------------------------------------------

def test_compact_has_separate_cache_key():
    """cache key 一定要分開,唔可以共用。"""
    web = _web()
    # v3.3.55: cache key 格式改成 `f"{_ck}{_pp_sfx}" + ("_compact" if compact else "")`
    #   (_pp_sfx = 每頁餐數 suffix,避免唔同 per_page 互相覆蓋)
    assert re.search(
        r'cache_key\s*=\s*f"\{_ck\}\{_pp_sfx\}"\s*\+\s*\("_compact"\s+if\s+compact\s+else\s+""\)',
        web,
    ), "cache key 一定要為矮身版分開(否則會互相覆蓋)"


def test_compact_uses_distinct_filenames():
    """輸出檔名一定要帶 _compact。"""
    web = _web()
    assert re.search(r'_c_sfx\s*=\s*"_compact"\s*if\s+compact\s*else\s*""', web), (
        "API 層嘅輸出檔名要帶 _compact"
    )
    assert 'nutrition_{date_iso}{_c_suffix}' in web, (
        "renderer 層嘅輸出檔名要帶 _compact"
    )


def test_compact_does_not_overwrite_legacy():
    """矮身版唔可以寫 legacy 檔(會覆蓋原尺寸版本)。"""
    web = _web()
    seg = web[web.index("legacy_write_path = None"):web.index("legacy_write_path = None") + 400]
    assert "not compact" in seg, (
        "legacy 檔寫入要加 `not compact` 保護,否則矮身版會蓋咗原版"
    )


def test_compact_download_filename():
    """下載檔名要帶 _compact,唔可以撞原版。

    v3.3.54: 格式統一做 `gymbro_foodlog_<date>[_compact]_p<N>.jpg`
    (之前係 `_p1_compact.jpg`,同 preview 嘅 `_compact_p1.jpg` 唔一致)。
    """
    web = _web()
    assert "'_compact' if compact else ''" in web, (
        "Content-Disposition 嘅檔名要區分矮身版"
    )
    assert "gymbro_foodlog_{target_date}" in web, "server 檔名要有 gymbro_foodlog_ 前綴"


# ---------------------------------------------------------------------------
# 4/5. 前端
# ---------------------------------------------------------------------------

def test_frontend_compact_toggle_removed():
    """v3.3.59:個 toggle row 由 modal **移除**。

    Jim OOB 2026-10-27「Remove the toggle row ... such that I can see the
    button download button」—— 佢喺 modal 底部佔 ~42pt,真機短屏會將
    下載掣推落視窗外(Playwright 量到喺 375x667 掣 bottom=621,喺
    viewport 內;但 Jim 實際部機嘅可見 app 視窗更矮,iOS 底條 /
    home indicator / Slide Over 都会食高度)。

    後端 `?compact=1` 仍然支援(舊 URL 唔會壞),只係冇 UI 開關。
    """
    tpl = _strip_comments(_tpl())
    assert "foodCardPreview.compact = !foodCardPreview.compact" not in tpl, (
        "仲有「檔案細啲」toggle handler(應該移除)"
    )
    assert "檔案細啲" not in tpl, "仲有「檔案細啲」toggle row(應該移除)"




def test_frontend_reload_method_exists():
    """切換之後要重新載入預覽。"""
    tpl = _tpl()
    assert "async reloadFoodCardPreview()" in tpl, "缺少 reloadFoodCardPreview()"
    assert "reloadFoodCardPreview()" in tpl, "toggle 要叫 reloadFoodCardPreview()"












# ---------------------------------------------------------------------------
# 回歸守衛
# ---------------------------------------------------------------------------

def test_card_render_pipeline_intact():
    """呢次修正唔准改壞 render pipeline。"""
    web = _web()
    for fn in ("_render_nutrition_card_pil", "_make_thumb", "_upload_to_drive"):
        assert f"def {fn}" in web, f"{fn} 唔見咗"
    # v3.3.55: MEALS_PER_PAGE 變成可調(per_page),但預設仍然係 6
    # v3.3.57: 每頁預設由 6 改成 5(Jim OOB「Make it 5 meals max per screen」)
    assert "CARD_PER_PAGE_DEFAULT = 5" in web, "每頁餐數預設一定要係 5"
    assert "MEALS_PER_PAGE = per_page if per_page in CARD_PER_PAGE_ALLOWED" in web, (
        "分頁邏輯唔可以改(只係加咗 per_page 參數)"
    )
