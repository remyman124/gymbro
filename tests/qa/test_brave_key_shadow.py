"""v3.3.65 regression — Brave API key 陰影 bug(2026-10-29)。

**Incident**:gymbro.log 出咗 258 次
`[brave_web_search] HTTPError: HTTP Error 422: Unprocessable Entity`,
每次 cheer 都係。查 Brave API 回應 body:

```json
{"type":"ErrorResponse","error":{"status":422,
 "detail":"The provided API key is invalid.",
 "meta":{"component":"authentication"},"code":"SUBSCRIPTION_TOKEN_INVALID"}}
```

即係 **認證失敗**,唔係 query 有問題。

**根因**:`gym_web.py` 啟動時依次

```python
load_dotenv("/home/work/.hermes-torres/.env")   # ← 先載入
load_dotenv("/home/work/.hermes/.env")          # ← 後載入
```

python-dotenv 嘅 `load_dotenv()` 預設 `override=False` → **先載入嗰個贏**。
`.hermes-torres/.env` 嗰條 `BRAVE_API_KEY` 已經失效(22 字元,唔再係有效
subscription),長期遮住 `.hermes/.env` 嗰條有效嘅(31 字元)。所以
`os.environ["BRAVE_API_KEY"]` 一直係嗰條壞嘅。

**後果**:cheer 嘅三樣 web context 全部變空 —— 新聞、基金 fact、利物浦
賽程(`_cheer_web_fact` 收到空結果就回 `''`)。

**修法**:唔再淨係信 `os.environ`。用 `dotenv_values()`(只解析唔注入)
由每份 .env 檔攞原始值,砌成候選清單;每次請求逐把試,遇到
401/403/422 就跳去下一把,成功嗰把記落 cache。

**守則**:
1. `_brave_api_key_candidates()` 必須包含每份 .env 檔嘅原始 key
   (唔可以淨係靠 `os.environ`)。
2. 401/403/422 → 試下一把,唔好即刻放棄(否則又變返「靜靜哋失效」)。
3. 成功嗰把要記低(cache),唔好每次都由頭試。
4. 認證失敗嘅 key 唔可以再單獨用。

⚠️ 本測試 **唔打任何真實 API**(用 fake opener),唔會寫任何生產資料。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import urllib.error
from pathlib import Path

import pytest

REPO = Path(os.environ.get("GYMBRO_REPO", "/home/work/projects/gymbro"))
GYM_WEB = REPO / "gym_web.py"


# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────

def _load_gym_web():
    """Load gym_web.py by path (gym_web/ package shadows it)."""
    spec = importlib.util.spec_from_file_location("gym_web_brave_test", GYM_WEB)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


class _FakeHTTPError(urllib.error.HTTPError):
    """Construct a HTTPError without touching the network."""
    def __init__(self, code):
        super().__init__("https://example.invalid", code, "boom", {}, None)


def _fake_urlopen(script):
    """Build a urlopen stub driven by `script`: {token_prefix: result}.

    result is either a dict (returned as JSON) or an int HTTP status code
    (raised as HTTPError).
    """
    def _open(req, timeout=None):
        token = ""
        for k, v in (getattr(req, "headers", {}) or {}).items():
            if k.lower() == "x-subscription-token":
                token = v
                break
        for prefix, result in script.items():
            if token.startswith(prefix):
                if isinstance(result, int):
                    raise _FakeHTTPError(result)
                return _FakeResp(json.dumps(result).encode())
        raise _FakeHTTPError(403)
    return _open


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ─────────────────────────────────────────────────────────────
# 1. Source-level invariants (cheap, no import needed)
# ─────────────────────────────────────────────────────────────

def test_source_has_key_candidate_list():
    """gym_web.py 必須有候選 key 清單(唔可以淨係 os.environ)。"""
    src = GYM_WEB.read_text(encoding="utf-8")
    assert "def _brave_api_key_candidates" in src, (
        "冇 _brave_api_key_candidates() —— 陰影 bug 未修")
    assert "dotenv_values" in src, (
        "要用 dotenv_values() 讀 .env 原始值,唔可以淨係靠 os.environ")


def test_source_load_dotenv_order_documented():
    """兩份 .env 嘅載入順序係呢個 bug 嘅核心 —— 註解要講清楚。"""
    src = GYM_WEB.read_text(encoding="utf-8")
    assert "load_dotenv(\"/home/work/.hermes-torres/.env\")" in src
    assert "load_dotenv(\"/home/work/.hermes/.env\")" in src
    i_torres = src.index("load_dotenv(\"/home/work/.hermes-torres/.env\")")
    i_hermes = src.index("load_dotenv(\"/home/work/.hermes/.env\")")
    assert i_torres < i_hermes, (
        "順序變咗 —— dotenv first-wins,誰先載入誰贏")


def test_source_treats_422_as_auth_failure():
    """401/403/422 都要當認證失敗 → 試下一把 key。"""
    src = GYM_WEB.read_text(encoding="utf-8")
    assert "e.code in (401, 403, 422)" in src, (
        "422(SUBSCRIPTION_TOKEN_INVALID)唔可以當成 fatal error,"
        "一定要跳去下一把候選 key")


def test_brave_helpers_no_longer_swallow_key_error():
    """唔准再 `except Exception: return []` 咁靜靜哋吞掉認證錯。"""
    src = GYM_WEB.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef)}
    for name in ("_brave_web_search", "_brave_image_search"):
        assert name in fns, f"{name} 唔見咗"
        body = ast.dump(fns[name])
        assert "except" not in body or "_brave_get_json" in body, (
            f"{name} 唔可以自己吞 exception —— 統一行 _brave_get_json,"
            "由佢逐把試 key")


# ─────────────────────────────────────────────────────────────
# 2. Behavioural tests (fake urlopen — never touches the network)
# ─────────────────────────────────────────────────────────────

def test_candidates_include_env_file_values(monkeypatch):
    """候選清單要包括 .env 檔入面嘅原始 key(唔淨係 os.environ)。"""
    m = _load_gym_web()
    monkeypatch.setenv("BRAVE_API_KEY", "ENVKEY")
    cands = m._brave_api_key_candidates()
    keys = [k for k, _ in cands]
    assert "ENVKEY" in keys, "os.environ 嘅 key 要喺候選清單入面"
    # .hermes/.env 嘅原始值一定要喺入面(就係被 load_dotenv 遮住嗰把)
    real = m._brave_env_key("/home/work/.hermes/.env")
    if real:
        assert real in keys, (
            ".hermes/.env 嘅原始 key 唔喺候選清單 —— 陰影 bug 未修")
    # 唔准有重複
    assert len(keys) == len(set(keys)), "候選清單有重複 key"


def test_candidates_are_deduped(monkeypatch):
    m = _load_gym_web()
    monkeypatch.setenv("BRAVE_API_KEY", "SAMEKEY")
    keys = [k for k, _ in m._brave_api_key_candidates()]
    assert len(keys) == len(set(keys))


def test_get_json_falls_back_to_next_key_on_422(monkeypatch):
    """第一把 key 被拒(422)→ 自動試第二把,唔好靜靜哋返回空。"""
    m = _load_gym_web()
    monkeypatch.setattr(
        m, "_brave_api_key_candidates",
        lambda: [("BADKEY", "bad"), ("GOODKEY", "good")],
    )
    monkeypatch.setattr(
        m.urllib.request, "urlopen",
        _fake_urlopen({
            "BADKEY": 422,
            "GOODKEY": {"web": {"results": [{"title": "T", "description": "D"}]}},
        }),
    )
    data = m._brave_get_json("https://example.invalid/web/search")
    assert data.get("web", {}).get("results"), (
        "422 之後應該跳去下一把 key 並攞到結果")
    assert m._BRAVE_KEY_CACHE["key"] == "GOODKEY", (
        "成功嗰把 key 要記低,下次唔使再試錯")


def test_get_json_falls_back_on_401_and_403(monkeypatch):
    for code in (401, 403):
        m = _load_gym_web()
        monkeypatch.setattr(
            m, "_brave_api_key_candidates",
            lambda: [("BADKEY", "bad"), ("GOODKEY", "good")],
        )
        monkeypatch.setattr(
            m.urllib.request, "urlopen",
            _fake_urlopen({
                "BADKEY": code,
                "GOODKEY": {"web": {"results": [{"title": "ok"}]}},
            }),
        )
        data = m._brave_get_json("https://example.invalid/web/search")
        assert data.get("web", {}).get("results"), (
            f"{code} 之後應該跳去下一把 key")


def test_get_json_returns_empty_when_all_keys_fail(monkeypatch):
    """全部 key 都唔得 → 返回空 dict(唔好拋 exception 出嚟搞冧 cheer)。"""
    m = _load_gym_web()
    monkeypatch.setattr(
        m, "_brave_api_key_candidates",
        lambda: [("K1", "a"), ("K2", "b")],
    )
    monkeypatch.setattr(
        m.urllib.request, "urlopen",
        _fake_urlopen({"K1": 422, "K2": 422}),
    )
    data = m._brave_get_json("https://example.invalid/web/search")
    assert data == {}


def test_non_auth_http_error_does_not_try_other_keys(monkeypatch):
    """500 = 唔關 key 事 → 唔應該浪費時間試其它 key。"""
    m = _load_gym_web()
    calls = []

    def _open(req, timeout=None):
        calls.append(req)
        raise _FakeHTTPError(500)

    monkeypatch.setattr(
        m, "_brave_api_key_candidates",
        lambda: [("K1", "a"), ("K2", "b")],
    )
    monkeypatch.setattr(m.urllib.request, "urlopen", _open)
    data = m._brave_get_json("https://example.invalid/web/search")
    assert data == {}
    assert len(calls) == 1, f"500 唔應該試多過一把 key(試咗 {len(calls)} 次)"


def test_web_search_returns_titles(monkeypatch):
    """正常情況下 _brave_web_search 照樣回「標題 — 描述」。"""
    m = _load_gym_web()
    monkeypatch.setattr(
        m, "_brave_api_key_candidates",
        lambda: [("GOODKEY", "good")],
    )
    monkeypatch.setattr(
        m.urllib.request, "urlopen",
        _fake_urlopen({
            "GOODKEY": {"web": {"results": [
                {"title": "利物浦贏波", "description": "2-1"},
                {"title": "", "description": "x"},
            ]}},
        }),
    )
    out = m._brave_web_search("Liverpool result", count=4)
    assert out == ["利物浦贏波 — 2-1"], f"結果唔對:{out}"


def test_web_search_empty_query_returns_empty(monkeypatch):
    m = _load_gym_web()
    assert m._brave_web_search("") == []
    assert m._brave_web_search("   ") == []


def test_image_search_returns_results(monkeypatch):
    """image search 都要行同一條 fallback 邏輯。"""
    m = _load_gym_web()
    monkeypatch.setattr(
        m, "_brave_api_key_candidates",
        lambda: [("BADKEY", "bad"), ("GOODKEY", "good")],
    )
    monkeypatch.setattr(
        m.urllib.request, "urlopen",
        _fake_urlopen({
            "BADKEY": 422,
            "GOODKEY": {"results": [{
                "title": "牛腩飯",
                "properties": {"url": "https://x.invalid/a.jpg"},
                "thumbnail": {"src": "https://x.invalid/t.jpg"},
                "source": "s",
                "confidence": "high",
            }]},
        }),
    )
    out = m._brave_image_search("牛腩飯", count=3)
    assert out and out[0]["url"] == "https://x.invalid/a.jpg", f"結果唔對:{out}"


def test_real_env_files_have_a_usable_key():
    """實機檢查:至少一份 .env 有一把真係用得嘅 key(長度 31)。

    ⚠️ 只讀檔案,唔會改動 os.environ,亦唔會打 API。
    """
    from dotenv import dotenv_values
    found = []
    for p in ("/home/work/.hermes/.env",
              "/home/work/.hermes-torres/.env",
              "/home/work/.hermes/profiles/frimpong/.env"):
        if not Path(p).exists():
            continue
        k = (dotenv_values(p).get("BRAVE_API_KEY") or "").strip()
        if k:
            found.append((p, len(k)))
    assert found, "所有 .env 都搵唔到 BRAVE_API_KEY"
    assert any(n >= 30 for _, n in found), (
        f"冇一把 key 似有效長度(>=30):{found}")
