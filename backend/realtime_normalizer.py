"""Read-only CSV normalization, independent of models and infrastructure.

normalize_snapshots(path_or_paths) returns JSON-ready stations, diagnostics,
selected_source_file and latest_observed_at. Directories include immediate CSV
children. Snapshot rank is its maximum parseable timestamp (even when that row's
counts are invalid); ties use the lexically greatest absolute path. No merging
of older snapshots occurs. Duplicate IDs keep the latest valid row, then the
last row on ties. Invalid rows are skipped, never coerced to zero.

Naive times are interpreted as Asia/Taipei. 抓取時間 is only a collection-time
proxy for observed_at, explicitly identified in observed_at_source. Missing
coordinates are null. A supplied dock count is retained on capacity mismatch;
only an absent dock column permits derivation, marked by available_docks_source.
"""

import csv
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    TAIPEI = ZoneInfo("Asia/Taipei")
except (ImportError, ZoneInfoNotFoundError):
    # Modern Taipei has used UTC+08 continuously since 1980. Reject older dates
    # in this fallback rather than silently misrepresent historical DST.
    TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")


ALIASES = {
    "official_station_id": ("official_station_id", "站號", "sno"),
    "station_name": ("station_name", "站名", "sna"),
    "district": ("district", "行政區", "sarea"),
    "observed_at": ("observed_at", "mday", "抓取時間"),
    "latitude": ("latitude", "lat", "緯度"),
    "longitude": ("longitude", "lng", "lon", "經度"),
    "total_slots": ("total_slots", "總車位", "tot"),
    "available_bikes": ("available_bikes", "可借車數", "sbi"),
    "available_docks": ("available_docks", "可還車位", "bemp"),
}
REQUIRED = set(ALIASES) - {"latitude", "longitude", "available_docks"}


def _time(value):
    value = value.strip()
    if not value:
        raise ValueError("missing observed_at")
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}_\d{2}:\d{2}:\d{2}", value):
        result = datetime.strptime(value, "%Y/%m/%d_%H:%M:%S")
    elif re.fullmatch(r"\d{14}", value):
        result = datetime.strptime(value, "%Y%m%d%H%M%S")
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(TAIPEI, timezone) and result.year < 1980:
        raise ValueError("Historical Taipei times require zoneinfo timezone data")
    if result.tzinfo is None:
        result = result.replace(tzinfo=TAIPEI)
    return result.astimezone(TAIPEI)


def _count(value, field):
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"{field}: expected non-negative integer, got {value!r}")
    return int(value)


def normalize_snapshots(inputs):
    """Accept one CSV, an iterable of CSV paths, or a directory; never write input.

    UTF-8 (with/without BOM) is supported. File/schema/row problems are returned
    as diagnostics. If no timestamp can be parsed, selection and latest time
    are null and stations is empty. Row numbers include the CSV header.
    """
    diagnostics = []

    def diagnostic(code, source, row=None, message="", station_id=None):
        diagnostics.append(dict(code=code, source_file=str(source), row=row,
                                official_station_id=station_id, message=message))

    if isinstance(inputs, (str, Path)):
        path = Path(inputs)
        paths = sorted(p for p in path.iterdir() if p.suffix.lower() == ".csv" and p.is_file()) if path.is_dir() else [path]
    else:
        paths = [Path(p) for p in inputs]
    candidates = []
    for path in sorted({p.resolve() for p in paths}):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle, strict=True)
                headers = reader.fieldnames or []
                if len(headers) != len(set(headers)):
                    raise ValueError("duplicate column names")
                columns = {key: next((a for a in aliases if a in headers), None)
                           for key, aliases in ALIASES.items()}
                missing = sorted(key for key in REQUIRED if columns[key] is None)
                if missing:
                    diagnostic("missing_columns", path, message=", ".join(missing))
                    continue
                parsed = []
                for row_number, row in enumerate(reader, 2):
                    if None in row or any(v is None for v in row.values()):
                        diagnostic("malformed_row", path, row_number, "column count mismatch")
                        continue
                    try:
                        observed = _time(row[columns["observed_at"]])
                    except ValueError as exc:
                        diagnostic("invalid_time", path, row_number, str(exc))
                        continue
                    parsed.append((row_number, row, observed))
                if parsed:
                    candidates.append((max(r[2] for r in parsed), str(path), columns, parsed))
                else:
                    diagnostic("no_valid_timestamps", path)
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            diagnostic("unreadable_csv", path, message=str(exc))

    result = dict(stations=[], diagnostics=diagnostics, selected_source_file=None,
                  latest_observed_at=None, timezone="Asia/Taipei")
    if not candidates:
        diagnostic("no_snapshot", "", message="No readable snapshot with valid timestamps")
        return result
    latest, source, columns, rows = max(candidates, key=lambda c: (c[0], c[1]))
    result.update(selected_source_file=source, latest_observed_at=latest.isoformat())
    if columns["observed_at"] == "抓取時間":
        diagnostic("collection_time_proxy", source, message="observed_at uses collection time; source has no station update timestamp")
    stations = {}
    for number, row, observed in rows:
        values = {key: row[col].strip() if col else "" for key, col in columns.items()}
        sid = values["official_station_id"]
        try:
            for key in ("official_station_id", "station_name", "district"):
                if not values[key]:
                    raise ValueError(f"missing {key}")
            total = _count(values["total_slots"], "total_slots")
            bikes = _count(values["available_bikes"], "available_bikes")
            docks = (_count(values["available_docks"], "available_docks")
                     if columns["available_docks"] else total - bikes)
            if bikes > total or docks > total:
                raise ValueError("bikes or docks exceed total_slots")
        except ValueError as exc:
            diagnostic("invalid_station", source, number, str(exc), sid)
            continue
        station = {key: values[key] for key in ("official_station_id", "station_name", "district")}
        station.update(observed_at=observed.isoformat(), observed_at_source=columns["observed_at"],
                       timezone="Asia/Taipei", total_slots=total, available_bikes=bikes,
                       available_docks=docks, source_file=source,
                       available_docks_source="reported" if columns["available_docks"] else "derived_total_minus_bikes")
        for key, limit in (("latitude", 90), ("longitude", 180)):
            station[key] = None
            try:
                if not values[key]:
                    diagnostic("missing_coordinate", source, number, key, sid)
                    continue
                coordinate = float(values[key])
                if not math.isfinite(coordinate) or abs(coordinate) > limit:
                    raise ValueError("out of range or non-finite")
                station[key] = coordinate
            except ValueError:
                diagnostic("invalid_coordinate", source, number, key, sid)
        if docks != total - bikes:
            diagnostic("dock_count_mismatch", source, number,
                       f"reported={docks}, total_minus_bikes={total - bikes}", sid)
        if not columns["available_docks"]:
            diagnostic("derived_docks", source, number, "total_slots - available_bikes", sid)
        if sid in stations:
            diagnostic("duplicate_station", source, number, "Keep latest valid time, last row on tie", sid)
            if observed < _time(stations[sid]["observed_at"]):
                continue
        stations[sid] = station
    result["stations"] = [stations[sid] for sid in sorted(stations)]
    return result
