"""Edge scan: segment finalized calls by pre-trade features, compare outcomes.
Median + bootstrap CI everywhere. Read-only against the live DB. Associations != causation."""
import random
import sqlite3
import statistics

DB = "data/engine_live.db"
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA busy_timeout = 30000")

calls = conn.execute(
    """SELECT id, mint, platform, account_ref, system_detection_ts, position_in_cycle,
              seconds_after_first_detection, net_return_15m, net_return_60m, mfe_15m, mae_15m,
              classification_net
       FROM call_events
       WHERE monitoring_complete = 1 AND net_return_15m IS NOT NULL"""
).fetchall()
print(f"priceable finalized calls: {len(calls)}")

# social state of the mint nearest (<=10 min before) each call
state_rows = conn.execute(
    "SELECT mint, ts, state FROM token_social_state ORDER BY mint, ts"
).fetchall()
states_by_mint: dict[str, list[tuple[float, str]]] = {}
for r in state_rows:
    states_by_mint.setdefault(r["mint"], []).append((r["ts"], r["state"]))


def state_at(mint: str, t: float) -> str:
    hist = states_by_mint.get(mint, [])
    best = None
    for ts, st in hist:
        if ts <= t:
            best = st
        else:
            break
    return best or "UNKNOWN"


def boot_ci(vals, iters=2000):
    if len(vals) < 10:
        return (float("nan"), float("nan"))
    meds = []
    for _ in range(iters):
        meds.append(statistics.median(random.choices(vals, k=len(vals))))
    meds.sort()
    return (meds[int(0.025 * iters)], meds[int(0.975 * iters)])


def report(name, subset):
    rets = [c["net_return_15m"] for c in subset]
    if not rets:
        return
    wins = sum(1 for r in rets if r > 1e-4)
    med = statistics.median(rets)
    lo, hi = boot_ci(rets)
    m60 = [c["net_return_60m"] for c in subset if c["net_return_60m"] is not None]
    med60 = statistics.median(m60) if m60 else float("nan")
    print(
        f"{name:<28} n={len(rets):>5}  win%={100*wins/len(rets):>5.1f}  "
        f"med15m={100*med:>+7.2f}%  CI95=[{100*lo:>+6.2f},{100*hi:>+6.2f}]  med60m={100*med60:>+7.2f}%"
    )


print("\n--- baseline (all priceable calls) ---")
report("ALL", calls)

print("\n--- by position in mention cycle (1 = first caller of the token) ---")
for label, lo, hi in [("1 (first)", 1, 1), ("2-3", 2, 3), ("4-10", 4, 10), ("11+", 11, 10**9)]:
    report(label, [c for c in calls if c["position_in_cycle"] and lo <= c["position_in_cycle"] <= hi])

print("\n--- by seconds after first detection of the token ---")
for label, lo, hi in [("<60s", 0, 60), ("1-5 min", 60, 300), ("5-15 min", 300, 900), ("15-60 min", 900, 3600), (">60 min", 3600, 10**12)]:
    report(label, [c for c in calls if c["seconds_after_first_detection"] is not None and lo <= c["seconds_after_first_detection"] < hi])

print("\n--- by social state at detection ---")
for st in ["ISOLATED", "EMERGING", "ACCELERATING", "COORDINATED", "VIRAL", "UNKNOWN"]:
    report(st, [c for c in calls if state_at(c["mint"], c["system_detection_ts"]) == st])

print("\n--- by platform ---")
for p in ["x", "telegram"]:
    report(p, [c for c in calls if c["platform"] == p])

conn.close()
