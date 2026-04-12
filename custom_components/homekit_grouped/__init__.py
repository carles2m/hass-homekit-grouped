"""homekit_grouped integration.

Spins up a parallel HomeKit bridge alongside HA's built-in one, exposing
grouped multi-service accessories for devices whose HA entities would
otherwise become many separate tiles in Apple Home.

YAML configuration:

    homekit_grouped:
      bridge:
        port: 21065
        name: "HA Grouped Bridge"
      devices:
        - profile: thinq_washer
          device_id: <ha_device_id>
          name: "Washer"
          category: faucet        # optional: sprinkler | faucet | fan | other | shower_head
          valve_type: faucet      # optional: generic | irrigation | shower | faucet
          finished_event_types:   # event_type values from event.*_notification
            - washing_is_complete # that trigger the "Finished" MotionSensor pulse
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .bridge import GroupedBridge
from .const import (
    CONF_BRIDGE,
    CONF_BRIDGE_NAME,
    CONF_BRIDGE_PORT,
    CONF_CATEGORY,
    CONF_DEVICE_ID,
    CONF_DEVICES,
    CONF_FINISHED_EVENT_TYPES,
    CONF_NAME,
    CONF_PROFILE,
    CONF_TILE_SERVICE,
    CONF_VALVE_TYPE,
    DEFAULT_BRIDGE_NAME,
    DEFAULT_PORT,
    DOMAIN,
)
from .profiles import PROFILES

_LOGGER = logging.getLogger(__name__)

_CATEGORY_VALUES = [
    "sprinkler",
    "faucet",
    "fan",
    "other",
    "shower_head",
    "door",
    "sensor",
    "window",
]
_VALVE_TYPE_VALUES = ["generic", "irrigation", "shower", "faucet"]

_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PROFILE): vol.In(list(PROFILES)),
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_CATEGORY): vol.In(_CATEGORY_VALUES),
        vol.Optional(CONF_VALVE_TYPE): vol.In(_VALVE_TYPE_VALUES),
        vol.Optional(CONF_FINISHED_EVENT_TYPES): vol.All(
            cv.ensure_list, [cv.string]
        ),
        vol.Optional(CONF_TILE_SERVICE): vol.In(["fan", "garage_door"]),
    }
)

_BRIDGE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_BRIDGE_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_BRIDGE_NAME, default=DEFAULT_BRIDGE_NAME): cv.string,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_BRIDGE, default={}): _BRIDGE_SCHEMA,
                vol.Required(CONF_DEVICES): vol.All(cv.ensure_list, [_DEVICE_SCHEMA]),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the homekit_grouped integration."""
    conf = config.get(DOMAIN)
    if not conf:
        return True

    bridge_conf = conf.get(CONF_BRIDGE) or {}
    port = bridge_conf.get(CONF_BRIDGE_PORT, DEFAULT_PORT)
    name = bridge_conf.get(CONF_BRIDGE_NAME, DEFAULT_BRIDGE_NAME)

    bridge = GroupedBridge(
        hass=hass,
        port=port,
        name=name,
        device_configs=conf[CONF_DEVICES],
    )

    hass.data.setdefault(DOMAIN, {})["bridge"] = bridge

    async def _on_started(_event):
        await bridge.async_start()

    hass.bus.async_listen_once("homeassistant_started", _on_started)

    async def _on_stop(_event):
        await bridge.async_stop()

    hass.bus.async_listen_once("homeassistant_stop", _on_stop)

    return True
