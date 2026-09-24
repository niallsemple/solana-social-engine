"""Paper strategy: repeat-call + low-liquidity cohort with a trailing exit.

Forward paper test of the one configuration the precursor analysis could NOT refute
(analysis/reports/precursor-20260923.md). Entry is fully ex-ante — only facts known at
detection time are used:

  ENTRY   position_in_cycle >= PAPER_MIN_POSITION_IN_CYCLE (a repeat caller of the token)
          AND entry liquidity < PAPER_MAX_LIQUIDITY_USD
          AND signal is fresh (detection within PAPER_ENTRY_WINDOW_SEC)
  EXIT    trailing stop: price <= peak * (1 - PAPER_TRAIL_PCT), peak anchored at entry
          OR hard exit at T + PAPER_HARD_EXIT_SEC at the latest observed price
          OR PRICE_STALE if the mint stops producing successful observations

Rules:
- paper only, no real orders, no extra API spend: prices come from the price_observations
  the sampler is already writing (T0/HF/endpoints/EXT) — never quoted on demand here.
- net returns use the same versioned cost model as call_events (net_of_costs), so paper
  PnL is comparable with the monitoring ledger.
- every entry/exit is appended as a JSON line to logs/paper_strategy.log — that file is
  the real-time signal feed a future live version would consume.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import config
from .analytics import ensure_default_cost_model, net_of_costs
from .db import now

STRATEGY_NAME = config.PAPER_STRATEGY
V2_STRATEGY_NAME = config.PAPER_V2_STRATEGY
LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "paper_strategy.log"
STALE_PRICE_SEC = 900.0  # no successful observation for 15 min -> exit, data is untrustworthy


def _log_signal(event: dict) -> None:
    """Append one JSON line to the signal feed. Logging must never crash the loop."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
    except Exception as e:
        print(f"paper signal log failed: {e!r}", flush=True)


def _cost_params(conn) -> tuple[int, dict]:
    cm_id = ensure_default_cost_model(conn)
    row = conn.execute("SELECT params_json FROM cost_models WHERE id = ?", (cm_id,)).fetchone()
    return cm_id, json.loads(row["params_json"])


def open_entries(conn, t: float, strategy: str = STRATEGY_NAME,
                 cooldown_sec: float | None = None) -> int:
    """Enter fresh eligible calls into one book. One paper trade per call event per book,
    one OPEN position per mint per book. If cooldown_sec is set (v2), a mint is skipped
    while it has a CLOSED trade in this book whose exit is newer than t - cooldown_sec."""
    cm_id, _ = _cost_params(conn)
    candidates = conn.execute(
        """SELECT c.id, c.mint, c.system_detection_ts, c.entry_price, c.position_in_cycle,
                  (SELECT p.liquidity_usd FROM price_observations p
                    WHERE p.call_event_id = c.id AND p.liquidity_usd IS NOT NULL
                    ORDER BY p.ts ASC LIMIT 1) AS liq
           FROM call_events c
           WHERE c.entry_price IS NOT NULL
             AND c.position_in_cycle IS NOT NULL
             AND c.position_in_cycle >= ?
             AND c.system_detection_ts >= ?          -- stale signals are not entries
             AND c.system_detection_ts <= ?          -- not from the future (clock skew guard)
             AND c.quality_status = 'OK'
             AND NOT EXISTS (SELECT 1 FROM paper_trades pt
                              WHERE pt.call_event_id = c.id AND pt.strategy = ?)
             AND NOT EXISTS (SELECT 1 FROM paper_trades pt
                              WHERE pt.mint = c.mint AND pt.strategy = ?
                                AND pt.status = 'OPEN')  -- one open position per mint per book
           ORDER BY c.id ASC LIMIT 50""",
        (config.PAPER_MIN_POSITION_IN_CYCLE, t - config.PAPER_ENTRY_WINDOW_SEC, t + 60,
         strategy, strategy),
    ).fetchall()
    opened = 0
    batch_mints: set[str] = set()  # NOT EXISTS runs before this pass's inserts — dedupe within batch
    cooldown_since = (t - cooldown_sec) if cooldown_sec else None
    for c in candidates:
        liq = c["liq"]
        if liq is None or liq <= 0 or liq >= config.PAPER_MAX_LIQUIDITY_USD:
            continue  # outside the cohort — skip silently, it ages out of the window
        if c["mint"] in batch_mints:
            continue  # already entered this mint earlier in the same pass
        if cooldown_since is not None:
            hot = conn.execute(
                """SELECT 1 FROM paper_trades
                   WHERE mint = ? AND strategy = ? AND status = 'CLOSED'
                     AND exit_ts IS NOT NULL AND exit_ts >= ? LIMIT 1""",
                (c["mint"], strategy, cooldown_since),
            ).fetchone()
            if hot:
                continue  # mint exited recently in this book — cooling down
        conn.execute(
            """INSERT INTO paper_trades
               (call_event_id, mint, strategy, entry_ts, entry_price, entry_liquidity_usd,
                position_in_cycle, status, peak_price, peak_ts, last_price, last_price_ts,
                cost_model_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c["id"], c["mint"], strategy, c["system_detection_ts"], c["entry_price"],
             liq, c["position_in_cycle"], "OPEN", c["entry_price"], c["system_detection_ts"],
             c["entry_price"], c["system_detection_ts"], cm_id, t),
        )
        _log_signal({"event": "ENTRY", "strategy": strategy, "ts": t,
                     "call_event_id": c["id"], "mint": c["mint"],
                     "entry_price": c["entry_price"], "liquidity_usd": liq,
                     "position_in_cycle": c["position_in_cycle"],
                     "trail_pct": config.PAPER_TRAIL_PCT,
                     "hard_exit_sec": config.PAPER_HARD_EXIT_SEC})
        batch_mints.add(c["mint"])
        opened += 1
    return opened


def _latest_price(conn, mint: str) -> tuple[float, float] | None:
    row = conn.execute(
        """SELECT price_usd, ts FROM price_observations
           WHERE mint = ? AND price_usd IS NOT NULL AND quality_status = 'OK'
           ORDER BY ts DESC LIMIT 1""",
        (mint,),
    ).fetchone()
    return (row["price_usd"], row["ts"]) if row else None


def update_open(conn, t: float) -> int:
    cm_id, params = _cost_params(conn)
    open_trades = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY id ASC LIMIT 500"
    ).fetchall()
    closed = 0
    for tr in open_trades:
        lp = _latest_price(conn, tr["mint"])
        if lp is None:
            continue  # no successful observation yet — wait
        price, price_ts = lp
        peak_price = tr["peak_price"] if tr["peak_price"] is not None else tr["entry_price"]
        peak_ts = tr["peak_ts"] if tr["peak_ts"] is not None else tr["entry_ts"]
        if price > peak_price:
            peak_price, peak_ts = price, price_ts
        conn.execute(
            "UPDATE paper_trades SET peak_price = ?, peak_ts = ?, last_price = ?, last_price_ts = ? WHERE id = ?",
            (peak_price, peak_ts, price, price_ts, tr["id"]),
        )
        exit_reason = None
        if t - price_ts > STALE_PRICE_SEC:
            exit_reason = "PRICE_STALE"
        elif t - tr["entry_ts"] >= config.PAPER_HARD_EXIT_SEC:
            exit_reason = "HARD_EXIT_60M"
        elif price <= peak_price * (1.0 - config.PAPER_TRAIL_PCT):
            exit_reason = "TRAILING_STOP"
        if exit_reason is None:
            continue
        gross = price / tr["entry_price"] - 1.0
        net = net_of_costs(gross, params, notional_usd=config.PAPER_NOTIONAL_USD)
        conn.execute(
            """UPDATE paper_trades
               SET status = 'CLOSED', exit_ts = ?, exit_price = ?, exit_reason = ?,
                   gross_return = ?, net_return = ?, cost_model_id = ?, closed_at = ?
               WHERE id = ?""",
            (t, price, exit_reason, gross, net, cm_id, t, tr["id"]),
        )
        _log_signal({"event": "EXIT", "strategy": tr["strategy"], "ts": t,
                     "paper_trade_id": tr["id"], "call_event_id": tr["call_event_id"],
                     "mint": tr["mint"], "reason": exit_reason,
                     "entry_price": tr["entry_price"], "exit_price": price,
                     "peak_price": peak_price, "hold_sec": round(t - tr["entry_ts"], 1),
                     "gross_return": round(gross, 6),
                     "net_return": round(net, 6) if net is not None else None})
        closed += 1
    return closed


def paper_tick(conn) -> dict:
    """One bounded pass: open fresh entries, then update/close open trades.
    Failures are rolled back by the caller's guard, same contract as sampler_tick."""
    t = now()
    # Two parallel books on the same signals: v1 control (no re-entry limit, kept as
    # the A/B baseline) and v2 (per-mint cooldown after each exit).
    opened = open_entries(conn, t)
    opened += open_entries(conn, t, strategy=V2_STRATEGY_NAME,
                           cooldown_sec=config.PAPER_V2_COOLDOWN_SEC)
    closed = update_open(conn, t)
    return {"opened": opened, "closed": closed}


def _book_report(conn, strategy: str, t: float, cooldown_note: str = "") -> list[str]:
    open_rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'OPEN' AND strategy = ? ORDER BY entry_ts DESC",
        (strategy,),
    ).fetchall()
    closed_rows = conn.execute(
        "SELECT * FROM paper_trades WHERE status = 'CLOSED' AND strategy = ? ORDER BY exit_ts DESC",
        (strategy,),
    ).fetchall()
    lines = [f"OPEN ({len(open_rows)})"]
    for tr in open_rows[:20]:
        lp = tr["last_price"] or tr["entry_price"]
        unreal = lp / tr["entry_price"] - 1.0
        age_m = (t - tr["entry_ts"]) / 60
        lines.append(
            f"  #{tr['id']} {tr['mint'][:8]}… entry {tr['entry_price']:.8g} last {lp:.8g} "
            f"({unreal:+.1%}) age {age_m:.0f}m peak {tr['peak_price']:.8g}"
        )
    n = len(closed_rows)
    lines.append("")
    lines.append(f"CLOSED ({n})")
    if n:
        nets = [r["net_return"] for r in closed_rows if r["net_return"] is not None]
        wins = sum(1 for r in nets if r > config.FLAT_TOLERANCE)
        flats = sum(1 for r in nets if abs(r) <= config.FLAT_TOLERANCE)
        mean_net = sum(nets) / len(nets) if nets else 0.0
        compounded = 1.0
        for r in nets:
            compounded *= 1.0 + r
        by_reason: dict[str, int] = {}
        for r in closed_rows:
            by_reason[r["exit_reason"]] = by_reason.get(r["exit_reason"], 0) + 1
        lines.append(
            f"  win rate {wins}/{len(nets)} ({wins/len(nets):.1%}), flats {flats}, "
            f"mean net {mean_net:+.2%}, compounded x{compounded:.4f}"
        )
        lines.append(f"  exits: {by_reason}")
        lines.append("  last 10:")
        for tr in closed_rows[:10]:
            lines.append(
                f"  #{tr['id']} {tr['mint'][:8]}… {tr['exit_reason']:<13} "
                f"net {tr['net_return']:+.2%} gross {tr['gross_return']:+.2%} "
                f"hold {(tr['exit_ts']-tr['entry_ts'])/60:.0f}m"
            )
    return lines


def status_report(conn) -> str:
    """Human-readable paper account summary for `run.py paper-status` — one section per book."""
    t = now()
    lines = [f"PAPER STRATEGIES  (as of {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))})",
             f"rules: position_in_cycle>={config.PAPER_MIN_POSITION_IN_CYCLE}, "
             f"liq<${config.PAPER_MAX_LIQUIDITY_USD:,.0f}, trail {config.PAPER_TRAIL_PCT:.0%}, "
             f"hard exit {config.PAPER_HARD_EXIT_SEC/60:.0f}m, notional ${config.PAPER_NOTIONAL_USD:,.0f}",
             ""]
    strategies = [r["strategy"] for r in conn.execute(
        "SELECT DISTINCT strategy FROM paper_trades ORDER BY strategy").fetchall()]
    for s in ([STRATEGY_NAME, V2_STRATEGY_NAME] + [x for x in strategies
                                                   if x not in (STRATEGY_NAME, V2_STRATEGY_NAME)]):
        label = f"=== {s} ==="
        if s == V2_STRATEGY_NAME:
            label += f"  (cooldown {config.PAPER_V2_COOLDOWN_SEC/3600:.0f}h per mint)"
        lines.append(label)
        lines.extend(_book_report(conn, s, t))
        lines.append("")
    return "\n".join(lines)
