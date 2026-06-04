#!/bin/bash
set -euo pipefail

source /opt/awg-bot/config.env

if [ -z "${BOT_TOKEN:-}" ]; then
  echo "BOT_TOKEN is empty"
  exit 1
fi

if [ -z "${BACKUP_CHAT_ID:-}" ]; then
  echo "BACKUP_CHAT_ID is empty"
  exit 1
fi

BACKUP_FILE="$(/usr/local/bin/awg-full-backup.sh)"
SHA_FILE="${BACKUP_FILE}.sha256"

CAPTION="AWG Full Backup
Date: $(date '+%Y-%m-%d %H:%M:%S')
Host: $(hostname)
File: $(basename "$BACKUP_FILE")"

curl -sS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendDocument" \
  -F chat_id="${BACKUP_CHAT_ID}" \
  -F caption="${CAPTION}" \
  -F document=@"${BACKUP_FILE}"

curl -sS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendDocument" \
  -F chat_id="${BACKUP_CHAT_ID}" \
  -F caption="SHA256 checksum" \
  -F document=@"${SHA_FILE}"

echo "Sent: $BACKUP_FILE"
