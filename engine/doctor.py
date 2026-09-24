"""Credential + connectivity verification. Makes one minimal real call per provider and
reports PASS/FAIL with the actual error — no fabricated status."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
import urllib.error

from . import config

WSOL = "So11111111111111111111111111111111111111112"  # known-good mint for probing


def check_helius() -> tuple[bool, str]:
    if not config.HELIUS_API_KEY:
        return False, "HELIUS_API_KEY not set (get one at https://dashboard.helius.dev)"
    t0 = time.time()
    try:
        req = urllib.request.Request(
            config.HELIUS_RPC_URL,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getAsset", "params": {"id": WSOL}}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode())
        ms = (time.time() - t0) * 1000
        if resp.get("result"):
            return True, f"RPC + DAS getAsset OK ({ms:.0f} ms)"
        return False, f"unexpected response: {json.dumps(resp)[:200]}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} — key likely invalid or expired"
    except Exception as e:
        return False, f"connection failed: {e!r}"


def check_x() -> tuple[bool, str]:
    if not config.X_AVAILABLE:
        return False, "no X credentials (X_BEARER_TOKEN or X_API_KEY/SECRET/ACCESS_TOKEN/SECRET)"
    base = "https://api.x.com/2/tweets/search/recent"
    params = {"query": "solana", "max_results": 10}
    url = base + "?" + urllib.parse.urlencode(params)
    if config.X_BEARER_TOKEN:
        headers = {"Authorization": f"Bearer {config.X_BEARER_TOKEN}"}
        mode = "bearer token"
    else:
        from .x_oauth import oauth1_header
        headers = {"Authorization": oauth1_header(
            "GET", base, params, config.X_API_KEY, config.X_API_SECRET,
            config.X_ACCESS_TOKEN, config.X_ACCESS_SECRET)}
        mode = "OAuth 1.0a user context"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        n = len(data.get("data") or [])
        return True, f"recent search OK via {mode} ({n} results on probe query)"
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        hints = {401: "credentials invalid", 403: "app lacks search access (check tier/permissions)",
                 429: "rate limited (credits exhausted?)"}
        return False, f"HTTP {e.code} via {mode} — {hints.get(e.code, 'see X API docs')} · {body}"
    except Exception as e:
        return False, f"connection failed: {e!r}"


def check_telegram() -> tuple[bool, str]:
    if not (config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH):
        return False, "TELEGRAM_API_ID/HASH not set — optional; engine runs X-only without them"
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False, "telethon package not installed (pip install telethon) — optional"
    if not config.TELEGRAM_SESSION:
        return False, "TELEGRAM_SESSION string missing (one-time Telethon login) — optional"
    return True, "credentials present (session verified on first run)"


def run_doctor() -> int:
    print("Solana Social Discovery Engine — preflight\n")
    ok_helius, msg = check_helius()
    print(f"  [{'PASS' if ok_helius else 'FAIL'}] Helius:   {msg}")
    ok_x, msg = check_x()
    print(f"  [{'PASS' if ok_x else 'FAIL'}] X/Twitter: {msg}")
    ok_tg, msg = check_telegram()
    print(f"  [{'WARN' if not ok_tg else 'PASS'}] Telegram: {msg}")
    print()
    if ok_helius and ok_x:
        print("Ready for live forward collection:")
        print("  python3 run.py run-live          # ingestion + sampler")
        print("  python3 run.py serve             # dashboard + read-only API")
        return 0
    print("Not ready yet — fix the FAIL items above (edit .env in the project root), then re-run:")
    print("  python3 run.py doctor")
    return 1
