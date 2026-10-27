"""v3.3.46 — Gymbro 出站訊息紀錄(outbox)。

Jim OOB 2026-10-27:「Just the message push from gymbro. Not our conversation.」
→ 收件箱 = **Gymbro 自己推出去嘅訊息**,唔係 Hermes Telegram 對話。

兩種 kind:
  - `push`  — Web Push 推播(提醒 / 訓練 / 瞓覺 / 任何 `/api/push/send`)
  - `cheer` — Cheer 訊息(app 內產生嘅打氣內容 + 語音 + 圖)

設計原則:
  - **只記錄**,唔會阻礙原本嘅推播 → 寫 log 失敗一律吞掉 + print
  - append-only JSON,最多留 `MAX_ITEMS` 條
  - 唔掂 Google Sheet / food-scan / vision 路徑
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

STORE = os.environ.get("GYMBRO_OUTBOX_STORE", "/home/work/.hermes/gymbro_outbox.json")
MAX_ITEMS = 500
HKT = timezone(timedelta(hours=8))

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(tz=HKT).isoformat()


def _path() -> str:
    """實際 store 路徑。環境變數每次都生效 → 測試可以用 runtime 覆蓋,
    QA suite 唔會污染生產 store。"""
    return os.environ.get("GYMBRO_OUTBOX_STORE") or STORE


def _load() -> list:
    try:
        path = _path()
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[outbox] load failed ({STORE}): {type(e).__name__}: {e}", flush=True)
        return []


def _save(items: list) -> bool:
    path = _path()
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items[-MAX_ITEMS:], f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[outbox] save failed ({path}): {type(e).__name__}: {e}", flush=True)
        return False


def log_message(kind: str, title: str = "", body: str = "", url: str = "",
                ok: bool = True, detail: str = "", sent: int = 0, failed: int = 0,
                source: str = "gymbro") -> dict:
    """記低一條出站訊息。**永遠唔會拋 exception**(唔可以阻礙推播)。"""
    rec = {
        "id": uuid.uuid4().hex[:12],
        "kind": (kind or "message")[:40],
        "title": (title or "")[:200],
        "body": (body or "")[:2000],
        "url": (url or "")[:2048],
        "ok": bool(ok),
        "detail": (detail or "")[:500],
        "sent": int(sent or 0),
        "failed": int(failed or 0),
        "source": (source or "gymbro")[:40],
        "ts": _now_iso(),
    }
    try:
        with _lock:
            items = _load()
            items.append(rec)
            _save(items)
    except Exception as e:
        print(f"[outbox] log_message failed: {type(e).__name__}: {e}", flush=True)
    return rec


def list_messages(limit: int = 50, before=None, kind: str = None,
                  query: str = None) -> dict:
    """最新 → 最舊。before = 游標(上一頁最後一條嘅 id)。"""
    try:
        limit = max(1, min(int(limit or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    items = _load()
    items.reverse()          # store 係舊 → 新,反轉做新 → 舊

    if kind and kind != "all":
        items = [i for i in items if i.get("kind") == kind]
    if query:
        q = query.strip().lower()
        if q:
            items = [i for i in items
                     if q in (i.get("title") or "").lower()
                     or q in (i.get("body") or "").lower()
                     or q in (i.get("detail") or "").lower()]

    total = len(items)
    if before:
        ids = [i.get("id") for i in items]
        try:
            idx = ids.index(before)
            items = items[idx + 1:]
        except ValueError:
            pass

    page = items[:limit]
    has_more = len(items) > limit
    return {
        "messages": page,
        "count": len(page),
        "total": total,
        "has_more": has_more,
        "next_before": page[-1].get("id") if (page and has_more) else None,
        "kind": kind or "all",
        "query": query or "",
    }


def stats() -> dict:
    items = _load()
    by_kind: dict = {}
    for i in items:
        k = i.get("kind") or "message"
        by_kind[k] = by_kind.get(k, 0) + 1
    last = items[-1] if items else None
    return {
        "total": len(items),
        "by_kind": by_kind,
        "push": by_kind.get("push", 0),
        "cheer": by_kind.get("cheer", 0),
        "last_ts": (last or {}).get("ts", ""),
        "last_title": (last or {}).get("title", ""),
        "store": STORE,
        "error": "",
    }
