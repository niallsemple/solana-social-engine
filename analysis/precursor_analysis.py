#!/usr/bin/env python3
"""Ex-ante precursor analysis: what detection-time features separate coins that
later pump (MFE_60m >= +10%) from those that don't?

EXPLORATORY / hypothesis-generating only. Not pre-registered. Any 'hit' here
must be validated on future forward data before it counts as an edge.
"""
import sqlite3, math, statistics
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "engine_live.db"
PUMP_THRESHOLD = 0.10  # mfe_60m >= +10%

def mann_whitney_p(a, b):
    """Approximate MWU p-value via normal approximation with tie-ignored ranks."""
    import math
    n1, n2 = len(a), len(b)
    if n1 < 5 or n2 < 5:
        return None, None
    combined = [(v, 1) for v in a] + [(v, 0) for v in b]
    combined.sort(key=lambda x: x[0])
    ranks = [0.0] * len(combined)
    i = 0
    while i < len(combined):
        j = i
        while j < len(combined) and combined[j][0] == combined[i][0]:
            j += 1
        avg = (i + j - 1) / 2.0 + 1.0
        for k in range(i, j):
            ranks[k] = avg
        i = j
    r1 = sum(r for r, (_, g) in zip(ranks, combined) if g == 1)
    u1 = r1 - n1 * (n1 + 1) / 2.0
    mu = n1 * n2 / 2.0
    sigma = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12.0)
    z = (u1 - mu) / sigma
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    auc = u1 / (n1 * n2)  # rank-biserial: P(pumper feature > non-pumper feature)
    return p, auc

def quartile_lift(values, labels):
    """Pump rate in top vs bottom quartile of feature."""
    if len(values) < 40:
        return None
    pairs = sorted((v, l) for v, l in zip(values, labels))
    q = len(pairs) // 4
    bottom = pairs[:q]
    top = pairs[-q:]
    br = sum(l for _, l in bottom) / len(bottom)
    tr = sum(l for _, l in top) / len(top)
    return br, tr

def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()

    calls = cur.execute("""
        SELECT id, mint, system_detection_ts, entry_price, position_in_cycle,
               seconds_after_first_detection, price_before, price_before_ts,
               mfe_60m, gross_return_60m
        FROM call_events
        WHERE monitoring_complete=1 AND entry_price IS NOT NULL
          AND mfe_60m IS NOT NULL AND quality_status='OK'
    """).fetchall()

    # T0 market features
    t0 = {}
    for cid, mcap, liq, holders in cur.execute("""
        SELECT call_event_id, mcap_usd, liquidity_usd, holders
        FROM price_observations WHERE horizon_label='T0' AND price_usd IS NOT NULL
    """):
        if cid is not None:
            t0[cid] = (mcap, liq, holders)

    # Latest virality snapshot at/before detection per mint
    vir = {}
    for mint, ts, row in cur.execute("""
        SELECT mint, ts, unique_accounts||'|'||mentions_per_min||'|'||mention_accel||'|'||
               original_ratio||'|'||virality_score||'|'||time_compression||'|'||community_count
        FROM virality_snapshots ORDER BY mint, ts
    """):
        vir.setdefault(mint, []).append((ts, row))

    def vir_at(mint, ts):
        snaps = vir.get(mint)
        if not snaps:
            return None
        best = None
        for s_ts, row in snaps:
            if s_ts <= ts + 1:
                best = row
            else:
                break
        if best is None:
            return None
        parts = best.split("|")
        try:
            return [float(p) if p not in ("None", "") else None for p in parts]
        except ValueError:
            return None

    rows = []
    for (cid, mint, dts, entry, pos, secs_after, pbefore, pbefore_ts, mfe, g60) in calls:
        label = 1 if mfe >= PUMP_THRESHOLD else 0
        feat = {}
        feat["position_in_cycle"] = pos
        feat["seconds_after_first"] = secs_after
        feat["pre_trend"] = (entry / pbefore - 1.0) if (pbefore and pbefore > 0 and pbefore_ts and pbefore_ts < dts) else None
        m = t0.get(cid)
        feat["mcap_t0"] = m[0] if m else None
        feat["liq_t0"] = m[1] if m else None
        feat["holders_t0"] = m[2] if m else None
        v = vir_at(mint, dts)
        if v:
            feat["unique_accounts"] = v[0]
            feat["mentions_per_min"] = v[1]
            feat["mention_accel"] = v[2]
            feat["original_ratio"] = v[3]
            feat["virality_score"] = v[4]
            feat["time_compression"] = v[5]
            feat["community_count"] = v[6]
        rows.append((label, feat))

    n = len(rows)
    n_pump = sum(l for l, _ in rows)
    base = n_pump / n
    print(f"n={n}  pumpers={n_pump}  base_rate={base:.1%}\n")

    features = sorted({k for _, f in rows for k in f})
    results = []
    for fname in features:
        vals = [(f[fname], l) for l, f in rows if f.get(fname) is not None]
        if len(vals) < 40:
            results.append((fname, len(vals), None, None, None, None, None, None))
            continue
        a = [v for v, l in vals if l == 1]
        b = [v for v, l in vals if l == 0]
        p, auc = mann_whitney_p(a, b)
        med_a = statistics.median(a)
        med_b = statistics.median(b)
        lift = quartile_lift([v for v, _ in vals], [l for _, l in vals])
        results.append((fname, len(vals), med_a, med_b, p, auc, lift[0] if lift else None, lift[1] if lift else None))

    print(f"{'feature':<22}{'n':>6}{'med_pump':>12}{'med_rest':>12}{'p':>10}{'AUC':>7}{'botQ%':>8}{'topQ%':>8}")
    for r in results:
        fname, cnt, ma, mb, p, auc, bq, tq = r
        if p is None:
            print(f"{fname:<22}{cnt:>6}   (too few)")
        else:
            sig = " ***" if p < 0.003 else (" *" if p < 0.05 else "")
            print(f"{fname:<22}{cnt:>6}{ma:>12.3g}{mb:>12.3g}{p:>10.2e}{auc:>7.3f}{bq:>8.1%}{tq:>8.1%}{sig}")
    print("\n*** = survives Bonferroni (alpha 0.003); * = nominal p<0.05")
    print("AUC = P(feature higher for pumper); 0.5 = no signal")
    print("botQ%/topQ% = pump rate in bottom/top quartile of feature")
    con.close()

if __name__ == "__main__":
    main()
