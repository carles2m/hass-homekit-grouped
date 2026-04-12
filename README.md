# homekit_grouped

Custom Home Assistant integration that exposes multiple HA entities from the same device
as a **single grouped HomeKit accessory** (multi-service HAP accessory), the way
Homebridge plugins do.

HA's built-in HomeKit Bridge exposes one HomeKit accessory per HA entity. For appliances
that have many facets (washer with cycle state, door lock, remaining time, power state,
etc.) this produces 5-10 separate tiles in Apple Home that all represent the same device.
This integration creates a parallel HomeKit bridge that groups those entities under one
accessory with multiple services, so Apple Home shows one tile.

## Status

Alpha. Use at your own risk. Tested with a specific setup; YMMV.

## Supported devices (v0.1)

- **LG ThinQ washer** — exposed as Valve (active while running) + OccupancySensor (cycle in progress) + Switch (power)
- **LG ThinQ dryer** — same shape as washer

More profiles to come. PRs welcome (but don't expect fast merges).

## How it works

- Spawns a second HomeKit bridge inside HA on a separate port (default 21065)
- You pair it in Apple Home separately from HA's built-in HomeKit bridge
- Exclude the grouped devices' entities from HA's built-in HomeKit bridge to avoid duplicates
- State sync: subscribes to HA state changes, pushes to HAP characteristics

## Install

1. HACS → Custom Repositories → add `https://github.com/carles2m/hass-homekit-grouped` as Integration
2. Install, restart HA
3. Add config to `configuration.yaml` (see below)
4. Restart HA
5. In Apple Home, add accessory, scan the QR shown in HA logs

## Configuration

```yaml
homekit_grouped:
  bridge:
    port: 21065
    name: "HA Grouped Bridge"
  devices:
    - profile: thinq_washer
      device_id: 29b86a58e7d41a7c3a0fb865bab61e14   # HA device_id of washer
      name: "Washer"
    - profile: thinq_washer   # dryer uses same profile
      device_id: 74a21be96458bab4d158cb8bf0a8f69e
      name: "Dryer"
```

Remember to also remove those devices' entities from HA's built-in HomeKit bridge filter
to avoid seeing them twice in Apple Home.

## License

MIT
