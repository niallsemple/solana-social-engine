"""Solana contract-address (mint) extraction and validation, plus ticker/URL/telegram-link extraction.

The contract address is the primary token identifier. $TICKER alone never establishes identity.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_SET = set(B58_ALPHABET)

# 32-44 char base58 candidates (Solana mints are 32-byte ed25519-ish pubkeys)
CA_CANDIDATE_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,15})\b")
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
TG_LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,64})", re.IGNORECASE)
PUMPFUN_RE = re.compile(r"pump\.fun/(?:coin/)?([1-9A-HJ-NP-Za-km-z]{32,44})")
DEXSCREENER_RE = re.compile(r"dexscreener\.com/solana/([1-9A-HJ-NP-Za-km-z]{32,44})")
# common false-positive CAs (well-known program/token ids that appear everywhere)
KNOWN_NON_MEMECOIN = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", # USDT
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # token program
    "11111111111111111111111111111111",              # system program
    "pump9XNz9RRtFy8bZWYjFrJr9Ka6BFSKqJ3zMU1pump",  # placeholder guard
}


def b58decode(s: str) -> bytes | None:
    n = 0
    for ch in s:
        if ch not in B58_SET:
            return None
        n = n * 58 + B58_ALPHABET.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def is_valid_mint(candidate: str) -> bool:
    """Valid = base58-decodes to exactly 32 bytes and not a known non-memecoin constant."""
    if candidate in KNOWN_NON_MEMECOIN:
        return False
    if not (32 <= len(candidate) <= 44):
        return False
    decoded = b58decode(candidate)
    return decoded is not None and len(decoded) == 32


@dataclass
class Extraction:
    valid_cas: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    telegram_links: list[str] = field(default_factory=list)
    pumpfun_cas: list[str] = field(default_factory=list)
    dexscreener_cas: list[str] = field(default_factory=list)


def extract(text: str) -> Extraction:
    out = Extraction()
    if not text:
        return out
    out.pumpfun_cas = PUMPFUN_RE.findall(text)
    out.dexscreener_cas = DEXSCREENER_RE.findall(text)
    candidates = set(CA_CANDIDATE_RE.findall(text)) | set(out.pumpfun_cas) | set(out.dexscreener_cas)
    out.candidates = sorted(candidates)
    out.valid_cas = sorted({c for c in candidates if is_valid_mint(c)})
    out.tickers = sorted({t.upper() for t in TICKER_RE.findall(text)})
    out.urls = URL_RE.findall(text)
    out.telegram_links = sorted({h.lower() for h in TG_LINK_RE.findall(text)})
    return out


def text_similarity(a: str, b: str) -> float:
    """Cheap Jaccard over word shingles for copy/near-duplicate detection."""
    sa, sb = set(a.lower().split()), set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
