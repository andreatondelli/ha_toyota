"""Fetch Toyota trip history and import as HA long-term statistics.

One coordinator per config entry. On the first run it backfills all available
months; subsequent runs are incremental (last 60 days). Raw trip data is
persisted in HA storage so cumulative sums stay correct even across restarts.
"""

from __future__ import annotations

import asyncio
import logging
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator as MainCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
_INTER_CALL_DELAY = 1.5      # seconds between consecutive get_trips() calls
_INCREMENTAL_LOOKBACK_DAYS = 60
_MAX_EMPTY_MONTHS = 3         # stop backfill after this many consecutive empty months
_MAX_YEARS_BACK = 10
UPDATE_INTERVAL = timedelta(hours=6)


# ---------------------------------------------------------------------------
# Trip serialisation
# ---------------------------------------------------------------------------

def _trip_to_dict(trip: Any) -> dict[str, Any]:
    """Serialise a pytoyoda Trip to a JSON-serialisable dict."""
    locs = getattr(trip, "locations", None)
    start_loc = locs.start if locs else None
    end_loc = locs.end if locs else None
    ev_dur = getattr(trip, "ev_duration", None)
    return {
        "start_time": trip.start_time.isoformat() if trip.start_time else None,
        "end_time": trip.end_time.isoformat() if trip.end_time else None,
        "duration_seconds": int(trip.duration.total_seconds()) if trip.duration else None,
        "distance_km": trip.distance,
        "fuel_consumed_l": trip.fuel_consumed,
        "avg_fuel_l100km": trip.average_fuel_consumed,
        "ev_distance_km": trip.ev_distance,
        "ev_duration_seconds": int(ev_dur.total_seconds()) if ev_dur else None,
        "start_lat": start_loc.lat if start_loc else None,
        "start_lon": start_loc.lon if start_loc else None,
        "end_lat": end_loc.lat if end_loc else None,
        "end_lon": end_loc.lon if end_loc else None,
    }


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_end(d: date) -> date:
    return date(d.year, d.month, monthrange(d.year, d.month)[1])


def _prev_month(d: date) -> date:
    first = d.replace(day=1)
    prev = first - timedelta(days=1)
    return prev.replace(day=1)


# ---------------------------------------------------------------------------
# Statistics import
# ---------------------------------------------------------------------------

def _import_statistics(hass: HomeAssistant, vin: str, trips: list[dict[str, Any]]) -> None:
    """Compute cumulative sums from sorted trips and push to HA recorder."""
    valid = sorted(
        (t for t in trips if t.get("start_time")),
        key=lambda t: t["start_time"],
    )
    if not valid:
        return

    cum_dist = cum_fuel = cum_ev = cum_dur = 0.0
    dist_rows: list[StatisticData] = []
    fuel_rows: list[StatisticData] = []
    ev_rows: list[StatisticData] = []
    dur_rows: list[StatisticData] = []

    for t in valid:
        start = datetime.fromisoformat(t["start_time"])

        dist = t.get("distance_km") or 0.0
        cum_dist += dist
        dist_rows.append(StatisticData(start=start, state=dist, sum=cum_dist))

        fuel = t.get("fuel_consumed_l") or 0.0
        cum_fuel += fuel
        fuel_rows.append(StatisticData(start=start, state=fuel, sum=cum_fuel))

        ev = t.get("ev_distance_km")
        if ev is not None:
            cum_ev += ev
            ev_rows.append(StatisticData(start=start, state=ev, sum=cum_ev))

        dur_s = t.get("duration_seconds")
        if dur_s is not None:
            dur_min = dur_s / 60.0
            cum_dur += dur_min
            dur_rows.append(StatisticData(start=start, state=dur_min, sum=cum_dur))

    prefix = f"{DOMAIN}:{vin.lower()}"

    def _push(stat_id: str, name: str, unit: str, rows: list[StatisticData]) -> None:
        if not rows:
            return
        async_add_external_statistics(
            hass,
            StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=name,
                source=DOMAIN,
                statistic_id=f"{prefix}_{stat_id}",
                unit_of_measurement=unit,
            ),
            rows,
        )

    _push("trip_distance", "Trip distance", "km", dist_rows)
    _push("trip_fuel_consumed", "Trip fuel consumed", "L", fuel_rows)
    _push("trip_ev_distance", "Trip EV distance", "km", ev_rows)
    _push("trip_duration_min", "Trip duration", "min", dur_rows)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class TripStatisticsCoordinator(DataUpdateCoordinator[None]):
    """Fetches Toyota trip history and imports as HA long-term statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        main_coordinator: MainCoordinator,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_trip_statistics",
            update_interval=UPDATE_INTERVAL,
        )
        self._main = main_coordinator

    async def _async_update_data(self) -> None:
        if not self._main.data:
            _LOGGER.debug("Trip statistics: main coordinator has no data yet, skipping")
            return

        for vehicle_data in self._main.data:
            vehicle = vehicle_data.get("data")
            if not vehicle or not vehicle.vin:
                continue
            try:
                await self._sync_vehicle(vehicle)
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Trip statistics sync failed for VIN ...%s", vehicle.vin[-6:], exc_info=True
                )

    async def _sync_vehicle(self, vehicle: Any) -> None:
        vin = vehicle.vin
        store: Store = Store(self.hass, STORAGE_VERSION, f"toyota_trips_{vin}")
        data: dict[str, Any] = await store.async_load() or {
            "trips": [],
            "backfill_done": False,
        }

        was_first_run = not data.get("backfill_done", False)
        initial_count = len(data["trips"])
        existing_starts: set[str] = {
            t["start_time"] for t in data["trips"] if t.get("start_time")
        }

        if was_first_run:
            new_trips = await self._backfill(vehicle)
            data["backfill_done"] = True
        else:
            today = date.today()
            from_d = today - timedelta(days=_INCREMENTAL_LOOKBACK_DAYS)
            raw = await vehicle.get_trips(from_d, today, full_route=False)
            new_trips = [_trip_to_dict(t) for t in (raw or [])]

        for trip in new_trips:
            key = trip.get("start_time")
            if key and key not in existing_starts:
                data["trips"].append(trip)
                existing_starts.add(key)

        added = len(data["trips"]) - initial_count

        if added > 0 or was_first_run:
            await store.async_save(data)
            if data["trips"]:
                _LOGGER.debug(
                    "Trip statistics: importing %d total trips for VIN ...%s (%d new)",
                    len(data["trips"]), vin[-6:], added,
                )
                _import_statistics(self.hass, vin, data["trips"])
        else:
            _LOGGER.debug("Trip statistics: no new trips for VIN ...%s", vin[-6:])

    async def _backfill(self, vehicle: Any) -> list[dict[str, Any]]:
        """Scan backwards month by month until we hit _MAX_EMPTY_MONTHS empty months."""
        today = date.today()
        month = _month_start(today)
        all_trips: list[dict[str, Any]] = []
        empty_streak = 0

        while empty_streak < _MAX_EMPTY_MONTHS:
            if month.year < today.year - _MAX_YEARS_BACK:
                _LOGGER.debug("Trip backfill: reached %d-year limit, stopping", _MAX_YEARS_BACK)
                break

            _LOGGER.debug(
                "Trip backfill: fetching %d-%02d for VIN ...%s",
                month.year, month.month, vehicle.vin[-6:],
            )
            try:
                raw = await vehicle.get_trips(month, _month_end(month), full_route=False)
                trips = [_trip_to_dict(t) for t in (raw or [])]
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Trip backfill: API error for %d-%02d, skipping month", month.year, month.month
                )
                trips = []

            if trips:
                all_trips.extend(trips)
                empty_streak = 0
            else:
                empty_streak += 1

            month = _prev_month(month)
            await asyncio.sleep(_INTER_CALL_DELAY)

        _LOGGER.info(
            "Trip backfill complete: %d trips for VIN ...%s", len(all_trips), vehicle.vin[-6:]
        )
        return all_trips
