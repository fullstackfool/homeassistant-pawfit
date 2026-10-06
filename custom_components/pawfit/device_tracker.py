import logging
import time
from datetime import datetime, timedelta

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.entity import Entity
from homeassistant.components.device_tracker import TrackerEntity, SourceType
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .pawfit_api import PawfitApiClient
from .const import (
    DOMAIN,
    UPDATE_INTERVAL_SECONDS,
    MIN_FETCH_SPACING_SECONDS,
    ACTIVITY_INTERVAL_SECONDS,
    KEEP_LAST_DATA_SECONDS,
)

# Define SOURCE_TYPE_GPS constant for device tracker
SOURCE_TYPE_GPS = "gps"

# Fallback: define DeviceTrackerEntity as base Entity if import fails
try:
    from homeassistant.components.device_tracker import TrackerEntity, SourceType
except ImportError:
    class SourceType:
        GPS = "gps"
    class TrackerEntity(Entity):
        pass

class PawfitDataUpdateCoordinator(DataUpdateCoordinator):
    """Fetch every tracker's data on a fixed schedule, with a hard rate limit.

    Fork change. Upstream polled every 60 s but switched to 1 s polling for
    10 minutes after any Find/Light/Alarm press, fetched activity on every
    refresh, and let any refresh request (homeassistant.update_entity, a
    button) trigger an extra fetch. Here:
    - location is fetched every UPDATE_INTERVAL_SECONDS, in one request for
      all trackers (upstream made the same request twice per refresh);
    - extra refresh requests are ignored until a minute has passed, and two
      fetches are never closer than MIN_FETCH_SPACING_SECONDS whatever asks;
    - daily activity is fetched at most every ACTIVITY_INTERVAL_SECONDS.
    """

    def __init__(self, hass, client, trackers):
        self.logger = logging.getLogger(__name__)
        super().__init__(
            hass,
            logger=self.logger,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self.client = client
        self.trackers = trackers
        self.tracker_ids = [t["tracker_id"] for t in trackers]
        self._last_fetch = None  # time.monotonic() of the last location fetch
        self._activity = {}  # str(tracker_id) -> activity stats
        self._activity_fetched = None  # time.monotonic() of the last activity fetch
        self._activity_day = None
        self.fetches_today = 0
        self._fetch_day = None
        self._last_success = None  # time.monotonic() of the last good location fetch
        self._failing_since = None  # time.monotonic() when the current failure streak began
        self.logger.info(f"PawfitDataUpdateCoordinator initialized with trackers: {self.tracker_ids}")

    def _seconds_since_fetch(self):
        if self._last_fetch is None:
            return None
        return time.monotonic() - self._last_fetch

    async def async_request_refresh(self) -> None:
        """Ignore refresh requests within a minute of the last fetch.

        Covers homeassistant.update_entity and anything else that asks for a
        refresh outside the schedule. Dropping them here, not only in
        _async_update_data, also stops a skipped request from pushing the
        next scheduled fetch back.
        """
        since = self._seconds_since_fetch()
        if since is not None and since < UPDATE_INTERVAL_SECONDS:
            self.logger.debug("Refresh request ignored: last Pawfit fetch %.0f s ago", since)
            return
        await super().async_request_refresh()

    @callback
    def async_set_mode_timer(self, tracker_id, key, value) -> None:
        """Record a Find/Light/Alarm change locally, without an extra fetch.

        The next scheduled fetch replaces it with what Pawfit reports.
        """
        if not self.data:
            return
        tracker_data = self.data.get(str(tracker_id))
        if tracker_data is None:
            return
        tracker_data[key] = value
        self.async_update_listeners()

    def _keep_last_data(self, err, now):
        """Ride out a failed fetch with the last known data (fork change).

        Pawfit's API sometimes stops answering for a few minutes. Upstream
        marked every entity unavailable on the first failure, so the cats
        vanished from maps. Here the last good data is kept, and the
        entities stay available, until KEEP_LAST_DATA_SECONDS after the last
        good fetch. The Last Time Seen sensor still shows how old it is.
        """
        if self._failing_since is None:
            self._failing_since = now
            self.logger.warning(
                "Pawfit fetch failed (%r); keeping the last known data for up to %d min",
                err,
                KEEP_LAST_DATA_SECONDS // 60,
            )
        if (
            self.data is not None
            and self._last_success is not None
            and now - self._last_success < KEEP_LAST_DATA_SECONDS
        ):
            return self.data
        raise UpdateFailed(f"No data from Pawfit: {err!r}") from err

    def _count_fetch(self) -> None:
        today = dt_util.now().date()
        if today != self._fetch_day:
            self._fetch_day = today
            self.fetches_today = 0
        self.fetches_today += 1

    async def _async_update_data(self):
        since = self._seconds_since_fetch()
        if since is not None and since < MIN_FETCH_SPACING_SECONDS:
            # Second guard: nothing reaches the Pawfit API inside the limit.
            self.logger.debug("Fetch skipped: last Pawfit fetch %.0f s ago", since)
            if self.data is not None:
                return self.data
            raise UpdateFailed("Waiting for the Pawfit rate limit")
        now = time.monotonic()
        self._last_fetch = now
        self._count_fetch()

        self.logger.debug(f"_async_update_data called for trackers: {self.tracker_ids}")
        try:
            location_data = await self.client.async_get_locations(self.tracker_ids)
        except Exception as err:  # timeouts, connection errors, bad responses
            return self._keep_last_data(err, now)
        location_data = {str(k): v for k, v in (location_data or {}).items()}
        if not location_data:
            return self._keep_last_data(UpdateFailed("no trackers in Pawfit's response"), now)
        if self._failing_since is not None:
            self.logger.warning(
                "Pawfit fetch recovered after %.0f s", now - self._failing_since
            )
        self._failing_since = None
        self._last_success = now

        # The Find/Light/Alarm timers are in the same response; upstream
        # fetched it a second time to read them.
        for tracker_data in location_data.values():
            raw = tracker_data.get("_raw") or {}
            tracker_data["find_timer"] = raw.get("timerGps", 0)
            tracker_data["light_timer"] = raw.get("timerLight", 0)
            tracker_data["alarm_timer"] = raw.get("timerSpeaker", 0)

        await self._async_update_activity(now)
        for tracker_id_str, tracker_data in location_data.items():
            stats = self._activity.get(tracker_id_str)
            if stats is not None:
                tracker_data.update({
                    "steps_today": stats.get("total_steps", 0),
                    "calories_today": stats.get("total_calories", 0.0),
                    "active_time_today": stats.get("total_active_hours", 0.0),
                })

        return location_data

    async def _async_update_activity(self, now) -> None:
        """Fetch daily activity if it is due (every 15 min, or a new day)."""
        # Same clock as the activity request's midnight-to-midnight window.
        today = datetime.now().date()
        due = (
            self._activity_fetched is None
            or today != self._activity_day
            # 30 s slack so a fetch landing a moment early still counts.
            or now - self._activity_fetched >= ACTIVITY_INTERVAL_SECONDS - 30
        )
        if not due:
            return
        self._activity_fetched = now
        self._activity_day = today
        for tracker_id in self.tracker_ids:
            # Returns zeros rather than raising if Pawfit fails.
            stats = await self.client.async_get_activity_stats(str(tracker_id))
            self.logger.debug(f"Received activity stats for tracker {tracker_id}: {stats}")
            self._activity[str(tracker_id)] = stats

class PawfitDeviceTracker(TrackerEntity):
    def __init__(self, tracker, coordinator):
        self._tracker = tracker
        self._coordinator = coordinator
        self._tracker_id = tracker["tracker_id"]
        self._attr_name = f"{tracker['name']}'s PawFit Tracker"
        self._attr_unique_id = str(tracker["petId"])
        self._attr_icon = "mdi:paw"
        self._attr_source_type = SourceType.GPS
        self._attr_device_info = {
            "identifiers": {(DOMAIN, str(self._tracker_id))},
            "name": f"{tracker['name']}'s PawFit Tracker",
            "model": tracker.get("model", "Unknown"),
            "manufacturer": "PawFit",
            "translation_key": "pawfit_tracker",
        }
        self._attr_latitude = None
        self._attr_longitude = None
        self._attr_location_accuracy = None
        self._attr_charging = None

    @property
    def charging(self):
        return self._attr_charging

    @property
    def source_type(self):
        return self._attr_source_type

    @property
    def available(self):
        """Return if entity is available."""
        return self._coordinator.last_update_success and self._attr_latitude is not None and self._attr_longitude is not None

    def _update_attrs(self):
        data = self._coordinator.data.get(str(self._tracker_id), {}) if self._coordinator.data else {}
        self._attr_latitude = float(data.get("latitude")) if data.get("latitude") else None
        self._attr_longitude = float(data.get("longitude")) if data.get("longitude") else None
        self._attr_location_accuracy = float(data.get("accuracy")) if data.get("accuracy") else None
        
        # Handle charging status. Battery percentage is exposed by the dedicated
        # battery sensor because device tracker battery_level is deprecated.
        battery_raw = data.get("battery")
        if battery_raw is not None:
            battery_value = int(battery_raw)
            model = data.get("_raw", {}).get("model")
            if model == "TR2A":
                # PawFit 2: value > 4 means charging
                self._attr_charging = battery_value > 4
            else:
                # PawFit 3: negative value indicates charging
                self._attr_charging = battery_value < 0
        else:
            self._attr_charging = None
        
        # Only log if there's an issue
        if not (self._attr_latitude and self._attr_longitude) and data:
            logging.warning(f"Tracker {self._tracker_id}: No location data available")

    async def async_update(self):
        await self._coordinator.async_request_refresh()
        self._update_attrs()

    async def async_added_to_hass(self):
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self._update_attrs()
        # Register for coordinator updates
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attrs()
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Pawfit device tracker entities from a config entry."""
    # Get the coordinator from hass.data (created in __init__.py)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    entities = []
    for tracker in coordinator.trackers:
        entities.append(PawfitDeviceTracker(tracker, coordinator))
    
    async_add_entities(entities)
