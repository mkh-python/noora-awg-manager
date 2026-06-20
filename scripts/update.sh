#!/bin/bash
set -Eeuo pipefail

CHAT_ID="${1:-}"
BOT_DIR="/opt/awg-bot"
CONFIG_FILE="$BOT_DIR/config.env"
REPO_DEFAULT="mkh-python/noora-awg-manager"
BRANCH_DEFAULT="main"
TMP_DIR="$(mktemp -d /tmp/noora-awg-update.XXXXXX)"
BACKUP_DIR="/root/noora-awg-update-backups"
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"

cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

read_env() {
  local key="$1" default="$2" value=""
  if [[ -f "$CONFIG_FILE" ]]; then
    value="$(grep -E "^${key}=" "$CONFIG_FILE" | tail -n1 | cut -d= -f2- || true)"
  fi
  printf '%s' "${value:-$default}"
}


ensure_env_key() {
  local key="$1" value="$2"
  if ! grep -qE "^${key}=" "$CONFIG_FILE" 2>/dev/null; then
    printf '%s=%s\n' "$key" "$value" >> "$CONFIG_FILE"
  fi
}

notify() {
  local text="$1"
  local token
  token="$(read_env BOT_TOKEN '')"
  [[ -n "$CHAT_ID" && -n "$token" ]] || return 0
  curl -fsS --max-time 20 \
    -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${CHAT_ID}" \
    --data-urlencode "text=${text}" >/dev/null || true
}

fail() {
  notify "❌ بروزرسانی ربات ناموفق بود.\n\n$1\n\nنسخه قبلی حفظ شده است."
  exit 1
}
trap 'code=$?; trap - ERR; fail "خطا در خط $LINENO رخ داد (کد $code)."' ERR

REPO="$(read_env GITHUB_REPO "$REPO_DEFAULT")"
BRANCH="$(read_env GITHUB_BRANCH "$BRANCH_DEFAULT")"
ARCHIVE_URL="https://codeload.github.com/${REPO}/zip/refs/heads/${BRANCH}"

mkdir -p "$BACKUP_DIR"
curl -fL --connect-timeout 15 --max-time 180 "$ARCHIVE_URL" -o "$TMP_DIR/source.zip"
unzip -q "$TMP_DIR/source.zip" -d "$TMP_DIR/unpacked"
SOURCE_DIR="$(find "$TMP_DIR/unpacked" -mindepth 1 -maxdepth 1 -type d | head -n1)"

[[ -n "$SOURCE_DIR" ]] || fail "فایل دانلودشده ساختار معتبری ندارد."
for required in VERSION bot/bot.py bot/requirements.txt scripts/backup.sh scripts/send-backup.sh scripts/update.sh systemd/awg-bot.service systemd/awg-full-backup.service systemd/awg-full-backup.timer; do
  [[ -f "$SOURCE_DIR/$required" ]] || fail "فایل ضروری در نسخه جدید وجود ندارد: $required"
done

NEW_VERSION="$(tr -d '[:space:]' < "$SOURCE_DIR/VERSION")"
[[ "$NEW_VERSION" =~ ^[0-9]+(\.[0-9]+){1,3}([-+][0-9A-Za-z.-]+)?$ ]] || fail "شماره نسخه جدید معتبر نیست."
python3 -m py_compile "$SOURCE_DIR/bot/bot.py" || fail "کد bot.py نسخه جدید خطای نحوی دارد."

# Backup only files that updater changes. User data/configuration remain untouched.
tar -czf "$BACKUP_DIR/manager-${STAMP}.tar.gz" \
  "$BOT_DIR/bot.py" "$BOT_DIR/requirements.txt" "$BOT_DIR/VERSION" \
  /usr/local/bin/awg-full-backup.sh /usr/local/bin/awg-send-backup-telegram.sh /usr/local/bin/noora-awg-update.sh \
  /etc/systemd/system/awg-bot.service /etc/systemd/system/awg-full-backup.service /etc/systemd/system/awg-full-backup.timer \
  2>/dev/null || true

install -m 0644 "$SOURCE_DIR/bot/bot.py" "$BOT_DIR/bot.py.new"
install -m 0644 "$SOURCE_DIR/bot/requirements.txt" "$BOT_DIR/requirements.txt.new"
install -m 0644 "$SOURCE_DIR/VERSION" "$BOT_DIR/VERSION.new"

# Install dependencies before switching code.
"$BOT_DIR/venv/bin/pip" install -r "$BOT_DIR/requirements.txt.new"

mv -f "$BOT_DIR/bot.py.new" "$BOT_DIR/bot.py"
mv -f "$BOT_DIR/requirements.txt.new" "$BOT_DIR/requirements.txt"
mv -f "$BOT_DIR/VERSION.new" "$BOT_DIR/VERSION"
install -m 0755 "$SOURCE_DIR/scripts/backup.sh" /usr/local/bin/awg-full-backup.sh
install -m 0755 "$SOURCE_DIR/scripts/send-backup.sh" /usr/local/bin/awg-send-backup-telegram.sh
install -m 0755 "$SOURCE_DIR/scripts/update.sh" /usr/local/bin/noora-awg-update.sh
install -m 0644 "$SOURCE_DIR/systemd/awg-bot.service" /etc/systemd/system/awg-bot.service
install -m 0644 "$SOURCE_DIR/systemd/awg-full-backup.service" /etc/systemd/system/awg-full-backup.service
install -m 0644 "$SOURCE_DIR/systemd/awg-full-backup.timer" /etc/systemd/system/awg-full-backup.timer

# Existing installations stay unlocked until the license API URL is configured.
CURRENT_ADMINS="$(read_env ADMINS '')"
ensure_env_key OWNER_ID "${CURRENT_ADMINS%%,*}"
ensure_env_key LICENSE_REQUIRED "0"
ensure_env_key LICENSE_API_URL ""

systemctl daemon-reload
systemctl enable awg-bot.service >/dev/null 2>&1 || true
systemctl restart awg-bot.service
systemctl is-active --quiet awg-bot.service || fail "سرویس ربات بعد از بروزرسانی اجرا نشد."

notify "✅ ربات با موفقیت به نسخه ${NEW_VERSION} بروزرسانی شد.\n\nبرای دیدن نسخه جدید یک /start بزن."
trap - ERR
