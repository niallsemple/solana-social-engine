"""Live price sampler. For every call event:
- high-frequency samples (~5-10s) during the first 15 minutes (path reconstruction)
- endpoint captures at T+1m/5m/15m/30m/60m
- dynamic extension: while new social mentions keep arriving, EXT samples continue
  every 60s until the push has been idle for EXTENSION_IDLE_SEC
- when monitoring completes, finalize_call() computes returns/MFE/MAE/classification

Failures are recorded as observations with error codes — never skipped silently.
"""
from __future__ import annotations

import threading
import time

from . import config
from .analytics import finalize_call, recompute_account_stats
from .db import connect, log_error, now

HORIZON_LABELS = [(60, "T+1m"), (300, "T+5m"), (900, "T+15m"), (1800, "T+30m"), (3600, "T+60m")]
EXT_INTERVAL_SEC = 60.0


def _due_labels(conn, call) -> list[tuple[str, float]]:
    """Which observations are due for this call right now. Horizon labels are satisfied
    by MINT-level coverage: if another call's observation already captured this mint near
    the target time, we don't re-quote (finalize_call reads observations by mint)."""
    t0 = call["system_detection_ts"]
    t = now()
    mint = call["mint"]
    have = {
        r["horizon_label"]
        for r in conn.execute(
            "SELECT DISTINCT horizon_label FROM price_observations WHERE call_event_id = ?", (call["id"],)
        ).fetchall()
    }
    due = []
    for secs, label in HORIZON_LABELS:
        target = t0 + secs
        if label in have or t < target:
            continue
        covered = conn.execute(
            "SELECT 1 FROM price_observations WHERE mint = ? AND horizon_label = ? AND ABS(ts - ?) <= 45 LIMIT 1",
            (mint, label, target),
        ).fetchone()
        if not covered:
            due.append((label, target))
    # high-frequency path sampling inside the first 15 minutes (mint-level coverage too)
    if t0 <= t <= t0 + config.HF_WINDOW_SEC:
        last_hf = conn.execute(
            "SELECT MAX(ts) AS m FROM price_observations WHERE mint = ? AND horizon_label = 'HF'",
            (mint,),
        ).fetchone()["m"]
        if last_hf is None or t - last_hf >= config.HF_INTERVAL_SEC:
            due.append(("HF", t))
    # dynamic extension: mentions still arriving after T+60m -> keep sampling (EXT)
    if t > t0 + 3600:
        last_mention = conn.execute(
            "SELECT MAX(system_detection_ts) AS m FROM call_events WHERE mint = ?", (mint,)
        ).fetchone()["m"]
        if last_mention and t - last_mention < config.EXTENSION_IDLE_SEC:
            last_ext = conn.execute(
                "SELECT MAX(ts) AS m FROM price_observations WHERE mint = ? AND horizon_label = 'EXT'",
                (mint,),
            ).fetchone()["m"]
            if last_ext is None or t - last_ext >= EXT_INTERVAL_SEC:
                due.append(("EXT", t))
    return due


def _monitoring_done(conn, call) -> bool:
    t0 = call["system_detection_ts"]
    t = now()
    mint = call["mint"]
    have = {
        r["horizon_label"]
        for r in conn.execute(
            "SELECT DISTINCT horizon_label FROM price_observations WHERE call_event_id = ?", (call["id"],)
        ).fetchall()
    }
    for secs, label in HORIZON_LABELS:
        if label in have:
            continue  # captured on time, or honestly marked (e.g. LATE_CAPTURE)
        cov = conn.execute(
            """SELECT 1 FROM price_observations WHERE mint = ? AND horizon_label = ?
               AND ABS(ts - ?) <= 45 LIMIT 1""",
            (mint, label, t0 + secs),
        ).fetchone()
        if not cov:
            return False
    last_mention = conn.execute(
        "SELECT MAX(system_detection_ts) AS m FROM call_events WHERE mint = ?", (mint,)
    ).fetchone()["m"]
    return bool(last_mention and t - last_mention >= config.EXTENSION_IDLE_SEC)


def sampler_tick(conn, price_provider, max_quotes: int = 40) -> dict:
    """One pass over open call events. Commits per call so slow HTTP quotes never hold
    the write lock across the whole pass. Bounded work per tick."""
    open_calls = conn.execute(
        "SELECT id, mint, system_detection_ts FROM call_events WHERE monitoring_complete = 0 ORDER BY id ASC LIMIT 500"
    ).fetchall()
    stats = {"sampled": 0, "finalized": 0, "errors": 0, "late_marked": 0}
    from .pricing import PriceQuote, record_observation
    horizon_names = {label for _, label in HORIZON_LABELS}
    for call in open_calls:
        if stats["sampled"] >= max_quotes:
            break
        try:
            due = _due_labels(conn, call)
            for label, target in due:
                if label in horizon_names and now() - target > 300:
                    # target missed by >5 min (e.g. backlog after an outage): record an
                    # honest LATE_CAPTURE marker instead of re-quoting forever. Never
                    # substitute a current price for a missed historical endpoint.
                    record_observation(conn, call["mint"],
                                       PriceQuote(ok=False, source="engine", error_code="LATE_CAPTURE"),
                                       call_event_id=call["id"], horizon_label=label)
                    stats["late_marked"] += 1
                    continue
                if stats["sampled"] >= max_quotes:
                    break
                q = price_provider.quote(call["mint"], conn=conn)  # network I/O happens BEFORE any write
                record_observation(conn, call["mint"], q, call_event_id=call["id"], horizon_label=label)
                stats["sampled"] += 1
            if _monitoring_done(conn, call):
                finalize_call(conn, call["id"])
                stats["finalized"] += 1
            conn.commit()  # release the write lock after each call
        except Exception as e:  # never let one token kill the loop
            stats["errors"] += 1
            try:
                conn.rollback()
                log_error(conn, "sampler", "UNKNOWN_ERROR", f"call {call['id']}: {e!r}"[:500])
                conn.commit()
            except Exception:
                pass
    return stats


SOCIAL_TICK_SEC = 30.0
SOCIAL_MAX_MINTS_PER_TICK = 50


def social_tick(conn) -> dict:
    """Refresh virality scores, social state transitions and coordinated-cluster
    detection for recently active mints. Bounded work per tick; a failure on one
    mint is rolled back and logged, never kills the sampler."""
    from .virality import compute_virality
    from .clusters import detect_clusters
    t = now()
    # snapshotless mints first: a brand-new token gets its T0 virality snapshot on the
    # very next tick (~30s after first detection), so "virality at entry" is measurable
    mints = [
        r["mint"]
        for r in conn.execute(
            """SELECT c.mint, MAX(c.system_detection_ts) AS last_call,
                      (SELECT COUNT(*) FROM virality_snapshots v WHERE v.mint = c.mint) AS snaps
               FROM call_events c WHERE c.system_detection_ts > ? GROUP BY c.mint
               ORDER BY (snaps = 0) DESC, last_call DESC LIMIT ?""",
            (t - 3600, SOCIAL_MAX_MINTS_PER_TICK),
        ).fetchall()
    ]
    stats = {"mints": 0, "clusters_found": 0, "errors": 0}
    for mint in mints:
        try:
            compute_virality(conn, mint)  # snapshots components + updates social state + alerts
            new_clusters = detect_clusters(conn, mint)
            stats["clusters_found"] += len(new_clusters)
            stats["mints"] += 1
            conn.commit()  # release the write lock between mints
        except Exception as e:
            stats["errors"] += 1
            try:
                conn.rollback()
                log_error(conn, "social_tick", "UNKNOWN_ERROR", f"mint {mint[:12]}: {e!r}"[:500])
                conn.commit()
            except Exception:
                pass
    return stats


def sampler_loop(db_path: str, price_provider, interval: float = 2.0, stop: threading.Event | None = None):
    conn = connect(db_path)
    last_stats = 0.0
    last_social = 0.0
    last_paper = 0.0
    while not (stop and stop.is_set()):
        try:
            sampler_tick(conn, price_provider)
        except Exception as e:
            print(f"sampler tick failed: {e!r}", flush=True)
            try:
                conn.rollback()
            except Exception:
                pass
        if time.time() - last_social > SOCIAL_TICK_SEC:  # virality + clusters every ~30s
            try:
                social_tick(conn)
            except Exception as e:
                print(f"social tick failed: {e!r}", flush=True)
                conn.rollback()
            last_social = time.time()
        if time.time() - last_paper > 5.0:  # paper strategy: entries + trailing exits every ~5s
            try:
                from .paper_strategy import paper_tick
                paper_tick(conn)
                conn.commit()
            except Exception as e:
                print(f"paper tick failed: {e!r}", flush=True)
                try:
                    conn.rollback()
                except Exception:
                    pass
            last_paper = time.time()
        if time.time() - last_stats > 300:  # refresh derived stats every 5 min
            try:
                recompute_account_stats(conn)
                conn.commit()
            except Exception as e:
                print(f"stats refresh failed: {e!r}", flush=True)
                conn.rollback()
            last_stats = time.time()
        time.sleep(interval)
    conn.close()


def start_background_sampler(db_path: str, price_provider) -> threading.Thread:
    stop = threading.Event()
    t = threading.Thread(target=sampler_loop, args=(db_path, price_provider, 2.0, stop), daemon=True)
    t.stop_event = stop  # type: ignore[attr-defined]
    t.start()
    return t
