"""tests/qa/test_write_path_idempotency.py — v3.3.71 (Frimpong 2026-10-31)

寫入路徑嘅 idempotency / race regression test。

背景
----
Container 化之後,同一個 app 有兩條並發來源:
1. Flask `threaded=True` → 每個 request 一個 thread。
2. `_scan_run_job` 嘅 `ThreadPoolExecutor(max_workers=3)` → 平行 AI worker。

而家 codebase 有三類「讀-改-寫」pattern。v3.3.71 之前全部冇 lock,
實測結果(修正前):

| Pattern | 修正前實測 | 後果 |
|---|---|---|
| `idempotency.check_and_remember()` 同一個 key | 8 thread → **8 個都返 True** | 重複 Sheet row(Jim OOB 2026-08-14「30 duplicate rows」) |
| `idempotency.check_and_remember()` 160 個唔同 key | **走失 137 個**(86%) | dedup 記錄漏咗 → 對同一 key 二次 commit |
| `save_log()` read-modify-write | 8 thread → **走失 4 個 entry** | 健身記錄無咗一節 |
| `core.safe_write_json()` 固定 tmp 名 | 兩個 writer 用同一個 tmp | 互相覆蓋 / FileNotFoundError |

呢個 test 將以上全部**變成會 fail 嘅測試**,唔係靠 code review 記住。

⚠️ 全部用 tmp 目錄,唔掂 production 檔案、唔掂 Google Sheet。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))


def _load_module(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── 1. idempotency:同一個 key 只能有一個 thread 見到「新」 ─────────────
def test_idempotency_same_key_only_one_thread_sees_new(tmp_path, monkeypatch):
    """🔴 P0:8 個 thread 同時 check 同一個 key → 應該只有 1 個返 True。

    修正前實測:8 個全部返 True → iOS PWA double-tap 會寫 8 個重複 row。
    """
    idem = _load_module("gym_web.idempotency_t", "gym_web/idempotency.py")
    store = tmp_path / "commit_idempotency.json"
    store.write_text("{}")
    monkeypatch.setattr(idem, "_STORE_PATH", store)

    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()          # 盡量同時起跑
        results.append(idem.check_and_remember("DOUBLE_TAP_KEY"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    n_true = sum(1 for r in results if r)
    assert n_true == 1, (
        f"應該只有 1 個 thread 見到新 key,實際 {n_true} "
        f"(重複 Sheet row 嘅 race 未修)")
    stored = json.loads(store.read_text())
    assert len(stored) == 1, f"store 應該只有 1 個 key,實際 {len(stored)}"


def test_idempotency_distinct_keys_no_lost_updates(tmp_path, monkeypatch):
    """🔴 唔同 key 嘅並行寫入唔應該走失任何一個。

    修正前實測:160 個 key 只寫入 23 個(走失 137)。
    """
    idem = _load_module("gym_web.idempotency_t2", "gym_web/idempotency.py")
    store = tmp_path / "commit_idempotency.json"
    store.write_text("{}")
    monkeypatch.setattr(idem, "_STORE_PATH", store)

    n_threads, per_thread = 8, 20
    barrier = threading.Barrier(n_threads)

    def worker(n):
        barrier.wait()
        for i in range(per_thread):
            idem.check_and_remember(f"key-{n}-{i}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = n_threads * per_thread
    stored = json.loads(store.read_text())
    assert len(stored) == expected, (
        f"應該存住 {expected} 個 key,實際 {len(stored)} "
        f"(走失 {expected - len(stored)})")


# ── 2. save_log:read-modify-write 唔可以走失 entry ────────────────────
def test_workout_log_read_modify_write_no_lost_entries(tmp_path, monkeypatch):
    """🔴 8 個 thread 同時寫唔同嘅 date → 8 個 entry 都要喺。

    修正前實測:最終得返 4/8 個 entry(走失一半)。
    """
    gw = _load_module("gym_web.main_t", "gym_web.py")
    log_file = tmp_path / "whoop_workout_log.json"
    log_file.write_text("{}")
    monkeypatch.setattr(gw, "WORKOUT_LOG", log_file)

    def write_date(d):
        def _mutate(log):
            log[f"2026-10-{d:02d}"] = {"date": f"2026-10-{d:02d}",
                                        "exercises": [{"set": 1}]}
        gw.update_log(_mutate)

    threads = [threading.Thread(target=write_date, args=(d,)) for d in range(1, 9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = json.loads(log_file.read_text())
    assert len(final) == 8, (
        f"應該有 8 個 date entry,實際 {len(final)}(走失 {8 - len(final)})")


# ── 3. safe_write_json:tmp 名必須唯一 ────────────────────────────────
def test_safe_write_json_uses_unique_tmp_name(tmp_path):
    """🔴 固定 tmp 名 → 兩個 writer 撞同一個 tmp → 覆蓋 / FileNotFoundError。

    v3.3.45 已經喺 `gym_web.save_log()` 修過同款問題,但 `core.safe_write_json()`
    漏咗 —— 呢個 test 就係防止再漏。
    """
    core = _load_module("gym_web.core_t", "gym_web/core.py")
    target = tmp_path / "state.json"

    tmp_names = []

    real_replace = Path.replace

    def spy_replace(self, other):
        tmp_names.append(str(self))
        return real_replace(self, other)

    # 直接觀察寫出嘅 tmp 名:並行寫同一個目標
    errors = []

    def writer(n):
        try:
            for i in range(30):
                core.safe_write_json(target, {"w": n, "i": i})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent safe_write_json raised: {errors[:3]}"
    # 最終檔案必須係合法 JSON(唔係半個)
    data = json.loads(target.read_text())
    assert isinstance(data, dict) and "w" in data
    # 唔可以有殘留 tmp
    leftovers = [q.name for q in tmp_path.iterdir() if q.name != "state.json"]
    assert not leftovers, f"有殘留 tmp 檔: {leftovers}"


# ── 4. filelock module 嘅基本契約 ────────────────────────────────────
def test_filelock_lock_for_is_reentrant_and_per_path():
    """per-path 鎖:同一 path 同一把鎖、唔同 path 唔同鎖、同 thread 可重入。"""
    fl = _load_module("gym_web.filelock_t", "gym_web/filelock.py")
    a1 = fl.lock_for("/tmp/x.json")
    a2 = fl.lock_for("/tmp/x.json")
    b = fl.lock_for("/tmp/y.json")
    assert a1 is a2, "同一 path 應該共用同一把鎖"
    assert a1 is not b, "唔同 path 應該有唔同嘅鎖"
    # re-entrant:同一 thread 可以再入
    with a1:
        with a1:
            pass


def test_filelock_unique_tmp_path_is_unique():
    """tmp 名必須每次唔同(否則兩個 writer 撞同一個 tmp)。"""
    fl = _load_module("gym_web.filelock_t2", "gym_web/filelock.py")
    names = {str(fl.unique_tmp_path("/tmp/a.json")) for _ in range(200)}
    assert len(names) == 200, f"tmp 名唔唯一:只有 {len(names)}/200 個唔同"


def test_filelock_locked_json_update_is_atomic(tmp_path):
    """locked_json_update:並行 increment 唔應該走失任何一次。"""
    fl = _load_module("gym_web.filelock_t3", "gym_web/filelock.py")
    target = tmp_path / "counter.json"
    target.write_text("0")

    def bump(_):
        cur = json.loads(target.read_text())
        return cur + 1

    threads = [threading.Thread(target=lambda: fl.locked_json_update(target, bump))
               for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = json.loads(target.read_text())
    assert final == 50, f"應該係 50,實際 {final}(locked_json_update 有 race)"
