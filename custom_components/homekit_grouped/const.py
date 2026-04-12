"""Constants for homekit_grouped."""

DOMAIN = "homekit_grouped"

CONF_BRIDGE = "bridge"
CONF_BRIDGE_PORT = "port"
CONF_BRIDGE_NAME = "name"
CONF_DEVICES = "devices"
CONF_DEVICE_ID = "device_id"
CONF_PROFILE = "profile"
CONF_NAME = "name"

DEFAULT_PORT = 21065
DEFAULT_BRIDGE_NAME = "HA Grouped Bridge"

# Where pairing state lives, inside the HA config dir so it persists
PAIRING_FILE = "homekit_grouped.state"
