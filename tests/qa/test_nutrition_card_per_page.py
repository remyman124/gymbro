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


def test_frontend_sends_per_page_everywhere():
    """預覽 / 切頁 / 下載 三條路徑都要送 per_page。"""
    tpl = _tpl()
    assert tpl.count("per_page=") >= 3, (
        "per_page 要出現喺:card_url fetch、card fetch(預覽)、切頁 fetch、下載 fetch"
    )
    assert "&per_page=${_pp}" in tpl
    assert "per_page=${_pp}${_cq}" in tpl, "切頁/下載 fetch 要同時帶 per_page 同 compact"


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
    tpl = _strip_comments(_tpl())
    assert 'h-[90dvh]' in tpl, "modal 要用 h-[90dvh](確定高度,flexbox 先分配到剩餘空間)"
    assert "flex-1 min-h-0" in tpl, "preview 要 min-h-0(可以喺 footer 變高時縮細)"
    assert "calc(90dvh - 56px - 76px)" not in tpl, (
        "舊嘅錯 max-height 仲喺度 —— 個 76px 假設係短屏裁走下載掣嘅元凶"
    )


def test_footer_rows_cannot_be_shrunk():
    """header / footer 每行都要 flex-shrink-0,否則 flexbox 會壓扁佢哋。

    用 `rfind('<div', 0, idx)` 抽返每行嘅**開頭 div tag** —— 唔可以用
    `tpl.find(marker)` 嘅前 400 字窗口:同一個 marker 可能喺更早嘅錯誤
    提示 block 出現過(例如 `x-show="foodCardPreview.pageTotal > 1"` 喺
    「JPG 唔見咗」嗰個錯誤提示都出現),窗口就會搵錯 div。
    """
    tpl = _strip_comments(_tpl())
    # 直接比對每行嘅開頭 class(每個都係 modal 入面嘅唯一 signature)
    # v3.3.59:矮身版 toggle row 已移除(4 行,唔係 5 行)
    for sig, label in (
        ("flex items-center justify-between px-4 flex-shrink-0", "header"),
        ("flex items-center justify-between gap-2 px-4 py-2 flex-shrink-0", "頁碼導航 row"),
        ("px-4 pt-2 pb-1 text-[10px] font-bold flex-shrink-0", "說明 row"),
        ("px-4 pb-2 flex flex-wrap gap-2 flex-shrink-0", "下載掣 row"),
    ):
        assert sig in tpl, f"搵唔到 {label} 嘅開頭 div(class signature 唔啱): {sig}"
        assert "flex-shrink-0" in sig, f"{label} 唔係 flex-shrink-0"

    # modal 內最少要有 4 個 flex-shrink-0(header + 頁碼 + 說明 + 下載掣)
    m = re.search(r'x-show="foodCardPreview\.visible".*?\n      </div>\n    </div>', tpl, re.S)
    assert m, "搵唔到食物日結 modal block"
    assert m.group(0).count("flex-shrink-0") >= 4, (
        f"modal 內最少要 4 個 flex-shrink-0,而家只有 "
        f"{m.group(0).count('flex-shrink-0')}"
    )


def test_download_buttons_are_above_the_preview():
    """v3.3.59:所有操作掣(下載 + 頁碼導航)一定要喺**預覽圖之前**。

    Jim OOB「Still too long ... such that I can see the button download button」
    —— Playwright 量到喺 375x667 / 320x568 掣 bottom(621 / 527)其實喺
    viewport 之內,即係話 Jim 實際部機嘅可見 app 視窗更矮(iOS 底條 /
    home indicator / Slide Over 食高度)。
    → 掣擺喺底部唔保證見到;搬到 modal 最上面就**保證**見到。
    """
    tpl = _strip_comments(_tpl())
    i_dl = tpl.find('x-for="pn in (foodCardPreview.pageTotal || 1)"')
    i_nav = tpl.find('x-show="foodCardPreview.pageTotal > 1"')
    i_prev = tpl.find("flex-1 min-h-0 overflow-y-auto")
    assert i_dl != -1 and i_nav != -1 and i_prev != -1, "搵唔到相關 block"
    assert i_dl < i_prev, "下載掣一定要喺預覽圖之前"
    assert i_nav < i_prev, "頁碼導航一定要喺預覽圖之前"
    # 而且 preview 一定係**最後**(搣之後)
    assert i_prev > max(i_dl, i_nav)


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
    assert "perPage: this.foodCardPreview.perPage || 5" in tpl


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
# 4b. 全部頁面嘅下載掣都要顯示(v3.3.57 Jim OOB)
# ---------------------------------------------------------------------------

def test_all_page_buttons_rendered():
    """下載掣要 x-for 晒 pageTotal —— 全部頁面都見到,唔會藏喺第 2 頁。"""
    tpl = _tpl()
    assert 'x-for="pn in (foodCardPreview.pageTotal || 1)"' in tpl, (
        "下載掣一定要按 pageTotal 全部 render"
    )
    assert "downloadFoodCardPage(pn)" in tpl, "每個掣要對應自己嗰頁"


def test_download_row_has_hint():
    """要有文字講明「全部 N 頁,撳一個落一張」。"""
    tpl = _tpl()
    assert "逐頁下載" in tpl
    assert "撳一個落一張" in tpl, "要講明係逐個下載,唔係一次過"


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
    assert '__version__ = "3.3.59"' in web
    assert "gym-web-v163" in web
