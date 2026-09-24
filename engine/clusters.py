"""Coordinated push / social cluster detection.

Flags groups of accounts suddenly discussing the same contract with similar/identical wording,
shared URLs, tight timestamps, or frequent historical co-occurrence. Creates SOCIAL_CLUSTER_IDs
and marks tokens COORDINATED. Findings are associations — evidence is stored in features_json.
"""
from __future__ import annotations

import json

from . import cas as ca
from .db import alert, now


def detect_clusters(conn, mint: str) -> list[int]:
    """Examine all calls for a mint; create/update clusters when coordination signals fire."""
    calls = conn.execute(
        """SELECT c.id, c.account_ref, c.platform, c.system_detection_ts, p.text, p.urls_json
           FROM call_events c LEFT JOIN social_posts p ON p.id = c.post_id
           WHERE c.mint = ? ORDER BY c.system_detection_ts""",
        (mint,),
    ).fetchall()
    if len(calls) < 3:
        return []

    created: list[int] = []
    groups: list[list[dict]] = []
    used = set()
    for i, a in enumerate(calls):
        if a["id"] in used:
            continue
        grp = [a]
        for b in calls[i + 1:]:
            if b["id"] in used or b["account_ref"] == a["account_ref"]:
                continue
            close_in_time = abs(b["system_detection_ts"] - a["system_detection_ts"]) <= 300
            similar_text = ca.text_similarity(a["text"] or "", b["text"] or "") >= 0.6
            shared_url = bool(set(json.loads(a["urls_json"] or "[]")) & set(json.loads(b["urls_json"] or "[]")))
            if close_in_time and (similar_text or shared_url):
                grp.append(b)
        if len(grp) >= 3:
            groups.append(grp)
            used.update(g["id"] for g in grp)

    for grp in groups:
        members = sorted({(g["platform"], g["account_ref"]) for g in grp})
        features = {
            "mint": mint,
            "n_accounts": len(members),
            "window_seconds": grp[-1]["system_detection_ts"] - grp[0]["system_detection_ts"],
            "evidence": "similar_text_or_shared_url_within_5min",
        }
        existing = conn.execute(
            """SELECT c.id FROM social_clusters c
               JOIN cluster_members m ON m.cluster_id = c.id
               WHERE m.member_ref = ? AND c.status = 'ACTIVE' LIMIT 1""",
            (members[0][1],),
        ).fetchone()
        if existing:
            cid = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO social_clusters (label, first_seen_ts, features_json, created_at) VALUES (?,?,?,?)",
                (f"cluster-{now():.0f}", now(), json.dumps(features), now()),
            )
            cid = cur.lastrowid
            created.append(cid)
        for platform, ref in members:
            conn.execute(
                "INSERT INTO cluster_members (cluster_id, member_type, member_ref, added_ts) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                (cid, "x_account" if platform == "x" else "tg_channel", ref, now()),
            )
        conn.execute(
            "INSERT INTO token_social_state (mint, ts, state, created_at) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            (mint, now(), "COORDINATED", now()),
        )
        conn.execute("UPDATE token_events SET current_state = 'COORDINATED' WHERE mint = ?", (mint,))
        alert(conn, "UNUSUAL_SOCIAL_CLUSTER", mint, json.dumps({"cluster_id": cid, **features}))
    return created


def cluster_performance(conn) -> list[dict]:
    """Median net 15m return and MFE of calls touching each cluster's member accounts."""
    out = []
    for c in conn.execute("SELECT id, label, first_seen_ts FROM social_clusters").fetchall():
        members = conn.execute(
            "SELECT member_ref FROM cluster_members WHERE cluster_id = ?", (c["id"],)
        ).fetchall()
        refs = [m["member_ref"] for m in members]
        if not refs:
            continue
        ph = ",".join("?" * len(refs))
        row = conn.execute(
            f"""SELECT COUNT(*) AS n, AVG(net_return_15m) AS avg_net, AVG(mfe_15m) AS avg_mfe
                FROM call_events WHERE account_ref IN ({ph}) AND monitoring_complete = 1""",
            refs,
        ).fetchone()
        med = conn.execute(
            f"""SELECT net_return_15m FROM call_events
                WHERE account_ref IN ({ph}) AND monitoring_complete = 1 AND net_return_15m IS NOT NULL
                ORDER BY net_return_15m LIMIT 1 OFFSET (SELECT COUNT(*) FROM call_events
                    WHERE account_ref IN ({ph}) AND monitoring_complete = 1 AND net_return_15m IS NOT NULL) / 2""",
            refs + refs,
        ).fetchone()
        out.append({
            "cluster_id": c["id"], "label": c["label"], "members": refs,
            "sample": row["n"], "avg_net_15m": row["avg_net"], "avg_mfe_15m": row["avg_mfe"],
            "median_net_15m": med["net_return_15m"] if med else None,
        })
    return out
