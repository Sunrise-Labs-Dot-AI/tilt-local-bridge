"""Strict configuration and capability gates for the Tilt BLE bridge."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_MAC_PATTERN = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
_PERMIT_MARKER = object()


class TiltBridgeConfigError(RuntimeError):
    """Raised when bridge configuration or a secret file is unsafe."""


class ShadeAccessDisabled(TiltBridgeConfigError):
    """Raised when runtime shade access was not enabled at both gates."""


@dataclass(frozen=True)
class ShadeConfig:
    id: str
    name: str
    mac: str
    pairing_key_file: Path = field(repr=False)


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    username_file: Path = field(repr=False)
    password_file: Path = field(repr=False)
    topic_prefix: str = "tilt/local"
    discovery_prefix: str = "homeassistant"
    keepalive_seconds: int = 60


@dataclass(frozen=True)
class BridgeAccessConfig:
    allow_reads: bool = False
    allow_position_writes: bool = False


@dataclass(frozen=True)
class TiltBridgeConfig:
    version: int
    access: BridgeAccessConfig
    mqtt: MqttConfig
    shades: tuple[ShadeConfig, ...]
    poll_interval_seconds: int = 120
    command_cooldown_seconds: int = 5


@dataclass(frozen=True)
class ShadeAccessPermit:
    """An unforgeable-in-normal-use token required by the live BLE transport."""

    can_read: bool
    can_write_position: bool
    _marker: object = field(repr=False, compare=False)

    def assert_valid(self) -> None:
        if self._marker is not _PERMIT_MARKER or not self.can_read:
            raise ShadeAccessDisabled("Shade communication does not have a valid permit.")

    def require_position_write(self) -> None:
        self.assert_valid()
        if not self.can_write_position:
            raise ShadeAccessDisabled("Shade position writes are not permitted.")


def authorize_shade_access(
    config: TiltBridgeConfig,
    *,
    request_reads: bool,
    request_position_writes: bool,
) -> ShadeAccessPermit:
    """Require matching configuration and launch-time gates for live access."""

    if request_position_writes and not request_reads:
        raise ShadeAccessDisabled("Position writes require read access for verification.")
    if not request_reads or not config.access.allow_reads:
        raise ShadeAccessDisabled("Shade reads are disabled by configuration or launch flags.")
    if request_position_writes and not config.access.allow_position_writes:
        raise ShadeAccessDisabled("Shade position writes are disabled by configuration.")
    return ShadeAccessPermit(
        can_read=True,
        can_write_position=request_position_writes,
        _marker=_PERMIT_MARKER,
    )


def load_config(path: Path) -> TiltBridgeConfig:
    """Load a versioned JSON config and reject all unknown fields."""

    raw = _load_json_object(path)
    _require_keys(
        raw,
        required={"version", "mqtt", "shades"},
        optional={"access", "poll_interval_seconds", "command_cooldown_seconds"},
        context="bridge config",
    )
    version = _require_int(raw["version"], "version", minimum=1, maximum=1)
    access = _parse_access(raw.get("access", {}))
    mqtt = _parse_mqtt(_require_mapping(raw["mqtt"], "mqtt"))
    shade_values = raw["shades"]
    if not isinstance(shade_values, list) or not shade_values:
        raise TiltBridgeConfigError("shades must be a non-empty list.")
    shades = tuple(_parse_shade(value, index) for index, value in enumerate(shade_values))
    _require_unique_shades(shades)
    return TiltBridgeConfig(
        version=version,
        access=access,
        mqtt=mqtt,
        shades=shades,
        poll_interval_seconds=_require_int(
            raw.get("poll_interval_seconds", 120),
            "poll_interval_seconds",
            minimum=30,
            maximum=3600,
        ),
        command_cooldown_seconds=_require_int(
            raw.get("command_cooldown_seconds", 5),
            "command_cooldown_seconds",
            minimum=2,
            maximum=60,
        ),
    )


def load_pairing_key(path: Path) -> bytes:
    """Load one root/service-readable 32-byte pairing key encoded as hex."""

    value = load_secret(path, label="pairing key")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise TiltBridgeConfigError("Pairing key file must contain exactly 64 hex digits.")
    return bytes.fromhex(value)


def load_secret(path: Path, *, label: str) -> str:
    """Read one non-symlink secret that is not exposed to other users."""

    if not path.is_absolute():
        raise TiltBridgeConfigError(f"{label} path must be absolute.")
    try:
        info = path.lstat()
    except OSError as exc:
        raise TiltBridgeConfigError(f"Unable to read {label} file.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TiltBridgeConfigError(f"{label} path must be a regular non-symlink file.")
    if info.st_mode & (stat.S_IWGRP | stat.S_IRWXO):
        raise TiltBridgeConfigError(
            f"{label} file must not be group-writable or accessible by other users."
        )
    if info.st_uid not in {0, os.geteuid()}:
        raise TiltBridgeConfigError(f"{label} file has an unexpected owner.")
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise TiltBridgeConfigError(f"Unable to read {label} file.") from exc
    if not value or "\n" in value or "\r" in value:
        raise TiltBridgeConfigError(f"{label} file must contain one non-empty line.")
    return value


def _load_json_object(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TiltBridgeConfigError("Unable to load bridge configuration.") from exc
    return _require_mapping(value, "bridge config")


def _parse_access(value: object) -> BridgeAccessConfig:
    raw = _require_mapping(value, "access")
    _require_keys(
        raw,
        required=set(),
        optional={"allow_reads", "allow_position_writes"},
        context="access",
    )
    allow_reads = _require_bool(raw.get("allow_reads", False), "access.allow_reads")
    allow_writes = _require_bool(
        raw.get("allow_position_writes", False), "access.allow_position_writes"
    )
    if allow_writes and not allow_reads:
        raise TiltBridgeConfigError("Position writes cannot be enabled without reads.")
    return BridgeAccessConfig(
        allow_reads=allow_reads,
        allow_position_writes=allow_writes,
    )


def _parse_mqtt(raw: Mapping[str, Any]) -> MqttConfig:
    _require_keys(
        raw,
        required={"host", "username_file", "password_file"},
        optional={"port", "topic_prefix", "discovery_prefix", "keepalive_seconds"},
        context="mqtt",
    )
    host = _require_nonempty_string(raw["host"], "mqtt.host")
    if any(character.isspace() for character in host):
        raise TiltBridgeConfigError("mqtt.host must not contain whitespace.")
    return MqttConfig(
        host=host,
        port=_require_int(
            raw.get("port", 1883), "mqtt.port", minimum=1, maximum=65535
        ),
        username_file=_require_absolute_path(raw["username_file"], "mqtt.username_file"),
        password_file=_require_absolute_path(raw["password_file"], "mqtt.password_file"),
        topic_prefix=_require_topic_prefix(
            raw.get("topic_prefix", "tilt/local"), "mqtt.topic_prefix"
        ),
        discovery_prefix=_require_topic_prefix(
            raw.get("discovery_prefix", "homeassistant"), "mqtt.discovery_prefix"
        ),
        keepalive_seconds=_require_int(
            raw.get("keepalive_seconds", 60),
            "mqtt.keepalive_seconds",
            minimum=15,
            maximum=300,
        ),
    )


def _parse_shade(value: object, index: int) -> ShadeConfig:
    raw = _require_mapping(value, f"shades[{index}]")
    _require_keys(
        raw,
        required={"id", "name", "mac", "pairing_key_file"},
        optional=set(),
        context=f"shades[{index}]",
    )
    shade_id = _require_nonempty_string(raw["id"], f"shades[{index}].id")
    if not _ID_PATTERN.fullmatch(shade_id):
        raise TiltBridgeConfigError(
            f"shades[{index}].id must use lowercase letters, digits, and underscores."
        )
    name = _require_nonempty_string(raw["name"], f"shades[{index}].name")
    if len(name) > 80:
        raise TiltBridgeConfigError(f"shades[{index}].name is too long.")
    mac = _require_nonempty_string(raw["mac"], f"shades[{index}].mac").upper()
    if not _MAC_PATTERN.fullmatch(mac):
        raise TiltBridgeConfigError(f"shades[{index}].mac is not a canonical BLE address.")
    return ShadeConfig(
        id=shade_id,
        name=name,
        mac=mac,
        pairing_key_file=_require_absolute_path(
            raw["pairing_key_file"], f"shades[{index}].pairing_key_file"
        ),
    )


def _require_unique_shades(shades: tuple[ShadeConfig, ...]) -> None:
    ids = {shade.id for shade in shades}
    macs = {shade.mac for shade in shades}
    key_paths = {shade.pairing_key_file for shade in shades}
    if len(ids) != len(shades):
        raise TiltBridgeConfigError("Shade ids must be unique.")
    if len(macs) != len(shades):
        raise TiltBridgeConfigError("Shade BLE addresses must be unique.")
    if len(key_paths) != len(shades):
        raise TiltBridgeConfigError("Each shade must use a distinct pairing-key file.")


def _require_keys(
    raw: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str],
    context: str,
) -> None:
    missing = required - raw.keys()
    unknown = raw.keys() - required - optional
    if missing:
        raise TiltBridgeConfigError(
            f"{context} is missing required fields: {', '.join(sorted(missing))}."
        )
    if unknown:
        raise TiltBridgeConfigError(
            f"{context} contains unknown fields: {', '.join(sorted(unknown))}."
        )


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TiltBridgeConfigError(f"{label} must be a JSON object.")
    return value


def _require_nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise TiltBridgeConfigError(f"{label} must be a non-empty trimmed string.")
    return value


def _require_absolute_path(value: object, label: str) -> Path:
    path = Path(_require_nonempty_string(value, label))
    if not path.is_absolute():
        raise TiltBridgeConfigError(f"{label} must be an absolute path.")
    return path


def _require_topic_prefix(value: object, label: str) -> str:
    prefix = _require_nonempty_string(value, label).strip("/")
    if not prefix or prefix != value or "+" in prefix or "#" in prefix or "//" in prefix:
        raise TiltBridgeConfigError(f"{label} is not a valid fixed MQTT topic prefix.")
    return prefix


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise TiltBridgeConfigError(f"{label} must be true or false.")
    return value


def _require_int(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise TiltBridgeConfigError(f"{label} must be an integer from {minimum} to {maximum}.")
    return value
