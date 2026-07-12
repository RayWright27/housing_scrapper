# Realty Tracker

Personal tool that tracks the prices of a fixed list of **CIAN** and **Avito**
real-estate listings and notifies you (Telegram) when a tracked price changes.
It stores a full price history in SQLite and shows it on a local web dashboard.

The full design/architecture spec lives in [CLAUDE.md](CLAUDE.md); this README is
just how to get it running.

## Requirements

- **Python 3.11+** (on Windows, install from python.org and tick *"Add to PATH"*).
- A Chromium browser downloaded by Playwright (one command below).

## Setup on a fresh machine

> The `venv/` folder is machine-specific — never copy it between computers.
> Always recreate the virtual environment locally.

```powershell
# 1. Create and activate a virtual environment (from the project root)
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install the Chromium browser used for fetching (one-time, ~150 MB)
playwright install chromium

# 4. Configuration — copy the template and fill in real values
copy .env.example .env            # macOS/Linux: cp .env.example .env
#   Edit .env: set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (optional), proxy, etc.

# 5. Create the database (skip if you carried over data/tracker.db)
python -m src.main init-db

# 6. Sanity check
pytest -q
```

### What must be carried over when moving machines

These are gitignored, so a fresh `git clone` will **not** include them — copy
them by hand if you want to keep your data/config:

- **`.env`** — your real secrets and settings.
- **`data/tracker.db`** — the price history (the valuable part). Without it you
  start empty via `init-db`.
- **`data/browser-profile/`**, **`data/chrome-grab/`** — browser profiles with
  cookies for the anti-bot (Avito) flow. Optional; regenerated on demand.

## Everyday commands

```powershell
python -m src.main list           # show the watchlist
python -m src.main add --source cian --url "https://..." --note "2-room"
python -m src.main remove --id 3  # deactivate a tracked source
python -m src.main run-once       # one tracking pass now (fetch -> diff -> notify)
python -m src.main run            # scheduler: a CIAN pass now, then every N hours
python -m src.main serve          # dashboard at http://127.0.0.1:8000 + scheduler
```

`serve` also runs the CIAN scheduler on a background thread, so one process gives
both the dashboard and periodic scraping (set `SCHEDULER_IN_SERVE=0` for
dashboard-only). The dashboard header shows a scheduler status pill so you can
see it is alive. Avito still needs a human to load its tabs and is not scheduled
(use the dashboard **Refresh** button, which opens the interactive grab).

### Start automatically at logon

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1   # register
powershell -ExecutionPolicy Bypass -File scripts\uninstall-autostart.ps1 # remove
```

This registers a Windows Scheduled Task that runs `serve` at logon (dashboard +
background scheduler). It changes a Windows setting — review the script first.

Other subcommands: `fetch`, `capture`, `ingest`, `grab`, `warmup`, `urls`,
`notify-test`. See `python -m src.main --help` and CLAUDE.md §11 for details.

Configuration keys are documented in [.env.example](.env.example).
