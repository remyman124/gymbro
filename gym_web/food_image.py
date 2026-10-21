"""v3.3.32 — 文字輸入嘅食物相策略:食物優先 → 餐廳次選 → 品牌 cover

Jim OOB 2026-10-21:
    「Moreover, the food image capture should be related to the food.
      Secondary option is the restaurant. Sometimes, I would input Protein
      drinks and brand. You research it online and put the brand cover as
      food image」

## 舊流程點解唔得

`/api/scan_preview_text` 原本只係用 Brave Images 搵第一個結果:

    query = "<餐廳> <菜名>" → "<菜名>" → "<用家輸入>"
    → 攞第一個結果,download 咗就算

實測 2026-10-21 嘅結果:

    輸入「Optimum Nutrition Gold Standard」 → 圖片來源 **istockphoto.com**
        (一張普通蛋白粉 stock 影,同品牌完全無關)
    輸入「叉燒飯」                        → 圖片來源 cookpad.com(煮食網,
        啱用但係「代表性」存疑)
    輸入「whey protein chocolate」         → 圖片來源 thefeed.com

即係:**零相關性驗證**,而且品牌飲品永遠搵唔到 brand cover。

## 新策略

| 情況 | 圖片來源(依序) |
|------|-----------------|
| 蛋白飲品 / 蛋白棒(有品牌) | ① 官方產品頁 `og:image`(就係 brand cover) ② 官方/零售商戶域嘅產品照 ③ 普通食物圖 |
| 一般食物 | ① `<餐廳> <菜名>` ② `<菜名>` ③ `<餐廳>`(次選) |
| 冇餐廳 | ① `<菜名>` ② 冇 |

每一個落地的相都會經 **視覺驗證**(問 AI「呢張相係咪 X?」),
唔相關就剔走試下一個 —— 呢個先係「related to the food」嘅真正保證。

本模組**唔 import gym_web**,所有網絡/視覺能力用參數注入(方便 unit test)。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ──────────────────────────────────────────────────────────────────────────
# 網域取捨
# ──────────────────────────────────────────────────────────────────────────

# stock photo / 素材網站 —— 啲相係「generic 圖」,同食物/品牌無關
# (實測「Optimum Nutrition Gold Standard」就係攞咗 istockphoto 張圖)
_STOCK_DOMAINS = (
    "istockphoto", "shutterstock", "gettyimages", "123rf", "dreamstime",
    "depositphotos", "alamy", "stock.adobe", "vectorstock", "pikbest",
    "lovepik", "freepik", "pngwing", "pngitem", "cleanpng", "transparentpng",
    "wallpaper", "zcool", "588ku", "90sheji", "16pic", "ibaotu", "tuchong",
    "pngtree", "picuki", "vectorium", "giphy", "tenor",
)

# 產品照可信嘅零售/官方域(搵 brand cover 嗰陣優先)
_RETAIL_DOMAINS = (
    "optimumnutrition.com", "myprotein.com", "muscletech.com",
    "dymatize.com", "herbalife.com", "questnutrition.com",
    "amazon.", "iherb.", "gnc", "healthline", "hk.healthline",
    "hk.buy.iherb", "watsons", "mannings", "circlek", "7-eleven",
    "hk.foody", "openrice", "delivery", "yoho", "giant", "parknshop",
)


def _domain(url: str) -> str:
    m = re.search(r"https?://([^/]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


def _is_stock(url: str) -> bool:
    d = _domain(url)
    return any(s in d for s in _STOCK_DOMAINS)


def _is_retail(url: str) -> bool:
    d = _domain(url)
    return any(s in d for s in _RETAIL_DOMAINS)


# ──────────────────────────────────────────────────────────────────────────
# 搜尋計劃
# ──────────────────────────────────────────────────────────────────────────

def plan_image_search(
    dish_name: str,
    restaurant: str = "",
    user_text: str = "",
    brand: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """產生依優先次序嘅搜尋計劃。

    每個 step: {"kind", "query", "prefer_domains"}
      kind = "brand_cover" | "food" | "restaurant"
    """
    dn = (dish_name or "").strip()
    rs = (restaurant or "").strip()
    steps: List[Dict[str, Any]] = []

    if brand:
        # ── 蛋白飲品 / 蛋白棒:brand cover 優先 ──
        b = brand["brand"]
        phrase = (brand.get("product_phrase") or "").strip()
        official = brand.get("domain") or ""
        q1 = " ".join(x for x in (b, phrase) if x).strip()
        if q1:
            steps.append({"kind": "brand_cover", "query": q1,
                          "prefer_domains": [official] if official else []})
        q2 = f"{b} {phrase} product".strip()
        steps.append({"kind": "brand_cover", "query": q2,
                      "prefer_domains": [official] if official else []})
        if dn:
            steps.append({"kind": "food", "query": dn, "prefer_domains": []})

    if rs and dn:
        # ⚠️ 餐廳名已經喺菜名入面就唔好再 prepend
        # (實測 2026-10-21:「星巴克拿鐵」→ query 變「星巴克 星巴克拿鐵」)
        if rs.lower() not in dn.lower():
            steps.append({"kind": "food", "query": f"{rs} {dn}",
                          "prefer_domains": []})
    if dn:
        steps.append({"kind": "food", "query": dn, "prefer_domains": []})
    if rs:
        # 次選:餐廳
        steps.append({"kind": "restaurant", "query": rs,
                      "prefer_domains": []})

    # 去重(同 query + kind 一齊重複嘅)
    seen = set()
    out = []
    for s in steps:
        key = (s["kind"], s["query"].lower())
        if not s["query"] or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# ──────────────────────────────────────────────────────────────────────────
# og:image —— 就係「brand cover」
# ──────────────────────────────────────────────────────────────────────────

_OG_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["'][^>]*?content\s*=\s*["']([^"']+)["']""",
    re.I)
_OG_RE_REV = re.compile(
    r"""<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]*?(?:property|name)\s*=\s*["'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["']""",
    re.I)


def extract_og_image(html: str) -> str:
    """由 HTML 抽 og:image / twitter:image。抽唔到回傳 ""。"""
    if not html:
        return ""
    for rx in (_OG_RE, _OG_RE_REV):
        m = rx.search(html)
        if m:
            url = m.group(1).strip()
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith("http"):
                return url
    return ""


# ──────────────────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────────────────

def pick_food_image(
    dish_name: str,
    restaurant: str = "",
    user_text: str = "",
    brand: Optional[Dict[str, Any]] = None,
    search_fn: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None,
    download_fn: Optional[Callable[[str, str], Optional[Path]]] = None,
    verify_fn: Optional[Callable[[Path, str, str], bool]] = None,
    ddg_fn: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None,
    fetch_page_fn: Optional[Callable[[str], str]] = None,
    max_candidates: int = 4,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """搵一張「同食物有關」嘅相。

    回傳 dict:
        image_path  : 本地路徑(冇搵到就 "")
        kind        : "brand_cover" | "food" | "restaurant" | ""
        query       : 用咗嘅 query
        source      : 圖片網域
        title       : 圖片標題
        verified    : 有冇通過視覺驗證
        via         : "og_image" | "search"
        attempts    : 每次嘗試嘅記錄(debug 用)
    """
    result: Dict[str, Any] = {
        "image_path": "", "kind": "", "query": "", "source": "",
        "title": "", "verified": False, "via": "", "attempts": [],
    }
    steps = plan_image_search(dish_name, restaurant, user_text, brand)
    if not steps:
        return result

    def _log(msg: str) -> None:
        if log:
            log(msg)

    # ── 步驟 0:品牌 → 官方產品頁 og:image(就係 brand cover)──
    if brand and ddg_fn and fetch_page_fn:
        b = brand["brand"]
        phrase = (brand.get("product_phrase") or "").strip()
        for q in (f"{b} {phrase}".strip(), f"{b} official product"):
            try:
                pages = ddg_fn(q, 5)
            except Exception as e:  # 搜尋失敗唔應該炸成個 flow
                _log(f"[food_image] ddg failed: {type(e).__name__}: {e}")
                break
            official = (brand.get("domain") or "").lower()
            # 官方域優先
            pages = sorted(
                pages,
                key=lambda p: 0 if (official and official in _domain(p.get("url", ""))) else 1)
            for p in pages[:4]:
                url = p.get("url") or ""
                if not url.startswith("http"):
                    continue
                try:
                    html = fetch_page_fn(url)
                except Exception as e:
                    _log(f"[food_image] fetch failed {url[:60]}: {type(e).__name__}")
                    continue
                og = extract_og_image(html or "")
                if not og:
                    continue
                result["attempts"].append(
                    {"via": "og_image", "url": og, "page": url})
                if download_fn:
                    path = download_fn(og, f"{b}")
                    if path:
                        verified = True
                        if verify_fn:
                            verified = bool(verify_fn(
                                path, b, "brand_cover"))
                        if verified:
                            result.update({
                                "image_path": str(path), "kind": "brand_cover",
                                "query": q, "source": _domain(og),
                                "title": p.get("title", ""), "verified": True,
                                "via": "og_image",
                            })
                            _log(f"[food_image] brand cover via og:image: {og[:90]}")
                            return result
                        # 驗證唔過 → 剔走,試下一個
                        result["attempts"][-1]["verified"] = False
                        _log(f"[food_image] og:image 過唔到視覺驗證,跳過: {og[:70]}")

        # 步驟 0b:官方網站直連(後備)——
        # DDG 每次排序唔同,官方頁有時唔會排第一,或者官方頁本身
        # 冇 og:image。直接打官方網域攞 brand cover 穩陣啲。
        official = (brand.get("domain") or "").lower()
        if official and fetch_page_fn and not result["image_path"]:
            for cand in (f"https://www.{official}/", f"https://{official}/"):
                try:
                    html = fetch_page_fn(cand)
                except Exception as e:
                    _log(f"[food_image] official fetch failed {cand}: "
                         f"{type(e).__name__}")
                    continue
                og = extract_og_image(html or "")
                if not og:
                    continue
                result["attempts"].append(
                    {"via": "og_image", "url": og, "page": cand})
                if not download_fn:
                    continue
                path = download_fn(og, f"{b}")
                if not path:
                    continue
                verified = True
                if verify_fn:
                    verified = bool(verify_fn(path, b, "brand_cover"))
                if verified:
                    result.update({
                        "image_path": str(path), "kind": "brand_cover",
                        "query": f"{b} {phrase}".strip() or b,
                        "source": _domain(og), "title": b,
                        "verified": True, "via": "og_image",
                    })
                    _log(f"[food_image] brand cover via official site: {og[:90]}")
                    return result
                result["attempts"][-1]["verified"] = False
    # ── 步驟 1:逐個搜尋計劃 ──
    tried = 0
    for step in steps:
        if not search_fn or not download_fn:
            break
        q = step["query"]
        try:
            results = search_fn(q, 5) or []
        except Exception as e:
            _log(f"[food_image] search failed {q[:50]}: {type(e).__name__}: {e}")
            continue
        if not results:
            result["attempts"].append({"via": "search", "query": q, "n": 0})
            continue
        # 排序:偏好域 > 零售域 > 其它;剔走 stock 網
        prefer = [d.lower() for d in (step.get("prefer_domains") or [])]
        def _score(r: Dict[str, Any]) -> int:
            d = _domain(r.get("url", ""))
            if prefer and any(p in d for p in prefer):
                return 3
            if _is_retail(r.get("url", "")):
                return 2
            if _is_stock(r.get("url", "")):
                return -1
            return 1
        results = [r for r in results if r.get("url")]
        results.sort(key=_score, reverse=True)
        results = [r for r in results if _score(r) >= 0]
        for r in results:
            if tried >= max_candidates:
                break
            tried += 1
            url = r.get("url", "")
            title = r.get("title", "")
            path = download_fn(url, dish_name or "food")
            rec = {"via": "search", "query": q, "url": url, "title": title}
            if not path:
                rec["error"] = "download_failed"
                result["attempts"].append(rec)
                continue
            verified = True
            if verify_fn:
                verified = bool(verify_fn(path, step["query"], step["kind"]))
            rec["verified"] = verified
            result["attempts"].append(rec)
            if verified:
                result.update({
                    "image_path": str(path), "kind": step["kind"],
                    "query": q, "source": _domain(url), "title": title,
                    "verified": True, "via": "search",
                })
                _log(f"[food_image] picked {step['kind']} image from "
                     f"{_domain(url)} for query {q[:50]!r}")
                return result
            _log(f"[food_image] rejected (唔相關): {title[:50]!r} from {_domain(url)}")
    return result


__all__ = [
    "plan_image_search", "pick_food_image", "extract_og_image",
    "_is_stock", "_is_retail", "_domain",
]
