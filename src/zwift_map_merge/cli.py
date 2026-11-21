"""Command line: ``serve`` (web UI + scheduler), ``merge-files`` (offline)."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="zwift-map-merge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="web UI and periodic scan")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8126)

    mf = sub.add_parser("merge-files", help="merge two local FIT files, without Garmin")
    mf.add_argument("watch", type=Path, help="watch FIT (full data)")
    mf.add_argument("zwift", type=Path, help="Zwift FIT (map)")
    mf.add_argument("-o", "--output", type=Path, required=True)
    mf.add_argument("--altitude", action="store_true", help="also copy the Zwift altitude")
    mf.add_argument("--tolerance", type=int, default=2, help="maximum time gap in seconds")

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        from .server import run
        run(args.host, args.port)
    elif args.cmd == "merge-files":
        from .merge import merge_track
        merged, stats = merge_track(args.watch.read_bytes(), args.zwift.read_bytes(),
                                    tolerance_s=args.tolerance, copy_altitude=args.altitude)
        args.output.write_bytes(merged)
        print(f"{stats.with_position}/{stats.target_records} points with a position "
              f"({stats.ratio:.0%}), {stats.source_points} Zwift points -> {args.output}")


if __name__ == "__main__":
    main()
