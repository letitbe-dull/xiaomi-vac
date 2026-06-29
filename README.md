<div align="center">

# Xiaomi Vacuum for Home Assistant

**Local control, optional live maps, and room-by-room cleaning for supported ijai, Dreame, Viomi, and Xiaomi-labelled vacuums.**

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5?logo=home-assistant&logoColor=white)](https://hacs.xyz/)
[![Release](https://img.shields.io/github/v/release/letitbe-dull/xiaomi-vac?include_prereleases)](https://github.com/letitbe-dull/xiaomi-vac/releases)
[![Stars](https://img.shields.io/github/stars/letitbe-dull/xiaomi-vac?style=social)](https://github.com/letitbe-dull/xiaomi-vac/stargazers)
[![Issues](https://img.shields.io/github/issues/letitbe-dull/xiaomi-vac)](https://github.com/letitbe-dull/xiaomi-vac/issues)
[![Last commit](https://img.shields.io/github/last-commit/letitbe-dull/xiaomi-vac)](https://github.com/letitbe-dull/xiaomi-vac/commits)

[Install](#installation) · [Configure](#configuration) · [Entities](#entities) · [Services](#services) · [Report a Bug](https://github.com/letitbe-dull/xiaomi-vac/issues/new)

</div>

---

## Why this exists

Your vacuum already knows the shape of your floor. This integration lets Home Assistant ask it nicely, over the LAN, without the answer taking a detour through someone else's cloud. Sign in with a Mi account and you also get the map: a camera entity and a Lovelace card that draw what the robot sees. Skip the account, hand over an IP and a token, and you keep local control with the map left off.

> [!NOTE]
> None of this is blessed by Xiaomi. It works by reverse-engineering the MIoT protocol and cloud API. If Xiaomi changes either and it affects your vacuum, please [open an issue](https://github.com/letitbe-dull/xiaomi-vac/issues/new).

## Supported models

These are the 65 models onboardable today; if yours misbehaves or isn't here, [open an issue](https://github.com/letitbe-dull/xiaomi-vac/issues/new) and we'll take a look.

| Brand | Models |
|-------|--------|
| **ijai** | v1, v2, v3, v13, v14, v15, v17, v18, v19 |
| **Xiaomi** | b106bk/eu, c101/eu, c103, c104, d106gl |
| **Viomi** | v12, v13, v15, v17, v18, v19, v22, v23, v24, v35, v38, v40, v45 |
| **Dreame** | p2008, p2009, p2027/28/28a/29/36, p2114a/o, p2140/a/p, p2148o, p2149o, p2150a/b/o, p2157, p2187, p2259, r2104, r2205, r2209, r2210, r2211o, r2215, r2216o, r2228/o/z, r2232a, r2233, r2235, r2246, r2247, r2254 |

> [!TIP]
> Not sure of your model string? It's on the device label, in the Mi Home app under device info, or in Home Assistant's device registry after a local-token setup attempt.

## Installation

### HACS (recommended)

1. [Install HACS](https://www.hacs.xyz/docs/use/download/download/) if you haven't already.
2. Add this repository as a custom HACS integration, or click the button below:

   [![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=letitbe-dull&repository=xiaomi-vac&category=integration)
3. Install **Xiaomi Vacuum**.
4. Restart Home Assistant.

### Manual

1. Copy the `xiaomi_vac` folder from this repo into your Home Assistant `custom_components` folder (`<config_dir>/custom_components/xiaomi_vac/`).
2. Restart Home Assistant.

## Configuration

1. Go to **Settings → Devices & Services**.
2. Click **+ ADD INTEGRATION** and search for **Xiaomi Vacuum**.
3. Choose a setup method:

   - **Mi Account (recommended)** - enter your Xiaomi/Mi Home **email and password**. The integration logs in, filters the account to supported vacuums, and stores the selected vacuum's local IP, token, and Xiaomi session tokens. Captcha and email 2FA are handled in-flow if Xiaomi demands them. The password is not persisted.
   - **Local token** - enter the vacuum's **IP address** and its 32-character **local token**. Core control and sensors are available; map rendering requires cloud credentials and is skipped in local-only mode.

4. Click **SUBMIT**. Home Assistant creates a device with all supported entities.

### Re-authentication

When the saved session can't be renewed, Home Assistant asks you to sign in again and updates the existing entry in place, so there's no need to delete anything and start over.

### Rate limiting

When Xiaomi wants 2FA it emails you a code, and if you hammer the sign-in button it will start holding those emails back, so go gently. Session tokens are kept between restarts, so an ordinary restart won't cost you another code.

## Entities

You get whatever your vacuum can actually do and nothing it can't, so a model that doesn't mop won't sprout a water-level control. The names below say what each one is.

| Entity | Type |
|--------|------|
| `vacuum.<name>` | Vacuum |
| `sensor.<name>_status` | Sensor |
| `sensor.<name>_battery` | Sensor |
| `select.<name>_fan_speed` | Select |
| `select.<name>_water_level` | Select |
| `select.<name>_mode` | Select |
| `select.<name>_sweep_type` | Select |
| `switch.<name>_repeat` | Switch |
| `switch.<name>_alarm` | Switch |
| `number.<name>_volume` | Number |
| `camera.<name>_map` | Camera (cloud setup only) |

## Services

### `xiaomi_vac.clean_segment`

Cleans one or more specific rooms by their room ID. Room IDs are exposed by the map camera attributes when a map is available.

| Field | Description | Example |
|-------|-------------|---------|
| `entity_id` | The vacuum entity. | `vacuum.xiaomi_robot_vacuum_s10` |
| `segments` | List of room IDs to clean. | `[10, 12]` |

**Example automation - clean the kitchen when you leave:**

```yaml
alias: Clean kitchen on departure
trigger:
  - platform: state
    entity_id: person.me
    to: not_home
action:
  - service: xiaomi_vac.clean_segment
    target:
      entity_id: vacuum.xiaomi_robot_vacuum_s10
    data:
      segments: [12]
```

## Map card

The integration serves a custom Lovelace card (`xiaomi-vac-card`) from `/xiaomi-vac-card/xiaomi-vac-card.js`. Storage-mode dashboards get the resource automatically. In YAML mode, add the resource manually, then add the card:

```yaml
type: custom:xiaomi-vac-card
vacuum: vacuum.xiaomi_robot_vacuum_s10
```

The card swipes between a vacuum image page and available maps. Decoded map data provides the room IDs used by `xiaomi_vac.clean_segment`.

> [!IMPORTANT]
> After updating the integration, do a **hard refresh** in your browser (Ctrl+Shift+R / "Empty cache and hard reload") to pick up the new card JavaScript. Restarting Home Assistant alone does not reload an already-open browser tab.

> [!NOTE]
> If your dashboard is in **YAML mode**, Home Assistant cannot auto-register Lovelace resources. Add `/xiaomi-vac-card/xiaomi-vac-card.js` as a JavaScript module resource.

## Debugging

To see what the integration is doing, add this to `configuration.yaml` and restart Home Assistant:

```yaml
logger:
  default: info
  logs:
    custom_components.xiaomi_vac: debug
```

## Contributing

Issues and pull requests are welcome - open one [here](https://github.com/letitbe-dull/xiaomi-vac/issues).

---

<div align="center">
<sub>Not affiliated with or endorsed by Xiaomi, ijai, Dreame, Viomi, or Roidmi. Use at your own risk.</sub>
</div>
