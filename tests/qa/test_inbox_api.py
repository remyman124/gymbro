"""v3.3.45 — 訊息收件箱 API guard。

背景:Jim OOB 2026-10-26「In gymbro, there should be an inbox showing all the
historical messages received」。收件箱讀 Hermes 嘅 `state.db`(唯讀)。

呢個檔案守住 4 樣嘢:

  1. `gym_web/inbox.py` **永遠唔會寫入** state.db(冇 INSERT/UPDATE/DELETE/
     COMMIT),亦唔會掂 Google Sheet / food-scan 路徑
  2. list / stats 嘅過濾邏輯正確:
     - role='tool'(工具執行結果)唔會入收件箱
     - 系統注入式 user 訊息(壓縮摘要 / gateway / skill 提示)唔會入收件箱
     - display_kind = internal_notification / hidden / failed_turn 唔會入
     - 同一段內容重複寫入過(session fork)→ 只留最新嗰條
  3. 分頁(before cursor)+ 方向 filter(dir=in/out)+ 全文搜索正常
  4. 前端有「收件箱」tab、搜尋框、方向 filter、載入更多按鈕 + API 路由

⛔ 測試用**自己造嘅 tmp SQLite fixture**,唔會讀亦唔會寫生產 state.db,
   亦**絕對唔會**寫入生產 Google Sheet `Nutrition` tab。
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path("/home/work/projects/gymbro")
TEMPLATE = REPO / "templates" / "index.html"
GYM_WEB = REPO / "gym_web.py"
INBOX = REPO / "gym_web" / "inbox.py"

sys.path.insert(0, str(REPO))


def _load_inbox():
    """Load gym_web.inbox by path(避免 `import gym_web` 解析到 package)。"""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    mod = importlib.import_module("gym_web.inbox")
    return importlib.reload(mod)


@pytest.fixture
def inbox_db(tmp_path, monkeypatch):
    """造一個細細嘅 SQLite fixture,模擬 Hermes state.db。"""
    path = tmp_path / "state.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            chat_id TEXT,
            chat_type TEXT,
            display_name TEXT,
            started_at TEXT,
            message_count INTEGER,
            title TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            platform_message_id TEXT,
            display_kind TEXT,
            display_metadata TEXT,
            api_content TEXT,
            tool_call_id TEXT,
            tool_name TEXT,
            tool_calls TEXT
        );
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, tool_calls);
        """
    )
    con.execute(
        "INSERT INTO sessions (id, source, chat_id, display_name) VALUES (?,?,?,?)",
        ("sess1", "telegram", "1424782299", "J M"),
    )
    rows = [
        # (role, content, kind)
        ("user", "我要去 gym", None),
        ("assistant", "收到,加油", None),
        ("tool", '{"tool": "output"}', None),                      # 工具輸出 → 唔入
        ("user", "[CONTEXT COMPACTION — REFERENCE ONLY] blah", None),  # 系統注入 → 唔入
        ("user", "[IMPORTANT: You are running as a scheduled cron job.]", None),  # 唔入
        ("user", "[OUT-OF-BAND USER MESSAGE — direct]\n真正嘅說話", None),  # 拆封 → 入
        ("user", "收到,加油", None),                                 # 同上 → 唔入
        ("assistant", "完全一樣嘅回覆", None),
        ("assistant", "完全一樣嘅回覆", None),                       # 重複 → 唔入
        ("assistant", "唔應該出現", "internal_notification"),        # 系統 → 唔入
        ("assistant", "唔應該出現 2", "failed_turn"),                # 唔入
    ]
    ts = 1793000000.0
    for i, (role, content, kind) in enumerate(rows):
        cur = con.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, display_kind)"
            " VALUES (?,?,?,?,?)",
            ("sess1", role, content, ts + i * 60, kind),
        )
        con.execute("INSERT INTO messages_fts (rowid, content) VALUES (?,?)",
                    (cur.lastrowid, content))
    con.commit()
    con.close()

    mod = _load_inbox()
    monkeypatch.setattr(mod, "STATE_DB", str(path))
    mod._cache = {}
    mod._cache_ts = 0.0
    return mod


# ---------------------------------------------------------------- 唯讀守門

def test_inbox_module_is_read_only():
    src = INBOX.read_text(encoding="utf-8")
    upper = src.upper()
    for bad in ("INSERT INTO", "UPDATE MESSAGES", "UPDATE SESSIONS", "DELETE FROM",
                "COMMIT", "REPLACE INTO", "DROP TABLE"):
        assert bad not in upper, f"inbox.py 唔可以有寫入 SQL: {bad}"
    assert "mode=ro" in src, "inbox.py 必須用 SQLite 唯讀模式開 state.db"


def test_inbox_does_not_touch_sheet_or_scan():
    src = INBOX.read_text(encoding="utf-8")
    for bad in ("sheets", "SPREADSHEET", "scan_commit", "minimax", "nutrition"):
        assert bad.lower() not in src.lower(), f"inbox.py 唔准掂 {bad}"


# ---------------------------------------------------------------- 過濾邏輯

def test_list_filters_tool_rows_and_system_noise(inbox_db):
    d = inbox_db.list_messages(chat_id="1424782299", limit=50)
    texts = [m["text"] for m in d["messages"]]
    assert not any("tool" in t for t in texts), "role='tool' 唔應該入收件箱"
    assert not any("CONTEXT COMPACTION" in t for t in texts)
    assert not any("scheduled cron job" in t for t in texts)
    assert not any("唔應該出現" in t for t in texts), "internal_notification/failed_turn 要濾走"
    # 真正嘅訊息一定要喺度
    assert any("我要去 gym" in t for t in texts)
    assert any("收到,加油" in t for t in texts)


def test_oob_wrapper_is_stripped(inbox_db):
    d = inbox_db.list_messages(chat_id="1424782299", limit=50)
    oob = [m for m in d["messages"] if "真正嘅說話" in m["text"]]
    assert len(oob) == 1
    assert "OUT-OF-BAND" not in oob[0]["text"], "OOB 包裝應該拆走,留返 Jim 原本嘅說話"


def test_duplicate_content_deduped(inbox_db):
    d = inbox_db.list_messages(chat_id="1424782299", limit=50)
    same = [m for m in d["messages"] if m["text"] == "完全一樣嘅回覆"]
    assert len(same) == 1, "同一段內容重複寫入過 → 只留最新嗰條"


def test_direction_filter(inbox_db):
    only_in = inbox_db.list_messages(chat_id="1424782299", limit=50, direction="in")
    assert only_in["messages"], "dir=in 應該有嘢"
    assert all(m["direction"] == "in" for m in only_in["messages"])
    only_out = inbox_db.list_messages(chat_id="1424782299", limit=50, direction="out")
    assert all(m["direction"] == "out" for m in only_out["messages"])


def test_pagination_cursor(inbox_db):
    p1 = inbox_db.list_messages(chat_id="1424782299", limit=2)
    assert p1["has_more"] is True
    assert p1["next_before"] is not None
    p2 = inbox_db.list_messages(chat_id="1424782299", limit=2, before=p1["next_before"])
    ids1 = {m["id"] for m in p1["messages"]}
    ids2 = {m["id"] for m in p2["messages"]}
    assert not (ids1 & ids2), "第二頁唔應該重疊第一頁"


def test_fulltext_search(inbox_db):
    d = inbox_db.list_messages(chat_id="1424782299", limit=50, query="gym")
    assert any("我要去 gym" in m["text"] for m in d["messages"]), "FTS 搜索要搵到嘢"


def test_stats_counts(inbox_db):
    st = inbox_db.stats(chat_id="1424782299")
    assert st["total_in"] >= 2
    assert st["total_out"] >= 1
    assert st["error"] == ""


def test_missing_db_is_fail_soft(inbox_db, monkeypatch):
    monkeypatch.setattr(inbox_db, "STATE_DB", "/nonexistent/does-not-exist.db")
    inbox_db._cache = {}
    d = inbox_db.list_messages(limit=5)
    assert d["messages"] == []
    assert d.get("error"), "DB 唔存在 → 應該回 error 欄位而唔係拋 exception"
    st = inbox_db.stats()
    assert st["error"]


# ---------------------------------------------------------------- 前端 + 路由

def test_routes_exist():
    src = GYM_WEB.read_text(encoding="utf-8")
    assert '@app.route("/api/inbox/messages"' in src
    assert '@app.route("/api/inbox/stats"' in src
    assert "from gym_web import inbox as _inbox" in src


def test_frontend_has_inbox_tab():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "inbox-tab-btn" in html
    assert "isTabVisible('inbox')" in html
    assert "inbox-search" in html
    assert "inbox-dir" in html
    assert "inbox-more-btn" in html
    assert "/api/inbox/messages" in html
    assert "/api/inbox/stats" in html
    assert "openInboxTab" in html
    assert "loadInbox" in html
    # tab bar 由 4 欄變 5 欄(新增收件箱)
    assert "grid-cols-5" in html
