#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""落 set 快路 — 由一句中英夾雜/廣東話文字解析出多組 set,直接寫入 gymbro 訓練 log。

用法
    python3 scripts/log_sets_from_text.py "臥推 60x8 60x8, 深蹲 80x10" --date 2026-10-22
    python3 scripts/log_sets_from_text.py "引體上升 8"           # 自重(唔記重量)
    python3 scripts/log_sets_from_text.py "肩推 40x10" --dry-run # 只解析唔寫入
    echo "臥推 60x8" | python3 scripts/log_sets_from_text.py -

規則
  - 動作名 + 一個或多个 spec:「60x8」「60 x 8」「60*8」「60 8」「x8」(只有次數 = 自重)
  - 「60x8x3」→ 3 組 60kg×8;「60 8 10」→ 重量 60,兩組分別 8 同 10 下
  - 自重動作名單(引體上升/伏地挺身/平板支撐…)→ 唔記重量
  - 中文動作名自動對應 app 目錄英文名(app 有 24 個預設動作);對唔到就原樣保留
  - set_n 交畀 server 自動遞增(同一動作喺嗰一節已做幾多組 + 1)

安全
  - 只經 /api/log_set 寫本機 /home/work/.whoop_workout_log.json,唔掂 Google Sheet
  - --dry-run 只解析唔寫入
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

GYM_WEB_BASE = "http://127.0.0.1:7000"
LOG_PATH = Path("/home/work/.whoop_workout_log.json")

# ── app 預設動作目錄(templates/index.html exerciseCategories)─────────────────
CATALOGUE = [
    "BB Bench Press", "Incline BB Press", "DB Bench Press", "Incline DB Press",
    "Pec Deck", "Cable Crossover",
    "Lat Pulldown", "Low Row (Cable)", "BB Bent-over Row", "Seated Row",
    "T-bar Row", "Pull-ups",
    "Squat", "Leg Press", "Leg Extension", "Leg Curl", "Romanian Deadlift",
    "Calf Raise",
    "DB OHP", "BB OHP", "DB Shoulder Raise", "Side Lateral Raise",
    "Cable Lateral", "Face Pull",
    "Plank", "Hanging Leg Raise", "Crunch", "Cable Crunch", "Russian Twist",
    "Ab Wheel",
]

# ── 中文 → app 目錄名 ────────────────────────────────────────────────────────
CN_MAP = {
    "槓臥推": "BB Bench Press", "杠铃卧推": "BB Bench Press", "臥推": "BB Bench Press",
    "上斜槓臥推": "Incline BB Press", "斜板臥推": "Incline BB Press", "上斜臥推": "Incline BB Press",
    "啞鈴臥推": "DB Bench Press", "上斜啞鈴臥推": "Incline DB Press",
    "上胸機": "Pec Deck", "蝴蝶機": "Pec Deck", "夾胸機": "Pec Deck",
    "繩索夾胸": "Cable Crossover", "龙门架夹胸": "Cable Crossover", "龍門架夾胸": "Cable Crossover",
    "下拉": "Lat Pulldown", "龍門架": "Lat Pulldown", "高下拉": "Lat Pulldown", "闊背下拉": "Lat Pulldown",
    "低位划船": "Low Row (Cable)", "坐姿划船": "Seated Row", "划船": "BB Bent-over Row",
    "俯身划船": "BB Bent-over Row", "彎身划船": "BB Bent-over Row", "T字划船": "T-bar Row",
    "深蹲": "Squat", "背蹲": "Squat", "杠铃深蹲": "Squat", "槓鈴深蹲": "Squat",
    "腿舉": "Leg Press", "腿屈伸": "Leg Extension", "腿彎舉": "Leg Curl",
    "羅馬尼亞硬拉": "Romanian Deadlift", "提踵": "Calf Raise", "小腿": "Calf Raise",
    "肩上推": "DB OHP", "肩推": "DB OHP", "啞鈴肩上推": "DB OHP",
    "槓鈴肩推": "BB OHP", "側平舉": "Side Lateral Raise", "繩索側平舉": "Cable Lateral",
    "面拉": "Face Pull", "平板支撐": "Plank", "平板": "Plank",
    "懸垂舉腿": "Hanging Leg Raise", "仰臥起坐": "Crunch", "仰臥捲腹": "Crunch",
    "捲腹": "Crunch", "繩索捲腹": "Cable Crunch", "俄式扭轉": "Russian Twist",
    "腹肌滾輪": "Ab Wheel", "滾輪": "Ab Wheel",
    "引體上升": "Pull-ups", "引體向上": "Pull-ups",
    "伏地挺身": "Push-ups", "掌上壓": "Push-ups", "俯臥撐": "Push-ups",
    "硬拉": "Deadlift", "傳統硬拉": "Deadlift",
}

# 自重動作(唔記重量)
BODYWEIGHT = {
    "引體上升", "引體向上", "伏地挺身", "掌上壓", "平板支撐", "平板", "仰臥起坐",
    "仰臥捲腹", "捲腹", "懸垂舉腿", "腹肌滾輪", "滾輪", "俄式扭轉", "撐體",
}
BODYWEIGHT_EN = {"pull-ups", "push-ups", "plank", "crunch", "ab wheel", "russian twist",
                 "hanging leg raise", "dip"}

NUM = r"\d+(?:\.\d+)?"


def map_exercise(raw: str) -> str:
    """中文 → app 目錄名;認唔到就原樣返(trim)。"""
    name = raw.strip().strip("的")
    if not name:
        return ""
    if name in CATALOGUE:
        return name
    if name in CN_MAP:
        return CN_MAP[name]
    # 嘗試包含式(例如「啞鈴臥推 60」被切剩「啞鈴臥推」)
    for cn, en in sorted(CN_MAP.items(), key=lambda kv: -len(kv[0])):
        if cn in name:
            return en
    for en in CATALOGUE:
        if en.lower() in name.lower():
            return en
    return name


def is_bodyweight(name: str) -> bool:
    return name in BODYWEIGHT or name.lower() in BODYWEIGHT_EN


def parse_segment(seg: str) -> list[dict]:
    """解析一段 → list[{exercise, weight_kg|None, reps}]"""
    seg = seg.strip()
    if not seg:
        return []
    # 動作名 = 開頭唔含數字嘅部分
    m = re.match(r"^([^\dxX*]+)", seg)
    if not m:
        return []
    raw_name = m.group(1).strip()
    rest = seg[m.end():].strip()
    ex = map_exercise(raw_name)
    if not ex:
        return []
    bw = is_bodyweight(ex)

    sets: list[dict] = []
    if not rest:
        # 淨係動作名 → 一次,次數用 Jim 偏好(10)
        sets.append({"exercise": ex, "weight_kg": None, "reps": 10})
        return sets

    # 分開兩類 token:WxR(有 x 記號)同「淨數字」
    wxr: list[tuple[float, float]] = []
    bare: list[float] = []
    for m in re.finditer(rf"({NUM})\s*[xX*×]\s*({NUM})|({NUM})", rest):
        if m.group(1):
            wxr.append((float(m.group(1)), float(m.group(2))))
        else:
            bare.append(float(m.group(3)))

    if not wxr and not bare:
        return []

    # 「x8」開頭 → 只有次數,冇重量
    reps_only = bool(re.match(r"^\s*[xX*×]", rest))

    if bw or reps_only:
        # 自重 / 只有次數:所有數字都係次數
        for _, r in wxr:
            sets.append({"exercise": ex, "weight_kg": None, "reps": int(r)})
        for n in bare:
            sets.append({"exercise": ex, "weight_kg": None, "reps": int(n)})
        return sets

    if wxr:
        for w, r in wxr:
            sets.append({"exercise": ex, "weight_kg": w, "reps": int(r)})
        # 尾隨嘅淨數字 → 同一重量嘅額外組數
        if bare and sets:
            for n in bare:
                sets.append({"exercise": ex, "weight_kg": sets[-1]["weight_kg"],
                             "reps": int(n)})
        return sets

    # 淨數字:第一個 = 重量,其餘 = 各組次數(單一數字 → 預設 10 下)
    weight = bare[0]
    if len(bare) == 1:
        sets.append({"exercise": ex, "weight_kg": weight, "reps": 10})
    else:
        for r in bare[1:]:
            sets.append({"exercise": ex, "weight_kg": weight, "reps": int(r)})
    return sets


def parse_text(text: str) -> list[dict]:
    text = text.replace("落set", " ").replace("落 set", " ").replace("logs", " ")
    parts = re.split(r"[,，、;；\n]+", text)
    out: list[dict] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 「60x8x3」→ 展開 3 組
        m = re.search(r"(\d+(?:\.\d+)?)\s*[xX*×]\s*(\d+)\s*[xX*×]\s*(\d+)$", p)
        if m:
            base = p[:m.start()].strip()
            w, r, n = float(m.group(1)), int(m.group(2)), int(m.group(3))
            for _ in range(n):
                out.extend(parse_segment(f"{base} {w}x{r}"))
            continue
        out.extend(parse_segment(p))
    return out


def post_set(entry: dict, date: str | None, source: str = "chat-log-sets") -> dict:
    payload = {"exercise": entry["exercise"], "reps": entry["reps"],
               "source": source, "form_check": "ok", "weight_note": ""}
    if entry.get("weight_kg") is not None:
        payload["weight_kg"] = entry["weight_kg"]
    if date:
        payload["date"] = date
    req = urllib.request.Request(
        f"{GYM_WEB_BASE}/api/log_set", data=json.dumps(payload).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", help="例:臥推 60x8 60x8, 深蹲 80x10")
    ap.add_argument("--date", help="YYYY-MM-DD(補記過去一節;預設今日)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source", default="chat-log-sets")
    args = ap.parse_args()

    text = args.text
    if not text or text == "-":
        text = sys.stdin.read()
    text = (text or "").strip()
    if not text:
        print("冇收到文字", file=sys.stderr)
        return 2

    parsed = parse_text(text)
    if not parsed:
        print("解析唔到任何 set。格式例:臥推 60x8 60x8, 深蹲 80x10", file=sys.stderr)
        return 2

    print(f"解析到 {len(parsed)} 組:")
    for p in parsed:
        w = f"{p['weight_kg']:g}kg" if p.get("weight_kg") is not None else "自重"
        print(f"  · {p['exercise']} — {w} × {p['reps']}")

    if args.dry_run:
        print("(dry-run,無寫入)")
        return 0

    ok = 0
    for p in parsed:
        res = post_set(p, args.date, args.source)
        if res.get("ok"):
            ok += 1
            e = res.get("entry", {})
            print(f"  ✅ 已記 set {e.get('set')} · {e.get('weight_kg')}kg × {e.get('reps')}"
                  f" → {e.get('exercise')}")
        else:
            print(f"  ❌ 失敗:{res.get('error')}")
    print(f"寫入 {ok}/{len(parsed)} 組" + (f" → {args.date}" if args.date else ""))
    return 0 if ok == len(parsed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
