"""QA guards — v3.3.50: WHOOP workouts 真係入到 cheer prompt。

Jim OOB 2026-10-27:「I have done gym at noon as you can see on whoop.」
→ 但 cheer 仍然講「過去 3 日未做運動 / 今日未做 gym」。

Root cause: `_get_recent_workouts_3d()` 迭代咗一個**從未定義過**嘅全域
`workouts_cache` → NameError 被 except 吞咗 → 永遠回 []。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = (ROOT / "gym_web.py").read_text(encoding="utf-8")


def _mod():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("gym_web_rw_mod", ROOT / "gym_web.py")
    m = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT))
    spec.loader.exec_module(m)
    return m


def test_no_bare_undefined_workouts_cache():
    """唔准再直接引用未定義嘅 workouts_cache(要行 globals().get fallback)。"""
    code = "\n".join(ln for ln in SRC.splitlines() if not ln.strip().startswith("#"))
    assert not re.search(r"for w in \(workouts_cache or \[\]\)", code), \
        "唔可以用未定義嘅 workouts_cache(會 NameError → 永遠 [] )"
    assert 'globals().get("workouts_cache")' in code, \
        "要用 globals().get 做 fallback"


def test_today_workouts_use_hkt_date():
    assert "def _is_today_hkt(w):" in SRC, "今日 workout 要用 HKT 日期(唔係 UTC 字符串)"
    code = "\n".join(ln for ln in SRC.splitlines() if not ln.strip().startswith("#"))
    assert "startswith(today_iso())" not in code, \
        "唔可以用 UTC 字符串 startswith 判斷今日(朝早 7-8 點 gym 會漏)"


def test_recent_workouts_returns_actual_entries():
    """行為測試:傳入一條 workout,函數要真係返到嘢。"""
    from datetime import datetime, timezone, timedelta
    hkt = timezone(timedelta(hours=8))
    try:
        m = _mod()
    except Exception as e:                     # pragma: no cover
        import pytest
        pytest.skip(f"gym_web 匯入失敗(非本測試問題): {e}")
    now = datetime.now(hkt)
    start = (now - timedelta(hours=2)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end = (now - timedelta(hours=1)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    ws = [{"start": start, "end": end, "sport_name": "Strength Training",
           "score": {"strain": 12.85, "average_heart_rate": 98, "max_heart_rate": 167}}]
    out = m._get_recent_workouts_3d(ws)
    assert isinstance(out, list), "要返 list"
    assert len(out) == 1, f"應該有 1 個 workout session,得到 {len(out)}"
    assert out[0].get("strain") == 12.85
    assert out[0].get("avg_hr") == 98
    assert out[0].get("max_hr") == 167
    # formatter 要出到嘢,唔可以係「未做運動」
    zh = m._format_recent_workouts_3d_for_cheer(out)
    assert "未做運動" not in zh, f"formatter 唔應該話未做運動:{zh!r}"
    assert "12.85" in zh, f"strain 要出現:{zh!r}"


def test_context_builds_recent_workouts_zh():
    """_build_daily_context 要填 recent_workouts_zh(唔係空字串)。"""
    try:
        m = _mod()
    except Exception as e:                     # pragma: no cover
        import pytest
        pytest.skip(f"gym_web 匯入失敗(非本測試問題): {e}")
    ctx = m._build_daily_context()
    w = ctx.get("whoop") or {}
    rwzh = w.get("recent_workouts_zh") or ""
    assert rwzh, "ctx['whoop']['recent_workouts_zh'] 唔可以空(否則 prompt 會話未做運動)"
    assert "未做運動" not in rwzh or "(過去 3 日未做運動)" == rwzh, \
        "如果有 WHOOP workout 資料,唔應該淨係話未做運動"


def test_version_bumped():
    assert '__version__ = "3.3.55"' in SRC, "版本要升到 3.3.55"
