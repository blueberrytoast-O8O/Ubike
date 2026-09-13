"""Stage 5 orchestration layer.

The pipeline coordinates completed backend modules. It does not implement
normalization, feature engineering, model inference, alert rules or routing.
"""

import copy
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.alert_engine import evaluate_alerts
from backend.history_adapter import build_station_id_mapping_from_normalized, load_history_records
from backend.model_service import ModelService
from backend.optimizer import optimize
from backend.realtime_normalizer import normalize_snapshots
from backend.station_reference import build_station_reference, enrich_station_coordinates

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")


def _time(value):
    if value is None:
        return datetime.now(TAIPEI)
    if isinstance(value, datetime):
        stamp = value
    else:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TAIPEI)
    return stamp.astimezone(TAIPEI)


def _records(value, key):
    return value.get(key, []) if isinstance(value, dict) else value or []


def _diagnostic(source_module, code, message="", **details):
    return dict(source_module=source_module, code=str(code), message=str(message), details=details)


def _module_diagnostics(source_module, rows):
    result = []
    for row in rows or []:
        item = copy.deepcopy(row)
        item.pop("source_module", None)
        code = item.pop("code", None) or item.pop("reason", None) or "diagnostic"
        message = item.pop("message", "")
        result.append(_diagnostic(source_module, code, message, **item))
    return result


def _strict_json(value):
    def check(obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            raise ValueError("NaN/Infinity is not allowed")
        if isinstance(obj, dict):
            return {str(k): check(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [check(v) for v in obj]
        if isinstance(obj, tuple):
            return [check(v) for v in obj]
        return obj
    checked = check(value)
    json.dumps(checked, ensure_ascii=False, allow_nan=False)
    return checked


def _split_predictions(predictions):
    horizon_30, horizon_60 = [], []
    for row in predictions or []:
        if row.get("horizon_minutes") == 30:
            horizon_30.append(row)
        elif row.get("horizon_minutes") == 60:
            horizon_60.append(row)
    return horizon_30, horizon_60


def _valid_coordinate(row):
    try:
        lat, lon = float(row.get("latitude")), float(row.get("longitude"))
        return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
    except (TypeError, ValueError):
        return False


def _status(has_current, prediction_status, alert_status, optimization_summary, stale):
    if not has_current:
        return "blocked"
    if stale:
        return "degraded"
    if prediction_status in ("ready", "ok") and alert_status == "ok":
        return "ready"
    return "degraded"


def run_realtime_pipeline(realtime_source=None, *, current_records=None,
                          previous_successful_state=None,
                          station_coordinate_reference=None,
                          historical_data_source=None,
                          model_pack_path=None,
                          holiday_calendar_path=None,
                          vehicle_state=None,
                          locked=None,
                          config=None,
                          now=None,
                          route_provider=None,
                          dependencies=None):
    """Run realtime orchestration and return one strict-JSON payload.

    realtime_source is passed to realtime_normalizer. current_records may be a
    pre-normalized list/dict for historical replay or tests.
    """
    deps = dict(normalize_snapshots=normalize_snapshots,
                build_station_reference=build_station_reference,
                enrich_station_coordinates=enrich_station_coordinates,
                build_mapping=build_station_id_mapping_from_normalized,
                load_history_records=load_history_records,
                model_service_class=ModelService,
                evaluate_alerts=evaluate_alerts,
                optimize=optimize)
    deps.update(dependencies or {})
    cfg = copy.deepcopy(config or {})
    clock = _time(now)
    diagnostics = []
    stale = False
    source_snapshot = None

    try:
        if current_records is not None:
            normalized = dict(stations=copy.deepcopy(_records(current_records, "stations")),
                              diagnostics=copy.deepcopy(current_records.get("diagnostics", [])) if isinstance(current_records, dict) else [],
                              selected_source_file=None,
                              latest_observed_at=None,
                              timezone="Asia/Taipei")
        else:
            normalized = deps["normalize_snapshots"](realtime_source)
        diagnostics.extend(_module_diagnostics("realtime_normalizer", normalized.get("diagnostics", [])))
        stations = copy.deepcopy(normalized.get("stations", []))
        source_snapshot = dict(selected_source_file=normalized.get("selected_source_file"),
                               latest_observed_at=normalized.get("latest_observed_at"),
                               timezone=normalized.get("timezone", "Asia/Taipei"))
        if not stations:
            raise ValueError("no current stations")
    except Exception as exc:
        previous = previous_successful_state or {}
        previous_stations = copy.deepcopy(previous.get("stations", []))
        if previous_stations:
            stations = previous_stations
            stale = True
            diagnostics.append(_diagnostic("pipeline", "realtime_update_failed_using_last_successful_state", exc))
            source_snapshot = copy.deepcopy(previous.get("source_snapshot", {}))
        else:
            diagnostics.append(_diagnostic("pipeline", "realtime_update_failed_no_previous_state", exc))
            result = dict(pipeline_status="blocked", stale=False, generated_at=clock.isoformat(),
                          source_snapshot=source_snapshot, stations=[],
                          prediction=dict(status="unavailable", horizon_30m=[], horizon_60m=[], diagnostics=[]),
                          alerts=dict(active_alerts=[], resolved_alerts=[], summary={}, diagnostics=[]),
                          optimization=dict(candidate_plans=[], requires_admin_confirmation=True, diagnostics=[]),
                          diagnostics=diagnostics)
            return _strict_json(result)

    try:
        reference = station_coordinate_reference
        if reference is None and cfg.get("station_reference_source"):
            reference = deps["build_station_reference"](cfg["station_reference_source"])
            diagnostics.extend(_module_diagnostics("station_reference", reference.get("diagnostics", [])))
        if reference is not None:
            enriched = deps["enrich_station_coordinates"](stations, reference,
                                                          overwrite_existing=bool(cfg.get("overwrite_existing_coordinates", False)))
            stations = enriched.get("stations", stations)
            diagnostics.extend(_module_diagnostics("station_reference", enriched.get("diagnostics", [])))
    except Exception as exc:
        diagnostics.append(_diagnostic("station_reference", "coordinate_enrichment_failed", exc))

    prediction_status, predictions, prediction_diagnostics = "unavailable", [], []
    if model_pack_path and historical_data_source:
        try:
            mapping = deps["build_mapping"](stations)
            diagnostics.extend(_module_diagnostics("history_adapter", mapping.get("diagnostics", [])))
            history = deps["load_history_records"](historical_data_source, mapping.get("mapping", {}),
                                                   end=clock.isoformat())
            diagnostics.extend(_module_diagnostics("history_adapter", history.get("diagnostics", [])))
            service = deps["model_service_class"](model_pack_path, holiday_calendar_path=holiday_calendar_path)
            prediction_result = service.predict(stations, history.get("records", []))
            prediction_status = prediction_result.get("status", "blocked")
            predictions = prediction_result.get("predictions", [])
            prediction_diagnostics = prediction_result.get("diagnostics", [])
        except Exception as exc:
            prediction_status = "blocked"
            prediction_diagnostics = [_diagnostic("model_service", "prediction_path_failed", exc)]
    elif model_pack_path:
        prediction_status = "blocked"
        prediction_diagnostics = [_diagnostic("model_service", "historical_data_source_missing",
                                             "Prediction requested without history")]
    horizon_30, horizon_60 = _split_predictions(predictions)

    try:
        alert_result = deps["evaluate_alerts"](stations, predictions=predictions,
                                               prediction_status=prediction_status,
                                               previous_state=(previous_successful_state or {}).get("alerts"),
                                               rules=cfg.get("alert_rules"),
                                               now=clock.isoformat())
    except Exception as exc:
        alert_result = dict(status="degraded", active_alerts=[], resolved_alerts=[],
                            alert_summary={}, diagnostics=[dict(code="alert_engine_failed", message=str(exc))],
                            prediction_status=prediction_status)
    diagnostics.extend(_module_diagnostics("model_service", prediction_diagnostics))
    diagnostics.extend(_module_diagnostics("alert_engine", alert_result.get("diagnostics", [])))

    try:
        optimization_result = deps["optimize"](alert_result.get("active_alerts", []), stations,
                                               vehicle_state or [], locked=locked,
                                               rules=cfg.get("optimizer_rules"),
                                               now=clock.isoformat(),
                                               road_time_provider=route_provider,
                                               prediction_status=prediction_status)
        _strict_json(optimization_result)
    except Exception as exc:
        optimization_result = dict(candidate_plans=[], excluded_alerts=[], unmet_demand=[],
                                   diagnostics=[dict(code="optimizer_failed", message=str(exc))],
                                   requires_admin_confirmation=True,
                                   optimization_summary=dict(candidate_count=0, prediction_status=prediction_status))
    diagnostics.extend(_module_diagnostics("optimizer", optimization_result.get("diagnostics", [])))

    route_method = None
    plans = optimization_result.get("candidate_plans", [])
    if plans:
        route_method = plans[0].get("distance_method")
    elif route_provider is None:
        route_method = "haversine"

    pipeline_status = _status(bool(stations), prediction_status, alert_result.get("status"),
                              optimization_result.get("optimization_summary", {}), stale)
    result = dict(pipeline_status=pipeline_status, stale=stale,
                  generated_at=clock.isoformat(),
                  source_snapshot=source_snapshot,
                  stations=stations,
                  prediction=dict(status=prediction_status, horizon_30m=horizon_30,
                                  horizon_60m=horizon_60,
                                  diagnostics=prediction_diagnostics),
                  alerts=dict(active_alerts=alert_result.get("active_alerts", []),
                              resolved_alerts=alert_result.get("resolved_alerts", []),
                              summary=alert_result.get("alert_summary", alert_result.get("summary", {})),
                              diagnostics=alert_result.get("diagnostics", [])),
                  optimization=dict(candidate_plans=plans,
                                    excluded_alerts=optimization_result.get("excluded_alerts", []),
                                    unmet_demand=optimization_result.get("unmet_demand", []),
                                    requires_admin_confirmation=True,
                                    route_estimation_method=route_method,
                                    diagnostics=optimization_result.get("diagnostics", []),
                                    summary=optimization_result.get("optimization_summary", {})),
                  diagnostics=diagnostics)
    return _strict_json(result)
