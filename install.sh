#!/bin/bash
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/mkh-python/noora-awg-manager/main"

BOT_DIR="/opt/awg-bot"
BOT_SERVICE="/etc/systemd/system/awg-bot.service"
BACKUP_SERVICE="/etc/systemd/system/awg-full-backup.service"
BACKUP_TIMER="/etc/systemd/system/awg-full-backup.timer"
BACKUP_SCRIPT="/usr/local/bin/awg-full-backup.sh"
SEND_BACKUP_SCRIPT="/usr/local/bin/awg-send-backup-telegram.sh"

AWG_DIR="/etc/amnezia"
AWG_WEB_DIR="/etc/amneziawg-web"
AWG_WEB_DATA="/var/lib/amneziawg-web"

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

detect_existing_install() {
  if [ -d "$BOT_DIR" ] || [ -f "$BOT_SERVICE" ] || [ -d "$AWG_DIR" ] || [ -d "$AWG_WEB_DIR" ]; then
    return 0
  fi
  return 1
}

create_local_backup_before_changes() {
  local ts
  ts="$(date +%Y-%m-%d_%H-%M-%S)"
  local out="/root/noora-awg-before-change-${ts}.tar.gz"

  echo
  echo "Creating safety backup before changes..."
  tar -czf "$out" \
    "$BOT_DIR" \
    "$BOT_SERVICE" \
    "$BACKUP_SERVICE" \
    "$BACKUP_TIMER" \
    "$BACKUP_SCRIPT" \
    "$SEND_BACKUP_SCRIPT" \
    "$AWG_DIR" \
    "$AWG_WEB_DIR" \
    "$AWG_WEB_DATA" \
    2>/dev/null || true

  echo "Backup saved:"
  echo "$out"
}

ask_token_and_owner() {
  echo
  read -rp "Enter Telegram BOT_TOKEN: " BOT_TOKEN
  read -rp "Enter your Telegram numeric OWNER_ID: " OWNER_ID

  SERVER_IP="$(curl -s --max-time 5 https://api.ipify.org || true)"
  if [ -z "$SERVER_IP" ]; then
    read -rp "Enter server public IP: " SERVER_IP
  fi
}

write_config_fresh() {
  mkdir -p "$BOT_DIR"

  cat > "$BOT_DIR/config.env" <<ENV
BOT_TOKEN=${BOT_TOKEN}
ADMINS=${OWNER_ID}
SERVER_ENDPOINT=${SERVER_IP}
AWG_PORT=64936
AWG_IFACE=awg0
SERVER_CONF=/etc/amnezia/amneziawg/awg0.conf
CLIENT_DIR=/etc/amnezia/amneziawg/clients
DNS=1.1.1.1,1.0.0.1
BACKUP_CHAT_ID=
BACKUP_LINK=
BACKUP_TIMES_PER_DAY=1
BACKUP_TIMES=00:00
PANEL_URL=
PANEL_DOMAIN=
ENV
}

download_manager_files() {
  echo
  echo "Downloading Noora AWG Manager files..."

  mkdir -p "$BOT_DIR" /usr/local/bin

  wget -qO "$BOT_DIR/bot.py" "$REPO_RAW/bot/bot.py"
  wget -qO "$BOT_DIR/requirements.txt" "$REPO_RAW/bot/requirements.txt"

  python3 -m venv "$BOT_DIR/venv"
  "$BOT_DIR/venv/bin/pip" install --upgrade pip
  "$BOT_DIR/venv/bin/pip" install -r "$BOT_DIR/requirements.txt"

  wget -qO "$BACKUP_SCRIPT" "$REPO_RAW/scripts/backup.sh"
  chmod +x "$BACKUP_SCRIPT"

  if wget -qO "$SEND_BACKUP_SCRIPT" "$REPO_RAW/scripts/send-backup.sh"; then
    chmod +x "$SEND_BACKUP_SCRIPT"
  fi

  wget -qO "$BOT_SERVICE" "$REPO_RAW/systemd/awg-bot.service"
  wget -qO "$BACKUP_SERVICE" "$REPO_RAW/systemd/awg-full-backup.service"
  wget -qO "$BACKUP_TIMER" "$REPO_RAW/systemd/awg-full-backup.timer"
}

start_services() {
  systemctl daemon-reload
  systemctl enable --now awg-bot

  if [ -f "$BACKUP_TIMER" ]; then
    systemctl enable awg-full-backup.timer >/dev/null 2>&1 || true
  fi
}

stop_manager_services() {
  systemctl stop awg-bot 2>/dev/null || true
  systemctl stop awg-full-backup.timer 2>/dev/null || true
  systemctl stop awg-full-backup.service 2>/dev/null || true
}


detect_main_iface() {
  ip route get 1.1.1.1 2>/dev/null | awk '
    {
      for (i=1;i<=NF;i++) {
        if ($i=="dev") {
          print $(i+1)
          exit
        }
      }
    }'
}

install_amneziawg_server_if_missing() {
  echo
  echo "[AWG] Checking AmneziaWG server..."

  if command -v awg >/dev/null 2>&1 && [ -f /etc/amnezia/amneziawg/awg0.conf ]; then
    echo "[AWG] AmneziaWG already installed. Skipping."
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

  mkdir -p /etc/amnezia/amneziawg/clients
  chmod 700 /etc/amnezia /etc/amnezia/amneziawg /etc/amnezia/amneziawg/clients

  AWG_IFACE_NAME="awg0"
  AWG_PORT_VALUE="${AWG_PORT:-64936}"
  MAIN_IFACE="$(detect_main_iface)"
  if [ -z "$MAIN_IFACE" ]; then
    MAIN_IFACE="eth0"
  fi

  SERVER_PRIVATE_KEY="$(awg genkey)"
  SERVER_PUBLIC_KEY="$(printf '%s' "$SERVER_PRIVATE_KEY" | awg pubkey)"

  Jc=5
  Jmin=50
  Jmax=1000
  S1=84
  S2=69
  S3=107
  S4=72
  H1=184145801
  H2=896102974
  H3=1412846426
  H4=1681983794

  cat > /etc/amnezia/amneziawg/awg0.conf <<CONF
[Interface]
PrivateKey = ${SERVER_PRIVATE_KEY}
Address = 10.66.66.1/24, fd42:42:42::1/64
ListenPort = ${AWG_PORT_VALUE}
Jc = ${Jc}
Jmin = ${Jmin}
Jmax = ${Jmax}
S1 = ${S1}
S2 = ${S2}
S3 = ${S3}
S4 = ${S4}
H1 = ${H1}
H2 = ${H2}
H3 = ${H3}
H4 = ${H4}
CONF

  chmod 600 /etc/amnezia/amneziawg/awg0.conf

  cat > /etc/sysctl.d/99-noora-awg.conf <<SYSCTL
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
SYSCTL

  sysctl --system >/dev/null || true

  iptables -C INPUT -p udp --dport "${AWG_PORT_VALUE}" -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p udp --dport "${AWG_PORT_VALUE}" -j ACCEPT
  iptables -C FORWARD -i awg0 -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -i awg0 -j ACCEPT
  iptables -C FORWARD -o awg0 -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -o awg0 -j ACCEPT
  iptables -t nat -C POSTROUTING -s 10.66.66.0/24 -o "${MAIN_IFACE}" -j MASQUERADE 2>/dev/null || iptables -t nat -I POSTROUTING 1 -s 10.66.66.0/24 -o "${MAIN_IFACE}" -j MASQUERADE

  netfilter-persistent save >/dev/null 2>&1 || true

  systemctl daemon-reload
  systemctl enable --now awg-quick@awg0

  echo
  echo "[AWG] AmneziaWG installed."
  echo "[AWG] Interface: awg0"
  echo "[AWG] Port: ${AWG_PORT_VALUE}"
  echo "[AWG] Public key: ${SERVER_PUBLIC_KEY}"
}


fresh_install() {
  echo
  echo "Starting fresh install..."

  install_deps
  ask_token_and_owner
  install_amneziawg_server_if_missing
  download_manager_files
  write_config_fresh
  start_services

  echo
  echo "Done."
  echo "Open your Telegram bot and send: /start"
}

update_install() {
  echo
  echo "Updating existing Noora AWG Manager..."
  echo "Configs, users, database and AmneziaWG settings will be preserved."

  create_local_backup_before_changes
  install_deps
  install_amneziawg_server_if_missing
  stop_manager_services

  if [ ! -f "$BOT_DIR/config.env" ]; then
    echo
    echo "config.env not found. Telegram token and owner ID are required."
    ask_token_and_owner
    write_config_fresh
  fi

  download_manager_files
  start_services

  echo
  echo "Update completed."
  systemctl status awg-bot --no-pager || true
}

reinstall_manager_only() {
  echo
  echo "Reinstalling Noora Manager only..."
  echo "AmneziaWG server, users and configs will be preserved."

  create_local_backup_before_changes
  install_deps
  install_amneziawg_server_if_missing
  stop_manager_services

  local old_config="/root/noora-awg-config.env.bak"
  if [ -f "$BOT_DIR/config.env" ]; then
    cp "$BOT_DIR/config.env" "$old_config"
  fi

  rm -rf "$BOT_DIR"
  rm -f "$BOT_SERVICE" "$BACKUP_SERVICE" "$BACKUP_TIMER" "$BACKUP_SCRIPT" "$SEND_BACKUP_SCRIPT"

  download_manager_files

  if [ -f "$old_config" ]; then
    cp "$old_config" "$BOT_DIR/config.env"
  else
    ask_token_and_owner
    write_config_fresh
  fi

  start_services

  echo
  echo "Manager reinstall completed."
  systemctl status awg-bot --no-pager || true
}

full_wipe_reinstall() {
  echo
  echo "WARNING: Full wipe will remove Noora Manager, bot data, AmneziaWG config and web panel data."
  echo "This can delete VPN users and server settings."
  echo
  read -rp "Type DELETE to continue: " CONFIRM

  if [ "$CONFIRM" != "DELETE" ]; then
    echo "Cancelled."
    exit 0
  fi

  create_local_backup_before_changes

  stop_manager_services
  systemctl stop amneziawg-web 2>/dev/null || true
  systemctl stop awg-quick@awg0 2>/dev/null || true

  rm -rf "$BOT_DIR"
  rm -rf "$AWG_DIR" "$AWG_WEB_DIR" "$AWG_WEB_DATA"
  rm -f "$BOT_SERVICE" "$BACKUP_SERVICE" "$BACKUP_TIMER"
  rm -f "$BACKUP_SCRIPT" "$SEND_BACKUP_SCRIPT"

  systemctl daemon-reload

  echo
  echo "Old installation removed."
  echo "Starting clean install..."

  fresh_install
}

show_existing_menu() {
  echo
  echo "Existing installation detected."
  echo
  echo "Choose what you want to do:"
  echo
  echo "1) Update"
  echo "   Update bot and scripts only. Keep configs, users and database."
  echo
  echo "2) Reinstall Manager"
  echo "   Reinstall bot and Noora services. Keep AmneziaWG users and configs."
  echo
  echo "3) Full Wipe + Reinstall"
  echo "   Delete Noora Manager, AmneziaWG config, web panel data and reinstall."
  echo
  echo "4) Exit"
  echo

  read -rp "Enter choice [1-4]: " choice

  case "$choice" in
    1)
      update_install
      ;;
    2)
      reinstall_manager_only
      ;;
    3)
      full_wipe_reinstall
      ;;
    4)
      echo "Exit."
      exit 0
      ;;
    *)
      echo "Invalid choice."
      exit 1
      ;;
  esac
}

main() {
  require_root

  echo "Noora AWG Manager Installer"
  echo

  if detect_existing_install; then
    show_existing_menu
  else
    fresh_install
  fi
}

main "$@"
