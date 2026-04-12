"""ThinQ washer/dryer profile.

Exposes one HAP accessory with:
  - Valve service    (Active+InUse while running, RemainingDuration from
                      the integration's *_remaining_time timestamp)
  - Switch service   (Power toggle; note: ThinQ's power switch lags the
                      physical appliance state, so this is informational)

Entity resolution is by device_id via the HA entity registry, so the
same profile works for washer and dryer regardless of their entity_ids.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pyhap.const import CATEGORY_FAUCET

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
_SERV_SWITCH = "Switch"
_CHAR_ACTIVE = "Active"
_CHAR_IN_USE = "InUse"
_CHAR_VALVE_TYPE = "ValveType"
_CHAR_REMAINING_DURATION = "RemainingDuration"
_CHAR_ON = "On"
_CHAR_NAME = "Name"
_CHAR_CONFIGURED_NAME = "ConfiguredName"

_VALVE_TYPE_GENERIC = 0

# HAP spec caps RemainingDuration at 3600 seconds (1 hour). Our washer cycles
# can exceed that; we clamp and let the tile simply say ">1h" until it's closer.
_REMAINING_DURATION_MAX = 3600


class ThinqWasherAccessory(GroupedAccessory):
    """HAP accessory for a single LG ThinQ washer or dryer."""

    category = CATEGORY_FAUCET

    def _setup_services(self) -> None:
        self._status_entity: str | None = None
        self._power_entity: str | None = None
        self._remaining_entity: str | None = None
        self._operation_entity: str | None = None
        self._resolve_entities()

        serv_valve = self.add_preload_service(
            _SERV_VALVE,
            [
                _CHAR_ACTIVE,
                _CHAR_IN_USE,
                _CHAR_VALVE_TYPE,
                _CHAR_REMAINING_DURATION,
                _CHAR_NAME,
                _CHAR_CONFIGURED_NAME,
            ],
        )
        self._char_active = serv_valve.configure_char(_CHAR_ACTIVE, value=0)
        self._char_in_use = serv_valve.configure_char(_CHAR_IN_USE, value=0)
        serv_valve.configure_char(_CHAR_VALVE_TYPE, value=_VALVE_TYPE_GENERIC)
        self._char_remaining = serv_valve.configure_char(
            _CHAR_REMAINING_DURATION, value=0
        )
        serv_valve.configure_char(_CHAR_NAME, value=self.display_name)
        serv_valve.configure_char(_CHAR_CONFIGURED_NAME, value=self.display_name)
        self._char_active.setter_callback = self._handle_valve_set

        serv_switch = self.add_preload_service(
            _SERV_SWITCH, [_CHAR_ON, _CHAR_NAME, _CHAR_CONFIGURED_NAME]
        )
        self._char_on = serv_switch.configure_char(_CHAR_ON, value=0)
        serv_switch.configure_char(_CHAR_NAME, value=f"{self.display_name} Power")
        serv_switch.configure_char(
            _CHAR_CONFIGURED_NAME, value=f"{self.display_name} Power"
        )
        self._char_on.setter_callback = self._handle_power_set

    def _resolve_entities(self) -> None:
        """Find the ThinQ entities bound to this device."""
        registry = er.async_get(self.hass)
        for entry in er.async_entries_for_device(registry, self.device_id):
            eid = entry.entity_id
            if eid.startswith("sensor.") and eid.endswith("_current_status"):
                self._status_entity = eid
            elif eid.startswith("switch.") and eid.endswith("_power"):
                self._power_entity = eid
            elif eid.startswith("sensor.") and eid.endswith("_remaining_time"):
                self._remaining_entity = eid
            elif eid.startswith("select.") and eid.endswith("_operation"):
                self._operation_entity = eid

        missing = [
            n
            for n, v in [
                ("current_status", self._status_entity),
                ("power", self._power_entity),
            ]
            if not v
        ]
        if missing:
            _LOGGER.warning(
                "Device %s (%s) missing expected entities: %s",
                self.device_id,
                self.display_name,
                missing,
            )

    def _watched_entities(self) -> Iterable[str]:
        for eid in (
            self._status_entity,
            self._power_entity,
            self._remaining_entity,
        ):
            if eid:
                yield eid

    def _push_state(self, entity_id: str, state: State | None) -> None:
        if state is None or state.state in ("unknown", "unavailable"):
            if entity_id == self._remaining_entity:
                self._char_remaining.set_value(0)
            return

        if entity_id == self._status_entity:
            running = 1 if state.state in _RUNNING_STATES else 0
            self._char_active.set_value(running)
            self._char_in_use.set_value(running)
            if not running:
                self._char_remaining.set_value(0)

        elif entity_id == self._power_entity:
            self._char_on.set_value(1 if state.state == "on" else 0)

        elif entity_id == self._remaining_entity:
            seconds = self._remaining_seconds(state.state)
            self._char_remaining.set_value(seconds)

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
        return min(remaining, _REMAINING_DURATION_MAX)

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

    def _handle_power_set(self, value: int) -> None:
        if not self._power_entity:
            return
        service = "turn_on" if value else "turn_off"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "switch",
                service,
                {"entity_id": self._power_entity},
                blocking=False,
            )
        )
