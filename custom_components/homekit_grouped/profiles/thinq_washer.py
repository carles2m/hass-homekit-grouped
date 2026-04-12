"""ThinQ washer/dryer profile.

Single-service accessory exposing a Valve (Irrigation-style) whose state and
countdown reflect the appliance's running cycle. Apple Home renders this as
an irrigation valve with "X min remaining" while a cycle is active.

Entity resolution is by device_id via the HA entity registry, so the same
profile works for washer and dryer regardless of their entity_ids.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pyhap.const import CATEGORY_SPRINKLER

from .base import GroupedAccessory

_LOGGER = logging.getLogger(__name__)

# ThinQ current_status values we treat as "cycle running"
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
_CHAR_ACTIVE = "Active"
_CHAR_IN_USE = "InUse"
_CHAR_VALVE_TYPE = "ValveType"
_CHAR_REMAINING_DURATION = "RemainingDuration"
_CHAR_SET_DURATION = "SetDuration"
_CHAR_NAME = "Name"
_CHAR_CONFIGURED_NAME = "ConfiguredName"

_VALVE_TYPE_IRRIGATION = 1

# HAP caps duration characteristics at 3600s.
_DURATION_MAX = 3600


class ThinqWasherAccessory(GroupedAccessory):
    """HAP accessory for a single LG ThinQ washer or dryer."""

    category = CATEGORY_SPRINKLER

    def _setup_services(self) -> None:
        self._status_entity: str | None = None
        self._remaining_entity: str | None = None
        self._total_entity: str | None = None
        self._operation_entity: str | None = None
        self._resolve_entities()

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
        self._char_active = serv_valve.configure_char(_CHAR_ACTIVE, value=0)
        self._char_in_use = serv_valve.configure_char(_CHAR_IN_USE, value=0)
        serv_valve.configure_char(_CHAR_VALVE_TYPE, value=_VALVE_TYPE_IRRIGATION)
        self._char_remaining = serv_valve.configure_char(
            _CHAR_REMAINING_DURATION, value=0
        )
        self._char_set_duration = serv_valve.configure_char(
            _CHAR_SET_DURATION, value=0
        )
        serv_valve.configure_char(_CHAR_NAME, value=self.display_name)
        serv_valve.configure_char(_CHAR_CONFIGURED_NAME, value=self.display_name)
        self._char_active.setter_callback = self._handle_valve_set

    def _resolve_entities(self) -> None:
        """Find the ThinQ entities bound to this device."""
        registry = er.async_get(self.hass)
        for entry in er.async_entries_for_device(registry, self.device_id):
            eid = entry.entity_id
            if eid.startswith("sensor.") and eid.endswith("_current_status"):
                self._status_entity = eid
            elif eid.startswith("sensor.") and eid.endswith("_remaining_time"):
                self._remaining_entity = eid
            elif eid.startswith("sensor.") and eid.endswith("_total_time"):
                self._total_entity = eid
            elif eid.startswith("select.") and eid.endswith("_operation"):
                self._operation_entity = eid

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
        ):
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
            running = 1 if state.state in _RUNNING_STATES else 0
            self._char_active.set_value(running)
            self._char_in_use.set_value(running)
            if not running:
                self._char_remaining.set_value(0)

        elif entity_id == self._remaining_entity:
            self._char_remaining.set_value(self._remaining_seconds(state.state))

        elif entity_id == self._total_entity:
            # total_time is in minutes.
            try:
                seconds = int(float(state.state)) * 60
            except (ValueError, TypeError):
                seconds = 0
            self._char_set_duration.set_value(min(max(seconds, 0), _DURATION_MAX))

    @staticmethod
    def _remaining_seconds(iso_ts: str) -> int:
        """ThinQ exposes remaining_time as an ISO timestamp of finish-at.
        Convert to remaining seconds from now, clamped to HAP's 0-3600 range."""
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

    # ---- writes from Apple Home back to HA -----------------------------

    def _handle_valve_set(self, value: int) -> None:
        if not self._operation_entity:
            return
        option = "start" if value else "stop"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": self._operation_entity, "option": option},
                blocking=False,
            )
        )
