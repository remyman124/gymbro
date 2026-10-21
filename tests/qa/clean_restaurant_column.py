"""清理 production `Nutrition` tab 餐廳欄入面嘅 AI 廢話。

背景:2026-10-21 實測 production 餐廳欄 145 行係 AI 嘅半句說話
    (「難以辨認餐廳」45、「描述可靠識別餐廳」13、「所以餐廳」…)。

做法(只郁 column E「餐廳/連鎖」,其他欄一律唔掂):
  1. 讀全表
  2. 每行餐廳值行消毒器 → 垃圾 → 先試用「歷史索引」反查返真店名
     (用同一碟菜名喺其他乾淨行出現過嘅店)
  3. 反查唔到 → 清空(唔會發明)
  4. 用 batchUpdate 一次過寫,只寫 column E
  5. 逐行對數

⚠️ 只清 column E。餐名 / 營養 / 時間 / 圖片 一律唔改。
⚠️ 預設 dryrun,加 --apply 先會真寫。
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, "/home/work/projects/gymbro")
from gym_web.food_context import (sanitise_restaurant_name,  # noqa: E402
                                   build_restaurant_history)

SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
TAB = "Nutrition"
TOKEN_FILE = Path("/home/work/.hermes/google_token.json")
APPLY = "--apply" in sys.argv


def load_token():
    return json.loads(TOKEN_FILE.read_text())["access_token"]


def sheet_range(tok, rng):
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
           + urllib.parse.quote(f"{TAB}!{rng}", safe=""))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    return json.loads(urllib.request.urlopen(req, timeout=60).read()).get("values", [])


def main():
    tok = load_token()
    rows = sheet_range(tok, "A1:N2000")
    if not rows:
        print("表係空嘅")
        return 1
    header, body = rows[0], rows[1:]

    # 歷史索引:用所有「餐廳乾淨」嘅行建
    clean_rows = []
    for r in body:
        if len(r) < 5:
            continue
        rest = (r[4] or "").strip()
        if rest and sanitise_restaurant_name(rest) == rest:
            clean_rows.append({"name": (r[3] or "").strip(),
                               "restaurant_chain": rest})
    hist = build_restaurant_history(sheet_rows=clean_rows, scan_entries=[])

    edits = []
    filled = blanked = kept = 0
    detail = []
    for i, r in enumerate(body, start=2):  # Sheet row index (1-based, header=1)
        rest = (r[4] if len(r) > 4 else "") or ""
        rest = rest.strip()
        if not rest:
            continue
        if sanitise_restaurant_name(rest) == rest:
            kept += 1
            continue
        dish = (r[3] if len(r) > 3 else "") or ""
        dish = dish.strip()
        # 反查:同一碟菜喺乾淨行出現過嘅店
        new_rest = ""
        if dish:
            got, src = hist.lookup(dish, dish, "")
            if got and src in ("history", "text"):
                new_rest = got
        if new_rest:
            filled += 1
        else:
            blanked += 1
        detail.append((i, dish[:30], rest[:34], new_rest))
        edits.append({
            "range": f"{TAB}!E{i}:E{i}",
            "values": [[new_rest]],
        })

    print(f"總行數(不含表頭): {len(body)}")
    print(f"餐廳欄乾淨,唔郁:  {kept}")
    print(f"AI 廢話,清走:      {blanked}")
    print(f"AI 廢話,反查返真店: {filled}")
    print(f"合共要改 column E:  {len(edits)}")
    print()
    print("前 25 個改動:")
    for i, dish, old, new in detail[:25]:
        print(f"  row {i:4d} | {dish:30s} | {old:34s} → {new or '(清空)'}")

    if not APPLY:
        print("\n(dryrun — 加 --apply 先會真寫)")
        return 0

    # 備份原值(可以 100% 還原)
    bak = Path("/home/work/projects/gymbro/scan_cache/restaurant_cleanup_backup.json")
    bak.parent.mkdir(parents=True, exist_ok=True)
    bak.write_text(json.dumps(
        {"sheet": SHEET_ID, "tab": TAB, "column": "E",
         "rows": [{"row": i, "dish": dish, "old": old, "new": new or ""}
                  for i, dish, old, new in detail]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n💾 原值備份: {bak}")

    if not edits:
        print("冇嘢要改")
        return 0
    body_json = {"valueInputOption": "RAW", "data": edits}
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/"
           f"values:batchUpdate")
    req = urllib.request.Request(
        url, data=json.dumps(body_json).encode(), method="POST",
        headers={"Authorization": "Bearer " + tok,
                 "Content-Type": "application/json"})
    res = json.loads(urllib.request.urlopen(req, timeout=120).read())
    total = res.get("totalUpdatedCells", 0)
    print(f"\n✅ 已寫入 {total} 個 cell(column E)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
