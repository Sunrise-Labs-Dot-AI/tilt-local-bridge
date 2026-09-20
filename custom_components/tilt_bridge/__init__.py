"""Tilt Local Bridge: shades over Bluetooth, straight from the bridge Raspberry Pi."""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .client import TiltBridgeClient
from .const import CONF_ADDRESS, CONF_BRIDGE_ID, CONF_BRIDGE_NAME, CONF_SEED
from .coordinator import TiltBridgeCoordinator
from .entity import bridge_device_info
from .protocol import Identity

PLATFORMS = [Platform.COVER, Platform.SENSOR]

type TiltBridgeConfigEntry = ConfigEntry[TiltBridgeCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: TiltBridgeConfigEntry) -> bool:
    address: str = entry.data[CONF_ADDRESS]
    identity = Identity.from_seed(bytes.fromhex(entry.data[CONF_SEED]))
    bridge_name: str = entry.data.get(CONF_BRIDGE_NAME) or "Tilt Local Bridge"
    client = TiltBridgeClient(
        identity,
        name=bridge_name,
        ble_device_provider=lambda: bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        ),
    )
    coordinator = TiltBridgeCoordinator(
        hass,
        entry,
        client,
        bridge_id=entry.data[CONF_BRIDGE_ID],
        bridge_name=bridge_name,
        address=address,
    )
    if bluetooth.async_ble_device_from_address(hass, address, connectable=True) is None:
        raise ConfigEntryNotReady(f"{bridge_name} is not in Bluetooth range yet")
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # The bridge device exists before any shade points at it via via_device.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **bridge_device_info(coordinator)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TiltBridgeConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
