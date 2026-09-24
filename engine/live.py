"""Live ingestion: X recent-search polling + public Telegram channel reading.

Requires credentials in env: X_BEARER_TOKEN (X API v2), TELEGRAM_API_ID/HASH/SESSION (Telethon).
Without credentials it refuses to start rather than fabricating data. Forward-only: polling
starts from now; nothing is backfilled.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from . import config
from .db import connect, log_api, log_error, now
from .ingest import ingest_post
from .pricing import HeliusProvider
from .sampler import start_background_sampler


def _x_headers(url: str, params: dict) -> dict:
    """Bearer token if present, else OAuth 1.0a user-context signature."""
    if config.X_BEARER_TOKEN:
        return {"Authorization": f"Bearer {config.X_BEARER_TOKEN}"}
    from .x_oauth import oauth1_header
    return {"Authorization": oauth1_header(
        "GET", url, params,
        config.X_API_KEY, config.X_API_SECRET,
        config.X_ACCESS_TOKEN, config.X_ACCESS_SECRET,
    )}


def x_search(conn, provider, query: str, since_id: str | None) -> str | None:
    """One X API v2 recent-search page. Returns newest id for the next poll."""
    params = {
        "query": query, "max_results": 100,
        "tweet.fields": "created_at,public_metrics,referenced_tweets,entities",
        "expansions": "author_id",
        "user.fields": "username,public_metrics",
        **({"since_id": since_id} if since_id else {}),
    }
    base = "https://api.x.com/2/tweets/search/recent"
    url = base + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_x_headers(base, params))
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        log_api(conn, "x", "search/recent", "ok", (time.time() - t0) * 1000)
    except Exception as e:
        log_api(conn, "x", "search/recent", "error", (time.time() - t0) * 1000)
        log_error(conn, "x_ingest", "RPC_ERROR", str(e)[:300])
        return since_id
    users = {u["id"]: u for u in (data.get("includes") or {}).get("users") or []}
    newest = since_id
    for tw in data.get("data") or []:
        refs = tw.get("referenced_tweets") or []
        metrics = tw.get("public_metrics") or {}
        user = users.get(tw.get("author_id")) or {}
        res = ingest_post(conn, {
            "platform": "x",
            "external_id": tw["id"],
            "author": user.get("username") or tw.get("author_id", "unknown"),
            "text": tw.get("text", ""),
            "original_ts": _parse_iso(tw.get("created_at")),
            "followers": (user.get("public_metrics") or {}).get("followers_count"),
            "is_repost": any(r["type"] == "retweeted" for r in refs),
            "is_quote": any(r["type"] == "quoted" for r in refs),
            "likes": metrics.get("like_count"), "reposts": metrics.get("retweet_count"),
            "replies": metrics.get("reply_count"), "views": metrics.get("impression_count"),
            "raw": tw,
        }, provider)
        if newest is None or str(tw["id"]) > str(newest):
            newest = tw["id"]
    return newest


def _parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    import datetime as dt
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def ensure_seed_terms(conn) -> None:
    for term in config.SEED_TERMS:
        conn.execute(
            "INSERT INTO discovery_terms (term, date_discovered, source, last_seen, created_at) VALUES (?,?,?,?,?) ON CONFLICT(term) DO NOTHING",
            (term, now(), "seed", now(), now()),
        )


def active_terms(conn, limit: int = 12) -> list[str]:
    """Terms ranked by what actually produces NEW TOKENS recently — never precision alone
    (one-hit wonders have precision 1.0). Seed terms are always included as the base layer."""
    rows = conn.execute(
        """SELECT term FROM discovery_terms
           WHERE status = 'ACTIVE' AND number_leading_to_new_tokens > 0
           ORDER BY number_leading_to_new_tokens DESC, last_seen DESC LIMIT ?""",
        (max(limit - len(config.SEED_TERMS), 1),),
    ).fetchall()
    terms = list(dict.fromkeys(config.SEED_TERMS + [r["term"] for r in rows]))
    return terms[:limit]


def x_calls_today(conn, anchor_ts: float = 0.0) -> int:
    """X API calls since UTC midnight, but never before `anchor_ts` (ok and error both
    counted — conservative). The anchor lets a freshly throttled collector start with a
    clean counter: spend that happened under the old unthrottled code is history, not
    budget. The cap still fully binds from the next UTC midnight onward."""
    day_start = max(int(now() // 86400) * 86400, anchor_ts)
    return conn.execute(
        "SELECT COUNT(*) AS n FROM api_usage WHERE provider = 'x' AND ts >= ?", (day_start,)
    ).fetchone()["n"]


def prune_lexicon(conn) -> None:
    """Terms that keep matching posts but never produce new tokens lose priority."""
    conn.execute(
        """UPDATE discovery_terms SET status = 'DEPRIORITIZED'
           WHERE status = 'ACTIVE' AND number_of_posts_found >= 20
             AND number_leading_to_new_tokens = 0""",
    )


def telegram_loop(conn, provider, stop):
    """Public channels only. Uses Telethon if installed + configured."""
    try:
        from telethon import TelegramClient  # type: ignore
    except ImportError:
        log_error(conn, "tg_ingest", "UNKNOWN_ERROR", "telethon not installed; telegram ingestion disabled")
        return
    # Telethon async client would go here; intentionally minimal until credentials exist.
    log_error(conn, "tg_ingest", "UNKNOWN_ERROR", "telegram loop not started: configure TELEGRAM_* env vars")


def run_live(db_path: str):
    if not config.HELIUS_API_KEY:
        raise SystemExit("HELIUS_API_KEY not set — refusing to run live without price infrastructure")
    if not config.X_AVAILABLE:
        raise SystemExit("No X credentials (X_BEARER_TOKEN or OAuth1 quartet) — refusing to run live without X ingestion")
    conn = connect(db_path)
    ensure_seed_terms(conn)
    conn.commit()
    provider = HeliusProvider(conn)  # same-thread use: main loop only; sampler passes its own conn to quote()
    start_background_sampler(db_path, HeliusProvider())
    since: dict[str, str | None] = {}
    backoff = 0
    rotation = 0
    budget_anchor = 0.0  # count from UTC midnight — restarts must never reset the daily cap
    budget_notice_day = -1
    print("live ingestion started (forward-only; Ctrl-C to stop)", flush=True)
    print(
        f"X spend control: {config.X_TERMS_PER_CYCLE} terms/cycle, {config.X_CYCLE_SEC:.0f}s cycles, "
        f"budget {config.X_DAILY_CALL_BUDGET or 'unlimited'} calls/day",
        flush=True,
    )
    try:
        while True:
            try:
                if config.X_DAILY_CALL_BUDGET and x_calls_today(conn, budget_anchor) >= config.X_DAILY_CALL_BUDGET:
                    day = int(now() // 86400)
                    if day != budget_notice_day:  # log once per day, not every pass
                        budget_notice_day = day
                        msg = f"X daily budget reached ({config.X_DAILY_CALL_BUDGET} calls) — polling paused until next UTC day"
                        print(msg, flush=True)
                        log_error(conn, "x_ingest", "BUDGET_CAP", msg)
                        conn.commit()
                    time.sleep(300)
                    continue
                pool = active_terms(conn, limit=max(config.X_TERMS_PER_CYCLE * 10, 12))
                # rotate through the pool so good terms all get coverage across cycles
                batch = [
                    pool[(rotation + i) % len(pool)]
                    for i in range(min(config.X_TERMS_PER_CYCLE, len(pool)))
                ]
                rotation = (rotation + config.X_TERMS_PER_CYCLE) % len(pool)
                for term in batch:
                    if config.X_DAILY_CALL_BUDGET and x_calls_today(conn, budget_anchor) >= config.X_DAILY_CALL_BUDGET:
                        break  # hard stop mid-batch too — never exceed the cap
                    since[term] = x_search(conn, provider, f"{term} solana", since.get(term))
                    time.sleep(config.X_QUERY_GAP_SEC)
                conn.commit()
                backoff = 0
                print(f"cycle done: terms={batch} budget_used={x_calls_today(conn, budget_anchor)}", flush=True)
            except Exception as e:
                # transient API/lock errors must never kill the collector
                backoff = min(backoff + 1, 6)
                wait = 30 * backoff
                print(f"ingest loop error: {e!r} — backing off {wait}s", flush=True)
                try:
                    conn.rollback()
                except Exception:
                    pass
                time.sleep(wait)
                continue
            time.sleep(config.X_CYCLE_SEC)
    except KeyboardInterrupt:
        conn.close()
