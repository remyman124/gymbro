"""PWA icon 完整性守衛(v3.3.43 — 2026-10-26)。

背景:`templates/index.html` 同 `/manifest.json` 都引用住 4 個 PWA icon
(`/static/gymbro_{favicon-32,icon-192,icon-512,apple-touch-icon}.png`),
但呢啲檔案**從來冇 commit 落 git** —— 佢哋淨係出現過喺
`/home/work/.hermes/image_cache`,一個會被 prune 嘅 cache dir。

後果:每次 prune 之後,4 個 icon 永久 404(每次 page load 都係),
iOS「加入主畫面」更加係拎唔到 icon。而家 icon 擺喺 repo 嘅 `static/`
(有 git 追蹤),`/static/<path>` route 先由 repo 搵,搵唔到先 fallback。

呢個守衛確保:
1. 4 個 icon 檔案真係喺 repo 入面(唔係淨係喺 cache)
2. 檔案係真嘅 PNG,尺寸啱
3. index.html + manifest.json 引用嘅每一個 /static/ 路徑都有對應檔案
"""
from pathlib import Path

import pytest

REPO = Path("/home/work/projects/gymbro")
STATIC = REPO / "static"
TEMPLATE = REPO / "templates" / "index.html"

REQUIRED_ICONS = {
    "gymbro_favicon-32.png": (32, 32),
    "gymbro_apple-touch-icon.png": (180, 180),
    "gymbro_icon-192.png": (192, 192),
    "gymbro_icon-512.png": (512, 512),
}


@pytest.mark.parametrize("name,expected_size", sorted(REQUIRED_ICONS.items()))
def test_icon_file_exists_in_repo(name, expected_size):
    """Icon 一定要喺 repo 入面(會被 git 追蹤),唔可以只靠 cache dir。"""
    path = STATIC / name
    assert path.is_file(), (
        f"{name} 唔喺 {STATIC} —— PWA icon 又變咗 404。"
        "Icon 必須 commit 落 repo,唔可以擺喺 image_cache(會被 prune)")
    assert path.stat().st_size > 0, f"{name} 係空檔案"


@pytest.mark.parametrize("name,expected_size", sorted(REQUIRED_ICONS.items()))
def test_icon_is_valid_png_with_right_size(name, expected_size):
    """Icon 係真 PNG,而且尺寸同 HTML/manifest 聲稱嘅一致。"""
    Image = pytest.importorskip("PIL.Image", reason="PIL 未裝,skip 尺寸檢查")
    path = STATIC / name
    with Image.open(path) as im:
        assert im.format == "PNG", f"{name} 唔係 PNG(係 {im.format})"
        assert im.size == expected_size, (
            f"{name} 尺寸 {im.size} != 期望 {expected_size}")


def test_every_referenced_static_path_has_a_file():
    """HTML + manifest 引用嘅每個 /static/ 路徑,都要有對應檔案喺 repo。"""
    import re

    refs = set()
    html = TEMPLATE.read_text(encoding="utf-8")
    refs |= set(re.findall(r'"/static/([^"]+)"', html))

    manifest = (REPO / "gym_web.py").read_text(encoding="utf-8")
    refs |= set(re.findall(r'"src":\s*"/static/([^"]+)"', manifest))

    assert refs, "搵唔到任何 /static/ 引用 —— 測試本身壞咗?"

    missing = sorted(r for r in refs if not (STATIC / r).is_file())
    assert not missing, (
        f"呢啲 /static/ 引用喺 repo 冇對應檔案:{missing} —— "
        "HTML/manifest 引用咗但 server 會 404")


def test_static_route_serves_from_repo_first():
    """`/static/<path>` route 要先由 repo 嘅 static/ 搵,先至唔會 404。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    assert "repo_static" in src, (
        "serve_static 冇用 repo 嘅 static/ 目錄 —— "
        "icon 又會跌返去 image_cache(會被 prune)")
    # fallback 仍然要保留(舊嘅 cache 檔仍然要 serve 到)
    assert "image_cache" in src, "serve_static 冇保留 image_cache fallback"
