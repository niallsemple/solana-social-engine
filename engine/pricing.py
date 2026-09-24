"""Price capture. Helius is the required infrastructure where supported; price quotes use
Jupiter/DexScreener reached through Helius RPC context and are labeled by source.

Every observation is recorded — successes AND failures. Failures use explicit error codes:
HELIUS_TOKEN_NOT_FOUND, HELIUS_PRICE_UNAVAILABLE, NO_LIQUID_MARKET, RPC_ERROR,
INVALID_MINT, POOL_NOT_FOUND, TOKEN_ALREADY_DEAD, PRICE_TIMEOUT, UNKNOWN_ERROR.
Missing data is never silently substituted.
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict

from . import config
from .cas import is_valid_mint
from .db import log_api, now


@dataclass
class PriceQuote:
    ok: bool
    price_usd: float | None = None
    sol_price_usd: float | None = None
    mcap_usd: float | None = None
    liquidity_usd: float | None = None
    supply: float | None = None
    holders: int | None = None
    pool: str | None = None
    source: str = "helius"
    error_code: str | None = None
    detail: str = ""


ERROR_CODES = [
    "HELIUS_TOKEN_NOT_FOUND", "HELIUS_PRICE_UNAVAILABLE", "NO_LIQUID_MARKET",
    "RPC_ERROR", "INVALID_MINT", "POOL_NOT_FOUND", "TOKEN_ALREADY_DEAD",
    "PRICE_TIMEOUT", "UNKNOWN_ERROR",
]


def _post_json(url: str, payload: dict, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get_json(url: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class HeliusProvider:
    """Helius DAS getAsset for supply/holders; DexScreener for market price/liquidity
    (labeled source='dexscreener' where used so provenance is auditable)."""

    name = "helius"

    def __init__(self, conn=None):
        # conn usage is same-thread only; callers in other threads pass their
        # own connection to quote(..., conn=...) for API logging.
        self.conn = conn
        if not config.HELIUS_API_KEY:
            raise RuntimeError("HELIUS_API_KEY not set")

    def quote(self, mint: str, conn=None) -> PriceQuote:
        if not is_valid_mint(mint):
            return PriceQuote(ok=False, source="helius", error_code="INVALID_MINT")
        q = PriceQuote(ok=False, source="helius")

        # 1) Helius DAS asset lookup
        t0 = time.time()
        try:
            resp = _post_json(config.HELIUS_RPC_URL, {
                "jsonrpc": "2.0", "id": 1, "method": "getAsset",
                "params": {"id": mint},
            })
            self._log_conn(conn or self.conn, "getAsset", "ok", t0)
        except urllib.error.URLError as e:
            self._log_conn(conn or self.conn, "getAsset", "rpc_error", t0)
            return PriceQuote(ok=False, source="helius", error_code="RPC_ERROR", detail=str(e)[:300])
        except TimeoutError:
            self._log_conn(conn or self.conn, "getAsset", "timeout", t0)
            return PriceQuote(ok=False, source="helius", error_code="PRICE_TIMEOUT")

        asset = resp.get("result")
        if not asset:
            return PriceQuote(ok=False, source="helius", error_code="HELIUS_TOKEN_NOT_FOUND")
        try:
            ti = asset.get("token_info") or {}
            supply_raw = ti.get("supply")
            decimals = ti.get("decimals") or 0
            if supply_raw is not None:
                q.supply = supply_raw / (10 ** decimals)
            # Helius DAS native price (USDC-denominated ≈ USD)
            pi = ti.get("price_info") or {}
            if pi.get("price_per_token") is not None:
                q.price_usd = float(pi["price_per_token"])
        except Exception:
            pass

        # Market data (liquidity / mcap / pool) via DexScreener; provenance labeled.
        t0 = time.time()
        try:
            ds = _get_json(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
            self._log_conn(conn or self.conn, "dexscreener/tokens", "ok", t0)
        except Exception as e:
            self._log_conn(conn or self.conn, "dexscreener/tokens", "error", t0)
            ds = {}
            if q.price_usd is None:
                return PriceQuote(ok=False, source="dexscreener", error_code="HELIUS_PRICE_UNAVAILABLE",
                                  detail=str(e)[:300], supply=q.supply)

        pairs = [p for p in (ds.get("pairs") or []) if p.get("chainId") == "solana"]
        if pairs:
            best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
            if q.price_usd is None:
                price = best.get("priceUsd")
                if price is not None:
                    q.price_usd = float(price)
                    q.source = "dexscreener"
            else:
                q.source = "helius+dexscreener"
            q.mcap_usd = best.get("marketCap") or best.get("fdv")
            q.liquidity_usd = float((best.get("liquidity") or {}).get("usd") or 0)
            q.pool = best.get("pairAddress")
            if q.liquidity_usd <= 0:
                q.error_code = "NO_LIQUID_MARKET"  # recorded alongside price — thin market is data
        if q.price_usd is None:
            err = "POOL_NOT_FOUND" if not pairs else "HELIUS_PRICE_UNAVAILABLE"
            return PriceQuote(ok=False, source=q.source, error_code=err, supply=q.supply)
        q.ok = True
        return q

    def _log_conn(self, conn, endpoint: str, status: str, t0: float) -> None:
        if conn is not None:
            log_api(conn, "helius", endpoint, status, (time.time() - t0) * 1000)


class SimulatedPriceProvider:
    """Deterministic simulated price path provider for replay/demo. Clearly labeled
    source='simulated'. Never used for live forward data."""

    name = "simulated"

    def __init__(self, paths: dict[str, list[tuple[float, float]]] | None = None):
        # paths: mint -> sorted [(ts, price)]; linear interpolation between points
        self.paths = paths or {}

    def quote(self, mint: str, conn=None) -> PriceQuote:
        if not is_valid_mint(mint):
            return PriceQuote(ok=False, source="simulated", error_code="INVALID_MINT")
        path = self.paths.get(mint)
        if not path:
            return PriceQuote(ok=False, source="simulated", error_code="HELIUS_TOKEN_NOT_FOUND")
        ts = now()
        price = _interp(path, ts)
        if price is None:
            return PriceQuote(ok=False, source="simulated", error_code="HELIUS_PRICE_UNAVAILABLE")
        return PriceQuote(ok=True, source="simulated", price_usd=price)


def _interp(path: list[tuple[float, float]], ts: float) -> float | None:
    if ts <= path[0][0]:
        return path[0][1]
    if ts >= path[-1][0]:
        return path[-1][1]
    for (t0, p0), (t1, p1) in zip(path, path[1:]):
        if t0 <= ts <= t1:
            f = (ts - t0) / max(t1 - t0, 1e-9)
            return p0 + f * (p1 - p0)
    return None


def record_observation(conn, mint: str, quote: PriceQuote, call_event_id: int | None = None,
                       horizon_label: str = "HF") -> int:
    """Persist every observation including failures. Never deletes; never substitutes."""
    cur = conn.execute(
        """INSERT INTO price_observations
           (mint, call_event_id, ts, horizon_label, price_usd, sol_price_usd, mcap_usd,
            liquidity_usd, supply, holders, pool, source, error_code, created_at, quality_status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (mint, call_event_id, now(), horizon_label, quote.price_usd, quote.sol_price_usd,
         quote.mcap_usd, quote.liquidity_usd, quote.supply, quote.holders, quote.pool,
         quote.source, quote.error_code, now(),
         "OK" if quote.ok else "PRICE_ERROR"),
    )
    return cur.lastrowid
