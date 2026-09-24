# Solana Memecoin Social Discovery Engine

A **research-only** engine that monitors public X/Twitter posts and public Telegram channels,
detects Solana contract addresses (CAs), and measures what happens to the token's price at
T+1m / 5m / 15m / 30m / 60m after each mention — gross **and** net of realistic costs.

**No trading. No backfilled history mixed with forward data. T0 = the moment _this system_
first detects the contract.**

## Quick start

```bash
# 1. seed a clearly-labeled SIMULATED dataset and open the dashboard
python3 run.py demo --tokens 12
python3 run.py serve            # or: npm run dev
# → http://localhost:7100

# print the FIRST MILESTONE audit record for call #1
python3 run.py milestone 1
```

## Live operation (forward-only)

```bash
export HELIUS_API_KEY=...      # required: Solana data infrastructure
export X_BEARER_TOKEN=...      # required: X API v2 recent search
# optional: TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION (Telethon, public channels only)
python3 run.py run-live        # ingestion + sampler
python3 run.py serve --with-sampler
```

Without credentials the engine refuses to go live rather than fabricating data.
Use `python3 run.py replay posts.jsonl` to feed captured posts through the real pipeline.

## X spend control (added 2026-09-22)

X recent-search is pay-per-use (~£0.028/call measured on this account). The live loop is throttled:

- `SSE_X_TERMS_PER_CYCLE` (default 2) — paid queries per cycle, rotating through the
  yield-ranked term pool so all productive terms get coverage
- `SSE_X_CYCLE_SEC` (default 900) — 15 minutes between cycles
- `SSE_X_DAILY_BUDGET` (default 180) — hard cap; polling pauses until the next UTC day
  and logs a BUDGET_CAP event when reached. 0 = unlimited
- Every cycle writes a `cycle done: terms=[...] budget_used=N` heartbeat to the collector
  log; a missing heartbeat means a stall
- Budget counter starts at collector start (spend under older unthrottled code is history),
  then binds fully from the next UTC midnight onward

Measured outcome: discovery rate is driven by time-of-day, not poll cadence — the throttle
cost no measurable discovery or outcome quality (detection-latency analysis 2026-09-22).

## Edge research workflow (added 2026-09-22)

- `analysis/edge_scan.py` — segment finalized calls (position, timing, movers vs dead),
  medians + bootstrap CIs
- `analysis/daily_edge_monitor.py` — deterministic daily report: pipeline health, spend vs
  budget, segment scan, per-token robustness (botnet-weighting guard), forward pipeline
  counts, and hypothesis forward-validation status
- Hypotheses are pre-registered in the DB with an immutable discovery/forward boundary
  (`engine/hypotheses.py`); filters live in the monitor script. Seven registered
  2026-09-22: late-entry decay, fast-pump round-trip, coordinated underperformance,
  early+accelerating, smart accounts, liquidity sweet spot, virality at entry
- `analysis/watchlist.json` — rug suspects + smart-account watch list with update rules
- Scheduled monitors: "SSE · Daily Edge Monitor" (08:12 Europe/London) and
  "SSE · Evening Edge Check" (21:12 Europe/London), both running the monitor script

## Architecture

```
engine/
  cas.py         Solana mint extraction + base58 validation ($TICKER alone ≠ identity)
  ingest.py      post normalization, dedupe, first-seen (T0), call events, lexicon learning,
                 network growth (Telegram links discovered from X and vice versa)
  pricing.py     Helius provider (DAS getAsset + labeled market-price source), explicit
                 error codes (HELIUS_TOKEN_NOT_FOUND / NO_LIQUID_MARKET / …), failures kept
  sampler.py     ~7s high-frequency sampling for the first 15 min, horizon endpoints,
                 dynamic extension while mentions keep arriving (30 min idle = stop)
  analytics.py   gross/net returns, MFE/MAE, time-to-peak/trough, cost model
                 (versioned in cost_models; raw observations never altered), WIN/LOSS/FLAT/
                 UNPRICEABLE/DEAD/RUGGED classification, per-account stats + roles +
                 marginal-MFE effect (association, not causation)
  virality.py    virality components collected separately; score from versioned weights;
                 social state machine ISOLATED→EMERGING→ACCELERATING→VIRAL→SATURATED→DECAYING→DEAD
  clusters.py    coordinated-push detection (shared text/URLs/tight timing) → SOCIAL_CLUSTER_ID
  hypotheses.py  registry with immutable discovery/forward boundary; forward validation only
  api.py         read-only REST/JSON + LLM tool interface (/mcp/tools, /mcp/call)
  demo_seed.py   simulated dataset (source='simulated', quality_status='SIMULATED')
```

Every call event stores `original_post_timestamp`, `system_detection_timestamp`,
`price_capture_timestamp` and detection latency separately.

## LLM access (read-only)

```
GET  /mcp/tools                     list tools
POST /mcp/call  {"tool": "get_token_social_timeline", "arguments": {"contract": "<mint>"}}
```

Tools: `get_recent_discoveries, get_token, get_token_social_timeline, get_account,
get_account_calls, get_top_early_callers, get_active_social_pushes, get_viral_tokens,
get_social_clusters, get_cluster_performance, get_telegram_leaders, get_x_leaders,
search_calls, compare_accounts, compare_clusters, get_current_hypotheses,
get_forward_validation_results, query_statistics, get_overview, get_lexicon, get_alerts,
get_data_quality, get_milestone_record`.

REST equivalents under `/api/...` (see dashboard network calls). `/api/stats/query?sql=...`
accepts single SELECT statements only. Every statistic traces back to underlying call IDs.

## Research integrity rules implemented

- Win = `net_return_15m > 0` (with float-noise tolerance); gross classification kept separately
- Failed price observations recorded with error codes, never deleted, shown on the dashboard
- Copy vs. original posts distinguished (near-duplicate detection)
- Roles (DISCOVERER / EARLY / AMPLIFIER / LATE / PEAK / POST-PEAK) only after n ≥ 10 calls
- Hypotheses registered before testing; discovery/forward boundary never moves
- Median + 95% CI reported everywhere; 3/3 never counts as an edge
- Cost model is versioned data, not code — change it without touching raw observations

## Cost model defaults (`default_v1`, editable in DB)

DEX fee 30 bps/side · slippage 100 bps/side · half-spread 50 bps/side ·
Solana tx + priority fees amortized over $1,000 notional · entry/exit latency recorded.

## Known limitations (v0.1)

- Telegram ingestion requires Telethon + credentials (stubbed with clear errors otherwise)
- Token USD price comes from the best-liquidity DexScreener Solana pair (Helius has no
  mid-price endpoint); every observation is labeled with its source
- Control-group matching (random comparable memecoins) is schema-ready but not yet populated
- SQLite for local-first operation; schema is PostgreSQL-compatible for migration
