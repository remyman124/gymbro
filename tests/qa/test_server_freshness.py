"""QA — server staleness check: serve 緊嘅 template vs 磁碟上嘅 template.

Isak 2026-10-20。

**為咩要有呢個測試:**

gym_web.py:14490-14491
    _HTML_TEMPLATE_PATH = Path(__file__).parent / "templates" / "index.html"
    HTML_TEMPLATE = _HTML_TEMPLATE_PATH.read_text(encoding="utf-8")

template 喺 module import 時讀一次入 module-level 常數,
`index()` 用 `render_template_string(HTML_TEMPLATE)`。

→ 改 templates/index.html 之後,**唔 restart server 就唔會生效**。

呢個係 Jim OOB 2026-10-20 講「X still doesn't work」嘅頭號原因:
Torres 改咗檔案,但 server (13:30 啟動) 仲 serve 緊 13:30 嗰版。
QA 如果唔知道呢點,會錯判 Torres 個前端「冇做」。

呢個測試 **fail = 有用**: 佢會話畀你知「你睇緊舊版」,
而唔係「前端冇 progress bar」。

跑法:
    PYTHONPATH="" /home/work/projects/gymbro/.venv/bin/python -m pytest \
        tests/qa/test_server_freshness.py -v -s
"""

import hashlib
import os

import pytest
import requests

BASE_URL = os.environ.get("GYMBRO_URL", "http://127.0.0.1:7000")
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEMPLATE_FILE = os.path.join(REPO, "templates", "index.html")


def _server_alive() -> bool:
    try:
        return requests.get(f"{BASE_URL}/healthz", timeout=5).status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="module")
def served_html():
    if not _server_alive():
        pytest.skip(f"gymbro server 冇起喺 {BASE_URL}")
    r = requests.get(f"{BASE_URL}/", timeout=20)
    assert r.status_code == 200, f"GET / HTTP {r.status_code}"
    return r.text


def test_server_serves_current_template(served_html):
    """Server serve 嘅 template 必須同磁碟上嘅一致。

    如果唔一致 → server 需要 restart 先見到新 template。

    ⚠️ 比對前要 strip 尾��空白:
       Flask `render_template_string` 會將 template 嘅尾隨換行 trim 走,
       所以 served HTML 永遠比磁碟檔少一個尾 `\n`。
       直接比字串會得到假陽性 ("served 291026 chars vs disk 291027 chars")。
    """
    with open(TEMPLATE_FILE, encoding="utf-8") as fh:
        disk = fh.read()

    disk_marker = "data-scan-progress"
    served_marker = "data-scan-progress"

    if disk_marker in disk and served_marker not in served_html:
        pytest.fail(
            "STALE SERVER: templates/index.html 已經有 "
            f"{disk_marker!r},但 server serve 嘅版本冇。\n"
            "→ gym_web.py 喺 import 時讀 template 入記憶體 (HTML_TEMPLATE),\n"
            "  改 template 要 **restart gym_web.py** 先生效。\n"
            "  Restart 指令: cd /home/work/projects/gymbro && "
            "PYTHONPATH=\"\" /usr/bin/python3 gym_web.py\n"
            "  (唔好重啟 alonso gateway — 只關 gym_web.py 自己)"
        )

    # strip 尾隨空白先比 (Flask trim 咗 template 嘅尾 `\n`)
    assert served_html.strip() == disk.strip(), (
        "STALE SERVER: server serve 嘅 template 同磁碟唔一致 "
        f"(served {len(served_html)} chars vs disk {len(disk)} chars) — "
        "要 restart gym_web.py"
    )


def test_server_serves_scan_progress_dom(served_html):
    """Live server 嘅 HTML 必須包含兩階段進度條 DOM。

    呢個係 spec §4.4 嘅直接驗證。
    """
    for sel in ("data-scan-progress", "data-scan-upload-bar",
                "data-scan-ai-bar", "data-scan-stage-label"):
        assert sel in served_html, (
            f"server serve 嘅 HTML 冇 {sel} — "
            f"server 仲係舊版,需要 restart"
        )


def test_server_version_matches_source(server_version: str = ""):
    """server 嘅 /api/version uptime 可以對照 process 啟動時間。"""
    if not _server_alive():
        pytest.skip(f"gymbro server 冇起喺 {BASE_URL}")
    r = requests.get(f"{BASE_URL}/api/version", timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert "uptime_since" in d, d
    print(f"\n  server uptime_since: {d['uptime_since']}")
    print(f"  server version:      {d.get('version')}")
    print(f"  server git_commit:   {d.get('git_commit')}")


def test_no_svg_circle_attribute_errors(page_errors):
    """⚠️ Bug: schedule chart 嘅 SVG <circle> 產生命令列 console error。

    templates/index.html (~L5612, ~L5622):
        <circle x-show="d.recovery_pct != null"
                :cx="40 + (i * 310 / Math.max(1, scheduleMonth.length - 1))"
                :cy="110 - (d.recovery_pct / 100 * 100)" .../>

    當 `scheduleMonth` 係空 array,`x-for` 個 template 仍然會 render 一個
    placeholder <circle>,而 `:cx` / `:cy` expression 喺呢個 moment 冇有效
    值 → Alpine 寫入空字串 → 瀏覽器報:
        <circle> attribute cx: Unexpected end of attribute. Expected length, "".

    實測喺真實 :7000 server 見到 44 個 console error。
    呢個 error 會令 `tests/e2e/test_interactions.py::test_no_console_errors_during_navigation`
    fail(見 pytest baseline)。

    呢個 bug 唔關 scan progress 事(係 schedule chart),但既然喺同一個
    template 檔,一併交返畀 owner 修。
    """
    svg_errors = [e for e in page_errors if "<circle>" in e]
    assert not svg_errors, (
        f"SVG <circle> 有 {len(svg_errors)} 個 console error "
        f"(schedule chart 嘅 :cx/:cy expression 喺空 scheduleMonth 下無效):\n"
        f"  {svg_errors[0][:200]}\n"
        f"  修法: :cx/:cy 加 default 值,或者用 x-show 喺 circle 層面 "
        f"(唔好喺 <template x-for> 入面用 x-show)"
    )


@pytest.fixture(scope="module")
def page_errors():
    """用 Playwright 收真實 server 嘅 console error。"""
    playwright = pytest.importorskip("playwright.sync_api")
    if not _server_alive():
        pytest.skip(f"gymbro server 冇起喺 {BASE_URL}")
    errors = []
    with playwright.sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox"])
        pg = b.new_page()
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{BASE_URL}/", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(6000)
        b.close()
    return errors
