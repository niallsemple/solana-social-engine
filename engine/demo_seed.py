"""Demo seeder: generates a clearly-labeled SIMULATED dataset (source='simulated') so the
dashboard, milestone records, analytics and LLM API can be evaluated end-to-end before live
credentials exist. It writes raw rows through the same schema and reuses finalize_call() and
recompute_account_stats() so derived numbers are computed by the real code path, not hard-coded.

NOT forward research data: every row carries source='simulated' / quality_status='SIMULATED'.
"""
from __future__ import annotations

import json
import math
import random
import time

from . import config
from .analytics import ensure_default_cost_model, finalize_call, recompute_account_stats
from .db import alert, connect, now
from .virality import compute_virality, ensure_default_virality_model

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

SCENARIOS = [
    # (weight, kind) — realistic memecoin mix: most die fast, a few run
    (30, "pump_dump"),      # fast +60-200% then retrace below entry
    (20, "instant_rug"),    # -80%+ within minutes
    (15, "slow_moon"),      # sustained grind up over the hour
    (15, "flat_dead"),      # nothing happens
    (10, "multi_wave"),     # two waves, second bigger
    (10, "early_then_die"), # early spike only
]

ACCOUNT_POOL = [
    # (handle, platform, typical behavior)
    ("sol_scanner_bot", "x", "discoverer"),
    ("DegenAlphaLeaks", "x", "discoverer"),
    ("memecoin_maxi", "x", "early"),
    ("GemHunterSOL", "x", "early"),
    ("CryptoWhale999", "x", "amplifier"),
    ("SolanaKingCalls", "x", "amplifier"),
    ("100xGemsDaily", "x", "late"),
    ("MoonShotAlerts", "x", "peak"),
    ("FomoTraderJoe", "x", "post_peak"),
    ("pumpfun_snipers", "telegram", "discoverer"),
    ("sol_gems_channel", "telegram", "early"),
    ("degen_plays_tg", "telegram", "amplifier"),
    ("whale_watcher_tg", "telegram", "late"),
]

TERMS = ["memecoins", "gem", "100x", "sending", "cto", "next runner", "ape", "cabal", "ticker is", "moon"]

TEMPLATES = [
    "this {tk} gem is sending 🚀 CA: {mint}",
    "{tk} looks like the next runner on solana, CA {mint} #memecoins",
    "aped {tk} here, {mint} 100x potential",
    "cto on {tk}? community takeover vibes. {mint}",
    "ticker is {tk} — {mint} — moon soon",
    "{tk} {mint} cabal is accumulating, don't fade",
]


def _rand_mint(rng: random.Random) -> str:
    return "".join(rng.choice(B58) for _ in range(44))


def _gen_path(rng, kind, t0):
    """[(offset_sec, multiplier)] from entry price 1.0."""
    pts = [(0, 1.0)]
    def noise(m, amp=0.03):
        return m * (1 + rng.uniform(-amp, amp))
    if kind == "pump_dump":
        peak = rng.uniform(1.6, 3.0); tp = rng.uniform(120, 600)
        pts += [(tp * 0.5, noise(1 + (peak - 1) * 0.4)), (tp, noise(peak)),
                (900, noise(peak * 0.55)), (1800, noise(peak * 0.35)), (3600, noise(peak * 0.22))]
    elif kind == "instant_rug":
        pts += [(rng.uniform(60, 240), noise(rng.uniform(1.1, 1.5))),
                (rng.uniform(300, 700), noise(0.15, 0.2)), (900, noise(0.08, 0.3)), (3600, noise(0.05, 0.4))]
    elif kind == "slow_moon":
        pts += [(300, noise(1.15)), (900, noise(1.45)), (1800, noise(1.9)), (3600, noise(rng.uniform(2.2, 3.5)))]
    elif kind == "flat_dead":
        pts += [(300, noise(1.0)), (900, noise(0.98)), (1800, noise(0.9)), (3600, noise(0.85))]
    elif kind == "multi_wave":
        pts += [(300, noise(1.5)), (700, noise(1.1)), (1500, noise(2.4)), (2400, noise(1.8)), (3600, noise(2.6))]
    else:  # early_then_die
        pts += [(180, noise(2.0)), (400, noise(1.2)), (900, noise(0.6)), (1800, noise(0.4)), (3600, noise(0.3))]
    return pts


def _interp(pts, off):
    if off <= pts[0][0]:
        return pts[0][1]
    if off >= pts[-1][0]:
        return pts[-1][1]
    for (a0, m0), (a1, m1) in zip(pts, pts[1:]):
        if a0 <= off <= a1:
            return m0 + (m1 - m0) * (off - a0) / max(a1 - a0, 1e-9)
    return pts[-1][1]


def seed(db_path: str, n_tokens: int = 12, rng_seed: int = 7):
    rng = random.Random(rng_seed)
    conn = connect(db_path)
    t_now = now()
    day_start = t_now - (t_now % 86400)
    kinds = [k for w, k in SCENARIOS for _ in range(w)]
    ensure_default_cost_model(conn)
    ensure_default_virality_model(conn)

    for term in TERMS:
        conn.execute(
            "INSERT INTO discovery_terms (term, date_discovered, source, last_seen, created_at) VALUES (?,?,?,?,?) ON CONFLICT(term) DO NOTHING",
            (term, day_start, "seed" if term == "memecoins" else "co-occurrence", t_now, day_start),
        )

    for i in range(n_tokens):
        kind = rng.choice(kinds)
        mint = _rand_mint(rng)
        ticker = f"SIM{i:02d}"
        t0 = day_start + rng.uniform(3600, max(t_now - day_start - 4200, 7200))
        if t0 > t_now - 3700:
            t0 = t_now - 3700 - rng.uniform(0, 3600)
        base_price = 10 ** rng.uniform(-7, -5)
        path = _gen_path(rng, kind, t0=0)

        # token + event
        conn.execute(
            "INSERT OR IGNORE INTO tokens (mint, symbol, first_seen_ts, launch_platform, status, created_at, source, quality_status) VALUES (?,?,?,?,?,?,?,?)",
            (mint, ticker, t0, "pump.fun", "RUGGED" if kind == "instant_rug" else "ACTIVE", t0, "simulated", "SIMULATED"),
        )
        ev = conn.execute(
            "INSERT INTO token_events (mint, first_detection_ts, social_push_start, created_at, quality_status) VALUES (?,?,?,?,?)",
            (mint, t0, t0, t0, "SIMULATED"),
        )
        event_id = ev.lastrowid

        # callers: discoverers first, amplifiers mid, lates near peak; coordinated bursts for some
        n_calls = {"flat_dead": rng.randint(1, 2), "instant_rug": rng.randint(2, 5)}.get(kind, rng.randint(3, 10))
        accounts = rng.sample(ACCOUNT_POOL, min(n_calls, len(ACCOUNT_POOL)))
        accounts.sort(key=lambda a: {"discoverer": 0, "early": 1, "amplifier": 2, "late": 3, "peak": 4, "post_peak": 5}[a[2]])
        offsets = sorted(rng.uniform(0, 2400) for _ in accounts)

        first_call_id = None
        for (handle, platform, behavior), off in zip(accounts, offsets):
            ts = t0 + off
            followers = rng.randint(200, 500_000) if platform == "x" else None
            table = "x_accounts" if platform == "x" else "telegram_channels"
            col = "followers" if platform == "x" else "members"
            conn.execute(
                f"INSERT OR IGNORE INTO {table} (handle, {col}, first_seen_ts, discovered_via, created_at) VALUES (?,?,?,?,?)",
                (handle, followers, ts, "simulated", ts),
            )
            text = rng.choice(TEMPLATES).format(tk=f"${ticker}", mint=mint)
            post = conn.execute(
                """INSERT INTO social_posts
                   (platform, external_id, author_ref, text, original_ts, system_ts, urls_json, tickers_json,
                    cas_json, is_repost, is_quote, likes, reposts, replies, views, raw_json, created_at, source, quality_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (platform, f"sim-{mint[:8]}-{int(ts)}", handle, text, ts - rng.uniform(2, 40), ts,
                 "[]", json.dumps([ticker]), json.dumps({"valid": [mint], "candidates": [mint]}),
                 1 if behavior == "post_peak" and rng.random() < 0.5 else 0, 0,
                 rng.randint(0, 400), rng.randint(0, 120), rng.randint(0, 60), rng.randint(100, 50000),
                 "{}", ts, "simulated", "SIMULATED"),
            )
            entry_price = base_price * _interp(path, off)
            call = conn.execute(
                """INSERT INTO call_events
                   (mint, token_event_id, post_id, platform, account_ref, original_post_ts,
                    system_detection_ts, detection_latency_ms, position_in_cycle,
                    seconds_after_first_detection, created_at, quality_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mint, event_id, post.lastrowid, platform, handle, ts - rng.uniform(2, 40), ts,
                 rng.uniform(500, 40000), 0, off, ts, "SIMULATED"),
            )
            if first_call_id is None:
                first_call_id = call.lastrowid
        # fix positions
        conn.execute("""
            UPDATE call_events SET position_in_cycle = (
              SELECT COUNT(*) FROM call_events c2
              WHERE c2.mint = call_events.mint AND c2.system_detection_ts <= call_events.system_detection_ts)
            WHERE mint = ?""", (mint,))

        # price observations: HF for 15 min + horizons + tail (attached to first call; finalize reads by mint)
        obs_ts = list(range(0, 901, 7)) + [60, 300, 900, 1800, 3600] + list(range(920, 3900, 120))
        seen = set()
        for off in sorted(set(obs_ts)):
            if off in seen:
                continue
            seen.add(off)
            label = {0: "T0", 60: "T+1m", 300: "T+5m", 900: "T+15m", 1800: "T+30m", 3600: "T+60m"}.get(off)
            if label is None:
                label = "HF" if off <= 900 else "EXT"
            price = base_price * _interp(path, off) * rng.uniform(0.995, 1.005)
            # simulated feed also has occasional failures — kept, never deleted
            fail = rng.random() < 0.02
            conn.execute(
                """INSERT INTO price_observations
                   (mint, call_event_id, ts, horizon_label, price_usd, sol_price_usd, mcap_usd,
                    liquidity_usd, source, error_code, created_at, quality_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mint, first_call_id, t0 + off, label,
                 None if fail else price, 200.0, None if fail else price * 1e9,
                 None if fail else rng.uniform(8_000, 400_000),
                 "simulated", "HELIUS_PRICE_UNAVAILABLE" if fail else None, t0 + off,
                 "SIMULATED" if not fail else "PRICE_ERROR"),
            )

        # per-call T0 entry already set; finalize each call through the real analytics path
        conn.execute("UPDATE call_events SET entry_price = ?, price_capture_ts = system_detection_ts WHERE mint = ? AND entry_price IS NULL",
                     (base_price, mint))
        for c in conn.execute("SELECT id FROM call_events WHERE mint = ?", (mint,)).fetchall():
            finalize_call(conn, c["id"])

        conn.execute("""
            UPDATE token_events SET
              total_x_mentions = (SELECT COUNT(*) FROM call_events WHERE mint = ? AND platform='x'),
              total_tg_mentions = (SELECT COUNT(*) FROM call_events WHERE mint = ? AND platform='telegram'),
              unique_x_accounts = (SELECT COUNT(DISTINCT account_ref) FROM call_events WHERE mint = ? AND platform='x'),
              unique_tg_channels = (SELECT COUNT(DISTINCT account_ref) FROM call_events WHERE mint = ? AND platform='telegram'),
              price_at_first_detection = ?
            WHERE id = ?""",
            (mint, mint, mint, mint, base_price, event_id))

        # virality + states (compute_virality uses now(); for demo we write snapshots along the push instead)
        for off, st in [(0, "ISOLATED"), (120, "EMERGING"), (420, "ACCELERATING"),
                        (900, "VIRAL" if n_calls >= 6 else "ACCELERATING"), (1800, "SATURATED"),
                        (2700, "DECAYING"), (3600, "DEAD")]:
            conn.execute(
                "INSERT INTO token_social_state (mint, ts, state, mpm, unique_callers, created_at) VALUES (?,?,?,?,?,?)",
                (mint, t0 + off, st, n_calls / max(off / 60, 1), min(n_calls, 1 + off // 200), t0 + off),
            )
        conn.execute(
            "UPDATE token_events SET current_state = 'DEAD', social_push_end = ?, total_push_duration = 3600 WHERE id = ?",
            (t0 + 3600, event_id),
        )
        score = n_calls * 1.2 + (2.0 if kind == "multi_wave" else 0)
        conn.execute(
            """INSERT INTO virality_snapshots
               (mint, ts, unique_accounts, unique_channels, mentions_per_min, mention_accel, reposts, replies,
                likes, views, community_count, original_ratio, follower_reach, tg_x_spread, x_tg_spread,
                time_compression, virality_score, model_name, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mint, t0 + 900, sum(1 for a in accounts if a[1] == "x"), sum(1 for a in accounts if a[1] == "telegram"),
             n_calls / 15.0, 0.3, rng.randint(0, 200), rng.randint(0, 100), rng.randint(0, 2000),
             rng.randint(1000, 80000), sum(1 for a in accounts if a[1] == "telegram"),
             rng.uniform(0.4, 1.0), rng.uniform(1e4, 1e6), rng.uniform(0, 1), rng.uniform(0, 1),
             n_calls / math.sqrt(15), score, "virality_v1", t0 + 900),
        )
        alert(conn, "NEW_CONTRACT_DETECTED", mint, json.dumps({"simulated": True}))
        if n_calls >= 5:
            alert(conn, "VIRALITY_ACCELERATION", mint, json.dumps({"simulated": True}))

        # lexicon counters for this token's posts
        for term in rng.sample(TERMS, k=min(3, len(TERMS))):
            conn.execute(
                """UPDATE discovery_terms SET number_of_posts_found = number_of_posts_found + ?,
                     number_containing_valid_ca = number_containing_valid_ca + ?,
                     number_leading_to_new_tokens = number_leading_to_new_tokens + 1,
                     precision_score = CAST(number_containing_valid_ca + ? AS REAL) / MAX(number_of_posts_found + ?, 1)
                   WHERE term = ?""",
                (n_calls, n_calls, n_calls, n_calls, term),
            )

    # one demo hypothesis registered NOW with an immutable boundary (no forward data yet)
    from .hypotheses import register_hypothesis
    hid = register_hypothesis(
        conn,
        "Calls made by 3+ independent accounts within the first 5 minutes have higher median MFE_15m than single-account calls",
        ["position_in_cycle", "seconds_after_first_detection", "mfe_15m"],
        "positive median MFE difference vs baseline",
    )

    recompute_account_stats(conn)
    conn.commit()
    n_calls_total = conn.execute("SELECT COUNT(*) AS n FROM call_events").fetchone()["n"]
    n_tok = conn.execute("SELECT COUNT(*) AS n FROM tokens").fetchone()["n"]
    conn.close()
    print(f"Seeded SIMULATED dataset: {n_tok} tokens, {n_calls_total} calls, demo hypothesis {hid}")
    print("All demo rows carry source='simulated' / quality_status='SIMULATED' and are NOT forward research data.")
