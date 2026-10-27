"""QA — v3.3.52: 平行 AI 掃描唔可以撞名(否則縮圖全部一樣 + 相片被吞)。

Jim OOB 2026-10-27 20:1x「For food upload, the immediate thumbnail are all the
same. Despite in the end, it works」。

實測證據(2026-10-27 20:01 批次,4 張相):
  * 上載:upload_20261027_200113 / _200115 / _200117 / _200120 (4 個唔同檔案)
  * log:  `running 4 photos in PARALLEL (3 workers)` → 3 個 worker 喺
    12:01:20 同一秒開工。
  * 但 scan_cache 只有 2 個 final 檔:
        scan_20261027_200120.jpg  (md5 == upload_20261027_200113 嘅相)
        scan_20261027_200212.jpg  (md5 == upload_20261027_200120 嘅相)
    → 2259737 bytes 嗰張同 3711222 bytes 嗰張喺本地**完全消失**。
  * Sheet 4 條 row 嘅 Drive URL 各自獨立(所以食物記錄係啱嘅),
    但 queue 縮圖全部指向同一個本地 /scan_img/scan_20261027_200120.jpg
    → 4 個縮圖一模一樣 ❗

根因:`final_name = f"scan_{now_hkt_dt.strftime('%Y%m%d_%H%M%S')}.jpg"`
只到**秒**精度。`now_hkt_dt` 喺 function 入口賦值,而 3 個平行
worker 同一秒開工 → 同一個 final_name → shutil.copyfile 互相覆蓋。

同一類 bug 喺 v3.3.25 (2026-10-21) 已經喺 **upload** 路徑修過
(`stored_name` 加 uuid),但 **AI/commit** 路徑漏咗 —— 呢個就係
Jim 講嘅「systemic problem」。

呢個 suite 守住:
  1. final_name 必須每個 photo 都唔同(uuid / monotonic 保證)。
  2. 唔准再有任何「秒精度」嘅 final 檔名(唔准撞名)。
  3. 同一個 final_name 寫入兩次 → 唔可以靜靜哋吞相。
  4. 回歸守衛:upload 路徑嘅 uuid 唔准被移除。
  5. 回歸守衛:唔准改壞其他 scan 相關 function 嘅存在。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "gym_web.py"


def _src():
    return WEB.read_text(encoding="utf-8")


def _code_only():
    """Source with comments stripped.

    必要:成段註解會引用舊嘅 `final_name = f"scan_{...}.jpg"` 作為 bug
    說明,如果唔剝走註解,source-grep 測試會誤以為 bug 仲喺度。
    """
    out = []
    for line in WEB.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        # 去掉行尾註解(小心唔好斬斷字串內嘅 #)
        if "#" in line:
            in_s = in_d = False
            cut = len(line)
            for i, ch in enumerate(line):
                if ch == "'" and not in_d:
                    in_s = not in_s
                elif ch == '"' and not in_s:
                    in_d = not in_d
                elif ch == "#" and not in_s and not in_d:
                    cut = i
                    break
            line = line[:cut]
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 1. final_name 必須唯一
# ---------------------------------------------------------------------------

def test_final_name_has_unique_component():
    """final_name 必須包含 uuid / nanosecond / monotonic 呢類唯一成分。"""
    src = _code_only()
    # final_name 嘅賦值有兩種合法形式:
    #   (a) final_name = _unique_scan_name("scan", ".jpg", now_hkt_dt)  ← 推薦
    #   (b) final_name = f"...uuid..."                                 ← 直接寫 uuid
    helper_calls = re.findall(r'final_name\s*=\s*_unique_scan_name\(', src)
    fstring_finals = re.findall(r'final_name\s*=\s*f"([^"]+)"', src)
    assert helper_calls or fstring_finals, "搵唔到 final_name"

    bad = []
    for f in fstring_finals:
        if "uuid" in f or "%f" in f or "token_hex" in f:
            continue
        if re.search(r"%Y%m%d_%H%M%S", f):
            bad.append(f)
    assert not bad, (
        "呢啲 final 檔名淨係得秒精度 → 平行 worker 同一秒會撞名,後寫嘅"
        "覆蓋前寫嘅(相片被吞 + 縮圖全部一樣):\n  " + "\n  ".join(bad)
    )


def test_final_name_uses_uuid():
    """必須真係用 uuid,而唔係只靠 timestamp。"""
    src = _code_only()
    helper_calls = re.findall(r'final_name\s*=\s*_unique_scan_name\(', src)
    fstring_finals = re.findall(r'final_name\s*=\s*f"([^"]+)"', src)
    assert helper_calls or fstring_finals
    # 每個 final_name 都必須有唯一成分:helper 调用 或者 f-string 內有 uuid
    assert all("uuid" in f or "token_hex" in f or "%f" in f
               for f in fstring_finals), (
        "final_name 應該用 _unique_scan_name() 或者直接加 uuid:%s" % fstring_finals
    )
    # 而且至少要有 final_name 經 helper(集中處理)
    assert helper_calls, "final_name 應該用 _unique_scan_name() 集中處理唯一命名"


def test_uuid_is_imported():
    """gym_web.py 必須 import 到 uuid(唔係靠 wildcard)。"""
    src = _src()
    assert re.search(r"^import uuid", src, re.M) or re.search(r"^import .*\buuid\b", src, re.M), (
        "gym_web.py 冇 import uuid"
    )


# ---------------------------------------------------------------------------
# 2. 唔准再有任何秒精度嘅 scan 檔名
# ---------------------------------------------------------------------------

def test_no_second_resolution_scan_filenames():
    """任何 `scan_<timestamp>.jpg` 都要有唯一後綴。"""
    src = _code_only()
    # 搵出所有 f"scan_{...}.jpg" 形式
    pats = re.findall(r'f"(scan_\{[^"]*?\}\.jpg)"', src)
    assert pats, "搵唔到 scan_*.jpg 命名"
    bad = [p for p in pats
           if "uuid" not in p and "%f" not in p and "token_hex" not in p
           and "safe_slug" not in p]
    assert not bad, (
        "呢啲 scan 檔名得秒精度,平行處理會撞名:\n  " + "\n  ".join(bad)
    )


def test_preview_filename_also_unique():
    """preview_*.jpg 一樣有撞名風險(多張相同時 preview)。"""
    src = _src()
    for m in re.finditer(r'f"(preview_\{[^"]*?\}\.jpg)"', src):
        f = m.group(1)
        assert ("uuid" in f or "token_hex" in f or "%f" in f
                or "safe_slug" in f or "uuid4" in f), (
            "preview 檔名得秒精度會撞名:%s" % f
        )


# ---------------------------------------------------------------------------
# 3. 撞名唔可以靜靜哋吞相
# ---------------------------------------------------------------------------

def test_final_write_detects_existing_file():
    """寫 final 檔之前必須檢查係咪已經存在(唔可以靜靜哋覆蓋)。"""
    src = _src()
    assert re.search(r"final_path\s*\.exists\(\)|_unique_scan_name|_claim_scan_name", src), (
        "final 檔名寫入之前完全冇檢查係咪已經存在 → 相片會靜靜哋被吞"
    )


def test_unique_name_helper_exists():
    """應該有一個集中嘅 helper 產生唯一名(避免散落各處)。"""
    src = _src()
    assert re.search(r"def _unique_scan_name\(", src), (
        "應該有一個 _unique_scan_name() helper,集中處理唯一命名"
    )


def test_unique_name_helper_is_collision_safe():
    """helper 本身必須保證唔會返回已存在嘅名(要真係 atomic)。"""
    src = _src()
    m = re.search(r"def _unique_scan_name\((.*?)\n(?=\S)", src, re.S)
    assert m, "搵唔到 _unique_scan_name"
    body = m.group(1)
    assert "exists()" in body or "O_EXCL" in body or "uuid" in body, (
        "_unique_scan_name 必須保證唯一(用 exists() 檢查或 uuid)"
    )


# ---------------------------------------------------------------------------
# 4/5. 回歸守衛
# ---------------------------------------------------------------------------

def test_upload_path_uuid_still_there():
    """v3.3.25 修過嘅 upload 路徑 uuid 唔准被移除。"""
    src = _src()
    assert 'stored_name = f"upload_{ts_str}_{uuid.uuid4().hex[:8]}_{i}{ext}"' in src, (
        "upload 路徑嘅 uuid 命名被移除咗 —— v3.3.25 修過嘅 bug 會復發"
    )


def test_core_scan_functions_still_exist():
    """呢次修正唔准改壞其他嘢。"""
    src = _src()
    for fn in ("_scan_preview_core", "_append_to_sheet_nutrition",
               "_minimax_vision", "_extract_dish_name", "_make_thumb",
               "_thumb_url_for", "_upload_to_drive"):
        assert f"def {fn}" in src, f"{fn} 唔見咗"
