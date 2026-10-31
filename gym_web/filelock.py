"""gym_web/filelock.py — v3.3.71 (Frimpong 2026-10-31):跨 thread 嘅檔案寫入協調。

背景
----
容器化之後,同一個 app 有**兩條**並發來源:

1. Flask 本身 `threaded=True` → 每個 request 一個 thread。
2. v3.3.70 `_scan_run_job` 用 `ThreadPoolExecutor(max_workers=3)` 平行做 AI,
   每個 worker 寫 state 檔。

而家 codebase 入面有三類「讀-改-寫」pattern,**全部冇 lock**:

| 位置 | pattern | 壞法(實測) |
|---|---|---|
| `gym_web/idempotency.py` `check_and_remember()` | load → check → modify → persist | 8 個 thread 同 key → **8 個都返 True** → 重複 Sheet row |
| `gym_web.py` `save_log()` | load log → modify → write | 8 個 thread → **走失 4 個 entry** |
| `gym_web/core.py` `safe_write_json()` | 固定 tmp 名 → replace | 兩個 writer 用同一個 tmp 名 → 互相覆蓋 |

`safe_write_json()` 更加係用**固定** tmp 名(`path.suffix + ".tmp"`),
所以就算外層有 lock 都仲會撞 —— `save_log()` 喺 v3.3.45 已經改用
`pid + thread ident` 做 tmp 名,但 `core.safe_write_json()` 冇。

呢個 module 提供
--------------
- **per-path re-entrant lock**:同一個檔案嘅 read-modify-write 序列化,
  唔同檔案唔阻塞(唔會將所有寫入都排成一行龍)。
- **唯一 tmp 名**:每個 writer 用自己嘅 tmp,所以唔使靠 luck 唔撞。
- **`locked_json_update()`**:一次過做「讀 → 改 → 原子寫」,全部喺同一把鎖入面。

設計原則
--------
- **唔會死鎖**:用 `threading.RLock`(同一 thread 可以重入),
  而且每個 path 一把鎖 → 唔會出現「A 持住 X 等 Y,Y 持住 Y 等 X」。
- **fail-open**:任何 IO 錯誤唔會阻止 app 運作(同 `idempotency.py` 嘅
  既有契約一致)—— 只係 print 一行,唔會 raise 死 request。
- **唔會影響 Sheet 寫入**:呢個只管本機 state 檔。Sheet 嗰邊有自己嘅
  `_SHEET_WRITE_LOCK` + in-sheet dedup,兩層都要有。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

__all__ = [
    "lock_for",
    "locked_json_update",
    "unique_tmp_path",
    "atomic_write_json",
    "locked_read_json",
]

# path(str) -> RLock。用一張全局表 + 一把表鎖,避免 race 喺建鎖本身。
_locks: Dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(path: Any) -> threading.RLock:
    """攞(或者建)某個 path 專屬嘅 re-entrant lock。

    用 `str(path)` 做 key,咁 `Path("/a")` 同 `"/a"` 會共用同一把鎖。
    """
    key = str(path)
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = threading.RLock()
            _locks[key] = lk
        return lk


def unique_tmp_path(path: Any, tag: str = "") -> Path:
    """一個**唔會撞**嘅 tmp 路徑。

    `pid` + `thread ident` + `time_ns` + 隨機尾巴 —— 四重保險:
    就算同一 thread 同一納秒連續寫兩次都唔會撞。
    """
    p = Path(path)
    suffix = p.suffix
    # `foo.json` → `foo.json.tmp`;`foo` → `foo.tmp`
    stem = p.name if suffix else p.name
    tail = f".{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
    if tag:
        tail += f".{tag}"
    return p.with_name(f"{stem}{tail}.tmp")


def atomic_write_json(path: Any, data: Any, indent: int = 2) -> bool:
    """原子寫 JSON(tmp → `os.replace`),用唯一 tmp 名。

    回傳 True/False,**唔會 raise**。
    """
    p = Path(path)
    tmp = unique_tmp_path(p)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # 某些檔案系統唔支持 fsync;唔值得為此失敗
                pass
        os.replace(tmp, p)
        return True
    except Exception as e:
        print(f"[filelock] atomic_write_json failed for {p}: "
              f"{type(e).__name__}: {e}", flush=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def locked_read_json(path: Any, default: Any = None) -> Any:
    """喺 path 專屬鎖入面讀 JSON。讀唔到就回 default(唔會 raise)。"""
    p = Path(path)
    with lock_for(p):
        try:
            if p.exists():
                with open(p) as f:
                    return json.load(f)
        except Exception as e:
            print(f"[filelock] locked_read_json failed for {p}: "
                  f"{type(e).__name__}: {e}", flush=True)
        return {} if default is None else default


def locked_json_update(
    path: Any,
    mutator: Callable[[Any], Any],
    default: Any = None,
) -> Any:
    """「讀 → 改 → 原子寫」一次過做,全部喺同一把鎖入面。

    `mutator` 收到目前嘅內容,回傳新內容(或 None = 唔寫)。
    呢個係修 read-modify-write race 嘅正解 —— 唔係「讀完再寫」,
    而係「讀、改、寫」三步都喺鎖入面,唔會有中間畀人插隊。

    回傳 mutator 嘅結果(通常係 mutator 回傳嘅新內容)。
    """
    p = Path(path)
    with lock_for(p):
        data: Any
        try:
            if p.exists():
                with open(p) as f:
                    data = json.load(f)
            else:
                data = {} if default is None else default
        except Exception:
            data = {} if default is None else default
        try:
            new_data = mutator(data)
        except Exception as e:
            print(f"[filelock] locked_json_update mutator failed for {p}: "
                  f"{type(e).__name__}: {e}", flush=True)
            return data
        if new_data is not None:
            atomic_write_json(p, new_data)
        return new_data
