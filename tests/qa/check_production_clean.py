"""檢查 production `Nutrition` tab 有冇被測試污染。

Jim OOB 2026-10-20「someone has pollute the production data」——
golden copy 係神聖嘅。呢個 script 每次 QA / 部署後都要行。
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

SHEET_ID = "1YKjsQbTa3nBN7ubmD-zXAQHcuhDlQ1QaqeN_Cog6Oag"
TAB = "Nutrition"
TOKEN_FILE = Path("/home/work/.hermes/google_token.json")

JUNK_RE = re.compile(
    r"(難以辨認|描述可靠|可靠識別|相片可靠|睇唔到餐廳|冇見到餐廳|未見到餐廳|"
    r"所以餐廳|如果你係做餐廳|連鎖餐廳|一般茶餐廳|圖中未見到|頭都係一般|"
    r"屬於香港|單一香港|楚具體係|景係一個|自[主動].{0,4}火鍋|外賣餐廳|"
    r"茶餐廳|自助手|主動式|TBD|未識別菜式|未識別)")


def load_token():
    for cand in (TOKEN_FILE, Path("/home/work/projects/gymbro/google_token.json")):
        if cand.exists():
            try:
                return json.loads(cand.read_text())["access_token"]
            except Exception:
                continue
    raise SystemExit("搵唔到 Google token")


def read_rows(token):
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
           + urllib.parse.quote(TAB, safe="") + "!A1:N2000")
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token})
    data = json.loads(urllib.request.urlopen(req, timeout=60).read())
    return data.get("values", [])


def main():
    token = load_token()
    rows = read_rows(token)
    print(f"Nutrition tab 總行數: {len(rows)}")
    if not rows:
        print("  (空表)")
        return 0

    hdr = rows[0]
    body = rows[1:]

    # A=日期 B=時間 C=餐別 D=餐名 E=餐廳/連鎖 F=kcal ...
    by_date = Counter()
    junk_rows = []
    generic_rows = []
    for r in body:
        d = (r[0] if len(r) > 0 else "").strip()
        rest = (r[4] if len(r) > 4 else "").strip()
        name = (r[3] if len(r) > 3 else "").strip()
        by_date[d] += 1
        if JUNK_RE.search(rest):
            junk_rows.append((d, name, rest))
        if name in ("未識別菜式", "未識別"):
            generic_rows.append((d, name))

    print("\n最近 8 個日期行數:")
    for d, n in sorted(by_date.items(), reverse=True)[:8]:
        print(f"   {d or '(blank)':12s} {n}")

    print(f"\n餐廳欄垃圾行: {len(junk_rows)}")
    for d, n, rest in junk_rows[:10]:
        print(f"   {d} | {n!r} | {rest!r}")

    print(f"\n『未識別菜式』行: {len(generic_rows)}")
    for d, n in generic_rows[:10]:
        print(f"   {d} | {n!r}")

    # 今日 / 昨日
    import datetime
    hk = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    today = hk.strftime("%Y-%m-%d")
    yday = (hk - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"\n今日 ({today}) 行數: {by_date.get(today, 0)}")
    print(f"昨日 ({yday}) 行數: {by_date.get(yday, 0)}")

    bad = 0
    if generic_rows:
        print("❌ 有『未識別菜式』")
        bad = 1
    if junk_rows:
        print("❌ 餐廳欄有垃圾值")
        bad = 1
    if not bad:
        print("✅ production 表格乾淨")
    return bad


if __name__ == "__main__":
    sys.exit(main())
