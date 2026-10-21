"""v3.3.32 — 餐廳名消毒 + 餐廳歷史索引 + 品牌識別

Jim OOB 2026-10-21:
    「I want you to fix the manual food input workflow. It has been very buggy.
      Can you fill in the restaurant (in best effort basis) based on what I inputted.
      Moreover, the food image capture should be related to the food. Secondary
      option is the restaurant. Sometimes, I would input Protein drinks and brand.
      You research it online and put the brand cover as food image」

## 點解要有呢個模組

實測 production Sheet (`Nutrition` tab, 658 行) 嘅 **餐廳欄**:

    難以辨認餐廳 45 · 描述可靠識別餐廳 13 · 睇唔到餐廳 12 · 冇見到餐廳 12
    一間香港連鎖餐廳 10 · 相片可靠識別餐廳 5 · 所以餐廳 2
    「如果你係做餐廳」 1 · 「碟炸蔬菜係喺餐廳」 1 · 「楚具體係邊間餐廳」 1
    「景係一個露天餐廳」 1 · 「頭都係一般茶餐廳」 1

即係 **90% 係 AI 講嘅半句說話**,零資訊量。根因:`/api/scan_preview_text`
把 MiniMax 回傳嘅 `restaurant_guess` **原封不動**寫入 Sheet,冇任何
sanitise(消毒)步驟。

所以呢個模組做三件事:

1. `sanitise_restaurant_name()` —— 任何餐廳字串入去,垃圾變空字串
2. `build_restaurant_history()` + `lookup_restaurant()` —— 由歷史紀錄
   (Sheet + scan log) 建索引,best-effort 自動填餐廳
3. `match_brand()` —— 識別蛋白粉 / 蛋白棒 / RTD 飲品品牌,俾圖片策略
   (brand cover) 同營養覆蓋用

**設計原則**:寧願留空,都唔好寫入垃圾。空欄 = 零資訊損失;垃圾欄 = 污染
golden copy,而且會令下一次「歷史索引」再食返啲垃圾。

本模組**唔 import gym_web**(避免循環引用),所有依賴用參數注入。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────────
# 1. 餐廳名消毒器
# ──────────────────────────────────────────────────────────────────────────

# 散文標記 —— 出現喺餐廳字串入面 = AI 講咗句說話,而唔係一個名
# (由 production Sheet 實測 90% 垃圾值反推 + 一般化)
_JUNK_MARKERS: Tuple[str, ...] = (
    # 「睇唔到 / 冇見到」類
    "見到", "見唔到", "睇唔到", "睇到", "冇", "唔見", "唔知", "未知",
    "未能", "唔能", "未見", "未顯", "未提", "冇提",
    # 「AI 自我描述」類
    "難以", "辨認", "識別", "可靠",    "描述", "相片", "相中", "圖中", "圖片",
    "畫面", "照片",
    # 「不肯定 / 假設」類
    "一間", "任何", "所有", "應該", "所以", "如果", "邊間", "呢間", "嗰間",
    "可能", "似乎", "大概", "估計", "猜", "通常", "一般", "普通", "家用",
    "具體", "應該係", "做成", "做餐廳", "連鎖", "屬於", "唯一",
    # 散文式嘅「睇落/好似/以下為…」類 —— 實測 2026-10-21 清理時
    # 發現 production 仲有「似典型港式茶餐廳」「餐睇落好似茶餐廳」
    # 「以下為香港茶餐廳」「境似日本居酒屋」「下按一個標準餐廳」
    # 「足夠資料判定餐廳」「第一道菜所屬餐廳」「採用香港中式餐廳」…
    "似典型", "境似", "睇落", "睇起", "好似", "看似", "感覺", "以下",
    "採用", "判定", "足夠", "第一道菜", "其他配料", "或者餐廳",
    "至特定", "標準餐廳", "下按", "按香港", "所屬", "認其他", "或者", "典型",
    # 虛詞(喺真實店名入面極罕見)
    "係", "嘅", "啦",
    # 未定標記
    "tbd", "n/a", "unknown", "pending",
)

# 泛用場所字尾 —— 個名係一個場所類別,唔係一個專有名詞
# ⚠️ 唔可以加「飯店」(黃金飯店)、「酒樓」(譚仔酒樓)、「冰室」(華星冰室)
# ⚠️ 只喺名字夠短(≤6 字)時先當垃圾 ——「新羅寶韓國餐廳」(7 字)係真店名
_JUNK_SUFFIXES: Tuple[str, ...] = (
    "餐廳", "連鎖", "茶餐廳", "咖啡店", "快餐店", "小店", "火鍋店",
    "外賣店", "小食店", "食肆",
)
_JUNK_SUFFIX_MAX_LEN = 6

# 純場所類型(要完全一樣先算垃圾)——
# ⚠️ 唔可以當 suffix 用:「鳥丈居酒屋」係真店名,
#    但「居酒屋」三隻字淨係一個場所類別。
_JUNK_EXACT_VENUE = {
    "居酒屋", "酒樓", "茶餐廳", "餐廳", "咖啡店", "快餐店", "外賣店",
    "小店", "食肆", "火鍋店", "酒吧", "燒烤店", "粉麵店", "茶居",
}

# 純「菜系 / 餐廳類型」—— 係描述,唔係店名
_JUNK_CUISINES: Tuple[str, ...] = (
    "韓式燒肉", "中式", "日式", "西式", "泰式", "韓式", "越式", "台式",
    "港式", "粵菜", "川菜", "韓菜", "日式料理", "中式料理", "港式料理",
    "印度菜", "fusion", "餐廳", "連鎖餐廳",
)

# 英文泛用「場所」字串 —— 「Hainanese chicken rice shop」呢類
# (唔可以用裸 "shop" / "cafe",「Joe's Cafe」係真名)
_JUNK_EN_PHRASES: Tuple[str, ...] = (
    "rice shop", "coffee shop", "food shop", "food court", "eatery",
    "diner", "cafeteria", "buffet", "bbq joint", "takeaway shop",
    "fast food", "chain restaurant", "restaurants",
)

# 句子標點 —— 出現即代表 AI 輸出係句子而唔係名
# (括號唔算:「溫野菜 (Noubeni Shabu Shabu)」、「沙嗲王 (Satay King)」都係真名)
_SENTENCE_PUNCT: Tuple[str, ...] = ("。", "！", "？", "!", "?", ";", "；", "、")

# 唔准出現喺餐廳名嘅字符
# ⚠️ 唔好封鎖單引號 ——「Triple O's」、「McDonald's」、「D'Onofrio's」都係真名
_ILLEGAL_CHARS = re.compile(r"[\"`\\/<>{}|]")


_GENERIC_PRODUCT_NOUNS: Tuple[str, ...] = (
    "蛋白粉", "乳清蛋白", "蛋白質粉", "乳清蛋白粉", "蛋白飲", "蛋白棒",
    "蛋白質棒", "雞胸肉", "蛋白", "奶粉", "能量棒", "奶昔", "乳清",
    "補劑", "健身補劑", "蛋白補充劑", "健身粉", "乳清粉",
)


def product_name_is_specific(name: str) -> bool:
    """AI 為品牌產品出嘅菜名係咪夠具體。

    實測 2026-10-21:同一個「Optimum Nutrition Gold Standard」,
    AI 一次出「金標乳清蛋白粉」(啱),另一次出「雞胸肉」(完全唔啱)。
    品牌產品嘅名應該帶品牌/型號,唔應該淨係一個泛用食物名詞。
    """
    n = (name or "").strip()
    if not n:
        return False
    for g in _GENERIC_PRODUCT_NOUNS:
        if g in n:
            return False
    return True


def sanitise_restaurant_name(raw: Any, dish_name: str = "") -> str:
    """把任何餐廳字串消毒。垃圾 → 空字串;真名 → 乾淨版本。

    規則(全部由 production Sheet 實測垃圾值反推):
      1. 非字串 / 太短(<2) / 太長(>40) → ""
      2. 含散文標記(_JUNK_MARKERS) → ""
      3. 以泛用場所字尾(_JUNK_SUFFIXES)結尾 → ""
      4. 命中純菜系(_JUNK_CUISINES) → ""
      5. 含句子標點 → ""
      6. 含非法字符 → ""
      7. 同菜名完全一樣(AI 懶得填,直接抄菜名) → ""
      8. 清理首尾空白 + 括號內容(保留括號,例如「沙嗲王 (Satay King)」)

    `dish_name` 用嚟做第 7 條嘅比對。
    """
    if not isinstance(raw, str):
        return ""
    s = raw.strip().strip("　 ")
    if len(s) < 2 or len(s) > 40:
        return ""
    # 句子標點
    if any(p in s for p in _SENTENCE_PUNCT):
        return ""
    # 非法字符
    if _ILLEGAL_CHARS.search(s):
        return ""
    low = s.lower()
    # 散文標記
    if any(m in low for m in _JUNK_MARKERS):
        return ""
    # 泛用場所字尾(只限短名,避免誤殺「新羅寶韓國餐廳」)
    if len(s) <= _JUNK_SUFFIX_MAX_LEN and any(s.endswith(sfx) for sfx in _JUNK_SUFFIXES):
        return ""
    # 純場所類型(淨係呢個字)
    if s in _JUNK_EXACT_VENUE:
        return ""
    # 純菜系
    if any(low == c or low.startswith(c) for c in _JUNK_CUISINES):
        return ""
    # 英文泛用場所
    if any(p in low for p in _JUNK_EN_PHRASES):
        return ""
    # 同菜名一樣 → 唔算餐廳
    dn = (dish_name or "").strip()
    if dn and low == dn.lower():
        return ""
    # 「... (English)」→ 保留,但如果括號入面係散文就照剔(上面已處理)
    return s


def is_plausible_restaurant(raw: Any) -> bool:
    """bool 版本:sanitise 完仲有嘢 = 似餐廳名。"""
    return bool(sanitise_restaurant_name(raw))


# ──────────────────────────────────────────────────────────────────────────
# 2. 餐廳歷史索引
# ──────────────────────────────────────────────────────────────────────────

def _norm_dish(s: str) -> str:
    """菜名正規化:細寫、去空白、去標點、去常見量詞。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).strip().lower()
    s = re.sub(r"[\s　]+", "", s)
    s = re.sub(r"[，。、,.\-–—_/()（）\[\]「」『』:：,]", "", s)
    # 常見份量/修飾詞
    s = re.sub(r"(\d+)?(個|杯|碟|碗|份|件|條|包|樽|罐|盒|杯)", "", s)
    return s


def _bigrams(s: str) -> set:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _tokens(s: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) >= 3}


def _dish_similarity(a: str, b: str) -> float:
    """兩道菜名嘅相似度(0.0 - 1.0)。

    之前用「重疊 bigram 數 ≥ 2」做門檻,結果「乳清蛋白粉」會撞到
    「Dim sum dinner (8 dishes, Jim 60%)」呢類完全無關嘅菜
    (實測 2026-10-21,score 2.0 剛好過門檻)。

    而家分三級(由嚴到鬆):
      1. 正規化後完全一樣              → 1.00
      2. 一個係另一個嘅子字串          → 0.95
         (「叉燒」⊂「叉燒飯」、「叉燒飯」⊂「叉燒飯海鮮」)
      3. 英文 token 重疊 ≥ 2 且 bigram
         相似度 ≥ 0.5                   → 0.5 + 0.1×token 數
         ⚠️ 一定要 token ≥ 2 —— 否則「chocolate cake」會撞
            「chocolate peanut butter bar」(淨係「chocolate」一個
            共通,但 bigram 比例好高)
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.95
    ab, bb = _bigrams(a), _bigrams(b)
    inter = len(ab & bb)
    if not inter or not ab or not bb:
        return 0.0
    if ab <= bb or bb <= ab:
        return 1.0
    return inter / min(len(ab), len(bb))


class RestaurantHistory:
    """由歷史紀錄建立嘅餐廳索引。

    `restaurants` : {餐廳名: {"count": n, "dishes": [菜名, ...]}}
    `dish_map`   : {正規化菜名: 餐廳名}
    `names`      : [餐廳名, ...]  (按出現次數排,方便文字比對)
    """

    __slots__ = ("restaurants", "dish_map", "names")

    def __init__(self) -> None:
        self.restaurants: Dict[str, Dict[str, Any]] = {}
        self.dish_map: Dict[str, str] = {}
        self.names: List[str] = []

    # ── 建構 ────────────────────────────────────────────────────────────
    def add(self, dish: str, restaurant: str) -> None:
        clean = sanitise_restaurant_name(restaurant, dish_name=dish)
        if not clean:
            return
        rec = self.restaurants.setdefault(
            clean, {"count": 0, "dishes": []})
        rec["count"] += 1
        if dish and len(rec["dishes"]) < 25:
            rec["dishes"].append(dish)
        nd = _norm_dish(dish)
        if nd:
            # 記錄最常見嗰間(後到嘅唔好覆蓋早到嘅，除非舊嗰個冇出現過)
            self.dish_map.setdefault(nd, clean)

    def finalise(self) -> "RestaurantHistory":
        self.names = sorted(
            self.restaurants.keys(),
            key=lambda n: (-self.restaurants[n]["count"], len(n)),
        )
        return self

    # ── 查詢 ────────────────────────────────────────────────────────────
    def lookup(self, user_text: str, dish_name: str = "",
               ai_guess: str = "") -> Tuple[str, str]:
        """Best-effort 餐廳。回傳 (餐廳名 or "", 來源說明)。

        優先次序(由可靠到不可靠):
          1. `text` —— 用家輸入直接提到某個已知餐廳(最可靠)
          2. `history` —— 同一碟菜之前喺邊間食過
          3. `fuzzy` —— 輸入同歷史菜名有字串重疊
          4. `ai` —— AI 猜嘅,但通過咗消毒器
          5. `none` —— 搵唔到,留空
        """
        text_l = (user_text or "").lower()
        nd = _norm_dish(dish_name)

        # 1. 輸入直接提到已知餐廳(揀出現次數最多嗰間,避免「大快活」誤配)
        mentioned = [n for n in self.names if n and n.lower() in text_l]
        if mentioned:
            best = max(mentioned, key=lambda n: self.restaurants[n]["count"])
            return best, "text"

        # 2. 完全相同嘅菜名
        if nd and nd in self.dish_map:
            return self.dish_map[nd], "history"

        # 3. 模糊重疊 —— 只認「真係同一碟菜 / 好似嘅菜」
        #    (v3.3.32 收緊咗:之前 bigram 數 ≥ 2 就當中,
        #     「乳清蛋白粉」會撞到「Dim sum dinner (8 dishes…)」)
        q_tok = _tokens(user_text) | _tokens(dish_name)
        best_name, best_score = "", 0.0
        for rest, rec in self.restaurants.items():
            for hist_dish in rec["dishes"]:
                hd = _norm_dish(hist_dish)
                if not hd:
                    continue
                sim = _dish_similarity(nd, hd) if nd else 0.0
                tok_ov = len(q_tok & _tokens(hist_dish))
                if sim < 0.5:
                    continue
                # 英文 token 路徑:如果淨係靠一個共通 token(例如
                # 「chocolate」)撞出嚟,太容易誤配 → 要求 token ≥ 2
                if tok_ov and tok_ov < 2 and sim < 0.9:
                    continue
                # 頻率加成:食得越多間,越似 Jim 嘅常用店
                score = sim * (1.0 + 0.25 * (rec["count"] - 1))
                # 完全相同嘅菜名加權
                if hd and hd == nd:
                    score *= 2.5
                if score > best_score:
                    best_score, best_name = score, rest
        if best_name and best_score >= 0.5:
            return best_name, "fuzzy"

        # 4. AI 猜(通過消毒先會用)
        clean_ai = sanitise_restaurant_name(ai_guess, dish_name=dish_name)
        if clean_ai:
            return clean_ai, "ai"

        return "", "none"


def build_restaurant_history(
    sheet_rows: Optional[Iterable[Any]] = None,
    scan_entries: Optional[Iterable[Any]] = None,
) -> RestaurantHistory:
    """由 Sheet 行 + scan log 建索引。

    `sheet_rows`  : 每項要有 `.name` 同 `.restaurant`(NutritionRow),
                     或者係 (name, restaurant) tuple,或者係 dict。
    `scan_entries`: dict,有 'name' / 'restaurant_chain' / 'restaurant'。
    """
    hist = RestaurantHistory()

    def _name_of(obj: Any) -> str:
        if isinstance(obj, dict):
            return str(obj.get("name") or obj.get("dish_name") or "")
        nm = getattr(obj, "name", None)
        if nm:
            return str(nm)
        if isinstance(obj, (tuple, list)) and obj:
            return str(obj[0] or "")
        return ""

    def _rest_of(obj: Any) -> str:
        if isinstance(obj, dict):
            return str(obj.get("restaurant_chain") or obj.get("restaurant") or "")
        r = getattr(obj, "restaurant", None)
        if r:
            return str(r)
        rc = getattr(obj, "restaurant_chain", None)
        if rc:
            return str(rc)
        if isinstance(obj, (tuple, list)) and len(obj) > 1:
            return str(obj[1] or "")
        return ""

    for row in (sheet_rows or []):
        hist.add(_name_of(row), _rest_of(row))
    for e in (scan_entries or []):
        hist.add(_name_of(e), _rest_of(e))
    return hist.finalise()


# ──────────────────────────────────────────────────────────────────────────
# 3. 品牌識別(蛋白粉 / 蛋白棒 / RTD 飲品)
# ──────────────────────────────────────────────────────────────────────────

# 品牌別名 → 正規品牌名。用嚟識別「Optimum Nutrition Gold Standard」
# 呢類自然寫法(舊 brand table 只收「on gold standard」縮寫,自然寫法 miss 咗)
BRAND_ALIAS_TO_CANON: Dict[str, str] = {
    # 自然寫法 → 舊 brand table 嘅短寫法
    "optimum nutrition": "on",
    "optimumnutrition": "on",
    "my protein": "myprotein",
    "muscle tech": "muscletech",
    "quest nutrition": "quest",
    "dymatize iso 100": "dymatize iso100",
    "iso 100": "iso100",
}

BRAND_ALIASES: Dict[str, str] = {
    "optimum nutrition": "Optimum Nutrition",
    "optimum nutrition gold standard": "Optimum Nutrition",
    "on gold standard": "Optimum Nutrition",
    "gold standard whey": "Optimum Nutrition",
    "myprotein": "MyProtein",
    "my protein": "MyProtein",
    "myprotein impact whey": "MyProtein",
    "impact whey": "MyProtein",
    "myprotein clear whey": "MyProtein",
    "clear whey": "MyProtein",
    "muscletech": "MuscleTech",
    "muscle tech": "MuscleTech",
    "nitro tech": "MuscleTech",
    "nitrotech": "MuscleTech",
    "dymatize": "Dymatize",
    "dymatize iso100": "Dymatize",
    "iso100": "Dymatize",
    "herbalife": "Herbalife",
    "herbalife formula 1": "Herbalife",
    "formula 1 shake": "Herbalife",
    "quest": "Quest Nutrition",
    "quest nutrition": "Quest Nutrition",
    "quest bar": "Quest Nutrition",
    "quest hero bar": "Quest Nutrition",
}

# 品牌 → 官方網域(搵「brand cover」用)
BRAND_OFFICIAL_DOMAINS: Dict[str, str] = {
    "Optimum Nutrition": "optimumnutrition.com",
    "MyProtein": "myprotein.com",
    "MuscleTech": "muscletech.com",
    "Dymatize": "dymatize.com",
    "Herbalife": "herbalife.com",
    "Quest Nutrition": "questnutrition.com",
}

# 品牌 → 基礎營養(每份,brand table 冇命中 flavour 專屬值時用)
BRAND_BASELINE: Dict[str, Dict[str, Any]] = {
    "Optimum Nutrition": {
        "kcal": 120, "protein": 24, "carbs": 3, "fat": 1,
        "_source": "Optimum Nutrition Gold Standard Whey (1 scoop + water), baseline",
    },
    "MyProtein": {
        "kcal": 103, "protein": 21, "carbs": 1, "fat": 1.5,
        "_source": "MyProtein Impact Whey (1 scoop + water), baseline",
    },
    "MuscleTech": {
        "kcal": 130, "protein": 30, "carbs": 3, "fat": 1.5,
        "_source": "MuscleTech Nitro-Tech (1 scoop + water), baseline",
    },
    "Dymatize": {
        "kcal": 110, "protein": 25, "carbs": 1, "fat": 0.5,
        "_source": "Dymatize ISO100 (1 scoop + water), baseline",
    },
    "Herbalife": {
        "kcal": 220, "protein": 18, "carbs": 24, "fat": 5,
        "_source": "Herbalife Formula 1 (1 scoop + 250ml milk), baseline",
    },
    "Quest Nutrition": {
        "kcal": 200, "protein": 21, "carbs": 22, "fat": 8,
        "fiber": 14, "sugar": 1, "sodium": 280, "sat_fat": 3, "trans_fat": 0,
        "_source": "Quest Bar (60g bar), baseline",
    },
}

# 已知香港連鎖餐廳 —— 只喺「用家輸入直接提到」嗰陣用,
# 純粹由文字推斷(唔會喺「latte」上面亂填星巴克)。
KNOWN_HK_CHAINS: Dict[str, str] = {
    "星巴克": "星巴克", "starbucks": "Starbucks",
    "麥當勞": "麥當勞", "mcdonald": "麥當勞", "mcdonalds": "麥當勞",
    "肯德基": "肯德基", "kfc": "肯德基",
    "大家樂": "大家樂", "大快活": "大快活",
    "沙嗲王": "沙嗲王", "shake shack": "Shake Shack",
    "n+ burger": "N+ Burger HK", "海底撈": "海底撈",
    "瑞幸": "瑞幸咖啡", "luckin": "瑞幸咖啡",
    "noc": "NOC Coffee",
    "華星冰室": "華星冰室", "澳洲牛奶": "澳洲牛奶公司",
    "黃金飯店": "黃金飯店", "小肥羊": "小肥羊",
    "譚仔": "譚仔雲南米線", "三哥": "三哥米線",
    "沾仔記": "沾仔記", "勝香園": "勝香園",
    "肥姐": "肥姐小食店", "佳佳甜品": "佳佳甜品",
    "松記糖水": "松記糖水", "發記腸粉": "發記腸粉粥",
    "鏞記": "鏞記酒家", "陸羽": "陸羽茶室",
    "添好運": "添好運", "點心代表": "點心代表",
    "敘福樓": "敘福樓", "温野菜": "溫野菜", "溫野菜": "溫野菜",
    "omakase": "Omakase",
}


def match_brand(text: str) -> Optional[Dict[str, Any]]:
    """從用家輸入識別蛋白飲品品牌。回傳 None = 唔係品牌飲品。

    回傳 {"brand", "alias", "domain", "baseline", "product_phrase"}
    """
    if not text or not isinstance(text, str):
        return None
    low = text.lower().strip()
    if not low:
        return None
    # 最長別名優先("optimum nutrition gold standard" 贏 "optimum nutrition")
    hits = [(a, b) for a, b in BRAND_ALIASES.items() if a in low]
    if not hits:
        return None
    hits.sort(key=lambda x: -len(x[0]))
    alias, brand = hits[0]
    # 產品短名(用嚟搵 brand cover):去掉品牌字樣剩低嘅描述詞
    product_phrase = low
    for a, _b in BRAND_ALIASES.items():
        product_phrase = product_phrase.replace(a, " ")
    product_phrase = re.sub(r"\s+", " ", product_phrase).strip()
    return {
        "brand": brand,
        "alias": alias,
        "domain": BRAND_OFFICIAL_DOMAINS.get(brand, ""),
        "baseline": BRAND_BASELINE.get(brand, {}),
        "product_phrase": product_phrase,
    }


def detect_mentioned_chain(text: str) -> str:
    """用家輸入有冇直接提到一間已知連鎖餐廳。回傳店名或 ""。"""
    if not text:
        return ""
    low = text.lower()
    best, best_len = "", 0
    for key, name in KNOWN_HK_CHAINS.items():
        k = key.lower()
        if k in low and len(k) > best_len:
            best, best_len = name, len(k)
    return best


# 蛋白飲品 / 蛋白棒 / RTD 嘅字眼 —— 出現即代表呢個係「產品」而唔係「一碟菜」
# (圖片策略會跟住改用產品照 / brand cover,唔係搵一碟菜嘅相)
_PRODUCT_HINTS: Tuple[str, ...] = (
    "protein", "whey", " whey", "casein", "iso100", "isolate",
    "pre-workout", "preworkout", "mass gainer", "bcaa",
    "蛋白粉", "乳清", "蛋白質粉", "蛋白棒", "蛋白飲", "乳清蛋白",
    "能量棒", "代餐", "奶昔", "粉裝", "罐裝",
)


def looks_like_product(text: str) -> bool:
    """呢段輸入係咪講緊一個「產品」(蛋白粉 / 蛋白棒 / RTD)而唔係一碟菜。"""
    if not text:
        return False
    low = text.lower()
    return any(h in low for h in _PRODUCT_HINTS)


__all__ = [
    "sanitise_restaurant_name", "is_plausible_restaurant",
    "RestaurantHistory", "build_restaurant_history",
    "match_brand", "detect_mentioned_chain", "looks_like_product",
    "BRAND_ALIASES", "BRAND_ALIAS_TO_CANON", "BRAND_OFFICIAL_DOMAINS", "BRAND_BASELINE",
    "KNOWN_HK_CHAINS",
]
