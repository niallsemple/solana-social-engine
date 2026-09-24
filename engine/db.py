"""Persistent storage. SQLite for local-first operation; schema is PostgreSQL-compatible
(no SQLite-only types) so it can be migrated later. Raw records are never overwritten by
derived statistics; derived tables are materialized snapshots marked as such."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    mint                TEXT PRIMARY KEY,
    symbol              TEXT,
    name                TEXT,
    first_seen_ts       REAL NOT NULL,        -- system detection time of the contract (T0 anchor)
    token_created_ts    REAL,                 -- on-chain creation / token age anchor, if known
    launch_platform     TEXT,                 -- pump.fun / raydium / unknown
    status              TEXT DEFAULT 'ACTIVE',-- ACTIVE / DEAD / RUGGED / INVALID
    created_at          REAL NOT NULL,
    source              TEXT,
    quality_status      TEXT DEFAULT 'OK'
);

CREATE TABLE IF NOT EXISTS token_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    mint                    TEXT NOT NULL REFERENCES tokens(mint),
    first_detection_ts      REAL NOT NULL,
    total_x_mentions        INTEGER DEFAULT 0,
    total_tg_mentions       INTEGER DEFAULT 0,
    unique_x_accounts       INTEGER DEFAULT 0,
    unique_tg_channels      INTEGER DEFAULT 0,
    mention_velocity        REAL,             -- mentions/min over the push
    mention_acceleration    REAL,
    peak_social_ts          REAL,
    peak_social_mpm         REAL,
    price_at_first_detection REAL,
    price_at_social_peak    REAL,
    peak_price              REAL,
    peak_price_ts           REAL,
    social_push_start       REAL,
    social_push_peak        REAL,
    social_push_end         REAL,
    total_push_duration     REAL,
    current_state           TEXT DEFAULT 'ISOLATED',
    created_at              REAL NOT NULL,
    quality_status          TEXT DEFAULT 'OK'
);

CREATE TABLE IF NOT EXISTS x_accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    handle          TEXT UNIQUE NOT NULL,
    x_user_id       TEXT,
    followers       INTEGER,
    first_seen_ts   REAL NOT NULL,
    discovered_via  TEXT,
    research_weight REAL DEFAULT 1.0,
    created_at      REAL NOT NULL,
    quality_status  TEXT DEFAULT 'OK'
);

CREATE TABLE IF NOT EXISTS telegram_channels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    handle          TEXT UNIQUE NOT NULL,     -- @channel or t.me/slug
    tg_id           TEXT,
    members         INTEGER,
    first_seen_ts   REAL NOT NULL,
    discovered_via  TEXT,
    research_weight REAL DEFAULT 1.0,
    created_at      REAL NOT NULL,
    quality_status  TEXT DEFAULT 'OK'
);

CREATE TABLE IF NOT EXISTS social_posts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    platform            TEXT NOT NULL,        -- 'x' | 'telegram'
    external_id         TEXT,                 -- platform-native post id
    author_ref          TEXT NOT NULL,        -- handle of account/channel
    text                TEXT,
    original_ts         REAL,                 -- timestamp claimed by the source platform
    system_ts           REAL NOT NULL,        -- when OUR system saw it (T0 anchor)
    urls_json           TEXT,
    tickers_json        TEXT,
    cas_json              TEXT,               -- candidate + validated CAs extracted
    is_repost           INTEGER DEFAULT 0,
    is_quote            INTEGER DEFAULT 0,
    is_copy             INTEGER DEFAULT 0,    -- near-duplicate of an earlier post
    originality_score   REAL,
    likes               INTEGER,
    reposts             INTEGER,
    replies             INTEGER,
    views               INTEGER,
    raw_json            TEXT,
    created_at          REAL NOT NULL,
    source              TEXT,
    quality_status      TEXT DEFAULT 'OK',
    UNIQUE(platform, external_id)
);
CREATE INDEX IF NOT EXISTS idx_posts_ts ON social_posts(system_ts);
CREATE INDEX IF NOT EXISTS idx_posts_author ON social_posts(author_ref);

CREATE TABLE IF NOT EXISTS call_events (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    mint                        TEXT NOT NULL REFERENCES tokens(mint),
    token_event_id              INTEGER REFERENCES token_events(id),
    post_id                     INTEGER REFERENCES social_posts(id),
    platform                    TEXT NOT NULL,
    account_ref                 TEXT NOT NULL,
    original_post_ts            REAL,
    system_detection_ts         REAL NOT NULL,
    price_capture_ts            REAL,
    detection_latency_ms        REAL,
    entry_price                 REAL,
    entry_error                 TEXT,          -- HELIUS_* / NO_LIQUID_MARKET / ... / NULL
    position_in_cycle           INTEGER,       -- 1 = first caller of this token
    seconds_after_first_detection REAL,
    price_before_ts             REAL,          -- trend-before anchor
    price_before                REAL,
    gross_return_1m             REAL,
    gross_return_5m             REAL,
    gross_return_15m            REAL,
    gross_return_30m            REAL,
    gross_return_60m            REAL,
    net_return_1m               REAL,
    net_return_5m               REAL,
    net_return_15m              REAL,
    net_return_30m              REAL,
    net_return_60m              REAL,
    highest_price_15m           REAL,
    lowest_price_15m            REAL,
    mfe_15m                     REAL,
    mae_15m                     REAL,
    time_to_peak_15m            REAL,
    time_to_trough_15m          REAL,
    highest_price_60m           REAL,
    lowest_price_60m            REAL,
    mfe_60m                     REAL,
    mae_60m                     REAL,
    time_to_peak_60m            REAL,
    time_to_trough_60m          REAL,
    callers_before_peak         INTEGER,
    classification_gross        TEXT,          -- WIN/LOSS/FLAT/UNPRICEABLE/DEAD/RUGGED/INVALID
    classification_net          TEXT,
    cost_model_id               INTEGER REFERENCES cost_models(id),
    monitoring_complete         INTEGER DEFAULT 0,
    created_at                  REAL NOT NULL,
    quality_status              TEXT DEFAULT 'OK'
);
CREATE INDEX IF NOT EXISTS idx_calls_mint ON call_events(mint);
CREATE INDEX IF NOT EXISTS idx_calls_account ON call_events(account_ref);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON call_events(system_detection_ts);

CREATE TABLE IF NOT EXISTS price_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mint            TEXT NOT NULL,
    call_event_id   INTEGER REFERENCES call_events(id),
    ts              REAL NOT NULL,             -- price capture timestamp
    horizon_label   TEXT,                      -- T0 / T+1m / T+5m / T+15m / T+30m / T+60m / HF / EXT
    price_usd       REAL,
    sol_price_usd   REAL,
    mcap_usd        REAL,
    liquidity_usd   REAL,
    supply          REAL,
    holders         INTEGER,
    pool            TEXT,
    source          TEXT,                      -- helius / jupiter-via-helius / simulated / ...
    error_code      TEXT,                      -- NULL on success, HELIUS_* etc. on failure
    created_at      REAL NOT NULL,
    quality_status  TEXT DEFAULT 'OK'
);
CREATE INDEX IF NOT EXISTS idx_prices_mint_ts ON price_observations(mint, ts);
CREATE INDEX IF NOT EXISTS idx_prices_call ON price_observations(call_event_id);

CREATE TABLE IF NOT EXISTS social_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,             -- search_term / channel / account / url
    value           TEXT NOT NULL,
    discovered_via  TEXT,
    first_seen_ts   REAL NOT NULL,
    last_seen_ts    REAL NOT NULL,
    status          TEXT DEFAULT 'ACTIVE',
    created_at      REAL NOT NULL,
    UNIQUE(kind, value)
);

CREATE TABLE IF NOT EXISTS discovery_terms (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    term                        TEXT UNIQUE NOT NULL,
    date_discovered             REAL NOT NULL,
    source                      TEXT,          -- seed / co-occurrence / url / manual
    number_of_posts_found       INTEGER DEFAULT 0,
    number_containing_valid_ca  INTEGER DEFAULT 0,
    number_leading_to_new_tokens INTEGER DEFAULT 0,
    subsequent_performance      REAL,          -- median net 15m return of discovered calls
    precision_score             REAL,          -- valid-CA posts / posts found
    last_seen                   REAL NOT NULL,
    status                      TEXT DEFAULT 'ACTIVE',  -- ACTIVE / DEPRIORITIZED / RETIRED
    created_at                  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS social_clusters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT,
    first_seen_ts   REAL NOT NULL,
    features_json   TEXT,                      -- evidence: shared text/urls/timestamps
    status          TEXT DEFAULT 'ACTIVE',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id  INTEGER NOT NULL REFERENCES social_clusters(id),
    member_type TEXT NOT NULL,                 -- x_account / tg_channel
    member_ref  TEXT NOT NULL,
    added_ts    REAL NOT NULL,
    PRIMARY KEY (cluster_id, member_type, member_ref)
);

CREATE TABLE IF NOT EXISTS social_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src_type    TEXT NOT NULL,                 -- token / x_account / tg_channel / ticker / url / cluster
    src_id      TEXT NOT NULL,
    dst_type    TEXT NOT NULL,
    dst_id      TEXT NOT NULL,
    relation    TEXT NOT NULL,                 -- MENTIONED/REPOSTED/COPIED/LINKED_TO/EARLY_ON/CO_OCCURS_WITH/PRECEDES/FOLLOWS/AMPLIFIES_WITH
    ts          REAL NOT NULL,
    weight      REAL DEFAULT 1.0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON social_edges(src_type, src_id);

CREATE TABLE IF NOT EXISTS token_social_state (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mint        TEXT NOT NULL,
    ts          REAL NOT NULL,
    state       TEXT NOT NULL,                 -- ISOLATED/EMERGING/ACCELERATING/COORDINATED/VIRAL/SATURATED/DECAYING/DEAD
    mpm         REAL,                          -- mentions per minute at transition
    unique_callers INTEGER,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_state_mint ON token_social_state(mint, ts);

CREATE TABLE IF NOT EXISTS virality_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mint                TEXT NOT NULL,
    ts                  REAL NOT NULL,
    unique_accounts     INTEGER,
    unique_channels     INTEGER,
    mentions_per_min    REAL,
    mention_accel       REAL,
    reposts             INTEGER,
    replies             INTEGER,
    likes               INTEGER,
    views               INTEGER,
    community_count     INTEGER,
    original_ratio      REAL,
    follower_reach      REAL,
    tg_x_spread         REAL,                  -- tg mentions after first x mention / total
    x_tg_spread         REAL,
    time_compression    REAL,                  -- callers / sqrt(window minutes)
    virality_score      REAL,
    model_name          TEXT,
    created_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_virality_mint ON virality_snapshots(mint, ts);

CREATE TABLE IF NOT EXISTS virality_models (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    weights_json TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_models (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    params_json TEXT NOT NULL,                 -- every assumption recorded
    created_at  REAL NOT NULL
);

-- Derived statistics: materialized snapshots, reproducible from raw tables.
CREATE TABLE IF NOT EXISTS account_statistics (
    account_ref                 TEXT NOT NULL,
    platform                    TEXT NOT NULL,
    computed_at                 REAL NOT NULL,
    total_calls                 INTEGER,
    unique_tokens_called        INTEGER,
    wins                        INTEGER,
    losses                      INTEGER,
    flat                        INTEGER,
    unpriceable                 INTEGER,
    rugged                      INTEGER,
    gross_win_rate              REAL,
    net_win_rate                REAL,
    avg_return_1m               REAL,
    avg_return_5m               REAL,
    avg_return_15m              REAL,
    avg_return_30m              REAL,
    avg_return_60m              REAL,
    median_return_15m           REAL,
    avg_mfe                     REAL,
    avg_mae                     REAL,
    median_mfe                  REAL,
    median_mae                  REAL,
    avg_position_in_cycle       REAL,
    avg_seconds_after_first_detection REAL,
    pct_calls_before_price_peak REAL,
    pct_calls_after_price_peak  REAL,
    avg_mcap_at_call            REAL,
    avg_liquidity_at_call       REAL,
    best_call_id                INTEGER,
    worst_call_id               INTEGER,
    sample_size                 INTEGER,
    ci95_low_15m                REAL,
    ci95_high_15m               REAL,
    role_label                  TEXT,          -- DISCOVERER/EARLY CALLER/AMPLIFIER/.../UNCLASSIFIED
    marginal_mfe_effect         REAL,          -- estimated incremental MFE when joining a running push
    marginal_sample             INTEGER,
    PRIMARY KEY (account_ref, platform)
);

CREATE TABLE IF NOT EXISTS channel_statistics (
    channel_ref TEXT PRIMARY KEY,
    computed_at REAL NOT NULL,
    stats_json  TEXT NOT NULL                  -- same shape as account_statistics
);

CREATE TABLE IF NOT EXISTS token_statistics (
    mint        TEXT PRIMARY KEY,
    computed_at REAL NOT NULL,
    stats_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id           TEXT UNIQUE NOT NULL,
    description             TEXT NOT NULL,
    created_at              REAL NOT NULL,
    discovery_dataset_end_time REAL NOT NULL,  -- hard boundary; never moved
    features_used           TEXT,
    expected_relationship   TEXT,
    minimum_sample_requirement INTEGER DEFAULT 30,
    testing_method          TEXT,
    status                  TEXT DEFAULT 'FORWARD_TESTING'  -- FORWARD_TESTING / SUPPORTED / REFUTED / INSUFFICIENT_DATA
);

CREATE TABLE IF NOT EXISTS hypothesis_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id   TEXT NOT NULL REFERENCES hypotheses(hypothesis_id),
    computed_at     REAL NOT NULL,
    dataset         TEXT NOT NULL,             -- DISCOVERY / FORWARD
    sample_size     INTEGER,
    gross_win_rate  REAL,
    net_win_rate    REAL,
    avg_return      REAL,
    median_return   REAL,
    avg_mfe         REAL,
    avg_mae         REAL,
    ci95_low        REAL,
    ci95_high       REAL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,                 -- NEW_CONTRACT_DETECTED / SECOND_INDEPENDENT_CALLER / ...
    mint        TEXT,
    payload_json TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS system_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    component   TEXT NOT NULL,
    error_code  TEXT NOT NULL,
    detail      TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS data_quality (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    issue       TEXT NOT NULL,                 -- duplicate_post / deleted_post / ticker_collision / ...
    ref_type    TEXT,
    ref_id      TEXT,
    detail      TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    provider    TEXT NOT NULL,                 -- helius / x / telegram
    endpoint    TEXT,
    status      TEXT,
    latency_ms  REAL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    call_event_id        INTEGER NOT NULL REFERENCES call_events(id),
    mint                 TEXT NOT NULL,
    strategy             TEXT NOT NULL,        -- repeat_lowliq_trail_v1 / repeat_lowliq_cooldown_v2 / ...
    entry_ts             REAL NOT NULL,
    entry_price          REAL NOT NULL,
    entry_liquidity_usd  REAL,
    position_in_cycle    INTEGER,
    status               TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN / CLOSED
    peak_price           REAL,
    peak_ts              REAL,
    last_price           REAL,
    last_price_ts        REAL,
    exit_ts              REAL,
    exit_price           REAL,
    exit_reason          TEXT,                 -- TRAILING_STOP / HARD_EXIT_60M / PRICE_STALE
    gross_return         REAL,
    net_return           REAL,
    cost_model_id        INTEGER REFERENCES cost_models(id),
    created_at           REAL NOT NULL,
    closed_at            REAL,
    UNIQUE(call_event_id, strategy)            -- one paper trade per signal per book (books are independent)
);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_trades(status);
CREATE INDEX IF NOT EXISTS idx_paper_mint ON paper_trades(mint);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path) -> None:
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # persistent file setting — set once here
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def now() -> float:
    return time.time()


def _safe_write(fn, *args) -> None:
    """Logging must never crash the collector. On lock contention, print to stderr."""
    import sqlite3 as _s
    try:
        fn(*args)
    except _s.OperationalError as e:
        print(f"db log skipped (locked): {e}", flush=True)


def log_error(conn: sqlite3.Connection, component: str, error_code: str, detail: str = "") -> None:
    _safe_write(lambda: conn.execute(
        "INSERT INTO system_errors (ts, component, error_code, detail, created_at) VALUES (?,?,?,?,?)",
        (now(), component, error_code, detail[:2000], now()),
    ))


def log_quality(conn: sqlite3.Connection, issue: str, ref_type: str = "", ref_id: str = "", detail: str = "") -> None:
    _safe_write(lambda: conn.execute(
        "INSERT INTO data_quality (ts, issue, ref_type, ref_id, detail, created_at) VALUES (?,?,?,?,?,?)",
        (now(), issue, ref_type, str(ref_id), detail[:2000], now()),
    ))


def log_api(conn: sqlite3.Connection, provider: str, endpoint: str, status: str, latency_ms: float) -> None:
    _safe_write(lambda: conn.execute(
        "INSERT INTO api_usage (ts, provider, endpoint, status, latency_ms, created_at) VALUES (?,?,?,?,?,?)",
        (now(), provider, endpoint, status, latency_ms, now()),
    ))


def alert(conn: sqlite3.Connection, kind: str, mint: str | None = None, payload: str = "{}") -> None:
    _safe_write(lambda: conn.execute(
        "INSERT INTO alerts (ts, kind, mint, payload_json, created_at) VALUES (?,?,?,?,?)",
        (now(), kind, mint, payload, now()),
    ))
