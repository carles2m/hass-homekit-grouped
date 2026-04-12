"""Base class for grouped HomeKit accessories."""

from __future__ import annotations

import hashlib
import logging
from abc import abstractmethod
from typing import Iterable

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event
from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_OTHER

_LOGGER = logging.getLogger(__name__)


def _stable_aid(device_id: str) -> int:
    """Derive a HomeKit AID from a device_id that is stable across HA
    restarts. Python's built-in hash() is randomized per process, so using
    it for AID means Apple Home sees a new accessory after every restart
    and forgets room/type/notification customizations.

    HomeKit AIDs are 1..2^32-1. AID 1 is reserved for the bridge itself;
    we keep accessories in 2..2^31-1 to avoid signed-int surprises.
    """
    digest = hashlib.sha256(device_id.encode("utf-8")).digest()
    aid = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    return aid if aid > 1 else 2


class GroupedAccessory(Accessory):
    """Base class: a single HAP accessory composed of multiple services
    driven by multiple HA entities from the same HA device."""

    category = CATEGORY_OTHER

    def __init__(
        self,
        driver,
        hass: HomeAssistant,
        name: str,
        device_id: str,
        overrides: dict | None = None,
    ) -> None:
        # AID derived from a stable SHA-256 of device_id so Apple Home's
        # per-accessory customizations (room, type, notifications) survive
        # HA restarts.
        aid = _stable_aid(device_id)
        super().__init__(driver=driver, display_name=name, aid=aid)
        self.hass = hass
        self.device_id = device_id
        self.overrides = overrides or {}
        self._setup_services()

    # ---- profile hooks --------------------------------------------------

    @abstractmethod
    def _setup_services(self) -> None:
        """Create HomeKit services and their characteristics.

        Called once during __init__. Subclasses should add_preload_service()
        and wire initial characteristics. Do NOT subscribe to HA state here;
        entities may not be loaded yet.
        """

    @abstractmethod
    def _watched_entities(self) -> Iterable[str]:
        """Return the HA entity_ids this accessory reflects."""

    @abstractmethod
    def _push_state(self, entity_id: str, state: State | None) -> None:
        """Copy the given HA state into the appropriate HAP characteristic(s)."""

    # ---- lifecycle ------------------------------------------------------

    async def async_wire_state_listeners(self) -> None:
        """Subscribe to HA state changes and push initial state."""
        entities = list(self._watched_entities())
        _LOGGER.debug(
            "Accessory %r watching %d entities: %s",
            self.display_name,
            len(entities),
            entities,
        )

        # Prime current state.
        for eid in entities:
            self._push_state(eid, self.hass.states.get(eid))

        @callback
        def _state_changed(event):
            eid = event.data.get("entity_id")
            new_state = event.data.get("new_state")
            try:
                self._push_state(eid, new_state)
            except Exception:
                _LOGGER.exception(
                    "Accessory %r failed processing state for %s",
                    self.display_name,
                    eid,
                )

        async_track_state_change_event(self.hass, entities, _state_changed)
