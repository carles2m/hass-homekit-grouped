"""Konnected GDO blaQ profile.

Grouped accessory for a Konnected GDO blaQ (GDOv2-Q) garage door
controller running Konnected's ESPHome firmware. HA's built-in HomeKit
Bridge would turn this device into separate tiles — and, because the
firmware's cover carries no device_class, the door tile would be a
window shade rather than a garage door.

Services exposed:
  - GarageDoorOpener: the door (PRIMARY)
      * ObstructionDetected mirrors the opener's safety-beam sensor
      * LockCurrentState / LockTargetState carry the opener's remote
        lockout. HAP lists them as optional characteristics OF the
        garage service, but Apple Home does NOT render them (verified
        2026-10-07 on a paired bridge: door, light and motion showed up,
        the lock did not). They are kept because they're correct per
        spec and other controllers may use them — the lockout that
        Apple Home actually shows is the Switch below.
  - Lightbulb:   the opener's light
  - MotionSensor: the opener's motion detector
  - Switch:      the opener's remote lockout (what Apple Home renders)

Each service is created only if the matching entity exists on the
device, so a trimmed-down firmware build doesn't get dead services.

Also works for an esphome-ratgdo board if its entity names match the
suffixes in _resolve_entities.
"""

from __future__ import annotations

import logging
from typing import Iterable

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pyhap.const import CATEGORY_GARAGE_DOOR_OPENER

from .base import GroupedAccessory

_LOGGER = logging.getLogger(__name__)

_SERV_GARAGE = "GarageDoorOpener"
_SERV_LIGHTBULB = "Lightbulb"
_SERV_MOTION = "MotionSensor"
_SERV_SWITCH = "Switch"

_CHAR_CURRENT_DOOR_STATE = "CurrentDoorState"
_CHAR_TARGET_DOOR_STATE = "TargetDoorState"
_CHAR_OBSTRUCTION_DETECTED = "ObstructionDetected"
_CHAR_LOCK_CURRENT_STATE = "LockCurrentState"
_CHAR_LOCK_TARGET_STATE = "LockTargetState"
_CHAR_ON = "On"
_CHAR_MOTION_DETECTED = "MotionDetected"
_CHAR_NAME = "Name"
_CHAR_CONFIGURED_NAME = "ConfiguredName"

# CurrentDoorState: 0 Open, 1 Closed, 2 Opening, 3 Closing, 4 Stopped
_DOOR_OPEN = 0
_DOOR_CLOSED = 1
_DOOR_OPENING = 2
_DOOR_CLOSING = 3
_DOOR_STOPPED = 4

_CURRENT_DOOR_STATE = {
    "open": _DOOR_OPEN,
    "closed": _DOOR_CLOSED,
    "opening": _DOOR_OPENING,
    "closing": _DOOR_CLOSING,
}
# TargetDoorState: 0 Open, 1 Closed. A door that is moving reports the
# state it is heading for, so Apple Home's control doesn't fight the door.
_TARGET_DOOR_STATE = {
    "open": _DOOR_OPEN,
    "opening": _DOOR_OPEN,
    "closed": _DOOR_CLOSED,
    "closing": _DOOR_CLOSED,
}

# LockCurrentState: 0 Unsecured, 1 Secured, 2 Jammed, 3 Unknown
_LOCK_UNSECURED = 0
_LOCK_SECURED = 1
_LOCK_UNKNOWN = 3

_UNAVAILABLE = frozenset({"unknown", "unavailable"})


class KonnectedGdoAccessory(GroupedAccessory):
    """HAP accessory for a Konnected GDO blaQ device."""

    category = CATEGORY_GARAGE_DOOR_OPENER

    def _setup_services(self) -> None:
        self._door_entity: str | None = None
        self._obstruction_entity: str | None = None
        self._lock_entity: str | None = None
        self._light_entity: str | None = None
        self._motion_entity: str | None = None
        self._resolve_entities()

        # --- GarageDoorOpener: the door itself ------------------------------
        self._char_door_current = None
        self._char_door_target = None
        self._char_obstruction = None
        self._char_lock_current = None
        self._char_lock_target = None
        serv_garage = None
        if self._door_entity:
            serv_garage = self._add_garage_door()

        # --- Lightbulb: the opener's light ----------------------------------
        self._char_light_on = None
        if self._light_entity:
            self._char_light_on = self._add_light(
                f"{self.display_name} Light"
            )

        # --- MotionSensor: the opener's motion detector ---------------------
        self._char_motion = None
        if self._motion_entity:
            self._char_motion = self._add_motion(
                f"{self.display_name} Motion"
            )

        # --- Switch: the remote lockout -------------------------------------
        # Appended LAST: this accessory is already paired, and pyhap assigns
        # IIDs in order of addition, so anything inserted earlier would shift
        # every later IID and risk Apple Home's schema cache.
        self._char_lockout_on = None
        if self._lock_entity:
            self._char_lockout_on = self._add_lockout_switch(
                f"{self.display_name} Remote Lockout"
            )

        # Apple Home only renders linked sub-services when the parent is
        # flagged HAP-primary — category alone is not enough.
        if serv_garage is not None:
            self.set_primary_service(serv_garage)

    # ---- service builders ----------------------------------------------

    def _add_garage_door(self):
        """The door, plus obstruction and (if present) the remote lockout.

        ConfiguredName is deliberately absent: HAP does not list it for
        GarageDoorOpener, and iOS silently drops a service that carries a
        characteristic outside its allowed set.
        """
        chars = [
            _CHAR_CURRENT_DOOR_STATE,
            _CHAR_TARGET_DOOR_STATE,
            _CHAR_OBSTRUCTION_DETECTED,
            _CHAR_NAME,
        ]
        if self._lock_entity:
            chars += [_CHAR_LOCK_CURRENT_STATE, _CHAR_LOCK_TARGET_STATE]

        serv = self.add_preload_service(_SERV_GARAGE, chars)
        self._char_door_current = serv.configure_char(
            _CHAR_CURRENT_DOOR_STATE, value=_DOOR_CLOSED
        )
        self._char_door_target = serv.configure_char(
            _CHAR_TARGET_DOOR_STATE, value=_DOOR_CLOSED
        )
        self._char_door_target.setter_callback = self._handle_door_set
        self._char_obstruction = serv.configure_char(
            _CHAR_OBSTRUCTION_DETECTED, value=False
        )
        if self._lock_entity:
            self._char_lock_current = serv.configure_char(
                _CHAR_LOCK_CURRENT_STATE, value=_LOCK_UNKNOWN
            )
            self._char_lock_target = serv.configure_char(
                _CHAR_LOCK_TARGET_STATE, value=_LOCK_UNSECURED
            )
            self._char_lock_target.setter_callback = self._handle_lock_set
        serv.configure_char(_CHAR_NAME, value=self.display_name)
        return serv

    def _add_light(self, name: str):
        serv = self.add_preload_service(
            _SERV_LIGHTBULB, [_CHAR_ON, _CHAR_NAME, _CHAR_CONFIGURED_NAME]
        )
        char = serv.configure_char(_CHAR_ON, value=False)
        char.setter_callback = self._handle_light_set
        serv.configure_char(_CHAR_NAME, value=name)
        serv.configure_char(_CHAR_CONFIGURED_NAME, value=name)
        return char

    def _add_lockout_switch(self, name: str):
        """The opener's remote lockout as a plain Switch. On = locked out.

        A LockMechanism would read as a deadbolt on the door, which this is
        not — it only disables the RF remotes — and Apple Home would make it
        an authenticated control. A Switch is also a service this repo has
        proven renders as a sub-service.
        """
        serv = self.add_preload_service(
            _SERV_SWITCH, [_CHAR_ON, _CHAR_NAME, _CHAR_CONFIGURED_NAME]
        )
        char = serv.configure_char(_CHAR_ON, value=False)
        char.setter_callback = self._handle_lockout_set
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

    # ---- entity discovery ----------------------------------------------

    def _resolve_entities(self) -> None:
        registry = er.async_get(self.hass)
        for entry in er.async_entries_for_device(registry, self.device_id):
            eid = entry.entity_id
            if eid.startswith("cover.") and eid.endswith("_garage_door"):
                self._door_entity = eid
            elif eid.startswith("binary_sensor.") and eid.endswith("_obstruction"):
                self._obstruction_entity = eid
            elif eid.startswith("lock."):
                self._lock_entity = eid
            elif eid.startswith("light.") and eid.endswith("_garage_light"):
                self._light_entity = eid
            elif eid.startswith("binary_sensor.") and eid.endswith("_motion"):
                self._motion_entity = eid

        if self._door_entity is None:
            _LOGGER.error(
                "%s: device %s has no cover.*_garage_door entity — the "
                "accessory will have no door service",
                self.display_name,
                self.device_id,
            )
        missing = [
            name
            for name, value in [
                ("obstruction", self._obstruction_entity),
                ("lock", self._lock_entity),
                ("light", self._light_entity),
                ("motion", self._motion_entity),
            ]
            if value is None
        ]
        if missing:
            _LOGGER.warning(
                "%s: device %s missing optional GDO entities: %s",
                self.display_name,
                self.device_id,
                missing,
            )

    def _watched_entities(self) -> Iterable[str]:
        for eid in (
            self._door_entity,
            self._obstruction_entity,
            self._lock_entity,
            self._light_entity,
            self._motion_entity,
        ):
            if eid:
                yield eid

    # ---- HA -> HomeKit --------------------------------------------------

    def _push_state(self, entity_id: str, state: State | None) -> None:
        if state is None:
            return

        if entity_id == self._door_entity:
            self._push_door(state.state)
        elif entity_id == self._obstruction_entity:
            if self._char_obstruction is not None:
                self._char_obstruction.set_value(state.state == "on")
        elif entity_id == self._lock_entity:
            self._push_lock(state.state)
        elif entity_id == self._light_entity:
            if self._char_light_on is not None and state.state not in _UNAVAILABLE:
                self._char_light_on.set_value(state.state == "on")
        elif entity_id == self._motion_entity:
            if self._char_motion is not None:
                self._char_motion.set_value(state.state == "on")

    def _push_door(self, source_state: str) -> None:
        if self._char_door_current is None or self._char_door_target is None:
            return
        if source_state in _UNAVAILABLE:
            return
        self._char_door_current.set_value(
            _CURRENT_DOOR_STATE.get(source_state, _DOOR_STOPPED)
        )
        # Leave the target alone for a state we can't read as a destination,
        # so Apple Home keeps showing what was last asked for.
        if (target := _TARGET_DOOR_STATE.get(source_state)) is not None:
            self._char_door_target.set_value(target)

    def _push_lock(self, source_state: str) -> None:
        if self._char_lockout_on is not None and source_state in (
            "locked",
            "unlocked",
        ):
            self._char_lockout_on.set_value(source_state == "locked")
        if self._char_lock_current is None or self._char_lock_target is None:
            return
        if source_state == "locked":
            self._char_lock_current.set_value(_LOCK_SECURED)
            self._char_lock_target.set_value(_LOCK_SECURED)
        elif source_state == "unlocked":
            self._char_lock_current.set_value(_LOCK_UNSECURED)
            self._char_lock_target.set_value(_LOCK_UNSECURED)
        else:
            # locking / unlocking / jammed / unavailable: report Unknown and
            # keep the target as asked until the lock settles.
            self._char_lock_current.set_value(_LOCK_UNKNOWN)

    # ---- HomeKit -> HA --------------------------------------------------

    def _handle_door_set(self, value: int) -> None:
        if not self._door_entity:
            return
        service = "close_cover" if value == _DOOR_CLOSED else "open_cover"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "cover",
                service,
                {"entity_id": self._door_entity},
                blocking=False,
            )
        )

    def _handle_lock_set(self, value: int) -> None:
        self._call_lock("lock" if value == _LOCK_SECURED else "unlock")

    def _call_lock(self, service: str) -> None:
        if not self._lock_entity:
            return
        self.hass.async_create_task(
            self.hass.services.async_call(
                "lock",
                service,
                {"entity_id": self._lock_entity},
                blocking=False,
            )
        )

    def _handle_lockout_set(self, value: int) -> None:
        self._call_lock("lock" if value else "unlock")

    def _handle_light_set(self, value: int) -> None:
        if not self._light_entity:
            return
        service = "turn_on" if value else "turn_off"
        self.hass.async_create_task(
            self.hass.services.async_call(
                "light",
                service,
                {"entity_id": self._light_entity},
                blocking=False,
            )
        )
