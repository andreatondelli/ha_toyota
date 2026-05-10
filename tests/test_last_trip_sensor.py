"""Unit tests for the last_trip sensor value and attribute functions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.toyota.sensor import (
    LAST_TRIP_ENTITY_DESCRIPTION,
    _format_last_trip_attributes,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _FakeLocation:
    def __init__(self, lat: float, lon: float) -> None:
        self.lat = lat
        self.lon = lon


class _FakeLocations:
    def __init__(self, start, end) -> None:
        self.start = start
        self.end = end


class _FakeTrip:
    def __init__(
        self,
        distance: float,
        start_time: datetime,
        end_time: datetime,
        duration: timedelta,
        fuel_consumed: float,
        average_fuel_consumed: float,
        ev_distance=None,
        ev_duration=None,
        locations=None,
    ) -> None:
        self.distance = distance
        self.start_time = start_time
        self.end_time = end_time
        self.duration = duration
        self.fuel_consumed = fuel_consumed
        self.average_fuel_consumed = average_fuel_consumed
        self.ev_distance = ev_distance
        self.ev_duration = ev_duration
        self.locations = locations


class _FakeVehicle:
    def __init__(self, last_trip=None) -> None:
        self.last_trip = last_trip


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_START = datetime(2026, 5, 7, 15, 59, 30, tzinfo=timezone.utc)
_END   = datetime(2026, 5, 7, 16, 37, 52, tzinfo=timezone.utc)
_DURATION = timedelta(minutes=38, seconds=22)

_LOCATIONS = _FakeLocations(
    start=_FakeLocation(lat=44.8841, lon=10.3555),
    end=_FakeLocation(lat=44.6973, lon=10.5304),
)


def _make_trip(ev_distance=12.701, locations=_LOCATIONS) -> _FakeTrip:
    return _FakeTrip(
        distance=33.01,
        start_time=_START,
        end_time=_END,
        duration=_DURATION,
        fuel_consumed=1.229,
        average_fuel_consumed=3.723,
        ev_distance=ev_distance,
        ev_duration=timedelta(minutes=20, seconds=26),
        locations=locations,
    )


# ---------------------------------------------------------------------------
# value_fn
# ---------------------------------------------------------------------------

def test_value_fn_returns_none_when_no_last_trip():
    assert LAST_TRIP_ENTITY_DESCRIPTION.value_fn(_FakeVehicle()) is None


def test_value_fn_returns_rounded_distance():
    vehicle = _FakeVehicle(last_trip=_make_trip())
    assert LAST_TRIP_ENTITY_DESCRIPTION.value_fn(vehicle) == 33.0


# ---------------------------------------------------------------------------
# attributes_fn — base fields
# ---------------------------------------------------------------------------

def test_attributes_returns_none_when_no_last_trip():
    assert _format_last_trip_attributes(_FakeVehicle()) is None


def test_attributes_base_fields():
    attrs = _format_last_trip_attributes(_FakeVehicle(last_trip=_make_trip()))
    assert attrs["start_time"] == _START.isoformat()
    assert attrs["end_time"] == _END.isoformat()
    assert attrs["duration"] == "0:38"
    assert attrs["fuel_consumed"] == 1.229
    assert attrs["average_fuel_consumed"] == 3.723


# ---------------------------------------------------------------------------
# attributes_fn — coordinates
# ---------------------------------------------------------------------------

def test_attributes_contain_coordinates():
    attrs = _format_last_trip_attributes(_FakeVehicle(last_trip=_make_trip()))
    assert attrs["start_latitude"] == 44.8841
    assert attrs["start_longitude"] == 10.3555
    assert attrs["end_latitude"] == 44.6973
    assert attrs["end_longitude"] == 10.5304


def test_attributes_omit_coordinates_when_locations_is_none():
    vehicle = _FakeVehicle(last_trip=_make_trip(locations=None))
    attrs = _format_last_trip_attributes(vehicle)
    assert "start_latitude" not in attrs
    assert "end_latitude" not in attrs


def test_attributes_omit_coordinates_when_start_end_are_none():
    vehicle = _FakeVehicle(
        last_trip=_make_trip(locations=_FakeLocations(start=None, end=None))
    )
    attrs = _format_last_trip_attributes(vehicle)
    assert "start_latitude" not in attrs
    assert "end_latitude" not in attrs


# ---------------------------------------------------------------------------
# attributes_fn — EV fields
# ---------------------------------------------------------------------------

def test_attributes_contain_ev_fields_when_present():
    attrs = _format_last_trip_attributes(_FakeVehicle(last_trip=_make_trip(ev_distance=12.701)))
    assert attrs["ev_distance"] == 12.7
    assert attrs["ev_distance_pct"] == round(12.701 / 33.01 * 100, 1)


def test_attributes_omit_ev_fields_when_ev_distance_is_none():
    attrs = _format_last_trip_attributes(_FakeVehicle(last_trip=_make_trip(ev_distance=None)))
    assert "ev_distance" not in attrs
    assert "ev_distance_pct" not in attrs


def test_no_division_by_zero_when_distance_is_zero():
    trip = _FakeTrip(
        distance=0.0,
        start_time=_START,
        end_time=_END,
        duration=_DURATION,
        fuel_consumed=0.0,
        average_fuel_consumed=0.0,
        ev_distance=1.0,
        locations=None,
    )
    attrs = _format_last_trip_attributes(_FakeVehicle(last_trip=trip))
    assert "ev_distance_pct" not in attrs
