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

HEALTHCHECK --interval=30s --timeout=6s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${GYMBRO_PORT}/api/health" || exit 1

CMD ["python3", "gym_web.py"]
