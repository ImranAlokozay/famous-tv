#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from iptv_health.maintenance import run_maintenance  # noqa: E402


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Conservatively check and propose repairs for the famous-tv IPTV playlist")
    parser.add_argument("--mode", choices=("health-only", "check-repair", "repair-failed"),
                        default="health-only")
    parser.add_argument("--playlist", type=Path, default=ROOT / "public/tv.m3u")
    parser.add_argument("--config", type=Path, default=ROOT / "config/iptv_health.json")
    parser.add_argument("--previous-report", type=Path)
    parser.add_argument("--workers", type=positive_int)
    parser.add_argument("--timeout", type=positive_int)
    parser.add_argument("--retries", type=nonnegative_int)
    args = parser.parse_args()
    try:
        result = run_maintenance(
            root=ROOT, mode=args.mode, playlist_path=args.playlist.resolve(),
            config_path=args.config.resolve(), workers=args.workers,
            timeout=args.timeout, retries=args.retries,
            previous_report=args.previous_report.resolve() if args.previous_report else None)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"IPTV maintenance failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
