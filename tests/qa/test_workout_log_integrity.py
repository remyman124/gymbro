"""v3.3.40 regression — 本機 workout log 嘅 key ↔ inner `date` 必須對齊。

2026-10-23 21:00 HKT 實測故障 (cron brain-improving 巡檢時發現):

    /home/work/.whoop_workout_log.json 嘅 `2026-10-23` key 入面,
    裝住一節 `date: "2026-10-05"` 嘅 session。而真正嘅 `2026-10-05`
    key 已經唔存在 —— 嗰節 session 被人用錯 key 搬咗走。

成因鏈(全部係同一晚 v3.3.39 開發期間發生):
  1. 舊寫法 `log[today_iso()] = session` 永遠寫去「今日」key,
     就算 session 本身係另一日。
  2. 補記(date 參數)功能一開,呢個 bug 即刻變成「補記會覆蓋今日 session」。
  3. 開發時做端到端測試 → 補記去 2026-10-05 → 10/05 嘅 session 被寫到
     `2026-10-23` key 底下。
  4. 清理測試資料時,agent 見到 `2026-10-05` 有內容(其實係被搬錯位嗰節),
     就當測試殘留剷咗 `2026-10-05` key → 真實嗰節淨返喺錯嘅 key 底下。
  5. v3.3.39 修正咗寫入端(3 處改成 `log[session.get("date") or today_iso()]`),
     但**冇任何測試捉到 key ↔ inner date 唔一致** → 資料已經靜靜地爛咗。

守:
  1. `get_or_create_session()` 必須 normalize inner `date` 去等於佢自己嘅 key
  2. 寫入端必須用 session 自身日期做 key(唔准 `log[today_iso()] = session`)
  3. `save_log()` 必須原子寫(tmp + os.replace)→ 唔准半截 JSON 落地
  4. **真實 log 檔本身**要通過 key ↔ inner date 一致性檢查
     (只讀檢查,唔准改 Jim 嘅資料)

⚠️ 全部測試都唔寫入任何本機 log 或 Google Sheet。
   第 4 項只讀 `/home/work/.whoop_workout_log.json`,唔會改動佢。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO = Path("/home/work/projects/gymbro")
GYM_WEB = REPO / "gym_web.py"
REAL_LOG = Path("/home/work/.whoop_workout_log.json")


def _gym_web_src() -> str:
    return GYM_WEB.read_text()


# ── 1. get_or_create_session 必須 normalize inner date ─────────────────────

def test_get_or_create_session_normalizes_inner_date():
    """key 同 inner `date` 唔一致就要 normalize —— 否則下次寫入寫去錯 key。"""
    tree = ast.parse(_gym_web_src())
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "get_or_create_session"), None)
    assert fn is not None, "搵唔到 get_or_create_session"

    src = ast.get_source_segment(_gym_web_src(), fn)
    assert "log[today].get(\"date\") != today" in src, (
        "get_or_create_session 唔可以 normalize inner date "
        "(key ↔ inner date 唔一致會令補記寫去錯 key)")
    assert 'log[today]["date"] = today' in src, (
        "normalize 之後要寫返 `log[today][\"date\"] = today`")


# ── 2. 寫入端用 session 自身日期做 key ─────────────────────────────────────

def test_no_hardcoded_today_key_write():
    """`log[today_iso()] = session` 會令補記覆蓋今日 session → 唔准再存在。"""
    src = _gym_web_src()
    assert "log[today_iso()] = session" not in src, "仲有硬寫 today_iso() 嘅寫入"
    assert 'log[session.get("date") or today_iso()] = session' in src


# ── 3. save_log 原子寫 ─────────────────────────────────────────────────────

def test_save_log_is_atomic():
    """寫到一半 crash 會截斷成半個 JSON → 成個 log 冇晒。要 tmp + replace。"""
    tree = ast.parse(_gym_web_src())
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "save_log"), None)
    assert fn is not None, "搵唔到 save_log"
    src = ast.get_source_segment(_gym_web_src(), fn)
    assert "os.replace(" in src, "save_log 要用 os.replace 做原子換入"
    assert ".tmp" in src, "save_log 要先寫去 .tmp 檔"
    assert "WORKOUT_LOG.write_text(" not in src, (
        "唔可以直接 write_text 落 WORKOUT_LOG(會截斷)")


# ── 4. 真實 log 檔 key ↔ inner date 一致性(只讀) ───────────────────────────

@pytest.mark.skipif(not REAL_LOG.exists(), reason="本機 log 未存在(乾淨機)")
def test_real_workout_log_keys_match_inner_dates():
    """真實 log 每個 key 都要等於入面嘅 `date`。

    呢個係 2026-10-23 事故嘅直接捉測試:當日 `2026-10-23` key 入面裝住
    一節 `date: "2026-10-05"` 嘅 session,而 `2026-10-05` key 已經消失。
    """
    raw = REAL_LOG.read_text()
    log = json.loads(raw)  # 先確認係完整合法 JSON(唔係半截)

    mismatches = [
        (key, val.get("date"))
        for key, val in log.items()
        if isinstance(val, dict) and val.get("date") and val.get("date") != key
    ]
    assert not mismatches, (
        f"workout log 有 key ↔ inner date 唔一致:{mismatches} —— "
        "補記/清理會把真實 session 搬去錯 key,資料已經爛咗")


@pytest.mark.skipif(not REAL_LOG.exists(), reason="本機 log 未存在(乾淨機)")
def test_real_workout_log_dates_are_iso_formatted():
    """每個 key 都係 YYYY-MM-DD —— 唔係就係壞 key(例如被 date 欄污染)。"""
    import re
    log = json.loads(REAL_LOG.read_text())
    bad = [k for k in log if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", k)]
    assert not bad, f"workout log 有唔合法嘅 date key:{bad}"
