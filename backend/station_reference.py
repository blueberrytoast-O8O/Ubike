"""Station coordinate reference built from cleaned monthly history."""

import csv
import copy
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
REQUIRED_COLUMNS = ("datetime", "station_name", "lng", "lat")


def _paths(source):
    path = Path(source)
    if path.is_dir():
        return sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".csv")
    return [path]


def _time(value):
    stamp = datetime.fromisoformat(str(value).strip())
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TAIPEI)
    return stamp.astimezone(TAIPEI)


def _coordinate(row):
    lat = float(row.get("lat"))
    lon = float(row.get("lng"))
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("coordinate out of range")
    return lat, lon


def _has_valid_station_coordinate(row):
    try:
        lat = float(row.get("latitude"))
        lon = float(row.get("longitude"))
        return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
    except (TypeError, ValueError):
        return False


def build_station_reference(source, coordinate_selection_policy="latest_observation"):
    """Scan cleaned history and return station_name -> latitude/longitude.

    latest_observation is deterministic: newest datetime wins; ties use the
    lexicographically greatest coordinate pair.
    """
    if coordinate_selection_policy != "latest_observation":
        raise ValueError("unsupported coordinate_selection_policy")
    candidates, coordinate_sets, diagnostics = {}, {}, []

    def note(code, file_path, row_number=None, station_name=None, message="", **extra):
        item = dict(code=code, source_file=str(file_path), row=row_number,
                    station_name=station_name, message=str(message))
        item.update(extra)
        diagnostics.append(item)

    for file_path in _paths(source):
        try:
            with Path(file_path).open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                missing = [col for col in REQUIRED_COLUMNS if col not in (reader.fieldnames or [])]
                if missing:
                    note("missing_columns", file_path, message=", ".join(missing))
                    continue
                for row_number, row in enumerate(reader, 2):
                    name = (row.get("station_name") or "").strip()
                    try:
                        if not name:
                            raise ValueError("station_name missing")
                        stamp = _time(row["datetime"])
                        lat, lon = _coordinate(row)
                    except Exception as exc:
                        note("invalid_coordinate_row", file_path, row_number, name or None, exc)
                        continue
                    pair = (round(lat, 7), round(lon, 7))
                    coordinate_sets.setdefault(name, set()).add(pair)
                    current = candidates.get(name)
                    if current is None or (stamp, pair) > (current["observed_at"], current["pair"]):
                        candidates[name] = dict(observed_at=stamp, pair=pair, latitude=lat, longitude=lon,
                                                source_file=str(file_path), row=row_number)
        except OSError as exc:
            note("unreadable_csv", file_path, message=exc)

    reference = {}
    for name in sorted(candidates):
        choice = candidates[name]
        distinct_count = len(coordinate_sets[name])
        if distinct_count > 1:
            diagnostics.append(dict(code="multiple_historical_coordinates", source_file=choice["source_file"],
                                    row=choice["row"], station_name=name,
                                    message="selected latest_observation coordinate",
                                    distinct_coordinate_count=distinct_count,
                                    coordinate_selection_policy=coordinate_selection_policy))
        reference[name] = dict(station_name=name, latitude=choice["latitude"], longitude=choice["longitude"],
                               observed_at=choice["observed_at"].isoformat(),
                               source_file=choice["source_file"],
                               coordinate_selection_policy=coordinate_selection_policy,
                               distinct_coordinate_count=distinct_count)
    json.dumps(reference, ensure_ascii=False, allow_nan=False)
    json.dumps(diagnostics, ensure_ascii=False, allow_nan=False)
    return dict(station_reference=reference, diagnostics=diagnostics,
                station_count=len(reference), coordinate_selection_policy=coordinate_selection_policy)


def enrich_station_coordinates(normalized_stations, station_reference, overwrite_existing=False):
    """Return copied station rows enriched by station_name, without mutating input."""
    rows = normalized_stations.get("stations", []) if isinstance(normalized_stations, dict) else normalized_stations
    ref = station_reference.get("station_reference", station_reference)
    enriched, diagnostics = [], []
    for row in rows:
        item = copy.deepcopy(row)
        name = item.get("station_name") if isinstance(item, dict) else None
        if _has_valid_station_coordinate(item) and not overwrite_existing:
            enriched.append(item)
            continue
        match = ref.get(name) if isinstance(name, str) else None
        if not match:
            item["latitude"] = item.get("latitude")
            item["longitude"] = item.get("longitude")
            diagnostics.append(dict(code="station_reference_not_found", station_name=name,
                                    official_station_id=item.get("official_station_id")))
            enriched.append(item)
            continue
        item["latitude"] = match["latitude"]
        item["longitude"] = match["longitude"]
        enriched.append(item)
    json.dumps(enriched, ensure_ascii=False, allow_nan=False)
    json.dumps(diagnostics, ensure_ascii=False, allow_nan=False)
    return dict(stations=enriched, diagnostics=diagnostics,
                enriched_count=sum(1 for row in enriched if _has_valid_station_coordinate(row)),
                unmatched_count=sum(1 for d in diagnostics if d["code"] == "station_reference_not_found"))
