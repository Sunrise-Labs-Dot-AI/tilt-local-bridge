"""Polling coordinator for one bridge."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    TiltBridgeClient,
    TiltBridgeError,
    TiltBridgeNotPaired,
    TiltBridgeSession,
    TiltBridgeUnavailable,
)
from .const import (
    DOMAIN,
    HOLD_LAST_STATUS_SECONDS,
    MOVING_POLL_INTERVAL_SECONDS,
    MOVING_POLL_WINDOW_SECONDS,
    POLL_INTERVAL_SECONDS,
)
from .protocol import BridgeStatus

_LOGGER = logging.getLogger(__name__)


class TiltBridgeCoordinator(DataUpdateCoordinator[BridgeStatus]):
    """Read the bridge's cached shade status; poll faster while a shade moves."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: TiltBridgeClient,
        *,
        bridge_id: str,
        bridge_name: str,
        address: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {bridge_name}",
            update_interval=timedelta(seconds=POLL_INTERVAL_SECONDS),
        )
        self.client = client
        self.bridge_id = bridge_id
        self.bridge_name = bridge_name
        self.address = address
        self._burst_until: float | None = None
        self._last_success_at: float | None = None
        self.missed_polls = 0

    async def _async_update_data(self) -> BridgeStatus:
        try:
            status: BridgeStatus = await self.client.run(lambda session: session.status())
        except TiltBridgeNotPaired as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except TiltBridgeUnavailable as exc:
            # A marginal link misses the odd poll. Keep showing the last status
            # for a while so the covers stay usable; a command still opens a
            # fresh connection of its own.
            if self.data is not None and self._within_hold():
                self.missed_polls += 1
                _LOGGER.debug(
                    "Poll of %s missed (%s); keeping the last status (%s in a row)",
                    self.bridge_name,
                    exc,
                    self.missed_polls,
                )
                self._set_interval(self.data)
                return self.data
            raise UpdateFailed(str(exc)) from exc
        except TiltBridgeError as exc:
            raise UpdateFailed(str(exc)) from exc
        self._last_success_at = time.monotonic()
        self.missed_polls = 0
        self._set_interval(status)
        return status

    def _within_hold(self) -> bool:
        if self._last_success_at is None:
            return False
        return time.monotonic() - self._last_success_at < HOLD_LAST_STATUS_SECONDS

    def _set_interval(self, status: BridgeStatus) -> None:
        now = time.monotonic()
        moving = any(
            shade.target is not None and shade.target != shade.position for shade in status.shades
        )
        bursting = self._burst_until is not None and now < self._burst_until
        seconds = MOVING_POLL_INTERVAL_SECONDS if moving or bursting else POLL_INTERVAL_SECONDS
        self.update_interval = timedelta(seconds=seconds)

    async def async_set_position(self, shade_id: str, position: int) -> None:
        """Queue one absolute position on the bridge and watch it arrive."""

        async def operation(session: TiltBridgeSession) -> str:
            return await session.set_position(shade_id, position)

        try:
            outcome = await self.client.run(operation)
        except TiltBridgeNotPaired as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except TiltBridgeError as exc:
            raise HomeAssistantError(str(exc)) from exc
        _LOGGER.debug("Bridge %s took position %s for %s: %s", self.bridge_name, position, shade_id, outcome)
        self._last_success_at = time.monotonic()
        self.missed_polls = 0
        self._burst_until = time.monotonic() + MOVING_POLL_WINDOW_SECONDS
        if self.data is not None:
            shades = tuple(
                replace(shade, target=position if shade.position != position else None)
                if shade.id == shade_id
                else shade
                for shade in self.data.shades
            )
            self.async_set_updated_data(replace(self.data, shades=shades))
        self.update_interval = timedelta(seconds=MOVING_POLL_INTERVAL_SECONDS)
        await self.async_request_refresh()

    async def async_refresh_shades(self) -> None:
        """Ask the bridge to re-read every shade over Bluetooth, then poll."""

        try:
            await self.client.run(lambda session: session.refresh())
        except TiltBridgeError as exc:
            raise HomeAssistantError(str(exc)) from exc
        self._burst_until = time.monotonic() + MOVING_POLL_INTERVAL_SECONDS * 2
        await self.async_request_refresh()
