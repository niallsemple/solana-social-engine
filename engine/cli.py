"""CLI entry point.

Commands:
  init                 create DB schema
  serve                run REST/JSON API + dashboard (port via --port / SSE_PORT)
  demo                 seed a clearly-labeled SIMULATED forward dataset, then optionally serve
  replay <file.jsonl>  ingest posts from a JSONL replay file (X/Telegram connector output format)
  run-live             start live ingestion + sampler loop (requires API credentials)
  milestone <call_id>  print the FIRST MILESTONE audit record for one call
"""
from __future__ import annotations

import argparse
import sys

from . import config
from .db import init_db


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sse", description="Solana Memecoin Social Discovery Engine (research only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    sp = sub.add_parser("serve")
    sp.add_argument("--host", default=config.HTTP_HOST)
    sp.add_argument("--port", type=int, default=config.HTTP_PORT)
    sp.add_argument("--with-sampler", action="store_true", help="also run the price sampler loop")

    dp = sub.add_parser("demo")
    dp.add_argument("--tokens", type=int, default=12)
    dp.add_argument("--serve", action="store_true")
    dp.add_argument("--port", type=int, default=config.HTTP_PORT)

    rp = sub.add_parser("replay")
    rp.add_argument("file")

    sub.add_parser("run-live")

    sub.add_parser("doctor")

    mp = sub.add_parser("milestone")
    mp.add_argument("call_id", type=int)

    sub.add_parser("paper-status")

    args = p.parse_args(argv)

    if args.cmd == "init":
        init_db(config.DB_PATH)
        print(f"Initialized DB at {config.DB_PATH}")
        return 0

    if args.cmd == "serve":
        init_db(config.DB_PATH)
        from .api import serve
        serve(host=args.host, port=args.port, with_sampler=args.with_sampler)
        return 0

    if args.cmd == "demo":
        init_db(config.DB_PATH)
        from .demo_seed import seed
        seed(config.DB_PATH, n_tokens=args.tokens)
        if args.serve:
            from .api import serve
            serve(host=config.HTTP_HOST, port=args.port)
        return 0

    if args.cmd == "replay":
        init_db(config.DB_PATH)
        from .ingest import replay_jsonl
        replay_jsonl(config.DB_PATH, args.file)
        return 0

    if args.cmd == "run-live":
        init_db(config.DB_PATH)
        from .live import run_live
        run_live(config.DB_PATH)
        return 0

    if args.cmd == "doctor":
        from .doctor import run_doctor
        return run_doctor()

    if args.cmd == "milestone":
        from .analytics import milestone_record
        print(milestone_record(config.DB_PATH, args.call_id))
        return 0

    if args.cmd == "paper-status":
        from .db import connect
        from .paper_strategy import status_report
        conn = connect(config.DB_PATH)
        print(status_report(conn))
        conn.close()
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
