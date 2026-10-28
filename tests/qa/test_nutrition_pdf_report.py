"""v3.3.62 — 營養日結由「逐頁 JPG」改成「單一 PDF」。

Jim OOB 2026-10-27:
  「As we use pdf. Just one click download pdf and allow me to close the share
   such that I can be back to gymbro interface after download.」
  「I think you change gymbro jpg download to pdf download. An overhaul.
   Make the pdf more informative as we have more spaces now.」
  「For food description and verdict, don't just comment the percentage.
   The ai visual info has more useful info. Don't drop it down the road」
"""
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(ROOT, "gym_web.py")
TPL = os.path.join(ROOT, "templates", "index.html")
BASE = "http://127.0.0.1:7000"


def _src(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ─────────────────────────────────────────────────────────────
# 1. 後端 endpoint
# ─────────────────────────────────────────────────────────────
def test_pdf_endpoint_exists():
    src = _src(WEB)
    assert '@app.route("/api/nutrition/pdf", methods=["GET"])' in src
    assert '@app.route("/api/nutrition/pdf_info", methods=["GET"])' in src
    assert '@app.route("/nutrition_pdf/<filename>", methods=["GET"])' in src


def test_pdf_endpoint_sends_attachment():
    """落檔一定要用 Content-Disposition: attachment —— 唔可以令 iOS 開新頁面。"""
    src = _src(WEB)
    assert 'mimetype="application/pdf"' in src
    assert 'Content-Disposition' in src
    assert 'attachment; filename=' in src


def test_pdf_download_has_no_target_blank():
    """v3.3.61 教訓:target="_blank" 令 iOS 開新頁蓋住 PWA,撳唔到返。"""
    tpl = _src(TPL)
    assert "target=\"_blank\"" not in tpl.split("foodCardPdfUrl")[1][:2000]


def test_pdf_cache_dir_and_lru():
    src = _src(WEB)
    assert "PDF_CACHE_DIR" in src
    assert "PDF_CACHE_MAX" in src
    assert "OrderedDict" in src


def test_pdf_cache_invalidated_on_edit():
    """改咗餐名 / 刪咗餐 → 當日 PDF 一定要失效(唔可以攞舊檔)。"""
    src = _src(WEB)
    assert "_pdf_cache_invalidate" in src
    n = src.count("_pdf_cache_invalidate(")
    # 定義 + 至少 6 個編輯/刪除位
    assert n >= 7, f"失效點太少: {n}"


def test_ai_profile_persistence_layer():
    """AI 描述/判詞一定要落地(唔可以淨係喺 request 期間存在)。"""
    p = os.path.join(ROOT, "gym_web", "ai_profile.py")
    assert os.path.exists(p)
    src = _src(p)
    assert "nutrition_desc_cache" in src
    assert "def put_profile" in src
    assert "def ensure_profiles" in src
    assert "def get_profile" in src


def test_pdf_report_module():
    p = os.path.join(ROOT, "gym_web", "pdf_report.py")
    assert os.path.exists(p)
    src = _src(p)
    assert "def render_nutrition_pdf" in src
    assert "reportlab" in src
    # 繁中字型
    assert "wqy" in src.lower() or "zenhei" in src.lower()


def test_scan_persists_ai_description():
    """掃描 commit 之後一定要寫低 AI 視覺描述(下游 PDF 靠佢)。"""
    src = _src(WEB)
    assert "ai_profile" in src
    assert "put_profile" in src
    assert "vision_raw_desc" in src or "vision_desc" in src


# ─────────────────────────────────────────────────────────────
# 2. 前端:單一下載掣
# ─────────────────────────────────────────────────────────────
def test_single_pdf_download_anchor():
    tpl = _src(TPL)
    assert 'data-testid="food-card-pdf-download"' in tpl
    assert '⬇ 下載 PDF' in tpl
    # 一個 PDF 掣就夠
    assert tpl.count('data-testid="food-card-pdf-download"') == 1


def test_old_per_page_jpg_buttons_removed():
    """逐頁 JPG 下載掣要完全收埋(而家得一粒 PDF 掣)。"""
    tpl = _src(TPL)
    assert "foodCardPageUrl" not in tpl
    assert "switchFoodCardPage" not in tpl
    assert "prewarmFoodCardPages" not in tpl
    assert "onFoodCardDownloadClick" not in tpl
    assert "foodCardDlStyle" not in tpl


def test_pdf_helpers_exist():
    tpl = _src(TPL)
    assert "foodCardPdfUrl()" in tpl
    assert "foodCardPdfFileName()" in tpl
    assert "onFoodCardPdfDownload()" in tpl
    assert "refreshFoodCardPdfInfo(" in tpl


def test_pdf_button_above_preview():
    """短屏(iPhone Duo)都要見到下載掣 → 掣喺 preview 上面。"""
    tpl = _src(TPL)
    i_btn = tpl.find('data-testid="food-card-pdf-download"')
    # ⚠️ v3.3.62:modal 搬咗去 <body> 直接子元素(修 stacking context),
    #    所以 JS(喺 script 入面,行數早過 markup)會出現
    #    `foodCardPreview.pdfPreviewUrl` 字樣 → 要搵 <img> 嘅 :src,
    #    唔係搵變數名。
    i_prev = tpl.find(':src="foodCardPreview.pdfPreviewUrl"')
    assert i_btn > 0 and i_prev > 0
    assert i_btn < i_prev, "PDF 下載掣一定要喺 preview 之前"


def test_auto_close_after_download():
    """落完自動閂 modal → Jim 撳「關閉」iOS share sheet 就返返主介面。"""
    tpl = _src(TPL)
    assert "_pdfCloseTimer" in tpl
    assert "closeFoodCardPreview()" in tpl


def test_close_button_visible_text():
    tpl = _src(TPL)
    assert "✕ 關閉" in tpl


def test_pdf_hint_mentions_close():
    tpl = _src(TPL)
    assert "落完撳右上角「關閉」返去" in tpl


def test_pdf_info_polling():
    """未 ready 要輪詢(第一次生成要 AI 分析)。"""
    tpl = _src(TPL)
    assert "_pdfPollTimer" in tpl
    assert "pdf_info" in tpl
    assert "&warm=1" in tpl


# ─────────────────────────────────────────────────────────────
# 3. 實機 API(需要 server 行緊)
# ─────────────────────────────────────────────────────────────
def _curl(path):
    return subprocess.run(
        ["curl", "-s", "-m", "120", BASE + path],
        capture_output=True, text=True).stdout


def test_live_pdf_info_endpoint():
    import json
    out = _curl("/api/nutrition/pdf_info?date=2026-10-27")
    if not out.strip():
        pytest.skip("server 未行")
    d = json.loads(out)
    assert d["ok"] is True
    assert d["meal_count"] >= 1
    assert "totals" in d
    assert "kcal" in d["totals"]


def test_live_pdf_download_headers():
    out = subprocess.run(
        ["curl", "-s", "-m", "180", "-D", "-", "-o", "/dev/null",
         BASE + "/api/nutrition/pdf?date=2026-10-27"],
        capture_output=True, text=True).stdout
    if not out.strip():
        pytest.skip("server 未行")
    low = out.lower()
    assert "content-type: application/pdf" in low
    assert "content-disposition: attachment" in low
    assert "gymbro_foodlog_2026-10-27.pdf" in out


# ─────────────────────────────────────────────────────────────
# 4. 版本 pin
# ─────────────────────────────────────────────────────────────
def test_version_pins():
    web = _src(WEB)
    assert '__version__ = "3.3.62"' in web
    assert "gym-web-v166" in web
