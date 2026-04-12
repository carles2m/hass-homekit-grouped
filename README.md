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

## Supported devices

- **LG ThinQ washer / dryer** (`thinq_washer` profile) — Valve with countdown +
  MotionSensor for cycle-complete notification, triggered by the ThinQ
  integration's `event.*_notification` entity
- **Home Connect fridge-freezer** (`home_connect_fridge` profile) — 2 Contact
  Sensors (doors) + 3 Motion Sensors (door-left-open alarms, over-temperature
  alarm) + 2 Temperature Sensors (setpoints)

More profiles to come. PRs welcome (but don't expect fast merges).

## How it works

- Spawns a second HomeKit bridge inside HA on a separate port (default 21065)
- You pair it in Apple Home separately from HA's built-in HomeKit bridge
- Exclude the grouped devices' entities from HA's built-in HomeKit bridge to avoid duplicates
- State sync: subscribes to HA state changes, pushes to HAP characteristics
- Accessory AIDs are derived from a stable SHA-256 of the HA device_id, so Apple
  Home customizations (room, type, notifications) survive HA restarts

## Install

1. HACS → Custom Repositories → add `https://github.com/carles2m/hass-homekit-grouped` as Integration
2. Install, restart HA
3. Add config to `configuration.yaml` (see below)
4. Restart HA
5. In Apple Home, add accessory, scan the QR shown in HA logs (PIN is in the HA log line
   starting with `HomeKit Grouped Bridge ready`)

## Configuration

```yaml
homekit_grouped:
  bridge:
    port: 21065
    name: "HA Grouped Bridge"

  devices:
    # LG ThinQ washer
    - profile: thinq_washer
      device_id: <ha_device_id_of_washer>
      name: "Washer"
      category: faucet            # sprinkler | faucet | fan | other | shower_head
      valve_type: faucet          # generic | irrigation | shower | faucet
      finished_event_types:       # event_type values from event.*_notification
        - washing_is_complete     # that fire the "Finished" MotionSensor pulse

    # LG ThinQ dryer (same profile, different config)
    - profile: thinq_washer
      device_id: <ha_device_id_of_dryer>
      name: "Dryer"
      category: fan               # gives the fan tile icon in Apple Home
      valve_type: irrigation      # irrigation renders the cleanest countdown UI
      finished_event_types:
        - drying_is_complete

    # Home Connect fridge-freezer
    - profile: home_connect_fridge
      device_id: <ha_device_id_of_fridge>
      name: "Fridge"
      category: other             # other | sensor | door | window
      tile_service: garage_door   # (optional) see "room tiles" below
```

### Per-device options

- **`category`** — affects the Apple Home tile icon. Valid across profiles:
  `sprinkler`, `faucet`, `fan`, `other`, `shower_head`, `door`, `sensor`, `window`.
  Not every profile supports every category; check profile source if unsure.
- **`valve_type`** (thinq_washer only) — `generic`, `irrigation`, `shower`, or
  `faucet`. Determines valve semantics in Apple Home. `irrigation` produces
  the cleanest "X min remaining" countdown.
- **`finished_event_types`** (thinq_washer only) — list of `event_type` values
  emitted on the device's `event.*_notification` entity that should fire the
  "Finished" MotionSensor pulse (one-shot iOS notification per cycle end).
  No default — must be set per device since event names vary by appliance.
- **`tile_service`** (home_connect_fridge only) — optional `garage_door`.
  Adds a fake actionable service so Apple Home shows the accessory as a
  room tile. See "Getting a room tile" below.

### Remember to remove entities from HA's built-in HomeKit bridge

Once a device is exposed here, remove its entities from HA HomeKit's
`include_entities` filter (Settings → Devices & Services → HomeKit Bridge →
Configure) so you don't see the same accessory twice in Apple Home.

### Tip: change the parent tile icon via "Display As"

For multi-service accessories (like the fridge), Apple Home picks the tile
icon based on the primary service type, not the HAP category. To change
how the parent tile looks:

1. Open the grouped accessory in Apple Home (e.g. Fridge)
2. Long-press a sub-accessory (e.g. Fridge Refrigerator Door) → Settings
3. Change **Display As** from "Contact Sensor" to "Door" (or Window, etc.)
4. The parent tile's icon updates to match

This works across restarts because AIDs/IIDs are stable. Do it once per
device.

### Getting a room tile for a sensor-only accessory (`tile_service`)

Apple Home only shows tiles in room views for accessories with at least
one "actionable" service (lights, switches, valves, locks, fans, garage
doors, thermostats). Pure-sensor accessories — like the Home Connect
fridge, which is just contact sensors, motion sensors, and temperature
sensors — live in the accessory list but never get a room tile.

The `tile_service` option adds a fake actionable service to the
accessory so Apple Home gives it a room tile. Currently supported on
`home_connect_fridge`:

- `garage_door` — adds a HAP `GarageDoorOpener` service driven by the
  refrigerator door's open/closed state. The fridge appears as a
  door-style tile in its room. Writes are silently reverted — you
  can't actually command the fridge open/closed from HomeKit.

The service is appended LAST to the accessory's service list so it
doesn't shift IIDs of existing services. Your Display As overrides,
room assignments, and notification preferences on the other sensors
are preserved.

**Heads up on notifications.** iOS treats GarageDoorOpener as a
safety-relevant device type and enables status-change notifications
on it by default — meaning you'll get a push every time the fridge
door opens or closes. To silence:

1. In the Home app, long-press the fridge's garage-door tile
2. Settings → Notifications → **turn off status-change notifications**
3. (Repeat on every family member's iPhone — notification prefs are
   per-device, not shared via iCloud)

If you don't want room-tile visibility badly enough to manage this
per-phone, leave `tile_service` unset and use the Display As workaround
(above) instead.

## License

MIT
