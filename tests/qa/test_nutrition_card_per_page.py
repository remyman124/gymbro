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
    """v3.3.57:上限 5 餐 —— 選項 5/4/3,預設 5。

    6 唔再係選項,但仍然喺 ALLOWED 入面(要繼續支援 v3.3.55 之前
    6 餐/頁 嗰啲無 suffix 嘅 legacy 檔名)。
    """
    web = _web()
    assert "CARD_PER_PAGE_DEFAULT = 5" in web
    assert "CARD_PER_PAGE_CHOICES = (5, 4, 3)" in web
    assert "CARD_PER_PAGE_LEGACY = 6" in web
    assert "CARD_PER_PAGE_ALLOWED = (6, 5, 4, 3)" in web


def test_per_page_max_is_five():
    """Jim OOB 2026-10-27「Make it 5 meals max per screen」—— 任何選項都唔准 >5。"""
    web = _web()
    import re as _re
    m = _re.search(r"CARD_PER_PAGE_CHOICES = \(([^)]*)\)", web)
    assert m, "搵唔到 CARD_PER_PAGE_CHOICES"
    vals = [int(v.strip()) for v in m.group(1).split(",") if v.strip()]
    assert max(vals) == 5, f"每頁上限一定要係 5,而家係 {max(vals)}"
    assert 6 not in vals, "6 唔再可以係 UI 選項(只可以留喺 ALLOWED 做 legacy)"


def test_per_page_parser_clamps():
    """非法值要跌返 6(唔可以 raise)。"""
    web = _web()
    m = re.search(r"def _card_per_page\(raw\).*?\n\n\ndef ", web, re.S)
    assert m, "搵唔到 _card_per_page()"
    body = m.group(0)
    assert "except (TypeError, ValueError)" in body, "非法值唔可以 raise"
    assert "return v if v in CARD_PER_PAGE_ALLOWED else CARD_PER_PAGE_DEFAULT" in body


def test_renderer_uses_per_page():
    """renderer 一定要用 per_page 切片。"""
    web = _web()
    assert "per_page: int = CARD_PER_PAGE_DEFAULT" in web, "renderer 要有 per_page 參數"
    assert "MEALS_PER_PAGE = per_page if per_page in CARD_PER_PAGE_ALLOWED else CARD_PER_PAGE_DEFAULT" in web
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
    # v3.3.57:只有 legacy(6 餐/頁)用無 suffix 檔名;5/4/3 一律帶 suffix
    assert '"" if int(per_page) == CARD_PER_PAGE_LEGACY else f"_pp{int(per_page)}"' in m.group(0)


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
    """per_page=6(legacy)一定要保持舊檔名(backwards compatible)。"""
    web = _web()
    assert 'f"nutrition_{target_date}{_pp_sfx}_p{p}.jpg"' in web
    assert "CARD_PER_PAGE_LEGACY = 6" in web, "legacy 值一定要係 6"
    assert "CARD_PER_PAGE_DEFAULT = 5" in web, "新預設一定要係 5"
    assert "and not compact" in web, "legacy 檔寫入條件(要留意 per_page)"


def test_new_default_does_not_collide_with_legacy():
    """新預設(5 餐)唔可以撞 legacy(6 餐)嘅檔名 —— suffix 唔可以空。"""
    import re as _re
    web = _web()
    m = _re.search(r"def _card_pp_suffix\(per_page: int\).*?\n\n\ndef ", web, _re.S)
    assert m, "搵唔到 _card_pp_suffix()"
    body = m.group(0)
    assert "CARD_PER_PAGE_LEGACY" in body
    assert "CARD_PER_PAGE_DEFAULT" not in body, (
        "suffix 唔可以跟 default(預設改成 5 之後會撞 legacy 6 餐嘅檔名)"
    )


# ---------------------------------------------------------------------------
# 4. 前端
# ---------------------------------------------------------------------------

def test_frontend_has_per_page_state():
    tpl = _tpl()
    assert "perPage: 5" in tpl, "state 要有 perPage(預設 5)"




def test_page_size_buttons_removed():
    """v3.3.58 (Jim OOB「Remove page size buttons」):每頁餐數 chip 要完全移除。

    每頁固定 5 餐(v3.3.57 嘅「Make it 5 meals max per screen」),
    所以前端唔應該再畀用戶揀餐數。
    """
    tpl = _strip_comments(_tpl())
    assert "每頁餐數" not in tpl, "仲有「每頁餐數」標籤(應該移除)"
    assert 'x-for="opt in [5, 4, 3]"' not in tpl, "仲有 5/4/3 chip row(應該移除)"
    assert "foodCardPreview.perPage = opt" not in tpl, "仲有改 perPage 嘅 chip handler"
    # 但 state 仍然要存在(用嚟砌檔名),而且固定 5
    assert "perPage: 5" in tpl, "state 要保留 perPage(用嚟砌 _pp5 檔名)"


def test_modal_uses_definite_height_for_short_screens():
    """v3.3.58 (Jim OOB「My screen height is very short. Can't see the download
    buttons」):modal 要用**確定高度** h-[90dvh],preview 用 flex-1 min-h-0。

    舊做法 `max-h-[90dvh]` + preview 寫死 `max-height: calc(90dvh - 56px - 76px)`
    → 個 76px 假設 footer 只有一行,但實際 footer ≈ 114px(加埋 chip row 更到
    ~166px)→ 短屏(iPhone SE 667pt)時 56 + preview + 114 > 90dvh(600)
    → modal 撞上限 + overflow:hidden → **下載掣被裁走**,Jim 撳唔到。
    """
    # v3.3.64 (Jim OOB「No viewing. Just immediate share」):modal 冇預覽區
    # → 個 modal 本身好矮(≈160px)→ **唔使** h-[90dvh]。
    # (用 90dvh 反而會變成一個大大個空 modal,掣飄喺中間)
    tpl = _strip_comments(_tpl())
    assert 'h-[90dvh]' not in tpl, "冇預覽區,唔使再鎖死 90dvh"
    assert "flex-1 min-h-0" not in tpl, "冇 preview flex item"
    assert "calc(90dvh - 56px - 76px)" not in tpl, (
        "舊嘅錯 max-height 仲喺度 —— 個 76px 假設係短屏裁走下載掣嘅元凶"
    )
    # 兩粒分享掣一定要喺 modal 入面
    assert 'data-testid="food-card-jpg-share"' in tpl
    assert 'data-testid="food-card-pdf-share"' in tpl






def test_compact_toggle_row_removed_from_modal():
    """v3.3.59:「檔案細啲」toggle row 由 modal 移除。

    Jim OOB「Remove the toggle row ... such that I can see the button
    download button」—— 佢喺底部佔 ~42pt,真機短屏會將下載掣推落視窗外。
    """
    tpl = _strip_comments(_tpl())
    assert "檔案細啲" not in tpl, "仲有「檔案細啲」toggle row(應該移除)"
    assert "foodCardPreview.compact = !foodCardPreview.compact" not in tpl, (
        "仲有 toggle handler(應該移除)"
    )
    # 但後端仍然支援 compact=1(舊 URL 唔會壞)
    web = _web()
    assert "compact" in web, "後端仲要支援 compact"


def test_frontend_pp_suffix_helper():
    """前端 suffix helper 要同 server 一致。"""
    tpl = _tpl()
    m = re.search(r"_cardPpSuffix\(pp\)\s*\{(.*?)\n    \},", tpl, re.S)
    assert m, "搵唔到 _cardPpSuffix()"
    assert "'_pp' + v" in m.group(1)
    assert "v === 5 || v === 4 || v === 3" in m.group(1), (
        "5/4/3 要有 suffix(6 先係 legacy 舊名)"
    )
















# ---------------------------------------------------------------------------
# 4b. 全部頁面嘅下載掣都要顯示(v3.3.57 Jim OOB)
# ---------------------------------------------------------------------------





def test_no_bulk_download_left():
    """唔准有「一次過落晒」嘅 bulk 下載路徑。

    ⚠️ 要先剝走註解 —— 註解入面會提到舊做法「⬇ 下載全部 N 頁」(用嚟
    解釋點解改成逐頁),唔係實際 code。
    """
    tpl = _strip_comments(_tpl())
    for bad in ("下載全部", "downloadAllFoodCardPages", "_downloadFoodCardFromModalLegacy"):
        assert bad not in tpl, f"仲有殘留嘅 bulk 下載路徑: {bad}"


# ---------------------------------------------------------------------------
# 5. PWA install banner 唔可以遮住 modal(真 bug,2026-10-27 實測)
# ---------------------------------------------------------------------------

def test_foodcard_modal_above_pwa_banner():
    """PWA install banner 唔可以蓋住 modal 底部嗰行掣。

    實測:banner 係 `position:fixed; bottom:80px; z-index:9999`,
    modal 原本係 `z-50` → banner 蓋住「每頁餐數 chip / 檔案細啲 /
    ⬇ 下載掣」嗰行,真機上撳落去完全冇反應(elementFromPoint 實測
    撳落去係 banner 而唔係掣)。
    """
    tpl = _tpl()
    m = re.search(r'x-show="foodCardPreview\.visible".*?style="([^"]*)"', tpl, re.S)
    assert m, "搵唔到食物日結 modal"
    zm = re.search(r"z-index:\s*(\d+)", m.group(1))
    assert zm, f"modal 要用 inline z-index(唔好靠 Tailwind class): {m.group(1)}"
    modal_z = int(zm.group(1))
    assert modal_z >= 10000, f"modal z-index 要 >= 10000(高過 banner),而家係 {modal_z}"


def test_pwa_banner_below_modals():
    """banner z-index 要低過 modal。"""
    tpl = _tpl()
    m = re.search(r"banner\.style\.cssText = '[^']*z-index:(\d+)", tpl)
    assert m, "搵唔到 banner z-index"
    banner_z = int(m.group(1))
    assert banner_z < 10000, f"banner z-index 要低過 modal(10000),而家係 {banner_z}"


def test_modal_opens_hides_banner():
    """打開 modal 時要收埋 banner(唔好喺半透明 overlay 底下若隱若現)。"""
    tpl = _tpl()
    assert "document.getElementById('pwa-install-banner')" in tpl, (
        "showFoodCardPreview 要收埋 PWA banner"
    )


# ---------------------------------------------------------------------------
# 6. 版本
# ---------------------------------------------------------------------------

def test_version_bumped():
    web = _web()
    assert '__version__ = "3.3.68"' in web
    assert "gym-web-v172" in web
