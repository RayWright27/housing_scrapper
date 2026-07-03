-- Realty Tracker canonical DDL (CLAUDE.md §5).
-- Single source of truth for the schema. Idempotent: safe to run repeatedly.
-- Money is stored as INTEGER rubles. price_history is append-only.

-- The fixed list of things to watch.
CREATE TABLE IF NOT EXISTS tracked_sources (
    id      INTEGER PRIMARY KEY,
    source  TEXT    NOT NULL,              -- 'cian' | 'avito'
    url     TEXT    NOT NULL,              -- listing URL or saved-search URL
    kind    TEXT    NOT NULL,              -- 'listing' | 'search'
    note    TEXT,                          -- human label
    active  INTEGER NOT NULL DEFAULT 1     -- 1/0
);

-- One row per discovered real-estate object.
CREATE TABLE IF NOT EXISTS listings (
    id             INTEGER PRIMARY KEY,
    source         TEXT    NOT NULL,        -- 'cian' | 'avito'
    external_id    TEXT    NOT NULL,        -- the site's own listing id
    url            TEXT    NOT NULL,
    title          TEXT,
    address        TEXT,
    rooms          INTEGER,                 -- nullable (studio = 0)
    area_total     REAL,                    -- m², nullable
    area_living    REAL,
    area_kitchen   REAL,
    floor          INTEGER,
    floors_total   INTEGER,
    first_seen_at  TEXT    NOT NULL,        -- ISO-8601 UTC
    last_seen_at   TEXT    NOT NULL,        -- ISO-8601 UTC
    is_active      INTEGER NOT NULL DEFAULT 1,
    raw_json       TEXT,                    -- last normalized payload (debug)
    UNIQUE (source, external_id)            -- identity key for upserts
);

-- Append-only price observations; the heart of the system.
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY,
    listing_id  INTEGER NOT NULL REFERENCES listings (id),
    price       INTEGER NOT NULL,           -- rubles, no decimals
    currency    TEXT    NOT NULL DEFAULT 'RUB',
    observed_at TEXT    NOT NULL            -- ISO-8601 UTC
);

CREATE INDEX IF NOT EXISTS idx_price_history_listing_observed
    ON price_history (listing_id, observed_at);

-- Which listing each tracked_source produced, plus the consecutive-miss counter
-- that drives delisting (§8.7). A search source links to many listings; a
-- listing source to one. Operational state (like listings.is_active), not a
-- derived value. A pair goes is_linked=0 once delisted from that source.
CREATE TABLE IF NOT EXISTS source_listings (
    tracked_source_id  INTEGER NOT NULL REFERENCES tracked_sources (id),
    listing_id         INTEGER NOT NULL REFERENCES listings (id),
    consecutive_misses INTEGER NOT NULL DEFAULT 0,
    is_linked          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tracked_source_id, listing_id)
);
