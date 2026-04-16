"""Device profiles for homekit_grouped."""

from .base import GroupedAccessory
from .coway_air_purifier import CowayAirPurifierAccessory
from .econet_water_heater import EcoNetWaterHeaterAccessory
from .home_connect_fridge import HomeConnectFridgeAccessory
from .thinq_washer import ThinqWasherAccessory

PROFILES: dict[str, type[GroupedAccessory]] = {
    "thinq_washer": ThinqWasherAccessory,
    "home_connect_fridge": HomeConnectFridgeAccessory,
    "econet_water_heater": EcoNetWaterHeaterAccessory,
    "coway_air_purifier": CowayAirPurifierAccessory,
}


def get_profile(name: str) -> type[GroupedAccessory]:
    """Return the accessory class for a named profile."""
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown profile '{name}'. Available: {sorted(PROFILES)}"
        ) from exc
