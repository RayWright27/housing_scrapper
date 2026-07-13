# CLAUDE.md — Realty Tracker

> Instructions for Claude Code working in this repository.
> Read this fully at the start of every session before editing anything.

## 1. What this project is

A personal tool that tracks the **prices of a fixed list of real-estate listings**
on **CIAN** and **Avito** (Russian platforms) and notifies the owner when a
tracked listing's price changes.

Scope is deliberately small:
- We watch a **known set of listing URLs** (and optionally saved-search URLs),
  not the entire site catalog.
- We run at **low frequency** (a few times per day), not continuously.
- Single user, single machine. No multi-tenant, no public API, no scale concerns.

This is a *price history + change notification* system. The scraping is a means
to an end; the valuable, durable part is the stored price history and the
change-detection logic.

## 2. Core architectural principle (read this twice)

**Isolate the volatile from the stable.**

Scrapers break — sites change markup and tighten anti-bot defenses. Storage,
normalization, change detection, and notifications do not. The codebase MUST be
layered so that when a scraper breaks, only one adapter file changes and nothing
else is touched.

Concretely:
- All site-specific knowledge lives **only** inside `src/adapters/<site>.py`.
- Everything downstream of normalization works on a single site-agnostic schema
  and must never import or reference anything site-specific.
- If you find yourself putting a CIAN- or Avito-specific branch outside
  `src/adapters/`, that is a design error — stop and reconsider.

## 3. Tech stack

- **Python 3.11+**
- **Playwright** (Chromium) for rendering / anti-bot-tolerant fetching.
  Prefer a reverse-engineered JSON endpoint when one is stable; fall back to
  Playwright when not.
- **SQLite** via the stdlib `sqlite3` (no ORM needed at this scale; keep it
  transparent). SQLAlchemy is acceptable only if explicitly requested.
- **FastAPI** + **uvicorn** for the local read/manage web dashboard (§9).
- **Chart.js** (loaded from CDN in the static page) for the price chart.
  **Leaflet** + OpenStreetMap tiles (also CDN, no API key) for the map panel.
  No frontend build step — one static HTML page, vanilla JS.
- **Telegram Bot API** for push notifications on price changes — called with the
  stdlib `urllib` (no `python-telegram-bot` dependency; the need is a single
  `sendMessage` POST, so a library does not earn its place).
- **In-process scheduling** via a plain stdlib loop in `main.py` (`run` command) —
  a fixed interval is all we need, so no APScheduler dependency.
- **pytest** for tests.
- **python-dotenv** for config/secrets.

Two presentation surfaces with distinct jobs: the **web dashboard** is for
viewing history and managing the tracked list; **Telegram** is push-only (it
pings you when a price changes). Neither contains scraping or business logic —
both are thin consumers of `repository`.

Do not add dependencies casually. Each new dependency must earn its place;
propose it and explain why before adding it to `requirements.txt`.

## 4. Directory layout

```
realty-tracker/
├── CLAUDE.md              # this file
├── README.md
├── .gitignore
├── .env.example          # committed; documents required env vars
├── .env                  # NEVER committed; real secrets
├── requirements.txt
├── config.py             # loads env, exposes typed settings
├── src/
│   ├── adapters/
│   │   ├── base.py       # RawListing dataclass + SiteAdapter protocol
│   │   ├── cian.py       # CIAN adapter (build this first)
│   │   └── avito.py      # Avito adapter (build this LAST)
│   ├── normalize.py      # RawListing -> Listing (site-agnostic schema)
│   ├── storage/
│   │   ├── schema.sql    # canonical DDL
│   │   ├── db.py         # connection, migration/bootstrap
│   │   └── repository.py # upsert_listing, record_price, last_price, ...
│   ├── tracker.py        # orchestration: fetch -> normalize -> diff -> notify
│   ├── notify/
│   │   ├── telegram.py   # DB-free formatter + sender + outbox port
│   │   └── sink.py       # composition glue: DB-backed outbox + get_meta seam
│   ├── web/
│   │   ├── app.py        # FastAPI app: JSON API over repository + add/remove
│   │   └── static/
│   │       └── index.html  # single-page dashboard (vanilla JS + Chart.js CDN)
│   └── main.py           # entrypoint / CLI (incl. `run` scheduler loop)
├── tests/
│   ├── fixtures/         # saved raw payloads/HTML for offline adapter tests
│   ├── test_storage.py
│   ├── test_change_detection.py
│   └── test_normalize.py
└── data/
    └── tracker.db        # the SQLite file; gitignored
```

## 5. Data model

Three core tables (below), plus operational tables — `source_listings`
(source↔listing links + the consecutive-miss counter that drives delisting,
§8.7), `pending_notifications` (the Telegram outbox, §9b), `app_meta` (a small
key/value store for the scheduler heartbeat + last-backup timestamp),
`listing_targets` (a user-set price target per listing — display context for
notifications and the dashboard, never a change-detection input) and
`listing_links` (a user assertion that several rows are the SAME physical flat
published on different sites — each row keeps its own history; the dashboard
only shows the partner's price next to a row for comparison). Treat
`schema.sql` as the single source of truth for DDL.

**`tracked_sources`** — the fixed list of things to watch.
| column      | type    | notes                                  |
|-------------|---------|----------------------------------------|
| id          | INTEGER | PK                                     |
| source      | TEXT    | 'cian' \| 'avito'                      |
| url         | TEXT    | listing URL (or saved-search URL)      |
| kind        | TEXT    | 'listing' \| 'search'                  |
| note        | TEXT    | human label                            |
| active      | INTEGER | 1/0                                    |

**`listings`** — one row per discovered real-estate object.
| column         | type    | notes                               |
|----------------|---------|-------------------------------------|
| id             | INTEGER | PK (internal)                       |
| source         | TEXT    | 'cian' \| 'avito'                   |
| external_id    | TEXT    | the site's own listing id           |
| url            | TEXT    |                                     |
| title          | TEXT    |                                     |
| address        | TEXT    |                                     |
| rooms          | INTEGER | nullable (studio = 0)               |
| area_total     | REAL    | m², nullable                        |
| area_living    | REAL    | nullable                            |
| area_kitchen   | REAL    | nullable                            |
| floor          | INTEGER | nullable                            |
| floors_total   | INTEGER | nullable                            |
| lat            | REAL    | WGS-84, nullable (from site payload)|
| lon            | REAL    | nullable; upsert keeps old on NULL  |
| first_seen_at  | TEXT    | ISO-8601 UTC                        |
| last_seen_at   | TEXT    | ISO-8601 UTC                        |
| is_active      | INTEGER | 0 once it disappears from the site  |
| raw_json       | TEXT    | last normalized payload (debug)     |

`UNIQUE(source, external_id)` — this is the identity key for upserts.

**`price_history`** — append-only; the heart of the system.
| column       | type    | notes                    |
|--------------|---------|--------------------------|
| id           | INTEGER | PK                       |
| listing_id   | INTEGER | FK -> listings.id        |
| price        | INTEGER | in rubles, no decimals   |
| currency     | TEXT    | default 'RUB'            |
| observed_at  | TEXT    | ISO-8601 UTC             |

Index on `(listing_id, observed_at)`. **Never UPDATE a price row — only INSERT.**
A price "change" is the difference between the newest observation and the
previous newest for that listing.

**`pending_notifications`** — the Telegram outbox (§9b). Transient operational
state, not durable history. A message that fails to send (e.g. Telegram
unreachable, no VPN) is queued here as fully-rendered text and replayed, oldest
first, the next time a send succeeds. **One row per (message, recipient)**: a
message with several recipients is queued only for the chats it actually failed
to reach, so a replay never re-sends to a recipient that already got it (no
duplicate on partial delivery).
| column          | type    | notes                                  |
|-----------------|---------|----------------------------------------|
| id              | INTEGER | PK                                     |
| chat_id         | TEXT    | the single recipient this row is for   |
| text            | TEXT    | fully-rendered message body            |
| created_at      | TEXT    | ISO-8601 UTC (when queued)             |
| attempts        | INTEGER | delivery attempts so far               |
| last_attempt_at | TEXT    | ISO-8601 UTC, nullable                 |
| last_error      | TEXT    | exception type name (redacted)         |

## 6. Adapter contract

Every adapter implements the same shape so the rest of the system is uniform.

```python
# src/adapters/base.py
from dataclasses import dataclass
from typing import Protocol

@dataclass
class RawListing:
    source: str
    external_id: str
    url: str
    price: int               # rubles
    title: str | None = None
    address: str | None = None
    rooms: int | None = None
    area_total: float | None = None
    floor: int | None = None
    floors_total: int | None = None
    lat: float | None = None   # WGS-84 coordinates when the payload has them
    lon: float | None = None
    extra: dict | None = None  # anything site-specific, stays opaque downstream

class SiteAdapter(Protocol):
    source: str
    def fetch_listing(self, url: str) -> RawListing | None: ...
    def fetch_search(self, url: str) -> list[RawListing]: ...
```

Rules:
- An adapter returns `None` (single) or `[]` (search) on a soft failure
  (blocked, captcha, transient) — it must NOT crash the whole run.
- An adapter NEVER writes to the DB and NEVER sends notifications. It only fetches
  and parses. Side effects live in `tracker.py`.
- Parsing must be defensive: missing optional fields are `None`, not exceptions.
  Only a missing `price` or `external_id` is a hard parse failure for that item.

## 7. Scraping etiquette (mandatory, non-negotiable)

This is a polite, low-volume personal client, not a mass scraper. Behave like one:
- **Low frequency.** Default interval is several hours. Never poll in a tight loop.
- One persistent browser context per run; reuse cookies across the run.
- Randomized small delays between requests within a run.
- Realistic, stable User-Agent and headers.
- On detecting a block/captcha: back off, log it, skip — do **not** retry-hammer.
- Respect a configurable global rate limit in `config.py`.
- No attempt to break captchas or defeat security in code. If a site hard-blocks,
  we surface that to the user and stop; we do not escalate evasion.

CIAN first (easier, often has a JSON endpoint). Avito LAST (hardest defenses).

## 8. Change detection (the core logic)

In `tracker.py`, per tracked source:
1. Fetch via the right adapter -> `RawListing`(s).
2. Normalize -> upsert into `listings` (update `last_seen_at`, `is_active=1`).
3. Read `last_price(listing_id)` from `price_history`.
4. If no prior price -> INSERT first observation, emit a "now tracking" event.
5. If new price != last price -> INSERT new observation, emit a "price changed"
   event carrying old, new, delta, and percent.
6. If new price == last price -> do nothing (no row, no noise).
7. Listings in `tracked_sources` that were not seen this run -> mark
   `is_active=0` after N consecutive misses (configurable), emit "delisted".
   A **soft block** (captcha / anti-bot / timeout) is NOT a miss: the adapter
   raises `SiteBlocked` and the tracker skips that source for the run without
   penalising it — otherwise a few blocked runs would falsely delist a live
   listing. Only a confirmed absence (a 404 → `None`/`[]` sentinel) counts.

Change detection must be fully unit-testable on fake data with no network and no
real DB file (use an in-memory SQLite). This is the part we trust most, so it
gets the most tests.

## 9. Presentation layer

Two thin consumers of `repository`. Neither ever scrapes, normalizes, or contains
business logic — they read stored state and render it. The only write either is
allowed is adding/deactivating rows in `tracked_sources` (see below). This keeps
§2 intact: presentation can be rebuilt or replaced without touching the core.

### 9a. Web dashboard (local, primary UI)

A single-page FastAPI app on `localhost`, started with `serve` (§11). The backend
exposes a small read-only JSON API plus two write endpoints; the frontend is one
static `index.html` with vanilla JS and Chart.js from CDN. No auth (loopback
only), no frontend build step.

Layout — three stacked regions on one page:

1. **Summary strip** — small metric cards: total tracked, active, delisted,
   number of price changes in the last 7 days. The header also shows a
   **scheduler status pill** (`GET /api/scheduler`): a health dot (green ok /
   amber overdue / red error) plus "ran … · next in …", kept live by a timer, so
   the background/autostarted scheduler is visibly alive and trustworthy.

2. **Tracking section** (the add/manage surface):
   - A text input for a listing URL + an optional note field + an "add" button.
     On submit, the backend detects the source from the URL host (cian.ru →
     `cian`, avito.ru → `avito`), inserts into `tracked_sources`, and triggers
     **one immediate fetch** for that source so the user sees data at once
     instead of waiting for the next scheduled pass. The events that fetch
     produces are handed to the notifier (via an injected seam built from
     `src.notify.sink`), so a newly-tracked listing pings Telegram just like a
     `run-once` pass — respecting `NOTIFY_ON_NEW`, and queueing to the outbox if
     offline. The web layer never formats or sends anything itself; it only
     calls the seam. Reject unknown hosts with a clear message.
   - A table of all tracked objects, one row each, columns:
     source badge · object summary (rooms · area · floor · note) · current price
     · **₽/m²** · **target** (a per-listing price target you set inline; shows the
     target + % above/below it, green when at/below) · **Δ total** (% from first
     observed price) · **days since last price change** · **last checked**
     (relative `last_seen_at` + a freshness dot) · remove (×). Delisted rows
     render dimmed but stay visible. After a
     `Refresh`, rows carry a transient badge — updated / unchanged / not checked
     — computed client-side by diffing each row's price and `last_seen_at`
     against the pre-refresh snapshot; the badges clear on the next reload.

3. **Chart section** (configurable price history):
   - Listing selector: multi-select chips to overlay several objects on one chart.
   - **Map panel** beside the chart (Leaflet + OpenStreetMap tiles from CDN, no
     API key): a pin for each listing currently selected for the chart, in the
     same series colour; popup carries the object summary, current price, ₽/m²,
     target (if set) and a link. Coordinates come from the adapters' payloads
     (`listings.lat/lon`) — the dashboard never geocodes; a selected listing
     without stored coordinates is counted in a small hint and appears after its
     next successful fetch.
   - Time range: all / 90d / 30d.
   - Y-axis mode: `₽` (absolute) / `₽/m²` (size-normalized) / `% from start`
     (each series rebased to its first observation — lets objects of different
     price scales be compared on one axis).
   - The line MUST use **step interpolation** (`stepped: true` / stepAfter).
     Price is piecewise-constant: it holds flat, then jumps at an observation.
     Straight diagonal interpolation would imply gradual change that never
     happened and is a correctness bug, not a style choice. Mark the observation
     points where the price actually changed.

4. **Detail panel** (below the chart, for the selected object): current price,
   min/max over the period, number of price changes, days tracked, and a link
   back to the original listing.

API shape (keep it this simple):
- `GET /api/listings` — all tracked objects with computed fields (current price,
  ₽/m², Δ total, days since change, status).
- `GET /api/listings/{id}/history?range=all|90d|30d` — price observations for the
  chart.
- `GET /api/summary` — the metric-strip numbers.
- `GET /api/scheduler` — the background scheduler's heartbeat (last/next run,
  last status/error, event count) from `app_meta`; feeds a header status pill
  (health dot + relative times) so the autostarted scheduler is observable. This
  is a read of operational state, not business data.
- `POST /api/tracked` — body `{url, note}`; detect source, insert, fetch once,
  notify (via the injected seam) on what the fetch found.
- `PUT /api/listings/{id}/target` — body `{target_price}`; set a per-listing
  price target (rubles). `DELETE` clears it. A target is display CONTEXT only —
  it enriches the price-change notification and the dashboard row with
  distance-to-target; it does NOT gate change detection or which events notify.
- `PUT /api/listings/{id}/link` — body `{other_id}`; assert the two rows are the
  same physical flat on different sites (the CIAN/Avito double-listing case).
  `DELETE` unlinks. Display context only: each row keeps its own price history;
  the row gains the partner's current price + gap. Linking rows that already
  belong to groups merges the groups; a group left with one member dissolves.
- `DELETE /api/tracked/{id}` — deactivate (set `active=0`); never hard-delete
  history.
- `POST /api/refresh` — run one CIAN pass now (like `run-once`) and notify on
  what it finds; also launch the interactive Avito grab (`avito-grab.ps1`) in a
  separate console, since that step needs a human to load tabs / solve a
  challenge and cannot run headlessly (§7). The launcher is an injected seam
  (a no-op in tests), like `notify` — the web layer itself spawns nothing.

Computed fields (₽/m², Δ total, medians, days-since-change) are derived in the
API/repository layer from `listings` + `price_history` — they are NOT stored
columns. Round money to whole rubles and ratios to sensible precision before
sending to the client.

### 9b. Telegram (push notifications)

Unchanged in role: on a "price changed" or "delisted" event from `tracker.py`,
send a short message (old → new, delta, percent, link). It does not serve the
history UI; the dashboard does. Telegram stays optional — if no token is
configured, the system runs dashboard-only without error.

Delivery is best-effort but does not lose messages: a failed send (Telegram
unreachable) is stored in the `pending_notifications` outbox and replayed,
oldest first, at the start of the next `notify` (i.e. the next run-once / grab /
ingest). The outbox is a DB-free injected port on the notifier — the concrete,
DB-backed implementation is built at the CLI seam in `main.py`, so the Telegram
layer keeps no DB access (§2). This stays within §9's "add/deactivate
`tracked_sources` is the only write" spirit: the outbox is the notifier's own
delivery bookkeeping, not business state.

## 10. Configuration & secrets

- All secrets and tunables come from environment via `config.py` (loaded from
  `.env` with python-dotenv).
- `.env.example` lists every required key with placeholder values and is committed.
- `.env` holds real values and is **gitignored**. NEVER write real secrets into
  any committed file, test, or log line.
- Required keys: `DB_PATH`, `POLL_INTERVAL_HOURS` (the `run` scheduler's CIAN
  interval), `WEB_HOST` (default `127.0.0.1`), `WEB_PORT` (default `8000`), and
  proxy vars if used (`PROXY_URL` or empty).
- Operational tunables (all have defaults): `DB_BACKUP_KEEP` (timestamped DB
  snapshots kept in `data/backups`, made at startup and then at most once/24h
  while the scheduler runs — not just once per launch), `SCHEDULER_IN_SERVE`
  (when `serve` runs, also run the CIAN scheduler on a background thread so one
  process gives both the dashboard and periodic scraping; `0` = dashboard only),
  `CIAN_HEADLESS` (CIAN fetches run headless regardless of `BROWSER_HEADLESS`,
  which stays headful for the Avito grab/warmup), and `AVITO_GRAB_LOAD_TIME`
  (seconds the grab helper waits for tabs before auto-reading them).
- Optional keys: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — if either is absent,
  Telegram notifications are disabled and the rest still runs. `TELEGRAM_CHAT_ID`
  may list **several** recipients, comma-separated; every id receives each
  message (a bot reaches only chats it is told about, so each recipient must
  message the bot once — use the `chat-ids` command to discover their id).
- Never log token values, proxy credentials, or full cookies. Redact in logs.

## 11. Commands

```bash
# one-time setup
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# initialize the database (creates data/tracker.db from schema.sql)
python -m src.main init-db

# add something to track (CLI; the dashboard does the same via the UI)
python -m src.main add --source cian --url "https://..." --note "2-room near park"

# run a single tracking pass now
python -m src.main run-once

# run the scheduler (long-running)
python -m src.main run

# start the local web dashboard at http://WEB_HOST:WEB_PORT
# (also runs the CIAN scheduler on a background thread unless SCHEDULER_IN_SERVE=0)
python -m src.main serve

# start the dashboard + scheduler automatically at Windows logon (one process).
# Registers a Scheduled Task; review it first. Remove with uninstall-autostart.ps1.
powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1

# discover recipient chat ids (each person messages the bot first, then run this)
python -m src.main chat-ids

# tests
pytest -q
pytest tests/test_change_detection.py -q   # the critical suite
```

Keep these commands working. If you change the CLI surface, update this section
in the same commit.

## 12. Coding conventions

- Type hints everywhere. Run with `python -m mypy src` cleanly if mypy is present.
- Small, single-responsibility functions. No god-modules.
- All timestamps stored as ISO-8601 UTC strings; convert at the edges only.
- No bare `except:`; catch specific exceptions. Adapters may catch broadly but
  must log and return the soft-failure sentinel, never swallow silently.
- Prices are integers in rubles. No floats for money.
- Logging via the stdlib `logging` module, not `print`, outside the CLI.

## 13. Testing approach

- `test_change_detection.py` and `test_storage.py` use **in-memory SQLite** and
  fabricated data — fast, deterministic, no network.
- Adapter tests are **offline**: save a real raw payload/HTML once into
  `tests/fixtures/` and parse that fixture. Do NOT hit the live network in tests.
- When you fix an adapter after a site change, capture a fresh fixture and add a
  regression test for the field that broke.
- Computed dashboard fields (₽/m², Δ total, days-since-change, min/max) are
  derived logic — unit-test them on fabricated `price_history` rows in-memory.
  The web layer itself stays thin enough to need little testing.

## 14. Git conventions

- `main` stays runnable. Build features on short-lived branches.
- Conventional commit subjects, imperative mood, ≤72 chars:
  `feat: add CIAN listing adapter`, `fix: handle missing kitchen area`,
  `test: cover delisting after N misses`.
- One logical change per commit. Never commit `.env`, `data/`, or fixtures
  containing personal data.
- Before committing, run `pytest -q` and stop if it fails.

## 15. Hard "do NOT" list

- Do NOT put site-specific logic anywhere outside `src/adapters/`.
- Do NOT UPDATE rows in `price_history`; it is append-only.
- Do NOT put business logic, scraping, or DB writes (other than the
  `tracked_sources` add/deactivate) in the web or Telegram layer.
- Do NOT store computed values (₽/m², deltas) as columns — derive them.
- Do NOT bind the dashboard to a non-loopback host or add it to CI; it is a
  local, no-auth tool. `WEB_HOST` stays `127.0.0.1` unless the user changes it.
- Do NOT interpolate the price chart with straight diagonals — step lines only.
- Do NOT commit secrets, the SQLite DB, or `.venv/`.
- Do NOT add retry storms or captcha-breaking; respect §7.
- Do NOT run the scraper from CI / GitHub Actions (datacenter IPs get blocked
  instantly and it violates the sites' terms). Scraping runs locally only.
- Do NOT add dependencies without proposing them first.
- Do NOT make a real network request inside a unit test.

## 16. Build order (for reference)

1. storage + schema + change-detection tests
2. normalize.py + adapter base/protocol
3. CIAN adapter (against your own real URLs)
4. wire tracker.py end-to-end on CIAN
5. Telegram notifications
6. web dashboard — API over `repository`, then the static page (table, then
   chart). Build it on CIAN data while it is the only working source.
7. scheduler
8. Avito adapter (last — expect the most iteration here)

When in doubt about scope or a tradeoff, prefer the smallest change that keeps
§2 intact, and ask before doing anything large or destructive.
