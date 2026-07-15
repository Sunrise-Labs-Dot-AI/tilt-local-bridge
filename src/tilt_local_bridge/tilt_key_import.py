"""Offline import of Tilt pairing keys from a protected cloud-store export."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tilt_bridge_config import (
    ShadeConfig,
    TiltBridgeConfig,
    TiltBridgeConfigError,
    load_pairing_key,
)


_HEX_32_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX_MAC_PATTERN = re.compile(r"^[0-9a-fA-F]{12}$")
_MAX_STORE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class KeyImportResult:
    imported_shade_ids: tuple[str, ...]
    unchanged_shade_ids: tuple[str, ...]


def import_pairing_keys(
    store_path: Path,
    config: TiltBridgeConfig,
    *,
    replace_existing: bool = False,
) -> KeyImportResult:
    """Match configured MACs and atomically write keys without displaying them."""

    store = _load_protected_store(store_path)
    shades_by_mac = {shade.mac: shade for shade in config.shades}
    keys_by_mac: dict[str, str] = {}
    for candidate in _walk_mappings(store):
        mac = _normalize_mac(candidate.get("id"))
        pairing_key = candidate.get("pairingKey")
        if mac not in shades_by_mac or not isinstance(pairing_key, str):
            continue
        shade_id = shades_by_mac[mac].id
        if not _HEX_32_PATTERN.fullmatch(pairing_key):
            raise TiltBridgeConfigError(
                f"Cloud record has an invalid pairing key for configured shade {shade_id}."
            )
        normalized_key = pairing_key.lower()
        previous = keys_by_mac.setdefault(mac, normalized_key)
        if previous != normalized_key:
            raise TiltBridgeConfigError(
                f"Cloud record has conflicting pairing keys for configured shade {shade_id}."
            )

    missing = [shade.id for shade in config.shades if shade.mac not in keys_by_mac]
    if missing:
        raise TiltBridgeConfigError(
            "Cloud record is missing configured shades: " + ", ".join(sorted(missing))
        )

    imported: list[str] = []
    unchanged: list[str] = []
    for shade in config.shades:
        changed = _write_pairing_key(
            shade,
            keys_by_mac[shade.mac],
            replace_existing=replace_existing,
        )
        (imported if changed else unchanged).append(shade.id)
    return KeyImportResult(tuple(imported), tuple(unchanged))


def _load_protected_store(path: Path) -> Any:
    if not path.is_absolute():
        raise TiltBridgeConfigError("Cloud-store export path must be absolute.")
    try:
        info = path.lstat()
    except OSError as exc:
        raise TiltBridgeConfigError("Unable to read cloud-store export.") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise TiltBridgeConfigError("Cloud-store export must be a regular non-symlink file.")
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise TiltBridgeConfigError(
            "Cloud-store export must not be accessible by group or other users."
        )
    if info.st_uid not in {0, os.geteuid()}:
        raise TiltBridgeConfigError("Cloud-store export has an unexpected owner.")
    if info.st_size > _MAX_STORE_BYTES:
        raise TiltBridgeConfigError("Cloud-store export is unexpectedly large.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TiltBridgeConfigError("Unable to parse cloud-store export.") from exc


def _walk_mappings(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def _normalize_mac(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    compact = re.sub(r"[:-]", "", value)
    if not _HEX_MAC_PATTERN.fullmatch(compact):
        return None
    compact = compact.upper()
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def _write_pairing_key(
    shade: ShadeConfig,
    key_hex: str,
    *,
    replace_existing: bool,
) -> bool:
    path = shade.pairing_key_file
    parent = path.parent
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        raise TiltBridgeConfigError(
            f"Pairing key directory is unavailable for configured shade {shade.id}."
        ) from exc
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise TiltBridgeConfigError(
            f"Pairing key directory is unsafe for configured shade {shade.id}."
        )
    if parent_info.st_uid not in {0, os.geteuid()}:
        raise TiltBridgeConfigError(
            f"Pairing key directory has an unexpected owner for configured shade {shade.id}."
        )

    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise TiltBridgeConfigError(
            f"Unable to inspect pairing key file for configured shade {shade.id}."
        ) from exc
    else:
        existing = load_pairing_key(path).hex()
        if secrets.compare_digest(existing, key_hex):
            return False
        if not replace_existing:
            raise TiltBridgeConfigError(
                f"Pairing key already exists for configured shade {shade.id}; "
                "replacement was not approved."
            )

    temporary = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        _write_all(descriptor, (key_hex + "\n").encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        path.chmod(0o600)
    except OSError as exc:
        raise TiltBridgeConfigError(
            f"Unable to write pairing key for configured shade {shade.id}."
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return True


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("Pairing key write made no progress.")
        remaining = remaining[written:]
