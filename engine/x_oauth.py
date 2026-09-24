"""Minimal OAuth 1.0a (HMAC-SHA1) request signing for X API v2 user-context auth.
Used when no app-only bearer token is available. Stdlib only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
import uuid


def _pct(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


def oauth1_header(method: str, url: str, query_params: dict,
                  api_key: str, api_secret: str,
                  access_token: str, access_secret: str) -> str:
    oauth = {
        "oauth_consumer_key": api_key,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": access_token,
        "oauth_version": "1.0",
    }
    # signature base: all query + oauth params, sorted, percent-encoded
    all_params = {**query_params, **oauth}
    pairs = []
    for k, v in all_params.items():
        for vv in (v if isinstance(v, list) else [v]):
            pairs.append((_pct(k), _pct(vv)))
    pairs.sort()
    param_string = "&".join(f"{k}={v}" for k, v in pairs)
    base_url = url.split("?")[0]
    base_string = "&".join([method.upper(), _pct(base_url), _pct(param_string)])
    signing_key = f"{_pct(api_secret)}&{_pct(access_secret)}"
    sig = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    ).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))
