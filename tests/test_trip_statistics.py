"""Unit tests for trip_statistics helpers."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.toyota.trip_statistics import (
    _import_statistics,
    _month_end,
    _prev_month,
    _trip_to_dict,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _FakeLocation:
    def __init__(self, lat: float, lon: float) -> None:
        self.lat = lat
        self.lon = lon


class _FakeLocations:
    def __init__(self, start=None, end=None) -> None:
        self.start = start
        self.end = end


def _make_trip(
    start: datetime,
    distance: float = 33.01,
    fuel: float = 1.229,
    avg_fuel: float = 3.723,
    ev_distance: float | None = 12.701,
    duration_s: int = 2302,
    ev_duration_s: int | None = 1226,
    locations=None,
) -> MagicMock:
    trip = MagicMock()
    trip.start_time = start
    trip.end_time = start + timedelta(seconds=duration_s)
    trip.duration = timedelta(seconds=duration_s)
    trip.distance = distance
    trip.fuel_consumed = fuel
    trip.average_fuel_consumed = avg_fuel
    trip.ev_distance = ev_distance
    trip.ev_duration = timedelta(seconds=ev_duration_s) if ev_duration_s is not None else None
    trip.locations = locations or _FakeLocations(
        start=_FakeLocation(44.8841, 10.3555),
        end=_FakeLocation(44.6973, 10.5304),
    )
    return trip


_T0 = datetime(2026, 1, 10, 9, 5, 0, tzinfo=timezone.utc)
_T1 = datetime(2026, 1, 12, 14, 0, 0, tzinfo=timezone.utc)
_T2 = datetime(2026, 1, 15, 8, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def test_month_end_january():
    assert _month_end(date(2026, 1, 1)) == date(2026, 1, 31)


def test_month_end_february_non_leap():
    assert _month_end(date(2025, 2, 1)) == date(2025, 2, 28)


def test_month_end_february_leap():
    assert _month_end(date(2024, 2, 1)) == date(2024, 2, 29)


def test_prev_month_normal():
    assert _prev_month(date(2026, 3, 15)) == date(2026, 2, 1)


def test_prev_month_crosses_year():
    assert _prev_month(date(2026, 1, 1)) == date(2025, 12, 1)


# ---------------------------------------------------------------------------
# _trip_to_dict
# ---------------------------------------------------------------------------

def test_trip_to_dict_full():
    trip = _make_trip(_T0)
    d = _trip_to_dict(trip)

    assert d["start_time"] == _T0.isoformat()
    assert d["distance_km"] == 33.01
    assert d["fuel_consumed_l"] == 1.229
    assert d["avg_fuel_l100km"] == 3.723
    assert d["ev_distance_km"] == 12.701
    assert d["duration_seconds"] == 2302
    assert d["ev_duration_seconds"] == 1226
    assert d["start_lat"] == 44.8841
    assert d["start_lon"] == 10.3555
    assert d["end_lat"] == 44.6973
    assert d["end_lon"] == 10.5304


def test_trip_to_dict_no_locations():
    trip = _make_trip(_T0, locations=_FakeLocations(start=None, end=None))
    d = _trip_to_dict(trip)
    assert d["start_lat"] is None
    assert d["end_lat"] is None


def test_trip_to_dict_no_ev():
    trip = _make_trip(_T0, ev_distance=None, ev_duration_s=None)
    d = _trip_to_dict(trip)
    assert d["ev_distance_km"] is None
    assert d["ev_duration_seconds"] is None


# ---------------------------------------------------------------------------
# _import_statistics — cumulative sums
# ---------------------------------------------------------------------------

def _make_trip_dict(start: datetime, distance: float, fuel: float, ev: float | None, dur_s: int) -> dict:
    return {
        "start_time": start.isoformat(),
        "distance_km": distance,
        "fuel_consumed_l": fuel,
        "ev_distance_km": ev,
        "duration_seconds": dur_s,
    }


def test_import_statistics_calls_recorder():
    trips = [
        _make_trip_dict(_T0, distance=10.0, fuel=0.5, ev=5.0, dur_s=600),
        _make_trip_dict(_T1, distance=20.0, fuel=1.0, ev=None, dur_s=1200),
        _make_trip_dict(_T2, distance=15.0, fuel=0.8, ev=8.0, dur_s=900),
    ]

    calls: list = []

    with patch(
        "custom_components.toyota.trip_statistics.async_add_external_statistics",
        side_effect=lambda hass, meta, data: calls.append((meta["statistic_id"], list(data))),
    ):
        _import_statistics(MagicMock(), "TESTVIN01", trips)

    stat_ids = [c[0] for c in calls]
    assert "toyota:testvin01_trip_distance" in stat_ids
    assert "toyota:testvin01_trip_fuel_consumed" in stat_ids
    assert "toyota:testvin01_trip_ev_distance" in stat_ids
    assert "toyota:testvin01_trip_duration_min" in stat_ids


def test_import_statistics_cumulative_distance():
    trips = [
        _make_trip_dict(_T0, distance=10.0, fuel=0.5, ev=None, dur_s=600),
        _make_trip_dict(_T1, distance=20.0, fuel=1.0, ev=None, dur_s=1200),
    ]
    captured: list = []
    with patch(
        "custom_components.toyota.trip_statistics.async_add_external_statistics",
        side_effect=lambda hass, meta, data: captured.append((meta["statistic_id"], list(data))),
    ):
        _import_statistics(MagicMock(), "VIN01", trips)

    dist_rows = next(d for sid, d in captured if "distance" in sid)
    assert dist_rows[0]["sum"] == pytest.approx(10.0)
    assert dist_rows[1]["sum"] == pytest.approx(30.0)
    assert dist_rows[0]["state"] == pytest.approx(10.0)
    assert dist_rows[1]["state"] == pytest.approx(20.0)


def test_import_statistics_sorted_by_start_time():
    # Feed in reverse order — sums must still be chronological
    trips = [
        _make_trip_dict(_T2, distance=15.0, fuel=0.8, ev=None, dur_s=900),
        _make_trip_dict(_T0, distance=10.0, fuel=0.5, ev=None, dur_s=600),
    ]
    captured: list = []
    with patch(
        "custom_components.toyota.trip_statistics.async_add_external_statistics",
        side_effect=lambda hass, meta, data: captured.append((meta["statistic_id"], list(data))),
    ):
        _import_statistics(MagicMock(), "VIN02", trips)

    dist_rows = next(d for sid, d in captured if "distance" in sid)
    assert dist_rows[0]["start"] == datetime.fromisoformat(_T0.isoformat())
    assert dist_rows[0]["sum"] == pytest.approx(10.0)
    assert dist_rows[1]["sum"] == pytest.approx(25.0)


def test_import_statistics_ev_rows_only_when_ev_present():
    trips = [
        _make_trip_dict(_T0, distance=10.0, fuel=0.5, ev=None, dur_s=600),
        _make_trip_dict(_T1, distance=20.0, fuel=1.0, ev=5.0, dur_s=1200),
    ]
    captured: list = []
    with patch(
        "custom_components.toyota.trip_statistics.async_add_external_statistics",
        side_effect=lambda hass, meta, data: captured.append((meta["statistic_id"], list(data))),
    ):
        _import_statistics(MagicMock(), "VIN03", trips)

    ev_rows = next((d for sid, d in captured if "ev_distance" in sid), None)
    assert ev_rows is not None
    assert len(ev_rows) == 1  # only the second trip has ev_distance
    assert ev_rows[0]["sum"] == pytest.approx(5.0)


def test_import_statistics_empty_list_does_nothing():
    with patch(
        "custom_components.toyota.trip_statistics.async_add_external_statistics"
    ) as mock:
        _import_statistics(MagicMock(), "VIN99", [])
    mock.assert_not_called()


def test_import_statistics_vin_lowercased():
    trips = [_make_trip_dict(_T0, distance=5.0, fuel=0.2, ev=None, dur_s=300)]
    captured: list = []
    with patch(
        "custom_components.toyota.trip_statistics.async_add_external_statistics",
        side_effect=lambda hass, meta, data: captured.append(meta["statistic_id"]),
    ):
        _import_statistics(MagicMock(), "ABC123XYZ", trips)

    for sid in captured:
        assert "ABC" not in sid
        assert "abc123xyz" in sid
