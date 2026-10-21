"""
PRODUCTION POLLUTION GUARD (added 2026-10-20 after Jim OOB:
"Wait! Someone has pollute the production data. Pls avoid doing so")

The Google Sheet `Nutrition` tab is Jim's GOLDEN COPY of his real food log.
Any test / QA / E2E run that commits a scan into it is a bug.

This test is the safety net: it scans the production sheet for the
unmistakable signature of a failed / synthetic test scan and FAILS the
suite if it finds any.

Test signature = dish name is exactly "未識別菜式" (unrecognised dish) AND
calories == 0. A real Jim scan always has a real dish name and a real
calorie count; a synthetic fixture can never produce those.

Run:  PYTHONPATH='' .venv/bin/python -m pytest tests/qa/test_no_test_pollution.py -v

NOTE: this test only READS the sheet. It never writes.
"""

import json
import os
import re
import time
import urllib.request
import urllib.parse
from pathlib import Path

import pytest

GYM_WEB = Path("/home/work/projects/gymbro/gym_web.py")
TOKEN_PATH = Path("/home/work/.hermes/google_token.json")
TEST_NAMES = {"未識別菜式", "未識別食物", "test", "test fixture", "測試圖片"}


def _sheet_ids():
    src = GYM_WEB.read_text()
    sid = re.search(r'NUTRITION_SHEET_ID\s*=\s*["\']([^"\']+)', src).group(1)
    tab = re.search(r'NUTRITION_SHEET_TAB\s*=\s*["\']([^"\']+)', src).group(1)
    return sid, tab


def _refresh(tok):
    """用 refresh_token 換新 access token,原子寫返落 token 檔。"""
    body = urllib.parse.urlencode({
        "client_id": tok["client_id"],
        "client_secret": tok["client_secret"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=body, method="POST")
    data = json.loads(urllib.request.urlopen(req, timeout=20).read())
    new_at = data["access_token"]
    tok["access_token"] = new_at
    tok["token"] = new_at
    tok["expires_at"] = time.time() + float(data.get("expires_in", 3600))
    tok["refreshed_at"] = time.time()
    # 原子寫:temp file + os.replace(避免半個檔被其他 process 讀到)
    tmp = TOKEN_PATH.with_suffix(".json.tmp-%d" % os.getpid())
    tmp.write_text(json.dumps(tok, indent=2))
    os.replace(tmp, TOKEN_PATH)
    return new_at


def _access_token():
    """攞一個**有效**嘅 access token。

    ⚠️ 2026-10-21 修正:舊版淨係喺 `token` 欄缺失時先 refresh,但個欄
    可能仲留住**已經過期**嘅 token → Sheets API 直接 401,QA 組就報
    「pollution」—— 其實係 token 過期,唔係真有污染(虛驚一場,
    但會掩蓋真正嘅污染)。所以而家:
      1) 讀 `expires_at`,過期就即刻 refresh
      2) 401 都會再 refresh 一次重試
    """
    tok = json.loads(TOKEN_PATH.read_text())
    at = tok.get("access_token") or tok.get("token")
    exp = tok.get("expires_at")
    if at and isinstance(exp, (int, float)) and exp > time.time() + 60:
        return at
    if not tok.get("refresh_token"):
        return at
    return _refresh(tok)


def _read_nutrition_rows():
    at = _access_token()
    if not at:
        pytest.skip("no Google token available — cannot audit production sheet")
    sid, tab = _sheet_ids()
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/"
           f"{urllib.parse.quote(tab, safe='')}!A1:N2000")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + at})
    try:
        payload = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        # token 過期/被 revoke → refresh 一次再試
        tok = json.loads(TOKEN_PATH.read_text())
        at = _refresh(tok)
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + at})
        payload = json.loads(urllib.request.urlopen(req, timeout=30).read())
    return payload.get("values", [])


def test_production_sheet_has_no_test_pollution():
    """The golden copy must never contain a synthetic/failed test row."""
    rows = _read_nutrition_rows()
    offenders = []
    for idx, row in enumerate(rows):
        if len(row) < 6:
            continue
        name = str(row[3]).strip()
        if not name:
            continue
        name_norm = name.lower()
        if name in TEST_NAMES or name_norm in TEST_NAMES:
            try:
                kcal = float(str(row[5]).strip() or 0)
            except ValueError:
                kcal = -1.0
            offenders.append((idx + 1, name, row[5]))

    assert not offenders, (
        "PRODUCTION POLLUTION: the Nutrition tab (Jim's golden copy) contains "
        f"{len(offenders)} test-signature row(s) — name in {sorted(TEST_NAMES)}.\n"
        "These come from QA/E2E runs committing into the live sheet.\n"
        f"  offending rows: {offenders[:20]}\n"
        "Clean them (see gymbro-scan-progress skill '生產資料禁令' → 清理工具箱) "
        "and use synthetic fixtures only."
    )


def test_production_sheet_recent_rows_have_no_zero_kcal_placeholders():
    """Belt-and-braces: any 0-kcal row in the last 60 grid rows is a bug."""
    rows = _read_nutrition_rows()
    tail = rows[-60:]
    offenders = []
    for offset, row in enumerate(tail):
        if len(row) < 6:
            continue
        name = str(row[3]).strip()
        if not name:
            continue
        try:
            kcal = float(str(row[5]).strip() or 0)
        except ValueError:
            continue
        if kcal == 0:
            offenders.append((len(rows) - len(tail) + offset + 1, name, row[5]))
    assert not offenders, (
        "PRODUCTION POLLUTION: zero-calorie rows in the tail of the Nutrition "
        f"tab: {offenders[:20]}"
    )
