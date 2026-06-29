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
CONF_DEVICE_ID = "device_id"
CONF_WIFI_SN = "wifi_sn"
CONF_SSECURITY = "ssecurity"
CONF_SERVICE_TOKEN = "service_token"
CONF_PASS_TOKEN = "pass_token"

DEFAULT_SCAN_INTERVAL = 10  # seconds, local MIoT polling
MAP_SCAN_INTERVAL = 30      # seconds, cloud map refresh while cleaning
MAP_IDLE_INTERVAL = 300     # seconds, cloud map refresh while docked/idle

# Servers accepted by the Xiaomi cloud.
SERVERS = ["cn", "de", "us", "ru", "tw", "sg", "in", "i2"]
