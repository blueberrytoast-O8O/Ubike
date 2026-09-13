"""V2 feature contract transcribed from the user-supplied training program.

build_features(current, history, categories) accepts normalized station records
or normalizer results. It emits one latest/current feature row per requested
station, never forecasts a past row as a substitute for a blocked current row.
The returned `features` is an internal pandas DataFrame, not a JSON payload.
`stations`, `blocked_stations` and `diagnostics` contain JSON-safe metadata.

Counts and flags use int64, pandas 2.2 calendar components use int32, derived
features use float64, and station_id is an unordered categorical with EXACT
model category order. This is an explicit inference schema: the training code
did not persist original numeric dtypes, which also depended on its input CSVs.

Require 672 preceding model slots plus current, with no missing half-hour slot.
Raw observed_at is preserved. Nearest-slot alignment accepts configurable drift
(default 120 seconds); duplicate slots block the station rather than selecting
one observation. Calendar JSON is injectable and includes explicit coverage.
Current and adjacent model dates must be covered; no holidays are inferred.
"""

import json
import math
from pathlib import Path
from collections import defaultdict
from datetime import date, timedelta

FEATURES = tuple("""station_id total_slots month day hour minute day_of_week
time_slot week_slot hour_sin hour_cos week_sin week_cos is_weekend
is_national_holiday is_day_off is_day_before_holiday is_day_after_holiday
bikes bike_ratio lag_30m lag_1h lag_2h lag_24h lag_48h lag_1w lag_2w
diff_30m diff_1h diff_2h change_rate_30m change_rate_1h rolling_mean_1h
rolling_mean_2h rolling_mean_3h rolling_std_2h rolling_min_2h rolling_max_2h""".split())
LAGS = dict(lag_30m=1, lag_1h=2, lag_2h=4, lag_24h=48,
            lag_48h=96, lag_1w=336, lag_2w=672)
CALENDAR = "month day hour minute day_of_week time_slot week_slot".split()
FLAGS = "is_weekend is_national_holiday is_day_off is_day_before_holiday is_day_after_holiday".split()
DTYPES = {f: ("category" if f == "station_id" else "int32" if f in CALENDAR
              else "int64" if f in FLAGS + ["total_slots", "bikes"] else "float64")
          for f in FEATURES}
DEFAULT_ALIGNMENT_TOLERANCE_SECONDS = 120
DEFAULT_CALENDAR_PATH = Path(__file__).with_name("config") / "training_holidays.json"


def load_holiday_calendar(path=None):
    """Read a calendar JSON without modifying it; reject invalid coverage/dates."""
    with Path(path or DEFAULT_CALENDAR_PATH).open(encoding="utf-8-sig") as handle:
        config = json.load(handle)
    start = date.fromisoformat(config["coverage_start"])
    end = date.fromisoformat(config["coverage_end"])
    holidays = config["holidays"]
    if start > end or not isinstance(holidays, list):
        raise ValueError("invalid holiday calendar coverage or holidays")
    parsed = [date.fromisoformat(day) for day in holidays]
    if any(day < start or day > end for day in parsed):
        raise ValueError("holiday outside declared coverage")
    return dict(coverage_start=start, coverage_end=end,
                holidays=frozenset(day.isoformat() for day in parsed))


def align_model_time_slot(value, tolerance_seconds=DEFAULT_ALIGNMENT_TOLERANCE_SECONDS):
    """Return nearest Taipei half-hour slot; reject drift beyond tolerance.

    Tolerance is inclusive and must be below 15 minutes to avoid ambiguous slots.
    """
    if isinstance(tolerance_seconds, bool) or not isinstance(tolerance_seconds, (int, float)) or not math.isfinite(tolerance_seconds) or not 0 <= tolerance_seconds < 900:
        raise ValueError("alignment tolerance must be finite and in [0, 900) seconds")
    stamp = _timestamp(value)
    slot = stamp.round("30min")
    if abs((stamp - slot).total_seconds()) > tolerance_seconds:
        raise ValueError(f"off_schedule_snapshot: {stamp.isoformat()}, tolerance={tolerance_seconds}s")
    return slot


def _records(value):
    return list(value["stations"] if isinstance(value, dict) else value)


def _timestamp(value):
    import pandas as pd
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("observed_at must include timezone")
    return stamp.tz_convert("Asia/Taipei")


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("counts must be numeric integers")
    if not math.isfinite(value) or value < 0 or int(value) != value or value > 2**53:
        raise ValueError("invalid count")
    return int(value)


def _station_features(rows, categories, calendar=None):
    """Formula kernel on one validated, ordered station; retains NaN as trained."""
    import numpy as np
    import pandas as pd
    calendar = load_holiday_calendar() if calendar is None else calendar
    data = pd.DataFrame(rows)
    dates = pd.DatetimeIndex(data["model_time_slot"])
    # Local wall dates match the training program's naive holiday dates.
    local = dates.tz_localize(None)
    frame = pd.DataFrame(index=data.index)
    frame["station_id"] = pd.Categorical(data["station_name"], categories=categories, ordered=False)
    frame["total_slots"] = data["total_slots"]
    for key in CALENDAR[:5]:
        frame[key] = getattr(local, "dayofweek" if key == "day_of_week" else key)
    frame["time_slot"] = frame.hour * 2 + frame.minute // 30
    frame["week_slot"] = frame.day_of_week * 48 + frame.time_slot
    for prefix, slot, period in (("hour", "time_slot", 48), ("week", "week_slot", 336)):
        frame[prefix + "_sin"] = np.sin(2 * np.pi * frame[slot] / period)
        frame[prefix + "_cos"] = np.cos(2 * np.pi * frame[slot] / period)
    frame["is_weekend"] = frame.day_of_week.isin([5, 6]).astype(int)
    frame["is_national_holiday"] = local.strftime("%Y-%m-%d").isin(calendar["holidays"]).astype(int)
    frame["is_day_off"] = ((frame.is_weekend == 1) | (frame.is_national_holiday == 1)).astype(int)
    for key, delta in (("is_day_before_holiday", 1), ("is_day_after_holiday", -1)):
        frame[key] = (local.normalize() + pd.Timedelta(days=delta)).strftime("%Y-%m-%d").isin(calendar["holidays"]).astype(int)
    bikes = data["available_bikes"]
    frame["bikes"] = bikes
    frame["bike_ratio"] = bikes / data.total_slots.replace(0, np.nan)
    for key, steps in LAGS.items():
        frame[key] = bikes.shift(steps)
    for suffix in ("30m", "1h", "2h"):
        frame["diff_" + suffix] = bikes - frame["lag_" + suffix]
    for suffix in ("30m", "1h"):
        frame["change_rate_" + suffix] = frame["diff_" + suffix] / frame["lag_" + suffix].replace(0, np.nan)
    previous = bikes.shift(1)
    for hours, window in ((1, 2), (2, 4), (3, 6)):
        frame[f"rolling_mean_{hours}h"] = previous.rolling(window, min_periods=window).mean()
    window = previous.rolling(4, min_periods=2)
    frame["rolling_std_2h"] = window.std(ddof=1)
    frame["rolling_min_2h"] = window.min()
    frame["rolling_max_2h"] = window.max()
    frame = frame.loc[:, list(FEATURES)].replace([np.inf, -np.inf], np.nan)
    return frame.astype({key: dtype for key, dtype in DTYPES.items() if key != "station_id"})


def build_features(current, history, categories, *,
                   alignment_tolerance_seconds=DEFAULT_ALIGNMENT_TOLERANCE_SECONDS,
                   holiday_calendar_path=None):
    """Return ready/partial/blocked with per-station diagnostics and aligned rows."""
    import pandas as pd
    calendar = load_holiday_calendar(holiday_calendar_path)
    # Validate configuration even for empty input.
    align_model_time_slot("2026-01-01T00:00:00+08:00", alignment_tolerance_seconds)
    categories = list(categories)
    if not categories or any(not isinstance(c, str) or not c for c in categories) or len(set(categories)) != len(categories):
        raise ValueError("invalid model category contract")
    diagnostics, blocked, frames, metadata = [], [], [], []
    grouped = defaultdict(list)
    targets = defaultdict(list)
    for row in _records(history):
        grouped[row.get("station_name")].append(row)
    for row in _records(current):
        targets[row.get("station_name")].append(row)

    for name, requests in targets.items():
        first = requests[-1]
        identity = dict(official_station_id=first.get("official_station_id"), station_name=name)
        station_diagnostics = []

        def note(code, message, severity="error"):
            item = dict(code=code, message=message, severity=severity, **identity)
            diagnostics.append(item)
            station_diagnostics.append(item)

        if not isinstance(name, str) or name not in categories:
            note("unknown_model_station", "Station name absent from model categories")
        if len(requests) != 1:
            note("duplicate_current_station", "Exactly one current row per station_name is required")
        valid = []
        current_slot = None
        try:
            current_time = _timestamp(first["observed_at"])
            try:
                current_slot = align_model_time_slot(current_time, alignment_tolerance_seconds)
            except ValueError as exc:
                note("off_schedule_snapshot", str(exc))
            if not isinstance(identity["official_station_id"], str) or not identity["official_station_id"]:
                raise ValueError("official_station_id must be a nonempty string")
            for source in grouped[name] + [first]:
                stamp = _timestamp(source["observed_at"])
                if stamp > current_time:
                    note("future_history_ignored", "History after observed_at is excluded", "warning")
                    continue
                if source["official_station_id"] != identity["official_station_id"]:
                    raise ValueError("station_name maps to multiple official IDs")
                try:
                    slot = align_model_time_slot(stamp, alignment_tolerance_seconds)
                except ValueError as exc:
                    if source is not first:
                        note("off_schedule_snapshot", str(exc))
                    continue
                row = dict(source, observed_at=stamp, model_time_slot=slot,
                           available_bikes=_count(source["available_bikes"]),
                           total_slots=_count(source["total_slots"]))
                if row["available_bikes"] > row["total_slots"]:
                    raise ValueError("available_bikes exceeds total_slots")
                valid.append(row)
            slots = set()
            for row in valid:
                slot = row["model_time_slot"]
                if slot in slots:
                    note("duplicate_model_time_slot", f"Multiple observations in {slot.isoformat()}; station blocked")
                slots.add(slot)
            valid.sort(key=lambda row: row["model_time_slot"])
            if len(slots) < 673:
                note("insufficient_history", f"Need 673 model slots including current; received {len(slots)}")
            ordered_slots = sorted(slots)
            if any(b - a != pd.Timedelta(minutes=30) for a, b in zip(ordered_slots, ordered_slots[1:])):
                note("non_contiguous_history", "Consecutive model_time_slot values must be 30 minutes apart")
            day = (current_slot if current_slot is not None else current_time).date()
            if day - timedelta(days=1) < calendar["coverage_start"] or day + timedelta(days=1) > calendar["coverage_end"]:
                note("holiday_calendar_out_of_range", f"Current/adjacent model dates exceed calendar coverage {calendar['coverage_start']}..{calendar['coverage_end']}")
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            note("invalid_station_history", str(exc))
        if any(d["severity"] == "error" for d in station_diagnostics):
            blocked.append(dict(**identity, observed_at=first.get("observed_at"),
                                model_time_slot=current_slot.isoformat() if current_slot is not None else None,
                                diagnostics=station_diagnostics))
            continue
        frame = _station_features(valid, categories, calendar).tail(1).reset_index(drop=True)
        missing = frame.columns[frame.isna().any()].tolist()
        if missing:
            note("undefined_features", "Training dropna rejects: " + ", ".join(missing))
            blocked.append(dict(**identity, observed_at=first.get("observed_at"),
                                model_time_slot=current_slot.isoformat() if current_slot is not None else None,
                                diagnostics=station_diagnostics))
            continue
        frames.append(frame)
        metadata.append(dict(**identity, observed_at=first["observed_at"],
                             model_time_slot=current_slot.isoformat(),
                             total_slots=valid[-1]["total_slots"], diagnostics=station_diagnostics))
    empty = pd.DataFrame({key: pd.Series(dtype=dtype) for key, dtype in DTYPES.items()})
    empty["station_id"] = pd.Categorical([], categories=categories, ordered=False)
    return dict(status="partial" if frames and blocked else "ready" if frames else "blocked",
                features=pd.concat(frames, ignore_index=True) if frames else empty,
                stations=metadata, blocked_stations=blocked, diagnostics=diagnostics)
