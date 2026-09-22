import os
import asyncio
import re
import sqlite3
import subprocess
import ipaddress
import tempfile
import json
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
OWNER_ID = int(ENV.get("OWNER_ID", str(DEFAULT_OWNER_ID)).strip() or DEFAULT_OWNER_ID)
ADMINS.add(OWNER_ID)

CREATOR_USERNAME = "awgdeveloper"
CREATOR_URL = f"https://t.me/{CREATOR_USERNAME}"


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
    title = BOT_BUSY.get("title") or "غŒع© ط¹ظ…ظ„غŒط§طھ ظ…ط¯غŒط±غŒطھغŒ"
    return (
        f"âڈ³ {title} ط¯ط± ط­ط§ظ„ ط§ظ†ط¬ط§ظ… ط§ط³طھ.\n\n"
        "طھط§ ظ¾ط§غŒط§ظ† ط¹ظ…ظ„غŒط§طھطŒ ط¯ع©ظ…ظ‡â€Œظ‡ط§ ظˆ ط¯ط³طھظˆط±ظ‡ط§ ظ…ظˆظ‚طھط§ظ‹ ط؛غŒط±ظپط¹ط§ظ„ ظ‡ط³طھظ†ط¯.\n"
        "ع†ظ†ط¯ ظ„ط­ط¸ظ‡ طµط¨ط± ع©ظ†."
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
        raise RuntimeError("ط´ظ…ط§ط±ظ‡ ظ†ط³ط®ظ‡ ط¯ط±غŒط§ظپطھâ€Œط´ط¯ظ‡ ط§ط² GitHub ظ…ط¹طھط¨ط± ظ†غŒط³طھ.")
    return value


def start_update_job(chat_id):
    if not UPDATE_SCRIPT.exists():
        raise RuntimeError(f"ط§ط³ع©ط±غŒظ¾طھ ط¨ط±ظˆط²ط±ط³ط§ظ†غŒ ظ¾غŒط¯ط§ ظ†ط´ط¯: {UPDATE_SCRIPT}")

    run([
        "systemd-run",
        "--collect",
        str(UPDATE_SCRIPT),
        str(int(chat_id)),
    ])



def owner_only_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("ًں‘¥ ظ†ظ…ط§غŒط´ ط§ط¯ظ…غŒظ†â€Œظ‡ط§", callback_data="manage:list_admins"),
        ],
        [
            InlineKeyboardButton("â‍• ط§ظپط²ظˆط¯ظ† ط§ط¯ظ…غŒظ†", callback_data="manage:add_admin"),
            InlineKeyboardButton("â‍– ط­ط°ظپ ط§ط¯ظ…غŒظ†", callback_data="manage:remove_admin"),
        ],
        [
            InlineKeyboardButton("ًں“¦ طھظ†ط¸غŒظ… ع©ط§ظ†ط§ظ„ ط¨ع©ط§ظ¾", callback_data="manage:set_backup_channel"),
        ],
        [
            InlineKeyboardButton("ًںŒگ طھظ†ط¸غŒظ… ط¯ط§ظ…ظ†ظ‡ ظˆ SSL", callback_data="manage:set_domain_ssl"),
        ],
        [
            InlineKeyboardButton("ًں’¾ ع¯ط±ظپطھظ† ط¨ع©ط§ظ¾ ع©ط§ظ…ظ„", callback_data="manage:backup_now"),
        ],
        [
            InlineKeyboardButton("ًں”پ طھط¹ط¯ط§ط¯ ط¨ع©ط§ظ¾ ط±ظˆط²ط§ظ†ظ‡", callback_data="manage:set_backup_time"),
        ],
        [
            InlineKeyboardButton("â¬†ï¸ڈ ط¨ط±ظˆط²ط±ط³ط§ظ†غŒ ط±ط¨ط§طھ", callback_data="manage:update_bot"),
        ],
        [
            InlineKeyboardButton("ًں“‹ ظ†ظ…ط§غŒط´ طھظ†ط¸غŒظ…ط§طھ", callback_data="manage:show_settings"),
        ],
        [
            InlineKeyboardButton("â¬…ï¸ڈ ط¨ط§ط²ع¯ط´طھ", callback_data="menu:back"),
        ],
    ])


async def guard(update: Update):
    if not is_admin(update):
        await update.message.reply_text("Access denied.")
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
        ["â‍• ط³ط§ط®طھ ع©ط§ط±ط¨ط±", "ًں“‹ ظ„غŒط³طھ ع©ط§ط±ط¨ط±ط§ظ†"],
        ["ًں“± QR ع©ط§ط±ط¨ط±", "ًں“„ ظپط§غŒظ„ ع©ط§ظ†ظپغŒع¯"],
        ["â›” ط؛غŒط±ظپط¹ط§ظ„", "âœ… ظپط¹ط§ظ„â€Œط³ط§ط²غŒ"],
        ["â‍• طھظ…ط¯غŒط¯ ط­ط¬ظ…/ط±ظˆط²", "ًں—‘ ط­ط°ظپ ع©ط§ط±ط¨ط±"],
        ["ًں“ٹ ظˆط¶ط¹غŒطھ ط³ط±ظˆط±", "ًں†” ط¯ط±غŒط§ظپطھ ID"],
        ["ًں’¬ ع†طھ ط¨ط§ ط³ط§ط²ظ†ط¯ظ‡"],
    ]

    if user_id == OWNER_ID:
        rows.append(["âڑ™ï¸ڈ ظ…ط¯غŒط±غŒطھ"])

    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
    )


def creator_contact_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "ًں’¬ ط´ط±ظˆط¹ ع†طھ ط¨ط§ ط³ط§ط²ظ†ط¯ظ‡",
                url=CREATOR_URL,
            ),
        ],
    ])


def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("â¬…ï¸ڈ ط¨ط§ط²ع¯ط´طھ ط¨ظ‡ ظ…ظ†ظˆ", callback_data="menu:back")]
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
        "ظ¾ظ†ظ„ ظ…ط¯غŒط±غŒطھ AmneziaWG ط¢ظ…ط§ط¯ظ‡ ط§ط³طھ.",
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
        status = "âœ…" if r["enabled"] else "â›”"
        row_buttons.append(
            InlineKeyboardButton(f"{status} {name}", callback_data=f"user:{action}:{name}")
        )
        if len(row_buttons) == 2:
            buttons.append(row_buttons)
            row_buttons = []

    if row_buttons:
        buttons.append(row_buttons)

    buttons.append([InlineKeyboardButton("â¬…ï¸ڈ ط¨ط§ط²ع¯ط´طھ ط¨ظ‡ ظ…ظ†ظˆ", callback_data="menu:back")])
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
            text="ظ¾ظ†ظ„ ظ…ط¯غŒط±غŒطھ AmneziaWG ط¢ظ…ط§ط¯ظ‡ ط§ط³طھ.",
            reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None)
        )
    else:
        await send_main_menu(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("Access denied.")
        return
    await show_menu_message(update, context)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query.from_user or query.from_user.id not in ADMINS:
        await query.edit_message_text("Access denied.")
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
            text="ط¨ظ‡ ظ…ظ†ظˆغŒ ط§طµظ„غŒ ط¨ط±ع¯ط´طھغŒ.",
            reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None)
        )
        return

    if data == "menu:add":
        PENDING_ADD[query.from_user.id] = {"step": "name"}
        await query.edit_message_text(
            "ط§ط³ظ… ع©ط§ط±ط¨ط± ط¬ط¯غŒط¯ ط±ط§ ط¨ظپط±ط³طھ.\n\n"
            "ظ…ط«ط§ظ„:\n"
            "noora\n\n"
            "ظپظ‚ط· ط­ط±ظˆظپ ط§ظ†ع¯ظ„غŒط³غŒطŒ ط¹ط¯ط¯طŒ ط®ط· طھغŒط±ظ‡ ظˆ ط¢ظ†ط¯ط±ظ„ط§غŒظ† ظ…ط¬ط§ط² ط§ط³طھ.",
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
                text = "ع©ط§ط±ط¨ط±غŒ ظˆط¬ظˆط¯ ظ†ط¯ط§ط±ط¯."
            else:
                msg = []
                for r in rows:
                    used = r["total_rx"] + r["total_tx"]
                    limit = r["limit_bytes"]
                    status = "ظپط¹ط§ظ„ âœ…" if r["enabled"] else "ط؛غŒط±ظپط¹ط§ظ„ â›”"
                    msg.append(
                        f"{r['name']} | {status}\n"
                        f"ظ…طµط±ظپ: {human_bytes(used)} / {human_bytes(limit)}\n"
                        f"ط§ظ†ظ‚ط¶ط§: {r['expire_at'][:10]}\n"
                        f"IP: {r['ipv4']}"
                    )
                text = "\n\n".join(msg)
        except Exception as e:
            text = f"ط®ط·ط§:\n{e}"

        await query.edit_message_text(text[:3900], reply_markup=back_keyboard())
        return

    if data == "menu:qr":
        await query.edit_message_text(
            "ط¨ط±ط§غŒ ط¯ط±غŒط§ظپطھ QRطŒ ع©ط§ط±ط¨ط± ط±ط§ ط§ظ†طھط®ط§ط¨ ع©ظ†:",
            reply_markup=users_action_keyboard("qr"),
        )
        return

    if data == "menu:config":
        await query.edit_message_text(
            "ط¨ط±ط§غŒ ط¯ط±غŒط§ظپطھ ظپط§غŒظ„ ع©ط§ظ†ظپغŒع¯طŒ ع©ط§ط±ط¨ط± ط±ط§ ط§ظ†طھط®ط§ط¨ ع©ظ†:",
            reply_markup=users_action_keyboard("config"),
        )
        return

    if data == "menu:disable":
        await query.edit_message_text(
            "ط¨ط±ط§غŒ ط؛غŒط±ظپط¹ط§ظ„ ع©ط±ط¯ظ†طŒ ع©ط§ط±ط¨ط± ط±ط§ ط§ظ†طھط®ط§ط¨ ع©ظ†:",
            reply_markup=users_action_keyboard("disable"),
        )
        return

    if data == "menu:enable":
        await query.edit_message_text(
            "ط¨ط±ط§غŒ ظپط¹ط§ظ„â€Œط³ط§ط²غŒطŒ ع©ط§ط±ط¨ط± ط±ط§ ط§ظ†طھط®ط§ط¨ ع©ظ†:",
            reply_markup=users_action_keyboard("enable"),
        )
        return

    if data == "menu:delete":
        await query.edit_message_text(
            "ط¨ط±ط§غŒ ط­ط°ظپ ع©ط§ظ…ظ„طŒ ع©ط§ط±ط¨ط± ط±ط§ ط§ظ†طھط®ط§ط¨ ع©ظ†:",
            reply_markup=users_action_keyboard("delete"),
        )
        return

    if data == "menu:extend":
        await query.edit_message_text(
            "طھظ…ط¯غŒط¯ ط­ط¬ظ… ظˆ ط±ظˆط²:\n\n/extend name gb days\n\nظ…ط«ط§ظ„:\n/extend noora 10 15\n\nغŒط¹ظ†غŒ غ±غ° ع¯غŒع¯ ظˆ غ±غµ ط±ظˆط² ط¨ظ‡ ع©ط§ط±ط¨ط± ط§ط¶ط§ظپظ‡ ظ…غŒâ€Œط´ظˆط¯.",
            reply_markup=back_keyboard(),
        )
        return

    if data.startswith("user:"):
        try:
            _, action, name = data.split(":", 2)
            row = get_user_row(name)
            if not row:
                await query.edit_message_text("ع©ط§ط±ط¨ط± ظ¾غŒط¯ط§ ظ†ط´ط¯.", reply_markup=back_keyboard())
                return

            LAST_INLINE.pop(query.from_user.id, None)

            if action == "qr":
                path = CLIENT_DIR / f"awg0-client-{name}.conf"
                if not path.exists():
                    await query.edit_message_text("ظپط§غŒظ„ ع©ط§ظ†ظپغŒع¯ ظ¾غŒط¯ط§ ظ†ط´ط¯.", reply_markup=back_keyboard())
                    return
                png = qr_png(path.read_text())
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=open(png, "rb"), caption=f"QR: {name}")
                os.unlink(png)
                await query.edit_message_text("QR ط§ط±ط³ط§ظ„ ط´ط¯.", reply_markup=back_keyboard())
                return

            if action == "config":
                path = CLIENT_DIR / f"awg0-client-{name}.conf"
                if not path.exists():
                    await query.edit_message_text("ظپط§غŒظ„ ع©ط§ظ†ظپغŒع¯ ظ¾غŒط¯ط§ ظ†ط´ط¯.", reply_markup=back_keyboard())
                    return
                await context.bot.send_document(chat_id=query.message.chat_id, document=open(path, "rb"), filename=path.name)
                await query.edit_message_text("ظپط§غŒظ„ ع©ط§ظ†ظپغŒع¯ ط§ط±ط³ط§ظ„ ط´ط¯.", reply_markup=back_keyboard())
                return

            if action == "disable":
                disable_user(name)
                await query.edit_message_text(f"ع©ط§ط±ط¨ط± {name} ط؛غŒط±ظپط¹ط§ظ„ ط´ط¯.", reply_markup=back_keyboard())
                return

            if action == "enable":
                enable_user(name)
                await query.edit_message_text(f"ع©ط§ط±ط¨ط± {name} ظپط¹ط§ظ„ ط´ط¯.", reply_markup=back_keyboard())
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
                await query.edit_message_text(f"ع©ط§ط±ط¨ط± {name} ط­ط°ظپ ط´ط¯.", reply_markup=back_keyboard())
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
                    text=f"طھظ…ط¯غŒط¯ ع©ط§ط±ط¨ط±: {name}\n\nع†ظ†ط¯ ع¯غŒع¯ ط§ط¶ط§ظپظ‡ ط´ظˆط¯طں\n\nظ…ط«ط§ظ„:\n10",
                    reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None)
                )
                return

        except Exception as e:
            await query.edit_message_text(f"ط®ط·ط§:\n{e}", reply_markup=back_keyboard())
            return

    if data.startswith("manage:"):
        if query.from_user.id != OWNER_ID:
            await query.answer("Access denied.", show_alert=True)
            return

        action = data.split(":", 1)[1]

        if action == "list_admins":
            text = "ط§ط¯ظ…غŒظ†â€Œظ‡ط§غŒ ظپط¹ظ„غŒ:\n\n"
            for admin_id in sorted(ADMINS):
                if admin_id == OWNER_ID:
                    text += f"{admin_id}  ظ…ط§ظ„ع© ط§طµظ„غŒ\n"
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
                    "ط§ظپط²ظˆط¯ظ† ط§ط¯ظ…غŒظ† ط¬ط¯غŒط¯\n\n"
                    "ID ط¹ط¯ط¯غŒ ط§ط¯ظ…غŒظ† ط¬ط¯غŒط¯ ط±ط§ ط¨ظپط±ط³طھ.\n\n"
                    "ظ…ط«ط§ظ„:\n"
                    "123456789\n\n"
                    "ط¨ط±ط§غŒ ظ„ط؛ظˆ ط¨ظ†ظˆغŒط³: ظ„ط؛ظˆ"
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

            text = "ط­ط°ظپ ط§ط¯ظ…غŒظ†\n\nط§ط¯ظ…غŒظ†â€Œظ‡ط§غŒ ظ‚ط§ط¨ظ„ ط­ط°ظپ:\n\n"
            removable = [x for x in sorted(ADMINS) if x != OWNER_ID]
            if removable:
                text += "\n".join(str(x) for x in removable)
                text += "\n\nID ط¹ط¯ط¯غŒ ط§ط¯ظ…غŒظ†غŒ ع©ظ‡ ظ…غŒâ€Œط®ظˆط§ظ‡غŒ ط­ط°ظپ ط´ظˆط¯ ط±ط§ ط¨ظپط±ط³طھ."
            else:
                text += "ط§ط¯ظ…غŒظ† ظ‚ط§ط¨ظ„ ط­ط°ظپ ظˆط¬ظˆط¯ ظ†ط¯ط§ط±ط¯."

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text + "\n\nط¨ط±ط§غŒ ظ„ط؛ظˆ ط¨ظ†ظˆغŒط³: ظ„ط؛ظˆ",
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
                    "طھظ†ط¸غŒظ… ع©ط§ظ†ط§ظ„ ط¨ع©ط§ظ¾\n\n"
                    "ظ‚ط¨ظ„ ط§ط² ط§ط¯ط§ظ…ظ‡طŒ ط±ط¨ط§طھ ط¨ط§غŒط¯ ط¯ط§ط®ظ„ ع©ط§ظ†ط§ظ„ غŒط§ ع¯ط±ظˆظ‡ ط¨ع©ط§ظ¾ Admin ط¨ط§ط´ط¯.\n"
                    "ط¨ط±ط§غŒ ع©ط§ظ†ط§ظ„طŒ ط¯ط³طھط±ط³غŒ Post Messages ظ‡ظ… ظ„ط§ط²ظ… ط§ط³طھ.\n\n"
                    "ط­ط§ظ„ط§ ظ„غŒظ†ع© ع©ط§ظ†ط§ظ„/ع¯ط±ظˆظ‡ ط¨ع©ط§ظ¾ ط±ط§ ط¨ظپط±ط³طھ.\n\n"
                    "ظ…ط«ط§ظ„:\n"
                    "https://t.me/+xxxxxxxx\n\n"
                    "ط¨ط±ط§غŒ ظ„ط؛ظˆ ط¨ظ†ظˆغŒط³: ظ„ط؛ظˆ"
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
                    "طھظ†ط¸غŒظ… ط¯ط§ظ…ظ†ظ‡ ظˆ SSL\n\n"
                    "ظ‚ط¨ظ„ ط§ط² ط´ط±ظˆط¹طŒ ط¨ط§غŒط¯ ط¯ط± ظ¾ظ†ظ„ DNS ط¯ط§ظ…ظ†ظ‡ غŒع© A Record ط¨ط³ط§ط²غŒ.\n\n"
                    "ظ…ط«ط§ظ„:\n"
                    "panel.example.com  A  203.0.113.10\n\n"
                    "ط­ط§ظ„ط§ ط¯ط§ظ…ظ†ظ‡â€Œط§غŒ ع©ظ‡ ظ…غŒâ€Œط®ظˆط§ظ‡غŒ ط¨ط±ط§غŒ ظ¾ظ†ظ„ ط§ط³طھظپط§ط¯ظ‡ ط´ظˆط¯ ط±ط§ ط¨ظپط±ط³طھ.\n\n"
                    "ظ…ط«ط§ظ„:\n"
                    "panel.example.com\n\n"
                    "ط¨ط±ط§غŒ ظ„ط؛ظˆ ط¨ظ†ظˆغŒط³: ظ„ط؛ظˆ"
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
                    "طھظ†ط¸غŒظ… ط¯ط§ظ…ظ†ظ‡ ظˆ SSL\n\n"
                    "ظ‚ط¨ظ„ ط§ط² ط´ط±ظˆط¹طŒ ط¨ط§غŒط¯ ط¯ط± ظ¾ظ†ظ„ DNS ط¯ط§ظ…ظ†ظ‡ غŒع© A Record ط¨ط³ط§ط²غŒ.\n\n"
                    "ظ…ط«ط§ظ„:\n"
                    "panel.example.com  A  203.0.113.10\n\n"
                    "ط­ط§ظ„ط§ ط¯ط§ظ…ظ†ظ‡â€Œط§غŒ ع©ظ‡ ظ…غŒâ€Œط®ظˆط§ظ‡غŒ ط¨ط±ط§غŒ ظ¾ظ†ظ„ ط§ط³طھظپط§ط¯ظ‡ ط´ظˆط¯ ط±ط§ ط¨ظپط±ط³طھ.\n\n"
                    "ظ…ط«ط§ظ„:\n"
                    "panel.example.com\n\n"
                    "ط¨ط±ط§غŒ ظ„ط؛ظˆ ط¨ظ†ظˆغŒط³: ظ„ط؛ظˆ"
                ),
                reply_markup=main_keyboard(query.from_user.id),
            )
            return

        if action == "backup_now":
            set_busy("ع¯ط±ظپطھظ† ط¨ع©ط§ظ¾ ع©ط§ظ…ظ„", query.from_user.id)

            try:
                await query.message.delete()
            except Exception:
                pass

            LAST_INLINE.pop(query.from_user.id, None)

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    "âڈ³ ط¨ع©ط§ظ¾ ع©ط§ظ…ظ„ ط´ط±ظˆط¹ ط´ط¯.\n\n"
                    "ط§ط² ع©ط§ط±ط¨ط±ط§ظ†طŒ ط¯غŒطھط§ط¨غŒط³ ط¨ط§طھطŒ ع©ط§ظ†ظپغŒع¯â€Œظ‡ط§طŒ SSLطŒ nginx ظˆ firewall ط¨ع©ط§ظ¾ ع¯ط±ظپطھظ‡ ظ…غŒâ€Œط´ظˆط¯.\n"
                    "طھط§ ظ¾ط§غŒط§ظ† ط¹ظ…ظ„غŒط§طھطŒ ط¯ع©ظ…ظ‡â€Œظ‡ط§ ظ…ظˆظ‚طھط§ظ‹ ظ‚ظپظ„ ظ‡ط³طھظ†ط¯."
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
                        "âœ… ط¨ع©ط§ظ¾ ع©ط§ظ…ظ„ ط§ظ†ط¬ط§ظ… ط´ط¯.\n\n"
                        f"File: {Path(backup_path).name}\n"
                        f"Size: {size_mb:.2f} MB\n\n"
                        "ط§غŒظ† ظپط§غŒظ„ ط¨ط±ط§غŒ ط±غŒط³طھظˆط± ع©ط§ظ…ظ„ ع©ط§ط±ط¨ط±ط§ظ† ظˆ طھظ†ط¸غŒظ…ط§طھ ط§ط³طھظپط§ط¯ظ‡ ظ…غŒâ€Œط´ظˆط¯."
                    ),
                    reply_markup=main_keyboard(query.from_user.id),
                )
                return

            except Exception as e:
                clear_busy()
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"â‌Œ ط®ط·ط§ ط¯ط± ط¨ع©ط§ظ¾:\n\n{e}",
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
                    "طھظ†ط¸غŒظ… طھط¹ط¯ط§ط¯ ط¨ع©ط§ظ¾ ط®ظˆط¯ع©ط§ط±\n\n"
                    "ط±ظˆط²غŒ ع†ظ†ط¯ ط¨ط§ط± ط¨ع©ط§ظ¾ ع¯ط±ظپطھظ‡ ط´ظˆط¯طں\n\n"
                    "ظ…ط«ط§ظ„â€Œظ‡ط§:\n"
                    "1 = ط±ظˆط²غŒ غŒع© ط¨ط§ط±\n"
                    "2 = ظ‡ط± 12 ط³ط§ط¹طھ\n"
                    "4 = ظ‡ط± 6 ط³ط§ط¹طھ\n"
                    "6 = ظ‡ط± 4 ط³ط§ط¹طھ\n"
                    "12 = ظ‡ط± 2 ط³ط§ط¹طھ\n"
                    "24 = ظ‡ط± 1 ط³ط§ط¹طھ\n\n"
                    "ط¹ط¯ط¯ ظ…ط¬ط§ط²: 1 طھط§ 24\n"
                    "ط¨ط±ط§غŒ ظ„ط؛ظˆ ط¨ظ†ظˆغŒط³: ظ„ط؛ظˆ"
                ),
                reply_markup=main_keyboard(query.from_user.id),
            )
            return

        if action == "update_bot":
            try:
                installed = current_version()

                await query.edit_message_text(
                    "ًں”ژ ط¯ط± ط­ط§ظ„ ط¨ط±ط±ط³غŒ ظ†ط³ط®ظ‡ GitHub..."
                )

                available = await asyncio.to_thread(latest_version)

                if installed == available:
                    await query.edit_message_text(
                        "âœ… ط¢ط®ط±غŒظ† ظ†ط³ط®ظ‡ ط±ط§ ط¯ط§ط±غŒ.\n\n"
                        f"ظ†ط³ط®ظ‡ ظ†طµط¨â€Œط´ط¯ظ‡: {installed}",
                        reply_markup=owner_only_keyboard(),
                    )
                    return

                await query.edit_message_text(
                    "â¬†ï¸ڈ ظ†ط³ط®ظ‡ ط¬ط¯غŒط¯ ظ¾غŒط¯ط§ ط´ط¯.\n\n"
                    f"ظ†ط³ط®ظ‡ ظ†طµط¨â€Œط´ط¯ظ‡: {installed}\n"
                    f"ظ†ط³ط®ظ‡ ط¬ط¯غŒط¯: {available}\n\n"
                    "ط¯ط± ط­ط§ظ„ ط¨ط±ظˆط²ط±ط³ط§ظ†غŒ ع©ط§ظ…ظ„ ط§ط² GitHub ظ‡ط³طھظ…ط› ظ„ط·ظپط§ظ‹ طµط¨ط± ع©ظ†غŒط¯."
                )

                await asyncio.to_thread(
                    start_update_job,
                    query.message.chat_id,
                )
                return

            except Exception as e:
                await query.edit_message_text(
                    f"â‌Œ ط¨ط±ط±ط³غŒ غŒط§ ط´ط±ظˆط¹ ط¨ط±ظˆط²ط±ط³ط§ظ†غŒ ظ†ط§ظ…ظˆظپظ‚ ط¨ظˆط¯:\n\n{e}",
                    reply_markup=owner_only_keyboard(),
                )
                return

        if action == "show_settings":
            text = (
                "طھظ†ط¸غŒظ…ط§طھ ظپط¹ظ„غŒ:\n\n"
                f"VERSION: {current_version()}\n"
                f"OWNER_ID: {OWNER_ID}\n"
                f"ADMINS: {','.join(str(x) for x in sorted(ADMINS))}\n"
                f"BACKUP_CHAT_ID: {BACKUP_CHAT_ID or 'Not set'}\n"
                f"BACKUP_LINK: {BACKUP_LINK or 'Not set'}\n"
            )
            await query.edit_message_text(text, reply_markup=owner_only_keyboard())
            return

        if action == "back":
            await query.edit_message_text("ط¨ط®ط´ ظ…ط¯غŒط±غŒطھ ط§ط®طھطµط§طµغŒ ظ…ط§ظ„ع©:", reply_markup=owner_only_keyboard())
            return

    if data == "menu:status":
        try:
            text = pretty_server_status()
        except Exception as e:
            text = f"ط®ط·ط§:\n{e}"

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

    text = "ط´ظ†ط§ط³ظ‡â€Œظ‡ط§:\n\n"
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
        await update.message.reply_text("Name ظپظ‚ط· ط­ط±ظˆظپطŒ ط¹ط¯ط¯طŒ _ ظˆ - ط­ط¯ط§ع©ط«ط± غ±غµ ع©ط§ط±ط§ع©طھط±.")
        return

    gb = int(context.args[1])
    days = int(context.args[2])
    limit_bytes = gb * 1024 * 1024 * 1024
    expire_at = datetime.now(timezone.utc) + timedelta(days=days)

    con = db()
    if con.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
        con.close()
        await update.message.reply_text("ط§غŒظ† ظ†ط§ظ… ظ‚ط¨ظ„ط§ظ‹ ظˆط¬ظˆط¯ ط¯ط§ط±ط¯.")
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


    if is_busy():
        await update.message.reply_text(
            busy_text(),
            reply_markup=main_keyboard(uid)
        )
        return

    if text == "â‍• ط³ط§ط®طھ ع©ط§ط±ط¨ط±":
        await delete_last_inline(context, update.effective_chat.id, uid)
        PENDING_ADD[uid] = {"step": "name"}
        await update.message.reply_text(
            "ط§ط³ظ… ع©ط§ط±ط¨ط± ط¬ط¯غŒط¯ ط±ط§ ط¨ظپط±ط³طھ.\n\n"
            "ظ…ط«ط§ظ„:\n"
            "noora\n\n"
            "ظپظ‚ط· ط­ط±ظˆظپ ط§ظ†ع¯ظ„غŒط³غŒطŒ ط¹ط¯ط¯طŒ ط®ط· طھغŒط±ظ‡ ظˆ ط¢ظ†ط¯ط±ظ„ط§غŒظ† ظ…ط¬ط§ط² ط§ط³طھ.",
            reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None),
        )
        return

    if text == "ًں“‹ ظ„غŒط³طھ ع©ط§ط±ط¨ط±ط§ظ†":
        await delete_last_inline(context, update.effective_chat.id, uid)
        try:
            update_traffic_and_enforce()
            con = db()
            rows = con.execute("SELECT * FROM users ORDER BY name").fetchall()
            con.close()

            if not rows:
                out = "ع©ط§ط±ط¨ط±غŒ ظˆط¬ظˆط¯ ظ†ط¯ط§ط±ط¯."
            else:
                msg = []
                for r in rows:
                    used = r["total_rx"] + r["total_tx"]
                    limit = r["limit_bytes"]
                    status = "ظپط¹ط§ظ„ âœ…" if r["enabled"] else "ط؛غŒط±ظپط¹ط§ظ„ â›”"
                    msg.append(
                        f"{r['name']} | {status}\n"
                        f"ظ…طµط±ظپ: {human_bytes(used)} / {human_bytes(limit)}\n"
                        f"ط§ظ†ظ‚ط¶ط§: {r['expire_at'][:10]}\n"
                        f"IP: {r['ipv4']}"
                    )
                out = "\n\n".join(msg)
        except Exception as e:
            out = f"ط®ط·ط§:\n{e}"

        await update.message.reply_text(out[:3900], reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None))
        return

    if text == "ًں“± QR ع©ط§ط±ط¨ط±":
        await send_inline(
            update,
            context,
            "ط¨ط±ط§غŒ ط¯ط±غŒط§ظپطھ QRطŒ ع©ط§ط±ط¨ط± ط±ط§ ط§ظ†طھط®ط§ط¨ ع©ظ†:",
            reply_markup=users_action_keyboard("qr"),
        )
        return

    if text == "ًں“„ ظپط§غŒظ„ ع©ط§ظ†ظپغŒع¯":
        await send_inline(
            update,
            context,
            "ط¨ط±ط§غŒ ط¯ط±غŒط§ظپطھ ظپط§غŒظ„ ع©ط§ظ†ظپغŒع¯طŒ ع©ط§ط±ط¨ط± ط±ط§ ط§ظ†طھط®ط§ط¨ ع©ظ†:",
            reply_markup=users_action_keyboard("config"),
        )
        return

    if text == "â›” ط؛غŒط±ظپط¹ط§ظ„":
        await send_inline(
            update,
            context,
            "ط¨ط±ط§غŒ ط؛غŒط±ظپط¹ط§ظ„ ع©ط±ط¯ظ†طŒ ع©ط§ط±ط¨ط± ط±ط§ ط§ظ†طھط®ط§ط¨ ع©ظ†:",
            reply_markup=users_action_keyboard("disable"),
        )
        return

    if text == "âœ… ظپط¹ط§ظ„â€Œط³ط§ط²غŒ":
        await send_inline(
            update,
            context,
            "ط¨ط±ط§غŒ ظپط¹ط§ظ„â€Œط³ط§ط²غŒطŒ ع©ط§ط±ط¨ط± ط±ط§ ط§ظ†طھط®ط§ط¨ ع©ظ†:",
            reply_markup=users_action_keyboard("enable"),
        )
        return

    if text == "ًں—‘ ط­ط°ظپ ع©ط§ط±ط¨ط±":
        await send_inline(
            update,
            context,
            "ط¨ط±ط§غŒ ط­ط°ظپ ع©ط§ظ…ظ„طŒ ع©ط§ط±ط¨ط± ط±ط§ ط§ظ†طھط®ط§ط¨ ع©ظ†:",
            reply_markup=users_action_keyboard("delete"),
        )
        return

    if text == "â‍• طھظ…ط¯غŒط¯ ط­ط¬ظ…/ط±ظˆط²":
        await send_inline(
            update,
            context,
            "ط¨ط±ط§غŒ طھظ…ط¯غŒط¯طŒ ع©ط§ط±ط¨ط± ط±ط§ ط§ظ†طھط®ط§ط¨ ع©ظ†:",
            reply_markup=users_action_keyboard("extend"),
        )
        return

    if text == "ًں’¬ ع†طھ ط¨ط§ ط³ط§ط²ظ†ط¯ظ‡":
        await delete_last_inline(
            context,
            update.effective_chat.id,
            uid,
        )

        await update.message.reply_text(
            "ًں’¬ ط§ط±طھط¨ط§ط· ط¨ط§ ط³ط§ط²ظ†ط¯ظ‡ ط±ط¨ط§طھ\n\n"
            f"ط¢غŒط¯غŒ ط³ط§ط²ظ†ط¯ظ‡:\n@{CREATOR_USERNAME}\n\n"
            "ط¨ط±ط§غŒ ط´ط±ظˆط¹ ع¯ظپطھع¯ظˆ ط±ظˆغŒ ط¯ع©ظ…ظ‡ ط²غŒط± ط¨ط²ظ†.",
            reply_markup=creator_contact_keyboard(),
        )
        return

    if text == "âڑ™ï¸ڈ ظ…ط¯غŒط±غŒطھ":
        if not is_owner(update):
            return
        await send_inline(
            update,
            context,
            "ط¨ط®ط´ ظ…ط¯غŒط±غŒطھ ط§ط®طھطµط§طµغŒ ظ…ط§ظ„ع©:",
            reply_markup=owner_only_keyboard(),
        )
        return

    if text == "ًں“ٹ ظˆط¶ط¹غŒطھ ط³ط±ظˆط±":
        await delete_last_inline(context, update.effective_chat.id, uid)
        try:
            out = pretty_server_status()
        except Exception as e:
            out = f"ط®ط·ط§:\n{e}"
        await update.message.reply_text(
            out[:3900],
            reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None)
        )
        return

    if text == "ًں†” ط¯ط±غŒط§ظپطھ ID":
        await delete_last_inline(context, update.effective_chat.id, uid)

        chat = update.effective_chat
        user = update.effective_user

        out = "ط´ظ†ط§ط³ظ‡â€Œظ‡ط§:\n\n"
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
        raise RuntimeError("ط¹ط¯ط¯ ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ.")

    if times_per_day < 1 or times_per_day > 24:
        raise RuntimeError("ط¹ط¯ط¯ ط¨ط§غŒط¯ ط¨غŒظ† 1 طھط§ 24 ط¨ط§ط´ط¯.")

    interval_minutes = 1440 // times_per_day

    if 1440 % times_per_day != 0:
        raise RuntimeError(
            "ط¨ط±ط§غŒ طھظ‚ط³غŒظ… ط¯ظ‚غŒظ‚ ط±ظˆط²طŒ غŒع©غŒ ط§ط² ط§غŒظ† ط¹ط¯ط¯ظ‡ط§ ط±ط§ ط§ظ†طھط®ط§ط¨ ع©ظ†:\n"
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
            f"DNS ط¯ط§ظ…ظ†ظ‡ ظ‡ظ†ظˆط² ط±ظˆغŒ IP ط³ط±ظˆط± ظ†غŒط³طھ.\n\n"
            f"Domain: {domain}\n"
            f"Resolved IP: {resolved}\n"
            f"Server IP: {static_ip}\n\n"
            f"ط§ظˆظ„ A Record ط¯ط§ظ…ظ†ظ‡ ط±ط§ ط±ظˆغŒ {static_ip} ط¨ع¯ط°ط§ط±طŒ ط¨ط¹ط¯ ط¯ظˆط¨ط§ط±ظ‡ ط§ط¬ط±ط§ ع©ظ†."
        )

    if not resolved:
        raise RuntimeError(
            f"ط¯ط§ظ…ظ†ظ‡ ظ‡ظ†ظˆط² Resolve ظ†ظ…غŒâ€Œط´ظˆط¯.\n\n"
            f"غŒع© A Record ط¨ط³ط§ط²:\n"
            f"{domain}  A  {static_ip}\n\n"
            f"ط¨ط¹ط¯ ط§ط² ع†ظ†ط¯ ط¯ظ‚غŒظ‚ظ‡ ط¯ظˆط¨ط§ط±ظ‡ طھط³طھ ع©ظ†."
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

    if text in ["/cancel", "cancel", "ظ„ط؛ظˆ"]:
        PENDING_MANAGE.pop(uid, None)
        await update.message.reply_text(
            "ط¹ظ…ظ„غŒط§طھ ظ…ط¯غŒط±غŒطھغŒ ظ„ط؛ظˆ ط´ط¯.",
            reply_markup=main_keyboard(uid),
        )
        return True

    if step == "add_admin":
        try:
            new_admin = int(text)
        except Exception:
            await update.message.reply_text("ID ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ. ظپظ‚ط· ط¹ط¯ط¯ ط¨ظپط±ط³طھ.")
            return True

        if new_admin in ADMINS:
            PENDING_MANAGE.pop(uid, None)
            await update.message.reply_text(
                "ط§غŒظ† ID ط§ط² ظ‚ط¨ظ„ ط§ط¯ظ…غŒظ† ط§ط³طھ.",
                reply_markup=main_keyboard(uid),
            )
            return True

        ADMINS.add(new_admin)
        save_admins()
        PENDING_MANAGE.pop(uid, None)

        await update.message.reply_text(
            f"ط§ط¯ظ…غŒظ† ط§ط¶ط§ظپظ‡ ط´ط¯:\n{new_admin}",
            reply_markup=main_keyboard(uid),
        )
        return True

    if step == "remove_admin":
        try:
            remove_id = int(text)
        except Exception:
            await update.message.reply_text("ID ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ. ظپظ‚ط· ط¹ط¯ط¯ ط¨ظپط±ط³طھ.")
            return True

        if remove_id == OWNER_ID:
            PENDING_MANAGE.pop(uid, None)
            await update.message.reply_text(
                "ظ…ط§ظ„ع© ط§طµظ„غŒ ظ‚ط§ط¨ظ„ ط­ط°ظپ ظ†غŒط³طھ.",
                reply_markup=main_keyboard(uid),
            )
            return True

        if remove_id not in ADMINS:
            PENDING_MANAGE.pop(uid, None)
            await update.message.reply_text(
                "ط§غŒظ† ID ط¯ط§ط®ظ„ ظ„غŒط³طھ ط§ط¯ظ…غŒظ†â€Œظ‡ط§ ظ†غŒط³طھ.",
                reply_markup=main_keyboard(uid),
            )
            return True

        ADMINS.discard(remove_id)
        save_admins()
        PENDING_MANAGE.pop(uid, None)

        await update.message.reply_text(
            f"ط§ط¯ظ…غŒظ† ط­ط°ظپ ط´ط¯:\n{remove_id}",
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
                "ظ„غŒظ†ع© ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ.\n\n"
                "ظ„غŒظ†ع© ط¨ط§غŒط¯ ط¨ط§ t.me غŒط§ https://t.me ط´ط±ظˆط¹ ط´ظˆط¯.\n\n"
                "ظ…ط«ط§ظ„:\n"
                "https://t.me/+xxxxxxxx"
            )
            return True

        state["backup_link"] = text
        state["step"] = "backup_id"

        await update.message.reply_text(
            "ظ„غŒظ†ع© ط°ط®غŒط±ظ‡ ط´ط¯.\n\n"
            "ط­ط§ظ„ط§ ID ط¹ط¯ط¯غŒ ع©ط§ظ†ط§ظ„/ع¯ط±ظˆظ‡ ط¨ع©ط§ظ¾ ط±ط§ ط¨ظپط±ط³طھ.\n\n"
            "ظ†ع©طھظ‡ ظ…ظ‡ظ…:\n"
            "ط±ط¨ط§طھ ط¨ط§غŒط¯ ط¯ط§ط®ظ„ ظ‡ظ…ط§ظ† ع©ط§ظ†ط§ظ„/ع¯ط±ظˆظ‡ Admin ط¨ط§ط´ط¯.\n\n"
            "ظ…ط«ط§ظ„:\n"
            "-1001234567890"
        )
        return True

    if step == "backup_id":
        try:
            backup_id = int(text)
        except Exception:
            await update.message.reply_text(
                "ID ع©ط§ظ†ط§ظ„/ع¯ط±ظˆظ‡ ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ. ط¨ط§غŒط¯ ط¹ط¯ط¯ ط¨ط§ط´ط¯.\n\n"
                "ظ…ط«ط§ظ„:\n"
                "-1001234567890"
            )
            return True

        BACKUP_LINK = state.get("backup_link", "")
        BACKUP_CHAT_ID = str(backup_id)

        write_env_value("BACKUP_LINK", BACKUP_LINK)
        write_env_value("BACKUP_CHAT_ID", BACKUP_CHAT_ID)

        PENDING_MANAGE.pop(uid, None)

        await update.message.reply_text(
            "ع©ط§ظ†ط§ظ„ ط¨ع©ط§ظ¾ طھظ†ط¸غŒظ… ط´ط¯.\n\n"
            f"Link: {BACKUP_LINK}\n"
            f"Chat ID: {BACKUP_CHAT_ID}\n\n"
            "ط§ط² ط§غŒظ† ط¨ظ‡ ط¨ط¹ط¯ ع©ط§ظ†ظپغŒع¯ ع©ط§ط±ط¨ط±ط§ظ† ط¬ط¯غŒط¯ ط¨ط±ط§غŒ ع©ط§ظ†ط§ظ„ ط¨ع©ط§ظ¾ ظ‡ظ… ط§ط±ط³ط§ظ„ ظ…غŒâ€Œط´ظˆط¯.",
            reply_markup=main_keyboard(uid),
        )
        return True

    if step == "domain_name":
        domain = text.lower().strip()

        if not is_valid_domain(domain):
            await update.message.reply_text(
                "ط¯ط§ظ…ظ†ظ‡ ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ.\n\n"
                "ظ…ط«ط§ظ„ ط¯ط±ط³طھ:\n"
                "panel.example.com"
            )
            return True

        state["domain"] = domain
        state["step"] = "domain_ip"

        await update.message.reply_text(
            f"ط¯ط§ظ…ظ†ظ‡ ط«ط¨طھ ط´ط¯:\n{domain}\n\n"
            "ط­ط§ظ„ط§ IP ط§ط³طھط§طھغŒع© ط³ط±ظˆط± ط±ط§ ط¨ظپط±ط³طھ.\n\n"
            "ط¨ط±ط§غŒ ط§غŒظ† ط³ط±ظˆط± ظ…ط¹ظ…ظˆظ„ط§ظ‹ ط§غŒظ† ط§ط³طھ:\n"
            "203.0.113.10"
        )
        return True

    if step == "domain_ip":
        static_ip = text.strip()

        if not is_valid_ipv4(static_ip):
            await update.message.reply_text(
                "IP ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ.\n\n"
                "ظ…ط«ط§ظ„:\n"
                "203.0.113.10"
            )
            return True

        domain = state["domain"]
        resolved = resolve_domain_ipv4(domain)

        if not resolved:
            await update.message.reply_text(
                "ط¯ط§ظ…ظ†ظ‡ ظ‡ظ†ظˆط² Resolve ظ†ظ…غŒâ€Œط´ظˆط¯.\n\n"
                f"ط¯ط± DNS ط¯ط§ظ…ظ†ظ‡ ط§غŒظ† ط±ع©ظˆط±ط¯ ط±ط§ ط¨ط³ط§ط²:\n"
                f"{domain}  A  {static_ip}\n\n"
                "ط¨ط¹ط¯ ط§ط² ع†ظ†ط¯ ط¯ظ‚غŒظ‚ظ‡ ط¯ظˆط¨ط§ط±ظ‡ ظ‡ظ…غŒظ† IP ط±ط§ ط¨ظپط±ط³طھ."
            )
            return True

        if resolved != static_ip:
            await update.message.reply_text(
                "DNS ط¯ط§ظ…ظ†ظ‡ ظ‡ظ†ظˆط² ط±ظˆغŒ IP ط¯ط±ط³طھ ظ†غŒط³طھ.\n\n"
                f"ط¯ط§ظ…ظ†ظ‡: {domain}\n"
                f"IP ظپط¹ظ„غŒ ط¯ط§ظ…ظ†ظ‡: {resolved}\n"
                f"IP ط³ط±ظˆط±: {static_ip}\n\n"
                f"A Record ط¯ط§ظ…ظ†ظ‡ ط±ط§ ط±ظˆغŒ {static_ip} طھظ†ط¸غŒظ… ع©ظ†طŒ ط¨ط¹ط¯ ط¯ظˆط¨ط§ط±ظ‡ IP ط±ط§ ط¨ظپط±ط³طھ."
            )
            return True

        state["static_ip"] = static_ip
        state["step"] = "domain_email"

        await update.message.reply_text(
            "DNS ط¯ط±ط³طھ ط§ط³طھ.\n\n"
            "ط­ط§ظ„ط§ ط§غŒظ…غŒظ„ ط¨ط±ط§غŒ طµط¯ظˆط± SSL ط±ط§ ط¨ظپط±ط³طھ.\n\n"
            "ظ…ط«ط§ظ„:\n"
            "admin@example.com"
        )
        return True

    if step == "domain_email":
        email = text.strip()

        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            await update.message.reply_text(
                "ط§غŒظ…غŒظ„ ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ.\n\n"
                "ظ…ط«ط§ظ„:\n"
                "admin@example.com"
            )
            return True

        domain = state["domain"]
        static_ip = state["static_ip"]

        set_busy("طھظ†ط¸غŒظ… ط¯ط§ظ…ظ†ظ‡ ظˆ SSL", uid)

        await update.message.reply_text(
            "âڈ³ ط¹ظ…ظ„غŒط§طھ ط´ط±ظˆط¹ ط´ط¯.\n\n"
            "ط¯ط± ط­ط§ظ„ ظ†طµط¨ ظˆ طھظ†ط¸غŒظ… ط¯ط§ظ…ظ†ظ‡ ظˆ SSL...\n\n"
            f"Domain: {domain}\n"
            f"IP: {static_ip}\n"
            f"Email: {email}\n\n"
            "طھط§ ظ¾ط§غŒط§ظ† ط¹ظ…ظ„غŒط§طھطŒ ط¯ع©ظ…ظ‡â€Œظ‡ط§غŒ ط¨ط§طھ ظ…ظˆظ‚طھط§ظ‹ ظ‚ظپظ„ ظ‡ط³طھظ†ط¯.\n"
            "ظ…ظ…ع©ظ† ط§ط³طھ غ± طھط§ غ³ ط¯ظ‚غŒظ‚ظ‡ ط·ظˆظ„ ط¨ع©ط´ط¯."
        )

        try:
            url = await asyncio.to_thread(setup_domain_ssl, domain, static_ip, email)
            checks = await asyncio.to_thread(test_https_domain, domain)

            write_env_value("PANEL_DOMAIN", domain)
            write_env_value("PANEL_URL", url)

            PENDING_MANAGE.pop(uid, None)
            clear_busy()

            await update.message.reply_text(
                "âœ… ط¯ط§ظ…ظ†ظ‡ ظˆ SSL ط¨ط§ ظ…ظˆظپظ‚غŒطھ طھظ†ط¸غŒظ… ط´ط¯.\n\n"
                f"ط¢ط¯ط±ط³ ظ¾ظ†ظ„:\n{url}\n\n"
                "ظ†طھغŒط¬ظ‡ طھط³طھâ€Œظ‡ط§:\n"
                f"{checks}",
                reply_markup=main_keyboard(uid),
            )
            return True

        except Exception as e:
            checks = ""
            try:
                checks = await asyncio.to_thread(test_https_domain, domain)
            except Exception as ee:
                checks = f"طھط³طھ ظ†ظ‡ط§غŒغŒ ظ‡ظ… ط®ط·ط§ ط¯ط§ط¯:\n{ee}"

            PENDING_MANAGE.pop(uid, None)
            clear_busy()

            await update.message.reply_text(
                "â‌Œ ط®ط·ط§ ط¯ط± طھظ†ط¸غŒظ… ط¯ط§ظ…ظ†ظ‡/SSL\n\n"
                f"ط®ط·ط§:\n{e}\n\n"
                "ظˆط¶ط¹غŒطھ طھط³طھâ€Œظ‡ط§:\n"
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
                "ط¹ط¯ط¯ ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ.\n\n"
                "ظ…ط«ط§ظ„:\n"
                "4"
            )
            return True

        await update.message.reply_text(
            "ط¯ط± ط­ط§ظ„ طھظ†ط¸غŒظ… ط¨ع©ط§ظ¾ ط®ظˆط¯ع©ط§ط±...\n\n"
            f"طھط¹ط¯ط§ط¯ ط¨ع©ط§ظ¾ ط¯ط± ط±ظˆط²: {count}"
        )

        try:
            times, status = configure_backup_frequency(count)
            PENDING_MANAGE.pop(uid, None)

            await update.message.reply_text(
                "âœ… ط¨ع©ط§ظ¾ ط®ظˆط¯ع©ط§ط± طھظ†ط¸غŒظ… ط´ط¯.\n\n"
                f"طھط¹ط¯ط§ط¯ ط¨ع©ط§ظ¾ ط¯ط± ط±ظˆط²: {count}\n"
                f"ط³ط§ط¹طھâ€Œظ‡ط§غŒ ط§ط¬ط±ط§:\n{', '.join(times)}\n\n"
                f"{status[:1500]}",
                reply_markup=main_keyboard(uid),
            )
            return True

        except Exception as e:
            PENDING_MANAGE.pop(uid, None)
            await update.message.reply_text(
                f"â‌Œ ط®ط·ط§ ط¯ط± طھظ†ط¸غŒظ… ط¨ع©ط§ظ¾ ط®ظˆط¯ع©ط§ط±:\n\n{e}",
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

    if text in ["/cancel", "cancel", "ظ„ط؛ظˆ"]:
        PENDING_EXTEND.pop(uid, None)
        await update.message.reply_text("ط¹ظ…ظ„غŒط§طھ طھظ…ط¯غŒط¯ ظ„ط؛ظˆ ط´ط¯.", reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None))
        return True

    name = state["name"]

    if state["step"] == "gb":
        try:
            gb = int(text)
            if gb < 0 or gb > 10000:
                raise ValueError()
        except Exception:
            await update.message.reply_text("ط­ط¬ظ… ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ. ظپظ‚ط· ط¹ط¯ط¯ ط¨ظپط±ط³طھ. ظ…ط«ط§ظ„: 10")
            return True

        state["gb"] = gb
        state["step"] = "days"
        await update.message.reply_text(
            f"ط­ط¬ظ… ط§ط¶ط§ظپظ‡: {gb}GB\n\n"
            "ع†ظ†ط¯ ط±ظˆط² ط§ط¶ط§ظپظ‡ ط´ظˆط¯طں\n\n"
            "ظ…ط«ط§ظ„:\n"
            "15"
        )
        return True

    if state["step"] == "days":
        try:
            days = int(text)
            if days < 0 or days > 3650:
                raise ValueError()
        except Exception:
            await update.message.reply_text("طھط¹ط¯ط§ط¯ ط±ظˆط² ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ. ظپظ‚ط· ط¹ط¯ط¯ ط¨ظپط±ط³طھ. ظ…ط«ط§ظ„: 15")
            return True

        gb = state["gb"]

        con = db()
        row = con.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()

        if not row:
            con.close()
            PENDING_EXTEND.pop(uid, None)
            await update.message.reply_text("ع©ط§ط±ط¨ط± ظ¾غŒط¯ط§ ظ†ط´ط¯.", reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None))
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
            f"طھظ…ط¯غŒط¯ ط§ظ†ط¬ط§ظ… ط´ط¯.\n\n"
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
        "ًں“² ظ„غŒظ†ع© ظ†طµط¨ ط¨ط±ظ†ط§ظ…ظ‡ AmneziaWG\n\n"
        "ط¨ط±ط§غŒ ط§طھطµط§ظ„طŒ ط§ظˆظ„ ط¨ط±ظ†ط§ظ…ظ‡ ط±ط§ ظ†طµط¨ ع©ظ†طŒ ط¨ط¹ط¯ ظپط§غŒظ„ ع©ط§ظ†ظپغŒع¯ غŒط§ QR ط±ط§ ط¯ط§ط®ظ„ ط¨ط±ظ†ط§ظ…ظ‡ Import ع©ظ†.\n\n"
        "ًںچڈ iPhone / iPad:\n"
        "https://apps.apple.com/us/app/amneziawg/id6478942365\n\n"
        "ًں¤– Android - Google Play:\n"
        "https://play.google.com/store/apps/details?id=org.amnezia.vpn\n\n"
        "ًں¤– Android - APK / ظ‡ظ…ظ‡ ط¯ط§ظ†ظ„ظˆط¯ظ‡ط§:\n"
        "https://amnezia.org/downloads\n\n"
        "ًںھں Windows:\n"
        "https://amnezia.org/downloads\n\n"
        "ط±ط§ظ‡ظ†ظ…ط§غŒ ط³ط±غŒط¹:\n"
        "1. ط¨ط±ظ†ط§ظ…ظ‡ ط±ط§ ظ†طµط¨ ع©ظ†.\n"
        "2. ع¯ط²غŒظ†ظ‡ Import / Add tunnel ط±ط§ ط¨ط²ظ†.\n"
        "3. QR ط±ط§ ط§ط³ع©ظ† ع©ظ† غŒط§ ظپط§غŒظ„ conf ط±ط§ ظˆط§ط±ط¯ ع©ظ†.\n"
        "4. ط§طھطµط§ظ„ ط±ط§ ط±ظˆط´ظ† ع©ظ†."
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

    if text in ["/cancel", "cancel", "ظ„ط؛ظˆ"]:
        PENDING_ADD.pop(uid, None)
        await update.message.reply_text("ط¹ظ…ظ„غŒط§طھ ط³ط§ط®طھ ع©ط§ط±ط¨ط± ظ„ط؛ظˆ ط´ط¯.", reply_markup=main_keyboard(update.effective_user.id if update.effective_user else None))
        return

    if state["step"] == "name":
        name = text

        if not re.match(r"^[A-Za-z0-9_-]{1,15}$", name):
            await update.message.reply_text(
                "ط§ط³ظ… ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ. ظپظ‚ط· ط­ط±ظˆظپ ط§ظ†ع¯ظ„غŒط³غŒطŒ ط¹ط¯ط¯طŒ _ ظˆ - ط­ط¯ط§ع©ط«ط± غ±غµ ع©ط§ط±ط§ع©طھط±.\n\n"
                "ط¯ظˆط¨ط§ط±ظ‡ ط§ط³ظ… ط±ط§ ط¨ظپط±ط³طھ:"
            )
            return

        con = db()
        exists = con.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone()
        con.close()

        if exists:
            await update.message.reply_text("ط§غŒظ† ط§ط³ظ… ظ‚ط¨ظ„ط§ظ‹ ظˆط¬ظˆط¯ ط¯ط§ط±ط¯. غŒع© ط§ط³ظ… ط¯غŒع¯ط± ط¨ظپط±ط³طھ:")
            return

        state["name"] = name
        state["step"] = "gb"
        await update.message.reply_text(
            f"ط§ط³ظ… ع©ط§ط±ط¨ط±: {name}\n\n"
            "ط­ط¬ظ… ع†ظ†ط¯ ع¯غŒع¯ ط¨ط§ط´ط¯طں\n\n"
            "ظ…ط«ط§ظ„:\n"
            "20"
        )
        return

    if state["step"] == "gb":
        try:
            gb = int(text)
            if gb <= 0 or gb > 10000:
                raise ValueError()
        except Exception:
            await update.message.reply_text("ط­ط¬ظ… ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ. ظپظ‚ط· ط¹ط¯ط¯ ط¨ظپط±ط³طھ. ظ…ط«ط§ظ„: 20")
            return

        state["gb"] = gb
        state["step"] = "days"
        await update.message.reply_text(
            f"ط­ط¬ظ…: {gb}GB\n\n"
            "ط§ط¹طھط¨ط§ط± ع†ظ†ط¯ ط±ظˆط² ط¨ط§ط´ط¯طں\n\n"
            "ظ…ط«ط§ظ„:\n"
            "30"
        )
        return

    if state["step"] == "days":
        try:
            days = int(text)
            if days <= 0 or days > 3650:
                raise ValueError()
        except Exception:
            await update.message.reply_text("طھط¹ط¯ط§ط¯ ط±ظˆط² ظ†ط§ظ…ط¹طھط¨ط± ط§ط³طھ. ظپظ‚ط· ط¹ط¯ط¯ ط¨ظپط±ط³طھ. ظ…ط«ط§ظ„: 30")
            return

        name = state["name"]
        gb = state["gb"]
        limit_bytes = gb * 1024 * 1024 * 1024
        expire_at = datetime.now(timezone.utc) + timedelta(days=days)

        await update.message.reply_text(
            f"ط¯ط± ط­ط§ظ„ ط³ط§ط®طھ ع©ط§ط±ط¨ط±...\n\n"
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
                f"ع©ط§ط±ط¨ط± ط³ط§ط®طھظ‡ ط´ط¯.\n\n"
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
                f"ط®ط·ط§ ط¯ط± ط³ط§ط®طھ ع©ط§ط±ط¨ط±:\n{e}",
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
        return "ظ†ط¯ط§ط±ط¯"

    if ts <= 0:
        return "ظ†ط¯ط§ط±ط¯"

    now = int(datetime.now(timezone.utc).timestamp())
    diff = max(now - ts, 0)

    if diff < 60:
        return "ظ‡ظ…غŒظ† ط§ظ„ط§ظ†"

    minutes = diff // 60
    if minutes < 60:
        return f"{minutes} ط¯ظ‚غŒظ‚ظ‡ ظ¾غŒط´"

    hours = minutes // 60
    if hours < 24:
        return f"{hours} ط³ط§ط¹طھ ظ¾غŒط´"

    days = hours // 24
    return f"{days} ط±ظˆط² ظ¾غŒط´"


def status_from_handshake(ts):
    try:
        ts = int(ts)
    except Exception:
        return "ًں”´ ط¨ط¯ظˆظ† ط§طھطµط§ظ„"

    if ts <= 0:
        return "ًں”´ ظ‡ظ†ظˆط² ظˆطµظ„ ظ†ط´ط¯ظ‡"

    now = int(datetime.now(timezone.utc).timestamp())
    diff = max(now - ts, 0)

    if diff <= 180:
        return "ًںں¢ ط¢ظ†ظ„ط§غŒظ†"

    if diff <= 3600:
        return "ًںں، ط§ط®غŒط±ط§ظ‹ ظˆطµظ„ ط¨ظˆط¯ظ‡"

    return "âڑ« ط¢ظپظ„ط§غŒظ†"


def clean_ip(allowed_ips):
    if not allowed_ips:
        return "-"
    first = allowed_ips.split(",")[0].strip()
    return first.replace("/32", "")


def pretty_server_status():
    rows = peer_db_map()

    dump = awg("show", AWG_IFACE, "dump").splitlines()
    if not dump:
        return "ط§ط·ظ„ط§ط¹ط§طھغŒ ط§ط² ط³ط±ظˆط± ط¯ط±غŒط§ظپطھ ظ†ط´ط¯."

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

        if status.startswith("ًںں¢"):
            online_count += 1
        elif status.startswith("ًںں،"):
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

    text = "ًں“ٹ ظˆط¶ط¹غŒطھ ط³ط±ظˆط± AmneziaWG\n\n"
    text += f"ًں§© Interface: {AWG_IFACE}\n"
    text += f"ًں”Œ Port: {port}\n"
    if server_key:
        text += f"ًں”‘ Server Key: {server_key[:12]}...\n"

    text += "\n"
    text += f"ًں‘¥ طھط¹ط¯ط§ط¯ ع©ط§ط±ط¨ط±ط§ظ†: {len(peers)}\n"
    text += f"ًںں¢ ط¢ظ†ظ„ط§غŒظ†: {online_count}\n"
    text += f"ًںں، ط§ط®غŒط±ط§ظ‹ ظˆطµظ„ ط¨ظˆط¯ظ‡: {recent_count}\n"
    text += f"ًں”´ ط¢ظپظ„ط§غŒظ†/ط¨ط¯ظˆظ† ط§طھطµط§ظ„: {offline_count}\n"
    text += f"ًں“¦ ظ…طµط±ظپ ع©ظ„: {human_bytes(total_used)}\n"

    if not peers:
        text += "\nع©ط§ط±ط¨ط±غŒ ظˆط¬ظˆط¯ ظ†ط¯ط§ط±ط¯."
        return text

    text += "\nâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پ\n"

    for peer in peers:
        text += f"\nًں‘¤ {peer['name']}\n"
        text += f"ظˆط¶ط¹غŒطھ: {peer['status']}\n"
        text += f"ط¢ط®ط±غŒظ† ط§طھطµط§ظ„: {peer['last_seen']}\n"
        text += f"ظ…طµط±ظپ ع©ظ„: {human_bytes(peer['used'])}\n"
        text += f"ط¯ط±غŒط§ظپطھ/ط§ط±ط³ط§ظ„: {human_bytes(peer['rx'])} / {human_bytes(peer['tx'])}\n"

        if peer["limit"]:
            text += f"ط­ط¬ظ… ظ…ط¬ط§ط²: {human_bytes(peer['limit'])}\n"
            text += f"ط¨ط§ظ‚غŒâ€Œظ…ط§ظ†ط¯ظ‡: {human_bytes(peer['remaining'])}\n"

        text += f"ط§ظ†ظ‚ط¶ط§: {peer['expire']}\n"
        text += f"IP ط¯ط§ط®ظ„غŒ: {peer['ip']}\n"

        if peer["endpoint"] != "-":
            text += f"Endpoint: {peer['endpoint']}\n"

        text += "â”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پâ”پ\n"

    return text[:3900]




async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update):
        return
    try:
        out = pretty_server_status()
    except Exception as e:
        out = f"ط®ط·ط§:\n{e}"
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

