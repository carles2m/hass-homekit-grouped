"""Home Connect fridge profile.

Grouped accessory for a Home Connect (Bosch / Siemens / Thermador /
Gaggenau) fridge-freezer combo. Combines door contact sensors,
door-left-open alarms, over-temperature alarm, and setpoint readouts
into a single HAP accessory so Apple Home shows one tile instead of
the 7+ tiles HA's built-in HomeKit Bridge produces.

Services exposed:
  - ContactSensor: Refrigerator Door
  - ContactSensor: Freezer Door
  - MotionSensor:  Refrigerator Door Alarm   (door left open too long)
  - MotionSensor:  Freezer Door Alarm
  - MotionSensor:  Freezer Temperature Alarm
  - TemperatureSensor: Refrigerator Setpoint
  - TemperatureSensor: Freezer Setpoint

Temperature sensors reflect the appliance SETPOINT (what the user has
dialed the compartment to) because Home Connect does not expose a
measured internal temperature. They're read-only.

Alarm sensors wrap the Home Connect *_alarm entities which have enum
states: "off" | "present" | "confirmed". We treat "present" and
"confirmed" both as alarm-active (user has not yet made the appliance
clear the alarm). Motion follows alarm state directly — one iOS
notification per alarm fire (motion 0->1 only), quiet on clear.
"""

from __future__ import annotations

import logging
from typing import Iterable

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pyhap.const import CATEGORY_OTHER, CATEGORY_SENSOR

from .base import GroupedAccessory

_LOGGER = logging.getLogger(__name__)

# Home Connect alarm enum
_ALARM_ACTIVE = frozenset({"present", "confirmed"})

_SERV_CONTACT = "ContactSensor"
_SERV_MOTION = "MotionSensor"
_SERV_TEMPERATURE = "TemperatureSensor"

_CHAR_CONTACT_STATE = "ContactSensorState"
_CHAR_MOTION_DETECTED = "MotionDetected"
_CHAR_CURRENT_TEMPERATURE = "CurrentTemperature"
_CHAR_NAME = "Name"
_CHAR_CONFIGURED_NAME = "ConfiguredName"

_CATEGORY_MAP = {
    "other": CATEGORY_OTHER,
    "sensor": CATEGORY_SENSOR,
}
_DEFAULT_CATEGORY_NAME = "other"


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


class HomeConnectFridgeAccessory(GroupedAccessory):
    """HAP accessory for a Home Connect fridge-freezer device."""

    def _setup_services(self) -> None:
        # Home Connect entity slots
        self._refrigerator_door_entity: str | None = None
        self._freezer_door_entity: str | None = None
        self._refrigerator_alarm_entity: str | None = None
        self._freezer_alarm_entity: str | None = None
        self._freezer_temp_alarm_entity: str | None = None
        self._fridge_temp_entity: str | None = None
        self._freezer_temp_entity: str | None = None
        self._resolve_entities()

        cat_name = self.overrides.get("category") or _DEFAULT_CATEGORY_NAME
        self.category = _CATEGORY_MAP[cat_name]

        # --- ContactSensor: Refrigerator Door -------------------------------
        self._char_refrigerator_door = self._add_contact(
            f"{self.display_name} Refrigerator Door"
        )
        # --- ContactSensor: Freezer Door ------------------------------------
        self._char_freezer_door = self._add_contact(
            f"{self.display_name} Freezer Door"
        )
        # --- MotionSensor: Refrigerator Door Alarm --------------------------
        self._char_refrigerator_door_alarm = self._add_motion(
            f"{self.display_name} Refrigerator Door Alarm"
        )
        # --- MotionSensor: Freezer Door Alarm -------------------------------
        self._char_freezer_door_alarm = self._add_motion(
            f"{self.display_name} Freezer Door Alarm"
        )
        # --- MotionSensor: Freezer Temperature Alarm ------------------------
        self._char_freezer_temp_alarm = self._add_motion(
            f"{self.display_name} Freezer Temperature Alarm"
        )
        # --- TemperatureSensor: Refrigerator Setpoint -----------------------
        self._char_fridge_temp = self._add_temperature(
            f"{self.display_name} Refrigerator Temperature"
        )
        # --- TemperatureSensor: Freezer Setpoint ----------------------------
        self._char_freezer_temp = self._add_temperature(
            f"{self.display_name} Freezer Temperature"
        )

    def _add_contact(self, name: str):
        serv = self.add_preload_service(
            _SERV_CONTACT,
            [_CHAR_CONTACT_STATE, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
        )
        char = serv.configure_char(_CHAR_CONTACT_STATE, value=0)
        serv.configure_char(_CHAR_NAME, value=name)
        serv.configure_char(_CHAR_CONFIGURED_NAME, value=name)
        return char

    def _add_motion(self, name: str):
        serv = self.add_preload_service(
            _SERV_MOTION,
            [_CHAR_MOTION_DETECTED, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
        )
        char = serv.configure_char(_CHAR_MOTION_DETECTED, value=False)
        serv.configure_char(_CHAR_NAME, value=name)
        serv.configure_char(_CHAR_CONFIGURED_NAME, value=name)
        return char

    def _add_temperature(self, name: str):
        serv = self.add_preload_service(
            _SERV_TEMPERATURE,
            [_CHAR_CURRENT_TEMPERATURE, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
        )
        # HAP CurrentTemperature: minValue -270, maxValue 100, step 0.1, in °C.
        # Freezer setpoints down to ~-22°C fit within that range.
        char = serv.configure_char(_CHAR_CURRENT_TEMPERATURE, value=0.0)
        serv.configure_char(_CHAR_NAME, value=name)
        serv.configure_char(_CHAR_CONFIGURED_NAME, value=name)
        return char

    def _resolve_entities(self) -> None:
        registry = er.async_get(self.hass)
        for entry in er.async_entries_for_device(registry, self.device_id):
            eid = entry.entity_id
            if eid.startswith("binary_sensor.") and eid.endswith("_refrigerator_door"):
                self._refrigerator_door_entity = eid
            elif eid.startswith("binary_sensor.") and eid.endswith("_freezer_door"):
                self._freezer_door_entity = eid
            elif eid.startswith("sensor.") and eid.endswith("_refrigerator_door_alarm"):
                self._refrigerator_alarm_entity = eid
            elif eid.startswith("sensor.") and eid.endswith("_freezer_door_alarm"):
                self._freezer_alarm_entity = eid
            elif eid.startswith("sensor.") and eid.endswith("_freezer_temperature_alarm"):
                self._freezer_temp_alarm_entity = eid
            elif eid.startswith("number.") and eid.endswith("_refrigerator_temperature"):
                self._fridge_temp_entity = eid
            elif eid.startswith("number.") and eid.endswith("_freezer_temperature"):
                self._freezer_temp_entity = eid

        missing = [
            name
            for name, value in [
                ("refrigerator_door", self._refrigerator_door_entity),
                ("freezer_door", self._freezer_door_entity),
                ("refrigerator_door_alarm", self._refrigerator_alarm_entity),
                ("freezer_door_alarm", self._freezer_alarm_entity),
                ("freezer_temperature_alarm", self._freezer_temp_alarm_entity),
                ("refrigerator_temperature", self._fridge_temp_entity),
                ("freezer_temperature", self._freezer_temp_entity),
            ]
            if value is None
        ]
        if missing:
            _LOGGER.warning(
                "%s: device %s missing expected Home Connect entities: %s",
                self.display_name,
                self.device_id,
                missing,
            )

    def _watched_entities(self) -> Iterable[str]:
        for eid in (
            self._refrigerator_door_entity,
            self._freezer_door_entity,
            self._refrigerator_alarm_entity,
            self._freezer_alarm_entity,
            self._freezer_temp_alarm_entity,
            self._fridge_temp_entity,
            self._freezer_temp_entity,
        ):
            if eid:
                yield eid

    def _push_state(self, entity_id: str, state: State | None) -> None:
        if state is None:
            return

        if entity_id == self._refrigerator_door_entity:
            self._char_refrigerator_door.set_value(
                self._contact_value(state.state)
            )
        elif entity_id == self._freezer_door_entity:
            self._char_freezer_door.set_value(self._contact_value(state.state))
        elif entity_id == self._refrigerator_alarm_entity:
            self._char_refrigerator_door_alarm.set_value(
                state.state in _ALARM_ACTIVE
            )
        elif entity_id == self._freezer_alarm_entity:
            self._char_freezer_door_alarm.set_value(
                state.state in _ALARM_ACTIVE
            )
        elif entity_id == self._freezer_temp_alarm_entity:
            self._char_freezer_temp_alarm.set_value(
                state.state in _ALARM_ACTIVE
            )
        elif entity_id == self._fridge_temp_entity:
            self._push_temperature(self._char_fridge_temp, state)
        elif entity_id == self._freezer_temp_entity:
            self._push_temperature(self._char_freezer_temp, state)

    @staticmethod
    def _contact_value(source_state: str) -> int:
        """ContactSensorState: 0 = closed/contact_detected, 1 = open.
        HA binary_sensor with device_class=door is 'on' when open."""
        return 1 if source_state == "on" else 0

    def _push_temperature(self, char, state: State) -> None:
        """Number entity reports in °F; HAP CurrentTemperature expects °C."""
        if state.state in ("unknown", "unavailable"):
            return
        try:
            f = float(state.state)
        except (ValueError, TypeError):
            return
        unit = state.attributes.get("unit_of_measurement")
        if unit == "°C":
            char.set_value(round(f, 1))
        else:
            char.set_value(round(_f_to_c(f), 1))
