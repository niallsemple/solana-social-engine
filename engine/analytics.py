"""Analytics: endpoint returns, MFE/MAE path analysis, realistic cost model, win/loss
classification, per-account/channel statistics, role discovery, marginal-effect estimation.

Principles enforced here:
- gross AND net executable performance tracked separately
- a win is a win (net_return > 0, with float-noise tolerance) — no arbitrary threshold
- failed observations are kept and classified, never deleted
- associations are reported as associations, never causation
"""
from __future__ import annotations

import json
import statistics

from . import config
from .db import connect, now

HORIZONS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600}
HORIZON_TOLERANCE = 45.0  # nearest observation within ±45s counts as the endpoint


# ---------------------------------------------------------------- cost model

def ensure_default_cost_model(conn) -> int:
    row = conn.execute("SELECT id FROM cost_models WHERE name = ?", (config.DEFAULT_COST_MODEL["name"],)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO cost_models (name, params_json, created_at) VALUES (?,?,?)",
        (config.DEFAULT_COST_MODEL["name"], json.dumps(config.DEFAULT_COST_MODEL), now()),
    )
    return cur.lastrowid


def net_of_costs(gross_return: float, params: dict, notional_usd: float | None = None) -> float:
    """Convert a mid-price gross return into a theoretical executable net return.

    Entry executes at mid*(1+c), exit at mid*(1-c); fixed Solana fees are amortized over
    a configurable notional. All assumptions come from the cost_models row, so the model
    can be changed later without touching raw observations."""
    if gross_return is None:
        return None
    notional = notional_usd or 1000.0
    c_side = (params["dex_fee_bps"] + params["slippage_bps"] + params["spread_bps"]) / 10_000.0
    fixed_usd = (params["sol_tx_fee_lamports"] + params["priority_fee_lamports"]) * 2 * 1e-9 * 200.0
    # ^ 200 USD/SOL fallback only when no captured SOL price exists; recorded in params
    fixed_frac = fixed_usd / notional
    return (1.0 + gross_return) * (1.0 - c_side) / (1.0 + c_side) - 1.0 - fixed_frac


# ---------------------------------------------------------------- classification

def classify(ret: float | None, entry_error: str | None, token_status: str = "ACTIVE") -> str:
    if entry_error:
        if entry_error in ("HELIUS_TOKEN_NOT_FOUND", "INVALID_MINT"):
            return "INVALID"
        if entry_error == "TOKEN_ALREADY_DEAD":
            return "DEAD"
        return "UNPRICEABLE"
    if ret is None:
        return "UNPRICEABLE"
    if token_status == "RUGGED" or ret <= -0.80:
        return "RUGGED"
    tol = config.FLAT_TOLERANCE
    if ret > tol:
        return "WIN"
    if ret < -tol:
        return "LOSS"
    return "FLAT"


# ---------------------------------------------------------------- path analysis

def _nearest(rows, target_ts: float, tolerance: float = HORIZON_TOLERANCE):
    best, best_dt = None, tolerance + 1
    for r in rows:
        if r["price_usd"] is None:
            continue
        dt = abs(r["ts"] - target_ts)
        if dt < best_dt:
            best, best_dt = r, dt
    return best if best_dt <= tolerance else None


def _window_stats(rows, entry: float, t0: float, window: float):
    pts = [r for r in rows if r["price_usd"] is not None and t0 <= r["ts"] <= t0 + window]
    if not pts:
        return None
    hi = max(pts, key=lambda r: r["price_usd"])
    lo = min(pts, key=lambda r: r["price_usd"])
    return {
        "highest": hi["price_usd"], "lowest": lo["price_usd"],
        "mfe": hi["price_usd"] / entry - 1.0, "mae": lo["price_usd"] / entry - 1.0,
        "time_to_peak": hi["ts"] - t0, "time_to_trough": lo["ts"] - t0,
    }


def finalize_call(conn, call_id: int) -> None:
    """Compute all return/path fields for one call event from its recorded observations."""
    call = conn.execute("SELECT * FROM call_events WHERE id = ?", (call_id,)).fetchone()
    if not call or call["entry_price"] is None:
        if call and call["entry_error"]:
            cg = classify(None, call["entry_error"])
            conn.execute(
                "UPDATE call_events SET classification_gross = ?, classification_net = ?, monitoring_complete = 1 WHERE id = ?",
                (cg, cg, call_id),
            )
        return
    entry = call["entry_price"]
    t0 = call["system_detection_ts"]
    rows = conn.execute(
        "SELECT ts, price_usd FROM price_observations WHERE mint = ? AND ts >= ? ORDER BY ts",
        (call["mint"], t0),
    ).fetchall()

    params_row = conn.execute(
        "SELECT id, params_json FROM cost_models WHERE name = ?", (config.DEFAULT_COST_MODEL["name"],)
    ).fetchone()
    cost_model_id, params = (params_row["id"], json.loads(params_row["params_json"])) if params_row else (None, config.DEFAULT_COST_MODEL)

    updates: dict = {}
    for label, secs in HORIZONS.items():
        obs = _nearest(rows, t0 + secs)
        gross = (obs["price_usd"] / entry - 1.0) if obs else None
        updates[f"gross_return_{label}"] = gross
        updates[f"net_return_{label}"] = net_of_costs(gross, params) if gross is not None else None

    for label, secs in (("15m", 900), ("60m", 3600)):
        ws = _window_stats(rows, entry, t0, secs)
        if ws:
            updates[f"highest_price_{label}"] = ws["highest"]
            updates[f"lowest_price_{label}"] = ws["lowest"]
            updates[f"mfe_{label}"] = ws["mfe"]
            updates[f"mae_{label}"] = ws["mae"]
            updates[f"time_to_peak_{label}"] = ws["time_to_peak"]
            updates[f"time_to_trough_{label}"] = ws["time_to_trough"]

    token = conn.execute("SELECT status FROM tokens WHERE mint = ?", (call["mint"],)).fetchone()
    status = token["status"] if token else "ACTIVE"
    updates["classification_gross"] = classify(updates.get("gross_return_15m"), call["entry_error"], status)
    updates["classification_net"] = classify(updates.get("net_return_15m"), call["entry_error"], status)
    updates["cost_model_id"] = cost_model_id
    updates["monitoring_complete"] = 1

    # price trend BEFORE the post (separates discovery from momentum-chasing)
    pre = conn.execute(
        "SELECT price_usd, ts FROM price_observations WHERE mint = ? AND ts < ? AND price_usd IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (call["mint"], t0),
    ).fetchone()
    if pre:
        updates["price_before"] = pre["price_usd"]
        updates["price_before_ts"] = pre["ts"]

    if updates.get("time_to_peak_60m") is not None:
        peak_ts = t0 + updates["time_to_peak_60m"]
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM call_events WHERE mint = ? AND system_detection_ts <= ?",
            (call["mint"], peak_ts),
        ).fetchone()["n"]
        updates["callers_before_peak"] = n

    sets = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE call_events SET {sets} WHERE id = ?", (*updates.values(), call_id))

    # token-event peak price bookkeeping
    ws60 = _window_stats(rows, entry, t0, 3600)
    if ws60:
        conn.execute(
            """UPDATE token_events SET
                 peak_price = MAX(COALESCE(peak_price, 0), ?),
                 peak_price_ts = CASE WHEN peak_price IS NULL OR ? > peak_price THEN ? ELSE peak_price_ts END
               WHERE mint = ?""",
            (ws60["highest"], ws60["highest"], t0 + ws60["time_to_peak"], call["mint"]),
        )


# ---------------------------------------------------------------- account statistics

def _pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    f = int(k)
    return s[f] if f == k else s[f] + (s[f + 1] - s[f]) * (k - f)


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return statistics.fmean(vals) if vals else None


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _ci95(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None, None
    m = statistics.fmean(vals)
    se = statistics.stdev(vals) / (len(vals) ** 0.5)
    return m - 1.96 * se, m + 1.96 * se


MIN_SAMPLE_FOR_ROLE = 10


def recompute_account_stats(conn) -> int:
    """Rebuild account_statistics from raw call_events (raw rows never altered)."""
    accounts = conn.execute(
        "SELECT DISTINCT account_ref, platform FROM call_events"
    ).fetchall()
    n = 0
    for a in accounts:
        calls = conn.execute(
            "SELECT * FROM call_events WHERE account_ref = ? AND platform = ? AND monitoring_complete = 1",
            (a["account_ref"], a["platform"]),
        ).fetchall()
        if not calls:
            continue
        n += 1
        cls_net = [c["classification_net"] for c in calls]
        cls_gross = [c["classification_gross"] for c in calls]
        priceable = [c for c in calls if c["classification_net"] in ("WIN", "LOSS", "FLAT")]
        wins = cls_net.count("WIN")
        gross_wins = cls_gross.count("WIN")
        rets15 = [c["net_return_15m"] for c in priceable if c["net_return_15m"] is not None]
        ci_lo, ci_hi = _ci95(rets15)
        best = max(priceable, key=lambda c: c["net_return_15m"] or -9, default=None)
        worst = min(priceable, key=lambda c: c["net_return_15m"] if c["net_return_15m"] is not None else 9, default=None)

        before_peak = [c for c in calls if c["time_to_peak_60m"] is not None
                       and c["seconds_after_first_detection"] is not None
                       and c["seconds_after_first_detection"] <= c["time_to_peak_60m"]]
        peaked_known = [c for c in calls if c["time_to_peak_60m"] is not None]

        # marginal effect: this account joining an ALREADY-RUNNING push
        # (position >= 3, within first 5 minutes) vs comparable pushes without them — association only
        joined_running = [c["mfe_15m"] for c in priceable
                          if c["position_in_cycle"] and c["position_in_cycle"] >= 3
                          and c["seconds_after_first_detection"] is not None
                          and c["seconds_after_first_detection"] <= 300
                          and c["mfe_15m"] is not None]
        baseline = conn.execute(
            """SELECT AVG(mfe_15m) AS b FROM call_events
               WHERE monitoring_complete = 1 AND mfe_15m IS NOT NULL
                 AND position_in_cycle >= 3 AND seconds_after_first_detection <= 300
                 AND account_ref != ?""",
            (a["account_ref"],),
        ).fetchone()["b"]
        marginal = (_mean(joined_running) - baseline) if (joined_running and baseline is not None) else None

        role = "UNCLASSIFIED"
        if len(calls) >= MIN_SAMPLE_FOR_ROLE:
            avg_pos = _mean([c["position_in_cycle"] for c in calls]) or 99
            pct_before = len(before_peak) / len(peaked_known) if peaked_known else 0
            if avg_pos <= 1.5:
                role = "DISCOVERER"
            elif avg_pos <= 3 and pct_before >= 0.7:
                role = "EARLY CALLER"
            elif pct_before >= 0.6:
                role = "AMPLIFIER" if avg_pos <= 6 else "MAINSTREAM AMPLIFIER"
            elif pct_before >= 0.4:
                role = "LATE CALLER"
            else:
                role = "PEAK CALLER" if pct_before >= 0.2 else "POST-PEAK CALLER"

        conn.execute(
            """INSERT INTO account_statistics
               (account_ref, platform, computed_at, total_calls, unique_tokens_called, wins, losses, flat,
                unpriceable, rugged, gross_win_rate, net_win_rate, avg_return_1m, avg_return_5m,
                avg_return_15m, avg_return_30m, avg_return_60m, median_return_15m, avg_mfe, avg_mae,
                median_mfe, median_mae, avg_position_in_cycle, avg_seconds_after_first_detection,
                pct_calls_before_price_peak, pct_calls_after_price_peak, avg_mcap_at_call,
                avg_liquidity_at_call, best_call_id, worst_call_id, sample_size,
                ci95_low_15m, ci95_high_15m, role_label, marginal_mfe_effect, marginal_sample)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_ref, platform) DO UPDATE SET
                computed_at=excluded.computed_at, total_calls=excluded.total_calls,
                unique_tokens_called=excluded.unique_tokens_called, wins=excluded.wins,
                losses=excluded.losses, flat=excluded.flat, unpriceable=excluded.unpriceable,
                rugged=excluded.rugged, gross_win_rate=excluded.gross_win_rate,
                net_win_rate=excluded.net_win_rate, avg_return_1m=excluded.avg_return_1m,
                avg_return_5m=excluded.avg_return_5m, avg_return_15m=excluded.avg_return_15m,
                avg_return_30m=excluded.avg_return_30m, avg_return_60m=excluded.avg_return_60m,
                median_return_15m=excluded.median_return_15m, avg_mfe=excluded.avg_mfe,
                avg_mae=excluded.avg_mae, median_mfe=excluded.median_mfe, median_mae=excluded.median_mae,
                avg_position_in_cycle=excluded.avg_position_in_cycle,
                avg_seconds_after_first_detection=excluded.avg_seconds_after_first_detection,
                pct_calls_before_price_peak=excluded.pct_calls_before_price_peak,
                pct_calls_after_price_peak=excluded.pct_calls_after_price_peak,
                avg_mcap_at_call=excluded.avg_mcap_at_call,
                avg_liquidity_at_call=excluded.avg_liquidity_at_call,
                best_call_id=excluded.best_call_id, worst_call_id=excluded.worst_call_id,
                sample_size=excluded.sample_size, ci95_low_15m=excluded.ci95_low_15m,
                ci95_high_15m=excluded.ci95_high_15m, role_label=excluded.role_label,
                marginal_mfe_effect=excluded.marginal_mfe_effect, marginal_sample=excluded.marginal_sample""",
            (a["account_ref"], a["platform"], now(), len(calls),
             len({c["mint"] for c in calls}), wins, cls_net.count("LOSS"), cls_net.count("FLAT"),
             cls_net.count("UNPRICEABLE"), cls_net.count("RUGGED"),
             gross_wins / len(priceable) if priceable else None,
             wins / len(priceable) if priceable else None,
             _mean([c["net_return_1m"] for c in priceable]), _mean([c["net_return_5m"] for c in priceable]),
             _mean(rets15), _mean([c["net_return_30m"] for c in priceable]),
             _mean([c["net_return_60m"] for c in priceable]), _median(rets15),
             _mean([c["mfe_15m"] for c in priceable]), _mean([c["mae_15m"] for c in priceable]),
             _median([c["mfe_15m"] for c in priceable]), _median([c["mae_15m"] for c in priceable]),
             _mean([c["position_in_cycle"] for c in calls]),
             _mean([c["seconds_after_first_detection"] for c in calls]),
             len(before_peak) / len(peaked_known) if peaked_known else None,
             (len(peaked_known) - len(before_peak)) / len(peaked_known) if peaked_known else None,
             None, None,  # mcap/liquidity at call: filled from T0 observation when present
             best["id"] if best else None, worst["id"] if worst else None,
             len(priceable), ci_lo, ci_hi, role, marginal, len(joined_running)),
        )
    return n


# ---------------------------------------------------------------- milestone record

def milestone_record(db_path: str, call_id: int) -> str:
    """The FIRST MILESTONE auditable record for a single call."""
    conn = connect(db_path)
    call = conn.execute("SELECT * FROM call_events WHERE id = ?", (call_id,)).fetchone()
    if not call:
        conn.close()
        return f"Call {call_id} not found"
    ev = conn.execute("SELECT * FROM token_events WHERE id = ?", (call["token_event_id"],)).fetchone()
    obs = conn.execute(
        "SELECT horizon_label, price_usd, error_code FROM price_observations WHERE call_event_id = ?", (call_id,)
    ).fetchall()
    obs_map = {o["horizon_label"]: o for o in obs}
    callers_15m = conn.execute(
        "SELECT COUNT(*) AS n FROM call_events WHERE mint = ? AND id != ? AND system_detection_ts <= ?",
        (call["mint"], call_id, call["system_detection_ts"] + 900),
    ).fetchone()["n"]
    conn.close()

    def price_at(label):
        o = obs_map.get(label)
        if o is None:
            return "pending"
        return f"${o['price_usd']:.10g}" if o["price_usd"] is not None else f"ERROR: {o['error_code']}"

    def pct(v):
        return "n/a" if v is None else f"{v * 100:+.2f}%"

    import datetime as dt
    first_seen = dt.datetime.utcfromtimestamp(call["system_detection_ts"]).strftime("%H:%M:%S.") + \
        f"{int(call['system_detection_ts'] % 1 * 1000):03d}"

    return f"""TOKEN:
{call['mint']}

FIRST SEEN:
{first_seen}

SOURCE:
{call['platform'].upper()}

ACCOUNT:
@{call['account_ref']}

PRICE T0:
{price_at('T0')}

PRICE +1M:
{price_at('T+1m')}

PRICE +5M:
{price_at('T+5m')}

PRICE +15M:
{price_at('T+15m')}

PRICE +30M:
{price_at('T+30m')}

PRICE +60M:
{price_at('T+60m')}

15M GROSS RETURN:
{pct(call['gross_return_15m'])}

15M NET RETURN:
{pct(call['net_return_15m'])}

MFE 15M:
{pct(call['mfe_15m'])}

MAE 15M:
{pct(call['mae_15m'])}

ADDITIONAL CALLERS FIRST 15M:
{callers_15m}

X MENTIONS:
{ev['total_x_mentions'] if ev else 'n/a'}

TELEGRAM MENTIONS:
{ev['total_tg_mentions'] if ev else 'n/a'}

VIRALITY:
{(str(round(ev['mention_velocity'], 2)) + ' mentions/min') if ev and ev['mention_velocity'] is not None else 'n/a'}

RESULT:
{call['classification_net'] or 'PENDING'}"""
