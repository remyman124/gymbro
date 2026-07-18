# 🏋️ gymbro — Jim's Gym Logging Web App

> Uber-inspired mobile-first gym set logger with daily motivation

A minimalist, dark-themed gym set tracker that runs as a Flask web service on Jim's home server, accessible over Tailscale VPN from an iPhone on the bench. Tap, log, pyramid view, done.

Built around three ideas:

1. **Zero friction on the bench** — one-thumb logging between sets, no app store, just a PWA in the browser.
2. **Pyramid-style set tracking** — auto-classify each set as warm-up / working / burn-out so volume and intensity stay visible.
3. **Daily motivation** — a fresh AI-generated image each morning, served alongside the streak counter.

---

## ✨ Features

- **Four-tab mobile UI** — Log · Pyramid · History · You, inspired by Uber's bottom nav
- **Pyramid view** — visual stack of sets per exercise with warm-up / working / burn-out color coding
- **PWA install** — add to iPhone home screen, full-screen, no Safari chrome, wake-lock enabled
- **Daily motivation image** — `MiniMax image-01` generated each morning, cached locally, served via `/img/`
- **Streak tracking** — consecutive workout day counter with rest-day grace
- **One-tap default reps = 10** — calibrated to Jim's training plan
- **Tailscale-only access** — bound to `100.114.66.125:7000`, no public exposure
- **JSON file persistence** — `/home/work/.whoop_workout_log.json`, human-readable, version-controllable

---

## 🧰 Tech stack

| Layer | Tech |
|---|---|
| Backend | Flask 3.1.3 (Python 3.13) |
| Frontend | Tailwind CSS (CDN) + Alpine.js 3 |
| Image gen | MiniMax `image-01` model |
| Persistence | Local JSON file (no DB) |
| Transport | HTTPS via Tailscale mesh VPN |
| Deployment | systemd-less — runs as Jim's dev process on home server |

No build step. No npm. No database. Single `gym_web.py` file (~650 lines).

---

## 🚀 Quick start

```bash
# Install deps
pip install -r requirements.txt

# Run the server (binds 0.0.0.0:7000)
python3 gym_web.py
```

Then open:

- **Local:** http://localhost:7000
- **Tailscale (iPhone on bench):** http://100.114.66.125:7000

To enable daily motivation image generation, set:

```bash
export MINIMAX_API_KEY="sk-..."
```

…and run `scripts/gymbro_daily_image.py` once per morning (cron @ 06:00 HKT recommended).

---

## 📸 Screenshot

![Demo](docs/demo.png)

> Drop a phone screenshot at `docs/demo.png` after first deploy.

---

## 📁 Project layout

```
gymbro/
├── gym_web.py                         # Flask app (single file)
├── scripts/
│   └── gymbro_daily_image.py          # MiniMax image-01 daily motivation gen
├── docs/
│   ├── setup.md                       # Detailed install + run
│   └── architecture.md                # API + data flow
├── .github/
│   └── workflows/
│       └── python-ci.yml              # Syntax check on push to main
├── requirements.txt                   # Flask==3.1.3
├── LICENSE                            # MIT
└── README.md                          # You are here
```

---

## 🤝 Contributing

This is Jim's personal gym logger. PRs welcome for:

- Better set-detection algorithms (e.g. RPE-based intensity)
- Additional exercise presets (squat / bench / deadlift ramp defaults)
- Workout-volume charts
- Apple Health / Whoop integration

For everything else, fork it.

---

## 📄 License

MIT © Jim — see [LICENSE](LICENSE).