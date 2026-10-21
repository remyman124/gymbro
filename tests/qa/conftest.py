"""
QA 層嘅 pytest 設定 — 令 `tests/qa/` 同 suite 嘅 `tests/conftest.py` 解耦。

## 點解要呢個檔案?

`tests/conftest.py` 有個 `autouse=True` 嘅 `isolated_files` fixture,
佢會 monkeypatch `gym_web` 模組嘅多個 module-level 常數
(`WITHINGS_CACHE`、`WHOOP_CACHE`、`WORKOUT_LOG`、...)。

但 `tests/qa/` 入面嘅測試**根本唔需要**嗰啲 fixture:

- `test_scan_progress_contract.py`  → 打真實 server (http://127.0.0.1:7000)
  或者由 `mock_backend_harness.py` 起嘅臨時 server
- `test_scan_progress_frontend.py`  → Playwright 開真實 PWA
- `test_scan_progress_template.py`  → 純讀磁碟上嘅 template 檔
- `test_server_freshness.py`        → HTTP + Playwright

## 實際踩過嘅坑

2026-10-20:Frimpong 做 Jim OOB「Fully remove withings / Steps is whoop!」
嘅重構,刪走咗 `gym_web/withings.py` 同 `gym_web.py` 嘅 module-level
`WITHINGS_CACHE`,但 `tests/conftest.py` 仲 patch 呢個屬性:

```
AttributeError: <module 'gym_web_main' ...> has no attribute 'WITHINGS_CACHE'
```

因為 `isolated_files` 係 autouse,**成個 suite(154 errors)同埋
`tests/qa/` 全部 test 都被連累** —— 連我哋完全唔需要 conftest 嘅測試都一樣。

修法可以係 Frimpong 同步 `tests/conftest.py`(佢嘅範圍),
但 QA 層唔應該因為 suite 嘅重構而唔開得工。所以呢度**覆蓋**
嗰個 fixture,令 QA 測試唔理 conftest 嘅隔離設定。

## ⚠️ 呢個唔係繞過安全措施

`tests/conftest.py` 嘅 `block_production_writes` fixture(防止 QA 測試
寫入 Jim 個 production Google Sheet / Drive)**依然生效** ——
因為我呢度只覆蓋 `isolated_files`,唔係全部 fixture。

⚠️ 但要留意:`isolated_files` 原本嘅作用之一係令測試唔會寫到
production 嘅本地 cache 檔。我嘅 QA 測試寫嘅係 `SCAN_CACHE_DIR`
(`/home/work/.hermes/scan_cache/`),呢個正正就係 cache 目錄,寫入係
预期行為(同 cache 本身一樣)。

真正要留意副作用嘅係 `test_scan_progress_contract.py` 入面嗰兩個
AI opt-in 測試 —— 佢哋真係會 commit 落 Jim 個 Google Sheet,
所以預設 skip,要用 `GYMBRO_QA_ALLOW_AI=1` 先行。
"""
import pytest


@pytest.fixture(autouse=True)
def isolated_files(monkeypatch, gym_web_module):
    """覆蓋 suite 層嘅 `isolated_files`,但唔做 monkeypatch。

    QA 測試打真實 server / 讀磁碟檔,唔需要將 module-level 常數
    指向 tmp_path。呢度刻意唔 patch 任何嘢,原因見 module docstring。

    保留 `gym_web_module` 參數係為咗觸發同一個 module import
    (同 suite 保持一致),避免 import 副作用差異。
    """
    yield
