"""Ingestion pipeline: normalize X/Telegram posts, extract Solana CAs, create first-seen
token events and per-mention call events, grow the account/channel/term discovery network.

T0 = the first time OUR SYSTEM detects the contract (system_ts), never the original post time.
original_post_timestamp, system_detection_timestamp and price_capture_timestamp are stored
separately; detection latency is computed from them. Forward-only: nothing is backfilled.
"""
from __future__ import annotations

import json
import time

from . import cas as ca
from .db import alert, log_quality, now
from .pricing import PriceQuote, record_observation


def _upsert_account(conn, handle: str, platform: str, followers: int | None, discovered_via: str) -> None:
    table = "x_accounts" if platform == "x" else "telegram_channels"
    col = "followers" if platform == "x" else "members"
    row = conn.execute(f"SELECT id FROM {table} WHERE handle = ?", (handle,)).fetchone()
    if row is None:
        conn.execute(
            f"INSERT INTO {table} (handle, {col}, first_seen_ts, discovered_via, created_at) VALUES (?,?,?,?,?)",
            (handle, followers, now(), discovered_via, now()),
        )
    elif followers is not None:
        conn.execute(f"UPDATE {table} SET {col} = ? WHERE handle = ?", (followers, handle))


def _mark_copy(conn, post_id: int, text: str, mints: list[str]) -> tuple[int, float]:
    """Flag near-duplicates: ten copied posts carry different information than ten originals."""
    if not text or not mints:
        return 0, 1.0
    rows = conn.execute(
        """SELECT p.id, p.text FROM social_posts p
           JOIN call_events c ON c.post_id = p.id
           WHERE c.mint IN (%s) AND p.id != ? ORDER BY p.id DESC LIMIT 20"""
        % ",".join("?" * len(mints)),
        (*mints, post_id),
    ).fetchall()
    best = max((ca.text_similarity(text, r["text"] or "") for r in rows), default=0.0)
    is_copy = 1 if best >= 0.85 else 0
    return is_copy, round(1.0 - best, 3)


def _lexicon_hits(conn, text: str, has_valid_ca: bool, led_to_new_token: bool) -> list[str]:
    """Update DISCOVERY_LEXICON stats for every known term present in the text."""
    low = (text or "").lower()
    hits = []
    for row in conn.execute("SELECT id, term FROM discovery_terms WHERE status != 'RETIRED'").fetchall():
        term = row["term"]
        if term.lower() in low:
            hits.append(term)
            conn.execute(
                """UPDATE discovery_terms SET
                     number_of_posts_found = number_of_posts_found + 1,
                     number_containing_valid_ca = number_containing_valid_ca + ?,
                     number_leading_to_new_tokens = number_leading_to_new_tokens + ?,
                     last_seen = ?
                   WHERE id = ?""",
                (1 if has_valid_ca else 0, 1 if led_to_new_token else 0, now(), row["id"]),
            )
            conn.execute(
                "UPDATE discovery_terms SET precision_score = CAST(number_containing_valid_ca AS REAL)/MAX(number_of_posts_found,1) WHERE id = ?",
                (row["id"],),
            )
    return hits


def _learn_terms(conn, text: str, has_valid_ca: bool) -> None:
    """Discover candidate new terms: hashtags and slang-y tokens co-occurring with valid CAs."""
    if not has_valid_ca or not text:
        return
    for word in set(text.lower().split()):
        w = word.strip("#$.,!?:;\"'()[]")
        # must look like language, not a number/unit artifact
        if not (2 <= len(w) <= 24):
            continue
        if not (word.startswith("#") or word.startswith("$")):
            continue
        alpha = sum(ch.isalpha() for ch in w)
        if alpha < 2 or alpha / len(w) < 0.6:
            continue
        exists = conn.execute("SELECT id FROM discovery_terms WHERE term = ?", (w,)).fetchone()
        if not exists:
            conn.execute(
                """INSERT INTO discovery_terms (term, date_discovered, source, last_seen, created_at)
                   VALUES (?,?,?,?,?)""",
                (w, now(), "co-occurrence", now(), now()),
            )
            conn.execute(
                "INSERT INTO social_sources (kind, value, discovered_via, first_seen_ts, last_seen_ts, created_at) VALUES ('search_term',?,?,?,?,?) ON CONFLICT DO NOTHING",
                (w, "co-occurrence", now(), now(), now()),
            )


def ingest_post(conn, post: dict, price_provider=None) -> dict:
    """Normalize one raw post and run the full detection pipeline.

    post schema: {platform, external_id, author, text, original_ts, followers?,
                  likes?, reposts?, replies?, views?, is_repost?, is_quote?, raw?}
    Returns a summary of what happened (for replay logging)."""
    platform = post["platform"]
    assert platform in ("x", "telegram")
    author = post["author"].lstrip("@")
    system_ts = now()
    original_ts = post.get("original_ts") or system_ts
    result = {"post_id": None, "call_events": [], "new_tokens": [], "duplicate": False}

    ext_id = post.get("external_id")
    if ext_id is not None:
        dup = conn.execute(
            "SELECT id FROM social_posts WHERE platform = ? AND external_id = ?",
            (platform, str(ext_id)),
        ).fetchone()
        if dup:
            log_quality(conn, "duplicate_post", "social_post", dup["id"], f"{platform}:{ext_id}")
            result["duplicate"] = True
            result["post_id"] = dup["id"]
            return result
    if original_ts > system_ts + 300:
        log_quality(conn, "clock_error", "post", ext_id or "", "original_ts in the future")

    ext = ca.extract(post.get("text") or "")
    cur = conn.execute(
        """INSERT INTO social_posts
           (platform, external_id, author_ref, text, original_ts, system_ts, urls_json,
            tickers_json, cas_json, is_repost, is_quote, likes, reposts, replies, views,
            raw_json, created_at, source, quality_status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (platform, str(ext_id) if ext_id is not None else None, author, post.get("text"),
         original_ts, system_ts, json.dumps(ext.urls), json.dumps(ext.tickers),
         json.dumps({"valid": ext.valid_cas, "candidates": ext.candidates}),
         1 if post.get("is_repost") else 0, 1 if post.get("is_quote") else 0,
         post.get("likes"), post.get("reposts"), post.get("replies"), post.get("views"),
         json.dumps(post.get("raw") or {}), system_ts, post.get("source", platform), "OK"),
    )
    post_id = cur.lastrowid
    result["post_id"] = post_id

    _upsert_account(conn, author, platform, post.get("followers") or post.get("members"),
                    discovered_via=post.get("discovered_via", "seed-search"))

    # network growth: telegram links discovered anywhere become new channel sources
    for handle in ext.telegram_links:
        conn.execute(
            "INSERT INTO social_sources (kind, value, discovered_via, first_seen_ts, last_seen_ts, created_at) VALUES ('channel',?,?,?,?,?) ON CONFLICT(kind, value) DO UPDATE SET last_seen_ts = excluded.last_seen_ts",
            (handle, f"{platform}:{author}", system_ts, system_ts, system_ts),
        )
        _upsert_account(conn, handle, "telegram", None, discovered_via=f"{platform}:{author}")
        conn.execute(
            "INSERT INTO social_edges (src_type, src_id, dst_type, dst_id, relation, ts, created_at) VALUES (?,?,?,?,?,?,?)",
            ("x_account" if platform == "x" else "tg_channel", author, "tg_channel", handle, "LINKED_TO", system_ts, system_ts),
        )

    hits = _lexicon_hits(conn, post.get("text") or "", has_valid_ca=bool(ext.valid_cas),
                         led_to_new_token=False)
    _learn_terms(conn, post.get("text") or "", has_valid_ca=bool(ext.valid_cas))

    for mint in ext.valid_cas:
        token = conn.execute("SELECT mint, first_seen_ts FROM tokens WHERE mint = ?", (mint,)).fetchone()
        is_new_token = token is None
        if is_new_token:
            conn.execute(
                "INSERT INTO tokens (mint, symbol, first_seen_ts, launch_platform, created_at, source) VALUES (?,?,?,?,?,?)",
                (mint, ext.tickers[0] if ext.tickers else None, system_ts,
                 "pump.fun" if mint in ext.pumpfun_cas else "unknown", system_ts, platform),
            )
            conn.execute(
                "INSERT INTO token_events (mint, first_detection_ts, social_push_start, created_at) VALUES (?,?,?,?)",
                (mint, system_ts, system_ts, system_ts),
            )
            alert(conn, "NEW_CONTRACT_DETECTED", mint,
                  json.dumps({"platform": platform, "account": author, "post_id": post_id}))
            conn.execute(
                "INSERT INTO social_edges (src_type, src_id, dst_type, dst_id, relation, ts, created_at) VALUES (?,?,?,?,?,?,?)",
                ("x_account" if platform == "x" else "tg_channel", author, "token", mint, "EARLY_ON", system_ts, system_ts),
            )
            result["new_tokens"].append(mint)
            for h in hits:
                conn.execute(
                    "UPDATE discovery_terms SET number_leading_to_new_tokens = number_leading_to_new_tokens + 1 WHERE term = ?",
                    (h,),
                )

        ev = conn.execute("SELECT id FROM token_events WHERE mint = ? ORDER BY id DESC LIMIT 1", (mint,)).fetchone()
        prior_callers = conn.execute(
            "SELECT COUNT(DISTINCT account_ref) AS n FROM call_events WHERE mint = ?", (mint,)
        ).fetchone()["n"]
        position = prior_callers + 1
        first_seen = token["first_seen_ts"] if token else system_ts

        cur = conn.execute(
            """INSERT INTO call_events
               (mint, token_event_id, post_id, platform, account_ref, original_post_ts,
                system_detection_ts, detection_latency_ms, position_in_cycle,
                seconds_after_first_detection, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (mint, ev["id"], post_id, platform, author, original_ts, system_ts,
             (system_ts - original_ts) * 1000.0, position, system_ts - first_seen, system_ts),
        )
        call_id = cur.lastrowid
        result["call_events"].append(call_id)

        conn.execute(
            "INSERT INTO social_edges (src_type, src_id, dst_type, dst_id, relation, ts, created_at) VALUES (?,?,?,?,?,?,?)",
            ("x_account" if platform == "x" else "tg_channel", author, "token", mint, "MENTIONED", system_ts, system_ts),
        )

        if position == 2:
            alert(conn, "SECOND_INDEPENDENT_CALLER", mint, json.dumps({"call_id": call_id}))

        # T0 price capture for THIS call (each call has its own theoretical entry)
        if price_provider is not None:
            q: PriceQuote = price_provider.quote(mint)
            obs_id = record_observation(conn, mint, q, call_event_id=call_id, horizon_label="T0")
            cap_ts = now()
            if q.ok:
                conn.execute(
                    "UPDATE call_events SET entry_price = ?, price_capture_ts = ? WHERE id = ?",
                    (q.price_usd, cap_ts, call_id),
                )
                if position == 1:
                    conn.execute(
                        "UPDATE token_events SET price_at_first_detection = ? WHERE id = ?",
                        (q.price_usd, ev["id"]),
                    )
            else:
                conn.execute(
                    "UPDATE call_events SET entry_error = ?, price_capture_ts = ? WHERE id = ?",
                    (q.error_code, cap_ts, call_id),
                )
        else:
            conn.execute("UPDATE call_events SET entry_error = 'NO_PROVIDER' WHERE id = ?", (call_id,))

        # copy-vs-original: compare against earlier posts for this mint
        is_copy, originality = _mark_copy(conn, post_id, post.get("text") or "", [mint])
        conn.execute(
            "UPDATE social_posts SET is_copy = ?, originality_score = ? WHERE id = ?",
            (is_copy, originality, post_id),
        )

        _refresh_token_event(conn, mint, ev["id"])

    return result


def _refresh_token_event(conn, mint: str, event_id: int) -> None:
    """Update token_event aggregate counters after each call (mention velocity etc.)."""
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN platform = 'x' THEN 1 ELSE 0 END) AS x_n,
                  SUM(CASE WHEN platform = 'telegram' THEN 1 ELSE 0 END) AS tg_n,
                  COUNT(DISTINCT CASE WHEN platform = 'x' THEN account_ref END) AS ux,
                  COUNT(DISTINCT CASE WHEN platform = 'telegram' THEN account_ref END) AS utg,
                  MIN(system_detection_ts) AS t0, MAX(system_detection_ts) AS tmax
           FROM call_events WHERE mint = ?""",
        (mint,),
    ).fetchone()
    span_min = max((row["tmax"] - row["t0"]) / 60.0, 1e-6)
    velocity = row["total"] / span_min
    conn.execute(
        """UPDATE token_events SET total_x_mentions = ?, total_tg_mentions = ?,
             unique_x_accounts = ?, unique_tg_channels = ?, mention_velocity = ?,
             total_push_duration = ? WHERE id = ?""",
        (row["x_n"], row["tg_n"], row["ux"], row["utg"], velocity,
         row["tmax"] - row["t0"], event_id),
    )


def replay_jsonl(db_path: str, path: str, price_provider=None) -> None:
    """Replay a JSONL file of posts through the live pipeline at accelerated speed.
    Lines: {"platform": ..., "external_id": ..., "author": ..., "text": ...,
            "original_ts": ..., ...}
    Replay PRESERVES original_ts but assigns fresh system_ts (forward-only semantics)."""
    from .db import connect

    conn = connect(db_path)
    n_posts = n_calls = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            res = ingest_post(conn, json.loads(line), price_provider)
            n_posts += 1
            n_calls += len(res["call_events"])
            if n_posts % 100 == 0:
                conn.commit()
    conn.commit()
    conn.close()
    print(f"Replayed {n_posts} posts -> {n_calls} call events")
