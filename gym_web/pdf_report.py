"""
gym_web/pdf_report.py — 營養日結 PDF 報表 (v3.3.62)

Jim OOB 2026-10-27:
  「I think you change gymbro jpg download to pdf download. An overhaul.
    Make the pdf more informative as we have more spaces now.
    For food description and verdict, don't just comment the percentage.
    The ai visual info has more useful info. Don't drop it down the road」

## 點解要轉 PDF

JPG 卡有兩個死症:
  1. **高度硬上限** —— 1080×1736 晒,5 餐就爆,後面嘅餐冇位落。
  2. **每餐只有 240px 高** —— 塞唔落 AI 視覺描述 + 實質判詞,
     淨係得返一個 grade 字母同 kcal 丸。
     (講真,之前張卡由頭到尾淨係顯示「脂肪佔 58% 卡路里,偏高」。)

PDF 嘅頁係**流動嘅** —— 唔使 5 餐上限,想寫幾多寫幾多,
一餐可以佔成頁。所以「overhaul」唔係換個格式,係換個**資訊密度**。

## 版面

  Page 1  封面 / 摘要
          - 大標日期 + 星期
          - 全日總熱量 + 三大營養素長條(克數 + 卡路里佔比)
          - 統計藥丸(餐數 / 平均每餐 / 蛋白總量 / 最高卡餐)
          - 餐別分佈(早/午/晚/宵夜)
          - 評級分佈(A+ … F)
          - 時間線(每餐一個點 + 卡路里)
  Page 2+  每餐一卡(自動流動、唔限頁數)
          - 時間 / 餐別 / 評級章
          - 食物相(縮圖)
          - 餐名 / 餐廳 / 熱量 / 三大營養素
          - **描述** —— AI 視覺描述(食材、份量、煮法、賣相)
          - **判詞** —— 教練判詞(引用視覺資訊,唔淨係百分比)
          - 佔全日熱量百分比長條

## 字型

reportlab 只支援 TrueType(glyf)嘅 CJK 字型。Noto CJK 係 CFF/OTF →
reportlab 用唔到。改用 **WenQuanYi Zen Hei**(`/usr/share/fonts/truetype/wqy/
wqy-zenhei.ttc`,index 0)—— TrueType,而且覆蓋繁體中文。

粗體用 `setTextRenderMode(2)`(填色 + 描邊)做 faux bold,
唔使另外一個字型檔。
"""

from __future__ import annotations

import io
import os
import re
from datetime import datetime

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

BG_TOP = (0.11, 0.098, 0.18)
BG_BOT = (0.047, 0.039, 0.094)
CARD_BG = (0.145, 0.125, 0.235)
CARD_BG2 = (0.18, 0.16, 0.28)
BORDER = (0.27, 0.24, 0.40)
ACCENT = (0.655, 0.545, 0.980)
ACCENT_DIM = (0.45, 0.38, 0.72)
TEXT = (0.945, 0.945, 0.965)
TEXT_DIM = (0.62, 0.60, 0.72)
PROTEIN = (0.29, 0.87, 0.50)
CARBS = (0.98, 0.75, 0.14)
FAT = (0.96, 0.45, 0.71)
WARN = (0.98, 0.58, 0.24)

GRADE_COLORS = {
    "A+": (0.29, 0.87, 0.50),
    "A": (0.43, 0.90, 0.72),
    "B": (0.38, 0.65, 0.98),
    "C": (0.98, 0.75, 0.14),
    "D": (0.98, 0.58, 0.24),
    "F": (0.94, 0.27, 0.27),
}

PAGE_W = 595.27
PAGE_H = 841.89
MARGIN = 40.0
CONTENT_W = PAGE_W - MARGIN * 2

# ---------------------------------------------------------------------------
# Font registration
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
]
_FONT_NAME = "Helvetica"
_FONT_READY = False


def _register_font() -> str:
    """Register a CJK-capable TrueType font. Returns the font name."""
    global _FONT_NAME, _FONT_READY
    if _FONT_READY:
        return _FONT_NAME
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for path, idx in _FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont("GymCJK", path, subfontIndex=idx))
            _FONT_NAME = "GymCJK"
            _FONT_READY = True
            return _FONT_NAME
        except Exception as e:
            print(f"[pdf_report] font {path} failed: {type(e).__name__}: {e}",
                  flush=True)
    print("[pdf_report] no CJK TTF found, falling back to Helvetica",
          flush=True)
    _FONT_NAME = "Helvetica"
    _FONT_READY = True
    return _FONT_NAME


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3000 <= o <= 0x303F or 0x3400 <= o <= 0x4DBF
            or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF
            or 0xFF00 <= o <= 0xFFEF)


_NO_LINE_START = "，。、；：!?）】」』%,.;:!?)]}>"
_NO_LINE_END = "（【「『([{<"


def _wrap(text: str, font: str, size: float, max_w: float,
          max_lines: int = 99) -> list:
    """CJK-aware greedy wrap. Returns a list of lines."""
    from reportlab.pdfbase import pdfmetrics
    text = (text or "").replace("\r", "")
    lines = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            if lines and len(lines) < max_lines:
                lines.append("")
            continue
        cur = ""
        for ch in para:
            trial = cur + ch
            if pdfmetrics.stringWidth(trial, font, size) <= max_w or not cur:
                cur = trial
                continue
            # 需要斷行
            if ch in _NO_LINE_START and cur:
                # 標點唔可以喺行首 → 擺落上一行
                cur = cur + ch
                lines.append(cur)
                cur = ""
                continue
            if cur and cur[-1] in _NO_LINE_END:
                # 開引號唔可以喺行尾 → 推落下一行
                hold = cur[-1]
                cur = cur[:-1]
                lines.append(cur)
                cur = hold
                continue
            lines.append(cur)
            cur = ch
            if len(lines) >= max_lines:
                break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        if len(lines) >= max_lines:
            break
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if lines and max_lines < 99 and len(lines) == max_lines:
        # 最後一行加省略號(如果原文仲有)
        last = lines[-1]
        try:
            from reportlab.pdfbase import pdfmetrics
            if pdfmetrics.stringWidth(last + "…", font, size) <= max_w:
                lines[-1] = last + "…"
        except Exception:
            pass
    return lines


def _draw(c, x, y, s, size, color, font, bold=False, align="l", max_w=None):
    """Draw a single line of text. y = BASELINE."""
    from reportlab.pdfbase import pdfmetrics
    if not s:
        return 0
    if max_w:
        while pdfmetrics.stringWidth(s, font, size) > max_w and len(s) > 1:
            s = s[:-1]
    w = pdfmetrics.stringWidth(s, font, size)
    if align == "r":
        x = x - w
    elif align == "c":
        x = x - w / 2.0
    c.setFillColorRGB(*color)
    if bold:
        # Faux bold: text render mode 2 = 填色 + 描邊。
        # 線粗同描邊色要設喺 canvas(graphics state)—— PDFTextObject 冇 setLineWidth。
        c.setLineWidth(max(0.3, size * 0.045))
        c.setStrokeColorRGB(*color)
        t = c.beginText(x, y)
        t.setTextRenderMode(2)
        t.setFont(font, size)
        t.setFillColorRGB(*color)
        t.textOut(s)
        c.drawText(t)
    else:
        c.setFont(font, size)
        c.drawString(x, y, s)
    return w


def _para(c, x, y, text, size, color, font, max_w, leading=None,
          bold=False, max_lines=99, align="l"):
    """Draw wrapped paragraph. Returns new y (baseline of last line)."""
    leading = leading or size * 1.45
    lines = _wrap(text, font, size, max_w, max_lines)
    for ln in lines:
        _draw(c, x, y, ln, size, color, font, bold=bold, align=align,
              max_w=max_w)
        y -= leading
    return y


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def _gradient(c, w, h, top, bot):
    bands = 64
    bh = h / bands
    for i in range(bands):
        t = i / (bands - 1.0)
        c.setFillColorRGB(
            top[0] * (1 - t) + bot[0] * t,
            top[1] * (1 - t) + bot[1] * t,
            top[2] * (1 - t) + bot[2] * t,
        )
        c.rect(0, h - (i + 1) * bh, w, bh + 0.6, stroke=0, fill=1)


def _rrect(c, x, y, w, h, r, fill=None, stroke=None, lw=0.7):
    if fill is not None:
        c.setFillColorRGB(*fill)
    if stroke is not None:
        c.setStrokeColorRGB(*stroke)
        c.setLineWidth(lw)
    c.roundRect(x, y, w, h, r, stroke=1 if stroke is not None else 0,
                fill=1 if fill is not None else 0)


def _bar(c, x, y, w, h, pct, color, bg=(0.16, 0.14, 0.26)):
    _rrect(c, x, y, w, h, h / 2.0, fill=bg)
    fw = max(0.0, min(1.0, pct)) * w
    if fw > 1:
        _rrect(c, x, y, fw, h, h / 2.0, fill=color)


def _pill(c, x, y, text, size, fg, bg, font, pad=6.0, h=16.0, bold=True):
    from reportlab.pdfbase import pdfmetrics
    w = pdfmetrics.stringWidth(text, font, size) + pad * 2
    _rrect(c, x, y, w, h, h / 2.0, fill=bg)
    _draw(c, x + pad, y + (h - size) / 2.0 + size * 0.16, text, size, fg, font,
          bold=bold)
    return w


# ---------------------------------------------------------------------------
# Page builders
# ---------------------------------------------------------------------------

def _cover_page(c, font, date_iso, meals, totals, meta):
    # 注意:唔好喺呢度叫 showPage() —— 呢個係**第一頁**(封面),
    # 之前加咗會多出一張空白頁。
    _gradient(c, PAGE_W, PAGE_H, BG_TOP, BG_BOT)

    # brand chip
    _rrect(c, MARGIN, PAGE_H - 66, 168, 26, 13, fill=(0.19, 0.17, 0.31))
    _draw(c, MARGIN + 14, PAGE_H - 58, "gymbro · 營養日結", 11, ACCENT, font,
          bold=True)

    # date
    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d")
        wd = ["星期一", "星期二", "星期三", "星期四", "星期五",
              "星期六", "星期日"][d.weekday()]
        date_label = "%d年%d月%d日 %s" % (d.year, d.month, d.day, wd)
    except Exception:
        date_label = date_iso
    _draw(c, MARGIN, PAGE_H - 128, date_label, 34, TEXT, font, bold=True)
    _draw(c, MARGIN, PAGE_H - 150, "AI 視覺描述 + 教練判詞完整版", 12,
          TEXT_DIM, font)

    # total kcal block
    kcal = int(round(totals.get("kcal", 0) or 0))
    _draw(c, MARGIN, PAGE_H - 232, "全日總熱量", 12, TEXT_DIM, font)
    _draw(c, MARGIN, PAGE_H - 300, str(kcal), 68, ACCENT, font, bold=True)
    bw = 132
    _draw(c, MARGIN + 150, PAGE_H - 300, "kcal", 20, TEXT_DIM, font, bold=True)

    # macro bars
    p = totals.get("P", 0) or 0
    cb = totals.get("C", 0) or 0
    f = totals.get("F", 0) or 0
    p_kcal = p * 4
    c_kcal = cb * 4
    f_kcal = f * 9
    tot_k = max(1.0, p_kcal + c_kcal + f_kcal)
    y = PAGE_H - 340
    for label, grams, kc, color in (
            ("蛋白質 P", p, p_kcal, PROTEIN),
            ("碳水 C", cb, c_kcal, CARBS),
            ("脂肪 F", f, f_kcal, FAT)):
        _draw(c, MARGIN, y, label, 11, TEXT_DIM, font)
        _draw(c, MARGIN + 78, y, "%.0f g" % grams, 12, TEXT, font, bold=True,
              align="r")
        _draw(c, MARGIN + 150, y, "%.0f%%" % (kc / tot_k * 100), 11, color,
              font, bold=True)
        _bar(c, MARGIN + 160, y - 3, 250, 8, kc / tot_k, color)
        y -= 26

    # stat pills row
    y -= 22
    count = len(meals)
    avg = (kcal / count) if count else 0
    top_meal = max(meals, key=lambda m: m.get("kcal", 0) or 0) if meals else None
    stats = [
        ("餐數", "%d 餐" % count),
        ("平均每餐", "%.0f kcal" % avg),
        ("蛋白總量", "%.0f g" % p),
        ("最高卡", "%s %.0f" % (top_meal.get("name", "")[:6],
                               top_meal.get("kcal", 0) or 0) if top_meal else "—"),
    ]
    px = MARGIN
    for i, (k, v) in enumerate(stats):
        _rrect(c, px, y - 34, 122, 44, 10, fill=(0.16, 0.145, 0.27),
               stroke=BORDER)
        _draw(c, px + 10, y - 6, k, 9, TEXT_DIM, font)
        _draw(c, px + 10, y - 26, v, 12, TEXT, font, bold=True)
        px += 128
        if i == 1:
            px = MARGIN
            y -= 50

    # meal-type breakdown
    y -= 66
    _draw(c, MARGIN, y, "餐別分佈", 13, TEXT, font, bold=True)
    y -= 16
    buckets = {}
    for m in meals:
        key = (m.get("meal_type") or "").strip() or "餐"
        buckets[key] = buckets.get(key, 0) + (m.get("kcal", 0) or 0)
    if buckets:
        mx = max(buckets.values()) or 1
        for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]):
            _draw(c, MARGIN, y, k, 11, TEXT, font)
            _draw(c, MARGIN + 120, y, "%.0f kcal" % v, 11, TEXT_DIM, font,
                  align="r")
            _bar(c, MARGIN + 134, y - 2, 280, 9, v / mx, ACCENT)
            y -= 22

    # grade distribution
    y -= 16
    _draw(c, MARGIN, y, "教練評級分佈", 13, TEXT, font, bold=True)
    y -= 18
    grades = {}
    for m in meals:
        g = (m.get("grade") or "—").strip()
        grades[g] = grades.get(g, 0) + 1
    gx = MARGIN
    for g in ("A+", "A", "B", "C", "D", "F"):
        n = grades.get(g, 0)
        if n == 0:
            continue
        col = GRADE_COLORS.get(g, TEXT_DIM)
        w = _pill(c, gx, y - 14, "%s × %d" % (g, n), 11, (0.08, 0.07, 0.13),
                  col, font, pad=8, h=20)
        gx += w + 8
    if gx == MARGIN:
        _draw(c, MARGIN, y - 4, "—", 11, TEXT_DIM, font)

    # timeline
    y -= 46
    _draw(c, MARGIN, y, "進食時間線", 13, TEXT, font, bold=True)
    y -= 26
    tl_y = y
    _rrect(c, MARGIN, tl_y, CONTENT_W, 4, 2, fill=(0.22, 0.20, 0.34))
    # v3.3.62:同一時間(或者時間好接近)有多餐 → 標籤會疊埋一齊。
    # → 用「標籤實際佔據嘅 x 範圍」做碰撞偵測(唔係用圓點位置,圓點之間
    #    可以只差 20pt 但標籤各佔 36pt → 一定重疊)。
    from reportlab.pdfbase import pdfmetrics
    placed = []          # [(x_left, x_right, row)]
    for m in meals:
        hh, mm = _hhmm(m.get("time", ""))
        frac = (hh * 60 + mm) / (24 * 60.0)
        cx = MARGIN + CONTENT_W * frac
        col = ACCENT
        c.setFillColorRGB(*col)
        c.circle(cx, tl_y + 2, 5, stroke=0, fill=1)
        c.setFillColorRGB(0.11, 0.10, 0.20)
        c.circle(cx, tl_y + 2, 2, stroke=0, fill=1)
        lbl = "%s %d" % (m.get("time", "")[:5], int(m.get("kcal", 0) or 0))
        lw = pdfmetrics.stringWidth(lbl, font, 7.5)
        left = cx - lw / 2.0
        right = cx + lw / 2.0
        row = 0
        for (pl, pr, prow) in placed:
            if not (right < pl - 2 or left > pr + 2):   # 真係有重疊
                row = max(row, prow + 1)
        placed.append((left, right, row))
        _draw(c, cx, tl_y + 12 - row * 9, lbl, 7.5, TEXT_DIM, font, align="c")

    # footer
    _draw(c, MARGIN, 46, "gymbro · AI 視覺描述 + 私人教練判詞", 8.5,
          TEXT_DIM, font)
    _draw(c, PAGE_W - MARGIN, 46, meta.get("generated_at", ""), 8.5,
          TEXT_DIM, font, align="r")
    _draw(c, MARGIN, 32, "數據來源:Google Sheet Nutrition 分頁 · 描述/判詞由 "
          "AI 視覺分析生成", 7.5, (0.45, 0.43, 0.55), font)


def _hhmm(t: str):
    try:
        parts = (t or "0:0").strip().split(":")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return 0, 0


def _meal_card_height(meal, font):
    """Estimate the card height (needed for page flow).

    v3.3.62 重新編排過每張卡嘅垂直分帶(見 _draw_meal_card 嘅註解):
      band 1 pills      [top-30, top-10]
      band 2 圖+文字     圖 [top-106, top-30] · 名 top-46 · 副題 top-64 · 巨量 [top-88, top-70]
      band 3 描述        標籤 top-112,段落由 top-126
      band 4 判詞        緊接描述
      band 5 佔全日熱量  bar [bot+12, bot+19]
    呢個高度函數**必須同 _draw_meal_card 一致**,唔好淨係改一個。
    """
    h = 112.0  # band 1 + band 2 + 描述標籤
    desc = (meal.get("description") or "").strip()
    verdict = (meal.get("verdict") or "").strip()
    para_w = CONTENT_W - 32        # ⚠️ 一定要同 _draw_meal_card 一致
    if desc:
        lines = _wrap(desc, font, 9, para_w, 6)
        h += 14 + len(lines) * 13 + 2
    if verdict:
        lines = _wrap(verdict, font, 9.5, para_w, 7)
        h += 4 + 14 + len(lines) * 14 + 2
    h += 18  # 判詞最後一行 ↔「佔全日熱量」bar 嘅距離 + 底部留白
    return h


def _draw_meal_card(c, font, meal, y_top, totals_kcal):
    """Draw one meal card. y_top = top edge. Returns the new y (bottom).

    v3.3.62 重新編排咗垂直分帶(修 Jim 報嘅「文字重疊」):
      band 1  藥丸行     [top-30, top-10]  時間 + 餐別 + 評級
      band 2  圖 + 文字   圖 [top-106, top-30] · 餐名 top-46 · 副題 top-64
                            巨量丸 [top-88, top-70]
      band 3  描述       標籤 top-112,段落由 top-126
      band 4  判詞       緊接描述之下
      band 5  佔全日熱量 bar [bot+12, bot+19]

    ⚠️ 呢個函數同 `_meal_card_height()` **必須同步改**,唔好淨係改一個:
    高度估細咗 → 下一張卡會蓋住上一張嘅判詞 / bar。
    """
    desc = (meal.get("description") or "").strip()
    verdict = (meal.get("verdict") or "").strip()
    h = _meal_card_height(meal, font)
    top = y_top
    bot = y_top - h
    _rrect(c, MARGIN, bot, CONTENT_W, h, 12, fill=CARD_BG, stroke=BORDER)

    # ── band 1:藥丸行(時間 / 餐別 / 評級)────────────────────
    pill_y = top - 30          # 藥丸底
    tw_pill = _pill(c, MARGIN + 16, pill_y, meal.get("time", "") or "--:--",
                    11, TEXT, (0.22, 0.20, 0.34), font, pad=8, h=20)
    mtype = (meal.get("meal_type") or "").strip()
    if mtype:
        # 動態接喺時間藥丸後面(唔好固定 x → 會撞到餐名)
        _pill(c, MARGIN + 16 + tw_pill + 8, pill_y, mtype, 9, TEXT_DIM,
              (0.20, 0.19, 0.30), font, pad=7, h=20)
    grade = (meal.get("grade") or "").strip()
    if grade and grade != "—":
        gcol = GRADE_COLORS.get(grade, TEXT_DIM)
        from reportlab.pdfbase import pdfmetrics
        gw = pdfmetrics.stringWidth(grade, font, 13) + 16
        _rrect(c, MARGIN + CONTENT_W - 16 - gw, pill_y, gw, 26, 8, fill=gcol)
        _draw(c, MARGIN + CONTENT_W - 16 - gw / 2.0, pill_y + 7, grade, 13,
              (0.08, 0.07, 0.13), font, bold=True, align="c")

    # ── band 2:相 + 餐名 / 餐廳 / 巨量──────────────────────
    ix = MARGIN + 16
    iy = top - 106
    iw = 76
    img = meal.get("image_bytes")
    if img:
        try:
            from reportlab.lib.utils import ImageReader
            c.drawImage(ImageReader(io.BytesIO(img)), ix, iy, iw, iw,
                        preserveAspectRatio=True, anchor="c", mask="auto")
        except Exception as e:
            print(f"[pdf_report] image draw failed: {type(e).__name__}: {e}",
                  flush=True)
            _rrect(c, ix, iy, iw, iw, 8, fill=(0.22, 0.20, 0.34))
    else:
        _rrect(c, ix, iy, iw, iw, 8, fill=(0.22, 0.20, 0.34))
        _draw(c, ix + iw / 2.0, iy + iw / 2.0 - 4, "無圖", 10, TEXT_DIM, font,
              align="c")

    tx = ix + iw + 14
    tw = MARGIN + CONTENT_W - 16 - tx
    name = (meal.get("name") or "").strip() or "未命名"
    _draw(c, tx, top - 46, name, 13.5, TEXT, font, bold=True, max_w=tw)
    sub = meal.get("restaurant") or ""
    kcal = int(round(meal.get("kcal", 0) or 0))
    sub_txt = ("%s · " % sub) if sub else ""
    sub_txt += "%d kcal" % kcal
    _draw(c, tx, top - 64, sub_txt, 10, ACCENT, font, bold=True, max_w=tw)
    mx = tx
    for label, val, color in (("蛋白", meal.get("p", 0) or 0, PROTEIN),
                              ("碳水", meal.get("c", 0) or 0, CARBS),
                              ("脂肪", meal.get("f", 0) or 0, FAT)):
        _rrect(c, mx, top - 88, 62, 18, 6, fill=(0.21, 0.19, 0.32))
        c.setFillColorRGB(*color)
        c.rect(mx + 4, top - 84, 3, 10, stroke=0, fill=1)
        _draw(c, mx + 11, top - 82, "%s %.0f" % (label, val), 8.5, TEXT, font)
        mx += 66

    # ── band 3/4:描述 + 判詞───────────────────────────────
    cy = top - 112
    # ⚠️ v3.3.62:段落喺 MARGIN+16 開始、行足成卡闊 → wrap 闊度一定要用
    #    CONTENT_W - 32。舊 code 用咗 `tw`(右欄相片旁嘅窄闊度)→
    #    實際行數比 _meal_card_height() 估嘅多 → 判詞撞落「佔全日熱量」。
    para_w = CONTENT_W - 32
    if desc:
        _draw(c, MARGIN + 16, cy, "描述", 9.5, ACCENT, font, bold=True)
        cy = _para(c, MARGIN + 16, cy - 14, desc, 9, TEXT, font, para_w,
                   leading=13, max_lines=6) - 2
    if verdict:
        cy -= 4
        _draw(c, MARGIN + 16, cy, "判詞", 9.5, WARN, font, bold=True)
        cy = _para(c, MARGIN + 16, cy - 14, verdict, 9.5, TEXT, font,
                   para_w, leading=14, max_lines=7) - 2

    # ── band 5:佔全日熱量──────────────────────────────────
    total_k = totals_kcal or 1
    share = (meal.get("kcal", 0) or 0) / total_k * 100
    by = bot + 12
    _draw(c, MARGIN + 16, by, "佔全日熱量 %.0f%%" % share, 8.5, TEXT_DIM, font)
    _bar(c, MARGIN + 110, by - 1, CONTENT_W - 130, 7, share / 100.0, ACCENT)

    return bot


CONT_MAX_H = 14000.0   # PDF 單頁高度上限(200 inch = 14400pt),超咗就跌返多頁


def _continuous_height(meals, font) -> float:
    """一頁連續版需要幾高(精簡摘要 + 全部餐 + footer)。"""
    h = 300.0
    for m in meals:
        h += _meal_card_height(m, font) + 18
    return h


def _draw_continuous(c, font, date_iso, meals, totals, meta, page_h):
    """v3.3.64 (Jim OOB 2026-10-28「Both jpg and pdf have no page limit
    (continuous scrolling)」):一頁過 —— 摘要 + 全部餐,唔分頁,
    淨係一路 scroll 落去。"""
    _gradient(c, PAGE_W, page_h, BG_TOP, BG_BOT)
    y = page_h - 40

    # ── 摘要 header(精簡封面)──────────────────────────────
    _rrect(c, MARGIN, y - 26, 168, 26, 13, fill=(0.19, 0.17, 0.31))
    _draw(c, MARGIN + 14, y - 18, "gymbro · 營養日結", 11, ACCENT, font,
          bold=True)
    _draw(c, PAGE_W - MARGIN, y - 18, date_iso, 10, TEXT_DIM, font, align="r")
    y -= 44

    try:
        d = datetime.strptime(date_iso, "%Y-%m-%d")
        wd = ["星期一", "星期二", "星期三", "星期四", "星期五",
              "星期六", "星期日"][d.weekday()]
        date_label = "%d年%d月%d日 %s" % (d.year, d.month, d.day, wd)
    except Exception:
        date_label = date_iso
    _draw(c, MARGIN, y - 26, date_label, 24, TEXT, font, bold=True)
    y -= 34

    kcal = int(round(totals.get("kcal", 0) or 0))
    _draw(c, MARGIN, y - 40, str(kcal), 46, ACCENT, font, bold=True)
    _draw(c, MARGIN + 132, y - 40, "kcal", 16, TEXT_DIM, font, bold=True)
    p = totals.get("P", 0) or 0
    cb = totals.get("C", 0) or 0
    f = totals.get("F", 0) or 0
    _draw(c, MARGIN + 210, y - 26, "蛋白 %.0f g" % p, 11, TEXT, font)
    _draw(c, MARGIN + 210, y - 44, "碳水 %.0f g" % cb, 11, TEXT, font)
    _draw(c, MARGIN + 320, y - 26, "脂肪 %.0f g" % f, 11, TEXT, font)
    _draw(c, MARGIN + 320, y - 44, "%d 餐 · 連續頁(冇分頁)" % len(meals), 11,
          TEXT_DIM, font)
    y -= 62

    _draw(c, MARGIN, y - 14, "逐餐詳情", 15, TEXT, font, bold=True)
    y -= 32

    totals_kcal = totals.get("kcal", 0) or 0
    for m in meals:
        y = _draw_meal_card(c, font, m, y, totals_kcal) - 14

    _draw(c, MARGIN, 30, "gymbro · AI 視覺描述 + 私人教練判詞", 7.5,
          (0.45, 0.43, 0.55), font)


def render_nutrition_pdf(date_iso: str, meals: list, totals: dict,
                         meta: dict | None = None,
                         continuous: bool = False) -> bytes:
    """Render the nutrition report PDF. Returns the PDF bytes.

    `meals` entries: time, name, restaurant, meal_type, kcal, p, c, f,
                     grade, description, verdict, image_bytes.

    v3.3.64: `continuous=True` → **單一長頁**(摘要 + 全部餐,冇分頁),
    方便連續滾動睇。太高(> CONT_MAX_H)自動跌返多頁版。
    """
    from reportlab.pdfgen import canvas
    meta = meta or {}
    font = _register_font()

    buf = io.BytesIO()
    if continuous:
        need = _continuous_height(meals, font)
        if need <= CONT_MAX_H:
            page_h = max(PAGE_H, need)
            c = canvas.Canvas(buf, pagesize=(PAGE_W, page_h))
            c.setTitle("gymbro 營養日結 %s(連續)" % date_iso)
            c.setAuthor("gymbro")
            c.setSubject("營養日結 + AI 視覺描述 + 教練判詞(連續版)")
            _draw_continuous(c, font, date_iso, meals, totals, meta, page_h)
            c.save()
            return buf.getvalue()
        # 太高 → 用多頁版(唔好硬塞)
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setTitle("gymbro 營養日結 %s" % date_iso)
    c.setAuthor("gymbro")
    c.setSubject("營養日結 + AI 視覺描述 + 教練判詞")

    _cover_page(c, font, date_iso, meals, totals, meta)

    # ── meal pages ───────────────────────────────────────────────────
    c.showPage()
    _gradient(c, PAGE_W, PAGE_H, BG_TOP, BG_BOT)
    y = PAGE_H - 40
    _draw(c, MARGIN, y - 14, "逐餐詳情", 16, TEXT, font, bold=True)
    _draw(c, PAGE_W - MARGIN, y - 14, "%d 餐 · %s" % (len(meals), date_iso),
          10, TEXT_DIM, font, align="r")
    y -= 36

    totals_kcal = totals.get("kcal", 0) or 0
    for m in meals:
        need = _meal_card_height(m, font) + 18
        if y - need < 50:
            c.showPage()
            _gradient(c, PAGE_W, PAGE_H, BG_TOP, BG_BOT)
            y = PAGE_H - 40
        y = _draw_meal_card(c, font, m, y, totals_kcal) - 14

    # footer on last page
    _draw(c, MARGIN, 30, "gymbro · AI 視覺描述 + 私人教練判詞", 7.5,
          (0.45, 0.43, 0.55), font)
    # ⚠️ 唔好喺 save() 前叫 showPage() —— 會多出一張空白尾頁
    c.save()
    return buf.getvalue()
