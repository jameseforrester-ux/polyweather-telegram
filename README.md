# PolyWX Telegram Bot

A single-user Telegram bot that tracks Polymarket **daily high-temperature** markets, builds a calibrated fair-value distribution from **HRRR** + **METAR**, compares against the live CLOB order book, and alerts when the leading bucket weakens or a pinned bucket is busted by observations.

Analysis only — no auto-trading.

## What it does

1. **Discovers** active high-temp events from the Polymarket Gamma API every 5 min, parses the resolution airport from each event description, and stores the bucket ranges + CLOB token IDs.
2. **Pulls METAR** observations every 10 min from `aviationweather.gov` for each airport with an active event.
3. **Pulls HRRR** forecasts every hour via Herbie (AWS S3). For each airport it grabs every forecast hour from the latest run that covers up to the airport-local midnight, finds the 2-m temp at the nearest grid point, and stores the max.
4. **Computes a blended fair value** every 10 min using the math below.
5. **Pulls CLOB order books** as part of fair-value rendering and computes per-bucket edge (`fair − best_ask`).
6. **Alerts** when the leader bucket's fair-prob drops sharply, when a pinned bucket gets busted by an observation, or when the floor of a pinned bucket becomes unreachable given current conditions.

## The math (in one paragraph)

For each event, we estimate today's daily max as a truncated normal with mean = `fair_max` and stdev = `sigma`. The mean blends two things by a time-of-day weight `w` that ramps from 0 at sunrise to 1 at peak heating hour:

```
obs_projection   = climo_daily_max + (obs_max_so_far − climo_max_reached_by_now)
fair_max         = w * obs_projection + (1−w) * HRRR_pred_max
fair_max         = max(fair_max, obs_max_so_far)         # floor at observation
sigma            = sqrt(historical_RMSE(lead_hrs)² + recent_HRRR_run_stdev²)
```

The truncated-normal lower bound is `obs_max_so_far` — the daily max can never be less than what's already been observed. As the day progresses: HRRR weight falls, the anomaly-persistence projection driven by METAR rises, and any bucket whose upper bound is below the observed max gets prob 0 automatically.

## Repository layout

```
polyweather-telegram/
├── Dockerfile, docker-compose.yml, requirements.txt, .env.example
├── config/airport_normals.yaml  # curated monthly normals (sparse — only for cities we've tuned)
├── src/
│   ├── main.py                  # entrypoint
│   ├── bot.py                   # Telegram handlers (commands + inline buttons)
│   ├── scheduler.py             # periodic jobs (HRRR/METAR/Polymarket/alerts)
│   ├── db.py                    # SQLite state
│   ├── ingest/
│   │   ├── climatology.py       # airportsdata lookup + diurnal model + parametric fallback
│   │   ├── metar.py             # aviationweather.gov fetcher
│   │   ├── hrrr.py              # Herbie HRRR fetcher
│   │   └── polymarket.py        # Gamma + CLOB; parses ICAO from Wunderground URL
│   ├── analytics/
│   │   ├── nowcast.py           # anomaly-persistence anchor blend
│   │   ├── hrrr_error.py        # combined sigma
│   │   ├── distribution.py      # truncated-normal over buckets
│   │   └── edge.py              # bucket views w/ asks
│   └── alerts/triggers.py       # leader drop / busted / floor unreachable
└── scripts/                     # (reserved for backtests/calibration)
```

## How airports are resolved

Polymarket's resolution airport is **not always the obvious one for a city**:

| City on Polymarket | Resolution airport |
|---|---|
| New York | **KLGA** (LaGuardia) |
| Los Angeles | KLAX |
| Chicago | KORD (O'Hare) |
| Miami | KMIA |
| Austin | KAUS |
| **Denver** | **KBKF** (Buckley Space Force Base — *not* Denver International) |
| **Houston** | **KHOU** (William P. Hobby — *not* Bush Intercontinental) |
| Seattle | KSEA |
| Atlanta | KATL |

The bot doesn't guess. Every Polymarket high-temp market description contains a Wunderground URL like `https://www.wunderground.com/history/daily/us/co/aurora/KBKF` — the ICAO is the last path component. `ingest/polymarket.py` extracts it via regex; that's the source of truth.

Airport metadata (lat/lon, timezone, name) comes from the `airportsdata` Python package, which covers every ICAO in the world (~28k airports). So when Polymarket adds a new city, the bot already knows where the station is — it just needs the airport code, which the URL provides.

Monthly climatological normals (used for the anomaly nowcast) come from `config/airport_normals.yaml`, hand-curated only for verified Polymarket cities. **For any airport not in that YAML, the bot falls back to a parametric latitude × month climatology** so new markets still work (with degraded accuracy) and the bot logs a warning so you can add real normals later.

Polymarket also runs international markets (London, Hong Kong, Buenos Aires, Cape Town). Those are **out of scope for this build** because HRRR is CONUS-only. Adding an ECMWF or GFS fallback for international markets is a separate module.

## Telegram commands

| Command | What it does |
|---|---|
| `/start` | Hello + menu |
| `/markets` | Inline list of active markets, tap to drill in |
| `/fair nyc` | Fair-value + edge table for first matching city |
| `/watches` | Your pinned buckets |
| `/status` | METAR + HRRR freshness per airport |

On a market detail screen, each bucket has a **Watch** / **Unwatch** toggle. Pinned buckets get individual drop / bust alerts pushed automatically.

---

## Deployment to a VPS via PuTTY

> Below assumes Ubuntu 22.04 / 24.04. Adapt `apt` lines for other distros.

### Step 0 — Two things you need first

1. **Telegram bot token.** On Telegram, message [`@BotFather`](https://t.me/BotFather):
   - `/newbot` → answer the prompts (give it a name and a unique `@whatever_bot` username).
   - BotFather replies with a token like `7891234567:AAH...long-string...`. Keep this private.
2. **Your Telegram chat_id.** Message [`@userinfobot`](https://t.me/userinfobot). It replies with `Id: 123456789`. That number is your `TELEGRAM_OWNER_CHAT_ID`.

Also: **start a chat with your new bot** (just open it and send `/start`). The bot can't message you until you've initiated a chat.

### Step 1 — Push this project to a GitHub repo

On your local machine (Windows: use Git Bash or PowerShell with git installed):

```bash
cd polyweather-telegram                 # the folder you got from this tarball
git init
git add .
git commit -m "initial commit"
# create an empty repo on github.com first (private is fine), then:
git remote add origin git@github.com:<YOUR_USERNAME>/<REPO_NAME>.git
# OR for HTTPS:
# git remote add origin https://github.com/<YOUR_USERNAME>/<REPO_NAME>.git
git branch -M main
git push -u origin main
```

If the repo is **private**, generate a GitHub Personal Access Token (Settings → Developer settings → Personal access tokens → Fine-grained tokens → grant `Contents: read` on this repo) — you'll paste this as the password when cloning on the VPS.

### Step 2 — Connect to your VPS with PuTTY

1. Open PuTTY.
2. **Host Name (or IP address):** your VPS IP.
3. **Port:** `22` (default).
4. **Connection type:** SSH.
5. Click **Open**. Accept the host-key fingerprint the first time.
6. Login as `root` or your sudo user.

### Step 3 — One-time VPS setup (Docker + git)

Run these in the PuTTY terminal (paste with right-click):

```bash
# As root, or prefix each with sudo:
apt update && apt -y upgrade
apt -y install ca-certificates curl gnupg git

# Install Docker Engine + compose plugin (official script)
curl -fsSL https://get.docker.com | sh
apt -y install docker-compose-plugin

# Verify
docker --version
docker compose version
```

### Step 4 — Clone the repo onto the VPS

```bash
cd /opt                                 # or wherever you want it
git clone https://github.com/<YOUR_USERNAME>/<REPO_NAME>.git polyweather-telegram
cd polyweather-telegram
```

If it's a private repo, git will prompt for username + password. Username = your GitHub username; password = the **Personal Access Token** from Step 1 (not your GitHub password).

### Step 5 — Configure the bot

```bash
cp .env.example .env
nano .env
```

In nano, set at minimum:

```
TELEGRAM_BOT_TOKEN=<paste from BotFather>
TELEGRAM_OWNER_CHAT_ID=<paste from userinfobot>
```

Leave everything else at defaults to start. Save with `Ctrl+O`, `Enter`, then `Ctrl+X`.

### Step 6 — Build and run

```bash
docker compose up -d --build
```

First build will take ~3–5 min (downloads python, herbie, scipy, eccodes). Then:

```bash
docker compose logs -f polywx-bot
```

You should see lines like `Polling Telegram...` and shortly after, `Polymarket ingest: N high-temp events upserted`. `Ctrl+C` to detach from the log stream (the container keeps running).

### Step 7 — Test from Telegram

Open your bot in Telegram. Send `/markets`. You should get the list (might be empty for a minute while the first poll completes — give it ~30 seconds after startup).

### Updating later

When you push new commits to GitHub from your local machine:

```bash
# on the VPS
cd /opt/polyweather-telegram
git pull
docker compose up -d --build
```

### Useful commands

```bash
# Tail logs
docker compose logs -f polywx-bot

# Restart
docker compose restart polywx-bot

# Stop entirely
docker compose down

# Inspect the SQLite state (read-only)
docker compose exec polywx-bot sqlite3 /data/polywx.db ".tables"
docker compose exec polywx-bot sqlite3 /data/polywx.db "SELECT icao, MAX(obs_time_utc), MAX(temp_f) FROM metar_obs GROUP BY icao;"
```

---

## Tuning knobs (in `.env`)

| Var | Default | Meaning |
|---|---|---|
| `HRRR_POLL_SECONDS` | 3600 | HRRR refetch cadence |
| `METAR_POLL_SECONDS` | 600 | METAR refetch cadence |
| `POLYMARKET_POLL_SECONDS` | 300 | Gamma re-discovery cadence |
| `RECOMPUTE_SECONDS` | 600 | Fair-value recompute + alert check cadence |
| `ALERT_BUCKET_PROB_DROP` | 0.15 | Absolute prob drop that triggers an alert |
| `ALERT_MIN_FAIR_FOR_WATCH` | 0.05 | Don't re-alert on a pinned bucket already near zero |

## Things to refine later

- The historical RMSE seed table in `analytics/hrrr_error.py` is from public verification stats; replace with a backtest against your own archived METAR / HRRR data when you have it.
- Monthly climo normals in `config/airport_normals.yaml` are approximate — refine against NCEI station normals for each verified airport.
- The METAR nowcast is pure anomaly-persistence (additive). Upgrade path: regression on (current temp, dew point, wind, cloud cover, solar elevation) → daily max. Hold for v2 once a backtest harness exists.
- Polymarket bucket parsing is regex-based; if Polymarket changes their question phrasing, update the regex in `ingest/polymarket.py`.
- International markets (London, Hong Kong, Buenos Aires, Cape Town) require an ECMWF or GFS ingestor since HRRR is CONUS-only. Drop a `src/ingest/gfs.py` alongside `hrrr.py` if/when you want to cover them.
