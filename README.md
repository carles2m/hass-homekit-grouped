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
      device_id: <ha_device_id_of_washer>
      name: "Washer"
      category: faucet           # sprinkler | faucet | fan | other | shower_head
      valve_type: faucet         # generic | irrigation | shower | faucet
      finishing_states:          # cycle states that fire the "Finishing" sensor
        - spinning
        - drying
    - profile: thinq_washer      # dryer uses same profile with different config
      device_id: <ha_device_id_of_dryer>
      name: "Dryer"
      category: fan
      valve_type: irrigation     # irrigation gives the cleanest countdown UI
      finishing_states:
        - cooling
        - wrinkle_care
```

`category` affects the tile icon in Apple Home. `valve_type` determines whether
the valve shows as a generic valve, irrigation (sprinkler-style countdown),
shower, or water faucet. `finishing_states` must be set explicitly per device —
meaningful late-cycle phases differ wildly by appliance.

Remember to also remove those devices' entities from HA's built-in HomeKit bridge filter
to avoid seeing them twice in Apple Home.

## License

MIT
