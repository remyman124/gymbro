"""Start an ISOLATED gymbro instance for E2E tests.

- port 7001 (production stays on 7000, untouched)
- GYMBRO_WRITE_MODE=dryrun  → 唔會寫入 Jim 嘅 production Nutrition tab
                              (亦唔會上傳 Drive 檔案)
- 測試用嘅本地 scan_cache / scan_thumb_cache 檔案係 ephemeral buffer,
  測試完會清理。

⚠️ PYTHONPATH 一定要清空:呢部機嘅 Hermes venv 係 Python 3.14,佢個
site-packages 會令 system python3.13 載入 3.14 嘅 PIL `_imaging.so`
→ ImportError。所以清走 PYTHONPATH,再自己把 repo root 加返落 sys.path
(gym_web.py 會 import 同目錄嘅 workout_formatter 等模組)。
"""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path("/home/work/projects/gymbro")

os.environ["GYMBRO_WRITE_MODE"] = "dryrun"
os.environ["PYTHONUNBUFFERED"] = "1"
os.environ["GYMBRO_SCAN_PARALLEL"] = "3"

# 3.14 venv 嘅 site-packages 要踢走(見上面 docstring)。
_bad = "environments/44fe35297bb545df912bf006b0709541/venv/lib/python3.14/site-packages"
sys.path = [p for p in sys.path if _bad not in p]
# repo root 要喺最前(gym_web.py import 同目錄模組用)。
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("gym_web_test_inst", ROOT / "gym_web.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["gym_web_test_inst"] = mod
spec.loader.exec_module(mod)

print("[test-instance] write_mode=%s port=7001" % mod._write_mode(), flush=True)
assert mod._write_mode() == "dryrun", "test instance 必須係 dryrun!"

mod.app.run(host="127.0.0.1", port=7001, debug=False, threaded=True)
