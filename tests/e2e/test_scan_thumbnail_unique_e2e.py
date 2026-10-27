"""
E2E — v3.3.52: 平行掃描 4 張相 → 4 個唔同縮圖。

重現 Jim OOB 2026-10-27「For food upload, the immediate thumbnail are all
the same. Despite in the end, it works」。

舊 code 嘅行為(實測):
  * 4 張相、3 個平行 worker、全部同一秒開工
  * final_name = f"scan_{ts:%Y%m%d_%H%M%S}.jpg" → 撞名
  * shutil.copyfile 互相覆蓋 → 4 張相只剩 2 個檔案
  * 4 條 row 指向同一個 /scan_img/... URL → 4 個縮圖一模一樣

新 code:4 張相 → 4 個唔同檔名 → 4 個唔同 URL → 4 個唔同縮圖。

⚠️ 安全:行喺 GYMBRO_WRITE_MODE=dryrun,而且用**合成** fixture 圖
(唔會用真實食物相,避免 AI 生成似真實菜式 + 熱量嘅假 row)。
"""
import hashlib
import os
import sys
import time
from pathlib import Path

import requests

# ⚠️ MUST point at the ISOLATED dryrun instance (port 7001), NEVER
# production (7000). Writing to Jim's live Nutrition tab is forbidden.
BASE = os.environ.get("GYMBRO_TEST_BASE", "http://127.0.0.1:7001")
RESULTS = []


def log(name, ok, msg=""):
    print("  %s %s: %s" % ("OK  " if ok else "FAIL", name, msg))
    RESULTS.append((name, ok, msg))


def make_fixture(path: Path, seed: int):
    """Make 4 visually DIFFERENT synthetic images (solid colour + gradient)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (900, 900))
    d = ImageDraw.Draw(img)
    for y in range(900):
        r = (seed * 60 + y) % 256
        g = (seed * 90 + y // 2) % 256
        b = (seed * 120 + y // 3) % 256
        d.line([(0, y), (900, y)], fill=(r, g, b))
    d.ellipse([100 + seed * 40, 200, 500 + seed * 40, 600], fill=(255, 255, 255))
    d.text((80, 80), "QA FIXTURE %d" % seed, fill=(0, 0, 0))
    img.save(str(path), "JPEG", quality=90)
    return hashlib.md5(path.read_bytes()).hexdigest()


def main():
    fixtures = Path("/home/work/.hermes/cache/scratch/qa_v3352_fixtures")
    fixtures.mkdir(parents=True, exist_ok=True)

    print("=== 準備 4 張唔同嘅合成 fixture ===")
    paths, md5s = [], []
    for i in range(4):
        p = fixtures / ("qa_v3352_%d.jpg" % i)
        md5s.append(make_fixture(p, i))
        paths.append(p)
    assert len(set(md5s)) == 4, "fixture 唔應該一樣"
    print("  fixture md5: %s" % ", ".join(m[:8] for m in md5s))

    print("\n=== 上載 4 張相 ===")
    files = []
    for p in paths:
        files.append(("images", (p.name, open(p, "rb"), "image/jpeg")))
    r = requests.post(f"{BASE}/api/scan_upload", files=files, timeout=60)
    r.raise_for_status()
    data = r.json()
    uploads = data.get("uploads") or []
    log("E01 上載成功", len(uploads) == 4, "uploads=%d" % len(uploads))
    if len(uploads) != 4:
        print(json.dumps(data)[:600])
        return 1

    upload_names = [u["path"].rsplit("/", 1)[-1] for u in uploads]
    log("E02 4 個上載檔名全部唔同", len(set(upload_names)) == 4, str(upload_names))
    upload_m5 = [hashlib.md5(open(u["path"], "rb").read()).hexdigest() for u in uploads]
    log("E03 4 個上載內容全部唔同", len(set(upload_m5)) == 4, str([m[:8] for m in upload_m5]))

    print("\n=== 啟動 AI 掃描(平行) ===")
    r = requests.post(
        f"{BASE}/api/scan_recognize",
        json={"paths": [u["path"] for u in uploads]},
        timeout=60,
    )
    r.raise_for_status()
    job = r.json()
    job_id = job.get("job_id")
    log("E04 掃描 job 開到", bool(job_id), "job_id=%s" % job_id)

    print("  等 AI 跑完(最多 240 秒)...")
    final = None
    for _ in range(240):
        time.sleep(1)
        p = requests.get(f"{BASE}/api/scan_progress/{job_id}", timeout=30).json()
        if p.get("status") in ("completed", "failed", "error"):
            final = p
            break
    if not final:
        log("E05 掃描有完成", False, "timeout")
        return 1
    log("E05 掃描有完成", final.get("status") == "completed",
        "status=%s results=%d" % (final.get("status"), len(final.get("results") or [])))

    results = final.get("results") or []
    log("E06 有 4 個結果", len(results) == 4, "results=%d" % len(results))

    # 核心檢查:每個結果嘅 image_url 必須唔同
    image_urls = []
    for res in results:
        pv = res.get("preview") or {}
        image_urls.append(pv.get("image_url") or res.get("image_url") or "")
    non_empty = [u for u in image_urls if u]
    log("E07 每個結果都有 image_url", len(non_empty) == 4, str(image_urls))
    log("E08 4 個 image_url 全部唔同(縮圖唔會一樣)", len(set(non_empty)) == 4,
        "unique=%d" % len(set(non_empty)))

    # 核心檢查:實際檔案存在 + 內容唔同
    local_paths = []
    for res in results:
        ip = (res.get("preview") or {}).get("image_path") or res.get("image_path") or ""
        if ip:
            local_paths.append(Path(ip))
    log("E09 結果有本地路徑", len(local_paths) == 4, str([p.name for p in local_paths]))

    exist = [p.exists() for p in local_paths]
    log("E10 4 個 final 檔案全部存在(冇相被吞)", all(exist) and len(exist) == 4,
        "exist=%s" % exist)

    if all(exist) and local_paths:
        m5 = [hashlib.md5(p.read_bytes()).hexdigest() for p in local_paths]
        log("E11 4 個 final 檔案內容全部唔同", len(set(m5)) == 4,
            "unique=%d %s" % (len(set(m5)), [m[:8] for m in m5]))

        # 縮圖檔案
        thumbs = [p.parent.parent / "scan_thumb_cache" / (p.stem + ".thumb.jpg")
                  for p in local_paths]
        th_exist = [t.exists() for t in thumbs]
        log("E12 4 個縮圖檔全部存在", all(th_exist) and len(th_exist) == 4,
            str([t.name for t in thumbs]))
        if all(th_exist):
            th_m5 = [hashlib.md5(t.read_bytes()).hexdigest() for t in thumbs]
            log("E13 4 個縮圖內容全部唔同(Jim 見到嘅症狀)", len(set(th_m5)) == 4,
                "unique=%d %s" % (len(set(th_m5)), [m[:8] for m in th_m5]))

            # HTTP 層面:真正取縮圖,確認 byte 唔同
            http_m5 = []
            for t in thumbs:
                resp = requests.get(f"{BASE}{t.as_posix().replace('/home/work/.hermes/scan_thumb_cache', '/scan_thumb')}",
                                    timeout=30)
                http_m5.append(hashlib.md5(resp.content).hexdigest())
            log("E14 HTTP 取得嘅 4 個縮圖 byte 全部唔同", len(set(http_m5)) == 4,
                "unique=%d" % len(set(http_m5)))

    # 檔名格式:應該有 uuid 成分
    names = [p.name for p in local_paths]
    log("E15 final 檔名含 uuid 成分(唔再撞名)",
        all(len(n.split("_")) >= 4 for n in names) if names else False,
        str(names))

    failed = [r for r in RESULTS if not r[1]]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("FAILED: " + ", ".join(f[0] for f in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
