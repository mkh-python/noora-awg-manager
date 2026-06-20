#!/bin/bash
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/mkh-python/noora-awg-manager/main"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

BOT_DIR="/opt/awg-bot"
BOT_SERVICE="/etc/systemd/system/awg-bot.service"
BACKUP_SERVICE="/etc/systemd/system/awg-full-backup.service"
BACKUP_TIMER="/etc/systemd/system/awg-full-backup.timer"
BACKUP_SCRIPT="/usr/local/bin/awg-full-backup.sh"
SEND_BACKUP_SCRIPT="/usr/local/bin/awg-send-backup-telegram.sh"
UPDATE_SCRIPT="/usr/local/bin/noora-awg-update.sh"

AWG_DIR="/etc/amnezia"
AWG_WEB_DIR="/etc/amneziawg-web"
AWG_WEB_DATA="/var/lib/amneziawg-web"

LICENSE_API_URL="http://194.5.192.122"
SUPPORT_USERNAME="@awgdeveloper"
INSTALL_MODE=""
SERVER_CONF=""
CLIENT_DIR=""
AWG_IFACE=""
AWG_PORT="64936"

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root."
    exit 1
  fi
}

install_deps() {
  export DEBIAN_FRONTEND=noninteractive
  apt update
  apt install -y \
    curl wget git python3-venv python3-pip qrencode sqlite3 \
    iptables-persistent netfilter-persistent nginx certbot python3-certbot-nginx \
    rsync tar gzip ca-certificates lsb-release
}

detect_manager_install() {
  [ -d "$BOT_DIR" ] || [ -f "$BOT_SERVICE" ]
}

create_local_backup_before_changes() {
  local ts out
  ts="$(date +%Y-%m-%d_%H-%M-%S)"
  out="/root/noora-awg-before-change-${ts}.tar.gz"

  echo
  echo "Creating safety backup before changes..."
  tar -czf "$out" \
    "$BOT_DIR" "$BOT_SERVICE" "$BACKUP_SERVICE" "$BACKUP_TIMER" \
    "$BACKUP_SCRIPT" "$SEND_BACKUP_SCRIPT" "$UPDATE_SCRIPT" \
    "$AWG_DIR" "$AWG_WEB_DIR" "$AWG_WEB_DATA" \
    2>/dev/null || true
  echo "Backup saved: $out"
}

ask_token_and_owner() {
  echo
  read -rp "Enter Telegram BOT_TOKEN: " BOT_TOKEN
  read -rp "Enter your Telegram numeric OWNER_ID: " OWNER_ID

  if [ -z "${BOT_TOKEN// }" ] || [ -z "${OWNER_ID// }" ]; then
    echo "BOT_TOKEN and OWNER_ID are required."
    exit 1
  fi

  SERVER_IP="$(curl -s --max-time 5 https://api.ipify.org || true)"
  if [ -z "$SERVER_IP" ]; then
    read -rp "Enter server public IP: " SERVER_IP
  fi
}

choose_new_install_mode() {
  echo
  echo "Choose installation mode:"
  echo
  echo "1) Full installation: install AmneziaWG server + Noora Bot"
  echo "2) Bot only: connect Noora Bot to an existing AmneziaWG server"
  echo
  read -rp "Enter choice [1-2]: " choice

  case "$choice" in
    1) INSTALL_MODE="full" ;;
    2) INSTALL_MODE="bot-only" ;;
    *) echo "Invalid choice."; exit 1 ;;
  esac
}

detect_main_iface() {
  ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}'
}

extract_listen_port() {
  awk -F= '
    /^[[:space:]]*ListenPort[[:space:]]*=/ {
      gsub(/[[:space:]]/, "", $2); print $2; exit
    }
  ' "$1"
}

find_awg_configs() {
  local candidates=()
  local path

  for path in \
    /etc/amnezia/amneziawg/awg0.conf \
    /etc/amnezia/amneziawg/*.conf \
    /etc/amneziawg/*.conf \
    /etc/wireguard/*.conf; do
    [ -f "$path" ] || continue
    grep -qE '^\[Interface\]' "$path" || continue
    candidates+=("$path")
  done

  printf '%s\n' "${candidates[@]}" | awk 'NF && !seen[$0]++'
}

detect_existing_awg() {
  echo
  echo "[AWG] Detecting existing AmneziaWG installation..."

  if ! command -v awg >/dev/null 2>&1; then
    echo "The 'awg' command was not found."
    echo "Install AmneziaWG first or choose full installation."
    exit 1
  fi

  if ! command -v awg-quick >/dev/null 2>&1; then
    echo "The 'awg-quick' command was not found."
    echo "Install amneziawg-tools first or choose full installation."
    exit 1
  fi

  mapfile -t configs < <(find_awg_configs)

  if [ "${#configs[@]}" -eq 0 ]; then
    echo "No existing AWG config was detected automatically."
    read -rp "Enter the full path to the existing AWG config: " SERVER_CONF
  elif [ "${#configs[@]}" -eq 1 ]; then
    SERVER_CONF="${configs[0]}"
  else
    echo "Multiple AWG configs were found:"
    local i
    for i in "${!configs[@]}"; do
      printf '%d) %s\n' "$((i + 1))" "${configs[$i]}"
    done
    read -rp "Select config [1-${#configs[@]}]: " selected
    if ! [[ "$selected" =~ ^[0-9]+$ ]] || [ "$selected" -lt 1 ] || [ "$selected" -gt "${#configs[@]}" ]; then
      echo "Invalid selection."
      exit 1
    fi
    SERVER_CONF="${configs[$((selected - 1))]}"
  fi

  if [ ! -f "$SERVER_CONF" ]; then
    echo "Config file does not exist: $SERVER_CONF"
    exit 1
  fi

  AWG_IFACE="$(basename "$SERVER_CONF" .conf)"
  CLIENT_DIR="$(dirname "$SERVER_CONF")/clients"
  AWG_PORT="$(extract_listen_port "$SERVER_CONF" || true)"
  AWG_PORT="${AWG_PORT:-64936}"

  mkdir -p "$CLIENT_DIR"
  chmod 700 "$CLIENT_DIR" 2>/dev/null || true

  echo "[AWG] Existing config: $SERVER_CONF"
  echo "[AWG] Interface:       $AWG_IFACE"
  echo "[AWG] Listen port:     $AWG_PORT"
  echo "[AWG] Client folder:   $CLIENT_DIR"

  if awg show "$AWG_IFACE" >/dev/null 2>&1; then
    echo "[AWG] Interface is active."
  else
    echo "[AWG] Warning: interface is not currently active."
    echo "[AWG] Existing config will not be modified or restarted during installation."
  fi
}

set_full_install_paths() {
  SERVER_CONF="/etc/amnezia/amneziawg/awg0.conf"
  CLIENT_DIR="/etc/amnezia/amneziawg/clients"
  AWG_IFACE="awg0"
  AWG_PORT="64936"
}

install_amneziawg_server_if_missing() {
  echo
  echo "[AWG] Checking AmneziaWG server..."

  set_full_install_paths

  if command -v awg >/dev/null 2>&1 && command -v awg-quick >/dev/null 2>&1 && [ -f "$SERVER_CONF" ]; then
    echo "[AWG] Existing AmneziaWG detected. Its config will be preserved."
    AWG_PORT="$(extract_listen_port "$SERVER_CONF" || true)"
    AWG_PORT="${AWG_PORT:-64936}"
    return 0
  fi

  echo "[AWG] Installing AmneziaWG server..."
  export DEBIAN_FRONTEND=noninteractive
  apt update
  apt install -y software-properties-common curl wget gnupg2 ca-certificates iptables-persistent netfilter-persistent

  if ! grep -R "amnezia" /etc/apt/sources.list /etc/apt/sources.list.d >/dev/null 2>&1; then
    add-apt-repository -y ppa:amnezia/ppa
  fi

  apt update
  apt install -y amneziawg amneziawg-tools

  mkdir -p "$CLIENT_DIR"
  chmod 700 /etc/amnezia /etc/amnezia/amneziawg "$CLIENT_DIR"

  local main_iface server_private_key server_public_key
  main_iface="$(detect_main_iface)"
  main_iface="${main_iface:-eth0}"
  server_private_key="$(awg genkey)"
  server_public_key="$(printf '%s' "$server_private_key" | awg pubkey)"

  cat > "$SERVER_CONF" <<CONF
[Interface]
PrivateKey = ${server_private_key}
Address = 10.66.66.1/24, fd42:42:42::1/64
ListenPort = ${AWG_PORT}
Jc = 5
Jmin = 50
Jmax = 1000
S1 = 84
S2 = 69
S3 = 107
S4 = 72
H1 = 184145801
H2 = 896102974
H3 = 1412846426
H4 = 1681983794
CONF
  chmod 600 "$SERVER_CONF"

  cat > /etc/sysctl.d/99-noora-awg.conf <<SYSCTL
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
SYSCTL
  sysctl --system >/dev/null || true

  iptables -C INPUT -p udp --dport "$AWG_PORT" -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p udp --dport "$AWG_PORT" -j ACCEPT
  iptables -C FORWARD -i "$AWG_IFACE" -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -i "$AWG_IFACE" -j ACCEPT
  iptables -C FORWARD -o "$AWG_IFACE" -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -o "$AWG_IFACE" -j ACCEPT
  iptables -t nat -C POSTROUTING -s 10.66.66.0/24 -o "$main_iface" -j MASQUERADE 2>/dev/null || \
    iptables -t nat -I POSTROUTING 1 -s 10.66.66.0/24 -o "$main_iface" -j MASQUERADE
  netfilter-persistent save >/dev/null 2>&1 || true

  systemctl daemon-reload
  systemctl enable --now "awg-quick@${AWG_IFACE}"

  echo "[AWG] Installed interface: $AWG_IFACE"
  echo "[AWG] Port: $AWG_PORT"
  echo "[AWG] Public key: $server_public_key"
}

write_config_fresh() {
  mkdir -p "$BOT_DIR"
  cat > "$BOT_DIR/config.env" <<ENV
BOT_TOKEN=${BOT_TOKEN}
OWNER_ID=${OWNER_ID}
ADMINS=${OWNER_ID}
SERVER_ENDPOINT=${SERVER_IP}
AWG_PORT=${AWG_PORT}
AWG_IFACE=${AWG_IFACE}
SERVER_CONF=${SERVER_CONF}
CLIENT_DIR=${CLIENT_DIR}
DNS=1.1.1.1,1.0.0.1
BACKUP_CHAT_ID=
BACKUP_LINK=
GITHUB_REPO=mkh-python/noora-awg-manager
GITHUB_BRANCH=main
LICENSE_REQUIRED=1
LICENSE_API_URL=${LICENSE_API_URL}
SUPPORT_USERNAME=${SUPPORT_USERNAME}
BACKUP_TIMES_PER_DAY=1
BACKUP_TIMES=00:00
PANEL_URL=
PANEL_DOMAIN=
ENV
  chmod 600 "$BOT_DIR/config.env"
}

install_file() {
  local local_rel="$1" remote_rel="$2" target="$3" required="${4:-yes}"
  mkdir -p "$(dirname "$target")"

  if [ -f "$SCRIPT_DIR/$local_rel" ]; then
    cp "$SCRIPT_DIR/$local_rel" "$target"
    return 0
  fi

  if wget -qO "$target" "$REPO_RAW/$remote_rel"; then
    return 0
  fi

  rm -f "$target"
  if [ "$required" = "yes" ]; then
    echo "Failed to install required file: $remote_rel"
    exit 1
  fi
  return 1
}

download_manager_files() {
  echo
  echo "Installing Noora AWG Manager files..."
  mkdir -p "$BOT_DIR" /usr/local/bin

  install_file "bot/bot.py" "bot/bot.py" "$BOT_DIR/bot.py"
  install_file "bot/requirements.txt" "bot/requirements.txt" "$BOT_DIR/requirements.txt"
  install_file "VERSION" "VERSION" "$BOT_DIR/VERSION"

  if [ ! -x "$BOT_DIR/venv/bin/python" ]; then
    python3 -m venv "$BOT_DIR/venv"
  fi
  "$BOT_DIR/venv/bin/pip" install --upgrade pip
  "$BOT_DIR/venv/bin/pip" install -r "$BOT_DIR/requirements.txt"

  install_file "scripts/backup.sh" "scripts/backup.sh" "$BACKUP_SCRIPT"
  chmod +x "$BACKUP_SCRIPT"

  if install_file "scripts/send-backup.sh" "scripts/send-backup.sh" "$SEND_BACKUP_SCRIPT" no; then
    chmod +x "$SEND_BACKUP_SCRIPT"
  fi

  install_file "scripts/update.sh" "scripts/update.sh" "$UPDATE_SCRIPT"
  chmod +x "$UPDATE_SCRIPT"

  install_file "systemd/awg-bot.service" "systemd/awg-bot.service" "$BOT_SERVICE"
  install_file "systemd/awg-full-backup.service" "systemd/awg-full-backup.service" "$BACKUP_SERVICE"
  install_file "systemd/awg-full-backup.timer" "systemd/awg-full-backup.timer" "$BACKUP_TIMER"
}

stop_manager_services() {
  systemctl stop awg-bot 2>/dev/null || true
  systemctl stop awg-full-backup.timer 2>/dev/null || true
  systemctl stop awg-full-backup.service 2>/dev/null || true
}

start_services() {
  systemctl daemon-reload
  systemctl enable --now awg-bot
  if [ -f "$BACKUP_TIMER" ]; then
    systemctl enable --now awg-full-backup.timer >/dev/null 2>&1 || true
  fi
}

new_install() {
  choose_new_install_mode
  install_deps
  ask_token_and_owner

  if [ "$INSTALL_MODE" = "full" ]; then
    install_amneziawg_server_if_missing
  else
    detect_existing_awg
  fi

  download_manager_files
  write_config_fresh
  python3 -m py_compile "$BOT_DIR/bot.py"
  start_services

  echo
  echo "Installation completed."
  echo "Mode: $INSTALL_MODE"
  echo "AWG config: $SERVER_CONF"
  echo "Open your Telegram bot and send /start"
}

update_install() {
  echo
  echo "Updating existing Noora AWG Manager..."
  echo "Existing AWG configuration, users and peers will be preserved."

  create_local_backup_before_changes
  install_deps
  stop_manager_services

  if [ ! -f "$BOT_DIR/config.env" ]; then
    echo "config.env not found; detecting AWG and creating a new bot config."
    ask_token_and_owner
    detect_existing_awg
    write_config_fresh
  fi

  download_manager_files
  python3 -m py_compile "$BOT_DIR/bot.py"
  start_services
  systemctl status awg-bot --no-pager || true
}

reinstall_manager_only() {
  echo
  echo "Reinstalling Noora Bot only..."
  echo "Existing AWG configuration, keys, users and peers will be preserved."

  create_local_backup_before_changes
  install_deps
  stop_manager_services

  local old_config="/root/noora-awg-config.env.bak"
  [ ! -f "$BOT_DIR/config.env" ] || cp "$BOT_DIR/config.env" "$old_config"

  rm -rf "$BOT_DIR"
  rm -f "$BOT_SERVICE" "$BACKUP_SERVICE" "$BACKUP_TIMER" "$BACKUP_SCRIPT" "$SEND_BACKUP_SCRIPT"
  download_manager_files

  if [ -f "$old_config" ]; then
    cp "$old_config" "$BOT_DIR/config.env"
  else
    ask_token_and_owner
    detect_existing_awg
    write_config_fresh
  fi

  python3 -m py_compile "$BOT_DIR/bot.py"
  start_services
  systemctl status awg-bot --no-pager || true
}

full_wipe_reinstall() {
  echo
  echo "WARNING: this removes Noora Bot data and the default Noora-managed AWG config."
  echo "It can delete VPN users and server settings."
  read -rp "Type DELETE to continue: " confirm
  [ "$confirm" = "DELETE" ] || { echo "Cancelled."; exit 0; }

  create_local_backup_before_changes
  stop_manager_services
  systemctl stop amneziawg-web 2>/dev/null || true
  systemctl stop awg-quick@awg0 2>/dev/null || true

  rm -rf "$BOT_DIR" "$AWG_DIR" "$AWG_WEB_DIR" "$AWG_WEB_DATA"
  rm -f "$BOT_SERVICE" "$BACKUP_SERVICE" "$BACKUP_TIMER" "$BACKUP_SCRIPT" "$SEND_BACKUP_SCRIPT"
  systemctl daemon-reload
  new_install
}

show_existing_menu() {
  echo
  echo "Existing Noora Bot installation detected."
  echo
  echo "1) Update bot and scripts; preserve all configs and AWG users"
  echo "2) Reinstall bot only; preserve existing AWG installation"
  echo "3) Full wipe and reinstall"
  echo "4) Exit"
  echo
  read -rp "Enter choice [1-4]: " choice

  case "$choice" in
    1) update_install ;;
    2) reinstall_manager_only ;;
    3) full_wipe_reinstall ;;
    4) exit 0 ;;
    *) echo "Invalid choice."; exit 1 ;;
  esac
}

main() {
  require_root
  echo "Noora AWG Manager Installer v1.2.0"

  if detect_manager_install; then
    show_existing_menu
  else
    new_install
  fi
}

main "$@"
