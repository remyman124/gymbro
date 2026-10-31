"""
gym_web/ai_profile.py — AI 食物描述 + 教練判詞嘅持久化層 (v3.3.62)

Jim OOB 2026-10-27:
  「For food description and verdict, don't just comment the percentage.
    The ai visual info has more useful info. Don't drop it down the road」

## 問題(點解而家淨係得百分比)

AI 視覺描述 (`vision_desc` — 「一碗白飯配一塊煎雞扒,碟邊有少少黑椒汁,
餐廳係太興」) **從來冇 persist 过**:

  - Sheet 備註欄(J)只寫住 `scan_food` 標記 —— 描述冇入去
  - 本地 `food_scan_log.json` 停喺 2026-08-15(早已唔再寫)

→ 所以下游(JPG 卡 / PDF / 語音)淨係剩返 `_coach_comment_keyword` 嘅
  macro 百分比評論(「脂肪佔 58% 卡路里,偏高」)。
  Jim 講嘅「drop it down the road」係字面意思:**描述喺第一步就跌咗**。

## 修法

本 module 維護一個 per-row cache:

    /home/work/.hermes/nutrition_desc_cache.json
    { "<sheet_row_index>": {description, verdict, grade, ts} }

  - **掃描 commit 時**:直接寫入現成嘅 `vision_desc`(零 AI 成本)
  - **PDF render 時**:缺嘅 row 用 AI 補
    (冇 description → 一次 vision call 同時攞 description + verdict;
     有 description 冇 verdict → 一次 text call 攞 verdict)
    全部平行跑,結果永久 cache 落磁碟。

## 設計原則

1. **唔准淨係講百分比。** 描述同判詞都要引用視覺資訊(食材/份量/煮法)。
2. **AI 死咗唔可以跌返去百分比。** fallback 用「絕對克數 + 視覺描述」,
   例如「以份量睇呢碗偏大,蛋白得 13g,加多隻蛋先夠回復」——
   唔係「蛋白質 16%」。
3. **自我 contained。** 唔 import gym_web.py(會同 package 撞名),
   API 呼叫自己寫。
"""

from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from gym_web.filelock import unique_tmp_path as _unique_tmp_path

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

CACHE_PATH = Path("/home/work/.hermes/nutrition_desc_cache.json")

_lock = threading.Lock()
_cache: dict = {}
_loaded = False

# 視覺請求嘅節流(同 gym_web.py 一致:最少 1.1s 間隔,撞 429 退避 6s)
_vision_lock = threading.Lock()
_vision_last_ts = 0.0
VISION_MIN_INTERVAL = float(os.environ.get("GYMBRO_VISION_MIN_INTERVAL", "1.1"))
VISION_429_SLEEP = float(os.environ.get("GYMBRO_VISION_429_SLEEP", "6"))

# AI 池每次 render 最多補幾多個 row(避免一次燒爆 token)
MAX_AI_PER_RENDER = int(os.environ.get("GYMBRO_PROFILE_AI_MAX", "12"))
AI_WORKERS = int(os.environ.get("GYMBRO_PROFILE_AI_WORKERS", "4"))


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        try:
            raw = CACHE_PATH.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                _cache.update({str(k): v for k, v in data.items()
                               if isinstance(v, dict)})
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[ai_profile] cache load failed: {type(e).__name__}: {e}",
                  flush=True)
        _loaded = True


def _flush() -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # v3.3.71 (Frimpong):tmp 名要唯一。之前用固定嘅 `.json.tmp` →
        # 兩個 thread 同時 flush 會寫入同一個 tmp → 互相覆蓋 / 截斷。
        # 實測過嘅同款事故:google_token.json 被截斷成 0 bytes。
        tmp = _unique_tmp_path(CACHE_PATH)
        tmp.write_text(json.dumps(_cache, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except Exception as e:
        print(f"[ai_profile] cache write failed: {type(e).__name__}: {e}",
              flush=True)


def get_profile(row_index):
    """Return the cached profile for a Sheet row index, or None."""
    _ensure_loaded()
    if row_index is None:
        return None
    with _lock:
        p = _cache.get(str(row_index))
        return dict(p) if isinstance(p, dict) else None


def put_profile(row_index, description: str = "", verdict: str = "",
                grade: str = "", source: str = "") -> dict:
    """Merge-write one row's profile. Existing fields are kept unless
    the new value is non-empty."""
    if row_index is None:
        return {}
    _ensure_loaded()
    with _lock:
        key = str(row_index)
        cur = _cache.get(key) or {}
        for k, v in (("description", description), ("verdict", verdict),
                     ("grade", grade), ("source", source)):
            if v:
                cur[k] = v
        cur["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        _cache[key] = cur
    _flush()
    return dict(cur)


def put_profiles(items: dict) -> None:
    """Bulk merge-write {row_index: {description, verdict, ...}}."""
    if not items:
        return
    _ensure_loaded()
    with _lock:
        for k, v in items.items():
            if not isinstance(v, dict):
                continue
            cur = _cache.get(str(k)) or {}
            for f, fv in v.items():
                if fv:
                    cur[f] = fv
            cur["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            _cache[str(k)] = cur
    _flush()


def profile_count() -> int:
    _ensure_loaded()
    with _lock:
        return len(_cache)


# ---------------------------------------------------------------------------
# MiniMax helpers (self-contained)
# ---------------------------------------------------------------------------

def _api_key() -> str:
    return os.environ.get("MINIMAX_API_KEY", "")


def _throttle_vision() -> None:
    global _vision_last_ts
    with _vision_lock:
        wait = VISION_MIN_INTERVAL - (time.time() - _vision_last_ts)
        if wait > 0:
            time.sleep(wait)
        _vision_last_ts = time.time()


def _post_chat(payload: dict, timeout: int = 90, retry_429: bool = False) -> str:
    """POST to MiniMax chat/completions, return assistant text ('' on failure)."""
    key = _api_key()
    if not key:
        return ""
    req = urllib.request.Request(
        "https://api.minimax.io/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key},
    )
    for attempt in range(2):
        try:
            if payload.get("model") == "MiniMax-text-01":
                _throttle_vision()
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            content = ((data.get("choices") or [{}])[0]
                       .get("message", {}).get("content") or "")
            if "</think>" in content:
                content = content.split("</think>")[-1]
            if content.startswith("<think>"):
                content = ""
            return content.strip()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0 and retry_429:
                print(f"[ai_profile] 429, backing off {VISION_429_SLEEP:.0f}s",
                      flush=True)
                time.sleep(VISION_429_SLEEP)
                continue
            print(f"[ai_profile] HTTP {e.code}: {e.reason}", flush=True)
            return ""
        except Exception as e:
            print(f"[ai_profile] {type(e).__name__}: {e}", flush=True)
            return ""
    return ""


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response."""
    if not text:
        return {}
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_VISION_JSON_PROMPT = (
    "你係資深香港健身教練,亦都係食物視覺分析師。"
    "你望住呢張食物相,輸出**淨 JSON**,唔好 markdown、唔好 ``` 括號、"
    "唔好任何解釋。格式:\n"
    '{"description": "...", "verdict": "..."}\n\n'
    "description: 2-3 句繁體中文廣東話,具體講你見到啲咩食材、"
    "份量幾大(目測大碟/中碟/細碟/一嚿/一碗)、點煮(煎/炸/蒸/炒/滾/焗)、"
    "賣相特徵(例如 邊邊焦、有冇油光、生菜新唔新鮮、醬汁多唔多)。\n"
    "verdict: 2-3 句教練判詞 — 根據你實際見到嘅內容、份量同煮法,"
    "講呢一餐對增肌減脂嘅利弊(例如 蛋白夠唔夠、油多唔多、"
    "有冇纖維),再畀一句好具體嘅調整建議"
    "(例如 走少半碟汁、雞皮唔食、撈走一半汁汁、下一餐加多一碟菜)。\n\n"
    "兩個欄都要具體到唔可以用「蛋白質佔 30%」呢啲百分比代替 —— "
    "要講返你見到嘅嘢。唔准寫「這張圖片顯示」呢啲開場白。"
)

_VERDICT_JSON_PROMPT = (
    "食物:{name}{restaurant}\n"
    "營養:{kcal:.0f} kcal · 蛋白 {p:.0f}g · 碳水 {c:.0f}g · 脂肪 {f:.0f}g\n"
    "視覺描述:{desc}\n\n"
    "你係香港私人健身教練。根據上面嘅**實際內容**(食材、份量、煮法、賣相)"
    "寫 2-3 句判詞,輸出**淨 JSON**,唔好 markdown、唔好解釋:\n"
    '{"verdict": "..."}\n\n'
    "要求:必須引用視覺描述入面嘅具體內容(例如 見到有半碟汁、"
    "睇落係煎唔係炸、份量似大碟),講清對增肌減脂嘅利弊,"
    "再畀一句具體可執行嘅調整。**唔准淨係講宏量營養百分比** —— "
    "百分比可以提,但唔可以係唯一內容。"
)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _vision_profile(img_b64: str) -> dict:
    """One vision call → {description, verdict}."""
    payload = {
        "model": "MiniMax-text-01",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _VISION_JSON_PROMPT},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
        ]}],
        "max_tokens": 1200,
        "temperature": 0.3,
    }
    raw = _post_chat(payload, timeout=90, retry_429=True)
    obj = _extract_json(raw)
    return {
        "description": (obj.get("description") or "").strip(),
        "verdict": (obj.get("verdict") or "").strip(),
    }


def _verdict_only(name: str, restaurant: str, kcal: float, p: float,
                  c: float, f: float, desc: str) -> str:
    payload = {
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content": _VERDICT_JSON_PROMPT.format(
            name=name or "未知食物",
            restaurant=("（%s）" % restaurant) if restaurant else "",
            kcal=kcal or 0, p=p or 0, c=c or 0, f=f or 0,
            desc=desc or "（冇視覺描述）")}],
        "max_tokens": 500,
        "temperature": 0.4,
    }
    raw = _post_chat(payload, timeout=45)
    obj = _extract_json(raw)
    return (obj.get("verdict") or "").strip()


def _thumb_b64(row_index) -> str:
    """Fetch the row's thumbnail and return base64 (empty string on failure)."""
    if row_index is None:
        return ""
    try:
        from gym_web.image_proxy import scan_thumb
        resp = scan_thumb(int(row_index))
        if getattr(resp, "status_code", 0) == 200:
            return base64.b64encode(resp.get_data()).decode("ascii")
    except Exception as e:
        print(f"[ai_profile] thumb fetch failed row={row_index}: "
              f"{type(e).__name__}: {e}", flush=True)
    return ""


def fallback_verdict(name: str, restaurant: str, kcal: float, p: float,
                     c: float, f: float, desc: str) -> str:
    """AI 死咗都用呢個 —— **唔准跌返去淨百分比**。

    用絕對克數 + 份量語言 + 視覺描述,唔係「蛋白質 16%」。
    """
    bits = []
    if desc:
        bits.append(desc.rstrip("。"))
    kcal = kcal or 0
    p = p or 0
    f = f or 0
    c = c or 0
    if kcal >= 600:
        bits.append("以%.0f kcal計份量偏大,想控制總量要留意飯量同醬汁" % kcal)
    elif kcal and kcal <= 60:
        bits.append("呢杯幾乎零熱量,唔使擔心")
    if p >= 25:
        bits.append("蛋白%.0fg算足夠,恢復有底" % p)
    elif p and p < 10:
        bits.append("蛋白得%.0fg,對住呢份份量偏少,加多一隻蛋或者雞胸會好啲" % p)
    if f >= 25:
        bits.append("脂肪%.0fg係呢餐主要熱量來源,煎炸嘅話最易超標" % f)
    if c >= 60:
        bits.append("碳水%.0fg幾乎全部嚟自主食,飯可以減三分之一" % c)
    if not bits:
        bits.append("資料有限,下次影相影清楚啲我先評得準")
    return "。".join(bits) + "。"


def ensure_profiles(meals: list, allow_ai: bool = True) -> dict:
    """Fill in description + verdict for every meal.

    `meals` entries need: row_index, name, restaurant, kcal, p, c, f.
    Returns {row_index: profile_dict}.
    """
    _ensure_loaded()
    out = {}
    need_vision = []
    need_verdict = []

    for m in meals:
        ri = m.get("row_index")
        if ri is None:
            continue
        prof = get_profile(ri) or {}
        if prof.get("description"):
            out[ri] = prof
            if not prof.get("verdict"):
                need_verdict.append(m)
        else:
            need_vision.append(m)

    if not allow_ai or (not need_vision and not need_verdict):
        for m in need_vision + need_verdict:
            ri = m.get("row_index")
            if ri is None:
                continue
            prof = dict(out.get(ri) or get_profile(ri) or {})
            prof["verdict"] = prof.get("verdict") or fallback_verdict(
                m.get("name", ""), m.get("restaurant", ""), m.get("kcal", 0),
                m.get("p", 0), m.get("c", 0), m.get("f", 0),
                prof.get("description", ""))
            out[ri] = prof
        return out

    if len(need_vision) > MAX_AI_PER_RENDER:
        need_vision = need_vision[:MAX_AI_PER_RENDER]
    if len(need_verdict) > MAX_AI_PER_RENDER:
        need_verdict = need_verdict[:MAX_AI_PER_RENDER]

    def _do_vision(m):
        ri = m.get("row_index")
        # v3.3.62: caller 通常已經喺 request thread 攞好縮圖 →
        # 直接用,唔使喺呢個 worker 再攞(冇 request context 會死)。
        raw = m.get("image_bytes")
        b64 = base64.b64encode(raw).decode("ascii") if raw else _thumb_b64(ri)
        if not b64:
            return ri, {}
        return ri, _vision_profile(b64)

    def _do_verdict(m):
        ri = m.get("row_index")
        prof = out.get(ri) or get_profile(ri) or {}
        v = _verdict_only(m.get("name", ""), m.get("restaurant", ""),
                          m.get("kcal", 0), m.get("p", 0), m.get("c", 0),
                          m.get("f", 0), prof.get("description", ""))
        return ri, v

    results = {}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, AI_WORKERS)) as ex:
        futs = []
        for m in need_vision:
            futs.append(ex.submit(_do_vision, m))
        for m in need_verdict:
            futs.append(ex.submit(_do_verdict, m))
        for f in concurrent.futures.as_completed(futs):
            try:
                ri, payload = f.result(timeout=120)
            except Exception as e:
                print(f"[ai_profile] worker failed: {type(e).__name__}: {e}",
                      flush=True)
                continue
            if ri is None:
                continue
            if isinstance(payload, dict):
                results[ri] = {k: v for k, v in payload.items() if v}
            elif payload:
                results.setdefault(ri, {})["verdict"] = payload

    if results:
        put_profiles(results)

    for m in meals:
        ri = m.get("row_index")
        if ri is None:
            continue
        prof = dict(out.get(ri) or get_profile(ri) or {})
        if not prof.get("description"):
            prof["description"] = ""
        if not prof.get("verdict"):
            prof["verdict"] = fallback_verdict(
                m.get("name", ""), m.get("restaurant", ""), m.get("kcal", 0),
                m.get("p", 0), m.get("c", 0), m.get("f", 0),
                prof.get("description", ""))
        out[ri] = prof

    return out


if __name__ == "__main__":
    print("cache:", CACHE_PATH, "rows:", profile_count())
