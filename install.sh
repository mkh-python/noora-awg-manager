#!/bin/bash
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/mkh-python/noora-awg-manager/main"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root."
  exit 1
fi

echo "Noora AWG Manager Installer"
echo

apt update
apt install -y curl wget git python3-venv python3-pip qrencode sqlite3 iptables-persistent netfilter-persistent nginx certbot python3-certbot-nginx rsync tar gzip

mkdir -p /opt/awg-bot /usr/local/bin

echo
read -rp "Enter Telegram BOT_TOKEN: " BOT_TOKEN
read -rp "Enter your Telegram numeric OWNER_ID: " OWNER_ID

SERVER_IP="$(curl -s --max-time 5 https://api.ipify.org || true)"
if [ -z "$SERVER_IP" ]; then
  read -rp "Enter server public IP: " SERVER_IP
fi

echo
echo "[1/5] Installing bot files..."

wget -qO /opt/awg-bot/bot.py "$REPO_RAW/bot/bot.py"
wget -qO /opt/awg-bot/requirements.txt "$REPO_RAW/bot/requirements.txt"

python3 -m venv /opt/awg-bot/venv
/opt/awg-bot/venv/bin/pip install -r /opt/awg-bot/requirements.txt

cat > /opt/awg-bot/config.env <<ENV
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

echo
echo "[2/5] Installing backup scripts..."

wget -qO /usr/local/bin/awg-full-backup.sh "$REPO_RAW/scripts/backup.sh"
chmod +x /usr/local/bin/awg-full-backup.sh

if wget -qO /usr/local/bin/awg-send-backup-telegram.sh "$REPO_RAW/scripts/send-backup.sh"; then
  chmod +x /usr/local/bin/awg-send-backup-telegram.sh
fi

echo
echo "[3/5] Installing systemd services..."

wget -qO /etc/systemd/system/awg-bot.service "$REPO_RAW/systemd/awg-bot.service"
wget -qO /etc/systemd/system/awg-full-backup.service "$REPO_RAW/systemd/awg-full-backup.service"
wget -qO /etc/systemd/system/awg-full-backup.timer "$REPO_RAW/systemd/awg-full-backup.timer"

systemctl daemon-reload
systemctl enable --now awg-bot

echo
echo "[4/5] Checks..."

systemctl status awg-bot --no-pager || true

echo
echo "[5/5] Done."
echo
echo "Go to your Telegram bot and send:"
echo "/start"
echo
echo "Owner ID:"
echo "$OWNER_ID"
echo
echo "Server IP:"
echo "$SERVER_IP"
