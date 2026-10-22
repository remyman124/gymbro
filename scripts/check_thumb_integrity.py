#!/usr/bin/env python3
"""scripts/check_thumb_integrity.py — 縮圖完整性診斷(唯讀,唔會改任何嘢)

用途:Jim 話「啲縮圖一樣」時,一次性查清楚係邊一層出事:
  1. Sheet column K 重複      → 兩行指向同一個 Drive 檔(縮圖自然一樣)
  2. Drive 檔已被 trash       → Drive 會回同一張「垃圾桶」placeholder → 縮圖一樣
  3. Drive 檔唔存在           → 圖派唔到
  4. 伺服器派嘅縮圖 byte 相同 → 真正 byte 级重複(placeholder 證據)

用法:
    python3 scripts/check_thumb_integrity.py            # 全表掃描
    python3 scripts/check_thumb_integrity.py --rows 650-671   # 只掃行範圍
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gym_web.core import (  # noqa: E402
    NUTRITION_SHEET_ID,
    NUTRITION_TAB_NAME,
    drive_file_is_trashed,
    get_google_access_token,
)

BASE = "http://127.0.0.1:7000"


def _sheet_rows(access: str) -> list:
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{NUTRITION_SHEET_ID}"
           f"/values/{NUTRITION_TAB_NAME}!A1:K1000")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
    body = json.loads(urllib.request.urlopen(req, timeout=60).read())
    out = []
    for i, r in enumerate(body.get("values", [])):
        c = (r + [""] * 11)[:11]
        fid = ""
        if len(c) > 10 and "id=" in (c[10] or ""):
            fid = c[10].split("id=", 1)[1].split("&", 1)[0]
        out.append({"row": i + 1, "date": c[0], "time": c[1],
                    "name": (c[3] or "")[:34], "fid": fid})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", help="行範圍,例如 650-671")
    ap.add_argument("--skip-bytes", action="store_true",
                    help="唔好逐行拉 /scan_thumb(慢,但最直接)")
    args = ap.parse_args()

    access = get_google_access_token()
    if not access:
        print("❌ 攞唔到 Google access token")
        return 2

    rows = _sheet_rows(access)
    lo, hi = 2, len(rows)
    if args.rows:
        a, _, b = args.rows.partition("-")
        lo = int(a) if a else lo
        hi = int(b) if b else hi
    rows = [r for r in rows if lo <= r["row"] <= hi]
    with_fid = [r for r in rows if r["fid"]]
    print(f"掃描範圍 row {lo}-{hi}:{len(rows)} 行 | 有相 {len(with_fid)} 行 | 純文字 {len(rows)-len(with_fid)} 行")

    # 1. column K 重複
    dup = {k: v for k, v in Counter(r["fid"] for r in with_fid).items() if v > 1}
    print(f"\n═══ 1. column K 重複(同一個檔被引用多次):{len(dup)} ═══")
    for fid, n in dup.items():
        rs = [r for r in with_fid if r["fid"] == fid]
        print(f"   {fid} × {n}")
        for r in rs:
            print(f"      row {r['row']} | {r['date']} {r['time']} | {r['name']}")

    # 2/3. trashed / 唔存在
    trashed, missing, unknown = [], [], []
    for r in with_fid:
        st = drive_file_is_trashed(r["fid"])
        if st is True:
            trashed.append(r)
        elif st is False:
            pass
        else:
            unknown.append(r)
    print(f"\n═══ 2. Drive 檔已被 trash(縮圖會變成同一張 placeholder):{len(trashed)} ═══")
    for r in trashed:
        print(f"   row {r['row']} | {r['date']} {r['time']} | {r['name']} | {r['fid']}")
    print(f"\n═══ 3. 查唔到(可能已被刪/權限問題):{len(unknown)} ═══")
    for r in unknown[:20]:
        print(f"   row {r['row']} | {r['date']} {r['time']} | {r['name']} | {r['fid']}")

    # 4. byte 级重複
    if not args.skip_bytes:
        print("\n═══ 4. 伺服器實際派嘅縮圖(byte 级重複檢查)═══")
        seen = {}
        for r in with_fid:
            try:
                req = urllib.request.Request(f"{BASE}/scan_thumb/{r['row']}")
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                    code = resp.status
            except Exception as e:
                print(f"   row {r['row']} → 拉唔到: {e}")
                continue
            h = hashlib.md5(data).hexdigest()[:12]
            seen.setdefault(h, []).append((r["row"], len(data), code))
        dups = {h: v for h, v in seen.items() if len(v) > 1}
        print(f"   拉咗 {len(seen)} 張 | 相同 byte 組: {len(dups)}")
        for h, v in dups.items():
            print(f"   md5 {h} × {len(v)}: " + ", ".join(f"row{r}({sz}B,{c})" for r, sz, c in v))
        ok = sum(1 for h, v in seen.items() if len(v) == 1)
        print(f"   ✅ 獨立縮圖 {ok} 張 | ⚠️ 重複組 {len(dups)} 組")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
