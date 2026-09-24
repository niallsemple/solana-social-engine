"""Virality components + social push state machine.

Components are collected separately (never collapsed prematurely); the virality score uses
weights stored in virality_models so the formula can be re-estimated empirically later.
State labels are provisional rule-based classifications to be replaced by data-derived cutoffs.
"""
from __future__ import annotations

import json
import math

from . import config
from .db import alert, now


def ensure_default_virality_model(conn) -> None:
    conn.execute(
        "INSERT INTO virality_models (name, weights_json, created_at) VALUES (?,?,?) ON CONFLICT(name) DO NOTHING",
        (config.DEFAULT_VIRALITY_MODEL["name"], json.dumps(config.DEFAULT_VIRALITY_MODEL["weights"]), now()),
    )


def compute_virality(conn, mint: str) -> dict:
    """Snapshot all virality components for a token right now."""
    ensure_default_virality_model(conn)
    t = now()
    first = conn.execute("SELECT first_seen_ts FROM tokens WHERE mint = ?", (mint,)).fetchone()
    if not first:
        return {}
    t0 = first["first_seen_ts"]
    window_min = max((t - t0) / 60.0, 1e-6)

    calls = conn.execute(
        """SELECT c.platform, c.account_ref, c.system_detection_ts, p.is_copy, p.is_repost,
                  p.reposts, p.replies, p.likes, p.views
           FROM call_events c LEFT JOIN social_posts p ON p.id = c.post_id
           WHERE c.mint = ?""",
        (mint,),
    ).fetchall()
    x = [c for c in calls if c["platform"] == "x"]
    tg = [c for c in calls if c["platform"] == "telegram"]

    # mentions per minute over trailing 5 minutes + acceleration vs previous 5
    mpm_now = len([c for c in calls if t - c["system_detection_ts"] <= 300]) / 5.0
    mpm_prev = len([c for c in calls if 300 < t - c["system_detection_ts"] <= 600]) / 5.0
    accel = mpm_now - mpm_prev

    originals = [c for c in calls if not (c["is_copy"] or c["is_repost"])]
    first_x = min((c["system_detection_ts"] for c in x), default=None)
    first_tg = min((c["system_detection_ts"] for c in tg), default=None)
    tg_after_x = (
        len([c for c in tg if first_x and c["system_detection_ts"] > first_x]) / len(tg)
        if tg and first_x else None
    )
    x_after_tg = (
        len([c for c in x if first_tg and c["system_detection_ts"] > first_tg]) / len(x)
        if x and first_tg else None
    )
    follower_reach = 0.0
    for c in x:
        row = conn.execute("SELECT followers FROM x_accounts WHERE handle = ?", (c["account_ref"],)).fetchone()
        if row and row["followers"]:
            follower_reach += row["followers"]

    comp = {
        "unique_accounts": len({c["account_ref"] for c in x}),
        "unique_channels": len({c["account_ref"] for c in tg}),
        "mentions_per_min": mpm_now,
        "mention_accel": accel,
        "reposts": sum(c["reposts"] or 0 for c in calls),
        "replies": sum(c["replies"] or 0 for c in calls),
        "likes": sum(c["likes"] or 0 for c in calls),
        "views": sum(c["views"] or 0 for c in calls),
        "community_count": len({c["account_ref"] for c in tg}),
        "original_ratio": len(originals) / len(calls) if calls else None,
        "follower_reach": follower_reach,
        "tg_x_spread": tg_after_x,
        "x_tg_spread": x_after_tg,
        "time_compression": len(calls) / math.sqrt(window_min),
    }

    weights = json.loads(conn.execute(
        "SELECT weights_json FROM virality_models WHERE name = ?", (config.DEFAULT_VIRALITY_MODEL["name"],)
    ).fetchone()["weights_json"])
    score = (
        weights["unique_accounts"] * comp["unique_accounts"]
        + weights["unique_channels"] * comp["unique_channels"]
        + weights["mentions_per_min"] * comp["mentions_per_min"]
        + weights["mention_accel"] * max(comp["mention_accel"], 0)
        + weights["original_ratio"] * (comp["original_ratio"] or 0)
        + weights["community_count"] * comp["community_count"]
        + weights["log_follower_reach"] * math.log10(comp["follower_reach"] + 1)
        + weights["tg_x_spread"] * (comp["tg_x_spread"] or 0)
        + weights["time_compression"] * comp["time_compression"]
    )

    conn.execute(
        """INSERT INTO virality_snapshots
           (mint, ts, unique_accounts, unique_channels, mentions_per_min, mention_accel,
            reposts, replies, likes, views, community_count, original_ratio, follower_reach,
            tg_x_spread, x_tg_spread, time_compression, virality_score, model_name, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (mint, t, comp["unique_accounts"], comp["unique_channels"], comp["mentions_per_min"],
         comp["mention_accel"], comp["reposts"], comp["replies"], comp["likes"], comp["views"],
         comp["community_count"], comp["original_ratio"], comp["follower_reach"],
         comp["tg_x_spread"], comp["x_tg_spread"], comp["time_compression"], score,
         config.DEFAULT_VIRALITY_MODEL["name"], t),
    )
    comp["virality_score"] = round(score, 3)
    update_social_state(conn, mint, comp)
    return comp


def update_social_state(conn, mint: str, comp: dict) -> str:
    """Transition ISOLATED -> EMERGING -> ACCELERATING -> VIRAL -> SATURATED -> DECAYING -> DEAD.
    COORDINATED is set by cluster detection, not here. Transitions are recorded with evidence."""
    rules = config.SOCIAL_STATE_RULES
    cur = conn.execute(
        "SELECT state FROM token_social_state WHERE mint = ? ORDER BY ts DESC LIMIT 1", (mint,)
    ).fetchone()
    prev = cur["state"] if cur else None
    t = now()

    callers = comp["unique_accounts"] + comp["unique_channels"]
    mpm = comp["mentions_per_min"]
    last_mention = conn.execute(
        "SELECT MAX(system_detection_ts) AS m FROM call_events WHERE mint = ?", (mint,)
    ).fetchone()["m"]
    idle = t - last_mention if last_mention else 1e9

    if prev == "COORDINATED":
        state = prev if idle < rules["DECAYING_idle_sec"] else "DECAYING"
    elif idle >= rules["DEAD_idle_sec"] and prev in ("DECAYING", "SATURATED", "VIRAL", "ACCELERATING", "EMERGING", "ISOLATED"):
        state = "DEAD"
    elif idle >= rules["DECAYING_idle_sec"] and prev in ("VIRAL", "SATURATED", "ACCELERATING", "EMERGING"):
        state = "DECAYING"
    elif mpm >= rules["VIRAL_mpm"] and callers >= rules["VIRAL_min_unique"]:
        state = "VIRAL"
    elif mpm >= rules["ACCELERATING_mpm"] and comp["mention_accel"] > 0:
        state = "ACCELERATING"
    elif prev == "VIRAL" and mpm < rules["ACCELERATING_mpm"]:
        state = "SATURATED"
    elif callers >= rules["EMERGING_min_callers"]:
        state = "EMERGING"
    else:
        state = "ISOLATED"

    if state != prev:
        conn.execute(
            "INSERT INTO token_social_state (mint, ts, state, mpm, unique_callers, created_at) VALUES (?,?,?,?,?,?)",
            (mint, t, state, mpm, callers, t),
        )
        conn.execute("UPDATE token_events SET current_state = ? WHERE mint = ?", (state, mint))
        if state == "ACCELERATING":
            alert(conn, "SOCIAL_VELOCITY_ACCELERATING", mint, json.dumps({"mpm": mpm}))
        elif state == "VIRAL":
            alert(conn, "VIRALITY_ACCELERATION", mint, json.dumps({"mpm": mpm, "callers": callers}))
        elif state == "DEAD":
            conn.execute(
                "UPDATE token_events SET social_push_end = ?, total_push_duration = ? - social_push_start WHERE mint = ?",
                (t, t, mint),
            )
    return state
