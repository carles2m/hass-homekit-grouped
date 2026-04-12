"""Device profiles for homekit_grouped."""

from .base import GroupedAccessory
from .home_connect_fridge import HomeConnectFridgeAccessory
from .thinq_washer import ThinqWasherAccessory

PROFILES: dict[str, type[GroupedAccessory]] = {
    "thinq_washer": ThinqWasherAccessory,
    "home_connect_fridge": HomeConnectFridgeAccessory,
}


def get_profile(name: str) -> type[GroupedAccessory]:
    """Return the accessory class for a named profile."""
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown profile '{name}'. Available: {sorted(PROFILES)}"
        ) from exc
