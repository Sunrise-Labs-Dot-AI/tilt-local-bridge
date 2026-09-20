"""Constants for the Tilt Local Bridge integration."""

from __future__ import annotations

DOMAIN = "tilt_bridge"

REMOTE_SERVICE_UUID = "4e9a0001-3b7c-4f2e-9d61-5c8a2f7b0e10"
REMOTE_REQUEST_UUID = "4e9a0002-3b7c-4f2e-9d61-5c8a2f7b0e10"
REMOTE_RESPONSE_UUID = "4e9a0003-3b7c-4f2e-9d61-5c8a2f7b0e10"
REMOTE_INFO_UUID = "4e9a0004-3b7c-4f2e-9d61-5c8a2f7b0e10"

CONF_ADDRESS = "address"
CONF_BRIDGE_ID = "bridge_id"
CONF_BRIDGE_NAME = "bridge_name"
CONF_SEED = "seed"
CONF_DEVICE_NAME = "device_name"

DEFAULT_DEVICE_NAME = "Home Assistant"
POLL_INTERVAL_SECONDS = 60
MOVING_POLL_INTERVAL_SECONDS = 15
MOVING_POLL_WINDOW_SECONDS = 150
REQUEST_TIMEOUT_SECONDS = 12.0
# A missed poll keeps the last status this long before the shades go unavailable.
HOLD_LAST_STATUS_SECONDS = 600
