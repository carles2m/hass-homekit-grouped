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
- **EcoNet (Rheem) heat pump water heater** (`econet_water_heater` profile) —
  Thermostat with Off/Heat mode + temperature setpoint. Optional extras:
  MotionSensor alert (opt-in), OccupancySensor for low-hot-water (opt-in
  with threshold), and ContactSensor for no-hot-water (opt-in, opens at
  0% available). Replaces HA's built-in HomeKit thermostat mapping that
  spams `TargetHeatingCoolingState value=0 is invalid` errors on EcoNet
  modes like `eco` that don't map to HomeKit's mode vocabulary.
- **Coway Airmega / IoCare air purifier** (`coway_air_purifier` profile) —
  AirPurifier with Auto/Manual + speed slider (snaps to Off / Low / Med /
  High), linked AirQualitySensor (AirQuality enum + PM10Density), a
  Night-mode Switch that turns the `Night` preset on (off returns to
  `Auto`), a Lightbulb that
  wraps the physical LED switch, an opt-in LightSensor for the built-in
  lux reading, and an opt-in FilterMaintenance service that surfaces
  "Replace Filter" in Apple Home when life drops below a configurable
  threshold.
- **GE / Monogram gas cooktop** (`ge_cooktop` profile, via the
  third-party `ge_home` integration) — Valve as the primary service,
  carrying the kitchen timer (writable end-to-end: SetDuration in
  Apple Home pushes minutes to the device, RemainingDuration counts
  down, Active=0 stops the timer). Linked services: a sustained
  MotionSensor that fires while any burner is on, a pulse
  MotionSensor for the timer-expiry beep, and a read-only
  ContactSensor for the cooktop lock (closed=locked; the lock can't
  be unlocked from third-party clients per Brillion's per-client
  writability policy).

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

    # EcoNet (Rheem) heat pump water heater
    - profile: econet_water_heater
      device_id: <ha_device_id_of_water_heater>
      name: "Water Heater"
      hot_water_low_threshold: 30 # (optional) add OccupancySensor that
                                  # fires when available_hot_water < N%

    # Coway Airmega / IoCare air purifier
    - profile: coway_air_purifier
      device_id: <ha_device_id_of_purifier>
      name: "Air Purifier"
      # night_mode_switch: true     # (optional, default true)  Night preset switch
      # light: true                 # (optional, default true)  LED lightbulb
      # ambient_light_sensor: true  # (optional, default false) built-in lux sensor
      # filter_change_sensor: true  # (optional, default false) FilterMaintenance
      # filter_change_threshold: 10 # (optional, default 10)    fire ChangeFilter below this %

    # GE / Monogram gas cooktop (via ge_home integration)
    - profile: ge_cooktop
      device_id: <ha_device_id_of_cooktop>
      name: "Cooktop"
      # category: faucet            # (optional, default faucet)
      # valve_type: irrigation      # (optional, default irrigation)
      # entities:                   # (optional) explicit entity_id overrides
      #   kitchen_timer: number.cooktop_kitchen_timer
      #   kitchen_timer_alarm: binary_sensor.cooktop_kitchen_timer_alarm
```

### Per-device options

- **`entities`** — map of role name → `entity_id`, overriding discovery for that
  role. Profiles normally find their entities by scanning the HA device, which
  only returns entities *attached* to it. Use an override when an entity the
  accessory needs lives outside the device — for example one supplied by a
  helper integration whose platform has no config entry, since HA ignores
  `device_info` for those and never attaches them. Roles not listed still fall
  back to the device scan. An override naming a non-existent entity logs a
  distinct warning rather than the generic "missing entity" one.

  Overrides are explicit on purpose: a registry-wide suffix search would be
  simpler, but several GE appliances expose identically-suffixed entities
  (ovens also have `_kitchen_timer` numbers), so it could silently bind an
  accessory to the wrong appliance.

  `ge_cooktop` roles: `kitchen_timer`, `cooktop_on`, `kitchen_timer_alarm`,
  `locked`.

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
- **`hot_water_low_threshold`** (econet_water_heater only) — integer 1-99.
  When set, adds an OccupancySensor "Hot Water Low" that fires when the
  appliance's `available_hot_water` sensor drops below this percent.
  Unset by default — Apple Home's generic "Occupancy Detected" notification
  text can be confusing, so this sensor is opt-in.
- **`alert_sensor`** (econet_water_heater only) — boolean. When `true`,
  adds a MotionSensor "Alert" that fires when `alert_count` goes from
  0 to >0. Unset/false by default — the notification ("Motion Detected
  in \<room\>") doesn't identify which alert, and EcoNet doesn't expose
  alert-type metadata, so this is opt-in.
- **`no_hot_water_sensor`** (econet_water_heater only) — boolean. When
  `true`, adds a ContactSensor "No Hot Water" that opens when the
  appliance's `available_hot_water` sensor reaches 0%, closes when it
  climbs back above 0. Apple Home notifications include the accessory
  name ("\<Name\> No Hot Water: was opened") so it's clearly
  identifiable, at the cost of a second "was closed" notification when
  hot water comes back. Unset/false by default.
- **`night_mode_switch`** (coway_air_purifier only) — boolean, default
  `true`. Exposes a Switch that turns the fan's `Night` preset mode on;
  switching it off returns the fan to `Auto` (HomeKit's AirPurifier
  service has no native night-mode characteristic).
  Set to `false` to drop the switch if you don't use Night mode.
- **`light`** (coway_air_purifier only) — boolean, default `true`.
  Exposes the purifier's LED ring as a HomeKit Lightbulb driven by the
  corresponding `switch.*_light` entity. Set to `false` to hide it.
- **`ambient_light_sensor`** (coway_air_purifier only) — boolean,
  default `false`. Adds a HomeKit LightSensor driven by the purifier's
  built-in `sensor.*_lux` reading. Opt-in because flipping it on an
  already-paired accessory changes the service composition and, in
  rare cases, can require re-pairing the bridge in Apple Home. Leave
  unset unless you want the lux reading visible to Apple Home /
  HomeKit automations.
- **`filter_change_sensor`** (coway_air_purifier only) — boolean,
  default `false`. Adds a HomeKit FilterMaintenance service driven by
  the purifier's `sensor.*_max2_filter` life-remaining percentage.
  Apple Home shows a yellow "Filter" badge on the accessory tile and a
  "Replace Filter" row in accessory details when life drops below
  `filter_change_threshold`. Same opt-in rationale as the light
  sensor.
- **`filter_change_threshold`** (coway_air_purifier only) — integer
  1-99, default `10`. Percent life-remaining at which the
  FilterMaintenance service flips to "Change Filter". Only consulted
  when `filter_change_sensor` is on.

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
