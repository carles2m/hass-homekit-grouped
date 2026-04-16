"""Coway Airmega / IoCare air purifier profile.

Grouped accessory for Coway air purifiers paired through the IoCare
integration. Wraps the HA entities (fan, sensors, light switch) into a
single HAP accessory so Apple Home shows one tile with all controls.

HA's built-in HomeKit Bridge already handles fan entities reasonably
for air purifiers; this profile exists so the purifier lives on the
same grouped bridge as the washer/dryer/fridge/water heater (one
HomeKit bridge paired in Apple Home, consistent experience).

Services:
  - AirPurifier (primary) — Active + CurrentAirPurifierState +
    TargetAirPurifierState (auto/manual) + RotationSpeed. Driven by
    the `fan.*` entity; preset_mode "Auto" maps to Target=auto.
  - AirQualitySensor (secondary) — AirQuality enum from the
    `*_indoor_air_quality` sensor plus PM10Density from
    `*_particulate_matter_10`.
  - Switch "Night" (secondary) — toggles the "Night" preset mode on
    the fan (HomeKit AirPurifier has no native Night mode
    characteristic).
  - Lightbulb (secondary) — wraps the `switch.*_light` entity.
  - LightSensor (secondary, opt-in via `ambient_light_sensor: true`) —
    CurrentAmbientLightLevel fed by the `sensor.*_lux` entity.
  - FilterMaintenance (secondary, opt-in via `filter_change_sensor:
    true`) — FilterChangeIndication flips to ChangeFilter(1) when the
    `sensor.*_max2_filter` life-remaining percentage drops below
    `filter_change_threshold` (default 10). FilterLifeLevel mirrors
    the raw value. Both opt-in sensors are gated because enabling
    them on an already-paired accessory changes the service
    composition; worst case the bridge needs to be re-paired.

A prior FilterMaintenance attempt was pulled on the belief iOS
doesn't render the service. On re-reading the HAP spec,
FilterChangeIndication is *required* and the earlier implementation
likely omitted it — iOS silently ignores services missing required
characteristics, which matches the "doesn't render" symptom. This
implementation declares it correctly.

Entities ignored for now (niche config, set-and-forget):
  - select.*_current_timer, select.*_smart_mode_sensitivity,
    select.*_pre_filter_wash_frequency, sensor.*_pre_filter,
    sensor.*_timer_remaining.
"""

from __future__ import annotations

import logging
from typing import Iterable

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pyhap.const import CATEGORY_AIR_PURIFIER

from .base import GroupedAccessory

_LOGGER = logging.getLogger(__name__)

_SERV_AIR_PURIFIER = "AirPurifier"
_SERV_AIR_QUALITY = "AirQualitySensor"
_SERV_SWITCH = "Switch"
_SERV_LIGHTBULB = "Lightbulb"
_SERV_LIGHT_SENSOR = "LightSensor"
_SERV_FILTER_MAINTENANCE = "FilterMaintenance"

_CHAR_ACTIVE = "Active"
_CHAR_CURRENT_AP_STATE = "CurrentAirPurifierState"
_CHAR_TARGET_AP_STATE = "TargetAirPurifierState"
_CHAR_ROTATION_SPEED = "RotationSpeed"
_CHAR_AIR_QUALITY = "AirQuality"
_CHAR_PM10_DENSITY = "PM10Density"
_CHAR_AMBIENT_LIGHT = "CurrentAmbientLightLevel"
_CHAR_FILTER_CHANGE_INDICATION = "FilterChangeIndication"
_CHAR_FILTER_LIFE_LEVEL = "FilterLifeLevel"
_CHAR_ON = "On"
_CHAR_NAME = "Name"
_CHAR_CONFIGURED_NAME = "ConfiguredName"

# HAP CurrentAmbientLightLevel valid range in lux.
_LUX_MIN = 0.0001
_LUX_MAX = 100000.0

# HAP FilterChangeIndication enum: 0=FilterOK, 1=ChangeFilter.
_FILTER_OK = 0
_FILTER_CHANGE = 1
_FILTER_CHANGE_THRESHOLD_DEFAULT = 10  # % — fire ChangeFilter below this

# HAP AirPurifier enums
_AP_INACTIVE = 0
_AP_IDLE = 1
_AP_PURIFYING = 2
_AP_TARGET_MANUAL = 0
_AP_TARGET_AUTO = 1

# HAP AirQuality enum: 0=Unknown, 1=Excellent, 2=Good, 3=Fair, 4=Inferior, 5=Poor
_AIR_QUALITY_MAP = {
    # Coway integration values observed in the wild; mapped to HAP enum.
    "good": 2,
    "moderate": 3,
    "fair": 3,
    "unhealthy": 4,
    "inferior": 4,
    "very_unhealthy": 5,
    "very unhealthy": 5,
    "poor": 5,
    "bad": 5,
    "excellent": 1,
}

_NIGHT_PRESET = "Night"
_AUTO_PRESET = "Auto"


class CowayAirPurifierAccessory(GroupedAccessory):
    """HAP accessory for a Coway Airmega / IoCare air purifier."""

    category = CATEGORY_AIR_PURIFIER

    def _setup_services(self) -> None:
        self._fan_entity: str | None = None
        self._light_entity: str | None = None
        self._pm10_entity: str | None = None
        self._air_quality_entity: str | None = None
        self._lux_entity: str | None = None
        self._filter_entity: str | None = None
        self._resolve_entities()

        expose_night_switch = (
            self.overrides.get("night_mode_switch") is not False
        )
        expose_light = self.overrides.get("light") is not False
        expose_lux = self.overrides.get("ambient_light_sensor") is True
        expose_filter = self.overrides.get("filter_change_sensor") is True
        self._filter_change_threshold = int(
            self.overrides.get("filter_change_threshold")
            or _FILTER_CHANGE_THRESHOLD_DEFAULT
        )

        # --- AirPurifier (HAP primary service) ------------------------------
        # Mirrors HA core: sub-services (FilterMaintenance in particular)
        # only render when linked to a service that's marked as the
        # accessory's primary in HAP. Accessory category alone is not
        # enough; iOS looks at the is_primary_service flag.
        serv_ap = self.add_preload_service(
            _SERV_AIR_PURIFIER,
            [
                _CHAR_ACTIVE,
                _CHAR_CURRENT_AP_STATE,
                _CHAR_TARGET_AP_STATE,
                _CHAR_ROTATION_SPEED,
                _CHAR_NAME,
                _CHAR_CONFIGURED_NAME,
            ],
        )
        self._char_active = serv_ap.configure_char(_CHAR_ACTIVE, value=0)
        self._char_current_state = serv_ap.configure_char(
            _CHAR_CURRENT_AP_STATE, value=_AP_INACTIVE
        )
        self._char_target_state = serv_ap.configure_char(
            _CHAR_TARGET_AP_STATE, value=_AP_TARGET_MANUAL
        )
        self._char_speed = serv_ap.configure_char(
            _CHAR_ROTATION_SPEED,
            value=0,
            properties={"minValue": 0, "maxValue": 100, "minStep": 1},
        )
        serv_ap.configure_char(_CHAR_NAME, value=self.display_name)
        serv_ap.configure_char(_CHAR_CONFIGURED_NAME, value=self.display_name)

        self._char_active.setter_callback = self._handle_active_set
        self._char_target_state.setter_callback = self._handle_target_set
        self._char_speed.setter_callback = self._handle_speed_set

        # --- AirQualitySensor -----------------------------------------------
        self._char_air_quality = None
        self._char_pm10 = None
        if self._air_quality_entity or self._pm10_entity:
            aq_name = f"{self.display_name} Air Quality"
            chars = [_CHAR_AIR_QUALITY, _CHAR_NAME, _CHAR_CONFIGURED_NAME]
            if self._pm10_entity:
                chars.insert(1, _CHAR_PM10_DENSITY)
            serv_aq = self.add_preload_service(_SERV_AIR_QUALITY, chars)
            self._char_air_quality = serv_aq.configure_char(
                _CHAR_AIR_QUALITY, value=0
            )
            if self._pm10_entity:
                self._char_pm10 = serv_aq.configure_char(
                    _CHAR_PM10_DENSITY, value=0
                )
            serv_aq.configure_char(_CHAR_NAME, value=aq_name)
            serv_aq.configure_char(_CHAR_CONFIGURED_NAME, value=aq_name)
            serv_ap.add_linked_service(serv_aq)

        # --- Switch "Night" (preset mode) -----------------------------------
        self._char_night = None
        if expose_night_switch:
            night_name = f"{self.display_name} Night Mode"
            serv_night = self.add_preload_service(
                _SERV_SWITCH, [_CHAR_ON, _CHAR_NAME, _CHAR_CONFIGURED_NAME]
            )
            self._char_night = serv_night.configure_char(_CHAR_ON, value=0)
            serv_night.configure_char(_CHAR_NAME, value=night_name)
            serv_night.configure_char(
                _CHAR_CONFIGURED_NAME, value=night_name
            )
            self._char_night.setter_callback = self._handle_night_set

        # --- Lightbulb ------------------------------------------------------
        self._char_light = None
        if expose_light and self._light_entity:
            light_name = f"{self.display_name} Light"
            serv_light = self.add_preload_service(
                _SERV_LIGHTBULB,
                [_CHAR_ON, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
            )
            self._char_light = serv_light.configure_char(_CHAR_ON, value=0)
            serv_light.configure_char(_CHAR_NAME, value=light_name)
            serv_light.configure_char(
                _CHAR_CONFIGURED_NAME, value=light_name
            )
            self._char_light.setter_callback = self._handle_light_set

        # --- LightSensor ----------------------------------------------------
        self._char_ambient_light = None
        if expose_lux and self._lux_entity:
            lux_name = f"{self.display_name} Light Level"
            serv_lux = self.add_preload_service(
                _SERV_LIGHT_SENSOR,
                [_CHAR_AMBIENT_LIGHT, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
            )
            self._char_ambient_light = serv_lux.configure_char(
                _CHAR_AMBIENT_LIGHT, value=_LUX_MIN
            )
            serv_lux.configure_char(_CHAR_NAME, value=lux_name)
            serv_lux.configure_char(_CHAR_CONFIGURED_NAME, value=lux_name)
            serv_ap.add_linked_service(serv_lux)

        # --- FilterMaintenance (appended LAST) ------------------------------
        # Required char is FilterChangeIndication; FilterLifeLevel is
        # optional but useful. Omitting FilterChangeIndication would make
        # the service invalid and iOS would silently ignore it.
        self._char_filter_change = None
        self._char_filter_life = None
        if expose_filter and self._filter_entity:
            filter_name = f"{self.display_name} Filter"
            # Char set + order mirror HA core's reference implementation
            # (PR #142467). ConfiguredName is deliberately omitted — it's
            # not in HAP's spec for FilterMaintenance, and iOS rejects
            # services carrying off-spec characteristics.
            serv_filter = self.add_preload_service(
                _SERV_FILTER_MAINTENANCE,
                [
                    _CHAR_NAME,
                    _CHAR_FILTER_CHANGE_INDICATION,
                    _CHAR_FILTER_LIFE_LEVEL,
                ],
            )
            serv_filter.configure_char(_CHAR_NAME, value=filter_name)
            self._char_filter_change = serv_filter.configure_char(
                _CHAR_FILTER_CHANGE_INDICATION, value=_FILTER_OK
            )
            self._char_filter_life = serv_filter.configure_char(
                _CHAR_FILTER_LIFE_LEVEL, value=100
            )
            serv_ap.add_linked_service(serv_filter)

        self.set_primary_service(serv_ap)

    def _resolve_entities(self) -> None:
        registry = er.async_get(self.hass)
        for entry in er.async_entries_for_device(registry, self.device_id):
            eid = entry.entity_id
            if eid.startswith("fan.") and self._fan_entity is None:
                self._fan_entity = eid
            elif eid.startswith("switch.") and eid.endswith("_light"):
                self._light_entity = eid
            elif eid.startswith("sensor.") and eid.endswith("_particulate_matter_10"):
                self._pm10_entity = eid
            elif eid.startswith("sensor.") and eid.endswith("_indoor_air_quality"):
                self._air_quality_entity = eid
            elif eid.startswith("sensor.") and eid.endswith("_lux"):
                self._lux_entity = eid
            elif eid.startswith("sensor.") and eid.endswith("_max2_filter"):
                self._filter_entity = eid

        if not self._fan_entity:
            _LOGGER.warning(
                "%s: no fan.* entity found on device %s",
                self.display_name,
                self.device_id,
            )

    def _watched_entities(self) -> Iterable[str]:
        for eid in (
            self._fan_entity,
            self._light_entity,
            self._pm10_entity,
            self._air_quality_entity,
            self._lux_entity,
            self._filter_entity,
        ):
            if eid:
                yield eid

    def _push_state(self, entity_id: str, state: State | None) -> None:
        if state is None:
            return
        if entity_id == self._fan_entity:
            self._push_fan(state)
        elif entity_id == self._light_entity:
            if self._char_light is not None:
                self._char_light.set_value(1 if state.state == "on" else 0)
        elif entity_id == self._pm10_entity:
            self._push_pm10(state)
        elif entity_id == self._air_quality_entity:
            self._push_air_quality(state)
        elif entity_id == self._lux_entity:
            self._push_lux(state)
        elif entity_id == self._filter_entity:
            self._push_filter(state)

    def _push_fan(self, state: State) -> None:
        on = state.state == "on"
        self._char_active.set_value(1 if on else 0)
        self._char_current_state.set_value(
            _AP_PURIFYING if on else _AP_INACTIVE
        )

        pct = state.attributes.get("percentage")
        if pct is not None:
            try:
                self._char_speed.set_value(max(0, min(100, int(pct))))
            except (ValueError, TypeError):
                pass

        # Coway exposes multiple Auto variants ("Auto", "Auto (Eco)") —
        # both are Auto from HomeKit's point of view. Match by prefix so
        # future variants are captured automatically.
        preset = state.attributes.get("preset_mode") or ""
        self._char_target_state.set_value(
            _AP_TARGET_AUTO if preset.startswith(_AUTO_PRESET) else _AP_TARGET_MANUAL
        )
        if self._char_night is not None:
            self._char_night.set_value(1 if preset == _NIGHT_PRESET else 0)

    def _push_pm10(self, state: State) -> None:
        if self._char_pm10 is None:
            return
        if state.state in ("unknown", "unavailable"):
            return
        try:
            # HAP PM10Density: float, µg/m³, step 1, 0-1000.
            value = max(0.0, min(1000.0, float(state.state)))
        except (ValueError, TypeError):
            return
        self._char_pm10.set_value(round(value, 1))

    def _push_air_quality(self, state: State) -> None:
        if self._char_air_quality is None:
            return
        key = (state.state or "").strip().lower()
        self._char_air_quality.set_value(_AIR_QUALITY_MAP.get(key, 0))

    def _push_lux(self, state: State) -> None:
        if self._char_ambient_light is None:
            return
        if state.state in ("unknown", "unavailable"):
            return
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return
        self._char_ambient_light.set_value(
            max(_LUX_MIN, min(_LUX_MAX, value))
        )

    def _push_filter(self, state: State) -> None:
        # max2_filter is % life remaining (100=new, 0=dead). Fire
        # ChangeFilter when the value drops BELOW threshold.
        if self._char_filter_change is None:
            return
        if state.state in ("unknown", "unavailable"):
            return
        try:
            level = int(float(state.state))
        except (ValueError, TypeError):
            return
        level = max(0, min(100, level))
        if self._char_filter_life is not None:
            self._char_filter_life.set_value(level)
        self._char_filter_change.set_value(
            _FILTER_CHANGE if level < self._filter_change_threshold else _FILTER_OK
        )

    # ---- writes from HomeKit back to HA --------------------------------

    def _handle_active_set(self, value: int) -> None:
        if not self._fan_entity:
            return
        service = "turn_on" if value else "turn_off"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "fan",
                service,
                {"entity_id": self._fan_entity},
                blocking=False,
            )
        )

    def _handle_target_set(self, value: int) -> None:
        """Auto / Manual. Auto -> set preset_mode=Auto. Manual -> clear
        preset (fan service: set_preset_mode with `preset_mode: None` isn't
        supported; instead we set the fan percentage to its current value
        which clears preset in HA)."""
        if not self._fan_entity:
            return
        if value == _AP_TARGET_AUTO:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "fan",
                    "set_preset_mode",
                    {
                        "entity_id": self._fan_entity,
                        "preset_mode": _AUTO_PRESET,
                    },
                    blocking=False,
                )
            )
        else:
            # Manual: set a concrete percentage to exit preset mode.
            state = self.hass.states.get(self._fan_entity)
            pct = 33
            if state is not None:
                cur = state.attributes.get("percentage")
                if isinstance(cur, (int, float)) and cur > 0:
                    pct = int(cur)
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "fan",
                    "set_percentage",
                    {"entity_id": self._fan_entity, "percentage": pct},
                    blocking=False,
                )
            )

    def _handle_speed_set(self, value: int) -> None:
        if not self._fan_entity:
            return
        # Snap to the 4 valid values: 0 (off), 33 (low), 66 (med), 100 (high).
        v = int(value)
        if v <= 16:
            pct = 0
        elif v <= 49:
            pct = 33
        elif v <= 83:
            pct = 66
        else:
            pct = 100

        # HomeKit fires setter_callback for every intermediate slider value.
        # Skip when the snapped target matches the fan's current state —
        # otherwise redundant set_percentage calls would knock the Coway out
        # of Auto or Night preset just by nudging the slider.
        state = self.hass.states.get(self._fan_entity)
        if state is not None:
            is_on = state.state == "on"
            cur_pct = state.attributes.get("percentage")
            if pct == 0 and not is_on:
                return
            if pct > 0 and is_on and cur_pct == pct:
                return

        if pct == 0:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "fan",
                    "turn_off",
                    {"entity_id": self._fan_entity},
                    blocking=False,
                )
            )
            return
        self.hass.async_create_task(
            self.hass.services.async_call(
                "fan",
                "set_percentage",
                {"entity_id": self._fan_entity, "percentage": pct},
                blocking=False,
            )
        )

    def _handle_night_set(self, value: int) -> None:
        if not self._fan_entity:
            return
        if value:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "fan",
                    "set_preset_mode",
                    {
                        "entity_id": self._fan_entity,
                        "preset_mode": _NIGHT_PRESET,
                    },
                    blocking=False,
                )
            )
        else:
            # Exit Night by setting a manual percentage.
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "fan",
                    "set_percentage",
                    {"entity_id": self._fan_entity, "percentage": 33},
                    blocking=False,
                )
            )

    def _handle_light_set(self, value: int) -> None:
        if not self._light_entity:
            return
        service = "turn_on" if value else "turn_off"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "switch",
                service,
                {"entity_id": self._light_entity},
                blocking=False,
            )
        )
