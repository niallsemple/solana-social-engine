"""Daily edge monitor: hypothesis forward-validation + segment scan + spend check.
Deterministic; writes a markdown report and prints a compact summary for the cron agent."""
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.db import connect, now  # noqa: E402
from engine.hypotheses import evaluate_hypothesis  # noqa: E402

DB = "data/engine_live.db"

# Hypothesis filters (registered 2026-09-22; boundary immutable in hypotheses table)
HYPOTHESES = {
    "H-6FD9E601BF": ("Late entry decay (5-60 min after first detection)",
                     "seconds_after_first_detection BETWEEN 300 AND 3600", ()),
    "H-BE160B80BE": ("Fast-pump round-trip (MFE>=10% then hold)",
                     "mfe_15m >= 0.10", ()),
    "H-0B14531622": ("Coordinated underperformance (COORDINATED state at detection)",
                     "EXISTS (SELECT 1 FROM token_social_state s WHERE s.mint = call_events.mint "
                     "AND s.state = 'COORDINATED' AND s.ts <= call_events.system_detection_ts "
                     "AND s.ts > call_events.system_detection_ts - 600)", ()),
    "H-FC9B35BA00": ("Early + accelerating (pos<=3, mention_accel>0)",
                     "position_in_cycle <= 3 AND EXISTS (SELECT 1 FROM virality_snapshots v "
                     "WHERE v.mint = call_events.mint AND v.ts <= call_events.system_detection_ts "
                     "AND v.ts > call_events.system_detection_ts - 300 AND v.mention_accel > 0)", ()),
    "H-1816134498": ("Smart accounts (discovery-era top pumper callers)",
                     "account_ref IN ('SOLANACHAD009','DegenWar01','starofmartina','AlphaPulsevw')", ()),
    "H-0717F05C38": ("Liquidity sweet spot ($20k-$100k at detection)",
                     "(SELECT p.liquidity_usd FROM price_observations p WHERE p.mint = call_events.mint "
                     "AND p.liquidity_usd IS NOT NULL ORDER BY ABS(p.ts - call_events.system_detection_ts) LIMIT 1) "
                     "BETWEEN 20000 AND 100000", ()),
    "H-06985DC5FB": ("Virality at entry (first snapshot within 60s post-detection)",
                     "EXISTS (SELECT 1 FROM virality_snapshots v WHERE v.mint = call_events.mint "
                     "AND v.ts >= call_events.system_detection_ts "
                     "AND v.ts <= call_events.system_detection_ts + 60 "
                     "AND (v.mentions_per_min >= 1.0 OR v.mention_accel > 0))", ()),
}


def boot_ci(vals, iters=2000):
    if len(vals) < 10:
        return (float("nan"), float("nan"))
    meds = sorted(statistics.median(random.choices(vals, k=len(vals))) for _ in range(iters))
    return meds[int(0.025 * iters)], meds[int(0.975 * iters)]


def main():
    conn = connect(DB)
    conn.execute("PRAGMA busy_timeout = 30000")
    t = now()
    day_start = int(t // 86400) * 86400
    lines = []
    out = lines.append

    out(f"# Edge monitor — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

    # --- spend check ---
    x_today = conn.execute(
        "SELECT COUNT(*) AS n FROM api_usage WHERE provider='x' AND ts >= ?", (day_start,)
    ).fetchone()["n"]
    x_ok = conn.execute(
        "SELECT COUNT(*) AS n FROM api_usage WHERE provider='x' AND status='ok' AND ts >= ?", (day_start,)
    ).fetchone()["n"]
    posts_today = conn.execute(
        "SELECT COUNT(*) AS n FROM social_posts WHERE system_ts >= ?", (day_start,)
    ).fetchone()["n"]
    calls_today = conn.execute(
        "SELECT COUNT(*) AS n FROM call_events WHERE system_detection_ts >= ?", (day_start,)
    ).fetchone()["n"]
    out(f"## Pipeline health\n- X calls today: {x_today} (ok: {x_ok}) — budget 180/day\n"
        f"- Posts today: {posts_today} | new call events today: {calls_today}\n")

    # --- segment scan ---
    calls = conn.execute(
        """SELECT position_in_cycle AS pos, seconds_after_first_detection AS dt,
                  net_return_15m AS n15, net_return_60m AS n60, mfe_15m AS mfe, mae_15m AS mae
           FROM call_events WHERE monitoring_complete=1 AND net_return_15m IS NOT NULL"""
    ).fetchall()
    dead = [c for c in calls if (c["mfe"] or 0) < 0.005 and (c["mae"] or 0) > -0.005]
    movers = [c for c in calls if c not in dead]
    pumpers = [c for c in movers if (c["mfe"] or 0) >= 0.10]

    def seg(name, sub):
        rets = [c["n15"] for c in sub]
        if not rets:
            out(f"| {name} | 0 | — | — | — | — |")
            return
        med = statistics.median(rets)
        lo, hi = boot_ci(rets)
        win = 100 * sum(1 for r in rets if r > 1e-4) / len(rets)
        m60 = [c["n60"] for c in sub if c["n60"] is not None]
        med60 = statistics.median(m60) if m60 else float("nan")
        out(f"| {name} | {len(rets)} | {win:.1f}% | {100*med:+.2f}% | [{100*lo:+.2f},{100*hi:+.2f}] | {100*med60:+.2f}% |")

    out("## Segment scan (net returns, median + bootstrap CI95)\n"
        "| segment | n | win% | med 15m | CI95 | med 60m |\n|---|---|---|---|---|---|")
    seg("ALL priceable", calls)
    seg("dead/no-trade", dead)
    seg("movers", movers)
    seg("movers pos 1", [c for c in movers if c["pos"] == 1])
    seg("movers pos 2-3", [c for c in movers if c["pos"] and 2 <= c["pos"] <= 3])
    seg("movers pos 11+", [c for c in movers if c["pos"] and c["pos"] >= 11])
    seg("movers <60s", [c for c in movers if c["dt"] is not None and c["dt"] < 60])
    seg("movers 5-15min", [c for c in movers if c["dt"] is not None and 300 <= c["dt"] < 900])
    seg("movers 15-60min", [c for c in movers if c["dt"] is not None and 900 <= c["dt"] < 3600])
    seg("pumped >=10% in 15m", pumpers)
    out("")

    # --- per-token robustness (first call per mint; botnet-weighting guard) ---
    by_mint = {}
    for c in conn.execute(
        """SELECT mint, net_return_15m AS n15, mfe_15m AS mfe FROM call_events
           WHERE monitoring_complete=1 AND net_return_15m IS NOT NULL
           ORDER BY system_detection_ts ASC"""
    ).fetchall():
        by_mint.setdefault(c["mint"], c)
    toks = list(by_mint.values())
    tok_pump = 100 * sum(1 for c in toks if (c["mfe"] or 0) >= 0.10) / max(len(toks), 1)
    tok_win = 100 * sum(1 for c in toks if c["n15"] > 1e-4) / max(len(toks), 1)
    tok_med = statistics.median([c["n15"] for c in toks]) if toks else float("nan")
    out(f"## Per-token robustness (deduped, n={len(toks)})\n"
        f"- Baseline: win {tok_win:.1f}%, pump {tok_pump:.1f}%, median 15m {100*tok_med:+.2f}%\n"
        f"- Compare against per-call numbers above: big gaps mean heavily-called tokens are skewing the cut.\n")

    # --- reference-strategy paper ledger (forward-only, unit stake, first call per token) ---
    # Strategy: enter calls on tokens with $20k-$100k liquidity at detection, exit at T+10m
    # via HF path, net of the measured 3.54% round-trip cost floor. Tracks whether the
    # leading candidate is actually tradeable as a rule, not just a segment statistic.
    ledger_boundary = conn.execute(
        "SELECT discovery_dataset_end_time AS b FROM hypotheses WHERE hypothesis_id='H-0717F05C38'"
    ).fetchone()["b"]
    liq_sql = """(SELECT p.liquidity_usd FROM price_observations p WHERE p.mint = call_events.mint
                  AND p.liquidity_usd IS NOT NULL ORDER BY ABS(p.ts - call_events.system_detection_ts) LIMIT 1)"""
    fcalls = conn.execute(
        f"""SELECT mint, system_detection_ts AS t0, entry_price FROM call_events
            WHERE monitoring_complete=1 AND entry_price IS NOT NULL
              AND system_detection_ts > ? AND {liq_sql} BETWEEN 20000 AND 100000
            ORDER BY system_detection_ts""",
        (ledger_boundary,),
    ).fetchall()
    seen, trades, skipped = set(), [], 0
    for c in fcalls:
        if c["mint"] in seen:
            continue
        seen.add(c["mint"])
        px = conn.execute(
            """SELECT price_usd FROM price_observations WHERE mint=? AND price_usd IS NOT NULL
               AND ABS(ts - ?) <= 20 ORDER BY ABS(ts - ?) LIMIT 1""",
            (c["mint"], c["t0"] + 600, c["t0"] + 600),
        ).fetchone()
        if not px:
            skipped += 1
            continue
        trades.append(px["price_usd"] / c["entry_price"] - 1 - 0.0354)
    if trades:
        cum = 1.0
        for r in trades:
            cum *= 1 + r
        wins = sum(1 for r in trades if r > 0)
        out(f"## Reference strategy paper ledger (liq $20-100k, exit T+10m, unit stake)\n"
            f"- trades: {len(trades)} (skipped {skipped} without T+10m path quote)\n"
            f"- win rate: {100*wins/len(trades):.1f}% | median trade: {100*statistics.median(trades):+.2f}%\n"
            f"- cumulative multiplier: {cum:.3f}x ({100*(cum-1):+.1f}% on equal-weighted compounding)\n"
            f"- Research simulation, not advice; associations != causation.\n")
    else:
        out("## Reference strategy paper ledger\n- no forward in-band trades with path data yet.\n")

    # --- hypothesis forward validation ---
    boundary = conn.execute("SELECT MIN(discovery_dataset_end_time) AS b FROM hypotheses").fetchone()["b"]
    if boundary:
        pipe = conn.execute(
            """SELECT SUM(CASE WHEN monitoring_complete=0 THEN 1 ELSE 0 END) AS open_n,
                      SUM(CASE WHEN monitoring_complete=1 AND net_return_15m IS NOT NULL THEN 1 ELSE 0 END) AS priced15
               FROM call_events WHERE system_detection_ts > ?""",
            (boundary,),
        ).fetchone()
        out(f"## Forward pipeline (calls after earliest boundary)\n"
            f"- open/in-monitoring: {pipe['open_n'] or 0} | finalized with 15m price: {pipe['priced15'] or 0}\n")
    out("## Hypothesis status\n| id | hypothesis | status | forward n | fwd med 15m | fwd CI95 |\n|---|---|---|---|---|---|")
    alerts = []
    for hid, (label, filt, params) in HYPOTHESES.items():
        r = evaluate_hypothesis(conn, hid, filt, params)
        fwd = r.get("forward", {})
        ci = ""
        if fwd.get("ci95_low") is not None:
            ci = f"[{100*fwd['ci95_low']:+.2f},{100*fwd['ci95_high']:+.2f}]"
        med = f"{100*fwd['median_return']:+.2f}%" if fwd.get("median_return") is not None else "—"
        out(f"| {hid} | {label} | {r.get('status')} | {fwd.get('sample_size')} | {med} | {ci} |")
        if r.get("status") in ("SUPPORTED", "REFUTED"):
            alerts.append(f"{hid} ({label}) → {r['status']}")
    conn.commit()
    out("")
    if alerts:
        out("## ⚠ Status changes needing review\n" + "\n".join(f"- {a}" for a in alerts))
    else:
        out("## No hypothesis has reached a SUPPORTED/REFUTED verdict yet.")

    report_dir = Path("analysis/reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"edge-{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
    path.write_text("\n".join(lines))
    print("\n".join(lines))
    print(f"\nReport saved: {path}")
    conn.close()


if __name__ == "__main__":
    main()
