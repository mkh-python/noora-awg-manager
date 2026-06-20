#!/usr/bin/env bash
set -Eeuo pipefail

BOT_DIR="/opt/awg-bot"
CONFIG_FILE="${BOT_DIR}/config.env"
BOT_FILE="${BOT_DIR}/bot.py"
VERSION_FILE="${BOT_DIR}/VERSION"
REQUIREMENTS_FILE="${BOT_DIR}/requirements.txt"
SERVICE_NAME="awg-bot.service"
CHAT_ID="${1:-}"

TMP_DIR=""
BACKUP_DIR=""
FILES_REPLACED=0

log() {
    printf '[noora-update] %s\n' "$*"
}

read_config() {
    local key="$1"
    local default_value="${2:-}"
    local value=""

    if [[ -f "$CONFIG_FILE" ]]; then
        value="$(grep -E "^${key}=" "$CONFIG_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
    fi

    printf '%s' "${value:-$default_value}"
}

send_telegram() {
    local message="$1"
    local token=""

    token="$(read_config BOT_TOKEN "")"

    if [[ -z "$token" || -z "$CHAT_ID" ]]; then
        return 0
    fi

    curl -fsS \
        --connect-timeout 10 \
        --max-time 30 \
        -X POST \
        "https://api.telegram.org/bot${token}/sendMessage" \
        --data-urlencode "chat_id=${CHAT_ID}" \
        --data-urlencode "text=${message}" \
        >/dev/null 2>&1 || true
}

cleanup() {
    if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
        rm -rf "$TMP_DIR"
    fi
}

restore_backup() {
    if [[ "$FILES_REPLACED" -ne 1 ]]; then
        return 0
    fi

    if [[ -z "$BACKUP_DIR" || ! -d "$BACKUP_DIR" ]]; then
        return 0
    fi

    log "Restoring previous version..."

    if [[ -f "$BACKUP_DIR/bot.py" ]]; then
        install -m 0644 "$BACKUP_DIR/bot.py" "$BOT_FILE"
    fi

    if [[ -f "$BACKUP_DIR/requirements.txt" ]]; then
        install -m 0644 "$BACKUP_DIR/requirements.txt" "$REQUIREMENTS_FILE"
    fi

    if [[ -f "$BACKUP_DIR/VERSION" ]]; then
        install -m 0644 "$BACKUP_DIR/VERSION" "$VERSION_FILE"
    fi

    if [[ -f "$BACKUP_DIR/awg-bot.service" ]]; then
        install -m 0644 "$BACKUP_DIR/awg-bot.service" "/etc/systemd/system/${SERVICE_NAME}"
    fi

    systemctl daemon-reload || true
    systemctl restart "$SERVICE_NAME" || true
}

handle_error() {
    local exit_code=$?
    local line_number="${1:-unknown}"

    trap - ERR

    log "Update failed at line ${line_number}."
    restore_backup

    send_telegram "❌ بروزرسانی ربات ناموفق بود.

نسخه قبلی به‌صورت خودکار برگردانده شد."

    exit "$exit_code"
}

trap 'handle_error $LINENO' ERR
trap cleanup EXIT

if [[ "$(id -u)" -ne 0 ]]; then
    echo "This script must be run as root." >&2
    exit 1
fi

if [[ ! -d "$BOT_DIR" ]]; then
    echo "Bot directory not found: $BOT_DIR" >&2
    exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Config file not found: $CONFIG_FILE" >&2
    exit 1
fi

if [[ ! -f "$BOT_FILE" ]]; then
    echo "Bot file not found: $BOT_FILE" >&2
    exit 1
fi

if [[ ! -x "$BOT_DIR/venv/bin/python" ]]; then
    echo "Python virtual environment not found." >&2
    exit 1
fi

REPO="$(read_config GITHUB_REPO "mkh-python/noora-awg-manager")"
BRANCH="$(read_config GITHUB_BRANCH "main")"

if [[ ! "$REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
    echo "Invalid GITHUB_REPO: $REPO" >&2
    exit 1
fi

if [[ ! "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]]; then
    echo "Invalid GITHUB_BRANCH: $BRANCH" >&2
    exit 1
fi

ZIP_URL="https://codeload.github.com/${REPO}/zip/refs/heads/${BRANCH}"

TMP_DIR="$(mktemp -d /tmp/noora-awg-update.XXXXXX)"
BACKUP_DIR="/root/noora-awg-update-backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

CURRENT_VERSION="0.0.0"
if [[ -f "$VERSION_FILE" ]]; then
    CURRENT_VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
fi

log "Current version: $CURRENT_VERSION"
log "Repository: $REPO"
log "Branch: $BRANCH"
log "Downloading source..."

curl -fL \
    --connect-timeout 15 \
    --max-time 180 \
    "$ZIP_URL" \
    -o "$TMP_DIR/source.zip"

log "Extracting source..."
python3 -m zipfile -e "$TMP_DIR/source.zip" "$TMP_DIR/source"

SOURCE_ROOT="$(find "$TMP_DIR/source" -mindepth 1 -maxdepth 1 -type d -print -quit)"

if [[ -z "$SOURCE_ROOT" || ! -d "$SOURCE_ROOT" ]]; then
    echo "Downloaded source structure is invalid." >&2
    exit 1
fi

if [[ -f "$SOURCE_ROOT/bot/bot.py" ]]; then
    NEW_BOT_FILE="$SOURCE_ROOT/bot/bot.py"
elif [[ -f "$SOURCE_ROOT/bot.py" ]]; then
    NEW_BOT_FILE="$SOURCE_ROOT/bot.py"
else
    echo "bot.py was not found in the GitHub repository." >&2
    exit 1
fi

if [[ -f "$SOURCE_ROOT/bot/requirements.txt" ]]; then
    NEW_REQUIREMENTS_FILE="$SOURCE_ROOT/bot/requirements.txt"
elif [[ -f "$SOURCE_ROOT/requirements.txt" ]]; then
    NEW_REQUIREMENTS_FILE="$SOURCE_ROOT/requirements.txt"
else
    NEW_REQUIREMENTS_FILE=""
fi

if [[ ! -f "$SOURCE_ROOT/VERSION" ]]; then
    echo "VERSION was not found in the repository root." >&2
    exit 1
fi

NEW_VERSION="$(tr -d '[:space:]' < "$SOURCE_ROOT/VERSION")"
VERSION_PATTERN='^[0-9]+(\.[0-9]+){1,3}([-+][0-9A-Za-z.-]+)?$'

if [[ ! "$NEW_VERSION" =~ $VERSION_PATTERN ]]; then
    echo "Invalid GitHub VERSION: $NEW_VERSION" >&2
    exit 1
fi

log "Available version: $NEW_VERSION"

if [[ "$CURRENT_VERSION" == "$NEW_VERSION" ]]; then
    log "The latest version is already installed."

    send_telegram "✅ آخرین نسخه را داری.

نسخه نصب‌شده: ${CURRENT_VERSION}"

    exit 0
fi

log "Checking Python syntax..."
"$BOT_DIR/venv/bin/python" -m py_compile "$NEW_BOT_FILE"

log "Backing up current version..."
cp -a "$BOT_FILE" "$BACKUP_DIR/bot.py"

if [[ -f "$REQUIREMENTS_FILE" ]]; then
    cp -a "$REQUIREMENTS_FILE" "$BACKUP_DIR/requirements.txt"
fi

if [[ -f "$VERSION_FILE" ]]; then
    cp -a "$VERSION_FILE" "$BACKUP_DIR/VERSION"
fi

if [[ -f "/etc/systemd/system/${SERVICE_NAME}" ]]; then
    cp -a "/etc/systemd/system/${SERVICE_NAME}" "$BACKUP_DIR/awg-bot.service"
fi

if [[ -n "$NEW_REQUIREMENTS_FILE" ]]; then
    log "Installing Python requirements..."
    "$BOT_DIR/venv/bin/pip" install \
        --disable-pip-version-check \
        -r "$NEW_REQUIREMENTS_FILE"
fi

FILES_REPLACED=1

log "Replacing bot.py..."
install -m 0644 "$NEW_BOT_FILE" "$BOT_FILE"

log "Replacing VERSION..."
install -m 0644 "$SOURCE_ROOT/VERSION" "$VERSION_FILE"

if [[ -n "$NEW_REQUIREMENTS_FILE" ]]; then
    install -m 0644 "$NEW_REQUIREMENTS_FILE" "$REQUIREMENTS_FILE"
fi

if [[ -f "$SOURCE_ROOT/systemd/awg-bot.service" ]]; then
    log "Updating systemd service..."
    install -m 0644 \
        "$SOURCE_ROOT/systemd/awg-bot.service" \
        "/etc/systemd/system/${SERVICE_NAME}"
fi

if [[ -f "$SOURCE_ROOT/scripts/backup.sh" ]]; then
    install -m 0755 \
        "$SOURCE_ROOT/scripts/backup.sh" \
        "/usr/local/bin/awg-full-backup.sh"
fi

if [[ -f "$SOURCE_ROOT/scripts/send-backup.sh" ]]; then
    install -m 0755 \
        "$SOURCE_ROOT/scripts/send-backup.sh" \
        "/usr/local/bin/awg-send-backup-telegram.sh"
fi

log "Checking installed bot.py..."
"$BOT_DIR/venv/bin/python" -m py_compile "$BOT_FILE"

log "Restarting service..."
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"

sleep 5

if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    log "Service failed after update."
    journalctl -u "$SERVICE_NAME" -n 60 --no-pager || true
    exit 1
fi

FILES_REPLACED=0
trap - ERR

send_telegram "✅ ربات با موفقیت به نسخه ${NEW_VERSION} بروزرسانی شد.

برای مشاهده نسخه جدید یک /start بزن."

log "Update completed successfully."
log "Installed version: $NEW_VERSION"
log "Backup directory: $BACKUP_DIR"
