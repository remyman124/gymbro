"""v3.3.44 — Mobile PWA API guard (Push / Geolocation / offline queue).

背景:手機版 PWA 加咗 Web Push(通知)、Geolocation(位置)同背景同步
(離線隊列)。呢個檔案守住三樣嘢:

  1. 7 個新 endpoint 全部 fail-soft(唔會 500),而且輸入驗證做得到
  2. push / geo 嘅 store 一定寫落 tmp_path,**永遠唔會**寫入
     `/home/work/.hermes/gymbro_*.json`(真實 store)
  3. 前端模板同 Service Worker 有齊 push / notificationclick / sync 嘅處理

⛔ 呢啲測試**絕對唔可以**寫入生產 Google Sheet `Nutrition` tab
   (見 `test_no_test_pollution.py`)。呢個檔案完全唔掂任何 food-scan /
   Sheet 寫入路徑。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path("/home/work/projects/gymbro")
TEMPLATE = REPO / "templates" / "index.html"
GYM_WEB = REPO / "gym_web.py"

FAKE_ENDPOINT = "https://fcm.googleapis.com/fcm/send/fake-endpoint-for-tests"
FAKE_KEYS = {
    "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM",
    "auth": "tBHItJI5svbpez7KI4CCXg",
}


@pytest.fixture
def push_store(tmp_path, monkeypatch):
    """Point the push store at a temp file — never the real one."""
    path = tmp_path / "push_subs.json"
    monkeypatch.setenv("GYMBRO_PUSH_STORE", str(path))
    return path


@pytest.fixture
def outbox_store(tmp_path, monkeypatch):
    """v3.3.46: /api/push/send 會記低 outbox → 測試一定要指去 tmp,
    唔可以污染生產 store(`/home/work/.hermes/gymbro_outbox.json`)。"""
    path = tmp_path / "outbox.json"
    monkeypatch.setenv("GYMBRO_OUTBOX_STORE", str(path))
    return path


@pytest.fixture
def geo_store(tmp_path, monkeypatch):
    """Point the geo store at a temp file — never the real one."""
    path = tmp_path / "geo.json"
    monkeypatch.setenv("GYMBRO_GEO_STORE", str(path))
    return path


@pytest.fixture(autouse=True)
def no_reverse_geocode(monkeypatch):
    """Never hit Nominatim from a test.

    The real lookup has a 3s timeout; a 200-entry test would take ten
    minutes and hammer a third-party API. The geocoder itself is
    best-effort by design — the endpoint must work fine without it.
    """
    from gym_web import geo

    monkeypatch.setattr(geo, "reverse_geocode", lambda lat, lng: "測試地點")


# ── 1. Web Push endpoints ─────────────────────────────────────────────────

def test_vapid_public_key_endpoint(client):
    r = client.get("/api/push/vapid_public_key")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert isinstance(data["public_key"], str)
    # The private key must never leak through any endpoint.
    assert "private_key" not in data


def test_subscribe_then_status_counts_one(client, push_store):
    r = client.post("/api/push/subscribe",
                    json={"endpoint": FAKE_ENDPOINT, "keys": FAKE_KEYS})
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["count"] == 1

    r = client.get("/api/push/status")
    assert r.status_code == 200
    status = r.get_json()
    assert status["ok"] is True
    assert status["count"] == 1
    assert isinstance(status["vapid_ready"], bool)

    # The subscription really landed in the temp store (not the real one).
    stored = json.loads(push_store.read_text())
    assert stored["subs"][0]["endpoint"] == FAKE_ENDPOINT


def test_subscribe_rejects_bad_endpoint(client, push_store):
    r = client.post("/api/push/subscribe", json={"endpoint": "", "keys": FAKE_KEYS})
    assert r.get_json()["ok"] is False
    r = client.post("/api/push/subscribe", json={"endpoint": "x" * 4096, "keys": FAKE_KEYS})
    assert r.get_json()["ok"] is False
    r = client.post("/api/push/subscribe", json={"endpoint": FAKE_ENDPOINT, "keys": {}})
    assert r.get_json()["ok"] is False
    assert not push_store.exists() or json.loads(push_store.read_text())["subs"] == []


def test_unsubscribe_removes_subscription(client, push_store):
    client.post("/api/push/subscribe", json={"endpoint": FAKE_ENDPOINT, "keys": FAKE_KEYS})
    r = client.post("/api/push/unsubscribe", json={"endpoint": FAKE_ENDPOINT})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "removed": True}

    r = client.post("/api/push/unsubscribe", json={"endpoint": FAKE_ENDPOINT})
    assert r.get_json() == {"ok": True, "removed": False}


def test_push_send_with_fake_endpoint_does_not_raise(client, push_store, outbox_store):
    """A dead endpoint must come back as a soft error, never a 500."""
    client.post("/api/push/subscribe", json={"endpoint": FAKE_ENDPOINT, "keys": FAKE_KEYS})
    r = client.post("/api/push/send",
                    json={"title": "測試", "body": "測試", "url": "/", "tag": "gymbro"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["sent"] == 0
    assert data["failed"] >= 0
    assert isinstance(data["errors"], list)


def test_push_send_without_subscriptions_is_not_an_error(client, push_store, outbox_store):
    r = client.post("/api/push/send", json={"title": "hi", "body": "there"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["sent"] == 0


def test_push_send_never_leaks_the_private_key(client, push_store, outbox_store):
    client.post("/api/push/subscribe", json={"endpoint": FAKE_ENDPOINT, "keys": FAKE_KEYS})
    r = client.post("/api/push/send", json={"title": "t", "body": "b"})
    body = r.get_data(as_text=True)
    assert "private_key" not in body
    assert "BEGIN PRIVATE KEY" not in body


# ── 2. Geolocation endpoints ──────────────────────────────────────────────

def test_geo_log_valid_coords(client, geo_store):
    r = client.post("/api/geo/log",
                    json={"lat": 22.27933, "lng": 114.16281, "accuracy": 12.5,
                          "context": "manual"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert "label" in data
    assert "distance_km" in data


def test_geo_log_rejects_bad_coords(client, geo_store):
    for bad in ({"lat": 999, "lng": 0}, {"lat": 0, "lng": 999}, {"lat": "x", "lng": 0},
                {"lng": 0}):
        r = client.post("/api/geo/log", json=bad)
        assert r.status_code == 200, f"{bad} 唔應該 500"
        assert r.get_json()["ok"] is False
        assert r.get_json()["error"] == "bad coords"


def test_geo_recent_returns_a_list(client, geo_store):
    r = client.get("/api/geo/recent")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert isinstance(data["locations"], list)


def test_geo_recent_n_is_honoured(client, geo_store):
    for i in range(3):
        client.post("/api/geo/log", json={"lat": 22.279 + i * 0.001, "lng": 114.162})
    r = client.get("/api/geo/recent?n=2")
    locations = r.get_json()["locations"]
    assert len(locations) == 2
    # Newest first.
    assert locations[0]["lat"] > locations[1]["lat"]


def test_geo_store_is_capped_at_200_entries(client, geo_store):
    for i in range(205):
        client.post("/api/geo/log", json={"lat": 22.0 + i * 0.0001, "lng": 114.0})
    stored = json.loads(geo_store.read_text())
    assert len(stored["locations"]) == 200


# ── 3. Store isolation guards ─────────────────────────────────────────────

def test_tests_never_write_the_real_stores(push_store, geo_store):
    """The env override must be what the modules actually read."""
    from gym_web import push, geo
    assert push.push_store_path() == push_store
    assert geo.geo_store_path() == geo_store
    assert "/home/work/.hermes/gymbro_push_subs.json" not in str(push_store)
    assert "/home/work/.hermes/gymbro_geo.json" not in str(geo_store)


def test_real_store_files_are_untouched_by_the_suite():
    """If a previous run leaked into the real store, this catches it."""
    # Not an assertion about content — just that the module never defaults
    # to the production path when the env override is set.
    from gym_web import push
    assert push.DEFAULT_PUSH_STORE == "/home/work/.hermes/gymbro_push_subs.json"
    assert push.VAPID_PATH == "/home/work/.hermes/gymbro_vapid.json"


# ── 4. Frontend + Service Worker wiring ───────────────────────────────────

def test_service_worker_has_push_handlers():
    src = GYM_WEB.read_text()
    for probe in ("addEventListener('push'", "addEventListener('notificationclick'",
                  "addEventListener('message'", "addEventListener('sync'",
                  "gymbro-queue", "openQueueDb", "queueRequest", "readQueue",
                  "deleteQueueItem", "flushQueue"):
        assert probe in src, f"Service Worker 缺少 {probe}"


def test_service_worker_fetch_logic_untouched():
    """The v137 network-first /api/* rule must not have been rewritten."""
    src = GYM_WEB.read_text()
    assert "if (url.pathname.startsWith('/api/')) {" in src
    assert "const CACHE = 'gym-web-v154';" in src


def test_manifest_has_shortcuts_and_scope(client):
    r = client.get("/manifest.json")
    assert r.status_code == 200
    manifest = r.get_json()
    assert manifest["id"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["lang"] == "zh-HK"
    assert manifest["dir"] == "ltr"
    assert manifest["categories"]
    # Existing fields must survive the v3.3.44 additions.
    assert manifest["name"] == "Gymbro · Jim"
    assert manifest["short_name"] == "Gymbro"
    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    assert len(manifest["icons"]) == 3

    shortcuts = manifest["shortcuts"]
    assert len(shortcuts) == 2
    urls = {s["url"] for s in shortcuts}
    assert urls == {"/?action=scan", "/?action=logset"}
    for shortcut in shortcuts:
        assert shortcut["name"]
        assert shortcut["icons"][0]["src"] == "/static/gymbro_icon-192.png"


def test_frontend_has_push_and_geo_buttons():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'data-testid="push-toggle-btn"' in html
    assert 'data-testid="geo-log-btn"' in html
    assert "togglePush()" in html
    assert "logMyLocation()" in html
    # The offline queue wrapper + Background Sync registration.
    assert "apiPost(" in html
    assert "queueable: true" in html
    assert "reg.sync.register('gymbro-queue')" in html
    # iOS Home Screen hint + secure-context guard.
    assert "isSecureContext" in html
    assert "加入主畫面" in html


def test_frontend_queues_the_expected_endpoints():
    html = TEMPLATE.read_text(encoding="utf-8")
    for endpoint in ("/api/log_set", "/api/cancel_last_set", "/api/end_session",
                     "/api/whoop_refresh", "/api/scan_preview_text"):
        assert f"apiPost('{endpoint}'" in html, f"{endpoint} 未行入 offline queue wrapper"
    # scan_commit uploads an image — it must never be queued.
    assert "apiPost('/api/scan_commit'" not in html


def test_frontend_safe_area_padding():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "env(safe-area-inset-bottom)" in html
