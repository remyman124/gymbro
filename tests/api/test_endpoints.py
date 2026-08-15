"""API integration tests via the Flask test client.

Every test runs against temp files (see conftest.py autouse fixtures) — no
production state is read or written.
"""

import json

import pytest


# ── health / meta ────────────────────────────────────────────────────────

def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "ok"
    assert "today" in d


def test_version(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    d = r.get_json()
    assert d["version"].startswith("3.")


def test_manifest_is_valid_json(client):
    r = client.get("/manifest.json")
    assert r.status_code == 200
    d = json.loads(r.data)
    assert d["name"] or d["short_name"]


def test_service_worker_served(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert b"cache" in r.data.lower() or b"self" in r.data


def test_homepage_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"x-data" in r.data


# ── workout session ──────────────────────────────────────────────────────

def test_state_returns_session(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    d = r.get_json()
    assert "session" in d and "today" in d
    assert isinstance(d["session"].get("exercises"), list)


def test_log_set_requires_exercise(client):
    r = client.post("/api/log_set", json={"weight_kg": 40})
    assert r.status_code == 400
    assert "error" in r.get_json()


def test_log_set_appends_and_persists(client, isolated_files):
    r = client.post("/api/log_set", json={
        "exercise": "Bench Press", "weight_kg": 60, "reps": 10, "set_n": 1,
    })
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert d["entry"]["exercise"] == "Bench Press"
    assert d["total_sets"] == 1

    # written to the temp log, not production
    log = json.loads(isolated_files["WORKOUT_LOG"].read_text())
    assert any(
        e["exercise"] == "Bench Press"
        for s in log.values() for e in s.get("exercises", [])
    )


def test_log_set_infers_intensity_from_set_number(client):
    r = client.post("/api/log_set", json={"exercise": "Squat", "set_n": 1})
    assert r.get_json()["entry"]["intensity"]


def test_finish_exercise_summarizes_sets(client):
    for n, w in enumerate([40, 50, 60], start=1):
        client.post("/api/log_set", json={
            "exercise": "Deadlift", "weight_kg": w, "reps": 10, "set_n": n,
        })
    r = client.post("/api/finish_exercise", json={"exercise": "Deadlift"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["sets"] == 3
    assert d["total_vol_kg"] == 1500
    assert len(d["set_breakdown"]) == 3


def test_cancel_last_set_pops_entry(client):
    client.post("/api/log_set", json={"exercise": "Row", "weight_kg": 30, "reps": 8})
    before = client.get("/api/state").get_json()["session"]["exercises"]
    client.post("/api/cancel_last_set", json={})
    after = client.get("/api/state").get_json()["session"]["exercises"]
    assert len(after) == len(before) - 1


def test_end_session_returns_pyramid(client):
    client.post("/api/log_set", json={"exercise": "Curl", "weight_kg": 20, "reps": 12, "set_n": 1})
    client.post("/api/log_set", json={"exercise": "Curl", "weight_kg": 25, "reps": 10, "set_n": 2})
    r = client.post("/api/end_session", json={})
    assert r.status_code == 200
    d = r.get_json()
    assert d["total_sets"] == 2
    assert d["total_vol_kg"] == 20 * 12 + 25 * 10
    assert "Curl" in d["pyramid"]
    assert "Curl" in d["exercises"]


# ── history / streak ─────────────────────────────────────────────────────

def test_streak_zero_on_empty_log(client):
    r = client.get("/api/streak")
    assert r.status_code == 200
    d = r.get_json()
    assert d["streak"] == 0
    assert d["last_workout_date"] is None


def test_streak_counts_completed_days(client, isolated_files, gym_web_module):
    from datetime import timedelta
    today = gym_web_module.datetime.strptime(gym_web_module.today_iso(), "%Y-%m-%d").date()
    session = lambda: {
        "completed": True,
        "exercises": [{"exercise": f"E{i}", "weight_kg": 10, "reps": 10, "set": 1} for i in range(3)],
    }
    log = {(today - timedelta(days=i)).isoformat(): session() for i in range(3)}
    isolated_files["WORKOUT_LOG"].write_text(json.dumps(log))
    assert client.get("/api/streak").get_json()["streak"] == 3


def test_history_lists_sessions_desc(client, isolated_files):
    isolated_files["WORKOUT_LOG"].write_text(json.dumps({
        "2026-08-01": {"completed": True, "exercises": [
            {"exercise": "Bench", "weight_kg": 50, "reps": 10, "set": 1}]},
        "2026-08-02": {"completed": False, "exercises": []},
    }))
    d = client.get("/api/history").get_json()
    dates = [h["date"] for h in d["history"]]
    assert dates == sorted(dates, reverse=True)
    first = next(h for h in d["history"] if h["date"] == "2026-08-01")
    assert first["sets"] == 1
    assert first["total_vol_kg"] == 500
    assert first["exercises"] == ["Bench"]


# ── nutrition / scans ────────────────────────────────────────────────────

def test_nutrition_today_empty(client, gym_web_module):
    """v3.3.2: empty cache hydrates with no rows → 0 meals today."""
    cache = gym_web_module._get_nutrition_cache()
    cache.hydrate(lambda: [])  # empty Sheet
    d = client.get("/api/nutrition/today").get_json()
    assert d["meal_count"] == 0
    assert d["meals"] == []


def test_nutrition_today_totals(client, gym_web_module):
    today = gym_web_module.today_iso()
    from gym_web.cache import NutritionRow
    cache = gym_web_module._get_nutrition_cache()
    cache.insert_row(NutritionRow.from_sheet_row(
        1, [today, "12:00", "lunch", "白飯", "", "117",
            "2.4", "25.8", "0", "", ""]))
    cache.insert_row(NutritionRow.from_sheet_row(
        2, [today, "13:00", "lunch", "凍咖啡", "", "180",
            "4", "30", "6", "", ""]))
    d = client.get("/api/nutrition/today").get_json()
    assert d["meal_count"] == 2
    assert d["totals"]["kcal"] == pytest.approx(297, abs=1)


def test_scan_recent_empty(client, gym_web_module):
    """v3.3.2: empty cache hydrates with no rows → 200 with empty scans."""
    cache = gym_web_module._get_nutrition_cache()
    cache.hydrate(lambda: [])  # empty Sheet → cache hydrated with 0 rows
    r = client.get("/api/scan_recent")
    assert r.status_code == 200
    d = r.get_json()
    assert d["scans"] == []


def test_scan_recent_filters_failed_scans(client, gym_web_module):
    """v3.3.2: scan data lives in the in-memory NutritionCache, not JSON."""
    from gym_web.cache import NutritionRow
    cache = gym_web_module._get_nutrition_cache()
    cells = ["2026-08-09", "12:00", "lunch", "白飯", "", "117",
            "2.4", "25.8", "0", "", ""]
    cache.insert_row(NutritionRow.from_sheet_row(1, cells))
    r = client.get("/api/scan_recent?limit=10")
    assert r.status_code == 200
    d = r.get_json()
    scans = d if isinstance(d, list) else d.get("scans", d.get("recent", []))
    names = [s.get("name") for s in scans]
    assert "白飯" in names
    assert not any(n and n.startswith("食物 #") for n in names)


def test_scan_recent_respects_limit(client, gym_web_module):
    """v3.3.2: seed NutritionCache directly via NutritionRow.from_sheet_row."""
    from gym_web.cache import NutritionRow
    cache = gym_web_module._get_nutrition_cache()
    cache.insert_row(NutritionRow.from_sheet_row(
        1, ["2026-08-09", "12:00", "lunch", "白飯", "", "117",
            "2.4", "25.8", "0", "", ""]))
    cache.insert_row(NutritionRow.from_sheet_row(
        2, ["2026-08-09", "13:00", "lunch", "凍咖啡", "", "180",
            "4", "30", "6", "", ""]))
    d = client.get("/api/scan_recent?limit=1").get_json()
    scans = d if isinstance(d, list) else d.get("scans", d.get("recent", []))
    assert len(scans) <= 1


# ── scan preview — needs_user_input (v3.2.7.19) ──────────────────────────

def test_scan_preview_surfaces_extracted_entry_when_narration(monkeypatch, client, gym_web_module):
    """v3.2.7.19: when narration is detected in the extracted name, the
    preview must STILL return the best extracted entry (so the user can
    see/edit it) — only auto-commit is blocked. Jim OOB 2026-08-10
    'one picture without image and one image without image' — previously
    the preview was empty in this case.

    We monkeypatch the AI vision + enrichment + dish-name + narration
    classifier to inject a synthetic narration case, then assert the
    preview contains the extracted entry with needs_user_input=True.
    """
    import io
    # Force the AI classifier to flag the name as narration
    monkeypatch.setattr(gym_web_module, "_ai_check_narration",
                        lambda name: True)
    # Force the dish-name extractor to return a narration-ish name
    monkeypatch.setattr(gym_web_module, "_extract_dish_name",
                        lambda v, p, fallback="": "這張相顯示一支蘇打水樽")
    # Force pplx + apiyi to return consistent data
    monkeypatch.setattr(gym_web_module, "_pplx_enrich",
                        lambda v: "蘇打水 0 kcal, 0g protein, 0g carbs, 0g fat")
    monkeypatch.setattr(gym_web_module, "_apiyi_nutrition_enrich",
                        lambda v: '{"calories":0,"protein":0,"carbs":0,"fat":0}')
    monkeypatch.setattr(gym_web_module, "_apiyi_nutrition_enrich_multi",
                        lambda v: '{"dishes":[{"name":"這張相顯示一支蘇打水樽",'
                                  '"calories":0,"protein":0,"carbs":0,"fat":0}]}')
    monkeypatch.setattr(gym_web_module, "_minimax_vision",
                        lambda b64, prompt: "呢張相顯示一支蘇打水樽")
    monkeypatch.setattr(gym_web_module, "_apiyi_vision_analyze",
                        lambda b64, prompt: "一支蘇打水樽 透明")
    monkeypatch.setattr(gym_web_module, "_merge_nutrition_estimates",
                        lambda lst: {"calories": {"value": 0},
                                     "protein": {"value": 0},
                                     "carbs": {"value": 0},
                                     "fat": {"value": 0}})
    monkeypatch.setattr(gym_web_module, "_detect_shared_meal", lambda s: False)

    # Multipart upload (1x1 jpg)
    img_bytes = (
        b"/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
        b"Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIR"
        b"whMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMj"
        b"L/wAARCAABAAEDASIAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL"
        b"/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0f"
        b"AkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3"
        b"R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJyt"
        b"LT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD3+iiigD//2Q=="
    )

    r = client.post(
        "/api/scan_preview",
        data={"image": (io.BytesIO(img_bytes), "test.jpg")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200, r.data
    d = r.get_json()
    assert d["ok"] is True
    assert d["auto_committed"] is True
    # needs_user_input flag was removed in v3.2.7.23 — the gate no
    # longer exists, so the field shouldn't appear in the response.
    assert "needs_user_input" not in d
    # Entry was actually committed (committed list non-empty). The
    # narration-stripping in _extract_dish_name (test mocks it
    # differently here, but production strips aggressively via
    # skip_prefixes — lines 3042-3055 in gym_web.py).
    assert len(d["committed"]) >= 1


def test_scan_preview_extracts_empty_when_vision_fully_fails(monkeypatch, client, gym_web_module):
    """When vision completely fails (no extracted entry), the preview
    should still return a valid shape with empty suggested_entry fields
    and needs_user_input=True."""
    import io
    monkeypatch.setattr(gym_web_module, "_ai_check_narration", lambda name: False)
    monkeypatch.setattr(gym_web_module, "_extract_dish_name", lambda v, p, fallback="": "")
    monkeypatch.setattr(gym_web_module, "_pplx_enrich", lambda v: "")
    monkeypatch.setattr(gym_web_module, "_apiyi_nutrition_enrich", lambda v: "")
    monkeypatch.setattr(gym_web_module, "_apiyi_nutrition_enrich_multi", lambda v: "")
    monkeypatch.setattr(gym_web_module, "_minimax_vision", lambda b64, prompt: "（MiniMax vision 失敗）")
    monkeypatch.setattr(gym_web_module, "_apiyi_vision_analyze", lambda b64, prompt: "")
    monkeypatch.setattr(gym_web_module, "_merge_nutrition_estimates", lambda lst: {})
    monkeypatch.setattr(gym_web_module, "_detect_shared_meal", lambda s: False)

    img_bytes = b"/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAA=="
    r = client.post(
        "/api/scan_preview",
        data={"image": (io.BytesIO(img_bytes), "test.jpg")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert d["auto_committed"] is True
    assert "needs_user_input" not in d
    # Entry was committed even though vision failed — the extractor
    # returns its fallback ("食物" per Pattern 5 in _extract_dish_name).
    assert len(d["committed"]) >= 1


# ── health overlays (Whoop / Withings) ───────────────────────────────────

def test_health_overlay_shape(client):
    r = client.get("/api/health_overlay")
    assert r.status_code == 200
    d = r.get_json()
    for k in ("recovery", "weight_kg", "fat_pct", "steps_today"):
        assert k in d


def test_health_overlay_reads_caches(client, populated_whoop_cache, populated_withings_cache):
    d = client.get("/api/health_overlay").get_json()
    assert d["recovery"] == 72
    assert d["weight_kg"] == pytest.approx(75.2)
    assert d["fat_pct"] == pytest.approx(18.5)


def test_withings_steps_today_shape(client, populated_withings_cache):
    r = client.get("/api/withings_steps_today")
    assert r.status_code == 200
    d = r.get_json()
    assert "steps" in d and "syncing" in d


def test_whoop_calendar_returns_days(client, populated_whoop_cache):
    r = client.get("/api/whoop_activities_calendar?days=7")
    assert r.status_code == 200
    d = r.get_json()
    assert "days" in d
    assert len(d["days"]) == 7


def test_whoop_calendar_clamps_days(client, populated_whoop_cache):
    assert len(client.get("/api/whoop_activities_calendar?days=1").get_json()["days"]) == 7
    assert len(client.get("/api/whoop_activities_calendar?days=999").get_json()["days"]) == 90
    assert len(client.get("/api/whoop_activities_calendar?days=abc").get_json()["days"]) == 42


# ── misc ─────────────────────────────────────────────────────────────────

def test_context_get_and_post(client):
    r = client.post("/api/context", json={"note": "test note"})
    assert r.status_code in (200, 400)
    assert client.get("/api/context").status_code == 200


def test_unknown_route_404(client):
    assert client.get("/api/does_not_exist").status_code == 404
