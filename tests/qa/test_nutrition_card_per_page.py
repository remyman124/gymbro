"""QA — v3.3.55: 日結 JPG「每頁餐數」(6/4/3)→ 每張圖真正變矮。

Jim OOB 2026-10-27「Jpg download even shorter pls. I'm using iPhone duo」。

**點解唔可以直接「縮細」**
等比縮細(1080x1736 → 796x1280)比例一樣(1.607)→ 喺 iPhone 上面一樣係
撐滿螢幕闊度 → 顯示高度**完全一樣**(627pt)。縮細只係令檔案細啲,
畫面長度冇變 → Jim 講「even shorter」就係呢個原因。

**正確做法:每頁少啲餐**
行同一套排版(零 layout 風險、零 overlap 風險),只係每張圖放少啲餐:

    每頁餐數   6 餐/頁  1080 x 1736   比例 1.607   手機顯示 ~627pt
               4 餐/頁  1080 x 1256   比例 1.163   手機顯示 ~454pt  (-28%)
               3 餐/頁  1080 x 1016   比例 0.941   手機顯示 ~367pt  (-41%)

呢個 suite 守住:
  1. per_page 參數由 server 真正生效(唔係淨係前端假象)。
  2. **兩個 endpoint 要一致** —— card 同 card_url 用唔同 per_page 會令
     前端收到錯誤 page_total(超級難 debug)。
  3. 檔名 / cache key 要帶 per_page suffix,唔可以互相覆蓋。
  4. 6 餐/頁要保持舊檔名(backwards compatible,唔好整爛舊 URL)。
  5. 前端要真係送 per_page 落去(預覽 / 切頁 / 下載 三條路徑)。
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
# 1. per_page 真正生效
# ---------------------------------------------------------------------------

def test_per_page_choices_and_default():
    """只准 6/4/3,預設 6。"""
    web = _web()
    assert "CARD_PER_PAGE_DEFAULT = 6" in web
    assert "CARD_PER_PAGE_CHOICES = (6, 4, 3)" in web


def test_per_page_parser_clamps():
    """非法值要跌返 6(唔可以 raise)。"""
    web = _web()
    m = re.search(r"def _card_per_page\(raw\).*?\n\n\ndef ", web, re.S)
    assert m, "搵唔到 _card_per_page()"
    body = m.group(0)
    assert "except (TypeError, ValueError)" in body, "非法值唔可以 raise"
    assert "return v if v in CARD_PER_PAGE_CHOICES else CARD_PER_PAGE_DEFAULT" in body


def test_renderer_uses_per_page():
    """renderer 一定要用 per_page 切片。"""
    web = _web()
    assert "per_page: int = CARD_PER_PAGE_DEFAULT" in web, "renderer 要有 per_page 參數"
    assert "MEALS_PER_PAGE = per_page if per_page in CARD_PER_PAGE_CHOICES else CARD_PER_PAGE_DEFAULT" in web
    assert "page_start = (page_num - 1) * MEALS_PER_PAGE" in web


def test_card_endpoint_reads_per_page():
    """card endpoint 要由 query 讀 per_page。"""
    web = _web()
    assert '_card_per_page(request.args.get("per_page"))' in web


def test_card_url_reads_per_page():
    """card_url endpoint 一樣要讀 per_page。"""
    web = _web()
    assert web.count('_card_per_page(request.args.get("per_page"))') >= 2, (
        "card 同 card_url 都要讀 per_page"
    )


# ---------------------------------------------------------------------------
# 2. 兩個 endpoint 一致(最易出事嘅位)
# ---------------------------------------------------------------------------

def test_both_endpoints_use_same_page_total_formula():
    """兩個 endpoint 要用同一個 ceil-div 公式。"""
    web = _web()
    formula = "(total_meals + MEALS_PER_PAGE - 1) // MEALS_PER_PAGE"
    assert formula in web
    assert "(meal_count + MEALS_PER_PAGE - 1) // MEALS_PER_PAGE" in web, (
        "card_url 要用同一個公式,否則 page_total 會唔一致"
    )


def test_card_url_returns_per_page():
    """card_url 要回傳 per_page,方便前端核對。"""
    web = _web()
    assert '"per_page": MEALS_PER_PAGE' in web, "card_url response 要有 per_page"


# ---------------------------------------------------------------------------
# 3. 檔名 / cache key 唔可以撞
# ---------------------------------------------------------------------------

def test_pp_suffix_helper():
    """6 → ''(舊名),4/3 → '_pp4'/'_pp3'。"""
    web = _web()
    m = re.search(r"def _card_pp_suffix\(per_page: int\).*?\n\n\ndef ", web, re.S)
    assert m, "搵唔到 _card_pp_suffix()"
    assert '"" if per_page == CARD_PER_PAGE_DEFAULT else f"_pp{int(per_page)}"' in m.group(0)


def test_renderer_filename_has_pp_suffix():
    """renderer 輸出檔名要帶 pp suffix。"""
    web = _web()
    assert "_pp_suffix = _card_pp_suffix(MEALS_PER_PAGE)" in web
    assert "nutrition_{date_iso}{_c_suffix}{_pp_suffix}_p{page_num}.jpg" in web


def test_cache_key_has_pp_suffix():
    """cache key 要帶 pp suffix(唔同 per_page 唔可以共用)。"""
    web = _web()
    assert 'cache_key = f"{_ck}{_pp_sfx}" + ("_compact" if compact else "")' in web, (
        "cache key 要包括 per_page suffix"
    )


def test_card_url_filenames_have_pp_suffix():
    """card_url 回傳嘅檔名要帶 pp suffix。"""
    web = _web()
    assert 'f"nutrition_{target_date}{_pp_sfx}_p{p}.jpg"' in web


def test_default_keeps_legacy_filename():
    """per_page=6 一定要保持舊檔名(backwards compatible)。"""
    web = _web()
    # 舊 URL(/nutrition_card/nutrition_2026-10-27_p1.jpg)一定要仲 work
    assert 'f"nutrition_{target_date}{_pp_sfx}_p{p}.jpg"' in web
    assert "CARD_PER_PAGE_DEFAULT = 6" in web, "預設一定要係 6"
    # legacy 檔寫入條件:per_page=6 嗰陣先寫
    assert "and not compact" in web, "legacy 檔寫入條件(要留意 per_page)"


# ---------------------------------------------------------------------------
# 4. 前端
# ---------------------------------------------------------------------------

def test_frontend_has_per_page_state():
    tpl = _tpl()
    assert "perPage: 6" in tpl, "state 要有 perPage"


def test_frontend_sends_per_page_everywhere():
    """預覽 / 切頁 / 下載 三條路徑都要送 per_page。"""
    tpl = _tpl()
    assert tpl.count("per_page=") >= 3, (
        "per_page 要出現喺:card_url fetch、card fetch(預覽)、切頁 fetch、下載 fetch"
    )
    assert "&per_page=${_pp}" in tpl
    assert "per_page=${_pp}${_cq}" in tpl, "切頁/下載 fetch 要同時帶 per_page 同 compact"


def test_frontend_chip_row():
    """要有每頁餐數嘅選擇器。"""
    tpl = _tpl()
    assert "每頁餐數" in tpl, "要有「每頁餐數」標籤"
    assert 'x-for="opt in [6, 4, 3]"' in tpl, "要有 6/4/3 三個選項"
    assert "@click=\"foodCardPreview.perPage = opt; reloadFoodCardPreview()\"" in tpl, (
        "撳 chip 要即時重新載入"
    )


def test_frontend_pp_suffix_helper():
    """前端 suffix helper 要同 server 一致。"""
    tpl = _tpl()
    m = re.search(r"_cardPpSuffix\(pp\)\s*\{(.*?)\n    \},", tpl, re.S)
    assert m, "搵唔到 _cardPpSuffix()"
    assert "'_pp' + v" in m.group(1)
    assert "v === 4 || v === 3" in m.group(1), "只 4 同 3 要有 suffix(6 保持舊名)"


def test_frontend_preview_url_has_pp_suffix():
    """預覽 URL 要指向 pp 檔名。"""
    tpl = _tpl()
    assert "`nutrition_${date}${_cSfx}${_ppSfx}_p1.jpg`" in tpl
    assert "`nutrition_${date}${_cSfx}${_ppSfx}.jpg`" in tpl


def test_frontend_download_filename_has_pp_suffix():
    """下載檔名要帶 pp suffix。"""
    tpl = _tpl()
    assert "`gymbro_foodlog_${p.date}${_cSfx}${_ppSfx}_p${target}.jpg`" in tpl
    assert "`gymbro_foodlog_${p.date}${_cSfx}${_ppSfx}.jpg`" in tpl


def test_frontend_fallback_url_has_pp_suffix():
    """fallback URL 都要跟。"""
    tpl = _tpl()
    assert "_cardPpSuffix(foodCardPreview.perPage)" in tpl


def test_frontend_preserves_per_page_across_reopen():
    """關閉再打開要保留選擇。"""
    tpl = _tpl()
    assert "_keepPP" in tpl, "重新開啟要保留 perPage"
    assert "perPage: this.foodCardPreview.perPage || 6" in tpl


def test_no_stale_per_page_omission():
    """唔准有 fetch 食緊 date 但漏咗 per_page(會拿到唔同分頁)。

    per_page 可以用字面 `per_page=` 或者變數 `${_ppQ}` / `${_pp}`
    (preview fetch 用 `_ppQ`,切頁/下載用 `_pp` + literal)。
    """
    tpl = _strip_comments(_tpl())
    found = 0
    for m in re.finditer(r"fetch\(\s*`/api/nutrition/card\?([^`]+)`", tpl):
        q = m.group(1)
        found += 1
        assert ("per_page" in q) or ("_ppQ" in q) or ("_pp" in q), (
            f"呢個 card fetch 漏咗 per_page: {q}"
        )
    assert found >= 3, f"應該有至少 3 個 card fetch(preview/切頁/下載),搵到 {found}"


def test_ppq_variable_is_built_correctly():
    """`_ppQ` 要真係 `&per_page=<值>`。"""
    tpl = _tpl()
    assert "const _ppQ = `&per_page=${_pp}`;" in tpl, (
        "_ppQ 要係 &per_page=<值>"
    )


# ---------------------------------------------------------------------------
# 5. 版本
# ---------------------------------------------------------------------------

def test_version_bumped():
    web = _web()
    assert '__version__ = "3.3.55"' in web
    assert "gym-web-v159" in web
