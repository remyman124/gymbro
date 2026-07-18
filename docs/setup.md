# Setup — gymbro

A Flask-based gym set logger for personal use. Runs as a single Python process on Jim's home server, accessed over Tailscale VPN.

## Prerequisites

- Python 3.10+ (tested on 3.13)
- `pip`
- Optional: Tailscale for remote access (iPhone on the bench)
- Optional: `MINIMAX_API_KEY` for daily motivation image generation

## Install

```bash
git clone https://github.com/jimman-work/gymbro.git
cd gymbro
pip install -r requirements.txt
```

That's it. No system packages, no database, no build step.

## Run

```bash
python3 gym_web.py
```

The app binds to `0.0.0.0:7000` by default. Override with environment:

```bash
PORT=8080 python3 gym_web.py
```

### Access

| Where | URL |
|---|---|
| Same machine | http://localhost:7000 |
| Tailscale (iPhone) | http://100.114.66.125:7000 |
| Local network | http://192.168.x.x:7000 |

Add to iPhone home screen via Safari → Share → **Add to Home Screen** for PWA mode (full-screen, no browser chrome).

## Optional — daily motivation image

```bash
export MINIMAX_API_KEY="sk-..."
python3 scripts/gymbro_daily_image.py
```

Saves to `/home/work/.hermes/image_cache/gymbro_<YYYY-MM-DD>.png` and is served by the Flask app at `/img/<filename>`.

Suggested cron (06:00 HKT daily):

```cron
0 6 * * * cd /home/work/projects/gymbro && /usr/bin/env MINIMAX_API_KEY=sk-xxx /usr/bin/python3 scripts/gymbro_daily_image.py
```

## Port-forward / Tailscale-only

The app is **not** designed for public internet exposure. There is no auth layer. Bind it only to Tailscale (`100.x.x.x`) or a trusted LAN. If you want remote access from outside your tailnet, set up Tailscale on your iPhone first.

## Data location

All workout data lives in `/home/work/.whoop_workout_log.json`. Image cache lives in `/home/work/.hermes/image_cache/`. Both are git-ignored.

## Troubleshooting

- **`Address already in use`** — change `PORT` or kill the existing process: `lsof -i :7000`
- **PWA not installing** — must be served over HTTPS or localhost; Tailscale gives you HTTPS via `https://100.114.66.125:7000` automatically
- **Daily image missing** — check `MINIMAX_API_KEY` is set and `scripts/gymbro_daily_image.py` ran without error (logs go to stderr)