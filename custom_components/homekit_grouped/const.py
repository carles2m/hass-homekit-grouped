"""Constants for homekit_grouped."""

DOMAIN = "homekit_grouped"

CONF_BRIDGE = "bridge"
CONF_BRIDGE_PORT = "port"
CONF_BRIDGE_NAME = "name"
CONF_DEVICES = "devices"
CONF_DEVICE_ID = "device_id"
CONF_PROFILE = "profile"
CONF_NAME = "name"
CONF_CATEGORY = "category"
CONF_VALVE_TYPE = "valve_type"
CONF_FINISHED_EVENT_TYPES = "finished_event_types"
CONF_TILE_SERVICE = "tile_service"
CONF_HOT_WATER_LOW_THRESHOLD = "hot_water_low_threshold"
CONF_ALERT_SENSOR = "alert_sensor"
CONF_NO_HOT_WATER_SENSOR = "no_hot_water_sensor"
CONF_NIGHT_MODE_SWITCH = "night_mode_switch"
CONF_LIGHT = "light"
CONF_AMBIENT_LIGHT_SENSOR = "ambient_light_sensor"
CONF_FILTER_CHANGE_SENSOR = "filter_change_sensor"
CONF_FILTER_CHANGE_THRESHOLD = "filter_change_threshold"

DEFAULT_PORT = 21065
DEFAULT_BRIDGE_NAME = "HA Grouped Bridge"

# Where pairing state lives, inside the HA config dir so it persists
PAIRING_FILE = "homekit_grouped.state"
