"""Session behaviour against the fake bridge: nonces, pushes, refusals."""

from __future__ import annotations

import warnings
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.tilt_bridge.client import (
    _negotiated_mtu,
    TiltBridgeClient,
    TiltBridgeError,
    TiltBridgeNotPaired,
    TiltBridgeStaleServices,
    TiltBridgeUnavailable,
)
from custom_components.tilt_bridge.protocol import Identity

from .conftest import FakeBridge, client_factory_for


async def test_status_and_set_round_trip(identity: Identity, fake_bridge: FakeBridge) -> None:
    fake_bridge.paired.add(identity.public_key_hex)
    client = TiltBridgeClient(identity, name="Office Bridge", ble_device_provider=lambda: object(), client_factory=client_factory_for(fake_bridge))

    async def operation(session):
        status = await session.status()
        outcome = await session.set_position("door", 50)
        return status, outcome, await session.status()

    status, outcome, after = await client.run(operation)
    assert status.shade("door").position == 100
    assert outcome == "accepted"
    assert after.shade("door").target == 50
    assert fake_bridge.connections == 1
    assert [r["t"] for r in fake_bridge.requests] == ["nonce", "status", "set", "status"]


async def test_unpaired_is_reported_as_not_paired(identity: Identity, fake_bridge: FakeBridge) -> None:
    client = TiltBridgeClient(identity, name="Office Bridge", ble_device_provider=lambda: object(), client_factory=client_factory_for(fake_bridge))
    with pytest.raises(TiltBridgeNotPaired):
        await client.run(lambda session: session.status())


async def test_pairing_conversation(identity: Identity, fake_bridge: FakeBridge) -> None:
    client = TiltBridgeClient(identity, name="Office Bridge", ble_device_provider=lambda: object(), client_factory=client_factory_for(fake_bridge))

    async def ask(session):
        await session.handshake()
        assert session.paired is False
        return await session.pair("Home Assistant")

    reply = await client.run(ask)
    assert reply.status == "pending"
    assert reply.challenge.shade_name == "Door" and reply.challenge.direction == "down"
    assert fake_bridge.pending["name"] == "Home Assistant"
    fake_bridge.approve_on_status = True
    assert (await client.run(lambda session: session.pair_status())).status == "approved"
    assert identity.public_key_hex in fake_bridge.paired


async def test_out_of_range_and_refusals(identity: Identity, fake_bridge: FakeBridge) -> None:
    fake_bridge.paired.add(identity.public_key_hex)
    fake_bridge.writes = False
    client = TiltBridgeClient(identity, name="Office Bridge", ble_device_provider=lambda: object(), client_factory=client_factory_for(fake_bridge))
    with pytest.raises(TiltBridgeError) as refused:
        await client.run(lambda session: session.set_position("door", 10))
    assert refused.value.code == "writes_disabled"
    fake_bridge.unreachable = True
    with pytest.raises(TiltBridgeUnavailable):
        await client.run(lambda session: session.status())
    absent = TiltBridgeClient(identity, name="x", ble_device_provider=lambda: None, client_factory=client_factory_for(fake_bridge))
    with pytest.raises(TiltBridgeUnavailable):
        await absent.run(lambda session: session.status())


class _BlueZLikeClient:
    """Mimics bleak on BlueZ: the MTU property warns and answers 23 until acquired."""

    @property
    def mtu_size(self) -> int:
        warnings.warn("Using default MTU value. Call _acquire_mtu() first.")
        return 23


class _KnownMtuClient:
    mtu_size = 247


def test_unknown_mtu_is_treated_quietly_as_the_smallest() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _negotiated_mtu(_BlueZLikeClient()) is None
        assert _negotiated_mtu(_KnownMtuClient()) == 247
        assert _negotiated_mtu(object()) is None


async def test_stale_cached_services_are_cleared_and_reported_unavailable(
    identity: Identity, fake_bridge: FakeBridge
) -> None:
    fake_bridge.paired.add(identity.public_key_hex)
    fake_bridge.stale_services = True
    client = TiltBridgeClient(identity, name="Office Bridge", ble_device_provider=lambda: object(), client_factory=client_factory_for(fake_bridge))
    with pytest.raises(TiltBridgeStaleServices) as excinfo:
        await client.run(lambda session: session.status())
    assert isinstance(excinfo.value, TiltBridgeUnavailable)
    assert fake_bridge.cache_cleared == 1
    assert fake_bridge.requests == []

    # The cache is gone, so the next connection sees the service again.
    status = await client.run(lambda session: session.status())
    assert status.shade("door").position == 100
    assert fake_bridge.connections == 2


async def test_stale_services_are_cleared_by_address_when_the_client_has_one(
    identity: Identity, fake_bridge: FakeBridge
) -> None:
    fake_bridge.paired.add(identity.public_key_hex)
    fake_bridge.stale_services = True
    base_factory = client_factory_for(fake_bridge)

    async def factory(device):
        client = await base_factory(device)
        client.address = "AA:BB:CC:DD:EE:FF"
        return client

    client = TiltBridgeClient(identity, name="Office Bridge", ble_device_provider=lambda: object(), client_factory=factory)
    with patch("custom_components.tilt_bridge.client.clear_cache", new=AsyncMock(return_value=True)) as cleared:
        with pytest.raises(TiltBridgeStaleServices):
            await client.run(lambda session: session.status())
    cleared.assert_awaited_once_with("AA:BB:CC:DD:EE:FF")
    # The client method is the fallback only; by address it is not touched.
    assert fake_bridge.cache_cleared == 0
