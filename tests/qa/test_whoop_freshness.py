"""WHOOP freshness / band-off semantics — v3.3.35 (2026-10-21).

Jim OOB 2026-10-21 20:0x:
    「Why gymbro is saying my whoop band off?!!! The as of date, make it
      same as data as of data」

兩個 bug,一齊修:

1) **「WHOOP band off」係假警報。**
   舊邏輯由 cycle `updated_at` 嘅年齡推斷「band 除咗身」。實測推翻:
      • 連續 8 個 cycle 嘅 `updated_at` 全部落喺朝早 07:59-08:34 HKT,
        連 31147 步 / strain 18.1 嗰日都一樣 → 佢係「每日 08:00 寫一次
        記錄」嘅 record-level stamp,同 band 有冇戴完全無關;
      • 2026-10-21 三次 live pull(11:22 / 11:45 / 11:46)步數由 1042 升到
        1342,而 `updated_at` 一直企喺 08:14:53。
   → 所以舊邏輯**每晚 08:30 之後一定報「band off」**,無論 band 有冇戴。
   而家改成講真話:「個步數連續幾耐冇郁過」(>3h)→ badge 寫「步數冇增加」,
   唔再亂指住個 band 嘅狀態。

2) **同一份資料兩個「as of」。**
   `/api/health` 用 `_meta.sync_time_epoch`(一個已經唔存在嘅 script 留低嘅
   遺物,永遠停喺舊時間)→ 報 65 分鐘;app 用檔案 mtime → 報「as of 19:51」。
   Jim OOB:「make it same as data as of data」。
   而家:`data_as_of` = **個步數最後一次變化嘅時間**(data 嘅真正 as-of),
   `/api/health` 嘅 cache age = 檔案 mtime,兩邊一致。
"""

import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HKT = timezone(timedelta(hours=8))
REPO = Path("/home/work/projects/gymbro")
WHOOP_TOOL = Path("/home/work/tools/whoop_nutrition.py")


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _cycle(start, end, steps, updated_at):
    c = {
        "id": 1,
        "start": _iso(start),
        "end": _iso(end) if end else None,
        "timezone_offset": "+08:00",
        "score_state": "SCORED",
        "score": {"strain": 8.0, "kilojoule": 3000.0},
    }
    if steps is not None:
        c["step_count"] = steps
    if updated_at is not None:
        c["updated_at"] = _iso(updated_at)
    return c


@pytest.fixture
def cache_file():
    """A throwaway WHOOP cache file (never Jim's real one)."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)
    yield path
    try:
        path.unlink()
    except OSError:
        pass


def _write(path, cycles, meta=None, mtime=None):
    payload = {"cycles": cycles, "recovery": [], "sleep": [], "workouts": []}
    if meta is not None:
        payload["_meta"] = meta
    path.write_text(json.dumps(payload))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _hist(*pairs):
    """steps_history entries: (seconds_ago, steps)."""
    now = time.time()
    return [{"ts": now - ago, "steps": steps} for ago, steps in pairs]


# ── 1) 舊嘅 band-off 推論已經唔存在 ──────────────────────────────────────

def test_stale_source_stamp_does_not_flag_band_off(cache_file):
    """REGRESSION: a stale `updated_at` alone must never raise "band off"."""
    from gym_web.whoop import whoop_get_steps

    now = datetime.now(timezone.utc)
    cycles = [_cycle(now - timedelta(hours=10), None, 3364,
                     now - timedelta(hours=9, minutes=54))]
    _write(cache_file, cycles)

    r = whoop_get_steps(now=now, cache_path=cache_file)
    assert r["steps"] == 3364
    assert r["band_off"] is False
    assert r["band_off_basis"] == "steps_frozen"
    # source 嘅年齡仍然保留(做診斷用),但唔再驅動 band 判斷
    assert r["source_age_min"] == 594


def test_frozen_count_flags_band_off(cache_file):
    """個步數 >3h 冇郁過 → band_off(語意 = 個數停咗,唔係 band 除咗身)。"""
    from gym_web.whoop import whoop_get_steps

    now = datetime.now(timezone.utc)
    cycles = [_cycle(now - timedelta(hours=10), None, 3364,
                     now - timedelta(hours=9, minutes=54))]
    _write(cache_file, cycles,
           meta={"steps_history": _hist((60, 3364), (7200, 3364), (15000, 3364))})

    r = whoop_get_steps(now=now, cache_path=cache_file)
    assert r["band_off"] is True
    assert r["steps_frozen_min"] >= 240


# ── 2) as-of 語意 ─────────────────────────────────────────────────────────

def test_data_as_of_is_the_last_change(cache_file):
    """data_as_of = 個數最後一次變化嘅時間(唔係 pull 時間,唔係 source)。"""
    from gym_web.whoop import whoop_get_steps

    now = datetime.now(timezone.utc)
    cycles = [_cycle(now - timedelta(hours=6), None, 2286, now - timedelta(minutes=5))]
    _write(cache_file, cycles,
           meta={"steps_history": _hist((600, 1000), (300, 1500), (30, 2286))})

    r = whoop_get_steps(now=now, cache_path=cache_file)
    assert r["last_change_known"] is True
    assert r["steps_frozen_min"] <= 2
    expected = (now - timedelta(seconds=30)).astimezone(HKT).strftime("%Y-%m-%dT%H:%M")
    assert r["data_as_of"].startswith(expected)


def test_pull_age_and_source_age_are_separate(cache_file):
    """age_min = 幾耐之前 pull 過;source_age_min = source 自己幾耐冇更新。"""
    from gym_web.whoop import whoop_get_steps

    now = datetime.now(timezone.utc)
    cycles = [_cycle(now - timedelta(hours=6), None, 2286, now - timedelta(hours=2))]
    old_mtime = time.time() - 3600
    _write(cache_file, cycles, mtime=old_mtime)

    r = whoop_get_steps(now=now, cache_path=cache_file)
    assert r["age_min"] in (59, 60)           # pull 咗 1 個鐘前
    assert r["source_age_min"] == 120         # source 2 個鐘前
    assert r["stale"] is True                 # pull 舊過 30 分鐘


# ── 3) writer 一定要每次都重新蓋章 _meta(否則 health age 會停喺舊時間)──

def test_history_only_records_changes_not_every_pull():
    """v3.3.35+: 步數歷史**只記真變化**。

    如果每次 pull 都記(即使值一樣),96 條只夠 8 個鐘(app 每 5 分鐘 pull
    一次)→ 真正嘅「變化邊界」會被剪走,as-of 就會慢慢飄向「而家」,
    變返 v3.3.31 嗰個「講大話」嘅 as-of。
    """
    src = WHOOP_TOOL.read_text(encoding="utf-8")
    assert "_last_steps != _cur_steps" in src, \
        "writer 唔係只記變化(會令 96 條 history 只夠 8 個鐘)"
    assert "steps_last_seen_epoch" in src, "writer 冇記 last_seen"


def test_canonical_pull_script_preserves_and_extends_history():
    """v3.3.36: `whoop_pull_inline.py` 係第三個 writer。

    佢以前**覆寫**成個 cache → 靜靜哋 wipe 咗 `_meta.steps_history`
    (2026-10-21 20:16 實測:跑完 history 只剩 3 條,而且「變化邊界」唔見咗)。
    而家佢要 merge + 保留 + 延伸。
    """
    script = Path("/home/work/.hermes/skills/fitness/whoop/scripts/whoop_pull_inline.py")
    src = script.read_text(encoding="utf-8")
    assert "_prev_meta.get(\"steps_history\")" in src or \
        '_prev_meta.get("steps_history")' in src, \
        "canonical pull script 冇保留 steps_history"
    assert "steps_history" in src, "canonical pull script 完全冇提及 steps_history"
    # 一定要 merge(唔可以淨係覆寫)
    assert "cache.setdefault(_k, _v)" in src, \
        "canonical pull script 仍然係覆寫式寫 cache(會 wipe 其他 writer 嘅嘢)"


def test_writer_restamps_meta_on_every_sync():
    """`whoop_nutrition.py --sync` 每次寫入都要更新 sync_time_* + 步數歷史。

    v3.3.31 之後 `_meta.sync_time_epoch` 係一個**已經唔存在嘅 script**
    (whoop_pull_inline.py)留低嘅遺物 —— 永遠停喺舊時間,令 /api/health 報
    錯嘅 cache age(65 分鐘)而 app 報「as of 19:51」。
    """
    src = WHOOP_TOOL.read_text(encoding="utf-8")
    assert "sync_time_epoch" in src, "writer 唔再寫 sync_time_epoch"
    assert "steps_history" in src, "writer 唔再記步數變化史"
    # 每次 sync 都要重新蓋章(唔係淨係第一次)
    assert src.count('"sync_time_epoch"') >= 2, "sync_time_epoch 只出現一次"
    assert "_meta[\"steps_history\"]" in src or '"steps_history": _hist' in src \
        or "steps_history" in src


def test_health_cache_age_uses_file_mtime_not_stale_meta(cache_file):
    """health endpoint: cache age = 檔案 mtime,唔係 `_meta` 個舊 epoch。"""
    import re
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    # health endpoint 一定要以 mtime 為準
    idx = src.find('"whoop_cache_age_minutes"')
    assert idx > 0, "搵唔到 whoop_cache_age_minutes"
    window = src[max(0, idx - 2500):idx]
    assert "stat().st_mtime" in window, \
        "health cache age 冇用檔案 mtime(會用 _meta 個遺物 epoch)"
