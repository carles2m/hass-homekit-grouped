"""EcoNet (Rheem) heat pump water heater profile.

Grouped accessory for a Rheem EcoNet water heater. Replaces HA's built-in
HomeKit Bridge thermostat mapping which emits a persistent
`TargetHeatingCoolingState: value=0 is an invalid value` error when the
appliance is in an EcoNet-specific mode like `eco` that doesn't cleanly
map onto HomeKit's {Off, Heat, Cool, Auto} vocabulary.

Services:
  - Thermostat (primary) — Off/Heat mode toggle + temperature setpoint.
    Mode writes are subject to a known upstream bug in the EcoNet
    integration (home-assistant/core#159232): the cloud sometimes
    rejects an HA-issued `off` and reverts the entity state back to
    the base heating mode within seconds. We mask the visual bounce
    in HomeKit with a 15-second pending-mode suppression window so
    the user's tap doesn't snap back during the legitimate cloud
    round-trip; if the cloud genuinely rejects the write, HomeKit will
    re-sync to the actual state after the window expires.
    HAP requires CurrentTemperature but EcoNet doesn't expose a
    measured water temperature; we mirror the target temp as the
    current (same trick as the fridge profile).
  - MotionSensor "Alert" (opt-in) — fires when alert_count > 0.
  - OccupancySensor "Hot Water Low" (opt-in with threshold) — fires
    when available_hot_water drops below the configured percent.
"""

from __future__ import annotations

import logging
from typing import Iterable

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from pyhap.const import CATEGORY_THERMOSTAT

from .base import GroupedAccessory

_LOGGER = logging.getLogger(__name__)

_SERV_THERMOSTAT = "Thermostat"
_SERV_MOTION = "MotionSensor"
_SERV_OCCUPANCY = "OccupancySensor"
_SERV_CONTACT = "ContactSensor"

_CHAR_CURRENT_HEATING_COOLING = "CurrentHeatingCoolingState"
_CHAR_TARGET_HEATING_COOLING = "TargetHeatingCoolingState"
_CHAR_CURRENT_TEMPERATURE = "CurrentTemperature"
_CHAR_TARGET_TEMPERATURE = "TargetTemperature"
_CHAR_DISPLAY_UNITS = "TemperatureDisplayUnits"
_CHAR_MOTION_DETECTED = "MotionDetected"
_CHAR_OCCUPANCY_DETECTED = "OccupancyDetected"
_CHAR_CONTACT_STATE = "ContactSensorState"
_CHAR_NAME = "Name"
_CHAR_CONFIGURED_NAME = "ConfiguredName"

# HAP TargetHeatingCoolingState values
_HCS_OFF = 0
_HCS_HEAT = 1

# EcoNet operation modes that represent "actively heating (some flavor)"
_HEATING_MODES = frozenset(
    {"eco", "heat_pump", "electric", "high_demand", "gas", "performance"}
)
_DEFAULT_HEAT_MODE = "eco"

# Window to hold the pending-target-mode lock after a user write. EcoNet
# cloud round-trip can take several seconds, and the appliance state can
# flicker (off -> eco -> off) during reconciliation. We suppress any state
# push that contradicts the user's request for this long.
_PENDING_MODE_HOLD_SECONDS = 15

# HAP TemperatureDisplayUnits: 0 = Celsius, 1 = Fahrenheit
_DISPLAY_UNITS_FAHRENHEIT = 1

_PERM_READ = "pr"
_PERM_NOTIFY = "ev"


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


class EcoNetWaterHeaterAccessory(GroupedAccessory):
    """HAP accessory for a Rheem EcoNet heat pump water heater."""

    category = CATEGORY_THERMOSTAT

    def _setup_services(self) -> None:
        self._water_heater_entity: str | None = None
        self._running_entity: str | None = None
        self._hot_water_entity: str | None = None
        self._alert_count_entity: str | None = None
        self._last_heat_mode: str = _DEFAULT_HEAT_MODE
        # Pending-mode lock state. See _PENDING_MODE_HOLD_SECONDS comment.
        self._pending_target_mode: int | None = None
        self._pending_clear_cancel = None
        self._resolve_entities()

        hot_water_low_threshold = self.overrides.get(
            "hot_water_low_threshold"
        )
        alert_sensor = self.overrides.get("alert_sensor") is True
        no_hot_water_sensor = self.overrides.get("no_hot_water_sensor") is True

        # --- Thermostat (primary) -------------------------------------------
        serv_therm = self.add_preload_service(
            _SERV_THERMOSTAT,
            [
                _CHAR_CURRENT_HEATING_COOLING,
                _CHAR_TARGET_HEATING_COOLING,
                _CHAR_CURRENT_TEMPERATURE,
                _CHAR_TARGET_TEMPERATURE,
                _CHAR_DISPLAY_UNITS,
                _CHAR_NAME,
                _CHAR_CONFIGURED_NAME,
            ],
        )
        # TargetHeatingCoolingState restricted to Off/Heat (no cool/auto).
        # Writable: HomeKit can toggle the mode. The upstream EcoNet bug
        # may cause "off" writes to be reverted by the cloud — that's
        # surfaced to HA as a state flip back to the base mode and our
        # 15-second pending lock masks the visual bounce while it
        # resolves; if the cloud genuinely rejects, the lock expires and
        # HomeKit re-syncs to the actual state.
        self._char_target_mode = serv_therm.configure_char(
            _CHAR_TARGET_HEATING_COOLING,
            value=_HCS_OFF,
            valid_values={"Off": _HCS_OFF, "Heat": _HCS_HEAT},
        )
        self._char_target_mode.setter_callback = self._handle_mode_set
        self._char_current_mode = serv_therm.configure_char(
            _CHAR_CURRENT_HEATING_COOLING,
            value=_HCS_OFF,
            valid_values={"Off": _HCS_OFF, "Heat": _HCS_HEAT},
        )

        # Temperature range from the water_heater entity attrs.
        min_f, max_f = self._read_temp_range(110.0, 140.0)
        self._char_target_temp = serv_therm.configure_char(
            _CHAR_TARGET_TEMPERATURE,
            value=_f_to_c(120.0),
            properties={
                "minValue": round(_f_to_c(min_f), 1),
                "maxValue": round(_f_to_c(max_f), 1),
                "minStep": 0.5,
            },
        )
        self._char_current_temp = serv_therm.configure_char(
            _CHAR_CURRENT_TEMPERATURE, value=_f_to_c(120.0)
        )
        # TemperatureDisplayUnits is for an accessory's physical screen
        # and has no effect on Apple Home (which uses the phone's region)
        # or on HA. Read-only to suppress a toggle that does nothing.
        serv_therm.configure_char(
            _CHAR_DISPLAY_UNITS,
            value=_DISPLAY_UNITS_FAHRENHEIT,
            properties={"Permissions": [_PERM_READ, _PERM_NOTIFY]},
        )
        serv_therm.configure_char(_CHAR_NAME, value=self.display_name)
        serv_therm.configure_char(
            _CHAR_CONFIGURED_NAME, value=self.display_name
        )

        self._char_target_temp.setter_callback = self._handle_temp_set

        # --- MotionSensor: Alert (opt-in) -----------------------------------
        self._char_alert = None
        if alert_sensor:
            alert_name = f"{self.display_name} Alert"
            serv_alert = self.add_preload_service(
                _SERV_MOTION,
                [_CHAR_MOTION_DETECTED, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
            )
            self._char_alert = serv_alert.configure_char(
                _CHAR_MOTION_DETECTED, value=False
            )
            serv_alert.configure_char(_CHAR_NAME, value=alert_name)
            serv_alert.configure_char(_CHAR_CONFIGURED_NAME, value=alert_name)

        # --- OccupancySensor: Hot Water Low (opt-in) ------------------------
        self._char_hot_water_low = None
        self._hot_water_low_threshold = None
        if hot_water_low_threshold is not None and self._hot_water_entity:
            self._hot_water_low_threshold = int(hot_water_low_threshold)
            low_name = f"{self.display_name} Hot Water Low"
            serv_low = self.add_preload_service(
                _SERV_OCCUPANCY,
                [_CHAR_OCCUPANCY_DETECTED, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
            )
            self._char_hot_water_low = serv_low.configure_char(
                _CHAR_OCCUPANCY_DETECTED, value=0
            )
            serv_low.configure_char(_CHAR_NAME, value=low_name)
            serv_low.configure_char(_CHAR_CONFIGURED_NAME, value=low_name)

        # --- ContactSensor: No Hot Water (opt-in) ---------------------------
        # Fires (open) when available_hot_water reaches 0%, closes again
        # when it climbs above 0. ContactSensor notifications include the
        # accessory name in iOS ("<Name>: was opened" / "was closed") so
        # the alert is clearly identifiable, at the cost of two pings per
        # event (open, then close). Acceptable for a rare event like the
        # tank running dry.
        self._char_no_hot_water = None
        if no_hot_water_sensor and self._hot_water_entity:
            empty_name = f"{self.display_name} No Hot Water"
            serv_empty = self.add_preload_service(
                _SERV_CONTACT,
                [_CHAR_CONTACT_STATE, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
            )
            # ContactSensorState: 0 = closed (contact detected), 1 = open
            self._char_no_hot_water = serv_empty.configure_char(
                _CHAR_CONTACT_STATE, value=0
            )
            serv_empty.configure_char(_CHAR_NAME, value=empty_name)
            serv_empty.configure_char(_CHAR_CONFIGURED_NAME, value=empty_name)

        # Mark the Thermostat as HAP primary so future strict sub-services
        # (FilterMaintenance-class) would render. Current sub-services are
        # "forgiving" and work either way; this is purely future-proofing.
        self.set_primary_service(serv_therm)

    def _resolve_entities(self) -> None:
        registry = er.async_get(self.hass)
        for entry in er.async_entries_for_device(registry, self.device_id):
            eid = entry.entity_id
            if eid.startswith("water_heater."):
                self._water_heater_entity = eid
            elif eid.endswith("_running"):
                self._running_entity = eid
            elif eid.endswith("_available_hot_water"):
                self._hot_water_entity = eid
            elif eid.endswith("_alert_count"):
                self._alert_count_entity = eid

        if not self._water_heater_entity:
            _LOGGER.warning(
                "%s: no water_heater.* entity found on device %s",
                self.display_name,
                self.device_id,
            )

    def _read_temp_range(self, default_min_f: float, default_max_f: float):
        if not self._water_heater_entity:
            return default_min_f, default_max_f
        state = self.hass.states.get(self._water_heater_entity)
        if state is None:
            return default_min_f, default_max_f
        min_t = state.attributes.get("min_temp", default_min_f)
        max_t = state.attributes.get("max_temp", default_max_f)
        try:
            return float(min_t), float(max_t)
        except (TypeError, ValueError):
            return default_min_f, default_max_f

    def _watched_entities(self) -> Iterable[str]:
        for eid in (
            self._water_heater_entity,
            self._running_entity,
            self._hot_water_entity,
            self._alert_count_entity,
        ):
            if eid:
                yield eid

    def _push_state(self, entity_id: str, state: State | None) -> None:
        if state is None:
            return

        if entity_id == self._water_heater_entity:
            self._push_water_heater(state)
        elif entity_id == self._running_entity:
            self._push_running(state)
        elif entity_id == self._hot_water_entity:
            self._push_hot_water(state)
        elif entity_id == self._alert_count_entity:
            self._push_alert(state)

    def _push_water_heater(self, state: State) -> None:
        mode = state.state
        new_target: int | None = None
        if mode in _HEATING_MODES:
            self._last_heat_mode = mode
            new_target = _HCS_HEAT
        elif mode == "off":
            new_target = _HCS_OFF

        # Pending-lock: during the post-write hold window, only push
        # state changes that match the user's requested target. Mismatches
        # are silently dropped. The hold is cleared by a timer, not by the
        # first matching state, so EcoNet's flicker during reconciliation
        # doesn't trigger a bounce back to the old mode.
        if new_target is not None and self._pending_target_mode is not None:
            if new_target != self._pending_target_mode:
                return

        if new_target is not None:
            self._char_target_mode.set_value(new_target)

        # Temperature setpoint
        target_f = state.attributes.get("temperature")
        if target_f is not None:
            try:
                target_c = _f_to_c(float(target_f))
                self._char_target_temp.set_value(round(target_c, 1))
                current_f = state.attributes.get("current_temperature")
                if current_f is None:
                    self._char_current_temp.set_value(round(target_c, 1))
                else:
                    self._char_current_temp.set_value(
                        round(_f_to_c(float(current_f)), 1)
                    )
            except (TypeError, ValueError):
                pass

    def _push_running(self, state: State) -> None:
        if state.state == "on":
            self._char_current_mode.set_value(_HCS_HEAT)
        else:
            self._char_current_mode.set_value(_HCS_OFF)

    def _push_hot_water(self, state: State) -> None:
        try:
            pct = int(float(state.state))
        except (TypeError, ValueError):
            return
        if self._char_hot_water_low is not None and self._hot_water_low_threshold is not None:
            self._char_hot_water_low.set_value(
                1 if pct < self._hot_water_low_threshold else 0
            )
        if self._char_no_hot_water is not None:
            # ContactSensor: 1 = open (alarming) when no hot water
            self._char_no_hot_water.set_value(1 if pct == 0 else 0)

    def _push_alert(self, state: State) -> None:
        if self._char_alert is None:
            return
        try:
            count = int(float(state.state))
        except (TypeError, ValueError):
            return
        self._char_alert.set_value(count > 0)

    # ---- writes from HomeKit back to HA --------------------------------

    def _handle_mode_set(self, value: int) -> None:
        """HomeKit TargetHeatingCoolingState write. Map Off -> 'off' and
        Heat -> whatever non-off mode the appliance was last in (or
        'eco' as a safe default)."""
        if not self._water_heater_entity:
            return
        # Record the user-requested target and (re)start the suppression
        # timer. _push_water_heater drops contradicting state pushes during
        # the window so EcoNet cloud's slow round-trip doesn't visibly
        # bounce HomeKit's UI back to the previous mode.
        self._pending_target_mode = value
        if self._pending_clear_cancel is not None:
            try:
                self._pending_clear_cancel()
            except Exception:  # noqa: BLE001
                pass
            self._pending_clear_cancel = None
        self._pending_clear_cancel = async_call_later(
            self.hass,
            _PENDING_MODE_HOLD_SECONDS,
            self._clear_pending_mode,
        )
        if value == _HCS_OFF:
            operation_mode = "off"
        else:
            operation_mode = self._last_heat_mode
        self.hass.async_create_task(
            self.hass.services.async_call(
                "water_heater",
                "set_operation_mode",
                {
                    "entity_id": self._water_heater_entity,
                    "operation_mode": operation_mode,
                },
                blocking=False,
            )
        )

    def _clear_pending_mode(self, _now) -> None:
        self._pending_target_mode = None
        self._pending_clear_cancel = None

    def _handle_temp_set(self, value_c: float) -> None:
        """HomeKit TargetTemperature write (°C). EcoNet expects °F."""
        if not self._water_heater_entity:
            return
        target_f = round(_c_to_f(value_c))
        self.hass.async_create_task(
            self.hass.services.async_call(
                "water_heater",
                "set_temperature",
                {
                    "entity_id": self._water_heater_entity,
                    "temperature": target_f,
                },
                blocking=False,
            )
        )
