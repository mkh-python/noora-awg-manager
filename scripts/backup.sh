#!/bin/bash
set -euo pipefail

DATE="$(date +%Y-%m-%d_%H-%M-%S)"
BACKUP_BASE="/var/backups/awg-full"
TMP="$(mktemp -d)"
OUT="${BACKUP_BASE}/awg-full-backup-${DATE}.tar.gz"

mkdir -p "$BACKUP_BASE"
mkdir -p "$TMP/rootfs" "$TMP/metadata"

copy_path() {
  local p="$1"
  if [ -e "$p" ]; then
    cp -a --parents "$p" "$TMP/rootfs/" 2>/dev/null || true
  fi
}

copy_path /etc/amnezia
copy_path /etc/amneziawg-web
copy_path /var/lib/amneziawg-web
copy_path /opt/awg-bot
copy_path /etc/systemd/system/awg-bot.service
copy_path /etc/systemd/system/amneziawg-web.service
copy_path /etc/nginx
copy_path /etc/letsencrypt
copy_path /usr/local/bin/amneziawg-web
copy_path /usr/local/bin/amneziawg-install.sh

iptables-save > "$TMP/metadata/iptables-v4.rules" 2>/dev/null || true
ip6tables-save > "$TMP/metadata/iptables-v6.rules" 2>/dev/null || true

{
  echo "DATE=$DATE"
  echo "HOSTNAME=$(hostname)"
  echo
  echo "=== awg show ==="
  awg show 2>/dev/null || true
  echo
  echo "=== ports ==="
  ss -tulpen 2>/dev/null || true
} > "$TMP/metadata/server-info.txt"

tar -C "$TMP" -czf "$OUT" .
sha256sum "$OUT" > "${OUT}.sha256"
rm -rf "$TMP"

echo "$OUT"
