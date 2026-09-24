"""Engine configuration. All tunables live here or in DB tables — never hard-coded deep in logic."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def _load_env_file() -> None:
    """Load KEY=VALUE lines from .env in the project root (never overrides real env vars).
    The .env file stays on this machine and is git-ignored."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env_file()

DB_PATH = os.environ.get("SSE_DB_PATH", str(DATA_DIR / "engine.db"))

# --- API credentials (optional; engine runs in replay/demo mode without them) ---
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_RPC_URL = os.environ.get(
    "HELIUS_RPC_URL",
    f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else "",
)
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")
# X OAuth 1.0a user context (used when no bearer token is set)
X_API_KEY = os.environ.get("X_API_KEY", "")
X_API_SECRET = os.environ.get("X_API_SECRET", "")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN", "")
X_ACCESS_SECRET = os.environ.get("X_ACCESS_SECRET", "")
X_OAUTH1_AVAILABLE = all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET])
X_AVAILABLE = bool(X_BEARER_TOKEN) or X_OAUTH1_AVAILABLE
TELEGRAM_SESSION = os.environ.get("TELEGRAM_SESSION", "")  # telethon session string
TELEGRAM_API_ID = os.environ.get("TELEGRAM_API_ID", "")
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")

# --- Sampling schedule ---
HORIZONS_SEC = [60, 300, 900, 1800, 3600]          # T+1m/5m/15m/30m/60m
HF_WINDOW_SEC = 900                                  # high-frequency window: first 15 min
HF_INTERVAL_SEC = float(os.environ.get("SSE_HF_INTERVAL", "7"))  # target 5-10s
EXTENSION_IDLE_SEC = 1800                            # extend monitoring while calls keep arriving; stop after 30m idle

# --- X spend control (search/recent is pay-per-use; measured ~£0.028/call on this account) ---
X_TERMS_PER_CYCLE = int(os.environ.get("SSE_X_TERMS_PER_CYCLE", "2"))   # paid queries per cycle
X_CYCLE_SEC = float(os.environ.get("SSE_X_CYCLE_SEC", "900"))           # 15 min between cycles
X_QUERY_GAP_SEC = float(os.environ.get("SSE_X_QUERY_GAP_SEC", "10"))    # spacing between term queries
X_DAILY_CALL_BUDGET = int(os.environ.get("SSE_X_DAILY_BUDGET", "180"))  # hard cap, ~£5/day; 0 = unlimited

# --- Paper strategy: repeat-call + low-liquidity cohort with trailing exit ---
# Ex-ante rules from precursor analysis (analysis/reports/precursor-20260923.md): pumpers
# separate at detection (later position in cycle, lower liquidity) but no entry filter has
# positive fixed-horizon expectancy; winners peak ~T+30m and keep ~57% of MFE at T+60m,
# so the unrefuted configuration is EXIT TIMING on this cohort. Forward paper test only —
# no real orders, no extra API spend (reuses price_observations the sampler already takes).
PAPER_STRATEGY = os.environ.get("SSE_PAPER_STRATEGY", "repeat_lowliq_trail_v1")
PAPER_MIN_POSITION_IN_CYCLE = int(os.environ.get("SSE_PAPER_MIN_POS", "2"))   # repeat callers only
PAPER_MAX_LIQUIDITY_USD = float(os.environ.get("SSE_PAPER_MAX_LIQ", "30000"))
PAPER_TRAIL_PCT = float(os.environ.get("SSE_PAPER_TRAIL", "0.30"))            # stop = 30% off peak
PAPER_HARD_EXIT_SEC = float(os.environ.get("SSE_PAPER_HARD_EXIT", "3600"))    # flat exit at T+60m
PAPER_ENTRY_WINDOW_SEC = float(os.environ.get("SSE_PAPER_ENTRY_WINDOW", "300"))  # signals stale after 5m
PAPER_NOTIONAL_USD = float(os.environ.get("SSE_PAPER_NOTIONAL", "1000"))

# --- Paper v2: same entry cohort and exits, plus a per-mint re-entry cooldown ---
# Motivation: forward v1 showed serial re-entry on "zombie" mints (repeat-called but never
# moving) donates costs repeatedly; shadow replay (analysis/shadow_v2_cooldown.py) found
# every skipped re-entry lost money. v2 runs as a SEPARATE parallel paper book (strategy
# column distinguishes) so v1 stays untouched as the control in an A/B forward test.
PAPER_V2_STRATEGY = os.environ.get("SSE_PAPER_V2_STRATEGY", "repeat_lowliq_cooldown_v2")
PAPER_V2_COOLDOWN_SEC = float(os.environ.get("SSE_PAPER_V2_COOLDOWN", str(4 * 3600)))  # no re-entry on a mint within 4h of its v2 exit

# --- Discovery seed ---
SEED_TERMS = ["memecoins"]

# --- Classification ---
FLAT_TOLERANCE = 1e-4   # net |return| below this is FLAT (floating-point noise guard)

# --- Default cost model (versioned in DB; raw observations never altered) ---
DEFAULT_COST_MODEL = {
    "name": "default_v1",
    "dex_fee_bps": 30,            # per side
    "sol_tx_fee_lamports": 5000,
    "priority_fee_lamports": 50000,
    "slippage_bps": 100,          # estimated, per side
    "spread_bps": 50,             # half-spread, per side
    "entry_latency_ms": 1500,
    "exit_latency_ms": 1500,
    "sol_price_usd_assumed": None,  # if None, use SOL price captured at T0
}

# --- Virality default weights (stored in DB virality_models; re-estimated later empirically) ---
DEFAULT_VIRALITY_MODEL = {
    "name": "virality_v1",
    "weights": {
        "unique_accounts": 1.0,
        "unique_channels": 1.5,
        "mentions_per_min": 0.5,
        "mention_accel": 1.0,
        "original_ratio": 2.0,
        "community_count": 1.0,
        "log_follower_reach": 0.2,
        "tg_x_spread": 1.0,
        "time_compression": 1.0,
    },
}

# --- Social state thresholds (initial guesses; to be replaced by data-derived cutoffs) ---
SOCIAL_STATE_RULES = {
    "EMERGING_min_callers": 2,
    "ACCELERATING_mpm": 1.0,
    "VIRAL_mpm": 5.0,
    "VIRAL_min_unique": 10,
    "DECAYING_idle_sec": 900,
    "DEAD_idle_sec": EXTENSION_IDLE_SEC,
}

HTTP_HOST = os.environ.get("SSE_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("SSE_PORT", "7100"))
