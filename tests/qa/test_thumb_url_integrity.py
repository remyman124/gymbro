"""v3.3.38 regression — 縮圖「系統性」問題:URL 唔係內容定址 + trashed 檔派錯圖。

2026-10-22 第二次故障 (Jim OOB「The thumbnail are the same again. Pls fix the
thumbnail and the systemic problem.」)+ OOB「Do a deeper research」):

深查結果(全部實測):
  A. 伺服器每行派嘅縮圖 byte **全部唔同**(md5 24 行 24 個)→ 唔係渲染 bug
  B. Sheet column K 冇重複(除咗 2026-08 一對舊重複)、冇一行指向已被 trash
     嘅檔 → Drive/Drive-placeholder 唔係今次主因
  C. /api/scan_recent 嘅 scan_index / image_url / thumbnail_url / Alpine :key
     **全部唯一** → 唔係資料重複
  D. 前端 DOM 每行嘅 src **全部唔同** → 唔係 template 綁錯
  E. 但實測 `loading="lazy"` 嘅食物歷史縮圖 **從來冇發出過 request**
     (naturalWidth=0 / complete=false;改成 eager 即刻 20/20 全部載入)
     → 每一行都係同一個空白/失敗框 → Jim 見到「縮圖一樣」
  F. `/scan_thumb/<row_index>` 呢條 URL **唔係 immutable**:刪行 → row 移位、
     換相 → 檔案換咗,但 URL 冇變 → 客戶端(手機瀏覽器 / iOS 圖片解碼快取)
     可以繼續顯示舊圖 → 同一類「縮圖一樣」會無限復發

v3.3.38 修法(系統性,唔係逐個 case 打補丁):
  1. 內容定址 URL:`?v=<drive file id 嘅短 hash>` → 檔案換咗/移位 → URL 變
  2. 內容定址 URL → `Cache-Control: public, max-age=31536000, immutable`
     (舊 URL 仍然 no-store)
  3. trashed 守衛:派圖之前查 Drive 檔案係咪已被 trash → 係就 404,
     唔再派 Drive 嘅「垃圾桶」placeholder
  4. 前端食物歷史縮圖移除 loading="lazy"
  5. SW cache 版本 144 → 145(清走手機上面所有舊快取)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from gym_web import image_proxy  # noqa: E402
from gym_web.cache import _content_version  # noqa: E402

DRIVE_A = "https://drive.google.com/uc?export=view&id=FILE_A"
DRIVE_B = "https://drive.google.com/uc?export=view&id=FILE_B"


# ---------------------------------------------------------------- A. 版本號
def test_content_version_stable_for_same_file():
    assert _content_version(DRIVE_A) == _content_version(DRIVE_A)
    assert _content_version(DRIVE_A) == _content_version(
        "https://drive.google.com/file/d/FILE_A/view")


def test_content_version_changes_when_file_changes():
    assert _content_version(DRIVE_A) != _content_version(DRIVE_B)


def test_content_version_empty_for_text_only():
    assert _content_version("") == ""


def test_content_version_extracts_id_from_url_shapes():
    assert _content_version(DRIVE_A) == _content_version(
        "https://drive.google.com/uc?export=view&id=FILE_A&sz=w1000")
    assert _content_version("https://drive.google.com/file/d/FILE_A/view?usp=sharing") \
        == _content_version(DRIVE_A)


# ---------------------------------------------------------------- B. API 形狀
def test_pwa_dict_uses_content_addressed_urls():
    from gym_web.cache import NutritionRow
    row = NutritionRow.from_sheet_row(
        row_index=12,
        cells=["2026-10-22", "12:00", "午餐", "海南雞白飯", "諾富特",
               "500", "33", "55", "15", "scan_food", DRIVE_A],
    )
    d = row.to_pwa_dict()
    v = _content_version(DRIVE_A)
    assert d["thumbnail_url"] == f"/scan_thumb/12?v={v}"
    assert d["image_url"] == f"/scan_thumb/12?v={v}"


def test_pwa_dict_text_only_row_has_no_version():
    from gym_web.cache import NutritionRow
    row = NutritionRow.from_sheet_row(
        row_index=13,
        cells=["2026-10-22", "12:00", "午餐", "白飯", "", "260", "6", "56", "17", "", ""],
    )
    d = row.to_pwa_dict()
    assert "?v=" not in (d.get("thumbnail_url") or "")
    assert "?v=" not in (d.get("image_url") or "")


# ---------------------------------------------------------------- C. cache header
def test_img_headers_immutable_only_for_content_addressed():
    assert "immutable" in image_proxy._img_headers(True)["Cache-Control"]
    assert image_proxy._img_headers(True).get("X-Gymbro-Immutable") == "1"
    assert "no-store" in image_proxy._img_headers(False)["Cache-Control"]
    assert "X-Gymbro-Immutable" not in image_proxy._img_headers(False)


# ---------------------------------------------------------------- D. trashed 守衛
@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(image_proxy.bp)
    return app.test_client()


def test_trashed_drive_file_returns_404_not_placeholder(client, monkeypatch):
    """已被 trash 嘅 Drive 檔 → 404(唔派 placeholder)。"""
    monkeypatch.setattr(image_proxy, "_resolve_drive_url", lambda ri: DRIVE_A)
    monkeypatch.setattr(image_proxy, "_drive_file_trashed", lambda url: True)
    resp = client.get("/scan_thumb/12")
    assert resp.status_code == 404


def test_live_drive_file_is_served(client, monkeypatch):
    """正常檔案照派(守衛唔可以阻住正常圖)。"""
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (200, 30, 30)).save(buf, format="JPEG")
    monkeypatch.setattr(image_proxy, "_resolve_drive_url", lambda ri: DRIVE_A)
    monkeypatch.setattr(image_proxy, "_drive_file_trashed", lambda url: False)
    monkeypatch.setattr(image_proxy, "_fetch_drive_bytes", lambda url: buf.getvalue())
    resp = client.get("/scan_thumb/12")
    assert resp.status_code == 200
    assert resp.mimetype == "image/jpeg"


def test_trashed_check_fails_open_when_unknown(monkeypatch):
    """查唔到 trashed 狀態 → 唔阻住派圖(fail open)。"""
    monkeypatch.setattr(image_proxy, "_resolve_drive_url", lambda ri: DRIVE_A)
    seen = {}

    def fake(url):
        seen["url"] = url
        return None

    monkeypatch.setattr(image_proxy, "_drive_file_trashed", fake)
    assert image_proxy._drive_file_trashed(DRIVE_A) is None
    assert seen["url"] == DRIVE_A


def test_file_id_extraction():
    assert image_proxy._file_id_from_drive_url(DRIVE_A) == "FILE_A"
    assert image_proxy._file_id_from_drive_url(
        "https://drive.google.com/file/d/FILE_B/view") == "FILE_B"
    assert image_proxy._file_id_from_drive_url("") == ""


# ---------------------------------------------------------------- E. 前端 / SW
def test_food_history_thumbnail_not_lazy():
    """食物歷史縮圖唔可以用 loading="lazy"(實測:從來冇 load 過)。

    只睇 <img> tag 本身(唔理 HTML註釋,註釋會提到個字眼)。
    """
    html = (REPO / "templates" / "index.html").read_text()
    anchor = html.find("scan.thumbnail_url || scan.image_url")
    assert anchor > 0
    tag_start = html.rfind("<img", 0, anchor)
    tag_end = html.find(">", anchor)
    img_tag = html[tag_start:tag_end]
    assert "loading=" not in img_tag, img_tag
    assert "decoding=\"async\"" in img_tag, img_tag


def test_service_worker_cache_version_bumped():
    src = (REPO / "gym_web.py").read_text()
    assert "gym-web-v154" in src
    assert "gym-web-v145" not in src


def test_service_worker_content_addressed_cache_first():
    src = (REPO / "gym_web.py").read_text()
    assert "url.searchParams.get('v')" in src
    assert "caches.open(CACHE).then(cache => cache.match(e.request)" in src


def test_after_request_respects_immutable_marker():
    src = (REPO / "gym_web.py").read_text()
    assert 'X-Gymbro-Immutable' in src
    assert re.search(r'X-Gymbro-Immutable.*==\s*"1"', src, re.S)


# ---------------------------------------------------------------- F. 本地 Alpine
def test_alpine_is_vendored_locally():
    """Alpine 唔可以再靠 CDN(CDN 慢 → 成個 app 空 → 縮圖全部空框)。"""
    html = (REPO / "templates" / "index.html").read_text()
    assert 'src="/js/alpine-3.17.5.min.js"' in html
    # CDN 只可以留做後備(onerror fallback)
    assert html.count("unpkg.com/alpinejs") == 1
    assert "onerror=" in html.split("alpine-3.17.5.min.js")[1][:200]
    local = REPO / "static" / "js" / "alpine-3.17.5.min.js"
    assert local.exists()
    assert local.stat().st_size > 10_000
    assert 'version:"3.17.5"' in local.read_text()


def test_alpine_route_served_in_gym_web():
    src = (REPO / "gym_web.py").read_text()
    assert "/js/alpine-3.17.5.min.js" in src
    assert "_ALPINE_LOCAL" in src


def test_card_row_index_parsing_strips_content_version():
    """餐牌 JPG 解析 row index 必須剝走 ?v=<hash>,否則縮圖會喺卡上消失。"""
    src = (REPO / "gym_web.py").read_text()
    assert 'split("?", 1)[0]' in src
    assert src.count('split("?", 1)[0]') >= 2


# ---------------------------------------------------------------- G. 診斷工具
def test_integrity_script_exists():
    p = REPO / "scripts" / "check_thumb_integrity.py"
    assert p.exists()
    compile(p.read_text(), str(p), "exec")
