"""ThinQ washer/dryer profile.

Exposes one HAP accessory with:
  - Valve service    (Active when the appliance is running a cycle)
  - Switch service   (Power toggle)

Apple Home renders this as a single tile that shows the valve state as the
primary control and notifies on transitions. Cycle-end notifications come
from the Valve flipping Active=0 when the appliance reports status "end".

Uses the standard ThinQ integration entities (sensor.*_current_status,
switch.*_power) — no custom config needed beyond the device_id.

Entity resolution: the profile looks up entities by device_id via the HA
entity registry, so the same profile works for washer or dryer regardless
of which specific entity_ids they ended up with.
"""

from __future__ import annotations

import logging
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

# Pyhap service/characteristic names (from HAP spec)
_SERV_VALVE = "Valve"
_SERV_SWITCH = "Switch"
_CHAR_ACTIVE = "Active"
_CHAR_IN_USE = "InUse"
_CHAR_VALVE_TYPE = "ValveType"
_CHAR_ON = "On"
_CHAR_NAME = "Name"
_CHAR_CONFIGURED_NAME = "ConfiguredName"

# ValveType: 0=Generic, 1=Irrigation, 2=Shower, 3=Water Faucet
_VALVE_TYPE_GENERIC = 0


class ThinqWasherAccessory(GroupedAccessory):
    """HAP accessory for a single LG ThinQ washer or dryer."""

    category = CATEGORY_FAUCET  # Apple Home shows this as a water-valve style icon

    def _setup_services(self) -> None:
        self._status_entity: str | None = None
        self._power_entity: str | None = None
        self._resolve_entities()

        # Valve service — primary control surface.
        serv_valve = self.add_preload_service(
            _SERV_VALVE,
            [_CHAR_ACTIVE, _CHAR_IN_USE, _CHAR_VALVE_TYPE, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
        )
        self._char_active = serv_valve.configure_char(_CHAR_ACTIVE, value=0)
        self._char_in_use = serv_valve.configure_char(_CHAR_IN_USE, value=0)
        serv_valve.configure_char(_CHAR_VALVE_TYPE, value=_VALVE_TYPE_GENERIC)
        serv_valve.configure_char(_CHAR_NAME, value=self.display_name)
        serv_valve.configure_char(_CHAR_CONFIGURED_NAME, value=self.display_name)

        # Valve Active is writable in HomeKit (user can "close" the valve).
        # We interpret a write to Active=0 as "stop the cycle" via the HA switch.
        self._char_active.setter_callback = self._handle_valve_set

        # Switch service — power on/off.
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
        """Find the ThinQ status + power entities bound to this device."""
        registry = er.async_get(self.hass)
        for entry in er.async_entries_for_device(registry, self.device_id):
            eid = entry.entity_id
            if eid.endswith("_current_status") and eid.startswith("sensor."):
                self._status_entity = eid
            elif eid.endswith("_power") and eid.startswith("switch."):
                self._power_entity = eid
        if not self._status_entity:
            _LOGGER.warning(
                "No *_current_status sensor found for device %s (%s)",
                self.device_id,
                self.display_name,
            )
        if not self._power_entity:
            _LOGGER.warning(
                "No *_power switch found for device %s (%s)",
                self.device_id,
                self.display_name,
            )

    def _watched_entities(self) -> Iterable[str]:
        for eid in (self._status_entity, self._power_entity):
            if eid:
                yield eid

    def _push_state(self, entity_id: str, state: State | None) -> None:
        if state is None or state.state in ("unknown", "unavailable"):
            return

        if entity_id == self._status_entity:
            running = 1 if state.state in _RUNNING_STATES else 0
            self._char_active.set_value(running)
            self._char_in_use.set_value(running)

        elif entity_id == self._power_entity:
            on = 1 if state.state == "on" else 0
            self._char_on.set_value(on)

    # ---- writes from Apple Home back to HA -----------------------------

    def _handle_valve_set(self, value: int) -> None:
        """Apple Home toggled the valve. Value 1 = open (start), 0 = close (stop)."""
        if not self._status_entity:
            return
        # Find the paired select.*_operation entity to send start/stop.
        registry = er.async_get(self.hass)
        select_entity = None
        for entry in er.async_entries_for_device(registry, self.device_id):
            if entry.entity_id.endswith("_operation") and entry.entity_id.startswith(
                "select."
            ):
                select_entity = entry.entity_id
                break
        if not select_entity:
            return
        option = "start" if value else "stop"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": select_entity, "option": option},
                blocking=False,
            )
        )

    def _handle_power_set(self, value: int) -> None:
        """Apple Home toggled the power switch."""
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
