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
    assert '__version__ = "3.3.63"' in web
    assert "gym-web-v167" in web


# ─────────────────────────────────────────────────────────────
# 5. PDF 版面:文字唔准重疊(Jim OOB 2026-10-28「Some text in pdf is
#    overlapping with each other.」)
# ─────────────────────────────────────────────────────────────
def _bbox_overlaps(pdf_path):
    """用 pdftotext -bbox 逐頁比對 → 回傳重疊字組數 + 出界字組數。"""
    import subprocess
    import tempfile
    xml = tempfile.mktemp(suffix=".xml")
    try:
        subprocess.run(["pdftotext", "-bbox", pdf_path, xml], check=True,
                       capture_output=True)
        with open(xml, encoding="utf-8") as f:
            x = f.read()
    finally:
        if os.path.exists(xml):
            os.unlink(xml)

    import re
    pages = re.findall(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>',
                       x, re.S)

    def ov(a, b):
        ix = min(a[2], b[2]) - max(a[0], b[0])
        iy = min(a[3], b[3]) - max(a[1], b[1])
        if ix <= 0 or iy <= 0:
            return 0
        return (ix * iy) / min((a[2] - a[0]) * (a[3] - a[1]),
                               (b[2] - b[0]) * (b[3] - b[1]))

    bad = 0
    oob = 0
    for W, H, body in pages:
        ws = []
        for m in re.finditer(
                r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" '
                r'yMax="([\d.]+)">(.*?)</word>', body, re.S):
            ws.append((float(m.group(1)), float(m.group(2)), float(m.group(3)),
                       float(m.group(4)), m.group(5)))
        for i in range(len(ws)):
            for j in range(i + 1, len(ws)):
                a, b = ws[i], ws[j]
                if abs(a[1] - b[1]) > 12:
                    continue
                if ov(a, b) > 0.02:
                    bad += 1
        oob += sum(1 for w in ws if w[0] < -0.5 or w[1] < -0.5
                   or w[2] > float(W) + 0.5 or w[3] > float(H) + 0.5)
    return bad, oob, len(pages)


def test_rendered_pdf_has_no_text_overlap():
    """真正 render 一份 PDF(合成 data)→ 逐頁驗零重疊、零出界。

    v3.3.62 捉到嘅兩個真 bug:
      1. 餐別徽章(「晚餐」)壓住餐名開頭 —— 藥丸行同餐名字形縱向撞 8pt
      2. 描述段落 wrap 闊度用錯(用右欄窄闊度)→ 行數比高度估算多
         → 判詞撞落「佔全日熱量」bar
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pdf_report_under_test", os.path.join(ROOT, "gym_web", "pdf_report.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    meals = []
    for i, (t, name, kcal) in enumerate([
            ("08:30", "燕麥粥加藍莓", 320),
            ("13:41", "三文魚刺身配紅洋蔥絲", 250),
            ("13:42", "牛油果炒蛋碗", 330),
            ("20:00", "腸粉", 450),
            ("20:01", "鹹肉糭", 850),
            ("20:02", "滑雞粥", 280),
            ("21:10", "黑咖啡（無糖無奶）", 2)]):
        meals.append({
            "time": t, "name": name, "kcal": kcal, "p": 20, "c": 30, "f": 8,
            "meal_type": "晚餐" if t >= "16:00" else "午餐",
            "restaurant": "測試餐廳", "grade": "B",
            "description": ("呢碟嘢睇落有一層油光，份量屬於細碟，"
                            "顏色偏黃，應該用咗少少油。"),
            "verdict": ("呢餐有唔少蛋白質，不過份量可能唔夠，"
                        "建議下一餐加多一碟菜。"),
        })
    totals = {"kcal": sum(m["kcal"] for m in meals), "P": 140, "C": 210, "F": 56}
    data = mod.render_nutrition_pdf("2026-10-28", meals, totals,
                                    {"generated_at": "2026-10-28 12:00 HKT"})
    assert data[:4] == b"%PDF", "應該係真 PDF"
    tmp = os.path.join("/home/work/.hermes/cache/scratch",
                       "qa_pdf_layout_test.pdf")
    with open(tmp, "wb") as f:
        f.write(data)
    bad, oob, pages = _bbox_overlaps(tmp)
    assert bad == 0, f"PDF 有 {bad} 組文字重疊"
    assert oob == 0, f"PDF 有 {oob} 個字組出界"
    assert pages >= 2, f"頁數太少: {pages}"


def test_timeline_labels_staggered():
    """同一時間(或差 1 分鐘)嘅多餐 → 時間線標籤唔可以疊埋一齊。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pdf_report_under_test2", os.path.join(ROOT, "gym_web", "pdf_report.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    src = _src(os.path.join(ROOT, "gym_web", "pdf_report.py"))
    # 一定要用「標籤實際 x 範圍」做碰撞(唔係圓點位置)
    assert "placed = []" in src
    assert "stringWidth(lbl, font, 7.5)" in src
    assert "left = cx - lw / 2.0" in src
    assert "right = cx + lw / 2.0" in src


def test_meal_card_height_matches_draw():
    """高度函數同繪製函數一定要用同一個段落寬度。"""
    src = _src(os.path.join(ROOT, "gym_web", "pdf_report.py"))
    assert src.count("para_w = CONTENT_W - 32") >= 2, \
        "繪製同高度函數都要用同一個段落闊度"
    # 唔准再用右欄窄闊度 wrap 段落
    assert "font, tw - 0," not in src


# ─────────────────────────────────────────────────────────────
# 6. v3.3.63 — 真・落本機下載(Jim OOB 2026-10-28)
#    「The pdf download button is not really download. It is wrongly showing
#     a pop up that does not have a share feature. Pls make it download to
#     local」
#
#    病根:iOS Safari / iOS PWA 收到 `Content-Type: application/pdf`
#    + `Content-Disposition: attachment`,唔會落檔 —— 佢會開 Quick Look
#    預覽 pop-up(而且嗰個 pop-up 冇分享掣)。
#    修法:`?dl=1` → `application/octet-stream` + `X-Content-Type-Options:
#    nosniff` → iOS 當普通檔案落去「檔案」App。
# ─────────────────────────────────────────────────────────────
def test_pdf_dl_mode_sends_octet_stream():
    """?dl=1 一定要送 application/octet-stream + nosniff(否則 iOS 開預覽)。"""
    src = _src(WEB)
    assert 'dl_mode = (request.args.get("dl")' in src
    assert 'content_type="application/octet-stream"' in src
    assert '"X-Content-Type-Options": "nosniff"' in src
    # dl 模式一定要喺 pdf 模式之前 return(唔可以兩樣都送)
    assert src.index('if dl_mode:') < src.index('mimetype="application/pdf"')


def test_pdf_dl_mode_content_disposition_quoted():
    """filename 一定要有引號 + filename*(RFC 5987),iOS 先識得個名。"""
    src = _src(WEB)
    assert 'attachment; filename="{download_name}"' in src
    assert "filename*=UTF-8''{download_name}" in src


def test_pdf_inline_mode_still_serves_pdf():
    """冇 ?dl=1 照舊送 application/pdf(前端 blob 分享 / 預覽靠佢)。"""
    src = _src(WEB)
    assert 'mimetype="application/pdf"' in src


def test_frontend_download_anchor_uses_dl_mode():
    """前端下載掣一定要指去 ?dl=1,唔係淨 pdf URL。"""
    tpl = _src(TPL)
    assert ":href=\"foodCardPdfDownloadUrl()\"" in tpl
    assert "foodCardPdfDownloadUrl()" in tpl
    assert "&dl=1" in tpl
    # 下載掣唔可以再指住裸 pdf URL(要睇 <a ...> 個 opening tag)
    m = re.search(r'<a[^>]*data-testid="food-card-pdf-download"[^>]*>', tpl, re.S)
    assert m, "搵唔到下載掣"
    assert "foodCardPdfDownloadUrl()" in m.group(0)
    assert "foodCardPdfUrl()" not in m.group(0)


def test_frontend_has_native_share_button():
    """要有原生分享掣(navigator.share + canShare)—— 即係 Jim 講嘅 share 功能。"""
    tpl = _src(TPL)
    assert 'data-testid="food-card-pdf-share"' in tpl
    assert "navigator.canShare" in tpl
    assert "navigator.share(" in tpl
    assert "shareFoodCardPdf()" in tpl
    assert "new File([blob]" in tpl


def test_share_button_does_not_break_pdf_inline_url():
    """分享流程要攞 inline PDF(唔係 dl=1),先可以砌出 File 嚟 share。"""
    tpl = _src(TPL)
    i = tpl.find("async shareFoodCardPdf()")
    seg = tpl[i:i + 1200]
    assert "/api/nutrition/pdf?date=" in seg
    assert "&dl=1" not in seg


def test_live_pdf_dl_headers():
    """實測:?dl=1 → octet-stream + attachment + nosniff。"""
    out = subprocess.run(
        ["curl", "-s", "-m", "180", "-D", "-", "-o", "/dev/null",
         BASE + "/api/nutrition/pdf?date=2026-10-27&dl=1"],
        capture_output=True, text=True).stdout
    if not out.strip():
        pytest.skip("server 未行")
    low = out.lower()
    assert "content-type: application/octet-stream" in low
    assert "content-disposition: attachment" in low
    assert "x-content-type-options: nosniff" in low
    assert "gymbro_foodlog_2026-10-27.pdf" in out


def test_live_pdf_inline_still_pdf():
    """冇 dl=1 → 照舊 application/pdf(唔可以整爛預覽)。"""
    out = subprocess.run(
        ["curl", "-s", "-m", "180", "-D", "-", "-o", "/dev/null",
         BASE + "/api/nutrition/pdf?date=2026-10-27"],
        capture_output=True, text=True).stdout
    if not out.strip():
        pytest.skip("server 未行")
    low = out.lower()
    assert "content-type: application/pdf" in low
    assert "content-disposition: attachment" in low
