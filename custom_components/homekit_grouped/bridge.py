"""HomeKit bridge lifecycle for homekit_grouped.

Runs a pyhap AccessoryDriver inside HA's event loop, on a dedicated port,
with persistent pairing state stored in the HA config directory.

Uses HA's shared Zeroconf instance. All pyhap setup that does synchronous
filesystem I/O (resource loads, pairing state read) runs in an executor
so the event loop stays responsive during startup.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from homeassistant.components.zeroconf import async_get_instance
from homeassistant.core import HomeAssistant
from pyhap.accessory import Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_OTHER

from .const import (
    CONF_ALERT_SENSOR,
    CONF_AMBIENT_LIGHT_SENSOR,
    CONF_CATEGORY,
    CONF_DEVICE_ID,
    CONF_FINISHED_EVENT_TYPES,
    CONF_HOT_WATER_LOW_THRESHOLD,
    CONF_LIGHT,
    CONF_NAME,
    CONF_NIGHT_MODE_SWITCH,
    CONF_NO_HOT_WATER_SENSOR,
    CONF_PROFILE,
    CONF_TILE_SERVICE,
    CONF_VALVE_TYPE,
    PAIRING_FILE,
)
from .profiles import get_profile

_LOGGER = logging.getLogger(__name__)


class GroupedBridge:
    """Manages a parallel HomeKit bridge for grouped accessories."""

    def __init__(
        self,
        hass: HomeAssistant,
        port: int,
        name: str,
        device_configs: list[dict[str, Any]],
    ) -> None:
        self.hass = hass
        self.port = port
        self.name = name
        self.device_configs = device_configs
        self._driver: AccessoryDriver | None = None
        self._accessories: list = []

    @property
    def _state_path(self) -> str:
        return os.path.join(self.hass.config.path(), PAIRING_FILE)

    def _build_driver_and_bridge(self, zeroconf) -> AccessoryDriver:
        """Construct the AccessoryDriver, build the Bridge, attach accessories.

        Runs fully in an executor because pyhap's init reads JSON resource
        files and pairing state synchronously.
        """
        driver = AccessoryDriver(
            port=self.port,
            persist_file=self._state_path,
            loop=self.hass.loop,
            async_zeroconf_instance=zeroconf,
        )

        bridge = Bridge(driver, self.name)
        bridge.category = CATEGORY_OTHER

        for cfg in self.device_configs:
            profile_cls = get_profile(cfg[CONF_PROFILE])
            accessory = profile_cls(
                driver=driver,
                hass=self.hass,
                name=cfg[CONF_NAME],
                device_id=cfg[CONF_DEVICE_ID],
                overrides={
                    "category": cfg.get(CONF_CATEGORY),
                    "valve_type": cfg.get(CONF_VALVE_TYPE),
                    "finished_event_types": cfg.get(CONF_FINISHED_EVENT_TYPES),
                    "tile_service": cfg.get(CONF_TILE_SERVICE),
                    "hot_water_low_threshold": cfg.get(
                        CONF_HOT_WATER_LOW_THRESHOLD
                    ),
                    "alert_sensor": cfg.get(CONF_ALERT_SENSOR),
                    "no_hot_water_sensor": cfg.get(CONF_NO_HOT_WATER_SENSOR),
                    "night_mode_switch": cfg.get(CONF_NIGHT_MODE_SWITCH),
                    "light": cfg.get(CONF_LIGHT),
                    "ambient_light_sensor": cfg.get(CONF_AMBIENT_LIGHT_SENSOR),
                },
            )
            bridge.add_accessory(accessory)
            self._accessories.append(accessory)
            _LOGGER.info(
                "Registered grouped accessory %r (profile=%s, device=%s)",
                cfg[CONF_NAME],
                cfg[CONF_PROFILE],
                cfg[CONF_DEVICE_ID],
            )

        driver.add_accessory(accessory=bridge)
        return driver

    async def async_start(self) -> None:
        """Start the pyhap bridge. Must be called after HA is fully started."""
        _LOGGER.info(
            "Starting HomeKit grouped bridge %r on port %d with %d devices",
            self.name,
            self.port,
            len(self.device_configs),
        )

        zeroconf = await async_get_instance(self.hass)
        self._driver = await self.hass.async_add_executor_job(
            self._build_driver_and_bridge, zeroconf
        )

        for accessory in self._accessories:
            await accessory.async_wire_state_listeners()

        pin = self._driver.state.pincode.decode()
        _LOGGER.warning(
            "HomeKit Grouped Bridge ready. Add to Apple Home with PIN %s "
            "(setup id: %s)",
            pin,
            self._driver.state.setup_id,
        )

        await self._driver.async_start()

    async def async_stop(self) -> None:
        """Stop the pyhap bridge cleanly."""
        if self._driver is None:
            return
        _LOGGER.info("Stopping HomeKit grouped bridge %r", self.name)
        await self._driver.async_stop()
        self._driver = None
