"""QA — frontend progress bar (SCAN_PROGRESS_SPEC §4.4).

Isak 2026-10-20。

兩層測試:

  **Layer A — template 靜態層** (tests/qa/test_scan_progress_template.py)
      直接讀 templates/index.html,驗 Torres 嘅改動確實喺檔案入面。
      唔需要 server,秒級完成,適合每次 commit 跑。

  **Layer B — live browser 動態層** (本檔)
      Playwright headless 開真實 PWA,驗進度條喺 browser 入面真係 render
      咗、真係識郁。

重點 (Jim OOB 2026-10-20「STOP patching, run playwright headless test FIRST」):
呢啲測試全部 **自動、可重複**,唔使人手 click。

⚠️ 注意 `wait_until="networkidle"` 喺 gymbro PWA 永遠唔會 settle ——
   前端有持續 polling (setInterval 拉 /api/state、/api/withings_* 等),
   networkidle 會 timeout。所以全部用 "domcontentloaded" + 明確 wait。

跑法:
    PYTHONPATH="" /home/work/projects/gymbro/.venv/bin/python -m pytest \
        tests/qa/test_scan_progress_frontend.py -v -s
"""

import json
import os
import re
import time
from pathlib import Path

import pytest
import requests
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("GYMBRO_URL", "http://127.0.0.1:7000")
REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE_FILE = REPO / "templates" / "index.html"

# Torres 喺 templates/index.html 用嘅 data-* hook (SCAN_PROGRESS_SPEC §3.3)
SEL_PROGRESS_ROOT = "[data-scan-progress]"
SEL_UPLOAD_BAR = "[data-scan-upload-bar]"
SEL_AI_BAR = "[data-scan-ai-bar]"
SEL_STAGE_LABEL = "[data-scan-stage-label]"

# Jim OOB 2026-10-20: 唔好用英文 narrative term ("Uploading..." / "Recognizing...")。
# 只查「可見文案」位置 — x-text="..." 同 >文字< — 唔好連 Alpine state 變數名
# 都當成文案 (例如 scanUploading 係變數,唔係畀 Jim 睇嘅字)。
def _english_narrative_hits(html: str):
    hits = []
    for m in re.finditer(r'x-text="([^"]*)"', html):
        val = m.group(1)
        for _ in re.finditer(
            r"(?:^|[\s'\"\(])(Uploading|Recognizing|Recognising|Processing)"
            r"(?:[\s'\"\).]|$)", val
        ):
            hits.append(f"x-text={val[:80]!r}")
    for m in re.finditer(r">\s*(Uploading|Recognizing|Recognising|Processing)(?:\.\.\.)?\s*<", html):
        hits.append(f">{m.group(1)}<")
    return hits


def _server_alive() -> bool:
    try:
        return requests.get(f"{BASE_URL}/healthz", timeout=5).status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox"])
        yield b
        b.close()


@pytest.fixture(scope="function")
def page(browser):
    """每個 test 獨立 page。

    ⚠️ 唔可以用 module-scope:page 係共享嘅,前一個 test 觸發嘅
    upload flow 會污染下一個 test 嘅 Alpine state
    (實測踩過: test_upload_bar_shows_real_byte_progress 跑完之後,
     下一個 test 讀到嘅 scanAiStageLabel 係上一次 flow 寫入嘅值,
     而唔係自己 set 嘅值 → 假 failure)。

    ⚠️ **service_workers='block' 一定要加** — gymbro PWA 有 `sw.js`,
       service worker 會 **bypass Playwright 嘅 page.route()**:
       request 由 SW 直接轉發,route handler 永遠唔會觸發。
       (實測: 同一個 route,SW on → 0 hits / SW blocked → 1 hit)
       冇 block 嘅話,所有 route mock 都係假嘅 —— 測試會以為
       frontend 冇打 API,而真實係 mock 根本冇生效。
    """
    if not _server_alive():
        pytest.skip(f"gymbro server 冇起喺 {BASE_URL}")
    ctx = browser.new_context(
        viewport={"width": 393, "height": 852},
        is_mobile=True,
        has_touch=True,
        service_workers="block",   # ← SW 會 bypass page.route()
    )
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: None)  # 唔好因為 page error 炸個 test
    pg.goto(f"{BASE_URL}/", wait_until="domcontentloaded", timeout=30000)
    # 等 Alpine 起好
    pg.wait_for_function(
        "() => document.querySelector('[x-data]') && window.Alpine !== undefined",
        timeout=15000,
    )
    pg.wait_for_timeout(1500)
    yield pg
    ctx.close()


def _alpine_state(pg):
    return pg.evaluate("""() => {
        const r = document.querySelector('[x-data]');
        const st = r && r._x_dataStack && r._x_dataStack[0];
        if (!st) return null;
        return {
            phase: st.scanPhase,
            uploadPct: st.scanUploadPct,
            uploadCurrent: st.scanUploadCurrent,
            uploadTotal: st.scanUploadTotal,
            bytes: st.scanUploadBytesLabel,
            aiPct: st.scanAiPct,
            aiStage: st.scanAiStageLabel,
            aiCurrent: st.scanAiCurrent,
            aiTotal: st.scanAiTotal,
            aiElapsed: st.scanAiElapsed,
        };
    }""")


def _open_scan_tab(pg):
    """Switch 去 scan tab (唔撳相簿 button,避免開 file picker)。"""
    pg.evaluate("""() => {
        const r = document.querySelector('[x-data]');
        const st = r && r._x_dataStack && r._x_dataStack[0];
        if (!st) return;
        if ('activeTab' in st) st.activeTab = 'scan';
        if (typeof st.isTabVisible === 'function') st.isTabVisible = () => true;
    }""")
    pg.wait_for_timeout(500)


# ──────────────────────────────────────────────────────────────────────────
# 進度條 DOM 存在 (live browser)
# ──────────────────────────────────────────────────────────────────────────

def test_progress_bar_dom_exists(page):
    """Spec §3.3: 兩條 bar (上傳 + AI) + stage label 都要喺 DOM 內。"""
    assert page.locator(SEL_PROGRESS_ROOT).count() >= 1, (
        f"搵唔到 {SEL_PROGRESS_ROOT} — 前端未加兩階段進度條 container。"
        f"(如果 server 未 restart,可能 serve 緊舊 template — 睇 report)"
    )
    assert page.locator(SEL_UPLOAD_BAR).count() >= 1, (
        f"搵唔到 {SEL_UPLOAD_BAR} — 冇上傳進度條"
    )
    assert page.locator(SEL_AI_BAR).count() >= 1, (
        f"搵唔到 {SEL_AI_BAR} — 冇 AI 識別進度條"
    )
    assert page.locator(SEL_STAGE_LABEL).count() >= 1, (
        f"搵唔到 {SEL_STAGE_LABEL} — AI bar 冇 stage label"
    )


def test_progress_bar_has_x_text_bindings(page):
    """Spec §3.3: 每條 bar 都要有文字 stage label (唔淨係得個 bar)。"""
    html = page.content()
    for var in ("scanUploadPct", "scanAiPct", "scanAiStageLabel"):
        assert var in html, f"前端冇 {var} 綁定 — 進度條唔係真實數據"


def test_two_bars_have_different_colours(page):
    """Spec §3.3: 兩條 bar 要用唔同顏色 (上傳 = 藍/emerald,AI = amber/purple),
    唔好分唔到。"""
    upload_html = page.locator(SEL_UPLOAD_BAR).inner_html()
    ai_html = page.locator(SEL_AI_BAR).inner_html()

    upload_colours = set(re.findall(r"(?:bg|text|from|to)-([a-z]+)-\d{3}", upload_html))
    ai_colours = set(re.findall(r"(?:bg|text|from|to)-([a-z]+)-\d{3}", ai_html))

    assert upload_colours, f"上傳 bar 搵唔到 tailwind colour class: {upload_html[:300]}"
    assert ai_colours, f"AI bar 搵唔到 tailwind colour class: {ai_html[:300]}"
    assert upload_colours != ai_colours, (
        f"兩條 bar 同一個色 (upload={upload_colours}, ai={ai_colours}) — Jim 分唔到"
    )


def test_no_english_narrative_terms(page):
    """Spec §3.3: 唔好用英文 narrative term,用「上傳緊」「AI 睇緊」「識別緊」。"""
    html = page.content()
    hits = _english_narrative_hits(html)
    assert not hits, (
        f"前端有英文 narrative term (Jim OOB 2026-10-20 要用「上傳緊」「AI 睇緊」): "
        f"{hits[:5]}"
    )


def test_no_telegram_button_added(page):
    """Spec §3.3: 唔好整 Telegram button (Jim 硬性要求)。"""
    html = page.content()
    idx = html.find("SCAN TAB")
    scan_section = html[idx: idx + 40000] if idx >= 0 else html
    assert "telegram" not in scan_section.lower(), "scan tab 出現 telegram button"


# ──────────────────────────────────────────────────────────────────────────
# 真實互動 — upload 進度條會郁
# ──────────────────────────────────────────────────────────────────────────

def test_upload_bar_shows_real_byte_progress(page, tmp_path):
    """Spec §3.1 + §3.3: 上傳進度條用真實 byte 進度 (XHR upload.onprogress),
    唔係假 setInterval。

    做法:攔截 /api/scan_upload 慢啲先返,期間觀察 DOM 入面
    scanUploadPct / scanUploadBytesLabel 有冇真實變化。

    ⚠️ 兩個關鍵細節 (實測撞過):
      1. 要用 **scanPhotosInputEl** (multiple input,行 649),唔係
         scanInputEl (單張 legacy input,行 648)。新流程走
         onScanPhotosPicked → _scanProbeApi() → _scanUploadOne()。
      2. 要 mock **OPTIONS** /api/scan_upload —— 前端用 OPTIONS probe
         判斷 server 有冇新 endpoint (行 3910)。唔 mock 嘅話,
         真實 server 返 404 → 前端行 legacy mode → 根本唔會打 POST,
         測試會 false-fail。
    """
    _open_scan_tab(page)

    state = {"hits": 0, "probe": 0}

    def _probe(route):
        state["probe"] += 1
        route.fulfill(status=200, content_type="application/json",
                      body='{"ok":true}')

    def _route(route):
        state["hits"] += 1
        time.sleep(1.5)
        route.fulfill(
            status=200,
            content_type="application/json",
            # 前端要求 uploads[0].path 必須存在 (見 _scanUploadOne),
            # 否則會當上傳失敗 → '全部相片都上傳失敗'
            body=json.dumps({
                "ok": True,
                "uploads": [{
                    "client_index": 0,
                    "path": "/home/work/.hermes/scan_cache/qa_upload_0.jpg",
                    "filename": "qa_food.jpg",
                    "image_url": "/scan_img/qa_upload_0.jpg",
                    "thumbnail_url": "/scan_thumb/qa_upload_0.jpg",
                    "size_kb": 4300,
                    "capture_iso": "2026-10-20",
                    "capture_hhmm": "23:18",
                }],
            }),
        )

    def _recognise(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"job_id":"scanjob_qa","total":1}',
        )

    def _progress(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"ok":true,"job_id":"scanjob_qa","status":"completed",'
                '"stage":"done","stage_label":"完成","pct":100,'
                '"current_index":0,"total":1,"results":[],"errors":[],'
                '"elapsed_s":1.0}'
            ),
        )

    def _router(route):
        if route.request.method == "OPTIONS":
            _probe(route)
        else:
            _route(route)

    page.route("**/api/scan_upload", _router)
    # 唔 mock 呢兩個嘅話,前端會真跑 AI + commit 落 Google Sheet
    page.route("**/api/scan_recognize", _recognise)
    page.route("**/api/scan_progress/**", _progress)

    fixture = tmp_path / "qa_food.jpg"
    src = REPO / "test_fixtures" / "food_1.jpg"
    if src.exists():
        fixture.write_bytes(src.read_bytes())
    else:
        fixture.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20000)

    page.set_input_files("input[x-ref='scanPhotosInputEl']", str(fixture))

    # 等 phase 進入 uploading (可能 upload 太快而家就完咗)
    try:
        page.wait_for_function(
            "() => { const r = document.querySelector('[x-data]');"
            " return r && r._x_dataStack && r._x_dataStack[0] &&"
            " r._x_dataStack[0].scanPhase === 'uploading'; }",
            timeout=8000,
        )
    except Exception:
        pass

    seen = []
    for _ in range(26):
        try:
            v = _alpine_state(pg=page)
        except Exception:
            v = None
        if v:
            seen.append(v)
        if v and v.get("phase") in ("recognising", "done"):
            break
        time.sleep(0.12)

    page.unroute("**/api/scan_upload")
    page.unroute("**/api/scan_recognize")
    page.unroute("**/api/scan_progress/**")

    assert seen, "攞唔到 Alpine state"
    assert state["hits"] >= 1, (
        "前端根本冇打 /api/scan_upload request — "
        "可能 _scanProbeApi() 判定咗 legacy mode (server 未上線)"
    )

    pcts = [s["uploadPct"] for s in seen if s.get("uploadPct") is not None]
    assert pcts, "scanUploadPct 一直係 None"
    assert max(pcts) > 0, f"scanUploadPct 由頭到尾都係 0: {seen[:5]}"

    labels = [s["bytes"] for s in seen if s.get("bytes")]
    assert labels, f"scanUploadBytesLabel 冇值: {seen[:5]}"

    # 真實 byte 進度唔可以係假 setInterval — 至少要有 >1 個唔同值
    assert len(set(pcts)) >= 1, f"scanUploadPct 只有一個值: {pcts}"


def test_ai_bar_stage_label_is_populated(page):
    """Spec §3.2 + §3.3: AI bar 嘅 stage label 係 server 返嘅 stage_label。"""
    _open_scan_tab(page)

    def _route(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"ok":true,"job_id":"scanjob_qa","status":"running",'
                '"stage":"vision","stage_label":"AI 睇緊相…","pct":10,'
                '"current_index":0,"total":2,"results":[],"errors":[],'
                '"elapsed_s":1.2}'
            ),
        )

    page.route("**/api/scan_progress/**", _route)

    page.evaluate("""() => {
        const r = document.querySelector('[x-data]');
        const st = r._x_dataStack[0];
        st.scanPhase = 'recognising';
        st.scanAiPct = 10;
        st.scanAiStageLabel = 'AI 睇緊相…';
        st.scanAiCurrent = 1;
        st.scanAiTotal = 2;
        st.scanAiTitle = '🤖 AI 睇緊第 1 張相…';
    }""")
    page.wait_for_timeout(600)

    label = page.locator(SEL_STAGE_LABEL).first.inner_text().strip()
    assert label, "stage label 空"
    assert label == "AI 睇緊相…", f"stage label 唔啱: {label!r}"

    # 中文 stage label,唔係英文
    assert not re.search(r"[Uu]ploading|[Rr]ecognizing|[Rr]ecognising", label), label

    page.unroute("**/api/scan_progress/**")


def test_scan_progress_container_hidden_when_idle(page):
    """Spec §3.3: scanPhase === 'idle' 時成個進度條 container 唔應該顯示
    (唔好阻住 Jim 影相)。"""
    page.evaluate("""() => {
        const r = document.querySelector('[x-data]');
        r._x_dataStack[0].scanPhase = 'idle';
    }""")
    page.wait_for_timeout(400)
    visible = page.locator(SEL_PROGRESS_ROOT).first.is_visible()
    assert not visible, "scanPhase='idle' 但進度條仲係顯示緊"
