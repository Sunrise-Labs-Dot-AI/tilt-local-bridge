#!/usr/bin/env python3
"""Fail when public source contains likely private-home or secret material."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SKIP_PARTS = {".git", ".venv", "venv", "build", "dist", "__pycache__"}
TEXT_SUFFIXES = {
    "",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
ALLOWED_EMAIL_DOMAINS = {"example.com"}
ALLOWED_MAC_PREFIXES = {"02:00:00:00:00:", "AA:BB:CC:DD:EE:"}

EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.IGNORECASE)
MAC = re.compile(r"\b(?:[0-9A-F]{2}:){5}[0-9A-F]{2}\b", re.IGNORECASE)
PRIVATE_IPV4 = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)
DENY = {
    "absolute macOS home path": re.compile(r"/Users/"),
    "Tailscale hostname": re.compile(r"\b[a-z0-9-]+\.ts\.net\b", re.IGNORECASE),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "private project residue": re.compile(
        r"\b(?:tars|kanata|master_bedroom|master bedroom)\b", re.IGNORECASE
    ),
}


def files_to_scan() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.resolve() == SELF:
            continue
        if any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return sorted(files)


def scan(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError:
        return []
    relative = path.relative_to(ROOT)
    failures: list[str] = []
    for label, pattern in DENY.items():
        if pattern.search(text):
            failures.append(f"{relative}: {label}")
    if PRIVATE_IPV4.search(text):
        failures.append(f"{relative}: private IPv4 address")
    for match in EMAIL.finditer(text):
        if match.group(1).lower() not in ALLOWED_EMAIL_DOMAINS:
            failures.append(f"{relative}: non-example email address")
    for match in MAC.finditer(text):
        value = match.group(0).upper()
        if not any(value.startswith(prefix) for prefix in ALLOWED_MAC_PREFIXES):
            failures.append(f"{relative}: non-example BLE address")
    return failures


def main() -> int:
    failures = [failure for path in files_to_scan() for failure in scan(path)]
    if failures:
        print("Public-tree privacy guard failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Public-tree privacy guard passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
