# Precursor Analysis — 2026-09-23 (~03:45 BST)

**Question:** among calls that later pump (MFE_60m ≥ +10%), what is observable
*at detection time* that separates them from calls that don't?
**Status:** EXPLORATORY, not pre-registered. Any candidate must be validated on
future forward data before counting as an edge.

Sample: 4,908 finalized calls with valid entry + MFE_60m; 623 pumpers (12.7% base rate).

## What separates pumpers (Mann-Whitney, Bonferroni α=0.003)

| Feature | Pumpers median | Others median | p | AUC |
|---|---|---|---|---|
| position_in_cycle | 5 | 2 | <1e-16 | 0.61 |
| seconds_after_first | 1,620s | 116s | 4e-08 | 0.57 |
| liquidity T0 | $29.5k | $36.5k | 0.002 | 0.46 (lower is better) |
| original_ratio (n=563) | 0.86 | 0.67 | 0.003 | 0.61 |
| time_compression (n=563) | 0.83 | 0.74 | 0.002 | 0.61 |
| pre_trend, mcap, unique_accounts, virality_score | — | — | n.s. | — |

Pump-rate lift is real: repeat calls pump 15.6% vs 6.4% for first calls;
repeat + liquidity<$30k pumps 18.8% vs 12.7% base.

## But it does NOT convert to terminal returns

| Cohort (ex-ante selectable) | n | pump rate | mean net 60m |
|---|---|---|---|
| All | 3,888 | 12.7% | −9.4% |
| First call | 1,112 | 6.4% | −7.2% |
| Repeat call | 2,776 | 15.6% | −10.3% |
| Repeat + liq<$30k | 963 | 18.8% | −10.9% |
| (same cohort, pumpers only — circular) | 184 | 100% | +14.7% |

**Every cohort selectable at detection time has negative mean net-60m
expectancy.** The only positive cohort requires knowing the future.

## Where the money goes

Among 536 pumpers: median 57% of MFE retained at T+60m, 56% still ≥+10% at
T+60m, median time-to-peak ≈ 30 min. Pumps are slow, not instant round-trips —
the losses come from the ~81% of calls that never pump, not from winners
vanishing before exit.

## Conclusion

1. **No ex-ante edge exists at the 60-minute terminal horizon** from current
   features. Higher pump-hit-rate cohorts lose *more*, not less.
2. The structural opening is **exit timing inside the horizon**: winners peak
   around T+30m and keep ~57% of MFE at T+60m. A trailing/stop-based exit on
   the repeat-call + low-liquidity cohort is the only untested configuration
   that the data does not already refute. This is now the leading
   hypothesis-generating candidate — it needs a paper-trading forward test
   with intra-horizon exits, NOT more backtesting.
3. Virality features (original_ratio, time_compression) separate pumpers but
   only n=563 so far; keep collecting before any conclusion.

*Analysis script: `analysis/precursor_analysis.py`. Note: an earlier quartile
table from the script sorted on (value,label) tuples, which segregates labels
within tied values — quartile lift figures for tie-heavy features were
discarded and re-verified directly in SQL.*
