"""v3.3.45 — 訊息收件箱(Telegram / 其他平台嘅歷史訊息)。

Jim OOB 2026-10-26:「In gymbro, there should be an inbox showing all the
historical messages received」。

資料來源:Hermes 嘅 ``state.db``(``messages`` + ``sessions`` + ``messages_fts``)。

⚠️ 呢個模組**唯讀**:永遠唔會寫入 state.db、唔會掂 Google Sheet、唔會
掂食相 / vision 路徑。所有嘢都係 fail-soft:DB 唔存在 / 損壞 / 權限唔夠
→ 回空結果 + 一句 error,唔會拋 exception 出去。

Timestamp 喺 DB 係 float epoch(秒)→ 轉 HKT ISO 先俾前端顯示。
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

STATE_DB = os.environ.get("HERMES_STATE_DB", "/home/work/.hermes/state.db")
HKT = timezone(timedelta(hours=8))
MAX_LIMIT = 200
MAX_TEXT = 2000

_lock = threading.Lock()
_cache: dict = {}
_cache_ts = 0.0
_CACHE_TTL = 20.0  # 秒;同一秒內多個人撳唔好反覆打 DB


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _to_iso(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=HKT).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _clean(text, limit: int = MAX_TEXT) -> str:
    t = (text or "").strip()
    if len(t) > limit:
        t = t[:limit] + "…"
    return t


def _fts_query(q: str) -> str:
    """將用戶輸入包成 FTS5 安全 query(避免語法 error 令成個搜尋爆)。"""
    tokens = re.findall(r"[\w一-鿿]+", q, flags=re.UNICODE)
    if not tokens:
        return ""
    return " ".join(f'"{t}"' for t in tokens)


# 系統注入式嘅 user 訊息 → 唔好當成 Jim 嘅訊息
_NOISE_PREFIXES = (
    "[CONTEXT COMPACTION",
    "Gateway message origin",
    "[IMPORTANT:",
    "[STILL IN PROGRESS",
)
# OOB 包裝 → 拆返出 Jim 真正講嘅嘢
_OOB_PREFIX = "[OUT-OF-BAND USER MESSAGE"
_OOB_END = "[/OUT-OF-BAND USER MESSAGE]"
_IMAGE_MARK = "[The user sent an image"


def _strip_oob(text: str) -> str:
    """OOB 訊息係包住嘅 → 拆返出內文(保留 Jim 原本嘅說話)。"""
    t = (text or "").strip()
    if not t.startswith(_OOB_PREFIX):
        return t
    if "]" in t[:120]:
        # 格式: [OUT-OF-BAND USER MESSAGE — 說明] 之後正文明文
        head, _, rest = t.partition("]")
        if "USER MESSAGE" in head:
            t = rest.strip()
    if _OOB_END in t:
        t = t.split(_OOB_END)[0].strip()
    return t


def _row_to_msg(row) -> dict:
    role = row["role"]
    raw = row["content"] or ""
    text = _strip_oob(raw)
    return {
        "id": row["id"],
        "role": role,
        "direction": "in" if role == "user" else "out",
        "text": _clean(text),
        "is_image": raw.lstrip().startswith(_IMAGE_MARK),
        "is_oob": raw.lstrip().startswith(_OOB_PREFIX),
        "ts": row["timestamp"],
        "iso": _to_iso(row["timestamp"]),
        "platform_message_id": row["platform_message_id"],
        "session_id": row["session_id"],
        "author": row["display_name"] or "",
    }


def list_messages(chat_id=None, limit: int = 50, before=None,
                  direction: str = "all", query: str = None) -> dict:
    """回傳訊息清單(新 → 舊)。direction: all / in / out。"""
    limit = max(1, min(int(limit or 50), MAX_LIMIT))
    direction = (direction or "all").lower()
    if direction not in ("all", "in", "out"):
        direction = "all"
    query = (query or "").strip()

    global _cache_ts, _cache
    key = ("list", chat_id, limit, before, direction, query)
    with _lock:
        if _cache.get("key") == key and (_cache_ts and (datetime.now().timestamp() - _cache_ts) < _CACHE_TTL):
            return _cache["value"]

    # role='tool' 係工具執行結果(唔係人嘅訊息)→ 唔好入收件箱。
    # display_kind: internal_notification = 系統通知、hidden / failed_turn
    # = 唔應該俾 Jim 睇到嘅 → 全部唔入收件箱。
    where = ["length(coalesce(m.content,'')) > 0",
             "m.role IN ('user','assistant')",
             "coalesce(m.display_kind,'') NOT IN ('internal_notification','hidden','failed_turn')"]
    params: list = []
    if chat_id:
        where.append("s.chat_id = ?")
        params.append(str(chat_id))
    if direction == "in":
        where.append("m.role = 'user'")
    elif direction == "out":
        where.append("m.role = 'assistant'")
    if before:
        where.append("m.id < ?")
        params.append(int(before))
    # 系統注入嘅 user 訊息(壓縮摘要 / gateway / skill 提示)唔係 Jim 講嘅嘢
    # → 唔好入收件箱。
    for pref in _NOISE_PREFIXES:
        where.append("m.content NOT LIKE ?")
        params.append(pref + "%")
    where_sql = " AND ".join(where)

    rows = []
    use_fts = False
    if query:
        fts_q = _fts_query(query)
        if fts_q:
            use_fts = True
    try:
        con = _connect()
    except Exception as e:
        print(f"[inbox] cannot open {STATE_DB}: {type(e).__name__}: {e}", flush=True)
        return {"messages": [], "total": 0, "has_more": False, "next_before": None,
                "error": "inbox_db_unavailable"}
    try:
        if use_fts:
            sql = f"""
                SELECT m.id AS id, m.role AS role, m.content AS content,
                       m.timestamp AS timestamp, m.platform_message_id AS platform_message_id,
                       m.session_id AS session_id, s.display_name AS display_name
                FROM messages_fts f
                JOIN messages m ON m.id = f.rowid
                JOIN sessions s ON s.id = m.session_id
                WHERE messages_fts MATCH ? AND {where_sql}
                ORDER BY m.id DESC LIMIT ?
            """
            fparams = [fts_q] + params + [limit + 1]
        else:
            sql = f"""
                SELECT m.id AS id, m.role AS role, m.content AS content,
                       m.timestamp AS timestamp, m.platform_message_id AS platform_message_id,
                       m.session_id AS session_id, s.display_name AS display_name
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                WHERE {where_sql}
                ORDER BY m.id DESC LIMIT ?
            """
            fparams = params + [limit + 1]
        rows = con.execute(sql, fparams).fetchall()
    except Exception as e:
        print(f"[inbox] query failed (fts={use_fts}): {type(e).__name__}: {e}", flush=True)
        if use_fts:
            # FTS 壞咗 → 退返 LIKE
            try:
                like = "%" + query + "%"
                sql = f"""
                    SELECT m.id AS id, m.role AS role, m.content AS content,
                           m.timestamp AS timestamp, m.platform_message_id AS platform_message_id,
                           m.session_id AS session_id, s.display_name AS display_name
                    FROM messages m
                    JOIN sessions s ON s.id = m.session_id
                    WHERE {where_sql} AND m.content LIKE ?
                    ORDER BY m.id DESC LIMIT ?
                """
                rows = con.execute(sql, params + [like, limit + 1]).fetchall()
            except Exception as e2:
                print(f"[inbox] LIKE fallback failed: {type(e2).__name__}: {e2}", flush=True)
                rows = []
    finally:
        try:
            con.close()
        except Exception:
            pass

    has_more = len(rows) > limit
    rows = rows[:limit]
    # 去重:同一段內容可能喺 session fork / resume 時被重複寫入過
    # (例如同一條回覆出現 2 次,id 唔同但 timestamp 唔同)。
    # → 用內容雜湊 + 1 小時窗口去重,只留最新嗰條。
    seen = set()
    uniq = []
    for r in rows:
        msg = _row_to_msg(r)
        key = (msg["session_id"], msg["role"], msg["text"])
        if key in seen:
            continue          # 同一 session 內重複寫入過 → 跳過
        seen.add(key)
        uniq.append(msg)
    msgs = uniq
    out = {
        "messages": msgs,
        "count": len(msgs),
        "has_more": has_more,
        "next_before": msgs[-1]["id"] if (msgs and has_more) else None,
        "query": query,
        "direction": direction,
    }
    with _lock:
        _cache = {"key": key, "value": out}
        _cache_ts = datetime.now().timestamp()
    return out


def default_chat_id() -> str:
    """最近有訊息嘅 Telegram 對話 chat_id(收件箱預設範圍)。

    唔 hardcode 任何 chat id:每次都睇 DB(最新訊息嗰個對話),
    Jim 開新對話都唔會錯。
    """
    try:
        con = _connect()
    except Exception as e:
        print(f"[inbox] default_chat_id cannot open db: {type(e).__name__}: {e}", flush=True)
        return ""
    try:
        row = con.execute(
            """
            SELECT s.chat_id AS chat_id
            FROM messages m JOIN sessions s ON s.id = m.session_id
            WHERE s.chat_id IS NOT NULL AND s.chat_id != ''
              AND s.source = 'telegram'
              AND m.role IN ('user','assistant')
            ORDER BY m.timestamp DESC LIMIT 1
            """
        ).fetchone()
        return (row["chat_id"] if row else "") or ""
    except Exception as e:
        print(f"[inbox] default_chat_id failed: {type(e).__name__}: {e}", flush=True)
        return ""
    finally:
        try:
            con.close()
        except Exception:
            pass


def stats(chat_id=None) -> dict:
    """收件箱統計:收到 / 發出 / 總數 / 最後一條訊息時間。"""
    where = ["length(coalesce(m.content,'')) > 0",
             "m.role IN ('user','assistant')",
             "coalesce(m.display_kind,'') NOT IN ('internal_notification','hidden','failed_turn')"]
    params: list = []
    if chat_id:
        where.append("s.chat_id = ?")
        params.append(str(chat_id))
    for pref in _NOISE_PREFIXES:
        where.append("m.content NOT LIKE ?")
        params.append(pref + "%")
    where_sql = " AND ".join(where)
    out = {"total_in": 0, "total_out": 0, "total": 0, "sessions": 0,
           "last_ts": None, "last_iso": "", "error": ""}
    try:
        con = _connect()
    except Exception as e:
        print(f"[inbox] stats cannot open db: {type(e).__name__}: {e}", flush=True)
        out["error"] = "inbox_db_unavailable"
        return out
    try:
        # 同一段內容可能重複寫入過(session fork / resume)→ 用同 list
        # 一樣嘅 1 小時窗口去重,計數先準。
        rows = con.execute(f"""
            SELECT m.role AS role, m.content AS content, m.timestamp AS timestamp,
                   m.session_id AS session_id
            FROM messages m JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            ORDER BY m.timestamp DESC
        """, params).fetchall()
        seen = set()
        tin = tout = 0
        last_ts = None
        for r in rows:
            key = (r["session_id"], r["role"], (r["content"] or "").strip())
            ts = r["timestamp"] or 0
            if key in seen:
                continue
            seen.add(key)
            if r["role"] == "user":
                tin += 1
            else:
                tout += 1
            if last_ts is None or ts > last_ts:
                last_ts = ts
        out["total_in"] = tin
        out["total_out"] = tout
        out["total"] = tin + tout
        out["sessions"] = len({r["session_id"] for r in rows})
        out["last_ts"] = last_ts
        out["last_iso"] = _to_iso(last_ts)
    except Exception as e:
        print(f"[inbox] stats failed: {type(e).__name__}: {e}", flush=True)
        out["error"] = "inbox_query_failed"
    finally:
        try:
            con.close()
        except Exception:
            pass
    return out
