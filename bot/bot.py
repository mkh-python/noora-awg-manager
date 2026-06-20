import os
import asyncio
import re
import sqlite3
import subprocess
import ipaddress
import tempfile
import json
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

BASE = Path("/opt/awg-bot")
DB_PATH = BASE / "awg_bot.db"


def load_env(path="/opt/awg-bot/config.env"):
    data = {}
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


ENV = load_env()
BOT_TOKEN = ENV["BOT_TOKEN"]
ADMINS = {int(x.strip()) for x in ENV.get("ADMINS", "").split(",") if x.strip()}
SERVER_ENDPOINT = ENV["SERVER_ENDPOINT"]
AWG_PORT = int(ENV.get("AWG_PORT", "64936"))
AWG_IFACE = ENV.get("AWG_IFACE", "awg0")
SERVER_CONF = Path(ENV.get("SERVER_CONF", f"/etc/amnezia/amneziawg/{AWG_IFACE}.conf"))
CLIENT_DIR = Path(ENV.get("CLIENT_DIR", "/etc/amnezia/amneziawg/clients"))
DNS = ENV.get("DNS", "1.1.1.1,1.0.0.1")
BACKUP_CHAT_ID = ENV.get("BACKUP_CHAT_ID", "").strip()
BACKUP_LINK = ENV.get("BACKUP_LINK", "").strip()
GITHUB_REPO = ENV.get("GITHUB_REPO", "mkh-python/noora-awg-manager").strip()
GITHUB_BRANCH = ENV.get("GITHUB_BRANCH", "main").strip()
VERSION_FILE = BASE / "VERSION"
UPDATE_SCRIPT = Path("/usr/local/bin/noora-awg-update.sh")
DEFAULT_OWNER_ID = 7819156066
OWNER_ID = int(ENV.get("OWNER_ID", ENV.get("LICENSE_OWNER_ID", str(DEFAULT_OWNER_ID))).strip() or DEFAULT_OWNER_ID)
ADMINS.add(OWNER_ID)

CREATOR_USERNAME = "awgdeveloper"
CREATOR_URL = f"https://t.me/{CREATOR_USERNAME}"

LICENSE_REQUIRED = ENV.get("LICENSE_REQUIRED", "0").strip().lower() in {"1", "true", "yes", "on"}
LICENSE_API_URL = ENV.get("LICENSE_API_URL", "").strip().rstrip("/")
LICENSE_INSTALL_ID_FILE = BASE / "INSTALL_ID"
LICENSE_STATE_FILE = BASE / "license-state.json"
LICENSE_CACHE_SECONDS = 300
LICENSE_GRACE_SECONDS = 72 * 60 * 60

PENDING_LICENSE = {}
PENDING_ADD = {}
PENDING_EXTEND = {}
PENDING_MANAGE = {}
BOT_BUSY = {"active": False, "title": "", "owner": None}
LAST_INLINE = {}


def run(cmd, input_text=None):
    result = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def awg(*args):
    return run(["awg", *args])



def busy_text():
    title = BOT_BUSY.get("title") or "یک عملیات مدیریتی"
    return (
        f"⏳ {title} در حال انجام است.\n\n"
        "تا پایان عملیات، دکمه‌ها و دستورها موقتاً غیرفعال هستند.\n"
        "چند لحظه صبر کن."
    )


def set_busy(title, owner_id=None):
    BOT_BUSY["active"] = True
    BOT_BUSY["title"] = title
    BOT_BUSY["owner"] = owner_id


def clear_busy():
    BOT_BUSY["active"] = False
    BOT_BUSY["title"] = ""
    BOT_BUSY["owner"] = None


def is_busy():
    return bool(BOT_BUSY.get("active"))


def is_admin(update: Update):
    user = update.effective_user
    return user and user.id in ADMINS


def is_owner(update: Update):
    user = update.effective_user
    return bool(user and user.id == OWNER_ID)


def write_env_value(key, value):
    path = Path("/opt/awg-bot/config.env")
    lines = path.read_text().splitlines()
    found = False
    out = []

    for line in lines:
        if line.startswith(key + "="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)

    if not found:
        out.append(f"{key}={value}")

    path.write_text("\n".join(out) + "\n")


def save_admins():
    write_env_value("ADMINS", ",".join(str(x) for x in sorted(ADMINS)))


def current_version():
    try:
        value = VERSION_FILE.read_text(encoding="utf-8").strip()
        return value or "0.0.0"
    except OSError:
        return "0.0.0"


def latest_version():
    url = (
        "https://raw.githubusercontent.com/"
        f"{GITHUB_REPO}/{GITHUB_BRANCH}/VERSION"
    )
    value = run([
        "curl", "-fsSL",
        "--connect-timeout", "10",
        "--max-time", "20",
        url,
    ]).strip()

    pattern = r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?"
    if not re.fullmatch(pattern, value):
        raise RuntimeError("شماره نسخه دریافت‌شده از GitHub معتبر نیست.")
    return value


def start_update_job(chat_id):
    if not UPDATE_SCRIPT.exists():
        raise RuntimeError(f"اسکریپت بروزرسانی پیدا نشد: {UPDATE_SCRIPT}")

    run([
        "systemd-run",
        "--collect",
        str(UPDATE_SCRIPT),
        str(int(chat_id)),
    ])



def license_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🔑 وارد کردن لایسنس"],
            ["📨 درخواست لایسنس رایگان"],
            ["🔄 بررسی مجدد لایسنس"],
            ["💬 چت با پشتیبانی"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def get_install_id():
    try:
        value = LICENSE_INSTALL_ID_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass

    value = str(uuid.uuid4())
    LICENSE_INSTALL_ID_FILE.write_text(value + "\n", encoding="utf-8")
    os.chmod(LICENSE_INSTALL_ID_FILE, 0o600)
    return value


def load_license_state():
    try:
        data = json.loads(LICENSE_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_license_state(data):
    LICENSE_STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(LICENSE_STATE_FILE, 0o600)


def license_api_request(path, payload):
    if not LICENSE_API_URL:
        raise RuntimeError("آدرس سرور لایسنس تنظیم نشده است.")

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{LICENSE_API_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except Exception:
            detail = None
        raise RuntimeError(detail or f"خطای HTTP {exc.code} از سرور لایسنس") from exc
    except OSError as exc:
        raise RuntimeError(f"ارتباط با سرور لایسنس برقرار نشد: {exc}") from exc

    try:
        result = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("پاسخ سرور لایسنس معتبر نیست.") from exc

    if not isinstance(result, dict):
        raise RuntimeError("پاسخ سرور لایسنس معتبر نیست.")
    return result


def license_payload(user_id, license_key=None):
    payload = {
        "telegram_user_id": int(user_id),
        "install_id": get_install_id(),
        "version": current_version(),
    }
    if license_key:
        payload["license_key"] = license_key.strip().upper()
    return payload


def check_license_remote(user_id, force=False):
    if not LICENSE_REQUIRED:
        return {"valid": True, "status": "not_required", "message": "License disabled"}

    state = load_license_state()
    now_ts = int(datetime.now(timezone.utc).timestamp())
    checked_at = int(state.get("checked_at", 0) or 0)

    if not force and checked_at and now_ts - checked_at < LICENSE_CACHE_SECONDS:
        return state

    key = str(state.get("license_key", "")).strip()
    if not key:
        return {
            "valid": False,
            "status": "missing",
            "message": "لایسنس هنوز فعال نشده است.",
        }

    try:
        result = license_api_request(
            "/api/v1/license/check",
            license_payload(user_id, key),
        )
        result["license_key"] = key
        result["checked_at"] = now_ts
        if result.get("valid"):
            result["last_success_at"] = now_ts
        save_license_state(result)
        return result
    except Exception as exc:
        last_success = int(state.get("last_success_at", 0) or 0)
        if state.get("valid") and last_success and now_ts - last_success <= LICENSE_GRACE_SECONDS:
            state["grace"] = True
            state["message"] = "سرور لایسنس موقتاً در دسترس نیست؛ مهلت آفلاین فعال است."
            return state
        return {
            "valid": False,
            "status": "unavailable",
            "message": str(exc),
        }


def activate_license_remote(user_id, license_key):
    result = license_api_request(
        "/api/v1/license/activate",
        license_payload(user_id, license_key),
    )
    now_ts = int(datetime.now(timezone.utc).timestamp())
    result["license_key"] = license_key.strip().upper()
    result["checked_at"] = now_ts
    if result.get("valid"):
        result["last_success_at"] = now_ts
        save_license_state(result)
    return result


def request_license_remote(user_id):
    return license_api_request(
        "/api/v1/license/request",
        license_payload(user_id),
    )


def check_request_remote(user_id):
    result = license_api_request(
        "/api/v1/license/request/status",
        license_payload(user_id),
    )
    key = str(result.get("license_key", "")).strip()
    if result.get("status") == "approved" and key:
        return activate_license_remote(user_id, key)
    return result


def license_locked_text(result=None):
    result = result or {}
    status = result.get("status", "missing")
    message = result.get("message", "لایسنس فعال نیست.")
    expires = result.get("expires_at")

    text = (
        "🔒 فعال‌سازی لایسنس Noora AWG\n\n"
        "استفاده از ربات رایگان است، اما برای فعال‌شدن پنل به لایسنس رایگان نیاز داری.\n\n"
        f"وضعیت: {status}\n"
        f"توضیح: {message}"
    )
    if expires:
        text += f"\nتاریخ انقضا: {str(expires)[:10]}"
    text += "\n\nاز دکمه‌های زیر برای درخواست یا فعال‌سازی استفاده کن."
    return text


async def send_license_screen(update: Update, result=None):
    message = update.effective_message
    if message:
        await message.reply_text(
            license_locked_text(result),
            reply_markup=license_keyboard(),
        )


async def license_access_allowed(update: Update, force=False, show=True):
    if not LICENSE_REQUIRED:
        return True
    user = update.effective_user
    if not user:
        return False
    result = await asyncio.to_thread(check_license_remote, user.id, force)
    if result.get("valid"):
        return True
    if show:
        await send_license_screen(update, result)
    return False


async def handle_license_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    uid = update.effective_user.id

    if text == "💬 چت با پشتیبانی":
        await update.message.reply_text(
            "برای دریافت لایسنس رایگان با پشتیبانی ارتباط بگیر:\n"
            f"@{CREATOR_USERNAME}",
            reply_markup=creator_contact_keyboard(),
        )
        return True

    if text == "🔑 وارد کردن لایسنس":
        PENDING_LICENSE[uid] = "license_key"
        await update.message.reply_text(
            "کلید لایسنس را بفرست.\n\nمثال:\nNOORA-XXXX-XXXX-XXXX-XXXX",
            reply_markup=license_keyboard(),
        )
        return True

    if text == "📨 درخواست لایسنس رایگان":
        try:
            result = await asyncio.to_thread(request_license_remote, uid)
            await update.message.reply_text(
                "✅ درخواست لایسنس ثبت شد.\n\n"
                f"شماره درخواست: {result.get('request_id', '-')}\n"
                "بعد از تأیید پشتیبانی، دکمه «بررسی مجدد لایسنس» را بزن.",
                reply_markup=license_keyboard(),
            )
        except Exception as exc:
            await update.message.reply_text(
                f"❌ ثبت درخواست ناموفق بود:\n{exc}",
                reply_markup=license_keyboard(),
            )
        return True

    if text == "🔄 بررسی مجدد لایسنس":
        try:
            state = load_license_state()
            if state.get("license_key"):
                result = await asyncio.to_thread(check_license_remote, uid, True)
            else:
                result = await asyncio.to_thread(check_request_remote, uid)

            if result.get("valid"):
                await update.message.reply_text(
                    "✅ لایسنس فعال شد و پنل آماده استفاده است.",
                    reply_markup=main_keyboard(uid),
                )
            else:
                await update.message.reply_text(
                    license_locked_text(result),
                    reply_markup=license_keyboard(),
                )
        except Exception as exc:
            await update.message.reply_text(
                f"❌ بررسی لایسنس ناموفق بود:\n{exc}",
                reply_markup=license_keyboard(),
            )
        return True

    if PENDING_LICENSE.get(uid) == "license_key":
        key = text.strip().upper()
        if not re.fullmatch(r"NOORA(?:-[A-Z0-9]{4}){4}", key):
            await update.message.reply_text(
                "فرمت کلید معتبر نیست. دوباره کلید کامل را بفرست.",
                reply_markup=license_keyboard(),
            )
            return True

        try:
            result = await asyncio.to_thread(activate_license_remote, uid, key)
            PENDING_LICENSE.pop(uid, None)
            if result.get("valid"):
                await update.message.reply_text(
                    "✅ لایسنس با موفقیت فعال شد.\n\n"
                    f"اعتبار تا: {str(result.get('expires_at', '-'))[:10]}",
                    reply_markup=main_keyboard(uid),
                )
            else:
                await update.message.reply_text(
                    license_locked_text(result),
                    reply_markup=license_keyboard(),
                )
        except Exception as exc:
            PENDING_LICENSE.pop(uid, None)
            await update.message.reply_text(
                f"❌ فعال‌سازی ناموفق بود:\n{exc}",
                reply_markup=license_keyboard(),
            )
        return True

    await send_license_screen(update, await asyncio.to_thread(check_license_remote, uid, False))
    return True

def owner_only_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 نمایش ادمین‌ها", callback_data="manage:list_admins"),
        ],
        [
            InlineKeyboardButton("➕ افزودن ادمین", callback_data="manage:add_admin"),
            InlineKeyboardButton("➖ حذف ادمین", callback_data="manage:remove_admin"),
        ],
        [
            InlineKeyboardButton("📦 تنظیم کانال بکاپ", callback_data="manage:set_backup_channel"),
        ],
        [
            InlineKeyboardButton("🌐 تنظیم دامنه و SSL", callback_data="manage:set_domain_ssl"),
        ],
        [
            InlineKeyboardButton("💾 گرفتن بکاپ کامل", callback_data="manage:backup_now"),
        ],
        [
            InlineKeyboardButton("🔁 تعداد بکاپ روزانه", callback_data="manage:set_backup_time"),
        ],
        [
            InlineKeyboardButton("⬆️ بروزرسانی ربات", callback_data="manage:update_bot"),
        ],
        [
            InlineKeyboardButton("📋 نمایش تنظیمات", callback_data="manage:show_settings"),
        ],
        [
            InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:back"),
        ],
    ])


async def guard(update: Update):
    if not is_admin(update):
        await update.message.reply_text("Access denied.")
        return False
    if not await license_access_allowed(update):
        return False
    return True


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute("""
    CREATE TABLE IF NOT EXISTS users (
        name TEXT PRIMARY KEY,
        private_key TEXT NOT NULL,
        public_key TEXT NOT NULL,
        psk TEXT NOT NULL,
        ipv4 TEXT NOT NULL,
        ipv6 TEXT,
        limit_bytes INTEGER NOT NULL,
        expire_at TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        total_rx INTEGER NOT NULL DEFAULT 0,
        total_tx INTEGER NOT NULL DEFAULT 0,
        last_rx INTEGER NOT NULL DEFAULT 0,
        last_tx INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)
    con.commit()
    con.close()



def import_panel_users(default_gb=1024, default_days=3650):
    """
    Import users created by amneziawg-web panel into bot DB.
    Default quota for imported users: 1024GB / 3650 days.
    """
    txt = read_server_conf()

    pattern = re.compile(
        r"### Client\s+(.+?)\n"
        r"\[Peer\]\n"
        r"PublicKey\s*=\s*(.+?)\n"
        r"PresharedKey\s*=\s*(.+?)\n"
        r"AllowedIPs\s*=\s*(.+?)(?:\n\n|\Z)",
        re.S
    )

    con = db()
    now = datetime.now(timezone.utc)
    expire_at = now + timedelta(days=default_days)
    limit_bytes = default_gb * 1024 * 1024 * 1024

    imported = 0

    for m in pattern.finditer(txt):
        name = m.group(1).strip()
        public_key = m.group(2).strip()
        psk = m.group(3).strip()
        allowed_ips = m.group(4).strip().replace(" ", "")

        if con.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
            continue

        ipv4 = None
        ipv6 = None

        for part in allowed_ips.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                ipv6 = part
            else:
                ipv4 = part

        if not ipv4:
            continue

        private_key = "UNKNOWN"

        cfg_path = CLIENT_DIR / f"awg0-client-{name}.conf"
        if cfg_path.exists():
            try:
                cfg = cfg_path.read_text()
                km = re.search(r"^PrivateKey\s*=\s*(.+)$", cfg, re.M)
                if km:
                    private_key = km.group(1).strip()
            except Exception:
                pass

        con.execute(
            """INSERT INTO users
            (name, private_key, public_key, psk, ipv4, ipv6, limit_bytes, expire_at, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                name,
                private_key,
                public_key,
                psk,
                ipv4,
                ipv6,
                limit_bytes,
                expire_at.isoformat(),
                now.isoformat(),
            ),
        )
        imported += 1

    con.commit()
    con.close()
    return imported


def read_server_conf():
    if not SERVER_CONF.exists():
        raise RuntimeError(f"Server config not found: {SERVER_CONF}")
    return SERVER_CONF.read_text()


def server_public_key():
    return awg("show", AWG_IFACE, "public-key")


def parse_server_address():
    txt = read_server_conf()
    m = re.search(r"^Address\s*=\s*([^,\n]+)", txt, flags=re.M)
    if not m:
        return ipaddress.ip_interface("10.66.66.1/24")
    return ipaddress.ip_interface(m.group(1).strip())


def parse_obfs_params():
    txt = read_server_conf()
    keys = ["Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"]
    params = {}
    for k in keys:
        m = re.search(rf"^{k}\s*=\s*(.+)$", txt, flags=re.M)
        if m:
            params[k] = m.group(1).strip()
    return params


def used_ipv4s():
    used = set()
    con = db()
    for row in con.execute("SELECT ipv4 FROM users"):
        used.add(ipaddress.ip_interface(row["ipv4"]).ip)
    con.close()

    txt = read_server_conf()
    for m in re.finditer(r"AllowedIPs\s*=\s*([^\n]+)", txt):
        for part in m.group(1).split(","):
            part = part.strip()
            if ":" not in part and "/" in part:
                try:
                    used.add(ipaddress.ip_interface(part).ip)
                except Exception:
                    pass
    return used


def next_ipv4():
    iface = parse_server_address()
    net = iface.network
    used = used_ipv4s()
    used.add(iface.ip)
    for ip in net.hosts():
        if ip not in used:
            return f"{ip}/32"
    raise RuntimeError("No free IPv4 available.")


def genkey():
    return awg("genkey").strip()


def pubkey(private_key):
    return run(["awg", "pubkey"], input_text=private_key + "\n").strip()


def genpsk():
    return awg("genpsk").strip()


def marker(name):
    return f"### Client {name}"


def remove_peer_block(conf_text, name):
    escaped = re.escape(name)

    patterns = [
        rf"\n?### Client {escaped}\n\[Peer\]\n(?:.*\n)*?(?=\n### Client |\n# AWG_BOT_CLIENT:|\Z)",
        rf"\n?# AWG_BOT_CLIENT: {escaped}\n\[Peer\]\n(?:.*\n)*?(?=\n### Client |\n# AWG_BOT_CLIENT:|\Z)",
    ]

    out = conf_text
    for pattern in patterns:
        out = re.sub(pattern, "\n", out, flags=re.M)
    return out


def append_peer_to_conf(name, public_key, psk, allowed_ips):
    txt = read_server_conf()
    txt = remove_peer_block(txt, name).rstrip() + "\n\n"
    txt += f"{marker(name)}\n"
    txt += "[Peer]\n"
    txt += f"PublicKey = {public_key}\n"
    txt += f"PresharedKey = {psk}\n"
    txt += f"AllowedIPs = {allowed_ips}\n"
    SERVER_CONF.write_text(txt)


def remove_peer_from_conf(name):
    txt = read_server_conf()
    SERVER_CONF.write_text(remove_peer_block(txt, name))


def sync_awg():
    run(["bash", "-lc", f"awg syncconf {AWG_IFACE} <(awg-quick strip {AWG_IFACE})"])


def make_client_config(name, private_key, psk, ipv4, ipv6=None):
    params = parse_obfs_params()
    spub = server_public_key()

    lines = [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {ipv4}" + (f", {ipv6}" if ipv6 else ""),
        f"DNS = {DNS}",
    ]

    for k in ["Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"]:
        if k in params:
            lines.append(f"{k} = {params[k]}")

    lines += [
        "",
        "[Peer]",
        f"PublicKey = {spub}",
        f"PresharedKey = {psk}",
        f"Endpoint = {SERVER_ENDPOINT}:{AWG_PORT}",
        "AllowedIPs = 0.0.0.0/0, ::/0",
        "PersistentKeepalive = 25",
        "",
    ]
    return "\n".join(lines)


def save_client_config(name, content):
    CLIENT_DIR.mkdir(parents=True, exist_ok=True)
    path = CLIENT_DIR / f"awg0-client-{name}.conf"
    path.write_text(content)
    os.chmod(path, 0o600)

    try:
        import pwd
        import grp
        uid = pwd.getpwnam("awg-web").pw_uid
        gid = grp.getgrnam("awg-web").gr_gid
        os.chown(path, uid, gid)
    except Exception:
        pass

    return path


def qr_png(config_text):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmp.close()
    run(["qrencode", "-o", tmp.name], input_text=config_text)
    return tmp.name


def dump_peers():
    out = awg("show", AWG_IFACE, "dump")
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 8:
            rows.append({
                "public_key": parts[0],
                "endpoint": parts[2],
                "allowed_ips": parts[3],
                "latest": int(parts[4]) if parts[4].isdigit() else 0,
                "rx": int(parts[5]) if parts[5].isdigit() else 0,
                "tx": int(parts[6]) if parts[6].isdigit() else 0,
            })
    return rows


def human_bytes(n):
    n = int(n)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.2f} {u}"
        v /= 1024


def disable_user(name):
    con = db()
    row = con.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
    if not row:
        con.close()
        raise RuntimeError("User not found.")
    remove_peer_from_conf(name)
    try:
        awg("set", AWG_IFACE, "peer", row["public_key"], "remove")
    except Exception:
        pass
    con.execute("UPDATE users SET enabled=0 WHERE name=?", (name,))
    con.commit()
    con.close()
    sync_awg()


def enable_user(name):
    con = db()
    row = con.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
    if not row:
        con.close()
        raise RuntimeError("User not found.")
    allowed = row["ipv4"] + (f", {row['ipv6']}" if row["ipv6"] else "")
    append_peer_to_conf(name, row["public_key"], row["psk"], allowed)
    con.execute("UPDATE users SET enabled=1 WHERE name=?", (name,))
    con.commit()
    con.close()
    sync_awg()


def update_traffic_and_enforce():
    try:
        import_panel_users()
    except Exception as e:
        print("import panel users error:", e)

    peers = {p["public_key"]: p for p in dump_peers()}
    con = db()
    now = datetime.now(timezone.utc)

    for row in con.execute("SELECT * FROM users").fetchall():
        pub = row["public_key"]
        rx = row["last_rx"]
        tx = row["last_tx"]
        total_rx = row["total_rx"]
        total_tx = row["total_tx"]

        if pub in peers:
            cur_rx = peers[pub]["rx"]
            cur_tx = peers[pub]["tx"]
            delta_rx = cur_rx - rx if cur_rx >= rx else cur_rx
            delta_tx = cur_tx - tx if cur_tx >= tx else cur_tx
            total_rx += max(delta_rx, 0)
            total_tx += max(delta_tx, 0)

            con.execute(
                "UPDATE users SET last_rx=?, last_tx=?, total_rx=?, total_tx=? WHERE name=?",
                (cur_rx, cur_tx, total_rx, total_tx, row["name"]),
            )

        expire_at = datetime.fromisoformat(row["expire_at"])
        used = total_rx + total_tx
        if row["enabled"] and (used >= row["limit_bytes"] or now >= expire_at):
            con.commit()
            con.close()
            disable_user(row["name"])
            con = db()

    con.commit()
    con.close()




def main_keyboard(user_id=None):
    rows = [
        ["➕ ساخت کاربر", "📋 لیست کاربران"],
        ["📱 QR کاربر", "📄 فایل کانفیگ"],
        ["⛔ غیرفعال", "✅ فعال‌سازی"],
        ["➕ تمدید حجم/روز", "🗑 حذف کاربر"],
        ["📊 وضعیت سرور", "🆔 دریافت ID"],
        ["💬 چت با سازنده"],
    ]

    if user_id == OWNER_ID:
        rows.append(["⚙️ مدیریت"])

    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def creator_contact_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💬 شروع چت با سازنده",
                url=CREATOR_URL,
            ),
        ],
    ])


def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ بازگشت به منو", callback_data="menu:back")]
    ])


async def delete_last_inline(context: ContextTypes.DEFAULT_TYPE, chat_id: int, uid: int):
    msg_id = LAST_INLINE.get(uid)
    if not msg_id:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception:
        pass
    LAST_INLINE.pop(uid, None)


async def send_inline(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    await delete_last_inline(context, chat_id, uid)
    msg = await update.message.reply_text(text, reply_markup=reply_markup)
    LAST_INLINE[uid] = msg.message_id
    return msg


async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_last_inline(context, update.effective_chat.id, update.effective_user.id)
    await update.message.reply_text(
        "پنل مدیریت AmneziaWG آماده است.",
        reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None)
    )


def users_action_keyboard(action):
    try:
        import_panel_users()
    except Exception as e:
        print("import panel users error:", e)

    con = db()
    rows = con.execute("SELECT name, enabled FROM users ORDER BY name").fetchall()
    con.close()

    buttons = []
    row_buttons = []

    for r in rows:
        name = r["name"]
        status = "✅" if r["enabled"] else "⛔"
        row_buttons.append(
            InlineKeyboardButton(f"{status} {name}", callback_data=f"user:{action}:{name}")
        )
        if len(row_buttons) == 2:
            buttons.append(row_buttons)
            row_buttons = []

    if row_buttons:
        buttons.append(row_buttons)

    buttons.append([InlineKeyboardButton("⬅️ بازگشت به منو", callback_data="menu:back")])
    return InlineKeyboardMarkup(buttons)


def get_user_row(name):
    con = db()
    row = con.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
    con.close()
    return row


async def show_menu_message(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    if update.callback_query:
        try:
            await update.callback_query.message.delete()
        except Exception:
            pass
        LAST_INLINE.pop(update.callback_query.from_user.id, None)
        await context.bot.send_message(
            chat_id=update.callback_query.message.chat_id,
            text="پنل مدیریت AmneziaWG آماده است.",
            reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None)
        )
    else:
        await send_main_menu(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Access denied.")
        return
    if not await license_access_allowed(update):
        return
    await show_menu_message(update, context)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.from_user or query.from_user.id not in ADMINS:
        await query.edit_message_text("Access denied.")
        return

    if LICENSE_REQUIRED:
        result = await asyncio.to_thread(check_license_remote, query.from_user.id, False)
        if not result.get("valid"):
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=license_locked_text(result),
                reply_markup=license_keyboard(),
            )
            return

    if is_busy():
        await query.answer(busy_text(), show_alert=True)
        return

    data = query.data

    if data == "menu:back":
        try:
            await query.message.delete()
        except Exception:
            pass
        LAST_INLINE.pop(query.from_user.id, None)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="به منوی اصلی برگشتی.",
            reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None)
        )
        return

    if data == "menu:add":
        PENDING_ADD[query.from_user.id] = {"step": "name"}
        await query.edit_message_text(
            "اسم کاربر جدید را بفرست.\n\n"
            "مثال:\n"
            "noora\n\n"
            "فقط حروف انگلیسی، عدد، خط تیره و آندرلاین مجاز است.",
            reply_markup=back_keyboard(),
        )
        return

    if data == "menu:list":
        try:
            update_traffic_and_enforce()
            con = db()
            rows = con.execute("SELECT * FROM users ORDER BY name").fetchall()
            con.close()

            if not rows:
                text = "کاربری وجود ندارد."
            else:
                msg = []
                for r in rows:
                    used = r["total_rx"] + r["total_tx"]
                    limit = r["limit_bytes"]
                    status = "فعال ✅" if r["enabled"] else "غیرفعال ⛔"
                    msg.append(
                        f"{r['name']} | {status}\n"
                        f"مصرف: {human_bytes(used)} / {human_bytes(limit)}\n"
                        f"انقضا: {r['expire_at'][:10]}\n"
                        f"IP: {r['ipv4']}"
                    )
                text = "\n\n".join(msg)
        except Exception as e:
            text = f"خطا:\n{e}"

        await query.edit_message_text(text[:3900], reply_markup=back_keyboard())
        return

    if data == "menu:qr":
        await query.edit_message_text(
            "برای دریافت QR، کاربر را انتخاب کن:",
            reply_markup=users_action_keyboard("qr"),
        )
        return

    if data == "menu:config":
        await query.edit_message_text(
            "برای دریافت فایل کانفیگ، کاربر را انتخاب کن:",
            reply_markup=users_action_keyboard("config"),
        )
        return

    if data == "menu:disable":
        await query.edit_message_text(
            "برای غیرفعال کردن، کاربر را انتخاب کن:",
            reply_markup=users_action_keyboard("disable"),
        )
        return

    if data == "menu:enable":
        await query.edit_message_text(
            "برای فعال‌سازی، کاربر را انتخاب کن:",
            reply_markup=users_action_keyboard("enable"),
        )
        return

    if data == "menu:delete":
        await query.edit_message_text(
            "برای حذف کامل، کاربر را انتخاب کن:",
            reply_markup=users_action_keyboard("delete"),
        )
        return

    if data == "menu:extend":
        await query.edit_message_text(
            "تمدید حجم و روز:\n\n/extend name gb days\n\nمثال:\n/extend noora 10 15\n\nیعنی ۱۰ گیگ و ۱۵ روز به کاربر اضافه می‌شود.",
            reply_markup=back_keyboard(),
        )
        return

    if data.startswith("user:"):
        try:
            _, action, name = data.split(":", 2)
            row = get_user_row(name)
            if not row:
                await query.edit_message_text("کاربر پیدا نشد.", reply_markup=back_keyboard())
                return

            LAST_INLINE.pop(query.from_user.id, None)

            if action == "qr":
                path = CLIENT_DIR / f"awg0-client-{name}.conf"
                if not path.exists():
                    await query.edit_message_text("فایل کانفیگ پیدا نشد.", reply_markup=back_keyboard())
                    return
                png = qr_png(path.read_text())
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=open(png, "rb"), caption=f"QR: {name}")
                os.unlink(png)
                await query.edit_message_text("QR ارسال شد.", reply_markup=back_keyboard())
                return

            if action == "config":
                path = CLIENT_DIR / f"awg0-client-{name}.conf"
                if not path.exists():
                    await query.edit_message_text("فایل کانفیگ پیدا نشد.", reply_markup=back_keyboard())
                    return
                await context.bot.send_document(chat_id=query.message.chat_id, document=open(path, "rb"), filename=path.name)
                await query.edit_message_text("فایل کانفیگ ارسال شد.", reply_markup=back_keyboard())
                return

            if action == "disable":
                disable_user(name)
                await query.edit_message_text(f"کاربر {name} غیرفعال شد.", reply_markup=back_keyboard())
                return

            if action == "enable":
                enable_user(name)
                await query.edit_message_text(f"کاربر {name} فعال شد.", reply_markup=back_keyboard())
                return

            if action == "delete":
                disable_user(name)
                con = db()
                con.execute("DELETE FROM users WHERE name=?", (name,))
                con.commit()
                con.close()
                path = CLIENT_DIR / f"awg0-client-{name}.conf"
                if path.exists():
                    path.unlink()
                await query.edit_message_text(f"کاربر {name} حذف شد.", reply_markup=back_keyboard())
                return

            if action == "extend":
                PENDING_EXTEND[query.from_user.id] = {"step": "gb", "name": name}
                try:
                    await query.message.delete()
                except Exception:
                    pass
                LAST_INLINE.pop(query.from_user.id, None)
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"تمدید کاربر: {name}\n\nچند گیگ اضافه شود؟\n\nمثال:\n10",
                    reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None)
                )
                return

        except Exception as e:
            await query.edit_message_text(f"خطا:\n{e}", reply_markup=back_keyboard())
            return

    if data.startswith("manage:"):
        if query.from_user.id != OWNER_ID:
            await query.answer("Access denied.", show_alert=True)
            return

        action = data.split(":", 1)[1]

        if action == "list_admins":
            text = "ادمین‌های فعلی:\n\n"
            for admin_id in sorted(ADMINS):
                if admin_id == OWNER_ID:
                    text += f"{admin_id}  مالک اصلی\n"
                else:
                    text += f"{admin_id}\n"

            await query.edit_message_text(text, reply_markup=owner_only_keyboard())
            return

        if action == "add_admin":
            PENDING_MANAGE[query.from_user.id] = {"step": "add_admin"}
            try:
                await query.message.delete()
            except Exception:
                pass
            LAST_INLINE.pop(query.from_user.id, None)

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    "افزودن ادمین جدید\n\n"
                    "ID عددی ادمین جدید را بفرست.\n\n"
                    "مثال:\n"
                    "123456789\n\n"
                    "برای لغو بنویس: لغو"
                ),
                reply_markup=main_keyboard(query.from_user.id),
            )
            return

        if action == "remove_admin":
            PENDING_MANAGE[query.from_user.id] = {"step": "remove_admin"}
            try:
                await query.message.delete()
            except Exception:
                pass
            LAST_INLINE.pop(query.from_user.id, None)

            text = "حذف ادمین\n\nادمین‌های قابل حذف:\n\n"
            removable = [x for x in sorted(ADMINS) if x != OWNER_ID]
            if removable:
                text += "\n".join(str(x) for x in removable)
                text += "\n\nID عددی ادمینی که می‌خواهی حذف شود را بفرست."
            else:
                text += "ادمین قابل حذف وجود ندارد."

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text + "\n\nبرای لغو بنویس: لغو",
                reply_markup=main_keyboard(query.from_user.id),
            )
            return

        if action == "set_backup_channel":
            PENDING_MANAGE[query.from_user.id] = {"step": "backup_link"}
            try:
                await query.message.delete()
            except Exception:
                pass
            LAST_INLINE.pop(query.from_user.id, None)

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    "تنظیم کانال بکاپ\n\n"
                    "قبل از ادامه، ربات باید داخل کانال یا گروه بکاپ Admin باشد.\n"
                    "برای کانال، دسترسی Post Messages هم لازم است.\n\n"
                    "حالا لینک کانال/گروه بکاپ را بفرست.\n\n"
                    "مثال:\n"
                    "https://t.me/+xxxxxxxx\n\n"
                    "برای لغو بنویس: لغو"
                ),
                reply_markup=main_keyboard(query.from_user.id),
            )
            return

        if action == "set_domain_ssl":
            PENDING_MANAGE[query.from_user.id] = {"step": "domain_name"}
            try:
                await query.message.delete()
            except Exception:
                pass
            LAST_INLINE.pop(query.from_user.id, None)

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    "تنظیم دامنه و SSL\n\n"
                    "قبل از شروع، باید در پنل DNS دامنه یک A Record بسازی.\n\n"
                    "مثال:\n"
                    "panel.example.com  A  203.0.113.10\n\n"
                    "حالا دامنه‌ای که می‌خواهی برای پنل استفاده شود را بفرست.\n\n"
                    "مثال:\n"
                    "panel.example.com\n\n"
                    "برای لغو بنویس: لغو"
                ),
                reply_markup=main_keyboard(query.from_user.id),
            )
            return

        if action == "set_domain_ssl":
            PENDING_MANAGE[query.from_user.id] = {"step": "domain_name"}
            try:
                await query.message.delete()
            except Exception:
                pass
            LAST_INLINE.pop(query.from_user.id, None)

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    "تنظیم دامنه و SSL\n\n"
                    "قبل از شروع، باید در پنل DNS دامنه یک A Record بسازی.\n\n"
                    "مثال:\n"
                    "panel.example.com  A  203.0.113.10\n\n"
                    "حالا دامنه‌ای که می‌خواهی برای پنل استفاده شود را بفرست.\n\n"
                    "مثال:\n"
                    "panel.example.com\n\n"
                    "برای لغو بنویس: لغو"
                ),
                reply_markup=main_keyboard(query.from_user.id),
            )
            return

        if action == "backup_now":
            set_busy("گرفتن بکاپ کامل", query.from_user.id)

            try:
                await query.message.delete()
            except Exception:
                pass

            LAST_INLINE.pop(query.from_user.id, None)

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    "⏳ بکاپ کامل شروع شد.\n\n"
                    "از کاربران، دیتابیس بات، کانفیگ‌ها، SSL، nginx و firewall بکاپ گرفته می‌شود.\n"
                    "تا پایان عملیات، دکمه‌ها موقتاً قفل هستند."
                ),
                reply_markup=main_keyboard(query.from_user.id),
            )

            try:
                backup_path = await asyncio.to_thread(create_full_backup)
                sha_path = backup_path + ".sha256"

                size_mb = Path(backup_path).stat().st_size / 1024 / 1024

                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=open(backup_path, "rb"),
                    filename=Path(backup_path).name,
                    caption=f"Full backup\nSize: {size_mb:.2f} MB",
                )

                if Path(sha_path).exists():
                    await context.bot.send_document(
                        chat_id=query.message.chat_id,
                        document=open(sha_path, "rb"),
                        filename=Path(sha_path).name,
                        caption="SHA256 checksum",
                    )

                if BACKUP_CHAT_ID and str(BACKUP_CHAT_ID) != str(query.message.chat_id):
                    await context.bot.send_document(
                        chat_id=int(BACKUP_CHAT_ID),
                        document=open(backup_path, "rb"),
                        filename=Path(backup_path).name,
                        caption="Full backup from bot",
                    )

                    if Path(sha_path).exists():
                        await context.bot.send_document(
                            chat_id=int(BACKUP_CHAT_ID),
                            document=open(sha_path, "rb"),
                            filename=Path(sha_path).name,
                            caption="SHA256 checksum",
                        )

                clear_busy()

                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=(
                        "✅ بکاپ کامل انجام شد.\n\n"
                        f"File: {Path(backup_path).name}\n"
                        f"Size: {size_mb:.2f} MB\n\n"
                        "این فایل برای ریستور کامل کاربران و تنظیمات استفاده می‌شود."
                    ),
                    reply_markup=main_keyboard(query.from_user.id),
                )
                return

            except Exception as e:
                clear_busy()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"❌ خطا در بکاپ:\n\n{e}",
                    reply_markup=main_keyboard(query.from_user.id),
                )
                return

        if action == "set_backup_time":
            PENDING_MANAGE[query.from_user.id] = {"step": "backup_frequency"}
            try:
                await query.message.delete()
            except Exception:
                pass
            LAST_INLINE.pop(query.from_user.id, None)

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    "تنظیم تعداد بکاپ خودکار\n\n"
                    "روزی چند بار بکاپ گرفته شود؟\n\n"
                    "مثال‌ها:\n"
                    "1 = روزی یک بار\n"
                    "2 = هر 12 ساعت\n"
                    "4 = هر 6 ساعت\n"
                    "6 = هر 4 ساعت\n"
                    "12 = هر 2 ساعت\n"
                    "24 = هر 1 ساعت\n\n"
                    "عدد مجاز: 1 تا 24\n"
                    "برای لغو بنویس: لغو"
                ),
                reply_markup=main_keyboard(query.from_user.id),
            )
            return

        if action == "update_bot":
            try:
                installed = current_version()

                await query.edit_message_text(
                    "🔎 در حال بررسی نسخه GitHub..."
                )

                available = await asyncio.to_thread(latest_version)

                if installed == available:
                    await query.edit_message_text(
                        "✅ آخرین نسخه را داری.\n\n"
                        f"نسخه نصب‌شده: {installed}",
                        reply_markup=owner_only_keyboard(),
                    )
                    return

                await query.edit_message_text(
                    "⬆️ نسخه جدید پیدا شد.\n\n"
                    f"نسخه نصب‌شده: {installed}\n"
                    f"نسخه جدید: {available}\n\n"
                    "در حال بروزرسانی کامل از GitHub هستم؛ لطفاً صبر کنید."
                )

                await asyncio.to_thread(
                    start_update_job,
                    query.message.chat_id,
                )
                return

            except Exception as e:
                await query.edit_message_text(
                    f"❌ بررسی یا شروع بروزرسانی ناموفق بود:\n\n{e}",
                    reply_markup=owner_only_keyboard(),
                )
                return

        if action == "show_settings":
            text = (
                "تنظیمات فعلی:\n\n"
                f"VERSION: {current_version()}\n"
                f"OWNER_ID: {OWNER_ID}\n"
                f"ADMINS: {','.join(str(x) for x in sorted(ADMINS))}\n"
                f"BACKUP_CHAT_ID: {BACKUP_CHAT_ID or 'Not set'}\n"
                f"BACKUP_LINK: {BACKUP_LINK or 'Not set'}\n"
            )
            await query.edit_message_text(text, reply_markup=owner_only_keyboard())
            return

        if action == "back":
            await query.edit_message_text("بخش مدیریت اختصاصی مالک:", reply_markup=owner_only_keyboard())
            return

    if data == "menu:status":
        try:
            text = pretty_server_status()
        except Exception as e:
            text = f"خطا:\n{e}"

        await query.edit_message_text(text[:3900], reply_markup=back_keyboard())
        return

    if data == "menu:id":
        await query.edit_message_text(
            f"chat_id: {query.message.chat_id}\nuser_id: {query.from_user.id}",
            reply_markup=back_keyboard(),
        )
        return


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message

    text = "شناسه‌ها:\n\n"
    text += f"chat_id: {chat.id}\n"
    text += f"chat_type: {chat.type}\n"

    if chat.title:
        text += f"chat_title: {chat.title}\n"

    if user:
        text += f"user_id: {user.id}\n"
        text += f"username: @{user.username}\n" if user.username else "username: None\n"
    else:
        text += "user_id: None\n"
        text += "username: None\n"

    await msg.reply_text(
        text,
        reply_markup=main_keyboard(user.id if user else None) if chat.type == "private" else None
    )


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /add name gb days")
        return

    name = context.args[0]
    if not re.match(r"^[A-Za-z0-9_-]{1,15}$", name):
        await update.message.reply_text("Name فقط حروف، عدد، _ و - حداکثر ۱۵ کاراکتر.")
        return

    gb = int(context.args[1])
    days = int(context.args[2])
    limit_bytes = gb * 1024 * 1024 * 1024
    expire_at = datetime.now(timezone.utc) + timedelta(days=days)

    con = db()
    if con.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
        con.close()
        await update.message.reply_text("این نام قبلاً وجود دارد.")
        return
    con.close()

    priv = genkey()
    pub = pubkey(priv)
    psk = genpsk()
    ipv4 = next_ipv4()

    config = make_client_config(name, priv, psk, ipv4)
    path = save_client_config(name, config)

    append_peer_to_conf(name, pub, psk, ipv4)
    sync_awg()

    con = db()
    con.execute(
        """INSERT INTO users
        (name, private_key, public_key, psk, ipv4, ipv6, limit_bytes, expire_at, enabled, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (name, priv, pub, psk, ipv4, None, limit_bytes, expire_at.isoformat(), datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()

    await update.message.reply_text(f"User created: {name}\nIP: {ipv4}\nLimit: {gb}GB\nDays: {days}")
    await context.bot.send_document(update.effective_chat.id, document=open(path, "rb"), filename=path.name)
    png = qr_png(config)
    await context.bot.send_photo(update.effective_chat.id, photo=open(png, "rb"))
    os.unlink(png)

    await send_install_links(update.effective_chat.id, context)

    if BACKUP_CHAT_ID:
        await context.bot.send_document(int(BACKUP_CHAT_ID), document=open(path, "rb"), filename=path.name, caption=f"Backup config: {name}")






async def main_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    text = (update.message.text or "").strip()
    uid = update.effective_user.id

    if LICENSE_REQUIRED:
        result = await asyncio.to_thread(check_license_remote, uid, False)
        if not result.get("valid"):
            await handle_license_text(update, context)
            return

    if is_busy():
        await update.message.reply_text(
            busy_text(),
            reply_markup=main_keyboard(uid)
        )
        return

    if text == "➕ ساخت کاربر":
        await delete_last_inline(context, update.effective_chat.id, uid)
        PENDING_ADD[uid] = {"step": "name"}
        await update.message.reply_text(
            "اسم کاربر جدید را بفرست.\n\n"
            "مثال:\n"
            "noora\n\n"
            "فقط حروف انگلیسی، عدد، خط تیره و آندرلاین مجاز است.",
            reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None),
        )
        return

    if text == "📋 لیست کاربران":
        await delete_last_inline(context, update.effective_chat.id, uid)
        try:
            update_traffic_and_enforce()
            con = db()
            rows = con.execute("SELECT * FROM users ORDER BY name").fetchall()
            con.close()

            if not rows:
                out = "کاربری وجود ندارد."
            else:
                msg = []
                for r in rows:
                    used = r["total_rx"] + r["total_tx"]
                    limit = r["limit_bytes"]
                    status = "فعال ✅" if r["enabled"] else "غیرفعال ⛔"
                    msg.append(
                        f"{r['name']} | {status}\n"
                        f"مصرف: {human_bytes(used)} / {human_bytes(limit)}\n"
                        f"انقضا: {r['expire_at'][:10]}\n"
                        f"IP: {r['ipv4']}"
                    )
                out = "\n\n".join(msg)
        except Exception as e:
            out = f"خطا:\n{e}"

        await update.message.reply_text(out[:3900], reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None))
        return

    if text == "📱 QR کاربر":
        await send_inline(
            update,
            context,
            "برای دریافت QR، کاربر را انتخاب کن:",
            reply_markup=users_action_keyboard("qr"),
        )
        return

    if text == "📄 فایل کانفیگ":
        await send_inline(
            update,
            context,
            "برای دریافت فایل کانفیگ، کاربر را انتخاب کن:",
            reply_markup=users_action_keyboard("config"),
        )
        return

    if text == "⛔ غیرفعال":
        await send_inline(
            update,
            context,
            "برای غیرفعال کردن، کاربر را انتخاب کن:",
            reply_markup=users_action_keyboard("disable"),
        )
        return

    if text == "✅ فعال‌سازی":
        await send_inline(
            update,
            context,
            "برای فعال‌سازی، کاربر را انتخاب کن:",
            reply_markup=users_action_keyboard("enable"),
        )
        return

    if text == "🗑 حذف کاربر":
        await send_inline(
            update,
            context,
            "برای حذف کامل، کاربر را انتخاب کن:",
            reply_markup=users_action_keyboard("delete"),
        )
        return

    if text == "➕ تمدید حجم/روز":
        await send_inline(
            update,
            context,
            "برای تمدید، کاربر را انتخاب کن:",
            reply_markup=users_action_keyboard("extend"),
        )
        return

    if text == "💬 چت با سازنده":
        await delete_last_inline(
            context,
            update.effective_chat.id,
            uid,
        )

        await update.message.reply_text(
            "💬 ارتباط با سازنده ربات\n\n"
            f"آیدی سازنده:\n@{CREATOR_USERNAME}\n\n"
            "برای شروع گفتگو روی دکمه زیر بزن.",
            reply_markup=creator_contact_keyboard(),
        )
        return

    if text == "⚙️ مدیریت":
        if not is_owner(update):
            return
        await send_inline(
            update,
            context,
            "بخش مدیریت اختصاصی مالک:",
            reply_markup=owner_only_keyboard(),
        )
        return

    if text == "📊 وضعیت سرور":
        await delete_last_inline(context, update.effective_chat.id, uid)
        try:
            out = pretty_server_status()
        except Exception as e:
            out = f"خطا:\n{e}"
        await update.message.reply_text(
            out[:3900],
            reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None)
        )
        return

    if text == "🆔 دریافت ID":
        await delete_last_inline(context, update.effective_chat.id, uid)

        chat = update.effective_chat
        user = update.effective_user

        out = "شناسه‌ها:\n\n"
        out += f"chat_id: {chat.id}\n"
        out += f"chat_type: {chat.type}\n"

        if chat.title:
            out += f"chat_title: {chat.title}\n"

        if user:
            out += f"user_id: {user.id}\n"
            out += f"username: @{user.username}\n" if user.username else "username: None\n"

        await update.message.reply_text(
            out,
            reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None)
        )
        return







def is_valid_domain(domain):
    return bool(re.match(r"^(?!-)[A-Za-z0-9.-]{3,253}(?<!-)$", domain)) and "." in domain


def is_valid_ipv4(ip):
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip)) and all(0 <= int(x) <= 255 for x in ip.split("."))


def resolve_domain_ipv4(domain):
    try:
        out = run(["bash", "-lc", f"getent ahostsv4 {domain} | awk '{{print $1}}' | head -n1"])
        return out.strip()
    except Exception:
        return ""



def test_https_domain(domain):
    checks = []

    resolved = resolve_domain_ipv4(domain)
    checks.append(f"DNS: {resolved or 'FAILED'}")

    try:
        run(["bash", "-lc", "nginx -t"])
        checks.append("Nginx config: OK")
    except Exception as e:
        checks.append(f"Nginx config: FAILED - {e}")

    try:
        certs = run(["bash", "-lc", f"certbot certificates | grep -A2 -B1 '{domain}' || true"])
        checks.append("Certificate: OK" if domain in certs else "Certificate: NOT FOUND")
    except Exception as e:
        checks.append(f"Certificate: FAILED - {e}")

    try:
        status = run(["bash", "-lc", f"curl -k -sS -I --max-time 20 https://{domain} | head -n1"])
        checks.append(f"HTTPS: {status or 'NO RESPONSE'}")
    except Exception as e:
        checks.append(f"HTTPS: FAILED - {e}")

    try:
        ping = run(["bash", "-lc", f"ping -c 2 -W 2 {domain} | tail -n 2 || true"])
        checks.append("Ping: " + (ping.replace("\\n", " | ") if ping else "NO RESPONSE"))
    except Exception as e:
        checks.append(f"Ping: FAILED - {e}")

    return "\\n".join(checks)



def create_full_backup():
    out = run(["bash", "-lc", "/usr/local/bin/awg-full-backup.sh"])
    path = out.strip().splitlines()[-1].strip()
    if not path or not Path(path).exists():
        raise RuntimeError("Backup file was not created.")
    return path



def is_valid_time_hhmm(value):
    m = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", value.strip())
    return bool(m)


def configure_backup_timer(time_hhmm):
    # Backward compatible wrapper
    return configure_backup_frequency(1)


def configure_backup_frequency(times_per_day):
    try:
        times_per_day = int(times_per_day)
    except Exception:
        raise RuntimeError("عدد نامعتبر است.")

    if times_per_day < 1 or times_per_day > 24:
        raise RuntimeError("عدد باید بین 1 تا 24 باشد.")

    interval_minutes = 1440 // times_per_day

    if 1440 % times_per_day != 0:
        raise RuntimeError(
            "برای تقسیم دقیق روز، یکی از این عددها را انتخاب کن:\n"
            "1, 2, 3, 4, 6, 8, 12, 24"
        )

    times = []
    for i in range(times_per_day):
        total = i * interval_minutes
        hour = total // 60
        minute = total % 60
        times.append(f"{hour:02d}:{minute:02d}")

    service = """[Unit]
Description=Create and send full AmneziaWG backup

[Service]
Type=oneshot
ExecStart=/usr/local/bin/awg-send-backup-telegram.sh
"""

    oncalendar_lines = "\n".join(
        f"OnCalendar=*-*-* {t}:00" for t in times
    )

    timer = f"""[Unit]
Description=Automatic AmneziaWG full backup

[Timer]
{oncalendar_lines}
Persistent=true
Unit=awg-full-backup.service

[Install]
WantedBy=timers.target
"""

    Path("/etc/systemd/system/awg-full-backup.service").write_text(service)
    Path("/etc/systemd/system/awg-full-backup.timer").write_text(timer)

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "awg-full-backup.timer"])
    run(["systemctl", "restart", "awg-full-backup.timer"])

    write_env_value("BACKUP_TIMES_PER_DAY", str(times_per_day))
    write_env_value("BACKUP_TIMES", ",".join(times))

    status = run(["bash", "-lc", "systemctl list-timers awg-full-backup.timer --no-pager || true"])

    return times, status


    hour, minute = time_hhmm.split(":")

    service = """[Unit]
Description=Create and send full AmneziaWG backup

[Service]
Type=oneshot
ExecStart=/usr/local/bin/awg-send-backup-telegram.sh
"""

    timer = f"""[Unit]
Description=Daily AmneziaWG full backup

[Timer]
OnCalendar=*-*-* {hour}:{minute}:00
Persistent=true
Unit=awg-full-backup.service

[Install]
WantedBy=timers.target
"""

    Path("/etc/systemd/system/awg-full-backup.service").write_text(service)
    Path("/etc/systemd/system/awg-full-backup.timer").write_text(timer)

    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", "awg-full-backup.timer"])
    run(["systemctl", "restart", "awg-full-backup.timer"])

    write_env_value("BACKUP_TIME", time_hhmm)

    status = run(["bash", "-lc", "systemctl list-timers awg-full-backup.timer --no-pager || true"])
    return status


def setup_domain_ssl(domain, static_ip, email):
    resolved = resolve_domain_ipv4(domain)

    if resolved and resolved != static_ip:
        raise RuntimeError(
            f"DNS دامنه هنوز روی IP سرور نیست.\n\n"
            f"Domain: {domain}\n"
            f"Resolved IP: {resolved}\n"
            f"Server IP: {static_ip}\n\n"
            f"اول A Record دامنه را روی {static_ip} بگذار، بعد دوباره اجرا کن."
        )

    if not resolved:
        raise RuntimeError(
            f"دامنه هنوز Resolve نمی‌شود.\n\n"
            f"یک A Record بساز:\n"
            f"{domain}  A  {static_ip}\n\n"
            f"بعد از چند دقیقه دوباره تست کن."
        )

    run(["bash", "-lc", "apt update && apt install -y nginx certbot python3-certbot-nginx"])

    site_conf = f"""
server {{
    listen 80;
    server_name {domain};

    location / {{
        proxy_pass http://127.0.0.1:1373;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }}
}}
"""

    path = Path(f"/etc/nginx/sites-available/{domain}")
    path.write_text(site_conf)

    enabled = Path(f"/etc/nginx/sites-enabled/{domain}")
    if not enabled.exists():
        run(["ln", "-s", str(path), str(enabled)])

    run(["bash", "-lc", "nginx -t"])
    run(["systemctl", "enable", "--now", "nginx"])
    run(["systemctl", "reload", "nginx"])

    run(["bash", "-lc", "iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT || true"])
    run(["bash", "-lc", "iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT || true"])
    run(["bash", "-lc", "netfilter-persistent save || true"])

    cert_cmd = (
        f"certbot --nginx -d {domain} "
        f"--non-interactive --agree-tos "
        f"--email {email} "
        f"--redirect"
    )
    run(["bash", "-lc", cert_cmd])

    run(["bash", "-lc", "nginx -t"])
    run(["systemctl", "reload", "nginx"])

    # Final real HTTPS check
    run(["bash", "-lc", f"curl -k -sS -I --max-time 20 https://{domain} | head -n1"])

    return f"https://{domain}"


async def manage_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BACKUP_CHAT_ID, BACKUP_LINK

    if not is_owner(update):
        return False

    uid = update.effective_user.id
    if uid not in PENDING_MANAGE:
        return False

    text = (update.message.text or "").strip()
    state = PENDING_MANAGE[uid]
    step = state.get("step")

    if text in ["/cancel", "cancel", "لغو"]:
        PENDING_MANAGE.pop(uid, None)
        await update.message.reply_text(
            "عملیات مدیریتی لغو شد.",
            reply_markup=main_keyboard(uid),
        )
        return True

    if step == "add_admin":
        try:
            new_admin = int(text)
        except Exception:
            await update.message.reply_text("ID نامعتبر است. فقط عدد بفرست.")
            return True

        if new_admin in ADMINS:
            PENDING_MANAGE.pop(uid, None)
            await update.message.reply_text(
                "این ID از قبل ادمین است.",
                reply_markup=main_keyboard(uid),
            )
            return True

        ADMINS.add(new_admin)
        save_admins()
        PENDING_MANAGE.pop(uid, None)

        await update.message.reply_text(
            f"ادمین اضافه شد:\n{new_admin}",
            reply_markup=main_keyboard(uid),
        )
        return True

    if step == "remove_admin":
        try:
            remove_id = int(text)
        except Exception:
            await update.message.reply_text("ID نامعتبر است. فقط عدد بفرست.")
            return True

        if remove_id == OWNER_ID:
            PENDING_MANAGE.pop(uid, None)
            await update.message.reply_text(
                "مالک اصلی قابل حذف نیست.",
                reply_markup=main_keyboard(uid),
            )
            return True

        if remove_id not in ADMINS:
            PENDING_MANAGE.pop(uid, None)
            await update.message.reply_text(
                "این ID داخل لیست ادمین‌ها نیست.",
                reply_markup=main_keyboard(uid),
            )
            return True

        ADMINS.discard(remove_id)
        save_admins()
        PENDING_MANAGE.pop(uid, None)

        await update.message.reply_text(
            f"ادمین حذف شد:\n{remove_id}",
            reply_markup=main_keyboard(uid),
        )
        return True

    if step == "backup_link":
        if not (
            text.startswith("https://t.me/")
            or text.startswith("http://t.me/")
            or text.startswith("t.me/")
        ):
            await update.message.reply_text(
                "لینک نامعتبر است.\n\n"
                "لینک باید با t.me یا https://t.me شروع شود.\n\n"
                "مثال:\n"
                "https://t.me/+xxxxxxxx"
            )
            return True

        state["backup_link"] = text
        state["step"] = "backup_id"

        await update.message.reply_text(
            "لینک ذخیره شد.\n\n"
            "حالا ID عددی کانال/گروه بکاپ را بفرست.\n\n"
            "نکته مهم:\n"
            "ربات باید داخل همان کانال/گروه Admin باشد.\n\n"
            "مثال:\n"
            "-1001234567890"
        )
        return True

    if step == "backup_id":
        try:
            backup_id = int(text)
        except Exception:
            await update.message.reply_text(
                "ID کانال/گروه نامعتبر است. باید عدد باشد.\n\n"
                "مثال:\n"
                "-1001234567890"
            )
            return True

        BACKUP_LINK = state.get("backup_link", "")
        BACKUP_CHAT_ID = str(backup_id)

        write_env_value("BACKUP_LINK", BACKUP_LINK)
        write_env_value("BACKUP_CHAT_ID", BACKUP_CHAT_ID)

        PENDING_MANAGE.pop(uid, None)

        await update.message.reply_text(
            "کانال بکاپ تنظیم شد.\n\n"
            f"Link: {BACKUP_LINK}\n"
            f"Chat ID: {BACKUP_CHAT_ID}\n\n"
            "از این به بعد کانفیگ کاربران جدید برای کانال بکاپ هم ارسال می‌شود.",
            reply_markup=main_keyboard(uid),
        )
        return True

    if step == "domain_name":
        domain = text.lower().strip()

        if not is_valid_domain(domain):
            await update.message.reply_text(
                "دامنه نامعتبر است.\n\n"
                "مثال درست:\n"
                "panel.example.com"
            )
            return True

        state["domain"] = domain
        state["step"] = "domain_ip"

        await update.message.reply_text(
            f"دامنه ثبت شد:\n{domain}\n\n"
            "حالا IP استاتیک سرور را بفرست.\n\n"
            "برای این سرور معمولاً این است:\n"
            "203.0.113.10"
        )
        return True

    if step == "domain_ip":
        static_ip = text.strip()

        if not is_valid_ipv4(static_ip):
            await update.message.reply_text(
                "IP نامعتبر است.\n\n"
                "مثال:\n"
                "203.0.113.10"
            )
            return True

        domain = state["domain"]
        resolved = resolve_domain_ipv4(domain)

        if not resolved:
            await update.message.reply_text(
                "دامنه هنوز Resolve نمی‌شود.\n\n"
                f"در DNS دامنه این رکورد را بساز:\n"
                f"{domain}  A  {static_ip}\n\n"
                "بعد از چند دقیقه دوباره همین IP را بفرست."
            )
            return True

        if resolved != static_ip:
            await update.message.reply_text(
                "DNS دامنه هنوز روی IP درست نیست.\n\n"
                f"دامنه: {domain}\n"
                f"IP فعلی دامنه: {resolved}\n"
                f"IP سرور: {static_ip}\n\n"
                f"A Record دامنه را روی {static_ip} تنظیم کن، بعد دوباره IP را بفرست."
            )
            return True

        state["static_ip"] = static_ip
        state["step"] = "domain_email"

        await update.message.reply_text(
            "DNS درست است.\n\n"
            "حالا ایمیل برای صدور SSL را بفرست.\n\n"
            "مثال:\n"
            "admin@example.com"
        )
        return True

    if step == "domain_email":
        email = text.strip()

        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            await update.message.reply_text(
                "ایمیل نامعتبر است.\n\n"
                "مثال:\n"
                "admin@example.com"
            )
            return True

        domain = state["domain"]
        static_ip = state["static_ip"]

        set_busy("تنظیم دامنه و SSL", uid)

        await update.message.reply_text(
            "⏳ عملیات شروع شد.\n\n"
            "در حال نصب و تنظیم دامنه و SSL...\n\n"
            f"Domain: {domain}\n"
            f"IP: {static_ip}\n"
            f"Email: {email}\n\n"
            "تا پایان عملیات، دکمه‌های بات موقتاً قفل هستند.\n"
            "ممکن است ۱ تا ۳ دقیقه طول بکشد."
        )

        try:
            url = await asyncio.to_thread(setup_domain_ssl, domain, static_ip, email)
            checks = await asyncio.to_thread(test_https_domain, domain)

            write_env_value("PANEL_DOMAIN", domain)
            write_env_value("PANEL_URL", url)

            PENDING_MANAGE.pop(uid, None)
            clear_busy()

            await update.message.reply_text(
                "✅ دامنه و SSL با موفقیت تنظیم شد.\n\n"
                f"آدرس پنل:\n{url}\n\n"
                "نتیجه تست‌ها:\n"
                f"{checks}",
                reply_markup=main_keyboard(uid),
            )
            return True

        except Exception as e:
            checks = ""
            try:
                checks = await asyncio.to_thread(test_https_domain, domain)
            except Exception as ee:
                checks = f"تست نهایی هم خطا داد:\n{ee}"

            PENDING_MANAGE.pop(uid, None)
            clear_busy()

            await update.message.reply_text(
                "❌ خطا در تنظیم دامنه/SSL\n\n"
                f"خطا:\n{e}\n\n"
                "وضعیت تست‌ها:\n"
                f"{checks}",
                reply_markup=main_keyboard(uid),
            )
            return True


    if step == "backup_frequency":
        count_text = text.strip()

        try:
            count = int(count_text)
        except Exception:
            await update.message.reply_text(
                "عدد نامعتبر است.\n\n"
                "مثال:\n"
                "4"
            )
            return True

        await update.message.reply_text(
            "در حال تنظیم بکاپ خودکار...\n\n"
            f"تعداد بکاپ در روز: {count}"
        )

        try:
            times, status = configure_backup_frequency(count)
            PENDING_MANAGE.pop(uid, None)

            await update.message.reply_text(
                "✅ بکاپ خودکار تنظیم شد.\n\n"
                f"تعداد بکاپ در روز: {count}\n"
                f"ساعت‌های اجرا:\n{', '.join(times)}\n\n"
                f"{status[:1500]}",
                reply_markup=main_keyboard(uid),
            )
            return True

        except Exception as e:
            PENDING_MANAGE.pop(uid, None)
            await update.message.reply_text(
                f"❌ خطا در تنظیم بکاپ خودکار:\n\n{e}",
                reply_markup=main_keyboard(uid),
            )
            return True


    return False


async def extend_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return False

    uid = update.effective_user.id
    if uid not in PENDING_EXTEND:
        return False

    text = (update.message.text or "").strip()
    state = PENDING_EXTEND[uid]

    if text in ["/cancel", "cancel", "لغو"]:
        PENDING_EXTEND.pop(uid, None)
        await update.message.reply_text("عملیات تمدید لغو شد.", reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None))
        return True

    name = state["name"]

    if state["step"] == "gb":
        try:
            gb = int(text)
            if gb < 0 or gb > 10000:
                raise ValueError()
        except Exception:
            await update.message.reply_text("حجم نامعتبر است. فقط عدد بفرست. مثال: 10")
            return True

        state["gb"] = gb
        state["step"] = "days"
        await update.message.reply_text(
            f"حجم اضافه: {gb}GB\n\n"
            "چند روز اضافه شود؟\n\n"
            "مثال:\n"
            "15"
        )
        return True

    if state["step"] == "days":
        try:
            days = int(text)
            if days < 0 or days > 3650:
                raise ValueError()
        except Exception:
            await update.message.reply_text("تعداد روز نامعتبر است. فقط عدد بفرست. مثال: 15")
            return True

        gb = state["gb"]

        con = db()
        row = con.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()

        if not row:
            con.close()
            PENDING_EXTEND.pop(uid, None)
            await update.message.reply_text("کاربر پیدا نشد.", reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None))
            return True

        new_limit = row["limit_bytes"] + gb * 1024 * 1024 * 1024

        exp = datetime.fromisoformat(row["expire_at"])
        now = datetime.now(timezone.utc)
        if exp < now:
            exp = now
        new_exp = exp + timedelta(days=days)

        con.execute(
            "UPDATE users SET limit_bytes=?, expire_at=? WHERE name=?",
            (new_limit, new_exp.isoformat(), name)
        )
        con.commit()
        con.close()

        # If user was disabled, enable again after extension
        try:
            if not row["enabled"]:
                enable_user(name)
        except Exception:
            pass

        PENDING_EXTEND.pop(uid, None)

        await update.message.reply_text(
            f"تمدید انجام شد.\n\n"
            f"User: {name}\n"
            f"Added GB: {gb}\n"
            f"Added days: {days}\n"
            f"New limit: {human_bytes(new_limit)}\n"
            f"New expire: {new_exp.date()}",
            reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None)
        )
        return True

    return False



def install_links_text():
    return (
        "📲 لینک نصب برنامه AmneziaWG\n\n"
        "برای اتصال، اول برنامه را نصب کن، بعد فایل کانفیگ یا QR را داخل برنامه Import کن.\n\n"
        "🍏 iPhone / iPad:\n"
        "https://apps.apple.com/us/app/amneziawg/id6478942365\n\n"
        "🤖 Android - Google Play:\n"
        "https://play.google.com/store/apps/details?id=org.amnezia.vpn\n\n"
        "🤖 Android - APK / همه دانلودها:\n"
        "https://amnezia.org/downloads\n\n"
        "🪟 Windows:\n"
        "https://amnezia.org/downloads\n\n"
        "راهنمای سریع:\n"
        "1. برنامه را نصب کن.\n"
        "2. گزینه Import / Add tunnel را بزن.\n"
        "3. QR را اسکن کن یا فایل conf را وارد کن.\n"
        "4. اتصال را روشن کن."
    )


async def send_install_links(chat_id, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=chat_id,
        text=install_links_text(),
        disable_web_page_preview=True,
    )



async def add_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    uid = update.effective_user.id

    if LICENSE_REQUIRED:
        result = await asyncio.to_thread(check_license_remote, uid, False)
        if not result.get("valid"):
            await handle_license_text(update, context)
            return

    if uid in PENDING_MANAGE:
        handled = await manage_flow_handler(update, context)
        if handled:
            return

    if uid in PENDING_EXTEND:
        handled = await extend_flow_handler(update, context)
        if handled:
            return

    if uid not in PENDING_ADD:
        await main_text_router(update, context)
        return

    text = (update.message.text or "").strip()
    state = PENDING_ADD[uid]

    if text in ["/cancel", "cancel", "لغو"]:
        PENDING_ADD.pop(uid, None)
        await update.message.reply_text("عملیات ساخت کاربر لغو شد.", reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None))
        return

    if state["step"] == "name":
        name = text

        if not re.match(r"^[A-Za-z0-9_-]{1,15}$", name):
            await update.message.reply_text(
                "اسم نامعتبر است. فقط حروف انگلیسی، عدد، _ و - حداکثر ۱۵ کاراکتر.\n\n"
                "دوباره اسم را بفرست:"
            )
            return

        con = db()
        exists = con.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone()
        con.close()

        if exists:
            await update.message.reply_text("این اسم قبلاً وجود دارد. یک اسم دیگر بفرست:")
            return

        state["name"] = name
        state["step"] = "gb"
        await update.message.reply_text(
            f"اسم کاربر: {name}\n\n"
            "حجم چند گیگ باشد؟\n\n"
            "مثال:\n"
            "20"
        )
        return

    if state["step"] == "gb":
        try:
            gb = int(text)
            if gb <= 0 or gb > 10000:
                raise ValueError()
        except Exception:
            await update.message.reply_text("حجم نامعتبر است. فقط عدد بفرست. مثال: 20")
            return

        state["gb"] = gb
        state["step"] = "days"
        await update.message.reply_text(
            f"حجم: {gb}GB\n\n"
            "اعتبار چند روز باشد؟\n\n"
            "مثال:\n"
            "30"
        )
        return

    if state["step"] == "days":
        try:
            days = int(text)
            if days <= 0 or days > 3650:
                raise ValueError()
        except Exception:
            await update.message.reply_text("تعداد روز نامعتبر است. فقط عدد بفرست. مثال: 30")
            return

        name = state["name"]
        gb = state["gb"]
        limit_bytes = gb * 1024 * 1024 * 1024
        expire_at = datetime.now(timezone.utc) + timedelta(days=days)

        await update.message.reply_text(
            f"در حال ساخت کاربر...\n\n"
            f"Name: {name}\n"
            f"Limit: {gb}GB\n"
            f"Days: {days}"
        )

        try:
            priv = genkey()
            pub = pubkey(priv)
            psk = genpsk()
            ipv4 = next_ipv4()

            config = make_client_config(name, priv, psk, ipv4)
            path = save_client_config(name, config)

            append_peer_to_conf(name, pub, psk, ipv4)
            sync_awg()

            con = db()
            con.execute(
                """INSERT INTO users
                (name, private_key, public_key, psk, ipv4, ipv6, limit_bytes, expire_at, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    name,
                    priv,
                    pub,
                    psk,
                    ipv4,
                    None,
                    limit_bytes,
                    expire_at.isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            con.commit()
            con.close()

            PENDING_ADD.pop(uid, None)

            await update.message.reply_text(
                f"کاربر ساخته شد.\n\n"
                f"Name: {name}\n"
                f"IP: {ipv4}\n"
                f"Limit: {gb}GB\n"
                f"Expire: {expire_at.date()}",
                reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None),
            )

            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=open(path, "rb"),
                filename=path.name,
            )

            png = qr_png(config)
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=open(png, "rb"),
                caption=f"QR: {name}",
            )
            os.unlink(png)

            await send_install_links(update.effective_chat.id, context)

            if BACKUP_CHAT_ID:
                await context.bot.send_document(
                    int(BACKUP_CHAT_ID),
                    document=open(path, "rb"),
                    filename=path.name,
                    caption=f"Backup config: {name}",
                )

        except Exception as e:
            PENDING_ADD.pop(uid, None)
            await update.message.reply_text(
                f"خطا در ساخت کاربر:\n{e}",
                reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None),
            )
        return


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    update_traffic_and_enforce()
    con = db()
    rows = con.execute("SELECT * FROM users ORDER BY name").fetchall()
    con.close()
    if not rows:
        await update.message.reply_text("No users.")
        return

    msg = []
    for r in rows:
        used = r["total_rx"] + r["total_tx"]
        limit = r["limit_bytes"]
        status = "active" if r["enabled"] else "disabled"
        msg.append(
            f"{r['name']} | {status}\n"
            f"Used: {human_bytes(used)} / {human_bytes(limit)}\n"
            f"Expire: {r['expire_at'][:10]}\n"
            f"IP: {r['ipv4']}"
        )
    await update.message.reply_text("\n\n".join(msg))


async def send_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /config name")
        return
    name = context.args[0]
    path = CLIENT_DIR / f"awg0-client-{name}.conf"
    if not path.exists():
        await update.message.reply_text("Config not found.")
        return
    await update.message.reply_document(document=open(path, "rb"), filename=path.name)


async def send_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /qr name")
        return
    name = context.args[0]
    path = CLIENT_DIR / f"awg0-client-{name}.conf"
    if not path.exists():
        await update.message.reply_text("Config not found.")
        return
    png = qr_png(path.read_text())
    await update.message.reply_photo(photo=open(png, "rb"))
    os.unlink(png)


async def disable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /disable name")
        return
    disable_user(context.args[0])
    await update.message.reply_text("Disabled.")


async def enable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /enable name")
        return
    enable_user(context.args[0])
    await update.message.reply_text("Enabled.")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /delete name")
        return
    name = context.args[0]
    disable_user(name)
    con = db()
    con.execute("DELETE FROM users WHERE name=?", (name,))
    con.commit()
    con.close()
    path = CLIENT_DIR / f"awg0-client-{name}.conf"
    if path.exists():
        path.unlink()
    await update.message.reply_text("Deleted.")


async def extend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /extend name gb days")
        return
    name = context.args[0]
    add_gb = int(context.args[1])
    add_days = int(context.args[2])
    con = db()
    row = con.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
    if not row:
        con.close()
        await update.message.reply_text("User not found.")
        return
    new_limit = row["limit_bytes"] + add_gb * 1024 * 1024 * 1024
    exp = datetime.fromisoformat(row["expire_at"])
    if exp < datetime.now(timezone.utc):
        exp = datetime.now(timezone.utc)
    exp = exp + timedelta(days=add_days)
    con.execute("UPDATE users SET limit_bytes=?, expire_at=? WHERE name=?", (new_limit, exp.isoformat(), name))
    con.commit()
    con.close()
    await update.message.reply_text("Extended.")




def peer_db_map():
    try:
        import_panel_users()
    except Exception:
        pass

    con = db()
    rows = con.execute("SELECT * FROM users").fetchall()
    con.close()

    return {r["public_key"]: r for r in rows}


def pretty_duration_from_ts(ts):
    try:
        ts = int(ts)
    except Exception:
        return "ندارد"

    if ts <= 0:
        return "ندارد"

    now = int(datetime.now(timezone.utc).timestamp())
    diff = max(now - ts, 0)

    if diff < 60:
        return "همین الان"

    minutes = diff // 60
    if minutes < 60:
        return f"{minutes} دقیقه پیش"

    hours = minutes // 60
    if hours < 24:
        return f"{hours} ساعت پیش"

    days = hours // 24
    return f"{days} روز پیش"


def status_from_handshake(ts):
    try:
        ts = int(ts)
    except Exception:
        return "🔴 بدون اتصال"

    if ts <= 0:
        return "🔴 هنوز وصل نشده"

    now = int(datetime.now(timezone.utc).timestamp())
    diff = max(now - ts, 0)

    if diff <= 180:
        return "🟢 آنلاین"

    if diff <= 3600:
        return "🟡 اخیراً وصل بوده"

    return "⚫ آفلاین"


def clean_ip(allowed_ips):
    if not allowed_ips:
        return "-"
    first = allowed_ips.split(",")[0].strip()
    return first.replace("/32", "")


def pretty_server_status():
    rows = peer_db_map()

    dump = awg("show", AWG_IFACE, "dump").splitlines()
    if not dump:
        return "اطلاعاتی از سرور دریافت نشد."

    server_line = dump[0].split("\t")
    server_key = server_line[0] if len(server_line) > 0 else ""
    port = server_line[2] if len(server_line) > 2 else str(AWG_PORT)

    peers = []
    total_used = 0
    online_count = 0
    recent_count = 0
    offline_count = 0

    for line in dump[1:]:
        parts = line.split("\t")
        if len(parts) < 7:
            continue

        pub = parts[0]
        endpoint = parts[2] if len(parts) > 2 else ""
        allowed = parts[3] if len(parts) > 3 else ""
        latest = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        rx = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
        tx = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else 0

        used = rx + tx
        total_used += used

        dbrow = rows.get(pub)
        name = dbrow["name"] if dbrow else pub[:10]
        limit_bytes = int(dbrow["limit_bytes"]) if dbrow else 0
        expire_at = dbrow["expire_at"][:10] if dbrow else "-"

        remaining = max(limit_bytes - used, 0) if limit_bytes else 0

        status = status_from_handshake(latest)
        last_seen = pretty_duration_from_ts(latest)

        if status.startswith("🟢"):
            online_count += 1
        elif status.startswith("🟡"):
            recent_count += 1
        else:
            offline_count += 1

        peers.append({
            "name": name,
            "status": status,
            "last_seen": last_seen,
            "used": used,
            "rx": rx,
            "tx": tx,
            "limit": limit_bytes,
            "remaining": remaining,
            "expire": expire_at,
            "ip": clean_ip(allowed),
            "endpoint": endpoint if endpoint and endpoint != "(none)" else "-",
        })

    text = "📊 وضعیت سرور AmneziaWG\n\n"
    text += f"🧩 Interface: {AWG_IFACE}\n"
    text += f"🔌 Port: {port}\n"
    if server_key:
        text += f"🔑 Server Key: {server_key[:12]}...\n"

    text += "\n"
    text += f"👥 تعداد کاربران: {len(peers)}\n"
    text += f"🟢 آنلاین: {online_count}\n"
    text += f"🟡 اخیراً وصل بوده: {recent_count}\n"
    text += f"🔴 آفلاین/بدون اتصال: {offline_count}\n"
    text += f"📦 مصرف کل: {human_bytes(total_used)}\n"

    if not peers:
        text += "\nکاربری وجود ندارد."
        return text

    text += "\n━━━━━━━━━━━━━━\n"

    for peer in peers:
        text += f"\n👤 {peer['name']}\n"
        text += f"وضعیت: {peer['status']}\n"
        text += f"آخرین اتصال: {peer['last_seen']}\n"
        text += f"مصرف کل: {human_bytes(peer['used'])}\n"
        text += f"دریافت/ارسال: {human_bytes(peer['rx'])} / {human_bytes(peer['tx'])}\n"

        if peer["limit"]:
            text += f"حجم مجاز: {human_bytes(peer['limit'])}\n"
            text += f"باقی‌مانده: {human_bytes(peer['remaining'])}\n"

        text += f"انقضا: {peer['expire']}\n"
        text += f"IP داخلی: {peer['ip']}\n"

        if peer["endpoint"] != "-":
            text += f"Endpoint: {peer['endpoint']}\n"

        text += "━━━━━━━━━━━━━━\n"

    return text[:3900]




async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    try:
        out = pretty_server_status()
    except Exception as e:
        out = f"خطا:\n{e}"
    await update.message.reply_text(out[:3900])


async def quota_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        update_traffic_and_enforce()
    except Exception as e:
        print("quota error:", e)


def main():
    init_db()
    try:
        import_panel_users()
    except Exception as e:
        print("startup import panel users error:", e)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", chat_id))
    app.add_handler(MessageHandler(filters.Regex(r"^/id(@\\w+)?$"), chat_id))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("config", send_config))
    app.add_handler(CommandHandler("qr", send_qr))
    app.add_handler(CommandHandler("disable", disable_cmd))
    app.add_handler(CommandHandler("enable", enable_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("extend", extend_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_flow_handler))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_repeating(quota_job, interval=60, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()