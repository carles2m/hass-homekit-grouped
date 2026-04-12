"""ThinQ washer/dryer profile.

Read-only grouped accessory with two services:
  - Valve (Irrigation style) — Active+InUse reflect whether a cycle is
    running; SetDuration = total cycle time; RemainingDuration = time left.
    Active is marked read-only so Apple Home shows a status indicator, not
    an on/off toggle.
  - MotionSensor — fires briefly when the cycle transitions from running
    to not-running. Apple Home's "Activity Notifications" on this sensor
    produce a one-shot iOS notification when the cycle completes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from pyhap.const import (
    CATEGORY_FAN,
    CATEGORY_FAUCET,
    CATEGORY_OTHER,
    CATEGORY_SHOWER_HEAD,
    CATEGORY_SPRINKLER,
)

from .base import GroupedAccessory

_LOGGER = logging.getLogger(__name__)

# ThinQ current_status values we treat as "cycle running" for the Valve
_RUNNING_STATES = frozenset(
    {
        "running",
        "prewash",
        "rinsing",
        "spin",
        "spinning",
        "drying",
        "cooling",
        "wrinkle_care",
        "detergent_amount",
        "detecting",
    }
)

# "Finishing states" are per-device configurable via YAML. There is no
# default because the meaningful late-cycle phases differ wildly by
# appliance: dryers have cooling / wrinkle_care, washers might use
# spinning / drying, combo units have their own progression. The user
# picks which states should surface as "Finishing" for each device.

_SERV_VALVE = "Valve"
_SERV_MOTION = "MotionSensor"
_SERV_OCCUPANCY = "OccupancySensor"
_SERV_FAN = "Fanv2"
_CHAR_ACTIVE = "Active"
_CHAR_IN_USE = "InUse"
_CHAR_VALVE_TYPE = "ValveType"
_CHAR_REMAINING_DURATION = "RemainingDuration"
_CHAR_SET_DURATION = "SetDuration"
_CHAR_MOTION_DETECTED = "MotionDetected"
_CHAR_OCCUPANCY_DETECTED = "OccupancyDetected"
_CHAR_NAME = "Name"
_CHAR_CONFIGURED_NAME = "ConfiguredName"

_CATEGORY_MAP = {
    "sprinkler": CATEGORY_SPRINKLER,
    "faucet": CATEGORY_FAUCET,
    "fan": CATEGORY_FAN,
    "other": CATEGORY_OTHER,
    "shower_head": CATEGORY_SHOWER_HEAD,
}
_VALVE_TYPE_MAP = {
    "generic": 0,
    "irrigation": 1,
    "shower": 2,
    "faucet": 3,
}
_DEFAULT_CATEGORY_NAME = "sprinkler"
_DEFAULT_VALVE_TYPE_NAME = "irrigation"
_DURATION_MAX = 3600  # HAP cap

# How long to hold the MotionSensor "detected" after cycle end. Gives Apple
# Home enough time to fire the notification even if the user closes the app.
_MOTION_PULSE_SECONDS = 60

# HAP characteristic permissions
_PERM_READ = "pr"
_PERM_WRITE = "pw"
_PERM_NOTIFY = "ev"


class ThinqWasherAccessory(GroupedAccessory):
    """HAP accessory for a single LG ThinQ washer or dryer."""

    def _setup_services(self) -> None:
        self._status_entity: str | None = None
        self._remaining_entity: str | None = None
        self._total_entity: str | None = None
        self._prev_running: bool | None = None
        self._motion_reset_cancel = None
        self._resolve_entities()

        # Apply category + valve_type overrides from YAML, falling back to
        # defaults. category is a class-level attr pyhap reads during setup;
        # set it on the instance before pyhap serializes the accessory.
        cat_name = self.overrides.get("category") or _DEFAULT_CATEGORY_NAME
        self.category = _CATEGORY_MAP[cat_name]
        valve_type_name = (
            self.overrides.get("valve_type") or _DEFAULT_VALVE_TYPE_NAME
        )
        valve_type_value = _VALVE_TYPE_MAP[valve_type_name]

        # When category=fan (dryer), add a Fan service BEFORE the Valve so
        # Apple Home uses the fan icon for the tile. Valve stays as a
        # secondary service to keep the countdown visible in the detail view.
        self._char_fan_active = None
        if cat_name == "fan":
            serv_fan = self.add_preload_service(
                _SERV_FAN,
                [_CHAR_ACTIVE, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
            )
            self._char_fan_active = serv_fan.configure_char(
                _CHAR_ACTIVE,
                value=0,
                properties={"Permissions": [_PERM_READ, _PERM_NOTIFY]},
            )
            self._char_fan_active.setter_callback = self._revert_fan_active
            serv_fan.configure_char(_CHAR_NAME, value=self.display_name)
            serv_fan.configure_char(
                _CHAR_CONFIGURED_NAME, value=self.display_name
            )

        # --- Valve (primary) -------------------------------------------------
        serv_valve = self.add_preload_service(
            _SERV_VALVE,
            [
                _CHAR_ACTIVE,
                _CHAR_IN_USE,
                _CHAR_VALVE_TYPE,
                _CHAR_REMAINING_DURATION,
                _CHAR_SET_DURATION,
                _CHAR_NAME,
                _CHAR_CONFIGURED_NAME,
            ],
        )
        # Active read-only: we reflect appliance state; we do not let
        # Apple Home start/stop the cycle. HAP permits marking Active
        # as pr+ev (no pw) — Apple Home renders a status indicator.
        self._char_active = serv_valve.configure_char(
            _CHAR_ACTIVE,
            value=0,
            properties={"Permissions": [_PERM_READ, _PERM_NOTIFY]},
        )
        # Apple Home's tile UI hardcodes Valve/Active as a toggle regardless
        # of our read-only HAP permissions. Best we can do is revert any
        # client write to the real HA state via setter_callback — brief UI
        # flicker on tap, but the value always snaps back within ~1s.
        self._char_active.setter_callback = self._revert_active
        self._char_in_use = serv_valve.configure_char(_CHAR_IN_USE, value=0)
        serv_valve.configure_char(_CHAR_VALVE_TYPE, value=valve_type_value)
        self._char_remaining = serv_valve.configure_char(
            _CHAR_REMAINING_DURATION, value=0
        )
        self._char_set_duration = serv_valve.configure_char(
            _CHAR_SET_DURATION, value=0
        )
        serv_valve.configure_char(_CHAR_NAME, value=self.display_name)
        serv_valve.configure_char(_CHAR_CONFIGURED_NAME, value=self.display_name)

        # --- OccupancySensor "Finishing" (per-device configurable) ---------
        finishing = self.overrides.get("finishing_states") or []
        self._finishing_states = frozenset(finishing)
        self._char_finishing = None
        if self._finishing_states:
            finishing_name = f"{self.display_name} Finishing"
            serv_finishing = self.add_preload_service(
                _SERV_OCCUPANCY,
                [_CHAR_OCCUPANCY_DETECTED, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
            )
            self._char_finishing = serv_finishing.configure_char(
                _CHAR_OCCUPANCY_DETECTED, value=0
            )
            serv_finishing.configure_char(_CHAR_NAME, value=finishing_name)
            serv_finishing.configure_char(
                _CHAR_CONFIGURED_NAME, value=finishing_name
            )

        # --- MotionSensor (cycle-finished pulse) ----------------------------
        motion_name = f"{self.display_name} Finished"
        serv_motion = self.add_preload_service(
            _SERV_MOTION, [_CHAR_MOTION_DETECTED, _CHAR_NAME, _CHAR_CONFIGURED_NAME]
        )
        self._char_motion = serv_motion.configure_char(
            _CHAR_MOTION_DETECTED, value=False
        )
        serv_motion.configure_char(_CHAR_NAME, value=motion_name)
        serv_motion.configure_char(_CHAR_CONFIGURED_NAME, value=motion_name)

    def _resolve_entities(self) -> None:
        registry = er.async_get(self.hass)
        for entry in er.async_entries_for_device(registry, self.device_id):
            eid = entry.entity_id
            if eid.startswith("sensor.") and eid.endswith("_current_status"):
                self._status_entity = eid
            elif eid.startswith("sensor.") and eid.endswith("_remaining_time"):
                self._remaining_entity = eid
            elif eid.startswith("sensor.") and eid.endswith("_total_time"):
                self._total_entity = eid

        if not self._status_entity:
            _LOGGER.warning(
                "Device %s (%s) missing *_current_status sensor",
                self.device_id,
                self.display_name,
            )

    def _watched_entities(self) -> Iterable[str]:
        for eid in (self._status_entity, self._remaining_entity, self._total_entity):
            if eid:
                yield eid

    def _push_state(self, entity_id: str, state: State | None) -> None:
        if state is None or state.state in ("unknown", "unavailable"):
            if entity_id == self._remaining_entity:
                self._char_remaining.set_value(0)
            elif entity_id == self._total_entity:
                self._char_set_duration.set_value(0)
            return

        if entity_id == self._status_entity:
            running = state.state in _RUNNING_STATES
            self._char_active.set_value(1 if running else 0)
            self._char_in_use.set_value(1 if running else 0)
            if self._char_fan_active is not None:
                self._char_fan_active.set_value(1 if running else 0)
            if self._char_finishing is not None:
                self._char_finishing.set_value(
                    1 if state.state in self._finishing_states else 0
                )
            if not running:
                self._char_remaining.set_value(0)

            # Cycle-finished motion pulse: detect running -> not-running
            # transition. Skip the first push at startup (prev is None) so
            # we don't fire spuriously when the accessory is first primed.
            if self._prev_running is True and not running:
                self._fire_motion_pulse()
            self._prev_running = running

        elif entity_id == self._remaining_entity:
            self._char_remaining.set_value(self._remaining_seconds(state.state))

        elif entity_id == self._total_entity:
            try:
                seconds = int(float(state.state)) * 60
            except (ValueError, TypeError):
                seconds = 0
            self._char_set_duration.set_value(min(max(seconds, 0), _DURATION_MAX))

    def _fire_motion_pulse(self) -> None:
        """Pulse MotionDetected=True for a few seconds so iOS fires a
        one-shot notification for cycle completion."""
        # Cancel any in-flight reset from a prior pulse.
        if self._motion_reset_cancel is not None:
            try:
                self._motion_reset_cancel()
            except Exception:  # noqa: BLE001
                pass
            self._motion_reset_cancel = None

        _LOGGER.info("%s cycle finished — firing motion pulse", self.display_name)
        self._char_motion.set_value(True)

        def _reset(_now):
            self._char_motion.set_value(False)
            self._motion_reset_cancel = None

        self._motion_reset_cancel = async_call_later(
            self.hass, _MOTION_PULSE_SECONDS, _reset
        )

    def _revert_active(self, _requested_value: int) -> None:
        """Ignore client writes to Valve Active; snap back to real HA state."""
        if not self._status_entity:
            return
        real = self.hass.states.get(self._status_entity)
        if real is None:
            return
        running = 1 if real.state in _RUNNING_STATES else 0
        self._char_active.set_value(running)
        self._char_in_use.set_value(running)

    def _revert_fan_active(self, _requested_value: int) -> None:
        """Ignore client writes to Fan Active; snap back to real HA state."""
        if not self._status_entity or self._char_fan_active is None:
            return
        real = self.hass.states.get(self._status_entity)
        if real is None:
            return
        self._char_fan_active.set_value(
            1 if real.state in _RUNNING_STATES else 0
        )

    @staticmethod
    def _remaining_seconds(iso_ts: str) -> int:
        try:
            end = datetime.fromisoformat(iso_ts)
        except ValueError:
            return 0
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        remaining = int((end - now).total_seconds())
        if remaining <= 0:
            return 0
        return min(remaining, _DURATION_MAX)
