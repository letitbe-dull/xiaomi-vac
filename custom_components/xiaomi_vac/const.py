"""Constants for the Xiaomi Vacuum integration."""
from __future__ import annotations

DOMAIN = "xiaomi_vac"

CONF_HOST = "host"
CONF_TOKEN = "token"
CONF_MODEL = "model"
CONF_MAC = "mac"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SERVER = "server"
CONF_USER_ID = "user_id"
CONF_OWNER_UID = "owner_uid"
CONF_DEVICE_ID = "device_id"
CONF_WIFI_SN = "wifi_sn"
CONF_SSECURITY = "ssecurity"
CONF_SERVICE_TOKEN = "service_token"
CONF_PASS_TOKEN = "pass_token"
CONF_OAUTH_ACCESS_TOKEN = "oauth_access_token"
CONF_OAUTH_REFRESH_TOKEN = "oauth_refresh_token"
CONF_OAUTH_EXPIRES_TS = "oauth_expires_ts"
CONF_OAUTH_REGION = "oauth_region"
CONF_OAUTH_DEVICE_ID = "oauth_device_id"
CONF_OAUTH_REDIRECT_URI = "oauth_redirect_uri"

DEFAULT_SCAN_INTERVAL = 10  # seconds, local MIoT polling
MAP_SCAN_INTERVAL = 30      # seconds, cloud map refresh while cleaning
# While docked/idle the coordinator no longer polls on a short fixed clock —
# camera/http reads trigger an on-demand refresh instead (see
# XiaomiMapCoordinator.ensure_fresh). This is now just the safety-net
# interval for consumers that read coordinator.data without ever calling
# ensure_fresh themselves (diagnostics, the room list).
MAP_IDLE_INTERVAL = 900     # seconds, default safety-net cloud map refresh while docked/idle (user-configurable via number.py)
MAP_ON_DEMAND_MAX_AGE = 30  # seconds, idle on-demand refresh throttle when the idle_on_demand_refresh switch is on


# Servers accepted by the Xiaomi cloud.
SERVERS = ["cn", "de", "us", "ru", "tw", "sg", "in", "i2"]
