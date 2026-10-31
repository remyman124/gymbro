# GymBro — 營養/訓練 PWA(v3.3.70)
#
# 用途:令 gymbro 成為「6 個 sub-agent 共用嘅 Docker Catalog」入面一份
#       真實 entry(之前 gymbro 淨係靠 tmux + 系統 Python 行,冇 image)。
#
# Build:  docker build -t gymbro:local /home/work/projects/gymbro
# Run:    docker compose -f docker-compose.yml up -d
#
# ⚠️ 同一部機唔可以同時行 tmux 版同 container 版 —— 兩個都 bind 7000。
#    切換步驟(唔想影響 production 就唔好做):
#      1. tmux kill-session -t gymbro
#      2. docker compose -f docker-compose.yml up -d
#      3. curl -s http://127.0.0.1:7000/api/health
#    試跑 / 藍綠用(唔撞 port,唔寫入 production Sheet):
#      docker run --rm --network host -e GYMBRO_PORT=7001 \
#        -e GYMBRO_WRITE_MODE=dryrun gymbro:local
#
FROM python:3.12-slim

LABEL maintainer="Alonso <alonso@hermes.local>" \
      description="GymBro PWA — 營養掃描 / 訓練記錄 / 每日廣東話 cheer"

ENV TZ=Asia/Hong_Kong \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GYMBRO_PORT=7000

# CJK 字體係必需嘅:
#   fonts-noto-cjk      → cheer_motivation.png(PIL 畫中文)
#   fonts-wqy-zenhei    → 營養日結 PDF(reportlab 只支援 TrueType/glyf,
#                         Noto CJK 係 CFF/OTF 用唔到,要用文泉驛正黑)
#   fonts-dejavu-core   → 拉丁字型 fallback
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        tzdata \
        fonts-noto-cjk \
        fonts-wqy-zenhei \
        fonts-dejavu-core \
    && ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
# reportlab / redis 唔喺 requirements.txt 入面(reportlab 係 PDF 模組用,
# redis 係 gym_web/redis_cache.py 用)—— 一併裝埋,免得 container 少功能。
RUN pip install --no-cache-dir -r requirements.txt \
        reportlab==4.2.5 \
        redis \
        ddgs \
        opencc-python-reimplemented \
        faster-whisper\
        py-vapid \
        pywebpush \
        duckduckgo_search

COPY . .

EXPOSE 7000

# v3.3.71 (Frimpong 2026-10-31):health check 收緊。
# 原本淨係 `curl -fsS /api/health` —— curl -f 只檢查 HTTP status,
# 而 /api/health 永遠回 200(就算 ok:false),所以呢個 check 永遠 pass。
# 而家 assert 埋 body 嘅 "ok":true + startup_check 通過 + 版本一致。
# ⚠️ 唔好淨係 grep '"ok":true' —— body 內 "startup_check_ok" 都係 true 字串,
#    要用 python 解析 JSON 先准。
#
# ⚠️ v3.3.71 修正:version_match 唔可以係 hard requirement。
#    `_ver_img` 由 Dockerfile 嘅第一個 `vX.Y.Z` regex 抽 —— 而個 regex 會
#    撞 Dockerfile 註釋入面嘅版本號(例如 "v3.3.71 (Frimpong 2026-10-31)" 呢句
#    註釋,而唔係真正嘅 image 版本)。一 bump 版本就會 mismatch → 呢個 check
#    永久 exit 1 → container 永久 unhealthy → restart loop 風險。
#    所以:版本一致性係「資訊」,唔係「生死」—— 真正嘅生死判斷係
#    (a) /api/health 自己 ok:true(b) startup_check 冇 failed check。
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python3 -c "import json,os,sys,urllib.request; \
|u='http://127.0.0.1:'+os.environ.get('GYMBRO_PORT','7000')+'/api/health'; \
|d=json.load(urllib.request.urlopen(u,timeout=8)); \
|c=d.get('checks',{}); \
|sc=d.get('startup_check') or {}; \
|bad=[k for k in sc.get('failed_checks',[]) if k]; \
|sys.stderr.write('healthcheck failed_checks=%r version=%r version_match=%r\n'%(bad,d.get('version'),c.get('version_match'))); \
|sys.exit(0 if (d.get('ok') is True and not bad) else 1)" || exit 1

CMD ["python3", "gym_web.py"]
