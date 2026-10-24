import pytest

from gym_web.cache import NutritionCache, NutritionRow


HEADER = ["date", "time", "meal", "name", "restaurant", "kcal", "p", "c", "f", "notes", "image"]


def sheet_rows(entries):
    return [HEADER, *[list(row) for row in entries]]


def entry(index, date="2025-01-01", time="08:00", name="Oatmeal", date_index=0):
    return [date, time, "breakfast", name, "Cafe", "400", "20", "50", "10", "notes", f"https://img/{index}.jpg"]


def make_cache(rows=None):
    cache = NutritionCache()
    cache.hydrate(lambda: sheet_rows(rows or []))
    return cache


def test_nutrition_row_from_sheet_row_parses_11_cols():
    row = NutritionRow.from_sheet_row(2, entry(2, date="2025-02-03", time="07:45", name="Eggs", date_index=1))
    assert row.row_index == 2
    assert row.entry_id == "row-2"
    assert row.date == "2025-02-03"
    assert row.time == "07:45"
    assert row.kcal == 400
    assert (row.p, row.c, row.f) == (20, 50, 10)
    assert row.dt.isoformat() == "2025-02-03T07:45:00"


def test_nutrition_row_from_sheet_row_short_cells_pads():
    row = NutritionRow.from_sheet_row(3, ["2025-01-02", "lunch", "Soup"])
    assert row.date == "2025-01-02"
    assert row.meal == "Soup"
    assert row.name == ""
    assert row.kcal == 0.0
    assert row.drive_image_url == ""


# --- v3.3.41 (2026-10-24): column-aware serial coercion -------------------
# Regression: the serial→date/time conversion was applied to EVERY column.
# A kcal of 2590 (>1000) became the date string "2026-04-24" → float()
# raised ValueError → kcal silently 0.0 (45 real Sheet rows). A macro of
# exactly 1 fell into the 0<=c<=1 time branch → "24:00" → 0.0 (26 carb +
# 28 protein rows). Only column A (date) and B (time) may be serials.


def test_kcal_above_1000_is_not_treated_as_a_date_serial():
    row = NutritionRow.from_sheet_row(
        210, ["2026-04-24", "12:00", "lunch", "Big meal", "", 2590, 120, 200, 90, "", "", ""]
    )
    assert row.kcal == 2590.0
    assert (row.p, row.c, row.f) == (120.0, 200.0, 90.0)
    assert row.date == "2026-04-24"


def test_macro_value_of_exactly_one_is_not_treated_as_a_time_fraction():
    row = NutritionRow.from_sheet_row(
        652, ["2026-10-18", "18:31", "snack", "Tiny", "", 8, 0, 1, 1, "", "", ""]
    )
    assert row.kcal == 8.0
    assert row.p == 0.0
    assert row.c == 1.0
    assert row.f == 1.0


def test_date_and_time_columns_still_parse_serials():
    # 46088 = 2026-03-07 (1899-12-30 epoch); 0.5 = 12:00
    row = NutritionRow.from_sheet_row(
        2, [46088, 0.5, "lunch", "Serial", "", 800, 30, 40, 20, "", "", ""]
    )
    assert row.date == "2026-03-07"
    assert row.time == "12:00"
    assert row.kcal == 800.0
    assert (row.p, row.c, row.f) == (30.0, 40.0, 20.0)


def test_nutrition_row_to_pwa_dict_aliases():
    row = NutritionRow.from_sheet_row(4, entry(4, name="Rice"))
    result = row.to_pwa_dict()
    assert result["meal_type"] == result["meal"] == "breakfast"
    assert result["calories"] == result["kcal"] == 400
    assert result["restaurant_chain"] == result["restaurant"] == "Cafe"


def test_cache_hydrate_empty_returns_zero():
    assert make_cache().stats()["rows"] == 0


def test_cache_hydrate_loads_10_rows_and_indexes():
    rows = [entry(i, date=f"2025-01-{i:02d}", time=f"{i:02d}:00") for i in range(1, 11)]
    cache = make_cache(rows)
    assert len(cache.by_row) == 10
    assert len(cache.by_id) == 10
    assert set(cache.by_id.values()) == set(range(2, 12))
    assert len(cache.by_date) == 10
    assert cache.get_by_id("row-2").name == "Oatmeal"
    assert cache.get_by_date("2025-01-10")[0].row_index == 11


def test_cache_recent_sort_descending_by_dt():
    rows = [entry(i, date="2025-01-01", time=f"{i:02d}:00") for i in range(1, 4)]
    cache = make_cache(rows)
    assert [row.row_index for row in cache.get_recent()] == [4, 3, 2]


def test_cache_insert_row_indexes_correctly():
    cache = make_cache()
    row = NutritionRow.from_sheet_row(12, entry(12, date="2025-03-01", time="12:00"))
    cache.insert_row(row)
    assert cache.get_by_id("row-12") is row
    assert cache.get_by_date("2025-03-01") == [row]


def test_cache_insert_row_evicts_old_version():
    cache = make_cache([entry(1)])
    old = cache.get_by_id("row-2")
    new = NutritionRow.from_sheet_row(2, entry(2, name="Replaced", date="2025-03-01"))
    cache.insert_row(new)
    assert cache.get_by_id("row-2") is new
    assert cache.get_by_id("row-2") is not old
    assert cache.get_by_date("2025-03-01") == [new]
    assert "2025-01-01" not in cache.by_date


def test_cache_get_by_date_returns_only_that_date():
    cache = make_cache([entry(1, date="2025-01-01"), entry(2, date="2025-01-02")])
    result = cache.get_by_date("2025-01-02")
    assert [row.row_index for row in result] == [3]


def test_cache_hydrate_is_idempotent():
    rows = [entry(1), entry(2, date="2025-01-02")]
    cache = make_cache(rows)
    assert cache.hydrate(lambda: sheet_rows(rows)) == 2
    assert len(cache.by_row) == 2
    assert len(cache.by_id) == 2
    assert sum(len(items) for items in cache.by_date.values()) == 2


def test_cache_refresh_one_updates_existing_row():
    cache = make_cache([entry(1)])
    assert cache.refresh_one(2, lambda _: entry(1, date="2025-02-02", name="Moved")) is True
    assert cache.get_by_id("row-2").date == "2025-02-02"
    assert "2025-01-01" not in cache.by_date
    assert cache.get_by_date("2025-02-02")[0].name == "Moved"
