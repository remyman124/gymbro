"""QA — v3.3.54: 日結 JPG「逐頁下載」(每頁一個掣,唔再一次過載多張)。

Jim OOB 2026-10-27:
  「Don't download multiple image at the same time. Show multiple download
    button to download. Remove second button」

**點解要改(唔係口味問題)**
舊做法:一個「⬇ 下載全部 N 頁」掣 → JS loop 連續發 N 個 `a.click()`。
實測問題:
  * iOS Safari 會當成「多檔下載」→ 用戶要逐個撳「允許」,N 頁就撳 N 次。
  * 撞瀏覽器同時下載限制 → 後半截 request 唔會落地。
  * 檔名 `gymbro_foodlog_<date>_p1.jpg` … `_pN.jpg` 一次過彈出,
    iOS 相簿入面完全唔知邊張係邊張。

新做法:**每頁一個下載掣,撳邊個就只落嗰一張**。
順帶移除第二個「✕ 關閉」掣(右上角 ✕ / Esc / 點背景一樣可以閂)。

呢個 suite 守住:
  1. 每頁一個掣(x-for 逐頁渲染)。
  2. **唔准**再有「一次過載晒」嘅 loop / 按鈕 / 死代碼。
  3. 下載 function 一次只發一個 request。
  4. 第二個按鈕(關閉)真係冇咗,但右上角 ✕ 仲喺度(唔可以變成閂唔到)。
  5. 矮身版要同逐頁下載夾埋一齊(切頁都要帶 compact)。
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
    """抽掉 HTML 註解同 JS 行註解,避免註解入面嘅字串污染斷言。

    ⚠️ 唔可以用 `/* .*? */`(DOTALL)去抽 CSS/JS block 註解 —— template 裡面
    有幾個 CSS block 註解開頭但喺好後面先閉合,非貪心 regex 會由第一個 `/*`
    一路食到後面嘅 `*/`,中間成個 modal  markup 都畀人列走,斷言就會離譜咁失敗。
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)   # HTML 註解
    text = re.sub(r"(?m)^\s*//.*$", "", text)            # JS 行註解
    return text


# ---------------------------------------------------------------------------
# 1. 每頁一個下載掣
# ---------------------------------------------------------------------------

def test_page_buttons_use_x_for():
    """用 x-for 逐頁渲染下載掣。"""
    tpl = _tpl()
    assert 'x-for="pn in (foodCardPreview.pageTotal || 1)"' in tpl, (
        "下載掣要用 x-for 逐頁渲染"
    )
    assert ":key=\"'dl' + pn\"" in tpl, "x-for 要有 :key(Alpine 必需)"


def test_each_page_button_calls_download_page():
    """每個掣都要叫 downloadFoodCardPage(pn)。"""
    tpl = _tpl()
    assert '@click="downloadFoodCardPage(pn)"' in tpl, (
        "下載掣要叫 downloadFoodCardPage(pn)"
    )


def test_page_button_label_shows_page_number():
    """多頁日嘅掣要顯示頁碼。"""
    tpl = _tpl()
    assert "x-text=\"'⬇ 第 ' + pn + ' 頁'\"" in tpl, "多頁掣要顯示頁碼"
    assert "⬇ 下載 JPG" in tpl, "單頁日仍然叫「⬇ 下載 JPG」"


# ---------------------------------------------------------------------------
# 2. 唔准再有「一次過載晒」
# ---------------------------------------------------------------------------

def test_no_download_all_button():
    """唔准再有「下載全部」按鈕。"""
    tpl = _strip_comments(_tpl())
    assert "下載全部" not in tpl, "「下載全部」掣已經唔准存在"
    assert "下載全部" not in _web(), "server 都唔准提「下載全部」"


def test_no_multi_download_loop():
    """唔准再有 for-loop 連續落多頁。"""
    tpl = _strip_comments(_tpl())
    assert not re.search(r"for\s*\(\s*let\s+\w+\s*=\s*1\s*;\s*\w+\s*<=\s*pageTotal", tpl), (
        "唔准有「一次過 loop 晒所有頁」嘅 code"
    )
    assert "開始下載" not in tpl, "唔准再提示「開始下載 N 頁」"


def test_no_legacy_dead_code():
    """唔准留低 legacy 死代碼。"""
    tpl = _tpl()
    assert "_downloadFoodCardFromModalLegacy" not in tpl, (
        "唔准留 legacy 死代碼(嗰段就係「一次過載多張」嘅邏輯)"
    )


# ---------------------------------------------------------------------------
# 3. 一次只發一個 request
# ---------------------------------------------------------------------------

def test_download_page_fetches_single_page():
    """downloadFoodCardPage 只 fetch 一頁。"""
    tpl = _tpl()
    m = re.search(r"async downloadFoodCardPage\(pageNum\)\s*\{(.*?)\n    \},", tpl, re.S)
    assert m, "搵唔到 downloadFoodCardPage()"
    body = m.group(1)
    fetches = re.findall(r"await fetch\(", body)
    assert len(fetches) == 1, f"downloadFoodCardPage 只准 fetch 一次,搵到 {len(fetches)} 次"
    assert "&page=${target}" in body, "要明確帶 page=target"


def test_download_page_clamps_target():
    """要 clamp 頁碼,唔可以越界。"""
    tpl = _tpl()
    m = re.search(r"async downloadFoodCardPage\(pageNum\)\s*\{(.*?)\n    \},", tpl, re.S)
    body = m.group(1)
    assert "Math.min" in body and "Math.max" in body, "頁碼要 clamp 喺 1..pageTotal"


def test_download_page_has_reentrancy_guard():
    """防連撳 / 防重複下載。"""
    tpl = _tpl()
    m = re.search(r"async downloadFoodCardPage\(pageNum\)\s*\{(.*?)\n    \},", tpl, re.S)
    body = m.group(1)
    assert "if (p.downloading) return;" in body, "要有防重入 guard"
    assert "p.downloading = true;" in body and "p.downloading = false;" in body, (
        "downloading flag 要成對 set"
    )
    assert "downloading: false" in tpl, "初始 state 要有 downloading: false"


def test_download_filename_per_page():
    """下載檔名要帶頁碼(多頁日)。"""
    tpl = _tpl()
    m = re.search(r"async downloadFoodCardPage\(pageNum\)\s*\{(.*?)\n    \},", tpl, re.S)
    body = m.group(1)
    # v3.3.55: 檔名加咗每頁餐數 suffix `${_ppSfx}`
    assert "gymbro_foodlog_${p.date}${_cSfx}${_ppSfx}_p${target}.jpg" in body, (
        "多頁下載檔名要帶 _p<頁碼>"
    )
    assert "gymbro_foodlog_${p.date}${_cSfx}${_ppSfx}.jpg" in body, "單頁下載檔名唔使帶頁碼"


def test_modal_wrapper_downloads_current_page_only():
    """downloadFoodCardFromModal() 而家只落當前頁。"""
    tpl = _tpl()
    m = re.search(r"async downloadFoodCardFromModal\(\)\s*\{(.*?)\n    \},", tpl, re.S)
    assert m, "搵唔到 downloadFoodCardFromModal()"
    body = m.group(1)
    assert "downloadFoodCardPage" in body, "應該 delegate 去 downloadFoodCardPage"
    assert "for" not in body, "唔准有 loop"


# ---------------------------------------------------------------------------
# 4. 第二個按鈕(關閉)移除,但要仲閂得到
# ---------------------------------------------------------------------------

def test_footer_close_button_removed():
    """footer 個「✕ 關閉」掣要冇咗。"""
    tpl = _strip_comments(_tpl())
    assert "✕ 關閉" not in tpl, "footer 嘅「✕ 關閉」掣已經移除"
    # 淨係剩低 header 個 ✕(@click.self / @keydown.escape 係唔同 attribute 名)
    raw = _tpl()
    assert raw.count('@click="closeFoodCardPreview()"') == 1, (
        "只應該剩低一個 @click=\"closeFoodCardPreview()\"(header 個 ✕)"
    )


def test_header_close_button_still_exists():
    """右上角 ✕ 一定要仲喺度(唔可以變成閂唔到)。"""
    tpl = _tpl()
    assert 'data-testid="food-card-close"' in tpl, "header 個 ✕ 要保留"
    assert '@click="closeFoodCardPreview()"' in tpl, "header ✕ 要仍然叫 closeFoodCardPreview"


def test_escape_and_backdrop_still_close():
    """Esc + 點背景一樣可以閂。"""
    tpl = _tpl()
    assert '@keydown.escape.window="closeFoodCardPreview()"' in tpl, "Esc 要仍然可以閂"
    assert '@click.self="closeFoodCardPreview()"' in tpl, "點背景要仍然可以閂"


# ---------------------------------------------------------------------------
# 5. 矮身版 × 逐頁下載
# ---------------------------------------------------------------------------

def test_switch_page_respects_compact():
    """切頁都要帶 compact(否則會跳返原尺寸)。"""
    tpl = _tpl()
    m = re.search(r"async switchFoodCardPage\(delta\)\s*\{(.*?)\n    \},", tpl, re.S)
    assert m, "搵唔到 switchFoodCardPage()"
    body = m.group(1)
    assert "&compact=1" in body, "切頁 fetch 要帶 compact=1"
    assert "_cSfx" in body, "切頁嘅檔名要用 _cSfx"


def test_download_page_respects_compact():
    """逐頁下載都要尊重矮身版 toggle。"""
    tpl = _tpl()
    m = re.search(r"async downloadFoodCardPage\(pageNum\)\s*\{(.*?)\n    \},", tpl, re.S)
    body = m.group(1)
    assert "&compact=1" in body, "下載 fetch 要帶 compact=1"
    assert "_cSfx" in body, "下載檔名要用 _cSfx"


def test_page_button_style_helper_exists():
    """每頁掣嘅樣式 helper(目前頁高亮)。"""
    tpl = _tpl()
    assert "foodCardDlStyle(pn)" in tpl, "要有 foodCardDlStyle() helper"
    assert ':style="foodCardDlStyle(pn)"' in tpl, "掣要綁 foodCardDlStyle()"
    m = re.search(r"foodCardDlStyle\(pn\)\s*\{(.*?)\n    \},", tpl, re.S)
    assert m and "pageNum === pn" in m.group(1), "目前頁要高亮"


# ---------------------------------------------------------------------------
# 6. Server: 下載檔名格式
# ---------------------------------------------------------------------------

def test_server_download_name_format():
    """server 端矮身版檔名格式要同 preview 一致(_compact_p<N>)。"""
    web = _web()
    assert "gymbro_foodlog_{target_date}" in web, "server 檔名格式"
    assert "'_compact' if compact else ''" in web, "server 要按 compact 加 suffix"
    # 舊格式 _p1_compact 已廢除
    assert '_dn.replace(".jpg", "_compact.jpg")' not in web, (
        "舊格式(_p1_compact)已廢除,要用 _compact_p<N>"
    )


def test_version_and_cache_bumped():
    """版本 + SW cache 要 bump。"""
    web = _web()
    assert '__version__ = "3.3.59"' in web, "版本要係 3.3.57"
    assert "gym-web-v163" in web, "SW cache 要 bump 去做 v161"
