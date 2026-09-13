"""Adapter for cleaned monthly YouBike history.

The cleaned monthly files are authoritative history but they do not carry the
runtime system identifier. This module never fabricates official_station_id; a
caller must provide a station_name -> official_station_id mapping when records
are intended for the current feature_engineering contract.
"""

import csv
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.feature_engineering import align_model_time_slot

TAIPEI = ZoneInfo("Asia/Taipei")
REQUIRED_COLUMNS = ("datetime", "station_name", "total_slots", "bikes", "district", "lng", "lat")


def _paths(source):
    path = Path(source)
    if path.is_dir():
        return sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".csv")
    return [path]


def _local_time(value):
    stamp = datetime.fromisoformat(str(value).strip())
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TAIPEI)
    return stamp.astimezone(TAIPEI)


def _int(value, field):
    if str(value).strip() == "":
        raise ValueError(f"{field} missing")
    number = float(value)
    if not math.isfinite(number) or int(number) != number:
        raise ValueError(f"{field} must be an integer")
    return int(number)


def _float(value, field, limit):
    number = float(value)
    if not math.isfinite(number) or abs(number) > limit:
        raise ValueError(f"{field} out of range")
    return number


def _json_ready(rows):
    json.dumps(rows, ensure_ascii=False, allow_nan=False)
    return rows


def load_history_records(source, station_id_mapping=None, start=None, end=None,
                         station_names=None, alignment_tolerance_seconds=0,
                         invalid_row_policy="exclude"):
    """Return cleaned history rows in the feature_engineering history shape.

    source may be a CSV file or a directory of CSV files. start/end are inclusive
    local Taipei datetimes. station_names filters by station_name. Invalid rows
    are excluded by default and reported in diagnostics; no values are clamped.
    """
    if invalid_row_policy != "exclude":
        raise ValueError("only invalid_row_policy='exclude' is supported")
    mapping = dict(station_id_mapping or {})
    station_filter = set(station_names) if station_names is not None else None
    start_time = _local_time(start) if start is not None else None
    end_time = _local_time(end) if end is not None else None
    records, diagnostics = [], []
    last_slot_by_station = {}

    def note(code, file_path, row_number=None, station_name=None, message=""):
        diagnostics.append(dict(code=code, source_file=str(file_path), row=row_number,
                                station_name=station_name, message=str(message)))

    for file_path in _paths(source):
        try:
            with Path(file_path).open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                columns = reader.fieldnames or []
                missing = [col for col in REQUIRED_COLUMNS if col not in columns]
                if missing:
                    note("missing_columns", file_path, message=", ".join(missing))
                    continue
                for row_number, row in enumerate(reader, 2):
                    name = (row.get("station_name") or "").strip()
                    if station_filter is not None and name not in station_filter:
                        continue
                    try:
                        if not name:
                            raise ValueError("station_name missing")
                        observed = _local_time(row["datetime"])
                        if start_time is not None and observed < start_time:
                            continue
                        if end_time is not None and observed > end_time:
                            continue
                        official_id = mapping.get(name)
                        if not official_id:
                            note("missing_official_station_id_mapping", file_path, row_number, name)
                            continue
                        total = _int(row["total_slots"], "total_slots")
                        bikes = _int(row["bikes"], "bikes")
                        if bikes < 0:
                            raise ValueError("bikes < 0")
                        if total <= 0:
                            raise ValueError("total_slots <= 0")
                        if bikes > total:
                            raise ValueError("bikes > total_slots")
                        lat = _float(row["lat"], "lat", 90)
                        lon = _float(row["lng"], "lng", 180)
                        slot = align_model_time_slot(observed, alignment_tolerance_seconds)
                    except Exception as exc:
                        note("invalid_history_row", file_path, row_number, name or None, exc)
                        continue
                    previous = last_slot_by_station.get(name)
                    if previous is not None and slot <= previous:
                        note("non_monotonic_model_time_slot", file_path, row_number, name,
                             f"{previous.isoformat()} -> {slot.isoformat()}")
                    elif previous is not None and (slot - previous).total_seconds() != 1800:
                        note("non_30_minute_gap", file_path, row_number, name,
                             f"{previous.isoformat()} -> {slot.isoformat()}")
                    last_slot_by_station[name] = slot
                    records.append(dict(official_station_id=str(official_id),
                                        station_name=name,
                                        district=row["district"],
                                        observed_at=observed.isoformat(),
                                        model_time_slot=slot.isoformat(),
                                        total_slots=total,
                                        available_bikes=bikes,
                                        latitude=lat,
                                        longitude=lon))
        except OSError as exc:
            note("unreadable_csv", file_path, message=exc)
    records.sort(key=lambda r: (r["station_name"], r["model_time_slot"]))
    return dict(records=_json_ready(records), diagnostics=_json_ready(diagnostics),
                record_count=len(records), source_files=[str(p) for p in _paths(source)])


def build_station_id_mapping_from_normalized(normalized):
    """Build station_name -> official_station_id from realtime normalized rows."""
    rows = normalized.get("stations", []) if isinstance(normalized, dict) else normalized
    mapping, diagnostics = {}, []
    for row in rows:
        name = row.get("station_name") if isinstance(row, dict) else None
        sid = row.get("official_station_id") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name or not isinstance(sid, str) or not sid:
            diagnostics.append(dict(code="invalid_normalized_station_identity", station_name=name,
                                    official_station_id=sid))
            continue
        if name in mapping and mapping[name] != sid:
            diagnostics.append(dict(code="station_name_maps_to_multiple_official_ids",
                                    station_name=name, official_station_id=sid))
            continue
        mapping[name] = sid
    return dict(mapping=mapping, diagnostics=diagnostics, mapped_count=len(mapping))
