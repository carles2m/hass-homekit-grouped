"""ThinQ washer/dryer profile.

Read-only grouped accessory with services:
  - (Optional, if category=fan) Fanv2 — primary service so Apple Home uses
    the fan tile icon. Active reflects whether a cycle is running.
  - Valve (Irrigation/Faucet/etc. per YAML) — carries the countdown.
    Active+InUse reflect running; SetDuration = total cycle time from
    the ThinQ total_time sensor; RemainingDuration = seconds until the
    cycle finishes, derived from the ThinQ remaining_time timestamp.
  - MotionSensor "Finished" — fires a 60s motion pulse when the ThinQ
    integration emits a configured event_type on the device's
    event.*_notification entity (e.g. washing_is_complete). Apple Home
    notifies once per detected motion, giving a single iOS push per
    cycle.

Nothing in HomeKit is actually user-controllable: Apple Home may render
the Valve as a toggle, but any write is reverted to the real state.
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

_SERV_VALVE = "Valve"
_SERV_MOTION = "MotionSensor"
_SERV_FAN = "Fanv2"

_CHAR_ACTIVE = "Active"
_CHAR_IN_USE = "InUse"
_CHAR_VALVE_TYPE = "ValveType"
_CHAR_REMAINING_DURATION = "RemainingDuration"
_CHAR_SET_DURATION = "SetDuration"
_CHAR_MOTION_DETECTED = "MotionDetected"
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

_DURATION_MAX = 3600  # HAP cap on SetDuration / RemainingDuration
_MOTION_PULSE_SECONDS = 60

_PERM_READ = "pr"
_PERM_NOTIFY = "ev"


class ThinqWasherAccessory(GroupedAccessory):
    """HAP accessory for a single LG ThinQ washer or dryer."""

    def _setup_services(self) -> None:
        self._status_entity: str | None = None
        self._remaining_entity: str | None = None
        self._total_entity: str | None = None
        self._notification_entity: str | None = None
        self._motion_reset_cancel = None
        # Last-seen timestamp of the notification entity. Used to detect
        # whether we're looking at a replay of a historical event (HA
        # restart) or a genuinely new fire. None = haven't seen any yet.
        self._last_event_ts: str | None = None
        self._resolve_entities()

        cat_name = self.overrides.get("category") or _DEFAULT_CATEGORY_NAME
        self.category = _CATEGORY_MAP[cat_name]
        valve_type_name = (
            self.overrides.get("valve_type") or _DEFAULT_VALVE_TYPE_NAME
        )
        valve_type_value = _VALVE_TYPE_MAP[valve_type_name]

        self._finished_event_types = frozenset(
            self.overrides.get("finished_event_types") or []
        )

        # --- Fan (optional, primary) ----------------------------------------
        # When category=fan, add Fan BEFORE Valve so Apple Home uses the fan
        # icon for the tile. Valve stays secondary with the countdown.
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

        # --- Valve (countdown bearer) ---------------------------------------
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
        self._char_active = serv_valve.configure_char(
            _CHAR_ACTIVE,
            value=0,
            properties={"Permissions": [_PERM_READ, _PERM_NOTIFY]},
        )
        # Apple Home may still render Active as a toggle despite read-only
        # permissions. Revert-on-write swallows any user tap.
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

        # --- MotionSensor "Finished" ----------------------------------------
        # Triggered by event_type matches on event.*_notification, not by
        # state transitions. Only created if the user configured event types
        # AND we found a notification entity on the device.
        self._char_motion = None
        if self._finished_event_types and self._notification_entity:
            motion_name = f"{self.display_name} Finished"
            serv_motion = self.add_preload_service(
                _SERV_MOTION,
                [_CHAR_MOTION_DETECTED, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
            )
            self._char_motion = serv_motion.configure_char(
                _CHAR_MOTION_DETECTED, value=False
            )
            serv_motion.configure_char(_CHAR_NAME, value=motion_name)
            serv_motion.configure_char(_CHAR_CONFIGURED_NAME, value=motion_name)
        elif self._finished_event_types and not self._notification_entity:
            _LOGGER.warning(
                "%s: finished_event_types configured but no "
                "event.*_notification entity found on device %s — Finished "
                "sensor will not be created",
                self.display_name,
                self.device_id,
            )

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
            elif eid.startswith("event.") and eid.endswith("_notification"):
                self._notification_entity = eid

        if not self._status_entity:
            _LOGGER.warning(
                "Device %s (%s) missing *_current_status sensor",
                self.device_id,
                self.display_name,
            )

    def _watched_entities(self) -> Iterable[str]:
        for eid in (
            self._status_entity,
            self._remaining_entity,
            self._total_entity,
            self._notification_entity,
        ):
            if eid:
                yield eid

    def _push_state(self, entity_id: str, state: State | None) -> None:
        if state is None:
            return

        if entity_id == self._status_entity:
            if state.state in ("unknown", "unavailable"):
                return
            running = state.state in _RUNNING_STATES
            self._char_active.set_value(1 if running else 0)
            self._char_in_use.set_value(1 if running else 0)
            if self._char_fan_active is not None:
                self._char_fan_active.set_value(1 if running else 0)
            if not running:
                self._char_remaining.set_value(0)

        elif entity_id == self._remaining_entity:
            if state.state in ("unknown", "unavailable"):
                self._char_remaining.set_value(0)
                return
            self._char_remaining.set_value(self._remaining_seconds(state.state))

        elif entity_id == self._total_entity:
            if state.state in ("unknown", "unavailable"):
                self._char_set_duration.set_value(0)
                return
            try:
                seconds = int(float(state.state)) * 60
            except (ValueError, TypeError):
                seconds = 0
            self._char_set_duration.set_value(min(max(seconds, 0), _DURATION_MAX))

        elif entity_id == self._notification_entity:
            ts = state.state
            if ts in ("unknown", "unavailable", None):
                return
            # Initial priming (HA restart, bridge startup): remember the
            # current timestamp but do NOT fire — the event already happened,
            # possibly days ago. Only a subsequent timestamp change means a
            # genuinely new event.
            if self._last_event_ts is None:
                self._last_event_ts = ts
                return
            if ts == self._last_event_ts:
                return
            self._last_event_ts = ts
            self._maybe_fire_finished(state)

    def _maybe_fire_finished(self, state: State) -> None:
        """Fire the Finished motion pulse if the event matches a configured
        finished_event_type. HA event entities update state (a timestamp)
        and attribute event_type each time the source emits an event."""
        if self._char_motion is None:
            return
        event_type = state.attributes.get("event_type")
        if event_type is None:
            return
        if event_type not in self._finished_event_types:
            return

        # Cancel any in-flight reset from a prior pulse.
        if self._motion_reset_cancel is not None:
            try:
                self._motion_reset_cancel()
            except Exception:  # noqa: BLE001
                pass
            self._motion_reset_cancel = None

        _LOGGER.info(
            "%s: event '%s' matched — firing Finished motion pulse",
            self.display_name,
            event_type,
        )
        self._char_motion.set_value(True)

        def _reset(_now):
            if self._char_motion is not None:
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
