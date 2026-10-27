"""Runtime test — v3.3.52: 平行掃描唔可以撞名。

呢個係**真正執行** helper 嘅測試(唔係 grep source),直接驗證:
  1. 同一秒內連續叫 N 次 → N 個唔同檔名。
  2. 檔名真係落喺 SCAN_CACHE_DIR,而且唔會撞到已存在嘅檔。
  3. 模擬「3 個平行 worker 同一秒開工」嘅真實場景 →
     4 張相寫落 4 個唔同檔案,md5 全部唔同(冇相被吞)。
  4. 檔名仍然保留 timestamp(方便除錯 + 時間排序)。
"""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_gym_web():
    """按路徑載入 gym_web.py(唔好用 `import gym_web` —— 會解析到
    gym_web/ package,唔係 gym_web.py)。"""
    spec = importlib.util.spec_from_file_location("gym_web_mod", ROOT / "gym_web.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gym_web_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gw():
    return _load_gym_web()


def test_same_second_yields_different_names(gw):
    """同一秒內連續派生 200 個名 → 全部唔同。"""
    when = datetime(2026, 10, 27, 20, 1, 20, tzinfo=timezone(timedelta(hours=8)))
    names = [gw._unique_scan_name("scan", ".jpg", when) for _ in range(200)]
    assert len(set(names)) == 200, (
        "同一秒派生嘅名撞咗:%d 個重複" % (200 - len(set(names)))
    )


def test_name_has_timestamp_prefix(gw):
    """名要保留 timestamp(方便除錯 + 時間排序)。"""
    when = datetime(2026, 10, 27, 20, 1, 20, tzinfo=timezone(timedelta(hours=8)))
    name = gw._unique_scan_name("scan", ".jpg", when)
    assert name.startswith("scan_20261027_200120_"), name
    assert name.endswith(".jpg"), name


def test_name_does_not_exist_on_disk(gw):
    """派生嘅名唔應該已經存在喺 disk。"""
    name = gw._unique_scan_name("scan", ".jpg")
    assert not (gw.SCAN_CACHE_DIR / name).exists(), name


def test_helper_avoids_existing_file(gw, tmp_path, monkeypatch):
    """如果個名已經存在 → 必須派生另一個(唔可以靜靜哋撞)。"""
    monkeypatch.setattr(gw, "SCAN_CACHE_DIR", tmp_path)
    when = datetime(2026, 10, 27, 20, 1, 20, tzinfo=timezone(timedelta(hours=8)))
    first = gw._unique_scan_name("scan", ".jpg", when)
    (tmp_path / first).write_bytes(b"x")
    second = gw._unique_scan_name("scan", ".jpg", when)
    assert second != first, "helper 撞咗一個已經存在嘅檔名"
    assert not (tmp_path / second).exists()


def test_parallel_workers_same_second_do_not_collide(gw, tmp_path, monkeypatch):
    """重現 Jim 嘅 bug:4 張相、3 個平行 worker、全部同一秒開工。

    舊 code 嘅行為:4 張相 → 只有 2 個 final 檔(2 張被吞),
    而且 4 個 row 指向同一個 URL → 縮圖全部一樣。

    新 code:4 張相 → 4 個唔同檔案,內容全部唔同。
    """
    monkeypatch.setattr(gw, "SCAN_CACHE_DIR", tmp_path)
    when = datetime(2026, 10, 27, 20, 1, 20, tzinfo=timezone(timedelta(hours=8)))

    # 4 張「相」(內容唔同,simulate 不同真實檔案)
    payloads = [b"PHOTO-A" * 100, b"PHOTO-B" * 100, b"PHOTO-C" * 100, b"PHOTO-D" * 100]

    written = []
    for payload in payloads:
        name = gw._unique_scan_name("scan", ".jpg", when)
        path = tmp_path / name
        path.write_bytes(payload)          # 等同 shutil.copyfile(img_path, final_path)
        written.append((name, path))

    # 4 個唔同檔名
    assert len(set(n for n, _ in written)) == 4, (
        "平行 worker 撞名:%s" % [n for n, _ in written]
    )

    # 4 個檔案都仲喺度,而且內容冇被覆蓋
    assert len(list(tmp_path.glob("scan_*.jpg"))) == 4
    contents = [p.read_bytes() for _, p in written]
    assert len(set(contents)) == 4, "有相被覆蓋咗(內容一樣)"

    # 每個 URL 唔同 → 縮圖唔會一樣
    urls = {f"/scan_img/{n}" for n, _ in written}
    assert len(urls) == 4, "URL 重複 → 縮圖會全部一樣"
