"""Standalone presentation CLI. Exit 0 includes expected degradation; 1 is failure.

Replay uses the existing adapter -> ModelService (feature engineering and real
model inference) -> alert_engine path. Snapshot inputs in replay supply only
official identity mappings, never historical measurements or predictions.
"""

import argparse
from collections import Counter
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import statistics
import sys

from backend.alert_engine import evaluate_alerts
from backend.history_adapter import build_station_id_mapping_from_normalized, load_history_records
from backend.model_service import ModelService
from backend.pipeline import TAIPEI, run_realtime_pipeline
from backend.realtime_normalizer import normalize_snapshots


def build_parser():
    parser = argparse.ArgumentParser(description="New Taipei YouBike AI Dispatch Demo")
    modes = parser.add_subparsers(dest="mode", required=True)
    for mode in ("realtime", "replay"):
        sub = modes.add_parser(mode, help="HISTORICAL REPLAY" if mode == "replay" else "Realtime snapshots")
        sub.add_argument("--realtime-csv", nargs="+", required=mode == "realtime",
                         help="Snapshot CSVs; replay uses these ONLY for official station IDs")
        sub.add_argument("--history-source", required=mode == "replay")
        sub.add_argument("--model-pack", required=mode == "replay", help="Trusted real model ZIP or pkl")
        sub.add_argument("--holiday-calendar")
        sub.add_argument("--output-json")
        if mode == "realtime":
            sub.add_argument("--coordinate-source")
            sub.add_argument("--vehicle-state", help="JSON vehicle list")
        else:
            sub.add_argument("--origin", required=True, help="ISO timestamp with timezone")
            sub.add_argument("--station-id-mapping", help="JSON station_name -> official station ID; alternative to snapshots")
    return parser


def _input_paths(args):
    paths = list(args.realtime_csv or [])
    for key in ("history_source", "model_pack", "holiday_calendar", "coordinate_source",
                "vehicle_state", "station_id_mapping"):
        if getattr(args, key, None):
            paths.append(getattr(args, key))
    return [Path(p).resolve() for p in paths]


def _validate(args):
    inputs = _input_paths(args)
    for path in inputs:
        if not path.exists():
            raise ValueError(f"Input path does not exist: {path}")
    if args.output_json:
        output = Path(args.output_json).resolve()
        if output.exists() and output.suffix.lower() != ".json":
            raise ValueError("Refusing to overwrite an existing non-JSON file")
        for path in inputs:
            if output == path or (path.is_dir() and path in output.parents):
                raise ValueError("JSON output must not overwrite or be placed inside input data")
            if output.exists() and path.is_file() and output.samefile(path):
                raise ValueError("JSON output aliases an input file")


def _load_model(args):
    service = ModelService(args.model_pack, holiday_calendar_path=args.holiday_calendar)
    loaded = service.load()
    if loaded["status"] != "ready":
        raise ValueError("Model pack cannot be loaded: " + json.dumps(loaded["diagnostics"], ensure_ascii=False))
    return service


def _replay(args, service):
    origin = datetime.fromisoformat(args.origin.replace("Z", "+00:00"))
    if origin.tzinfo is None or origin.minute not in (0, 30) or origin.second or origin.microsecond:
        raise ValueError("Replay origin requires timezone and an exact half-hour slot")
    origin = origin.astimezone(TAIPEI)
    diagnostics = []
    if args.station_id_mapping:
        mapping = json.loads(Path(args.station_id_mapping).read_text(encoding="utf-8-sig"))
        if not isinstance(mapping, dict) or not mapping or any(
                not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in mapping.items()):
            raise ValueError("Station mapping must contain nonempty names and official ID strings")
    elif args.realtime_csv:
        normalized = normalize_snapshots(args.realtime_csv)
        mapped = build_station_id_mapping_from_normalized(normalized)
        mapping = mapped["mapping"]
        diagnostics.extend(normalized["diagnostics"] + mapped["diagnostics"])
    else:
        raise ValueError("Replay needs --realtime-csv or --station-id-mapping: cleaned history has no official IDs")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("Station mapping must have unique official IDs")
    adapted = load_history_records(args.history_source, mapping,
                                   start=(origin - timedelta(days=14)).isoformat(),
                                   end=(origin + timedelta(minutes=60)).isoformat())
    diagnostics.extend(adapted["diagnostics"])
    current, history, actuals = [], [], {}
    for row in adapted["records"]:
        stamp = datetime.fromisoformat(row["model_time_slot"])
        if stamp == origin:
            current.append(dict(row, available_docks=row["total_slots"] - row["available_bikes"],
                                available_docks_source="derived_total_minus_bikes"))
        elif stamp < origin:
            history.append(row)
        else:
            key = (row["official_station_id"], row["model_time_slot"])
            actuals.setdefault(key, []).append(row)
    prediction = service.predict(current, history)
    comparisons = []
    for row in prediction["predictions"]:
        target = (origin + timedelta(minutes=row["horizon_minutes"])).isoformat()
        matches = actuals.get((row["official_station_id"], target), [])
        if len(matches) == 1 and matches[0]["total_slots"] == row["total_slots"]:
            comparisons.append(dict(row, actual_bikes=matches[0]["available_bikes"]))
    evaluated_ids = {r["official_station_id"] for r in comparisons if r["horizon_minutes"] == 30} & {
        r["official_station_id"] for r in comparisons if r["horizon_minutes"] == 60}
    comparisons = [r for r in comparisons if r["official_station_id"] in evaluated_ids]
    metrics = {}
    for horizon in (30, 60):
        rows = [r for r in comparisons if r["horizon_minutes"] == horizon]
        errors = [abs(r["raw_prediction"] - r["actual_bikes"]) for r in rows]
        metrics[str(horizon)] = dict(prediction_count=sum(r["horizon_minutes"] == horizon for r in prediction["predictions"]),
                                    evaluated_count=len(rows), MAE=statistics.mean(errors) if errors else None,
                                    RMSE=math.sqrt(statistics.mean(e*e for e in errors)) if errors else None,
                                    median_absolute_error=statistics.median(errors) if errors else None)
    alerts = evaluate_alerts(current, prediction["predictions"], prediction["status"], now=origin.isoformat())
    eligible = {r["official_station_id"] for r in prediction["predictions"]}
    excluded = [dict(official_station_id=r["official_station_id"], station_name=r["station_name"],
                     reason="prediction_ineligible" if r["official_station_id"] not in eligible else "missing_or_ambiguous_actual")
                for r in current if r["official_station_id"] not in evaluated_ids]
    return dict(mode="replay", label="HISTORICAL REPLAY", evaluation_label="Historical replay evaluation",
                origin=origin.isoformat(), evaluation_status="ready" if evaluated_ids else "blocked",
                metric_basis="raw_prediction (unrounded model output); stations with both actual horizons",
                identity_source="station-id-mapping" if args.station_id_mapping else "realtime-csv (identity only)",
                stations=current, eligible_stations=len(eligible), evaluated_stations=len(evaluated_ids),
                excluded_stations=excluded, prediction=prediction, comparisons=comparisons,
                metrics=metrics, alerts=alerts, diagnostics=diagnostics)


def run_demo(args):
    """Return complete result without printing or mutating input files."""
    _validate(args)
    service = _load_model(args) if args.model_pack else None
    if args.mode == "replay":
        return _replay(args, service)
    vehicles = None
    if args.vehicle_state:
        vehicles = json.loads(Path(args.vehicle_state).read_text(encoding="utf-8-sig"))
        if not isinstance(vehicles, list):
            raise ValueError("Vehicle state must be a JSON list")
    result = run_realtime_pipeline(
        args.realtime_csv, historical_data_source=args.history_source,
        model_pack_path=args.model_pack, holiday_calendar_path=args.holiday_calendar,
        vehicle_state=vehicles, config={"station_reference_source": args.coordinate_source},
        dependencies={"model_service_class": lambda *a, **k: service} if service else None)
    return dict(result, mode="realtime")


def _diagnostics(title, rows):
    counts = Counter((r.get("source_module", ""), r.get("code", "diagnostic")) for r in rows)
    lines = [title + ":"]
    for (source, code), count in sorted(counts.items())[:20]:
        lines.append(f"  - {source + ': ' if source else ''}{code} ({count})")
    if len(counts) > 20:
        lines.append(f"  ... {len(counts)-20} more diagnostic types; full details in JSON")
    return lines if rows else [title + ": None"]


def _coordinates(stations):
    count = 0
    for row in stations:
        try:
            lat, lon = float(row["latitude"]), float(row["longitude"])
            count += math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
        except (KeyError, ValueError, TypeError):
            pass
    return count


def render_summary(result):
    lines = ["=== New Taipei YouBike AI Dispatch Demo ==="]
    prediction = result["prediction"]
    if result["mode"] == "realtime":
        stations = result["stations"]
        lines += ["Mode: Realtime", f"Pipeline status: {result['pipeline_status'].upper()}",
                  f"Realtime data: {'READY' if stations else 'BLOCKED'}",
                  f"Stations: {len(stations)}", f"Coordinates: {_coordinates(stations)} / {len(stations)} (final enriched state)",
                  f"Stale: {'Yes' if result['stale'] else 'No'} (pipeline fallback state)",
                  f"Snapshot observed at: {(result.get('source_snapshot') or {}).get('latest_observed_at')}",
                  "Prediction:", f"  Status: {prediction['status'].upper()}",
                  f"  30-minute forecasts: {len(prediction['horizon_30m'])}",
                  f"  60-minute forecasts: {len(prediction['horizon_60m'])}"]
    else:
        lines += ["Mode: HISTORICAL REPLAY", "Historical replay evaluation",
                  f"Replay origin: {result['origin']}", f"Eligible stations: {result['eligible_stations']}",
                  f"Evaluated stations: {result['evaluated_stations']}",
                  f"Excluded stations: {len(result['excluded_stations'])}",
                  f"Prediction status: {prediction['status'].upper()}", f"Metric basis: {result['metric_basis']}"]
        for horizon, values in result["metrics"].items():
            lines.append(f"{horizon}-minute: " + ", ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}" for k, v in values.items()))
        examples = {}
        for row in result["comparisons"]:
            examples.setdefault((row["station_name"], row["official_station_id"]), {})[row["horizon_minutes"]] = row
        lines.append("Prediction examples (rounded predicted_bikes / actual):")
        for (name, sid), values in sorted(examples.items())[:5]:
            lines.append(f"  {name} ({sid}): " + "; ".join(f"{h}m {values[h]['predicted_bikes']} / {values[h]['actual_bikes']}" for h in (30, 60)))
    lines += _diagnostics("Prediction diagnostics", prediction.get("diagnostics", []))
    active = result["alerts"]["active_alerts"]
    lines += ["Alerts:", f"  Active alerts: {len(active)}"]
    for label, field, value in (("Low bikes", "alert_type", "low_bikes"), ("Low docks", "alert_type", "low_docks"), ("Critical", "severity", "critical")):
        lines.append(f"  {label}: {sum(r.get(field) == value for r in active)}")
    for source in ("realtime", "forecast_30m", "forecast_60m"):
        lines.append(f"  {source}-triggered: {sum(source in r.get('triggered_by', []) for r in active)}")
    if result["mode"] == "realtime":
        opt = result["optimization"]
        lines += ["Optimization:", f"  Candidate plans: {len(opt['candidate_plans'])}",
                  "  Candidate plan: Requires admin confirmation",
                  f"  Route estimation: {opt.get('route_estimation_method')} (Haversine is straight-line estimation, not road travel time)"]
        lines += _diagnostics("Optimization diagnostics", opt["diagnostics"])
    lines += _diagnostics("Source/upstream and module diagnostics (not final coordinate counts)", result["diagnostics"])
    return "\n".join(lines)


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run_demo(args)
        if args.output_json:
            payload = json.dumps(_json_value(result), ensure_ascii=False, allow_nan=False, indent=2)
            Path(args.output_json).write_text(payload + "\n", encoding="utf-8")
        print(render_summary(result))
        if result["mode"] == "realtime":
            return 0 if result["stations"] and result["pipeline_status"] != "blocked" else 1
        return 0 if result["evaluation_status"] == "ready" else 1
    except Exception as exc:
        print(f"Demo failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
