#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
activate=0
install_packages=0
enable_service=0
allow_writes=0
service_user="tiltbridge"
config_path="/etc/tilt-local-bridge/bridge.json"
install_root="/opt/tilt-local-bridge"

usage() {
  cat <<'EOF'
Usage: sudo scripts/install.sh --activate [options]

Install Tilt Local Bridge on the Raspberry Pi running this command.

Options:
  --activate                 Required before the script changes the system
  --install-system-packages  Install BlueZ and Python dependencies with apt
  --enable                   Enable and start the service after validation
  --allow-position-writes    Add the launch-time write gate to the service
  --config PATH              Config path (default: /etc/tilt-local-bridge/bridge.json)
  --service-user USER        Service account (default: tiltbridge)
  --help                     Show this help

Without --enable, the unit is installed but left disabled and stopped.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --activate) activate=1 ;;
    --install-system-packages) install_packages=1 ;;
    --enable) enable_service=1 ;;
    --allow-position-writes) allow_writes=1 ;;
    --config)
      [[ $# -ge 2 ]] || { echo "error: --config requires a path" >&2; exit 2; }
      config_path="$2"
      shift
      ;;
    --service-user)
      [[ $# -ge 2 ]] || { echo "error: --service-user requires a user" >&2; exit 2; }
      service_user="$2"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "$activate" != "1" ]]; then
  echo "Dry run only. Re-run with --activate to install Tilt Local Bridge."
  echo "Service will be left disabled unless --enable is also supplied."
  exit 0
fi
if [[ "${EUID}" -ne 0 ]]; then
  echo "error: installation requires sudo" >&2
  exit 2
fi
if [[ ! "$service_user" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
  echo "error: --service-user is not a valid Linux account name" >&2
  exit 2
fi
if [[ "$config_path" != /* || "$config_path" =~ [[:space:]] ]]; then
  echo "error: --config must be an absolute path without whitespace" >&2
  exit 2
fi

if [[ "$install_packages" == "1" ]]; then
  apt-get update
  apt-get install -y \
    bluetooth bluez \
    python3-bleak python3-cryptography python3-paho-mqtt
fi

if ! id -u "$service_user" >/dev/null 2>&1; then
  useradd --system --home-dir /nonexistent --no-create-home \
    --shell /usr/sbin/nologin "$service_user"
fi

install -d -o root -g "$service_user" -m 0750 /etc/tilt-local-bridge
install -d -o root -g "$service_user" -m 0750 /etc/tilt-local-bridge/keys
install -d -o root -g root -m 0755 "$install_root/src"
rm -rf "$install_root/src/tilt_local_bridge"
cp -a "$repo_root/src/tilt_local_bridge" "$install_root/src/tilt_local_bridge"
chown -R root:root "$install_root/src/tilt_local_bridge"
find "$install_root/src/tilt_local_bridge" -type d -exec chmod 0755 {} +
find "$install_root/src/tilt_local_bridge" -type f -exec chmod 0644 {} +

if [[ ! -e "$config_path" ]]; then
  install -o root -g "$service_user" -m 0640 \
    "$repo_root/examples/bridge.example.json" "$config_path"
  echo "Installed an example config at $config_path. Replace its sample values."
fi

runtime_flags=(--expect-shade-reads)
serve_flags=(--allow-shade-reads)
if [[ "$allow_writes" == "1" ]]; then
  runtime_flags+=(--expect-position-writes)
  serve_flags+=(--allow-position-writes)
fi
runtime_flags_string="${runtime_flags[*]}"
serve_flags_string="${serve_flags[*]}"

supplementary_groups=""
if getent group bluetooth >/dev/null; then
  supplementary_groups="SupplementaryGroups=bluetooth"
fi

cat >/etc/systemd/system/tilt-local-bridge.service <<EOF
[Unit]
Description=Tilt roller shade BLE to Home Assistant MQTT bridge
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target
ConditionPathExists=$config_path

[Service]
Type=simple
User=$service_user
$supplementary_groups
WorkingDirectory=$install_root
Environment=PYTHONPATH=$install_root/src
ExecStartPre=/usr/bin/python3 -m tilt_local_bridge.tilt_bridge --config $config_path check-runtime $runtime_flags_string
ExecStart=/usr/bin/python3 -m tilt_local_bridge.tilt_bridge --config $config_path serve $serve_flags_string
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
UMask=0077

NoNewPrivileges=true
PrivateDevices=true
PrivateTmp=true
ProtectClock=true
ProtectControlGroups=true
ProtectHome=read-only
ProtectHostname=true
ProtectKernelLogs=true
ProtectKernelModules=true
ProtectKernelTunables=true
ProtectProc=invisible
ProtectSystem=strict
ProcSubset=pid
RemoveIPC=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_BLUETOOTH
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallArchitectures=native
SystemCallFilter=~@clock @cpu-emulation @debug @module @mount @obsolete @privileged @raw-io @reboot @resources @swap
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl reset-failed tilt-local-bridge.service >/dev/null 2>&1 || true

if [[ "$enable_service" == "1" ]]; then
  sudo -u "$service_user" env PYTHONPATH="$install_root/src" \
    /usr/bin/python3 -m tilt_local_bridge.tilt_bridge \
    --config "$config_path" check-runtime "${runtime_flags[@]}"
  systemctl enable --now tilt-local-bridge.service
else
  systemctl disable tilt-local-bridge.service >/dev/null 2>&1 || true
  systemctl stop tilt-local-bridge.service >/dev/null 2>&1 || true
fi

systemctl is-enabled tilt-local-bridge.service 2>/dev/null || true
systemctl is-active tilt-local-bridge.service 2>/dev/null || true
