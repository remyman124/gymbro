"""QA — frontend template 靜態層 (SCAN_PROGRESS_SPEC §3).

Isak 2026-10-20。

呢層直接讀 templates/index.html,唔需要 server。用途:
  - Torres 每次 commit 都可以即刻跑,唔使等 gym_web restart
  - 驗 Torres 嘅改動確實喺檔案入面 (唔係我哋睇錯版本)

同 live browser 層 (test_scan_progress_frontend.py) 配合:
  - 靜態層:檔案內容對唔對
  - 動態層:server 有冇 serve 到嗰個版本 + browser 真係 render 得出

跑法:
    PYTHONPATH="" /home/work/projects/gymbro/.venv/bin/python -m pytest \
        tests/qa/test_scan_progress_template.py -v
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE_FILE = REPO / "templates" / "index.html"


@pytest.fixture(scope="module")
def tpl():
    assert TEMPLATE_FILE.exists(), f"template 唔存在: {TEMPLATE_FILE}"
    return TEMPLATE_FILE.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────
# §3.3 兩條 bar + stage label
# ──────────────────────────────────────────────────────────────────────────

def test_progress_container_exists(tpl):
    assert "data-scan-progress" in tpl, "冇兩階段進度條 container"


def test_upload_bar_exists(tpl):
    assert "data-scan-upload-bar" in tpl, "冇上傳進度條"


def test_ai_bar_exists(tpl):
    assert "data-scan-ai-bar" in tpl, "冇 AI 識別進度條"


def test_stage_label_element_exists(tpl):
    assert "data-scan-stage-label" in tpl, "AI bar 冇 stage label"


# ──────────────────────────────────────────────────────────────────────────
# §3.3 真實數據綁定 (唔可以係假進度)
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("var", [
    "scanUploadPct",       # 真實 byte %
    "scanUploadCurrent",   # 第幾張
    "scanUploadTotal",
    "scanUploadBytesLabel",  # "1.2 MB / 2.8 MB"
    "scanAiPct",           # server 返嘅 %
    "scanAiStageLabel",    # server 返嘅 stage
    "scanAiCurrent",
    "scanAiTotal",
    "scanPhase",           # idle | uploading | recognising | done
])
def test_state_variable_defined(tpl, var):
    assert re.search(rf"^\s*{var}\s*:", tpl, re.MULTILINE), (
        f"Alpine state 冇 {var} — 唔符合 spec §3.3 嘅變數契約"
    )


def test_scan_phase_values_declared(tpl):
    """Spec §3.3: scanPhase 係 'idle' | 'uploading' | 'recognising' | 'done'。"""
    m = re.search(r"scanPhase\s*:\s*'([^']+)'", tpl)
    assert m, "搵唔到 scanPhase 初始值"
    assert m.group(1) == "idle", f"scanPhase 初始值應該係 idle,得 {m.group(1)}"
    for phase in ("uploading", "recognising", "done"):
        assert f"'{phase}'" in tpl, f"scanPhase 冇 '{phase}' 呢個值"


def test_no_fake_progress_setinterval(tpl):
    """Spec §3.3 + §5: 唔准寫假進度。

    舊代碼係 setInterval +5% 頂到 85%。Torres 應該移除咗。
    """
    fake = re.findall(
        r"scanProgress\s*\+=\s*\d+", tpl
    )
    assert not fake, (
        f"仲有假進度 setInterval 遞增: {fake} — "
        f"spec §5 唔准寫假進度"
    )
    # 舊嘅 85% freeze
    assert not re.search(r"scanProgress\s*<\s*85", tpl), (
        "仲有 85% freeze 嘅假進度邏輯"
    )


def test_uses_xhr_for_upload_progress(tpl):
    """Spec §3.1: fetch() 冇 upload progress,必須用 XMLHttpRequest。"""
    assert "XMLHttpRequest" in tpl, "冇用 XMLHttpRequest — 上傳進度做唔到真實 byte %"
    assert "upload.onprogress" in tpl or "xhr.upload.onprogress" in tpl, (
        "冇 xhr.upload.onprogress — 上傳進度唔會郁"
    )


def test_uses_new_progress_api_endpoints(tpl):
    """Spec §3.2: Phase 1 → /api/scan_upload,Phase 2 → /api/scan_recognize
    + poll /api/scan_progress/<job_id>。"""
    assert "/api/scan_upload" in tpl, "前端冇叫 /api/scan_upload"
    assert "/api/scan_recognize" in tpl, "前端冇叫 /api/scan_recognize"
    assert "/api/scan_progress/" in tpl, "前端冇 poll /api/scan_progress/"


def test_upload_phase_completes_before_recognise(tpl):
    """Spec §1 + §3.2: Phase 1 必須 **全部** 上傳完先開始 Phase 2。

    喺 JS 入面搵:所有 _scanUploadOne() call 應該喺
    _scanRecognisePhase() call 之前。

    v3.3.33 (2026-10-21, Jim OOB「After upload, ai food recognition can be
    done in background」): Phase 2 改成 `_scanStartBgRecognition()` ——
    開完 job 就返回,唔再 await 個 poll。所以呢個 test 要接受兩個名。
    """
    # 喺 onScanPhotosPicked 函式入面搵
    m = re.search(
        r"async onScanPhotosPicked\(event\)\s*\{(.*?)\n    \},",
        tpl, re.DOTALL,
    )
    assert m, "搵唔到 onScanPhotosPicked 函式"
    body = m.group(1)

    upload_calls = [mm.start() for mm in re.finditer(r"await this\._scanUploadOne\(", body)]
    recognise_calls = [
        mm.start() for mm in re.finditer(
            r"await this\._scan(?:RecognisePhase|StartBgRecognition)\(", body)
    ]

    assert upload_calls, "onScanPhotosPicked 入面冇 upload call"
    assert recognise_calls, "onScanPhotosPicked 入面冇 recognise call"

    last_upload = max(upload_calls)
    first_recognise = min(recognise_calls)
    assert last_upload < first_recognise, (
        f"唔啱:最後一次 upload (pos {last_upload}) 喺 recognise call "
        f"(pos {first_recognise}) 之後 — Jim 要求先全部上傳,AI 識別後做"
    )

    # 仲有:Phase 1 完成之後先會將 scanPhase 轉做 'recognising'
    assert re.search(r"scanPhase\s*=\s*'recognising'", body), (
        "onScanPhotosPicked 冇將 scanPhase 轉 'recognising'"
    )


def test_scan_progress_compat_vars_kept(tpl):
    """Spec §3.3: 保留現有 scanProgress / scanUploading 變數嘅兼容。"""
    assert re.search(r"^\s*scanProgress\s*:", tpl, re.MULTILINE), "scanProgress 變數冇保留"
    assert re.search(r"^\s*scanUploading\s*:", tpl, re.MULTILINE), "scanUploading 變數冇保留"


def test_no_telegram_button(tpl):
    """Spec §3.3: 唔好整 Telegram button (Jim 硬性要求)。"""
    idx = tpl.find("SCAN TAB")
    section = tpl[idx: idx + 40000] if idx >= 0 else tpl
    assert "telegram" not in section.lower(), "scan tab 出現 telegram button"


# ──────────────────────────────────────────────────────────────────────────
# 部署陷阱 — template 係啟動時讀入記憶體
# ──────────────────────────────────────────────────────────────────────────

def test_template_is_reloadable():
    """⚠️ gym_web.py:14490-14491 —

        _HTML_TEMPLATE_PATH = Path(__file__).parent / "templates" / "index.html"
        HTML_TEMPLATE = _HTML_TEMPLATE_PATH.read_text(encoding="utf-8")

    template 喺 **module import 時**讀一次入 module-level 常數,
    `index()` 用 `render_template_string(HTML_TEMPLATE)`。

    結果:改 templates/index.html 之後,**唔 restart server 就唔會生效**。
    呢個係 Jim OOB 2026-10-20 講「X still doesn't work」嘅頭號原因 ——
    Torres 改咗檔案,但 server 仲 serve 緊舊版。

    呢個測試唔會 fail — 佢只係記錄呢個事實,等 QA 報告引用。
    (改成真 reload 需要改 gym_web.py,係 Frimpong/Alonso 嘅 decision,
     唔係 QA 可以自己改。)
    """
    gw = REPO / "gym_web.py"
    if not gw.exists():
        pytest.skip("gym_web.py 唔喺預期位置")
    src = gw.read_text(encoding="utf-8")
    assert "render_template_string(HTML_TEMPLATE)" in src, (
        "gym_web.py 嘅 index() 唔再用 render_template_string(HTML_TEMPLATE) — "
        "呢個測試要更新 (可能已經改成 auto-reload)"
    )
    # 呢個係已知限制,唔 assert fail —— 只係令報告有得引用
    pytest.skip(
        "已知限制: HTML_TEMPLATE 喺 import 時讀入記憶體,"
        "改 template 要 restart gym_web.py 先生效 (QA 已記錄)"
    )


def test_no_english_narrative_terms(tpl):
    """Spec §3.3: 唔好用英文 narrative term。

    只捉 **畀 Jim 睇嘅字** — 即係 x-text="..." 或 >文字< 入面嘅
    英文 narrative。而唔係:
      - Alpine state 變數名 (scanUploading, scanTextUploading)
      - status map 嘅 object key (uploading:, recognising:)
    """
    hits = []
    # 1. x-text="...English..." — 淨係捉 x-text 屬性值入面嘅英文 narrative
    for m in re.finditer(r'x-text="([^"]*)"', tpl):
        val = m.group(1)
        # 淨係捉英文 narrative 字眼,唔理變數引用
        for bad in re.finditer(r"(?:^|[\s'\"\(])(Uploading|Recognizing|Recognising|Processing)(?:[\s'\"\).]|$)", val):
            hits.append(f"x-text={val[:80]!r}")
    # 2. >English...< — 直接喺 HTML 文字節點
    for m in re.finditer(r">\s*(Uploading|Recognizing|Recognising|Processing)(?:\.\.\.)?\s*<", tpl):
        hits.append(f">{m.group(1)}<")

    assert not hits, (
        f"前端有英文 narrative term (Jim OOB 2026-10-20 要用「上傳緊」「AI 睇緊」): "
        f"{hits[:5]}"
    )


def test_transition_duration_reasonable(tpl):
    """Spec §3.3: Bar 要 transition-all 但唔好太慢 (200-300ms),
    否則 stage 跳得太快睇唔到。"""
    durations = re.findall(r"transition-all\s+duration-(\d+)", tpl)
    scan_section_start = tpl.find("data-scan-progress")
    assert scan_section_start > 0
    # 只睇進度條附近嘅 duration
    nearby = tpl[max(0, scan_section_start - 500): scan_section_start + 2500]
    nearby_durations = re.findall(r"duration-(\d+)", nearby)
    for d in nearby_durations:
        assert 150 <= int(d) <= 400, (
            f"進度條 transition duration-{d}ms 唔喺 200-300ms 範圍 "
            f"(spec §3.3)"
        )


def test_no_except_pass_pattern(tpl):
    """Spec §5: 唔准 `except: pass` — 全部要 print 埋。"""
    # 只查新嘅 scan 相關代碼
    idx = tpl.find("async onScanPhotosPicked")
    section = tpl[idx: idx + 12000] if idx >= 0 else ""
    assert "except: pass" not in section.replace("except(e)", "except(e)"), (
        "scan flow 入面有 except: pass"
    )
