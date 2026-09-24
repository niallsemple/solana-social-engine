#!/usr/bin/env python3
"""Shadow v2 analysis: what would a per-mint cooldown have done to the v1 ledger?

v1 (live, untouched): enter every eligible repeat-call low-liq signal.
v2 (shadow):          same entries, but after ANY exit on a mint, no re-entry on
                       that mint for COOLDOWN_SEC. Entries already open are unaffected.

Both use the SAME recorded exits (entry timing is identical for kept trades; v1 exits
don't depend on other paper trades), so the v2 ledger is a strict subset of v1 trades.

Usage:  python3 analysis/shadow_v2_cooldown.py [cooldown_hours ...]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "engine_live.db"


def shadow(cooldown_sec: float) -> dict:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    trades = conn.execute(
        "SELECT id, mint, entry_ts, exit_ts, exit_reason, net_return "
        "FROM paper_trades ORDER BY entry_ts ASC, id ASC"
    ).fetchall()
    conn.close()

    kept, skipped = [], []
    last_exit_by_mint: dict[str, float] = {}
    for tr in trades:
        le = last_exit_by_mint.get(tr["mint"])
        if le is not None and tr["entry_ts"] - le < cooldown_sec:
            skipped.append(tr)
            continue
        kept.append(tr)
        if tr["exit_ts"] is not None:
            last_exit_by_mint[tr["mint"]] = tr["exit_ts"]

    def stats(rows):
        closed = [r for r in rows if r["net_return"] is not None]
        total = sum(r["net_return"] for r in closed) * 100
        wins = sum(1 for r in closed if r["net_return"] > 0.005)
        return {"trades": len(rows), "closed": len(closed),
                "net_pct": round(total, 1), "wins": wins}

    v1 = stats(trades)
    v2 = stats(kept)
    skip_stats = stats(skipped)
    return {"cooldown_h": cooldown_sec / 3600, "v1": v1, "v2": v2,
            "skipped": skip_stats,
            "skipped_mints": sorted({r["mint"][:8] for r in skipped})}


def main() -> None:
    hours = [float(a) for a in sys.argv[1:]] or [1.0, 4.0, 24.0, 1e9]
    for h in hours:
        r = shadow(h * 3600)
        label = "never re-enter" if h > 1e6 else f"{r['cooldown_h']:g}h"
        print(f"cooldown {label:>15} | v1: {r['v1']['closed']} closed, net {r['v1']['net_pct']:+.1f}% "
              f"| v2: {r['v2']['closed']} closed, net {r['v2']['net_pct']:+.1f}% "
              f"| skipped {r['skipped']['trades']} entries worth {r['skipped']['net_pct']:+.1f}%")
        if h <= 1e6:
            print(f"{'':>18} skipped mints: {', '.join(r['skipped_mints'][:12])}")


if __name__ == "__main__":
    main()
