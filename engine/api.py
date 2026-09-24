"""Read-only REST/JSON API + LLM tool interface + dashboard static host.

LLM access is READ-ONLY: GET endpoints only, and /api/stats/query accepts SELECT only.
Every statistic is traceable: detail endpoints include the underlying call IDs.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import config
from .analytics import milestone_record
from .clusters import cluster_performance
from .db import connect, init_db

DASHBOARD = Path(__file__).parent / "dashboard" / "index.html"


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _row(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


# ------------------------------------------------------------------ queries

def q_overview(conn):
    day_start = _row(conn, "SELECT strftime('%s','now','start of day') AS s")["s"]
    t = _row(conn, """
        SELECT
          (SELECT COUNT(*) FROM tokens WHERE first_seen_ts >= ?) AS tokens_today,
          (SELECT COUNT(*) FROM call_events WHERE system_detection_ts >= ?) AS calls_today,
          (SELECT COUNT(*) FROM call_events WHERE system_detection_ts >= ? AND platform='x') AS x_calls,
          (SELECT COUNT(*) FROM call_events WHERE system_detection_ts >= ? AND platform='telegram') AS tg_calls,
          (SELECT COUNT(*) FROM call_events WHERE system_detection_ts >= ? AND classification_net='WIN') AS net_wins,
          (SELECT COUNT(*) FROM call_events WHERE system_detection_ts >= ? AND classification_net='LOSS') AS net_losses
    """, (day_start,) * 6)
    priced = _row(conn, """
        SELECT COUNT(*) AS n,
               AVG(net_return_15m) AS avg15,
               AVG(mfe_15m) AS avg_mfe, AVG(mae_15m) AS avg_mae
        FROM call_events
        WHERE system_detection_ts >= ? AND monitoring_complete=1 AND net_return_15m IS NOT NULL
    """, (day_start,))
    med = _rows(conn, """
        SELECT net_return_15m AS r, mfe_15m AS mfe, mae_15m AS mae FROM call_events
        WHERE system_detection_ts >= ? AND monitoring_complete=1 AND net_return_15m IS NOT NULL
        ORDER BY net_return_15m
    """, (day_start,))
    import statistics
    rets = [m["r"] for m in med]
    mfes = [m["mfe"] for m in med if m["mfe"] is not None]
    maes = [m["mae"] for m in med if m["mae"] is not None]
    total_done = _row(conn, "SELECT COUNT(*) AS n FROM call_events WHERE system_detection_ts >= ? AND monitoring_complete=1", (day_start,))["n"]
    unrugs = _row(conn, """
        SELECT
          SUM(CASE WHEN classification_net IN ('RUGGED','DEAD') THEN 1 ELSE 0 END) AS rugs,
          SUM(CASE WHEN classification_net='UNPRICEABLE' THEN 1 ELSE 0 END) AS unpriceable
        FROM call_events WHERE system_detection_ts >= ? AND monitoring_complete=1
    """, (day_start,))
    errors = _row(conn, """
        SELECT COUNT(*) AS total, SUM(CASE WHEN error_code IS NOT NULL THEN 1 ELSE 0 END) AS failed
        FROM price_observations WHERE ts >= ?
    """, (day_start,))
    return {
        **t,
        "net_win_rate": (t["net_wins"] / priced["n"]) if priced["n"] else None,
        "median_15m_return": statistics.median(rets) if rets else None,
        "avg_15m_return": priced["avg15"],
        "median_mfe": statistics.median(mfes) if mfes else None,
        "median_mae": statistics.median(maes) if maes else None,
        "rug_dead_rate": (unrugs["rugs"] / total_done) if total_done else None,
        "unpriceable_rate": (unrugs["unpriceable"] / total_done) if total_done else None,
        "price_error_rate": (errors["failed"] / errors["total"]) if errors["total"] else None,
        "price_observations_today": errors["total"],
    }


def q_recent_discoveries(conn, limit=50):
    return _rows(conn, """
        SELECT t.mint, t.symbol, t.first_seen_ts, t.launch_platform, t.status,
               e.current_state, e.total_x_mentions, e.total_tg_mentions,
               e.unique_x_accounts, e.unique_tg_channels, e.mention_velocity,
               e.price_at_first_detection, e.peak_price
        FROM tokens t LEFT JOIN token_events e ON e.mint = t.mint
        ORDER BY t.first_seen_ts DESC LIMIT ?
    """, (limit,))


def q_token(conn, mint):
    tok = _row(conn, "SELECT * FROM tokens WHERE mint = ?", (mint,))
    if not tok:
        return None
    tok["event"] = _row(conn, "SELECT * FROM token_events WHERE mint = ? ORDER BY id DESC LIMIT 1", (mint,))
    tok["virality"] = _row(conn, "SELECT * FROM virality_snapshots WHERE mint = ? ORDER BY ts DESC LIMIT 1", (mint,))
    tok["calls"] = _rows(conn, """
        SELECT id, platform, account_ref, system_detection_ts, position_in_cycle,
               entry_price, entry_error, net_return_15m, mfe_15m, mae_15m,
               classification_net, monitoring_complete
        FROM call_events WHERE mint = ? ORDER BY system_detection_ts
    """, (mint,))
    return tok


def q_timeline(conn, mint):
    return {
        "prices": _rows(conn, """
            SELECT ts, price_usd, horizon_label, source, error_code
            FROM price_observations WHERE mint = ? AND price_usd IS NOT NULL ORDER BY ts
        """, (mint,)),
        "price_errors": _rows(conn, """
            SELECT ts, horizon_label, error_code FROM price_observations
            WHERE mint = ? AND error_code IS NOT NULL ORDER BY ts
        """, (mint,)),
        "calls": _rows(conn, """
            SELECT c.id, c.platform, c.account_ref, c.system_detection_ts, c.entry_price,
                   c.net_return_15m, c.classification_net, p.is_copy, p.is_repost, p.text
            FROM call_events c LEFT JOIN social_posts p ON p.id = c.post_id
            WHERE c.mint = ? ORDER BY c.system_detection_ts
        """, (mint,)),
        "states": _rows(conn, "SELECT ts, state, mpm, unique_callers FROM token_social_state WHERE mint = ? ORDER BY ts", (mint,)),
        "virality": _rows(conn, "SELECT ts, virality_score, mentions_per_min, unique_accounts, unique_channels FROM virality_snapshots WHERE mint = ? ORDER BY ts", (mint,)),
    }


def q_account(conn, platform, ref):
    stats = _row(conn, "SELECT * FROM account_statistics WHERE account_ref = ? AND platform = ?", (ref, platform))
    table = "x_accounts" if platform == "x" else "telegram_channels"
    prof = _row(conn, f"SELECT * FROM {table} WHERE handle = ?", (ref,))
    if not stats and not prof:
        return None
    calls = _rows(conn, """
        SELECT id, mint, system_detection_ts, position_in_cycle, net_return_15m, mfe_15m,
               classification_net FROM call_events
        WHERE account_ref = ? AND platform = ? ORDER BY system_detection_ts DESC LIMIT 100
    """, (ref, platform))
    return {"profile": prof, "statistics": stats, "recent_calls": calls,
            "audit_note": "all statistics computed from the call IDs in recent_calls (full list via /calls)"}


def q_leaders_early(conn, limit=20):
    return _rows(conn, """
        SELECT account_ref, platform, total_calls, net_win_rate, median_return_15m,
               avg_position_in_cycle, avg_seconds_after_first_detection,
               pct_calls_before_price_peak, sample_size, role_label
        FROM account_statistics
        WHERE sample_size >= 3
        ORDER BY avg_position_in_cycle ASC, net_win_rate DESC LIMIT ?
    """, (limit,))


def q_active_pushes(conn):
    return _rows(conn, """
        SELECT mint, current_state, mention_velocity, unique_x_accounts, unique_tg_channels,
               total_x_mentions + total_tg_mentions AS total_mentions,
               price_at_first_detection, peak_price, first_detection_ts
        FROM token_events
        WHERE current_state IN ('EMERGING','ACCELERATING','COORDINATED','VIRAL')
        ORDER BY mention_velocity DESC LIMIT 50
    """)


def q_viral(conn, limit=20):
    return _rows(conn, """
        SELECT v.mint, v.virality_score, v.mentions_per_min, v.unique_accounts,
               v.unique_channels, v.original_ratio, v.ts, t.symbol
        FROM virality_snapshots v
        JOIN (SELECT mint, MAX(ts) AS mts FROM virality_snapshots GROUP BY mint) latest
          ON latest.mint = v.mint AND latest.mts = v.ts
        LEFT JOIN tokens t ON t.mint = v.mint
        ORDER BY v.virality_score DESC LIMIT ?
    """, (limit,))


def q_search_calls(conn, q="", limit=100):
    like = f"%{q}%"
    return _rows(conn, """
        SELECT c.id, c.mint, c.platform, c.account_ref, c.system_detection_ts,
               c.net_return_15m, c.classification_net, c.monitoring_complete
        FROM call_events c
        WHERE c.mint LIKE ? OR c.account_ref LIKE ?
        ORDER BY c.system_detection_ts DESC LIMIT ?
    """, (like, like, limit))


def q_lexicon(conn):
    return _rows(conn, "SELECT * FROM discovery_terms ORDER BY COALESCE(precision_score, -1) DESC, number_of_posts_found DESC")


def q_quality(conn):
    return {
        "issues": _rows(conn, "SELECT * FROM data_quality ORDER BY ts DESC LIMIT 100"),
        "system_errors": _rows(conn, "SELECT * FROM system_errors ORDER BY ts DESC LIMIT 100"),
        "api_usage": _rows(conn, """
            SELECT provider, endpoint, status, COUNT(*) AS n, AVG(latency_ms) AS avg_ms
            FROM api_usage GROUP BY provider, endpoint, status ORDER BY n DESC
        """),
        "price_error_breakdown": _rows(conn, """
            SELECT error_code, COUNT(*) AS n FROM price_observations
            WHERE error_code IS NOT NULL GROUP BY error_code ORDER BY n DESC
        """),
    }


FORBIDDEN_SQL = ("insert", "update", "delete", "drop", "alter", "attach", "pragma",
                 "replace", "create", "vacuum", ";")


def q_stats_query(conn, sql: str):
    low = sql.strip().lower()
    if not low.startswith("select") or any(k in low for k in FORBIDDEN_SQL):
        return {"error": "read-only: single SELECT statements only"}
    try:
        return {"rows": _rows(conn, sql)[:500]}
    except sqlite3.Error as e:
        return {"error": str(e)}


TOOLS = {
    "get_recent_discoveries": lambda c, a: q_recent_discoveries(c, a.get("limit", 50)),
    "get_token": lambda c, a: q_token(c, a["contract"]),
    "get_token_social_timeline": lambda c, a: q_timeline(c, a["contract"]),
    "get_account": lambda c, a: q_account(c, a.get("platform", "x"), a["account_id"]),
    "get_account_calls": lambda c, a: _rows(c, "SELECT * FROM call_events WHERE account_ref = ? ORDER BY system_detection_ts DESC LIMIT ?", (a["account_id"], a.get("limit", 200))),
    "get_top_early_callers": lambda c, a: q_leaders_early(c, a.get("limit", 20)),
    "get_active_social_pushes": lambda c, a: q_active_pushes(c),
    "get_viral_tokens": lambda c, a: q_viral(c, a.get("limit", 20)),
    "get_social_clusters": lambda c, a: _rows(c, "SELECT * FROM social_clusters ORDER BY first_seen_ts DESC"),
    "get_cluster_performance": lambda c, a: cluster_performance(c),
    "get_telegram_leaders": lambda c, a: _rows(c, "SELECT * FROM account_statistics WHERE platform='telegram' AND sample_size >= 3 ORDER BY net_win_rate DESC LIMIT ?", (a.get("limit", 20),)),
    "get_x_leaders": lambda c, a: _rows(c, "SELECT * FROM account_statistics WHERE platform='x' AND sample_size >= 3 ORDER BY net_win_rate DESC LIMIT ?", (a.get("limit", 20),)),
    "search_calls": lambda c, a: q_search_calls(c, a.get("q", ""), a.get("limit", 100)),
    "compare_accounts": lambda c, a: [q_account(c, x.get("platform", "x"), x["ref"]) for x in a.get("accounts", [])],
    "compare_clusters": lambda c, a: [x for x in cluster_performance(c) if x["cluster_id"] in a.get("cluster_ids", [])] or cluster_performance(c),
    "get_current_hypotheses": lambda c, a: _rows(c, "SELECT * FROM hypotheses ORDER BY created_at DESC"),
    "get_forward_validation_results": lambda c, a: _rows(c, "SELECT * FROM hypothesis_results WHERE dataset='FORWARD' ORDER BY computed_at DESC"),
    "query_statistics": lambda c, a: q_stats_query(c, a.get("sql", "")),
    "get_overview": lambda c, a: q_overview(c),
    "get_lexicon": lambda c, a: q_lexicon(c),
    "get_alerts": lambda c, a: _rows(c, "SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (a.get("limit", 100),)),
    "get_data_quality": lambda c, a: q_quality(c),
    "get_milestone_record": lambda c, a: milestone_record(config.DB_PATH, a["call_id"]),
}


# ------------------------------------------------------------------ http

class Handler(BaseHTTPRequestHandler):
    server_version = "SSE/0.1"

    def _send(self, code: int, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str).encode())

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/mcp/call":
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "invalid JSON"}, 400)
            tool = payload.get("tool")
            args = payload.get("arguments") or {}
            fn = TOOLS.get(tool)
            if not fn:
                return self._json({"error": f"unknown tool '{tool}'", "available": sorted(TOOLS)}, 404)
            conn = connect(config.DB_PATH)
            try:
                return self._json({"tool": tool, "result": fn(conn, args)})
            except Exception as e:
                return self._json({"error": repr(e)}, 500)
            finally:
                conn.close()
        return self._json({"error": "not found"}, 404)

    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        conn = connect(config.DB_PATH)
        try:
            if path in ("/", "/index.html"):
                return self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
            if path == "/mcp/tools":
                return self._json({"tools": [{"name": n, "read_only": True} for n in sorted(TOOLS)]})
            if path == "/api/health":
                return self._json({"ok": True, "db": config.DB_PATH})
            if path == "/api/stats/overview":
                return self._json(q_overview(conn))
            if path == "/api/discoveries/recent":
                return self._json(q_recent_discoveries(conn, int(qs.get("limit", [50])[0])))
            if path.startswith("/api/token/"):
                mint = path.split("/")[3]
                if path.endswith("/timeline"):
                    return self._json(q_timeline(conn, mint))
                tok = q_token(conn, mint)
                return self._json(tok) if tok else self._json({"error": "not found"}, 404)
            if path.startswith("/api/account/"):
                parts = path.split("/")
                platform, ref = parts[3], parts[4]
                if len(parts) > 5 and parts[5] == "calls":
                    return self._json(_rows(conn, "SELECT * FROM call_events WHERE account_ref = ? AND platform = ? ORDER BY system_detection_ts DESC LIMIT 500", (ref, platform)))
                acc = q_account(conn, platform, ref)
                return self._json(acc) if acc else self._json({"error": "not found"}, 404)
            if path == "/api/leaders/early":
                return self._json(q_leaders_early(conn))
            if path == "/api/leaders/x":
                return self._json(TOOLS["get_x_leaders"](conn, {}))
            if path == "/api/leaders/telegram":
                return self._json(TOOLS["get_telegram_leaders"](conn, {}))
            if path == "/api/pushes/active":
                return self._json(q_active_pushes(conn))
            if path == "/api/tokens/viral":
                return self._json(q_viral(conn))
            if path == "/api/clusters":
                return self._json(_rows(conn, "SELECT * FROM social_clusters ORDER BY first_seen_ts DESC"))
            if path == "/api/clusters/performance":
                return self._json(cluster_performance(conn))
            if path == "/api/calls/search":
                return self._json(q_search_calls(conn, qs.get("q", [""])[0]))
            if path == "/api/hypotheses":
                return self._json(_rows(conn, "SELECT * FROM hypotheses ORDER BY created_at DESC"))
            if path == "/api/hypotheses/validation":
                return self._json(_rows(conn, "SELECT * FROM hypothesis_results ORDER BY computed_at DESC LIMIT 200"))
            if path == "/api/lexicon":
                return self._json(q_lexicon(conn))
            if path == "/api/alerts":
                return self._json(_rows(conn, "SELECT * FROM alerts ORDER BY ts DESC LIMIT 100"))
            if path == "/api/quality":
                return self._json(q_quality(conn))
            if path == "/api/stats/query":
                return self._json(q_stats_query(conn, qs.get("sql", [""])[0]))
            if path.startswith("/api/milestone/"):
                return self._send(200, milestone_record(config.DB_PATH, int(path.split("/")[3])).encode(), "text/plain; charset=utf-8")
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": repr(e)}, 500)
        finally:
            conn.close()


def serve(host: str, port: int, with_sampler: bool = False):
    init_db(config.DB_PATH)
    if with_sampler:
        from .pricing import HeliusProvider
        from .sampler import start_background_sampler
        provider = HeliusProvider()
        start_background_sampler(config.DB_PATH, provider)
        print("sampler: running (Helius)")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Solana Social Discovery Engine → http://{host}:{port}")
    print(f"LLM tools: GET /mcp/tools · POST /mcp/call  (read-only)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
