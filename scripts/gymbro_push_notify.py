#!/usr/bin/env python3
"""gymbro push 通知:訓練前 90 分鐘提提 Jim。

由 cron 每 15 分鐘行一次(no-agent)。行為:
  1. 讀今日 Google Calendar(primary)
  2. 搵出「訓練類」事件(gym / 舉重 / 健身 / 技術 / workout / lift / 操)
  3. 開始時間喺未來 90 分鐘內 → 發一個 push 通知
  4. 同一個 event 只發一次(state 檔記 dedup)

冇 subscription(VAPID 都未開)→ 靜靜哋 exit 0,唔會擾人。
"""
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HKT = timezone(timedelta(hours=8))
GWS = "/home/work/.hermes/node/bin/gws"
GYM_URL = "http://127.0.0.1:7000"
STATE = Path("/home/work/.hermes/cache/scratch/gymbro_push_notified.json")
LEAD_MINUTES = 90

GYM_WORDS = ("gym", "舉重", "健身", "訓練", "技術", "workout", "lift", "操", "gymbro")
SLEEP_WORDS = ("瞓", "睡", "補瞓", "上牀", "就寢", "sleep", "bedtime")


def run(cmd):
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=45)
        return r.stdout.strip()
    except Exception as e:
        print(f"[push_notify] cmd failed: {type(e).__name__}: {e}", flush=True)
        return ""


def today_events(now):
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    params = {
        "calendarId": "primary",
        "timeMin": start.astimezone(timezone.utc).isoformat(),
        "timeMax": end.astimezone(timezone.utc).isoformat(),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 50,
    }
    out = run([GWS, "calendar", "events", "list",
               "--params", json.dumps(params), "--format", "json"])
    if not out:
        return []
    try:
        data = json.loads(out)
    except Exception as e:
        print(f"[push_notify] calendar parse failed: {e}", flush=True)
        return []
    return data.get("items") or []


def parse_start(ev):
    raw = (ev.get("start") or {}).get("dateTime")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=HKT)
    return dt.astimezone(HKT)


def _event_text(ev):
    return " ".join([
        ev.get("summary") or "",
        ev.get("description") or "",
        ev.get("location") or "",
    ]).lower()


def is_gym(ev):
    text = _event_text(ev)
    return any(w.lower() in text for w in GYM_WORDS)


def is_sleep(ev):
    text = _event_text(ev)
    return any(w.lower() in text for w in SLEEP_WORDS)


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"notified": []}


def save_state(state):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[push_notify] state write failed: {e}", flush=True)


def send_push(title, body, url="/?tab=inbox"):
    payload = json.dumps({"title": title, "body": body, "url": url,
                           "tag": "gymbro-reminder"}).encode()
    req = urllib.request.Request(
        f"{GYM_URL}/api/push/send", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        print(f"[push_notify] send result: {out}", flush=True)
        return out
    except Exception as e:
        print(f"[push_notify] send failed: {type(e).__name__}: {e}", flush=True)
        return {}


def main():
    now = datetime.now(HKT)
    state = load_state()
    notified = set(state.get("notified") or [])
    events = today_events(now)
    if not events:
        print("[push_notify] no calendar events (or gcal unavailable) — nothing to do",
              flush=True)
        return 0

    window_end = now + timedelta(minutes=LEAD_MINUTES)
    queued = []
    for ev in events:
        gym = is_gym(ev)
        sleep = (not gym) and is_sleep(ev)
        if not (gym or sleep):
            continue
        start = parse_start(ev)
        if not start:
            continue
        if start <= now or start > window_end:
            continue
        ev_id = ev.get("id") or f"{start.isoformat()}"
        if ev_id in notified:
            continue
        title = "🏋️ 差唔多鐘要練" if gym else "😴 差唔多鐘要瞓"
        where = (ev.get("location") or "").strip()
        body = f"{start:%H:%M} {(ev.get('summary') or ('訓練' if gym else '瞓覺'))}"
        if where:
            body += f" · {where}"
        queued.append((ev_id, title, body))
        notified.add(ev_id)

    if not queued:
        print("[push_notify] no upcoming gym event inside the lead window", flush=True)
        return 0

    for ev_id, title, body in queued:
        print(f"[push_notify] notifying: {title} | {body}", flush=True)
        out = send_push(title, body, url="/?tab=inbox")
        # 只有真係送得出先 mark 做 notified,避免漏通知
        if out.get("sent", 0) > 0:
            state["notified"] = sorted(notified)[-200:]
            save_state(state)
        else:
            print("[push_notify] no subscription yet — will retry next tick",
                  flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
