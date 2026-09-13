"""Strict JSON integration entrypoint for Node.

This wrapper keeps human-readable demo output out of the Node contract. It
delegates computation to the completed backend pipeline and writes exactly one
JSON object to stdout.
"""

import argparse
import json
import sys

from backend.demo import _json_value
from backend.pipeline import run_realtime_pipeline


def build_parser():
    parser = argparse.ArgumentParser(description="Run realtime pipeline for Node integration")
    parser.add_argument("--realtime-csv", nargs="+", required=True)
    parser.add_argument("--history-source")
    parser.add_argument("--model-pack")
    parser.add_argument("--holiday-calendar")
    parser.add_argument("--coordinate-source")
    return parser


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    result = run_realtime_pipeline(
        args.realtime_csv,
        historical_data_source=args.history_source,
        model_pack_path=args.model_pack,
        holiday_calendar_path=args.holiday_calendar,
        config={"station_reference_source": args.coordinate_source} if args.coordinate_source else None,
    )
    sys.stdout.write(json.dumps(_json_value(dict(result, mode="realtime")), ensure_ascii=False, allow_nan=False))
    sys.stdout.write("\n")
    return 0 if result["stations"] and result["pipeline_status"] != "blocked" else 1


if __name__ == "__main__":
    sys.exit(main())
