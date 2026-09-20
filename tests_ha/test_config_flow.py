"""Discovery, the pairing ceremony, and reauth through the config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tilt_bridge.const import (
    CONF_ADDRESS,
    CONF_BRIDGE_ID,
    CONF_SEED,
    DOMAIN,
    REMOTE_SERVICE_UUID,
)
from custom_components.tilt_bridge.protocol import Identity

from .conftest import BRIDGE_ADDRESS, BRIDGE_ID, FakeBridge, client_factory_for


def _discovery() -> BluetoothServiceInfoBleak:
    return BluetoothServiceInfoBleak(
        name="Office Bridge",
        address=BRIDGE_ADDRESS,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[REMOTE_SERVICE_UUID],
        source="local",
        device=None,
        advertisement=None,
        connectable=True,
        time=0,
        tx_power=None,
    )


@pytest.fixture
def wired_flow(fake_bridge: FakeBridge):
    """Route the flow's client through the fake bridge and a fake BLE device."""

    factory = client_factory_for(fake_bridge)
    original_init = None
    from custom_components.tilt_bridge import config_flow as module

    class PatchedClient(module.TiltBridgeClient):
        def __init__(self, identity, *, name, ble_device_provider, client_factory=None):
            super().__init__(identity, name=name, ble_device_provider=lambda: object(), client_factory=factory)

    with patch.object(module, "TiltBridgeClient", PatchedClient), patch.object(
        module.bluetooth, "async_ble_device_from_address", return_value=object()
    ):
        yield


async def test_bluetooth_discovery_pairs_with_a_button_press(
    hass: HomeAssistant, fake_bridge: FakeBridge, wired_flow: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_discovery()
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pair"
    assert result["description_placeholders"]["direction"] == "down"
    assert result["description_placeholders"]["shade"] == "Door"
    assert result["description_placeholders"]["code"] == "482913"
    assert fake_bridge.pending is not None

    # Submitting before the press: still pending, same instruction.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "still_pending"}

    fake_bridge.approve_on_status = True
    with patch("custom_components.tilt_bridge.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Office Bridge"
    assert result["data"][CONF_ADDRESS] == BRIDGE_ADDRESS
    assert result["data"][CONF_BRIDGE_ID] == BRIDGE_ID
    seed = bytes.fromhex(result["data"][CONF_SEED])
    assert Identity.from_seed(seed).public_key_hex in fake_bridge.paired


async def test_user_flow_lists_advertising_bridges(hass: HomeAssistant, fake_bridge: FakeBridge, wired_flow: None) -> None:
    with patch(
        "custom_components.tilt_bridge.config_flow.bluetooth.async_discovered_service_info",
        return_value=[_discovery()],
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: BRIDGE_ADDRESS})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pair"


async def test_user_flow_aborts_when_nothing_advertises(hass: HomeAssistant, wired_flow: None) -> None:
    with patch(
        "custom_components.tilt_bridge.config_flow.bluetooth.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_devices_found"


async def test_already_configured_bridge_is_not_added_twice(hass: HomeAssistant, wired_flow: None) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=BRIDGE_ADDRESS, data={CONF_ADDRESS: BRIDGE_ADDRESS}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_discovery()
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_unreachable_bridge_shows_cannot_connect(hass: HomeAssistant, fake_bridge: FakeBridge, wired_flow: None) -> None:
    fake_bridge.unreachable = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_discovery()
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pair"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_retry_after_cannot_connect_shows_the_instruction_plain(
    hass: HomeAssistant, fake_bridge: FakeBridge, wired_flow: None
) -> None:
    """A submit that only just asked the bridge must not say "not approved yet"."""

    fake_bridge.unreachable = True
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_discovery()
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "cannot_connect"}

    fake_bridge.unreachable = False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pair"
    assert not result["errors"]
    assert result["description_placeholders"]["direction"] == "down"
    assert result["description_placeholders"]["code"] == "482913"
    assert fake_bridge.pending is not None

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "still_pending"}

    fake_bridge.approve_on_status = True
    with patch("custom_components.tilt_bridge.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
