"""v3.3.46 — Gymbro 推播收件箱(outbox)guard。

背景:Jim OOB 2026-10-27:「Just the message push from gymbro. Not our
conversation.」→ 收件箱 = **Gymbro 自己推出去嘅訊息**(Web Push 推播 + cheer),
唔係 Hermes Telegram 對話。

呢個檔案守住 4 樣嘢:

  1. `gym_web/outbox.py` **唔會**讀 Hermes `state.db`(唔再係對話收件箱)
  2. `log_message()` 只寫自己嘅 store(tmp_path),**唔會**掂生產 store
  3. list / stats / kind filter / 搜索 / 分頁 正常
  4. `/api/push/send` 同 cheer 完成**一定會**記低一條 outbox

⛔ 呢啲測試**絕對唔可以**寫入生產 Google Sheet `Nutrition` tab
   (見 `test_no_test_pollution.py`)。呢個檔案完全唔掂 food-scan / Sheet 路徑。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path("/home/work/projects/gymbro")
TEMPLATE = REPO / "templates" / "index.html"
GYM_WEB = REPO / "gym_web.py"
OUTBOX = REPO / "gym_web" / "outbox.py"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _load_outbox():
    mod = importlib.import_module("gym_web.outbox")
    return importlib.reload(mod)


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    """Outbox store 指去 tmp file —— 永遠唔碰真實 store。"""
    mod = _load_outbox()
    monkeypatch.setattr(mod, "STORE", str(tmp_path / "outbox.json"))
    return mod


# ---------------------------------------------------------------- 範圍守門

def test_outbox_does_not_read_conversation_db():
    """v3.3.46:收件箱唔再係「我哋嘅對話」→ 唔准讀 state.db。"""
    src = OUTBOX.read_text(encoding="utf-8")
    assert "state.db" not in src, "outbox 唔准讀 Hermes state.db(對話歷史)"
    assert "messages_fts" not in src


def test_outbox_does_not_touch_sheet_or_scan():
    src = OUTBOX.read_text(encoding="utf-8")
    for bad in ("sheets", "SPREADSHEET", "scan_commit", "minimax", "nutrition"):
        assert bad.lower() not in src.lower(), f"outbox.py 唔准掂 {bad}"


def test_no_state_db_reader_left():
    """舊嘅 state.db 讀取層要刪咗。"""
    assert not (REPO / "gym_web" / "inbox.py").exists(), "gym_web/inbox.py 應該刪走"
    src = GYM_WEB.read_text(encoding="utf-8")
    assert "from gym_web import inbox as _inbox" not in src
    assert "from gym_web import outbox as _outbox" in src


# ---------------------------------------------------------------- outbox 邏輯

def test_log_and_list(outbox):
    rec = outbox.log_message("push", title="訓練時間到", body="12:00 健身",
                              url="/", ok=True, sent=1)
    assert rec["id"]
    d = outbox.list_messages(limit=10)
    assert d["count"] == 1
    m = d["messages"][0]
    assert m["kind"] == "push"
    assert m["title"] == "訓練時間到"
    assert m["body"] == "12:00 健身"
    assert m["sent"] == 1
    assert m["ok"] is True
    assert m["ts"]


def test_store_is_json_file(outbox, tmp_path):
    outbox.log_message("cheer", title="🔥 Cheer · morning", body="加油")
    store = tmp_path / "outbox.json"
    assert store.exists()
    import json
    data = json.loads(store.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 1


def test_kind_filter(outbox):
    outbox.log_message("push", title="A", body="push")
    outbox.log_message("cheer", title="B", body="cheer")
    pushes = outbox.list_messages(kind="push")
    assert [m["kind"] for m in pushes["messages"]] == ["push"]
    cheers = outbox.list_messages(kind="cheer")
    assert [m["kind"] for m in cheers["messages"]] == ["cheer"]
    allm = outbox.list_messages(kind="all")
    assert allm["count"] == 2


def test_search(outbox):
    outbox.log_message("push", title="訓練時間到", body="12:00 健身")
    outbox.log_message("push", title="瞓覺時間到", body="22:30 瞓覺")
    hits = outbox.list_messages(query="健身")
    assert hits["count"] == 1
    assert "健身" in hits["messages"][0]["body"]


def test_newest_first_and_pagination(outbox):
    for i in range(5):
        outbox.log_message("push", title=f"T{i}", body=f"b{i}")
    d = outbox.list_messages(limit=2)
    assert d["count"] == 2
    assert d["messages"][0]["title"] == "T4"      # 最新喺最前
    assert d["has_more"] is True
    p2 = outbox.list_messages(limit=2, before=d["next_before"])
    ids1 = {m["id"] for m in d["messages"]}
    ids2 = {m["id"] for m in p2["messages"]}
    assert not (ids1 & ids2)


def test_stats(outbox):
    outbox.log_message("push", title="A")
    outbox.log_message("cheer", title="B")
    st = outbox.stats()
    assert st["total"] == 2
    assert st["push"] == 1
    assert st["cheer"] == 1
    assert st["last_title"] == "B"


def test_log_message_never_raises(outbox, monkeypatch):
    """寫 log 失敗唔可以阻礙推播 → log_message 要 swallow 住。"""
    def boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(outbox, "_load", boom)
    rec = outbox.log_message("push", title="X")
    assert rec["id"] and rec["title"] == "X"      # 唔拋 exception


def test_trim_to_max_items(outbox, monkeypatch):
    monkeypatch.setattr(outbox, "MAX_ITEMS", 3)
    for i in range(6):
        outbox.log_message("push", title=f"T{i}")
    items = outbox._load()
    assert len(items) == 3
    assert items[-1]["title"] == "T5"


# ---------------------------------------------------------------- 生產污染守門

def test_production_outbox_has_no_test_entries():
    """QA suite 唔准寫入生產 outbox store。

    背景:v3.3.46 加咗 outbox 記錄之後,`test_mobile_pwa_api.py` 嘅
    push-send 測試(POST /api/push/send)會令生產 store 多咗
    「hi / there」「t / b」「測試 / 測試」三條垃圾 → 測試要用
    `GYMBRO_OUTBOX_STORE` 指去 tmp。
    """
    import json
    prod = Path("/home/work/.hermes/gymbro_outbox.json")
    if not prod.exists():
        return
    try:
        data = json.loads(prod.read_text(encoding="utf-8"))
    except Exception:
        return
    bad_titles = {"hi", "t", "測試", "test", "Test"}
    bad = [i for i in data
           if (i.get("title") or "").strip() in bad_titles
           or (i.get("body") or "").strip() in {"there", "b", "測試"}]
    assert not bad, (
        "生產 outbox store 有測試條目(要用 GYMBRO_OUTBOX_STORE 隔離): "
        f"{[(b.get('ts'), b.get('title'), b.get('body')) for b in bad]}")


# ---------------------------------------------------------------- 接駁 + 前端

def test_push_send_is_logged():
    src = GYM_WEB.read_text(encoding="utf-8")
    assert "_outbox.log_message(" in src, "/api/push/send 一定要記低 outbox"
    assert '"push", title=title, body=body, url=url' in src


def test_cheer_is_logged():
    src = GYM_WEB.read_text(encoding="utf-8")
    assert '"cheer", title=f"🔥 Cheer · {fire_type}"' in src


def test_routes_exist():
    src = GYM_WEB.read_text(encoding="utf-8")
    assert '@app.route("/api/inbox/messages"' in src
    assert '@app.route("/api/inbox/stats"' in src
    assert "from gym_web import outbox as _outbox" in src


def test_frontend_has_inbox_tab():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "inbox-tab-btn" in html
    assert "isTabVisible('inbox')" in html
    assert "inbox-search" in html
    assert "inbox-kind" in html
    assert "inbox-more-btn" in html
    assert "/api/inbox/messages" in html
    assert "/api/inbox/stats" in html
    assert "openInboxTab" in html
    assert "loadInbox" in html
    assert "grid-cols-5" in html
    # 方向 filter(direction)已經唔存在 —— 收件箱唔再顯示對話
    assert "inboxDir" not in html
    assert "inboxKind" in html
