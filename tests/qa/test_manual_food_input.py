"""
v3.3.32 — 文字輸入(manual food input)流程嘅回歸測試

背景 (Jim OOB 2026-10-21):
  「I want you to fix the manual food input workflow. It has been very buggy.
    Can you fill in the restaurant (in best effort basis) based on what I inputted.
    Moreover, the food image capture should be related to the food. Secondary
    option is the restaurant. Sometimes, I would input Protein drinks and brand.
    You research it online and put the brand cover as food image」

實測 production Sheet (`Nutrition` tab, 658 行) 嘅餐廳欄:
    難以辨認餐廳 45 · 描述可靠識別餐廳 13 · 睇唔到餐廳 12 · 冇見到餐廳 12
    一間香港連鎖餐廳 10 · 相片可靠識別餐廳 5 · 所以餐廳 2
    「如果你係做餐廳」1 ·「碟炸蔬菜係喺餐廳」1 ·「楚具體係邊間餐廳」1
    「景係一個露天餐廳」1 ·「頭都係一般茶餐廳」1
→ **90% 係 AI 講嘅半句說話**,零資訊量。

呢組測試係 **unit level** — 唔打 network、唔打 Google API、唔掂
Jim 個 Sheet。純粹驗消毒器 / 歷史索引 / 品牌識別 / 圖片策略嘅邏輯。
"""

import importlib.util
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO / "test_fixtures"


@pytest.fixture(scope="module")
def gw():
    """Load gym_web.py by path (NOT as the `gym_web` package — that
    resolves to the gym_web/ directory, not gym_web.py)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    os.environ["GYMBRO_WRITE_MODE"] = "dryrun"
    spec = importlib.util.spec_from_file_location(
        "gym_web_manual", REPO / "gym_web.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fc():
    import sys
    sys.path.insert(0, str(REPO))
    from gym_web import food_context
    return food_context


@pytest.fixture(scope="module")
def fi():
    import sys
    sys.path.insert(0, str(REPO))
    from gym_web import food_image
    return food_image


# ──────────────────────────────────────────────────────────────────────────
# 1. 餐廳名消毒器 —— production 實測垃圾值全部要俾剔走
# ──────────────────────────────────────────────────────────────────────────

# 呢個 list 係直接由 production Sheet 餐廳欄抽出嘅真實垃圾值
PRODUCTION_JUNK = [
    "難以辨認餐廳", "描述可靠識別餐廳", "睇唔到餐廳", "冇見到餐廳",
    "一間香港連鎖餐廳", "相片可靠識別餐廳", "應該係餐廳", "唔知邊間餐廳",
    "相片可靠辨認餐廳", "香港本地連鎖餐廳", "任何香港連鎖餐廳",
    "當作一般茶餐廳", "邊間香港連鎖餐廳", "所以餐廳", "難以辨認邊間餐廳",
    "餐廳/連鎖", "TBD (receipt OCR 中餐廳 brand 未顯示)", "如果你係做餐廳",
    "外賣餐廳", "一般家用或小店", "碟炸蔬菜係喺餐廳", "係喺一間中式餐廳",
    "任何主流連鎖餐廳", "自助火鍋店", "港任何一間茶餐廳", "相中未見到餐廳",
    "頭都係一般茶餐廳", "屬於香港連鎖餐廳", "單一香港連鎖餐廳",
    "楚具體係邊間餐廳", "景係一個露天餐廳", "港邊一間連鎖餐廳",
    "普通茶餐廳", "圖中未見到餐廳", "可靠識別餐廳", "黑咖啡（辦公室/外帶）",
    "韓式燒肉", "Hainanese chicken rice shop", "普通香港咖啡店",
    "可靠辨認餐廳", "外賣餐廳", "茶餐廳", "一般茶餐廳",
    # 2026-10-21 清理 production 時先發現嘅散文式殘餘
    "似典型港式茶餐廳", "餐睇落好似茶餐廳", "以下為香港茶餐廳",
    "境似日本居酒屋", "居酒屋", "典型酒樓", "其他配料或者餐廳",
    "至特定品牌或餐廳", "下按一個標準餐廳", "按香港茶樓",
    "足夠資料判定餐廳", "第一道菜所屬餐廳", "認其他食材同餐廳",
    "採用香港中式餐廳", "但冇餐廳", "如果睇到餐廳", "港式快餐或茶餐廳",
    "香港茶餐廳",
]

REAL_RESTAURANTS = [
    "黃金飯店", "小肥羊", "溫野菜 (Noubeni Shabu Shabu, Harbour City)",
    "luckin coffee", "沙嗲王 (Satay King)", "Coke Zero",
    "Triple O's (by White Spot)", "N+ Burger HK", "Shake Shack",
    "Nestea", "星巴克", "大家樂", "沾仔記", "添好運", "明記燒臘",
    "龍景軒", "海底撈", "老馮茶居 (旺角)", "Pizza Hut", "新羅寶韓國餐廳",
    # production 清理後剩低嘅真店名(實測 2026-10-21)
    "N+ Burger HK", "NOC", "Coke Zero", "Goobne Chicken (TST)",
    "靠得住", "華星冰室", "麵尊", "Tangram", "Quest Nutrition",
    "luckin coffee", "小肥羊", "溫野菜 (Noubeni Shabu Shabu, Harbour City)",
    "沙嗲王 (Satay King)", "Triple O's (by White Spot)", "Shake Shack",
]


@pytest.mark.parametrize("junk", PRODUCTION_JUNK)
def test_production_junk_is_rejected(fc, junk):
    """production 觀察到嘅每一個垃圾餐廳值都一定要變空字串。"""
    assert fc.sanitise_restaurant_name(junk) == "", \
        f"垃圾餐廳值未被攔截:{junk!r}"


@pytest.mark.parametrize("good", REAL_RESTAURANTS)
def test_real_restaurants_survive(fc, good):
    """真實店名唔可以俾消毒器殺咗。"""
    assert fc.sanitise_restaurant_name(good) == good, \
        f"真實店名被誤殺:{good!r}"


def test_sanitiser_rejects_empty_and_non_string(fc):
    for bad in (None, "", " ", "  ", 123, [], {}):
        assert fc.sanitise_restaurant_name(bad) == ""


def test_sanitiser_rejects_dish_name_copy(fc):
    """AI 懶得填,直接抄菜名 → 唔算餐廳。"""
    assert fc.sanitise_restaurant_name("叉燒飯", dish_name="叉燒飯") == ""


# ──────────────────────────────────────────────────────────────────────────
# 2. 品牌識別 —— 自然寫法一定要認到
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,brand", [
    ("Optimum Nutrition Gold Standard", "Optimum Nutrition"),
    ("ON Gold Standard whey vanilla", "Optimum Nutrition"),
    ("myprotein impact whey", "MyProtein"),
    ("MyProtein Impact Whey Chocolate", "MyProtein"),
    ("MuscleTech Nitro Tech", "MuscleTech"),
    ("Dymatize ISO100", "Dymatize"),
    ("quest bar", "Quest Nutrition"),
    ("Herbalife Formula 1", "Herbalife"),
])
def test_brand_alias_detection(fc, text, brand):
    b = fc.match_brand(text)
    assert b is not None, f"品牌未被識別:{text!r}"
    assert b["brand"] == brand


def test_non_brand_text_is_not_brand(fc):
    for t in ("latte coffee", "叉燒飯", "白飯", "牛肉麵"):
        assert fc.match_brand(t) is None, f"唔應該當成品牌:{t!r}"


def test_brand_table_hits_natural_phrase(gw):
    """舊 brand table 淨係收「on gold standard」縮寫,自然寫法會 miss。

    v3.3.32 加咗 alias 正規化 —— 呢個係實測 2026-10-21 嘅 bug。
    """
    hit = gw._lookup_brand_macros("Optimum Nutrition Gold Standard")
    assert hit, "自然寫法應該命中 brand table"
    assert hit["restaurant_chain"] == "Optimum Nutrition"
    assert hit["kcal"] > 0 and hit["protein"] > 0


def test_brand_table_baseline_fallback(gw):
    """淨係講品牌冇 flavour → 用品牌 baseline。"""
    hit = gw._lookup_brand_macros("myprotein")
    assert hit, "應該有 baseline fallback"
    assert hit["restaurant_chain"] == "MyProtein"


def test_looks_like_product(fc):
    for t in ("whey protein chocolate", "蛋白粉", "蛋白棒", "casein shake"):
        assert fc.looks_like_product(t), f"應該當係產品:{t!r}"
    for t in ("叉燒飯", "latte", "咖啡"):
        assert not fc.looks_like_product(t), f"唔應該當係產品:{t!r}"


# ──────────────────────────────────────────────────────────────────────────
# 3. 餐廳歷史索引
# ──────────────────────────────────────────────────────────────────────────

def test_history_index_filters_junk(fc):
    """索引只收真店名,AI 廢話唔入索引。"""
    hist = fc.build_restaurant_history(
        sheet_rows=[
            {"name": "叉燒飯", "restaurant_chain": "黃金飯店"},
            {"name": "白飯", "restaurant_chain": "難以辨認餐廳"},
            {"name": "奶茶", "restaurant_chain": "描述可靠識別餐廳"},
        ],
        scan_entries=[],
    )
    assert "黃金飯店" in hist.restaurants
    assert "難以辨認餐廳" not in hist.restaurants
    assert "描述可靠識別餐廳" not in hist.restaurants
    assert len(hist.restaurants) == 1


def test_history_lookup_exact_dish(fc):
    hist = fc.build_restaurant_history(
        sheet_rows=[{"name": "叉燒飯", "restaurant_chain": "黃金飯店"}],
        scan_entries=[],
    )
    name, src = hist.lookup("叉燒飯", "叉燒飯", "")
    assert name == "黃金飯店"
    assert src == "history"


def test_history_lookup_substring_dish(fc):
    """「叉燒」⊂「叉燒飯」—— 應該配返同一間。"""
    hist = fc.build_restaurant_history(
        sheet_rows=[{"name": "叉燒飯海鮮", "restaurant_chain": "沙嗲王"}],
        scan_entries=[],
    )
    name, src = hist.lookup("叉燒", "叉燒", "")
    assert name == "沙嗲王", f"substring 匹配失敗,得到 {name!r}"


def test_history_lookup_rejects_unrelated_dish(fc):
    """v3.3.32 修嘅 bug:「乳清蛋白粉」唔可以撞到「Dim sum dinner…」。

    之前 bigram 重疊 ≥ 2 就當中,實測 score 2.0 剛好過門檻。
    """
    hist = fc.build_restaurant_history(
        sheet_rows=[{
            "name": "Dim sum dinner (8 dishes, Jim 60%)",
            "restaurant_chain": "HK dim sum 外賣",
        }],
        scan_entries=[],
    )
    name, src = hist.lookup("whey protein chocolate", "朱古力乳清蛋白粉", "")
    assert name == "", f"唔相關嘅菜唔應該配到餐廳,得到 {name!r}"


def test_history_lookup_rejects_single_token_overlap(fc):
    """「chocolate cake」唔應該撞「chocolate peanut butter bar」。"""
    hist = fc.build_restaurant_history(
        sheet_rows=[{
            "name": "chocolate peanut butter bar",
            "restaurant_chain": "Quest Nutrition",
        }],
        scan_entries=[],
    )
    name, src = hist.lookup("chocolate cake", "朱古力蛋糕", "")
    assert name == "", f"單 token 重疊唔應該當中,得到 {name!r}"


def test_history_lookup_text_mention_wins(fc, gw):
    """用家輸入直接提到某間已知餐廳 → 最高優先(喺 _resolve_restaurant)。"""
    name, src = gw._resolve_restaurant("星巴克latte", "拿鐵咖啡", "")
    assert name == "星巴克", f"應該認出星巴克,得到 {name!r}"
    assert src == "text"


def test_resolve_restaurant_never_returns_junk(gw):
    """_resolve_restaurant 出嚟嘅名一定要通過消毒器。"""
    for text, dish, ai in [
        ("whey protein chocolate", "朱古力乳清蛋白粉", "難以辨認餐廳"),
        ("latte coffee", "鮮奶咖啡", "普通香港咖啡店"),
        ("叉燒飯", "叉燒飯", "一間香港連鎖餐廳"),
    ]:
        name, src = gw._resolve_restaurant(text, dish, ai)
        assert gw._sanitise_restaurant_name(name, dish_name=dish) == name, \
            f"回傳咗未消毒嘅餐廳:{name!r} (text={text!r})"


def test_resolve_restaurant_product_without_brand_is_empty(gw):
    """蛋白產品但冇講品牌 → 唔好亂填另一個品牌。"""
    name, src = gw._resolve_restaurant("whey protein chocolate", "朱古力乳清蛋白粉", "")
    assert name == "", f"產品但冇品牌應該留空,得到 {name!r}"


def test_history_lookup_ai_guess_sanitised(fc):
    """AI 猜嘅垃圾唔會入;乾淨嘅先會用。"""
    hist = fc.build_restaurant_history(sheet_rows=[], scan_entries=[])
    name, src = hist.lookup("某啲嘢", "某啲嘢", "難以辨認餐廳")
    assert name == ""
    name, src = hist.lookup("某啲嘢", "某啲嘢", "添好運")
    assert name == "添好運"
    assert src == "ai"


# ──────────────────────────────────────────────────────────────────────────
# 4. 圖片策略
# ──────────────────────────────────────────────────────────────────────────

def test_plan_brand_cover_first(fi):
    brand = {"brand": "Optimum Nutrition", "alias": "optimum nutrition",
             "domain": "optimumnutrition.com", "product_phrase": "gold standard",
             "baseline": {}}
    steps = fi.plan_image_search("蛋白粉", "Optimum Nutrition", "ON Gold Standard", brand)
    assert steps[0]["kind"] == "brand_cover"
    assert "Optimum Nutrition" in steps[0]["query"]
    assert "optimumnutrition.com" in steps[0]["prefer_domains"]


def test_plan_food_before_restaurant(fi):
    steps = fi.plan_image_search("叉燒飯", "黃金飯店", "叉燒飯", None)
    kinds = [s["kind"] for s in steps]
    assert kinds[0] == "food"
    assert kinds[-1] == "restaurant", "餐廳相一定要排最後(次選)"
    assert kinds.count("food") >= 1


def test_plan_no_restaurant_step_without_restaurant(fi):
    steps = fi.plan_image_search("叉燒飯", "", "叉燒飯", None)
    assert all(s["kind"] != "restaurant" for s in steps)


def test_stock_domains_are_blocked(fi):
    assert fi._is_stock("https://www.istockphoto.com/photo-123")
    assert fi._is_stock("https://media.shutterstock.com/x.jpg")
    assert not fi._is_stock("https://cookpad.com/recipe/1")
    assert fi._is_retail("https://www.optimumnutrition.com/en-gb/p/xxx")
    assert not fi._is_retail("https://cookpad.com/recipe/1")


def test_og_image_extraction(fi):
    html = ('<html><head><meta property="og:image" '
            'content="https://cdn.example.com/cover.jpg" /></head></html>')
    assert fi.extract_og_image(html) == "https://cdn.example.com/cover.jpg"
    html2 = ('<meta name="twitter:image" content="//cdn.example.com/t.jpg">')
    assert fi.extract_og_image(html2) == "https://cdn.example.com/t.jpg"
    assert fi.extract_og_image("<html>nothing</html>") == ""


def test_pick_food_image_rejects_unrelated_candidate(fi, tmp_path):
    """驗證唔過嘅候選要被剔走,試下一個。"""
    calls = {"n": 0}

    def search_fn(q, count=5):
        return [
            {"url": "https://a.com/1.jpg", "title": "wrong", "source": "a.com"},
            {"url": "https://b.com/2.jpg", "title": "right", "source": "b.com"},
        ]

    def download_fn(url, slug):
        calls["n"] += 1
        p = tmp_path / f"{calls['n']}.jpg"
        p.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
        return p

    def verify_fn(path, expected, kind):
        return "b.com" in calls.get("last_url", "")

    # 用 title 判斷邊張啱
    def verify_fn2(path, expected, kind):
        return "2" in path.name

    res = fi.pick_food_image(
        dish_name="叉燒飯", restaurant="", user_text="叉燒飯", brand=None,
        search_fn=search_fn, download_fn=download_fn, verify_fn=verify_fn2,
    )
    assert res["image_path"], "應該搵到相"
    assert res["verified"] is True
    assert calls["n"] == 2, f"應該試咗 2 個候選,實際 {calls['n']}"


def test_pick_food_image_skips_stock_results(fi, tmp_path):
    def search_fn(q, count=5):
        return [{"url": "https://www.istockphoto.com/x.jpg",
                 "title": "stock", "source": "istockphoto.com"}]

    def download_fn(url, slug):
        raise AssertionError("stock 圖唔應該被 download")

    res = fi.pick_food_image(
        dish_name="蛋白粉", restaurant="", user_text="蛋白粉", brand=None,
        search_fn=search_fn, download_fn=download_fn, verify_fn=None,
    )
    assert res["image_path"] == ""
    assert res["verified"] is False


# ──────────────────────────────────────────────────────────────────────────
# 5. 伺服器端防線 + 前端
# ──────────────────────────────────────────────────────────────────────────

def test_commit_guards_restaurant(gw):
    """commit 路徑一定要有餐廳消毒 —— 呢個係寫入 golden copy 嘅最後閘。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    assert "_sanitise_restaurant_name(entry_rest" in src, \
        "scan_commit 缺少餐廳消毒"
    assert "[scan_commit] restaurant dropped" in src


def test_preview_text_uses_new_pipeline(gw):
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    assert "_pick_food_image(" in src, "preview_text 應該用新圖片策略"
    assert "_resolve_restaurant(" in src, "preview_text 應該用新餐廳解析"
    assert "restaurant_source" in src, "preview 應該回傳餐廳來源"


def test_frontend_single_success_flash():
    """舊版成功路徑連續 flash 三次,最後一句會蓋走「搵到食物相」嘅訊息。

    v3.3.32:成功路徑只可以 flash 一次,而且嗰一次要同時講埋
    熱量 + 相片狀態 + 餐廳來源。
    """
    tpl = (REPO / "templates" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"async submitScanText\(\)\s*\{(.*?)\n    \},", tpl, re.S)
    assert m, "搵唔到 submitScanText"
    body = m.group(1)
    # 成功路徑 = 由 `this.scanTextPreview = data.preview;` 到 `} catch` 之前
    start = body.find("this.scanTextPreview = data.preview;")
    assert start >= 0
    end = body.find("} catch(e) {")
    success = body[start:end if end > 0 else len(body)]
    flashes = re.findall(r"this\.flash\(", success)
    assert len(flashes) == 1, \
        f"成功路徑應該只 flash 一次,實際 {len(flashes)} 次"
    assert "restaurant_source" in body, "前端應該顯示餐廳來源"
    assert "image_search" in body, "前端應該顯示相片狀態"
    # 舊嘅三段式訊息唔可以再存在
    assert "✨ 已估算 + 從 ${imgInfo.source}" not in tpl, "舊嘅三段式 flash 仲喺度"


def test_frontend_shows_restaurant_source_badge():
    tpl = (REPO / "templates" / "index.html").read_text(encoding="utf-8")
    assert "🏪 餐廳來源" in tpl
    assert "驗證相符" in tpl or "未驗證" in tpl


# ──────────────────────────────────────────────────────────────────────────
# 6. 統一寫入閘(_append_to_sheet_nutrition 嘅唯一防線)
# ──────────────────────────────────────────────────────────────────────────
#
# 實測 2026-10-21 13:52(HKT):Jim 自己 13:16 影嘅午餐盒相,AI 認唔出,
# 相片路徑嘅 scan job 自動 commit 直接寫咗入 production Sheet:
#     餐名 =「黑金圓形餐盒內食物（無法識別）」
#     餐廳 =「描述可靠識別餐廳」
#     營養 = 0 / 0 / 0 / 0
# 相片路徑唔經 /api/scan_commit,所以 commit endpoint 嘅消毒器覆蓋唔到。
# → 防線必須放喺 _append_to_sheet_nutrition(唯一 production writer)。


def test_guard_blocks_unrecognisable_name(gw):
    entry = {
        "name": "黑金圓形餐盒內食物（無法識別）",
        "restaurant_chain": "描述可靠識別餐廳",
        "calories": 0, "protein": 0, "carbs": 0, "fat": 0,
    }
    res = gw._sheet_write_guard(entry)
    assert not res["ok"], "認唔出嘅名唔應該准入表"
    assert res["reason"] == "unusable_name"


@pytest.mark.parametrize("bad_name", [
    "未識別菜式", "未識別", "食物", "餐點", "黑金圓形餐盒內食物（無法識別）",
    "相入食物（無法辨認）", "睇唔到係咩", "唔知係咩",
])
def test_guard_blocks_all_generic_names(gw, bad_name):
    entry = {"name": bad_name, "restaurant_chain": "", "calories": 300,
             "protein": 20, "carbs": 30, "fat": 8}
    res = gw._sheet_write_guard(entry)
    assert not res["ok"], f"泛用名唔應該准入表:{bad_name!r}"


def test_guard_sanitises_restaurant(gw):
    entry = {"name": "星巴克拿鐵", "restaurant_chain": "描述可靠識別餐廳",
             "calories": 240, "protein": 10, "carbs": 24, "fat": 9}
    res = gw._sheet_write_guard(entry)
    assert res["ok"]
    assert entry["restaurant_chain"] == "", "AI 廢話餐廳應該變空字串"


def test_guard_allows_zero_calorie_water(gw):
    entry = {"name": "白開水", "restaurant_chain": "", "calories": 0,
             "protein": 0, "carbs": 0, "fat": 0}
    res = gw._sheet_write_guard(entry)
    assert res["ok"], "白開水 0 kcal 係正常,唔應該被擋"


def test_guard_blocks_zero_nutrition_unknown_food(gw):
    entry = {"name": "神秘食物", "restaurant_chain": "", "calories": 0,
             "protein": 0, "carbs": 0, "fat": 0}
    res = gw._sheet_write_guard(entry)
    assert not res["ok"], "完全冇營養資料嘅未知食物唔應該入表"
    assert res["reason"] == "zero_nutrition"


def test_guard_allows_normal_food(gw):
    entry = {"name": "叉燒飯", "restaurant_chain": "黃金飯店", "calories": 680,
             "protein": 30, "carbs": 90, "fat": 18}
    res = gw._sheet_write_guard(entry)
    assert res["ok"]
    assert entry["restaurant_chain"] == "黃金飯店"


def test_append_to_sheet_calls_the_guard(gw):
    """唔可以有人喺 write function 上面加嘢就漏咗防線。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    fn_start = src.index("def _append_to_sheet_nutrition(")
    fn_end = src.index("\ndef ", fn_start + 10)
    body = src[fn_start:fn_end]
    assert "_sheet_write_guard(entry)" in body, \
        "_append_to_sheet_nutrition 一定要行統一閘"


def test_every_food_writer_goes_through_append(gw):
    """所有食物寫入路徑都應該經過 _append_to_sheet_nutrition。"""
    src = (REPO / "gym_web.py").read_text(encoding="utf-8")
    # 相片自動 commit
    assert "sheet_result = _append_to_sheet_nutrition(entry)" in src
    # 文字輸入 / MCP / 修正 commit
    assert src.count("sheet_result = _append_to_sheet_nutrition(entry)") >= 2
