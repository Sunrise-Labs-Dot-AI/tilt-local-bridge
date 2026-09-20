"""Config flow: discover the bridge over Bluetooth, then pair with a shade button press."""

from __future__ import annotations

import logging
import secrets
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.core import callback

from .client import (
    TiltBridgeClient,
    TiltBridgeError,
    TiltBridgeSession,
    TiltBridgeUnavailable,
)
from .const import (
    CONF_ADDRESS,
    CONF_BRIDGE_ID,
    CONF_BRIDGE_NAME,
    CONF_DEVICE_NAME,
    CONF_SEED,
    DEFAULT_DEVICE_NAME,
    DOMAIN,
    REMOTE_SERVICE_UUID,
)
from .protocol import Identity, PairingReply

_LOGGER = logging.getLogger(__name__)


class TiltBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Bluetooth discovery, then the bridge's own pairing ceremony."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str = "Tilt Local Bridge"
        self._names: dict[str, str] = {}
        self._seed: bytes | None = None
        self._pending: PairingReply | None = None
        self._bridge_id: str | None = None
        self._bridge_name: str | None = None
        self._reauth_entry: ConfigEntry | None = None

    # ----- discovery -----

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self._name = discovery_info.name or self._name
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_pair()
        return self.async_show_form(
            step_id="confirm", description_placeholders={"name": self._name}
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._address = address
            self._name = self._names.get(address, self._name)
            return await self.async_step_pair()
        configured = self._async_current_ids()
        self._names = {}
        for info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
            if REMOTE_SERVICE_UUID in info.service_uuids and info.address not in configured:
                self._names[info.address] = info.name or info.address
        if not self._names:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(self._names)}),
        )

    # ----- reauth (the bridge forgot us) -----

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._reauth_entry = self._get_reauth_entry()
        self._address = entry_data[CONF_ADDRESS]
        self._seed = bytes.fromhex(entry_data[CONF_SEED])
        self._name = entry_data.get(CONF_BRIDGE_NAME) or self._name
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_pair()
        return self.async_show_form(
            step_id="reauth_confirm", description_placeholders={"name": self._name}
        )

    # ----- pairing -----

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._address is not None
        if self._seed is None:
            self._seed = secrets.token_bytes(32)
        identity = Identity.from_seed(self._seed)
        address = self._address
        client = TiltBridgeClient(
            identity,
            name=self._name,
            ble_device_provider=lambda: bluetooth.async_ble_device_from_address(
                self.hass, address, connectable=True
            ),
        )
        first_visit = user_input is None
        errors: dict[str, str] = {}
        try:
            outcome = await client.run(
                lambda session: self._pair_round(session, first_visit=first_visit)
            )
        except TiltBridgeUnavailable as exc:
            _LOGGER.debug("Pairing round could not reach %s: %s", self._name, exc)
            errors["base"] = "cannot_connect"
            outcome = "error"
        except TiltBridgeError as exc:
            errors["base"] = "busy" if exc.code == "busy" else "unknown"
            outcome = "error"
        if outcome == "approved":
            return self._async_finish()
        if outcome == "pending":
            # The bridge checked and the press has not landed yet. A round that
            # only just asked reports "requested" and shows the instruction plain.
            errors["base"] = "still_pending"
        elif outcome in ("denied", "expired"):
            errors["base"] = outcome
        challenge = self._pending.challenge if self._pending is not None else None
        return self.async_show_form(
            step_id="pair",
            errors=errors,
            description_placeholders={
                "name": self._bridge_name or self._name,
                "direction": challenge.direction if challenge else "the shade button",
                "shade": challenge.shade_name if challenge else "any shade",
                "code": (self._pending.code if self._pending and self._pending.code else "..."),
            },
        )

    async def _pair_round(self, session: TiltBridgeSession, *, first_visit: bool) -> str:
        """One connection's worth of pairing: request, or check, or re-request."""

        await session.handshake()
        self._bridge_id = session.bridge_id
        self._bridge_name = session.bridge_name or self._name
        if session.paired:
            return "approved"
        if self._pending is None or first_visit:
            self._pending = await session.pair(self._device_name())
            return self._pending.status if self._pending.status != "pending" else "requested"
        reply = await session.pair_status()
        if reply.status == "approved":
            return "approved"
        if reply.status == "pending":
            return "pending"
        # Denied, expired, or gone: ask again so the next press can land.
        outcome = reply.status if reply.status in ("denied", "expired") else "expired"
        self._pending = await session.pair(self._device_name())
        return outcome

    def _device_name(self) -> str:
        location = (self.hass.config.location_name or "").strip()
        name = f"{DEFAULT_DEVICE_NAME} ({location})" if location else DEFAULT_DEVICE_NAME
        return name[:40]

    @callback
    def _async_finish(self) -> ConfigFlowResult:
        assert self._address is not None and self._seed is not None and self._bridge_id
        data = {
            CONF_ADDRESS: self._address,
            CONF_SEED: self._seed.hex(),
            CONF_BRIDGE_ID: self._bridge_id,
            CONF_BRIDGE_NAME: self._bridge_name or self._name,
            CONF_DEVICE_NAME: self._device_name(),
        }
        if self._reauth_entry is not None:
            return self.async_update_reload_and_abort(self._reauth_entry, data_updates=data)
        return self.async_create_entry(title=self._bridge_name or self._name, data=data)
