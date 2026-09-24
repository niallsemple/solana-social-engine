"""Hypothesis registry + forward validation.

Every hypothesis is recorded BEFORE forward testing with an immutable boundary:
observations at/before discovery_dataset_end_time = DISCOVERY DATA; after = FORWARD
VALIDATION DATA. The boundary is never moved and history is never rewritten.
"""
from __future__ import annotations

import hashlib
import statistics

from .db import now


def register_hypothesis(conn, description: str, features_used: list[str],
                        expected_relationship: str, testing_method: str = "comparison_of_means",
                        minimum_sample: int = 30) -> str:
    boundary = now()
    hid = "H-" + hashlib.sha256(f"{description}|{boundary}".encode()).hexdigest()[:10].upper()
    import json
    conn.execute(
        """INSERT INTO hypotheses
           (hypothesis_id, description, created_at, discovery_dataset_end_time,
            features_used, expected_relationship, minimum_sample_requirement, testing_method)
           VALUES (?,?,?,?,?,?,?,?)""",
        (hid, description, boundary, boundary, json.dumps(features_used),
         expected_relationship, minimum_sample, testing_method),
    )
    return hid


def evaluate_hypothesis(conn, hypothesis_id: str, feature_filter_sql: str,
                        filter_params: tuple = ()) -> dict:
    """Compute stats on DISCOVERY and FORWARD sides of the immutable boundary.

    feature_filter_sql: extra WHERE clause fragment selecting calls that EXHIBIT the
    hypothesis feature (e.g. 'position_in_cycle >= 3 AND seconds_after_first_detection <= 300').
    Returns both sides; caller decides support/refutation with robust statistics."""
    h = conn.execute("SELECT * FROM hypotheses WHERE hypothesis_id = ?", (hypothesis_id,)).fetchone()
    if not h:
        return {"error": "hypothesis not found"}
    boundary = h["discovery_dataset_end_time"]
    out = {"hypothesis_id": hypothesis_id, "boundary": boundary}
    for label, cond in (("DISCOVERY", "system_detection_ts <= ?"), ("FORWARD", "system_detection_ts > ?")):
        rows = conn.execute(
            f"""SELECT net_return_15m, gross_return_15m, mfe_15m, mae_15m FROM call_events
                WHERE monitoring_complete = 1 AND net_return_15m IS NOT NULL
                  AND {cond} AND ({feature_filter_sql})""",
            (boundary, *filter_params),
        ).fetchall()
        rets = [r["net_return_15m"] for r in rows]
        gross = [r["gross_return_15m"] for r in rows]
        wins = sum(1 for v in rets if v > 0)
        gwins = sum(1 for v in gross if v > 0)
        ci_lo = ci_hi = None
        if len(rets) >= 2:
            m = statistics.fmean(rets)
            se = statistics.stdev(rets) / len(rets) ** 0.5
            ci_lo, ci_hi = m - 1.96 * se, m + 1.96 * se
        stats = {
            "sample_size": len(rets),
            "gross_win_rate": gwins / len(gross) if gross else None,
            "net_win_rate": wins / len(rets) if rets else None,
            "avg_return": statistics.fmean(rets) if rets else None,
            "median_return": statistics.median(rets) if rets else None,
            "avg_mfe": statistics.fmean([r["mfe_15m"] for r in rows if r["mfe_15m"] is not None]) if rows else None,
            "avg_mae": statistics.fmean([r["mae_15m"] for r in rows if r["mae_15m"] is not None]) if rows else None,
            "ci95_low": ci_lo, "ci95_high": ci_hi,
        }
        conn.execute(
            """INSERT INTO hypothesis_results
               (hypothesis_id, computed_at, dataset, sample_size, gross_win_rate, net_win_rate,
                avg_return, median_return, avg_mfe, avg_mae, ci95_low, ci95_high)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (hypothesis_id, now(), label, stats["sample_size"], stats["gross_win_rate"],
             stats["net_win_rate"], stats["avg_return"], stats["median_return"],
             stats["avg_mfe"], stats["avg_mae"], ci_lo, ci_hi),
        )
        out[label.lower()] = stats

    fwd = out["forward"]
    if fwd["sample_size"] < h["minimum_sample_requirement"]:
        status = "INSUFFICIENT_DATA"
    elif fwd["net_win_rate"] and fwd["ci95_low"] and fwd["ci95_low"] > 0:
        status = "SUPPORTED"
    elif fwd["ci95_high"] is not None and fwd["ci95_high"] < 0:
        status = "REFUTED"
    else:
        status = "FORWARD_TESTING"
    conn.execute("UPDATE hypotheses SET status = ? WHERE hypothesis_id = ?", (status, hypothesis_id))
    out["status"] = status
    return out
