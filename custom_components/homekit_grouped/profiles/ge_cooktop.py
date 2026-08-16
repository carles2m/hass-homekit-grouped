"""GE / Monogram gas cooktop profile (SmartHQ / ge_home).

Grouped HomeKit accessory for a GE-branded gas cooktop exposed through
the third-party `ge_home` (gehomesdk) HA integration. The cooktop
itself is unactionable from HomeKit — burners are physical knobs and
the lock cannot be unlocked from third-party clients (Brillion enforces
writability per OAuth client_id; gehomesdk is not whitelisted) — but
the kitchen timer is fully writable end-to-end, so it carries the
primary Valve service and gives the tile a useful action.

Services:
  - Valve (primary, "Kitchen Timer") — SetDuration writes to the
    underlying `number.*_kitchen_timer` entity (HA service
    number.set_value, value in minutes, 0-599 to match the device's
    own ceiling). RemainingDuration mirrors the entity * 60 seconds,
    capped at HAP's 1-hour limit. Active/InUse derived from
    "kitchen_timer > 0". Active writes are reverted (Apple Home may
    still render a toggle); the only working "stop" is SetDuration=0,
    which we forward as a write of 0.
  - MotionSensor "Cooktop" (linked) — sustained MotionDetected while
    any burner is on, driven by the `binary_sensor.*_cooktop_on`
    aggregate. Powers iOS "<Name> Cooktop on" notifications and
    "burners left on for too long" automations.
  - MotionSensor "Timer Alarm" (linked) — 60s motion pulse on the
    rising edge of `binary_sensor.*_kitchen_timer_alarm`, mirroring
    the washer "Finished" pulse. One iOS push per timer expiry.
  - ContactSensor "Lock" (linked) — closed=locked, open=unlocked,
    driven by the `binary_sensor.*_locked` device_class=lock entity.
    Read-only by HomeKit construction (ContactSensor has no writable
    char). User can `Display As: Lock` to get a lock icon. Writes
    aren't possible, which matches Brillion's policy and keeps Apple
    Home from offering an "Unlock" action that would fail.

`sensor.*_cooking_minutes` (cumulative cook-time counter) is
deliberately not exposed — it's a TOTAL_INCREASING diagnostic with no
clean HomeKit shape and lives fine as an HA-side sensor.
"""

from __future__ import annotations

import logging
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

_SERV_VALVE = "Valve"
_SERV_MOTION = "MotionSensor"
_SERV_CONTACT = "ContactSensor"

_CHAR_ACTIVE = "Active"
_CHAR_IN_USE = "InUse"
_CHAR_VALVE_TYPE = "ValveType"
_CHAR_REMAINING_DURATION = "RemainingDuration"
_CHAR_SET_DURATION = "SetDuration"
_CHAR_MOTION_DETECTED = "MotionDetected"
_CHAR_CONTACT_SENSOR_STATE = "ContactSensorState"
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
_DEFAULT_CATEGORY_NAME = "faucet"
_DEFAULT_VALVE_TYPE_NAME = "irrigation"

# HAP RemainingDuration / SetDuration cap, in seconds (Apple's UI tops
# out at 1 h regardless of what we report — clamping here keeps the
# slider behavior coherent).
_DURATION_MAX = 3600

# Device-side max kitchen timer minutes. SmartHQ exposes 10:59 in its
# UI but the cooktop rejects writes > 599. ge_home reports max=599.0
# in the entity attributes, kept here as a fallback.
_KITCHEN_TIMER_MAX_MIN = 599

# Length of the "Timer Alarm" motion pulse. The cooktop's alarm beeps
# for ~30 s on its own; 60 s gives Apple Home a comfortable window to
# emit the iOS push without re-triggering on any flap.
_MOTION_PULSE_SECONDS = 60

_PERM_READ = "pr"
_PERM_NOTIFY = "ev"

_LOCK_OPEN = 1  # ContactSensorState: contact NOT detected (unlocked)
_LOCK_CLOSED = 0  # ContactSensorState: contact detected (locked)


class GeCooktopAccessory(GroupedAccessory):
    """HAP accessory for a GE / Monogram gas cooktop via ge_home."""

    def _setup_services(self) -> None:
        self._timer_entity: str | None = None
        self._cooktop_on_entity: str | None = None
        self._alarm_entity: str | None = None
        self._lock_entity: str | None = None

        self._alarm_last_state: str | None = None
        self._motion_reset_cancel = None

        self._resolve_entities()

        cat_name = self.overrides.get("category") or _DEFAULT_CATEGORY_NAME
        self.category = _CATEGORY_MAP[cat_name]
        valve_type_name = (
            self.overrides.get("valve_type") or _DEFAULT_VALVE_TYPE_NAME
        )
        valve_type_value = _VALVE_TYPE_MAP[valve_type_name]

        # --- Valve (primary, kitchen timer) ---------------------------------
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
        self._char_active.setter_callback = self._on_active_write
        self._char_in_use = serv_valve.configure_char(_CHAR_IN_USE, value=0)
        serv_valve.configure_char(_CHAR_VALVE_TYPE, value=valve_type_value)
        self._char_remaining = serv_valve.configure_char(
            _CHAR_REMAINING_DURATION, value=0
        )
        # pyhap defaults SetDuration to minStep=1 (second). Apple
        # Home's irrigation valve slider widget is hardcoded to 5/10-
        # minute preset increments and ignores minStep regardless —
        # this is an Apple-side UX limitation that hits every HomeKit
        # irrigation valve, confirmed by the homebridge community.
        # We still set minStep=60 because it's respected on the write
        # path: Siri ("set the Cooktop timer to 7 minutes") and
        # HomeKit automations/Shortcuts can write arbitrary 1-min-
        # resolution values, and Apple Home displays them correctly.
        self._char_set_duration = serv_valve.configure_char(
            _CHAR_SET_DURATION,
            value=0,
            properties={"minStep": 60},
        )
        self._char_set_duration.setter_callback = self._on_set_duration
        timer_label = f"{self.display_name} Kitchen Timer"
        serv_valve.configure_char(_CHAR_NAME, value=timer_label)
        serv_valve.configure_char(_CHAR_CONFIGURED_NAME, value=timer_label)

        # --- MotionSensor "Cooktop" (sustained) -----------------------------
        cooktop_label = f"{self.display_name} Cooktop"
        serv_cooktop_motion = self.add_preload_service(
            _SERV_MOTION,
            [_CHAR_MOTION_DETECTED, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
        )
        self._char_cooktop_motion = serv_cooktop_motion.configure_char(
            _CHAR_MOTION_DETECTED, value=False
        )
        serv_cooktop_motion.configure_char(_CHAR_NAME, value=cooktop_label)
        serv_cooktop_motion.configure_char(
            _CHAR_CONFIGURED_NAME, value=cooktop_label
        )
        serv_valve.add_linked_service(serv_cooktop_motion)

        # --- MotionSensor "Timer Alarm" (pulse) -----------------------------
        alarm_label = f"{self.display_name} Timer Alarm"
        serv_alarm_motion = self.add_preload_service(
            _SERV_MOTION,
            [_CHAR_MOTION_DETECTED, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
        )
        self._char_alarm_motion = serv_alarm_motion.configure_char(
            _CHAR_MOTION_DETECTED, value=False
        )
        serv_alarm_motion.configure_char(_CHAR_NAME, value=alarm_label)
        serv_alarm_motion.configure_char(
            _CHAR_CONFIGURED_NAME, value=alarm_label
        )
        serv_valve.add_linked_service(serv_alarm_motion)

        # --- ContactSensor "Lock" (read-only) -------------------------------
        lock_label = f"{self.display_name} Lock"
        serv_lock = self.add_preload_service(
            _SERV_CONTACT,
            [_CHAR_CONTACT_SENSOR_STATE, _CHAR_NAME, _CHAR_CONFIGURED_NAME],
        )
        self._char_lock = serv_lock.configure_char(
            _CHAR_CONTACT_SENSOR_STATE, value=_LOCK_OPEN
        )
        serv_lock.configure_char(_CHAR_NAME, value=lock_label)
        serv_lock.configure_char(_CHAR_CONFIGURED_NAME, value=lock_label)
        serv_valve.add_linked_service(serv_lock)

        # Required for sub-services like FilterMaintenance to render —
        # also good hygiene for any future strict service we add.
        self.set_primary_service(serv_valve)

    def _resolve_entities(self) -> None:
        # Explicit config overrides win; the device scan below fills in the
        # rest. Needed because async_entries_for_device only returns entities
        # attached to the HA device, and some of a cooktop's entities come
        # from a helper integration whose platform has no config entry — HA
        # ignores device_info for those, so they are never attached.
        #
        # Deliberately NOT a registry-wide suffix search: ovens expose
        # identically-suffixed `_kitchen_timer` numbers, so a global match
        # could silently bind this accessory to the wrong appliance.
        overrides = self.overrides.get("entities") or {}
        self._timer_entity = overrides.get("kitchen_timer")
        self._cooktop_on_entity = overrides.get("cooktop_on")
        self._alarm_entity = overrides.get("kitchen_timer_alarm")
        self._lock_entity = overrides.get("locked")

        registry = er.async_get(self.hass)
        for entry in er.async_entries_for_device(registry, self.device_id):
            eid = entry.entity_id
            if (
                self._timer_entity is None
                and eid.startswith("number.")
                and eid.endswith("_kitchen_timer")
            ):
                self._timer_entity = eid
            elif (
                self._cooktop_on_entity is None
                and eid.startswith("binary_sensor.")
                and eid.endswith("_cooktop_on")
            ):
                self._cooktop_on_entity = eid
            elif (
                self._alarm_entity is None
                and eid.startswith("binary_sensor.")
                and eid.endswith("_kitchen_timer_alarm")
            ):
                self._alarm_entity = eid
            elif (
                self._lock_entity is None
                and eid.startswith("binary_sensor.")
                and eid.endswith("_locked")
            ):
                self._lock_entity = eid

        # An override naming an entity that does not exist is a config error,
        # not a discovery miss — say so distinctly, because the generic
        # "missing … entity" warning below would send you hunting the device.
        for label, eid in (
            ("kitchen_timer", self._timer_entity),
            ("cooktop_on", self._cooktop_on_entity),
            ("kitchen_timer_alarm", self._alarm_entity),
            ("locked", self._lock_entity),
        ):
            if (
                label in overrides
                and eid is not None
                and self.hass.states.get(eid) is None
            ):
                _LOGGER.warning(
                    "%s: configured %s override %s does not exist",
                    self.display_name,
                    label,
                    eid,
                )

        for label, eid in (
            ("kitchen_timer", self._timer_entity),
            ("cooktop_on", self._cooktop_on_entity),
            ("kitchen_timer_alarm", self._alarm_entity),
            ("locked", self._lock_entity),
        ):
            if eid is None:
                _LOGGER.warning(
                    "%s: device %s missing %s entity — corresponding "
                    "service will stay at its default value",
                    self.display_name,
                    self.device_id,
                    label,
                )

    def _watched_entities(self) -> Iterable[str]:
        for eid in (
            self._timer_entity,
            self._cooktop_on_entity,
            self._alarm_entity,
            self._lock_entity,
        ):
            if eid:
                yield eid

    def _push_state(self, entity_id: str, state: State | None) -> None:
        if state is None:
            return

        if entity_id == self._timer_entity:
            minutes = self._parse_minutes(state.state)
            seconds = min(max(minutes * 60, 0), _DURATION_MAX)
            running = 1 if minutes > 0 else 0
            self._char_active.set_value(running)
            self._char_in_use.set_value(running)
            self._char_remaining.set_value(seconds)
            # Mirror SetDuration so Apple Home's slider follows the
            # device when the user uses the cooktop's own buttons.
            self._char_set_duration.set_value(seconds)

        elif entity_id == self._cooktop_on_entity:
            on = state.state == "on"
            self._char_cooktop_motion.set_value(bool(on))

        elif entity_id == self._alarm_entity:
            current = state.state
            prev = self._alarm_last_state
            self._alarm_last_state = current
            # Initial priming during HA restart / bridge startup: just
            # remember the current state. Only a subsequent off→on
            # transition should fire the pulse.
            if prev is None:
                return
            if prev != "on" and current == "on":
                self._fire_alarm_pulse()

        elif entity_id == self._lock_entity:
            # device_class=lock: state="on" means LOCKED.
            locked = state.state == "on"
            self._char_lock.set_value(_LOCK_CLOSED if locked else _LOCK_OPEN)

    # ---- writes ---------------------------------------------------------

    def _on_set_duration(self, requested_value: int) -> None:
        """Forward HomeKit SetDuration writes to the kitchen_timer
        number entity. requested_value is seconds; the device wants
        minutes."""
        if not self._timer_entity:
            return
        try:
            seconds = int(requested_value)
        except (ValueError, TypeError):
            return
        # Round to nearest minute, then clamp to the device's own
        # ceiling. The HAP slider granularity is per-second but the
        # device only thinks in minutes — round-half-up keeps a 30 s
        # tap from silently truncating to 0.
        minutes = max(0, min((seconds + 30) // 60, _KITCHEN_TIMER_MAX_MIN))
        _LOGGER.info(
            "%s: SetDuration %ds → number.set_value %d min on %s",
            self.display_name,
            seconds,
            minutes,
            self._timer_entity,
        )
        self.hass.async_create_task(
            self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": self._timer_entity, "value": minutes},
                blocking=False,
            )
        )

    def _on_active_write(self, requested_value: int) -> None:
        """Apple Home renders Valve Active as a toggle. Writing 0 is
        useful — it stops the timer. Writing 1 with no SetDuration is
        meaningless on this device, so we ignore it and let the next
        state push restore the real Active value."""
        if not self._timer_entity:
            return
        try:
            requested = int(requested_value)
        except (ValueError, TypeError):
            return

        if requested == 0:
            _LOGGER.info(
                "%s: Active=0 from HomeKit → number.set_value 0 on %s",
                self.display_name,
                self._timer_entity,
            )
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "number",
                    "set_value",
                    {"entity_id": self._timer_entity, "value": 0},
                    blocking=False,
                )
            )
            return

        # Active=1 without a duration — snap back to whatever HA
        # currently reports. Mirrors the washer revert pattern.
        real = self.hass.states.get(self._timer_entity)
        minutes = self._parse_minutes(real.state) if real else 0
        running = 1 if minutes > 0 else 0
        self._char_active.set_value(running)
        self._char_in_use.set_value(running)

    # ---- helpers --------------------------------------------------------

    def _fire_alarm_pulse(self) -> None:
        if self._motion_reset_cancel is not None:
            try:
                self._motion_reset_cancel()
            except Exception:  # noqa: BLE001
                pass
            self._motion_reset_cancel = None

        _LOGGER.info(
            "%s: kitchen timer alarm started — firing motion pulse",
            self.display_name,
        )
        self._char_alarm_motion.set_value(True)

        def _reset(_now):
            self._char_alarm_motion.set_value(False)
            self._motion_reset_cancel = None

        self._motion_reset_cancel = async_call_later(
            self.hass, _MOTION_PULSE_SECONDS, _reset
        )

    @staticmethod
    def _parse_minutes(raw: str | None) -> int:
        if raw is None or raw in ("unknown", "unavailable"):
            return 0
        try:
            return max(0, int(round(float(raw))))
        except (ValueError, TypeError):
            return 0
