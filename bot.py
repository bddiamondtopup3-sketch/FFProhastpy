import os
import sys
import time
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
import telebot
from telebot import types

# ================= CONFIG =================
BOT_TOKEN = "9272130898:AAHBE2AHIpv-RrSmXPxMDNlRs6FlPAEYWHM"
OWNER_ID = 6448634538  # Main owner numeric ID
UPDATES_CHANNEL_URL = "https://t.me/+kqZUPQwWUiJlODI1"
BOTS_DIR = os.path.abspath("hosted_bots")
BACKUP_DIR = os.path.abspath("backup_bots")
DB_PATH = os.path.abspath("hosting_panel.db")
# ==========================================

os.makedirs(BOTS_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
bot = telebot.TeleBot(BOT_TOKEN)

running_procs = {}
user_states = {}


def get_system_stats():
    load_1m = 0.0
    try:
        load_1m = round(os.getloadavg()[0], 2)
    except Exception:
        load_1m = 0.0

    ram_pct = 0.0
    try:
        if os.path.exists("/proc/meminfo"):
            mem = {}
            with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        k = parts[0].strip()
                        v = parts[1].strip().split()[0]
                        if v.isdigit():
                            mem[k] = int(v)
            total = mem.get("MemTotal", 0)
            avail = mem.get("MemAvailable", mem.get("MemFree", 0))
            if total > 0:
                ram_pct = round(((total - avail) / total) * 100, 1)
    except Exception:
        ram_pct = 0.0

    return load_1m, ram_pct


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT,
                last_active TEXT,
                is_banned INTEGER DEFAULT 0,
                api_key TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS user_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                filename TEXT,
                uploaded_at TEXT,
                UNIQUE(user_id, filename)
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                max_files INTEGER,
                days INTEGER,
                price INTEGER
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                plan_name TEXT,
                max_files INTEGER,
                expires_at TEXT
            );

            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                method TEXT,
                plan_name TEXT,
                amount INTEGER,
                sender_info TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                message TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER,
                action TEXT,
                created_at TEXT
            );
            """
        )
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "last_active" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_active TEXT")
        if "is_banned" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")

        conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (OWNER_ID,))
        defaults = {
            "bot_locked": "0",
            "bkash": "Not Set",
            "nagad": "Not Set",
            "rocket": "Not Set",
            "upay": "Not Set",
            "updates_channel": UPDATES_CHANNEL_URL,
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

        existing_plans = conn.execute("SELECT COUNT(*) AS c FROM plans").fetchone()["c"]
        if existing_plans == 0:
            conn.executemany(
                "INSERT INTO plans (name, max_files, days, price) VALUES (?, ?, ?, ?)",
                [
                    ("Basic 5 Bots", 5, 30, 150),
                    ("Pro 10 Bots", 10, 30, 280),
                ],
            )


def log_activity(actor_id: int, action: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO activity_logs (actor_id, action, created_at) VALUES (?, ?, ?)",
            (actor_id, action, now),
        )


def register_user(msg):
    u = msg.from_user
    if not u:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, joined_at, last_active, is_banned)
            VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_active=excluded.last_active
            """,
            (u.id, u.username or "", u.first_name or "", now, now),
        )


def record_user_file(user_id: int, filename: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO user_files (user_id, filename, uploaded_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, filename) DO UPDATE SET
                uploaded_at=excluded.uploaded_at
            """,
            (user_id, filename, now),
        )


def remove_user_file_record(filename: str):
    with get_db() as conn:
        conn.execute("DELETE FROM user_files WHERE filename = ?", (filename,))


def get_user_uploaded_files(user_id: int):
    existing = set(list_bot_files())
    with get_db() as conn:
        rows = conn.execute(
            "SELECT filename, uploaded_at FROM user_files WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    res = []
    for r in rows:
        if r["filename"] in existing:
            res.append(dict(r))
    if not res and user_id == OWNER_ID:
        for fn in list_bot_files():
            res.append({"filename": fn, "uploaded_at": ""})
    return res


def is_user_banned(user_id: int) -> bool:
    if is_admin(user_id):
        return False
    with get_db() as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row and row["is_banned"] == 1)


def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    with get_db() as conn:
        row = conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None


def get_admin_ids():
    ids = {OWNER_ID}
    with get_db() as conn:
        rows = conn.execute("SELECT user_id FROM admins").fetchall()
    for r in rows:
        ids.add(r["user_id"])
    return list(ids)


def get_setting(key: str) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row and row["value"] is not None:
            return row["value"]
    return UPDATES_CHANNEL_URL if key == "updates_channel" else ""


def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_updates_channel_url() -> str:
    val = (get_setting("updates_channel") or "").strip()
    return val if val else UPDATES_CHANNEL_URL


def get_user_sub(user_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    try:
        exp = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
        if exp <= datetime.now():
            return None
    except Exception:
        return None
    return dict(row)


def main_keyboard(user_id: int = None) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(types.KeyboardButton("Updates Channel"))
    kb.add(types.KeyboardButton("Deploy Bot"), types.KeyboardButton("Bot Files"))
    if user_id is not None and is_admin(user_id):
        kb.add(types.KeyboardButton("View Plans"), types.KeyboardButton("Admin Panel"))
        kb.add(types.KeyboardButton("Speed & Ping"), types.KeyboardButton("Bot Stats"))
        kb.add(types.KeyboardButton("Terminal"))
    else:
        kb.add(types.KeyboardButton("View Plans"), types.KeyboardButton("Bot Stats"))
        kb.add(types.KeyboardButton("Speed & Ping"))
    return kb


def admin_panel_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("+ Add Plan", callback_data="adm:add_plan"),
        types.InlineKeyboardButton("Manage Plans", callback_data="adm:manage_plans"),
    )
    kb.add(
        types.InlineKeyboardButton("Add Subscription", callback_data="adm:add_sub"),
        types.InlineKeyboardButton("Remove Sub", callback_data="adm:rem_sub"),
    )
    kb.add(
        types.InlineKeyboardButton("Payment Settings", callback_data="adm:pay_set"),
        types.InlineKeyboardButton("Payment Requests", callback_data="adm:pay_req"),
    )
    kb.add(
        types.InlineKeyboardButton("Set Updates Channel", callback_data="adm:set_channel"),
        types.InlineKeyboardButton("Add Admin", callback_data="adm:add_admin"),
    )
    kb.add(
        types.InlineKeyboardButton("Remove Admin", callback_data="adm:rem_admin"),
        types.InlineKeyboardButton("Broadcast", callback_data="adm:broadcast"),
    )
    kb.add(
        types.InlineKeyboardButton("Lock/Unlock", callback_data="adm:lock_toggle"),
        types.InlineKeyboardButton("Run All Scripts", callback_data="adm:run_all"),
    )
    kb.add(
        types.InlineKeyboardButton("Bot Stats", callback_data="adm:bot_stats"),
        types.InlineKeyboardButton("User Management", callback_data="adm:users"),
    )
    kb.add(
        types.InlineKeyboardButton("API Management", callback_data="adm:api"),
        types.InlineKeyboardButton("Support Inbox", callback_data="adm:support"),
    )
    kb.add(
        types.InlineKeyboardButton("Server Monitor", callback_data="adm:server"),
        types.InlineKeyboardButton("Activity Logs", callback_data="adm:logs"),
    )
    return kb


def list_bot_files():
    return [
        f
        for f in sorted(os.listdir(BOTS_DIR))
        if os.path.isfile(os.path.join(BOTS_DIR, f)) and f.endswith(".py")
    ]


def list_backup_files():
    return [
        f
        for f in sorted(os.listdir(BACKUP_DIR))
        if os.path.isfile(os.path.join(BACKUP_DIR, f)) and f.endswith(".py")
    ]


def backup_bot_file(filename: str) -> str:
    src = os.path.join(BOTS_DIR, filename)
    if not os.path.exists(src):
        return ""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(filename)
    bname = f"{base}_{ts}{ext}"
    dst = os.path.join(BACKUP_DIR, bname)
    shutil.copy2(src, dst)
    return bname


def check_syntax_error(filepath: str):
    try:
        res = subprocess.run(
            [sys.executable, "-m", "py_compile", filepath],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode != 0:
            err = (res.stderr or res.stdout or "Syntax error").strip()
            return False, err
        return True, ""
    except Exception as e:
        return False, str(e)


def start_bot_file(filename: str):
    if filename in running_procs and running_procs[filename].poll() is None:
        return True, f"Already running (PID {running_procs[filename].pid}).", running_procs[filename].pid
    filepath = os.path.join(BOTS_DIR, filename)
    if not os.path.exists(filepath):
        return False, "File not found.", None

    ok_syn, syn_err = check_syntax_error(filepath)
    if not ok_syn:
        return False, f"Syntax Error:\n{syn_err}", None

    log_path = os.path.join(BOTS_DIR, f"{filename}.log")
    log_f = open(log_path, "a", buffering=1)
    proc = subprocess.Popen(
        [sys.executable, filepath],
        cwd=BOTS_DIR,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    running_procs[filename] = proc
    time.sleep(1.0)

    if proc.poll() is not None:
        running_procs.pop(filename, None)
        err_tail = ""
        if os.path.exists(log_path):
            with open(log_path, "r", errors="replace") as lf:
                err_tail = "".join(lf.readlines()[-15:]).strip()
        return False, f"Crashed on launch:\n{err_tail or 'Unknown error'}", None

    return True, f"Started {filename} (PID {proc.pid}).", proc.pid


def stop_bot_file(filename: str):
    proc = running_procs.get(filename)
    if not proc or proc.poll() is None:
        running_procs.pop(filename, None)
        return False, "Already stopped."
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    running_procs.pop(filename, None)
    return True, f"Stopped {filename}."


def send_files_panel(chat_id: int, user_id: int = None):
    files = list_bot_files()
    backups = list_backup_files()
    kb = main_keyboard(user_id)

    backup_summary = (
        f"Backups ({len(backups)}): " + ", ".join(backups[-5:])
        if backups
        else "Backups: None yet"
    )

    if not files:
        bot.send_message(
            chat_id,
            f"FF PRO\nBot Files\n\nNo .py files uploaded yet.\n{backup_summary}",
            reply_markup=kb,
        )
        return

    bot.send_message(
        chat_id,
        f"FF PRO\nBot Files\nManage Your Files:\n{backup_summary}",
        reply_markup=kb,
    )
    for fname in files:
        proc = running_procs.get(fname)
        is_run = proc is not None and proc.poll() is None
        status = f"Running (PID {proc.pid})" if is_run else "Stopped"
        mk = types.InlineKeyboardMarkup(row_width=3)
        if is_run:
            mk.add(
                types.InlineKeyboardButton("Stop", callback_data=f"bot:stop:{fname}"),
                types.InlineKeyboardButton("Restart", callback_data=f"bot:restart:{fname}"),
                types.InlineKeyboardButton("Logs", callback_data=f"bot:logs:{fname}"),
            )
        else:
            mk.add(
                types.InlineKeyboardButton("Start", callback_data=f"bot:start:{fname}"),
                types.InlineKeyboardButton("Logs", callback_data=f"bot:logs:{fname}"),
                types.InlineKeyboardButton("Delete", callback_data=f"bot:delete:{fname}"),
            )
        bot.send_message(chat_id, f"{fname} (py) - {status}", reply_markup=mk)


def send_user_profile(chat_id: int, target_uid: int):
    with get_db() as conn:
        u = conn.execute("SELECT * FROM users WHERE user_id = ?", (target_uid,)).fetchone()
    if not u:
        bot.send_message(chat_id, f"User {target_uid} not found.")
        return

    u_files = get_user_uploaded_files(target_uid)
    running_cnt = sum(
        1 for f in u_files if f["filename"] in running_procs and running_procs[f["filename"]].poll() is None
    )
    sub = get_user_sub(target_uid)
    sub_str = f"{sub['plan_name']} (Exp: {sub['expires_at']})" if sub else "None"
    banned = bool(u["is_banned"] == 1)
    status_str = "Banned" if banned else "Active"
    uname_str = f"@{u['username']}" if u["username"] else "N/A"
    api_str = u["api_key"] if u["api_key"] else "Not created"
    last_act = u["last_active"] or u["joined_at"] or "N/A"

    txt = (
        "User Profile\n\n"
        f"Telegram User ID: {u['user_id']}\n"
        f"Chat ID: {u['user_id']}\n"
        f"Name: {u['first_name'] or 'User'}\n"
        f"Username: {uname_str}\n"
        f"Joined: {u['joined_at'] or 'N/A'}\n"
        f"Last Active: {last_act}\n"
        f"Files: {len(u_files)}\n"
        f"Running Bots: {running_cnt}\n"
        f"Subscription: {sub_str}\n"
        f"API Key: {api_str}\n"
        f"Account Status: {status_str}"
    )

    mk = types.InlineKeyboardMarkup(row_width=2)
    if banned:
        mk.add(types.InlineKeyboardButton("Unban User", callback_data=f"adm:unban_user:{target_uid}"))
    else:
        mk.add(types.InlineKeyboardButton("Ban User", callback_data=f"adm:ban_user:{target_uid}"))
    mk.add(
        types.InlineKeyboardButton("User Files", callback_data=f"adm:ufiles:{target_uid}"),
        types.InlineKeyboardButton("Running Bots", callback_data=f"adm:urunning:{target_uid}"),
    )
    mk.add(types.InlineKeyboardButton("Back to User List", callback_data="adm:users"))
    bot.send_message(chat_id, txt, reply_markup=mk)


def send_user_files_list(chat_id: int, target_uid: int):
    u_files = get_user_uploaded_files(target_uid)
    if not u_files:
        bot.send_message(chat_id, f"Files for User {target_uid}:\n\nNo files found.")
        return

    mk = types.InlineKeyboardMarkup(row_width=1)
    for f in u_files:
        fname = f["filename"]
        is_run = fname in running_procs and running_procs[fname].poll() is None
        badge = "Running" if is_run else "Stopped"
        mk.add(
            types.InlineKeyboardButton(
                f"{fname} (py) [{badge}]",
                callback_data=f"adm:ufile_act:{target_uid}:{fname}",
            )
        )
    mk.add(types.InlineKeyboardButton("Back to Profile", callback_data=f"adm:uprofile:{target_uid}"))
    bot.send_message(chat_id, f"Files for User {target_uid}:", reply_markup=mk)


def send_user_file_actions(chat_id: int, target_uid: int, fname: str):
    fname = os.path.basename(fname)
    proc = running_procs.get(fname)
    is_run = proc is not None and proc.poll() is None
    status_str = f"Running (PID {proc.pid})" if is_run else "Stopped"
    txt = f"File: {fname}\nOwner ID: {target_uid}\nStatus: {status_str}"

    mk = types.InlineKeyboardMarkup(row_width=2)
    if is_run:
        mk.add(types.InlineKeyboardButton("Stop", callback_data=f"adm:uf_stop:{target_uid}:{fname}"))
    else:
        mk.add(types.InlineKeyboardButton("Start", callback_data=f"adm:uf_start:{target_uid}:{fname}"))
    mk.add(types.InlineKeyboardButton("Backup to Telegram", callback_data=f"adm:uf_backup:{target_uid}:{fname}"))
    mk.add(types.InlineKeyboardButton("Delete", callback_data=f"adm:uf_del:{target_uid}:{fname}"))
    mk.add(types.InlineKeyboardButton("Back to User Files", callback_data=f"adm:ufiles:{target_uid}"))
    bot.send_message(chat_id, txt, reply_markup=mk)


def check_locked_and_notify(message) -> bool:
    uid = message.from_user.id
    if is_user_banned(uid):
        bot.send_message(message.chat.id, "You are banned from using this bot.")
        return True
    if not is_admin(uid) and get_setting("bot_locked") == "1":
        bot.send_message(message.chat.id, "Bot is currently locked by Admin.")
        return True
    return False


@bot.message_handler(commands=["start"])
def cmd_start(message):
    register_user(message)
    uid = message.from_user.id
    user_states.pop(uid, None)
    if check_locked_and_notify(message):
        return
    bot.send_message(
        message.chat.id,
        "FF PRO Bot Hosting\nSelect an option below:",
        reply_markup=main_keyboard(uid),
    )


@bot.message_handler(commands=["support"])
def cmd_support(message):
    register_user(message)
    if check_locked_and_notify(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.send_message(message.chat.id, "Usage: /support <your message>")
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO support_messages (user_id, message, created_at) VALUES (?, ?, ?)",
            (message.from_user.id, parts[1].strip(), now),
        )
    bot.send_message(message.chat.id, "Support message sent to Admin.")


@bot.message_handler(func=lambda m: m.text == "Updates Channel")
def menu_updates(message):
    register_user(message)
    if is_user_banned(message.from_user.id):
        bot.send_message(message.chat.id, "You are banned from using this bot.")
        return
    uid = message.from_user.id
    ch_url = get_updates_channel_url()
    mk = types.InlineKeyboardMarkup(row_width=1)
    try:
        mk.add(types.InlineKeyboardButton("Open Updates Channel", url=ch_url))
    except Exception:
        pass

    if is_admin(uid):
        mk.add(types.InlineKeyboardButton("Change / Set Channel Link", callback_data="adm:set_channel"))

    bot.send_message(message.chat.id, f"Updates Channel:\n{ch_url}", reply_markup=mk)


@bot.message_handler(func=lambda m: m.text == "View Plans")
def menu_view_plans(message):
    register_user(message)
    if check_locked_and_notify(message):
        return

    with get_db() as conn:
        plans = conn.execute("SELECT * FROM plans ORDER BY id ASC").fetchall()

    sub = get_user_sub(message.from_user.id)
    sub_text = (
        f"Active Plan: {sub['plan_name']} | Max Files: {sub['max_files']} | Expires: {sub['expires_at']}"
        if sub
        else "Active Plan: None"
    )

    lines = ["FF PRO Hosting Plans", sub_text, ""]
    mk = types.InlineKeyboardMarkup(row_width=1)
    for p in plans:
        lines.append(
            f"- {p['name']}: {p['max_files']} Files | {p['days']} Days | {p['price']} BDT"
        )
        mk.add(
            types.InlineKeyboardButton(
                f"Buy {p['name']} ({p['price']} BDT)",
                callback_data=f"buyplan:{p['id']}",
            )
        )

    bot.send_message(message.chat.id, "\n".join(lines), reply_markup=mk)


@bot.message_handler(func=lambda m: m.text == "Deploy Bot")
def menu_deploy_bot(message):
    register_user(message)
    if check_locked_and_notify(message):
        return

    uid = message.from_user.id
    if not is_admin(uid):
        sub = get_user_sub(uid)
        if not sub:
            bot.send_message(message.chat.id, "You need an active subscription. Tap 'View Plans'.")
            return
        if len(get_user_uploaded_files(uid)) >= sub["max_files"]:
            bot.send_message(
                message.chat.id,
                f"Plan file limit reached ({sub['max_files']} files).",
            )
            return

    user_states[uid] = {"step": "await_bot_file", "data": {}}
    bot.send_message(message.chat.id, "Send your .py bot file as a document. It will auto-run immediately.")


@bot.message_handler(content_types=["document"])
def on_document(message):
    register_user(message)
    if check_locked_and_notify(message):
        return

    uid = message.from_user.id
    if not is_admin(uid):
        sub = get_user_sub(uid)
        if not sub:
            bot.send_message(message.chat.id, "No active subscription.")
            return
        if len(get_user_uploaded_files(uid)) >= sub["max_files"]:
            bot.send_message(message.chat.id, "File limit reached for your plan.")
            return

    doc = message.document
    if not doc.file_name or not doc.file_name.endswith(".py"):
        bot.send_message(message.chat.id, "Please send a valid .py file.")
        return

    safe_name = os.path.basename(doc.file_name)
    filepath = os.path.join(BOTS_DIR, safe_name)

    stop_bot_file(safe_name)

    finfo = bot.get_file(doc.file_id)
    content = bot.download_file(finfo.file_path)
    with open(filepath, "wb") as f:
        f.write(content)

    record_user_file(uid, safe_name)
    bname = backup_bot_file(safe_name)
    user_states.pop(uid, None)
    log_activity(uid, f"Uploaded & auto-started {safe_name} (Backup: {bname})")

    bot.send_message(message.chat.id, f"File {safe_name} uploaded successfully!")

    started, status_msg, pid = start_bot_file(safe_name)
    mk = types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        types.InlineKeyboardButton("Restart", callback_data=f"bot:restart:{safe_name}"),
        types.InlineKeyboardButton("Logs", callback_data=f"bot:logs:{safe_name}"),
    )

    if started:
        bot.send_message(
            message.chat.id,
            f"Python Script Started!\nFile: {safe_name}\nPID: {pid}\nBackup: {bname}",
            reply_markup=mk,
        )
    else:
        bot.send_message(
            message.chat.id,
            f"Script Start Failed!\nFile: {safe_name}\nBackup: {bname}\n\n{status_msg[:3500]}",
            reply_markup=mk,
        )


@bot.message_handler(func=lambda m: m.text == "Bot Files")
def menu_bot_files(message):
    register_user(message)
    if check_locked_and_notify(message):
        return
    send_files_panel(message.chat.id, message.from_user.id)


@bot.message_handler(func=lambda m: m.text == "Speed & Ping")
def menu_speed_ping(message):
    register_user(message)
    if is_user_banned(message.from_user.id):
        bot.send_message(message.chat.id, "You are banned from using this bot.")
        return
    t0 = time.perf_counter()
    bot.get_me()
    ping_ms = int((time.perf_counter() - t0) * 1000)
    load_1m, ram_pct = get_system_stats()
    txt = (
        "FF PRO\n"
        "Speed & Ping\n"
        "Server Status\n\n"
        f"Telegram API: {ping_ms} ms\n"
        f"Load (1m): {load_1m}\n"
        f"RAM: {ram_pct}%\n"
        "Status: Online"
    )
    bot.send_message(message.chat.id, txt)


@bot.message_handler(func=lambda m: m.text == "Bot Stats")
def menu_bot_stats(message):
    register_user(message)
    if is_user_banned(message.from_user.id):
        bot.send_message(message.chat.id, "You are banned from using this bot.")
        return
    files = list_bot_files()
    backups = list_backup_files()
    online = sum(1 for f in files if f in running_procs and running_procs[f].poll() is None)
    with get_db() as conn:
        u_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        s_count = conn.execute("SELECT COUNT(*) AS c FROM subscriptions").fetchone()["c"]
    txt = (
        "FF PRO Bot Stats\n\n"
        f"Total Users: {u_count}\n"
        f"Active/Recorded Subs: {s_count}\n"
        f"Total Bot Files: {len(files)}\n"
        f"Backup Files: {len(backups)}\n"
        f"Running Bots: {online}"
    )
    bot.send_message(message.chat.id, txt)


@bot.message_handler(func=lambda m: m.text == "Terminal")
def menu_terminal(message):
    register_user(message)
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "Admin only.")
        return
    bot.send_message(message.chat.id, "Run command: /exec <cmd>\nExample: /exec ls -la")


@bot.message_handler(commands=["exec"])
def cmd_exec(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "Admin only.")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.send_message(message.chat.id, "Usage: /exec <command>")
        return
    try:
        res = subprocess.run(
            parts[1].strip(),
            shell=True,
            cwd=BOTS_DIR,
            capture_output=True,
            text=True,
            timeout=15,
        )
        out = ((res.stdout or "") + (res.stderr or "")).strip() or "(No output)"
        log_activity(message.from_user.id, f"Exec: {parts[1].strip()[:60]}")
        bot.send_message(message.chat.id, out[:3800])
    except Exception as e:
        bot.send_message(message.chat.id, f"Error: {e}")


@bot.message_handler(func=lambda m: m.text == "Admin Panel")
def menu_admin_panel(message):
    register_user(message)
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "Access denied: Admin only.")
        return
    bot.send_message(
        message.chat.id,
        "FF PRO\nAdmin Panel\nAdmin Control Panel:",
        reply_markup=admin_panel_keyboard(),
    )


@bot.message_handler(func=lambda m: m.from_user.id in user_states)
def handle_state_input(message):
    uid = message.from_user.id
    if is_user_banned(uid):
        user_states.pop(uid, None)
        bot.send_message(message.chat.id, "You are banned from using this bot.")
        return
    st = user_states.get(uid)
    if not st:
        return
    step = st["step"]
    data = st["data"]
    txt = (message.text or "").strip()

    if step == "add_plan_name":
        data["name"] = txt
        st["step"] = "add_plan_files"
        bot.send_message(message.chat.id, "How many bot files allowed? (e.g. 5 or 10)")
    elif step == "add_plan_files":
        if not txt.isdigit():
            bot.send_message(message.chat.id, "Enter a valid number for max files.")
            return
        data["max_files"] = int(txt)
        st["step"] = "add_plan_days"
        bot.send_message(message.chat.id, "Validity in days? (e.g. 30)")
    elif step == "add_plan_days":
        if not txt.isdigit():
            bot.send_message(message.chat.id, "Enter a valid number of days.")
            return
        data["days"] = int(txt)
        st["step"] = "add_plan_price"
        bot.send_message(message.chat.id, "Plan price in BDT? (e.g. 200)")
    elif step == "add_plan_price":
        if not txt.isdigit():
            bot.send_message(message.chat.id, "Enter a valid price number.")
            return
        price = int(txt)
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO plans (name, max_files, days, price) VALUES (?, ?, ?, ?)",
                (data["name"], data["max_files"], data["days"], price),
            )
        user_states.pop(uid, None)
        log_activity(uid, f"Added/Updated plan {data['name']}")
        bot.send_message(
            message.chat.id,
            f"Plan saved:\n{data['name']} | {data['max_files']} Files | {data['days']} Days | {price} BDT",
            reply_markup=admin_panel_keyboard(),
        )

    elif step == "add_sub_uid":
        if not txt.isdigit():
            bot.send_message(message.chat.id, "Enter numeric Telegram User ID.")
            return
        data["target_uid"] = int(txt)
        st["step"] = "add_sub_files"
        bot.send_message(message.chat.id, "Max files for this user? (e.g. 10)")
    elif step == "add_sub_files":
        if not txt.isdigit():
            bot.send_message(message.chat.id, "Enter numeric file limit.")
            return
        data["max_files"] = int(txt)
        st["step"] = "add_sub_days"
        bot.send_message(message.chat.id, "Subscription duration in days? (e.g. 30)")
    elif step == "add_sub_days":
        if not txt.isdigit():
            bot.send_message(message.chat.id, "Enter numeric days.")
            return
        days = int(txt)
        exp_str = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO subscriptions (user_id, plan_name, max_files, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    plan_name=excluded.plan_name,
                    max_files=excluded.max_files,
                    expires_at=excluded.expires_at
                """,
                (data["target_uid"], f"Custom ({days}d)", data["max_files"], exp_str),
            )
        user_states.pop(uid, None)
        log_activity(uid, f"Added sub for {data['target_uid']} ({days}d, {data['max_files']} files)")
        bot.send_message(
            message.chat.id,
            f"Subscription active for {data['target_uid']} until {exp_str}.",
            reply_markup=admin_panel_keyboard(),
        )

    elif step == "rem_sub_uid":
        if not txt.isdigit():
            bot.send_message(message.chat.id, "Enter numeric User ID.")
            return
        t_uid = int(txt)
        with get_db() as conn:
            conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (t_uid,))
        user_states.pop(uid, None)
        log_activity(uid, f"Removed sub for {t_uid}")
        bot.send_message(message.chat.id, f"Removed subscription for {t_uid}.", reply_markup=admin_panel_keyboard())

    elif step == "set_pay_val":
        method = data["method"]
        set_setting(method, txt)
        user_states.pop(uid, None)
        log_activity(uid, f"Updated payment setting {method}")
        bot.send_message(message.chat.id, f"Updated {method.upper()} info to:\n{txt}", reply_markup=admin_panel_keyboard())

    elif step == "set_channel_url":
        if not (txt.startswith("http://") or txt.startswith("https://") or txt.startswith("t.me/")):
            bot.send_message(message.chat.id, "Send a valid channel link (e.g. https://t.me/your_channel):")
            return
        if txt.startswith("t.me/"):
            txt = "https://" + txt
        set_setting("updates_channel", txt)
        user_states.pop(uid, None)
        log_activity(uid, f"Updated channel link: {txt}")
        bot.send_message(
            message.chat.id,
            f"Updates Channel link updated to:\n{txt}",
            reply_markup=admin_panel_keyboard(),
        )

    elif step == "add_admin_uid":
        if not txt.isdigit():
            bot.send_message(message.chat.id, "Enter numeric User ID.")
            return
        t_uid = int(txt)
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (t_uid,))
        user_states.pop(uid, None)
        log_activity(uid, f"Added admin {t_uid}")
        bot.send_message(message.chat.id, f"Added admin {t_uid}.", reply_markup=admin_panel_keyboard())

    elif step == "rem_admin_uid":
        if not txt.isdigit():
            bot.send_message(message.chat.id, "Enter numeric User ID.")
            return
        t_uid = int(txt)
        if t_uid == OWNER_ID:
            bot.send_message(message.chat.id, "Cannot remove Owner.")
            return
        with get_db() as conn:
            conn.execute("DELETE FROM admins WHERE user_id = ?", (t_uid,))
        user_states.pop(uid, None)
        log_activity(uid, f"Removed admin {t_uid}")
        bot.send_message(message.chat.id, f"Removed admin {t_uid}.", reply_markup=admin_panel_keyboard())

    elif step == "broadcast_msg":
        user_states.pop(uid, None)
        with get_db() as conn:
            users = conn.execute("SELECT user_id FROM users").fetchall()
        sent, failed = 0, 0
        for r in users:
            try:
                bot.send_message(r["user_id"], txt)
                sent += 1
            except Exception:
                failed += 1
        log_activity(uid, f"Broadcast sent: {sent}, failed: {failed}")
        bot.send_message(message.chat.id, f"Broadcast done. Sent: {sent}, Failed: {failed}")

    elif step == "set_api_key":
        with get_db() as conn:
            conn.execute("UPDATE users SET api_key = ? WHERE user_id = ?", (txt, uid))
        user_states.pop(uid, None)
        bot.send_message(message.chat.id, "API Key updated.")

    elif step == "pay_submit_proof":
        plan = data["plan"]
        method = data["method"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO payment_requests (user_id, method, plan_name, amount, sender_info, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (uid, method, plan["name"], plan["price"], txt, now),
            )
            req_id = cur.lastrowid
        user_states.pop(uid, None)
        bot.send_message(
            message.chat.id,
            f"Payment request #{req_id} submitted ({method.upper()}). Waiting for Admin approval.",
        )

        u_obj = message.from_user
        uname_str = f"@{u_obj.username}" if u_obj and u_obj.username else "No username"
        mk_adm = types.InlineKeyboardMarkup(row_width=2)
        mk_adm.add(
            types.InlineKeyboardButton("Approve", callback_data=f"adm:approve_req:{req_id}"),
            types.InlineKeyboardButton("Reject", callback_data=f"adm:reject_req:{req_id}"),
        )
        alert_txt = (
            f"New Payment Request #{req_id}\n"
            f"User: {uid} ({uname_str})\n"
            f"Method: {method.upper()} | Plan: {plan['name']} ({plan['price']} BDT)\n"
            f"Proof: {txt}"
        )
        for adm_id in get_admin_ids():
            try:
                bot.send_message(adm_id, alert_txt, reply_markup=mk_adm)
            except Exception:
                pass


@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    data = call.data or ""
    uid = call.from_user.id

    if is_user_banned(uid) and not data.startswith("adm:"):
        bot.answer_callback_query(call.id, "You are banned.", show_alert=True)
        return

    if data.startswith("buyplan:"):
        plan_id = int(data.split(":", 1)[1])
        with get_db() as conn:
            plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan:
            bot.answer_callback_query(call.id, "Plan not found.")
            return
        mk = types.InlineKeyboardMarkup(row_width=2)
        for m in ["bkash", "nagad", "rocket", "upay"]:
            mk.add(types.InlineKeyboardButton(m.upper(), callback_data=f"paymethod:{plan_id}:{m}"))
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"Plan: {plan['name']} ({plan['price']} BDT, {plan['days']} Days, {plan['max_files']} Files)\nSelect payment method:",
            reply_markup=mk,
        )
        return

    if data.startswith("paymethod:"):
        _, plan_id_s, method = data.split(":", 2)
        plan_id = int(plan_id_s)
        with get_db() as conn:
            plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan:
            bot.answer_callback_query(call.id, "Plan not found.")
            return
        acc_info = get_setting(method)
        user_states[uid] = {
            "step": "pay_submit_proof",
            "data": {"plan": dict(plan), "method": method},
        }
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"{method.upper()} Account: {acc_info}\nAmount: {plan['price']} BDT\n\nSend your sender number or Transaction ID now:",
        )
        return

    if data.startswith("bot:"):
        _, action, fname = data.split(":", 2)
        fname = os.path.basename(fname)
        if action in ("start", "stop", "restart", "delete") and not is_admin(uid):
            bot.answer_callback_query(call.id, "Admin only!", show_alert=True)
            return

        if action == "start":
            ok, msg, _ = start_bot_file(fname)
            bot.answer_callback_query(call.id, msg[:180], show_alert=not ok)
            if not ok:
                bot.send_message(call.message.chat.id, f"Start failed for {fname}:\n\n{msg[:3500]}")
            send_files_panel(call.message.chat.id, uid)
        elif action == "stop":
            _, msg = stop_bot_file(fname)
            bot.answer_callback_query(call.id, msg)
            send_files_panel(call.message.chat.id, uid)
        elif action == "restart":
            stop_bot_file(fname)
            ok, msg, _ = start_bot_file(fname)
            bot.answer_callback_query(call.id, msg[:180], show_alert=not ok)
            if not ok:
                bot.send_message(call.message.chat.id, f"Restart failed for {fname}:\n\n{msg[:3500]}")
            send_files_panel(call.message.chat.id, uid)
        elif action == "delete":
            stop_bot_file(fname)
            p = os.path.join(BOTS_DIR, fname)
            if os.path.exists(p):
                os.remove(p)
            remove_user_file_record(fname)
            bot.answer_callback_query(call.id, f"Deleted {fname}")
            send_files_panel(call.message.chat.id, uid)
        elif action == "logs":
            lp = os.path.join(BOTS_DIR, f"{fname}.log")
            if os.path.exists(lp):
                with open(lp, "r", errors="replace") as f:
                    lines = f.readlines()[-25:]
                txt = "".join(lines).strip() or "Empty log."
            else:
                txt = "No log file."
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, f"Logs ({fname}):\n\n{txt[:3800]}")
        return

    if data.startswith("adm:"):
        if not is_admin(uid):
            bot.answer_callback_query(call.id, "Admin only!", show_alert=True)
            return

        parts = data.split(":")
        sub_cmd = parts[1]

        if sub_cmd == "add_plan":
            user_states[uid] = {"step": "add_plan_name", "data": {}}
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "Enter new Plan Name (e.g. VIP 20 Bots):")

        elif sub_cmd == "manage_plans":
            with get_db() as conn:
                plans = conn.execute("SELECT * FROM plans ORDER BY id ASC").fetchall()
            bot.answer_callback_query(call.id)
            if not plans:
                bot.send_message(call.message.chat.id, "No plans found.")
                return
            mk = types.InlineKeyboardMarkup(row_width=1)
            lines = ["Manage Plans:"]
            for p in plans:
                lines.append(f"#{p['id']} {p['name']} | {p['max_files']} files | {p['days']}d | {p['price']} BDT")
                mk.add(types.InlineKeyboardButton(f"Delete Plan: {p['name']}", callback_data=f"adm:del_plan:{p['id']}"))
            bot.send_message(call.message.chat.id, "\n".join(lines), reply_markup=mk)

        elif sub_cmd == "del_plan":
            pid = int(parts[2])
            with get_db() as conn:
                conn.execute("DELETE FROM plans WHERE id = ?", (pid,))
            bot.answer_callback_query(call.id, "Plan deleted.")
            bot.send_message(call.message.chat.id, "Plan deleted.", reply_markup=admin_panel_keyboard())

        elif sub_cmd == "add_sub":
            user_states[uid] = {"step": "add_sub_uid", "data": {}}
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "Enter target User ID:")

        elif sub_cmd == "rem_sub":
            user_states[uid] = {"step": "rem_sub_uid", "data": {}}
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "Enter User ID to remove subscription:")

        elif sub_cmd == "pay_set":
            mk = types.InlineKeyboardMarkup(row_width=2)
            for m in ["bkash", "nagad", "rocket", "upay"]:
                mk.add(types.InlineKeyboardButton(f"Set {m.upper()}", callback_data=f"adm:set_pay:{m}"))
            bot.answer_callback_query(call.id)
            txt = (
                "Payment Settings:\n"
                f"bKash: {get_setting('bkash')}\n"
                f"Nagad: {get_setting('nagad')}\n"
                f"Rocket: {get_setting('rocket')}\n"
                f"Upay: {get_setting('upay')}"
            )
            bot.send_message(call.message.chat.id, txt, reply_markup=mk)

        elif sub_cmd == "set_pay":
            m = parts[2]
            user_states[uid] = {"step": "set_pay_val", "data": {"method": m}}
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, f"Enter {m.upper()} number/instructions:")

        elif sub_cmd == "set_channel":
            user_states[uid] = {"step": "set_channel_url", "data": {}}
            bot.answer_callback_query(call.id)
            bot.send_message(
                call.message.chat.id,
                f"Current Updates Channel:\n{get_updates_channel_url()}\n\nSend new Updates Channel link:",
            )

        elif sub_cmd == "pay_req":
            with get_db() as conn:
                reqs = conn.execute(
                    "SELECT * FROM payment_requests WHERE status = 'pending' ORDER BY id DESC LIMIT 10"
                ).fetchall()
            bot.answer_callback_query(call.id)
            if not reqs:
                bot.send_message(call.message.chat.id, "No pending payment requests.")
                return
            for r in reqs:
                mk = types.InlineKeyboardMarkup(row_width=2)
                mk.add(
                    types.InlineKeyboardButton("Approve", callback_data=f"adm:approve_req:{r['id']}"),
                    types.InlineKeyboardButton("Reject", callback_data=f"adm:reject_req:{r['id']}"),
                )
                bot.send_message(
                    call.message.chat.id,
                    f"Req #{r['id']} | User: {r['user_id']}\nMethod: {r['method'].upper()} | Plan: {r['plan_name']} ({r['amount']} BDT)\nProof: {r['sender_info']}",
                    reply_markup=mk,
                )

        elif sub_cmd == "approve_req":
            req_id = int(parts[2])
            with get_db() as conn:
                r = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (req_id,)).fetchone()
                if r and r["status"] == "pending":
                    pl = conn.execute("SELECT * FROM plans WHERE name = ?", (r["plan_name"],)).fetchone()
                    days = pl["days"] if pl else 30
                    max_f = pl["max_files"] if pl else 5
                    exp = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        """
                        INSERT INTO subscriptions (user_id, plan_name, max_files, expires_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(user_id) DO UPDATE SET
                            plan_name=excluded.plan_name,
                            max_files=excluded.max_files,
                            expires_at=excluded.expires_at
                        """,
                        (r["user_id"], r["plan_name"], max_f, exp),
                    )
                    conn.execute("UPDATE payment_requests SET status = 'approved' WHERE id = ?", (req_id,))
                    try:
                        bot.send_message(r["user_id"], f"Payment approved! Plan {r['plan_name']} active until {exp}.")
                    except Exception:
                        pass
            bot.answer_callback_query(call.id, "Approved.")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass

        elif sub_cmd == "reject_req":
            req_id = int(parts[2])
            with get_db() as conn:
                r = conn.execute("SELECT * FROM payment_requests WHERE id = ?", (req_id,)).fetchone()
                if r and r["status"] == "pending":
                    conn.execute("UPDATE payment_requests SET status = 'rejected' WHERE id = ?", (req_id,))
                    try:
                        bot.send_message(r["user_id"], f"Payment request #{req_id} was rejected.")
                    except Exception:
                        pass
            bot.answer_callback_query(call.id, "Rejected.")
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            except Exception:
                pass

        elif sub_cmd == "add_admin":
            user_states[uid] = {"step": "add_admin_uid", "data": {}}
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "Enter User ID to promote to Admin:")

        elif sub_cmd == "rem_admin":
            user_states[uid] = {"step": "rem_admin_uid", "data": {}}
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "Enter Admin User ID to remove:")

        elif sub_cmd == "broadcast":
            user_states[uid] = {"step": "broadcast_msg", "data": {}}
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "Send the broadcast message text:")

        elif sub_cmd == "lock_toggle":
            cur = get_setting("bot_locked")
            nxt = "0" if cur == "1" else "1"
            set_setting("bot_locked", nxt)
            bot.answer_callback_query(call.id, f"Locked: {nxt == '1'}")
            bot.send_message(
                call.message.chat.id,
                f"Bot Lock State: {'LOCKED' if nxt == '1' else 'UNLOCKED'}",
                reply_markup=admin_panel_keyboard(),
            )

        elif sub_cmd == "run_all":
            files = list_bot_files()
            started = 0
            for f in files:
                ok, _, _ = start_bot_file(f)
                if ok:
                    started += 1
            bot.answer_callback_query(call.id, f"Started {started} bots.")
            send_files_panel(call.message.chat.id, uid)

        elif sub_cmd == "bot_stats":
            files = list_bot_files()
            online = sum(1 for f in files if f in running_procs and running_procs[f].poll() is None)
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, f"Scripts: {len(files)} | Running: {online}")

        elif sub_cmd == "users":
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT user_id, username, first_name, is_banned FROM users ORDER BY user_id DESC LIMIT 20"
                ).fetchall()
            bot.answer_callback_query(call.id)
            if not rows:
                bot.send_message(call.message.chat.id, "No users found.")
                return
            mk = types.InlineKeyboardMarkup(row_width=1)
            for r in rows:
                u_name = f"@{r['username']}" if r["username"] else "no_username"
                f_name = r["first_name"] or "User"
                b_tag = " [BANNED]" if r["is_banned"] == 1 else ""
                btn_txt = f"{f_name} | {r['user_id']} | {u_name}{b_tag}"[:64]
                mk.add(types.InlineKeyboardButton(btn_txt, callback_data=f"adm:uprofile:{r['user_id']}"))
            bot.send_message(call.message.chat.id, "Select a user:", reply_markup=mk)

        elif sub_cmd == "uprofile":
            t_uid = int(parts[2])
            bot.answer_callback_query(call.id)
            send_user_profile(call.message.chat.id, t_uid)

        elif sub_cmd == "ban_user":
            t_uid = int(parts[2])
            if t_uid == OWNER_ID:
                bot.answer_callback_query(call.id, "Cannot ban Owner!", show_alert=True)
                return
            with get_db() as conn:
                conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (t_uid,))
            log_activity(uid, f"Banned user {t_uid}")
            bot.answer_callback_query(call.id, f"Banned {t_uid}")
            send_user_profile(call.message.chat.id, t_uid)

        elif sub_cmd == "unban_user":
            t_uid = int(parts[2])
            with get_db() as conn:
                conn.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (t_uid,))
            log_activity(uid, f"Unbanned user {t_uid}")
            bot.answer_callback_query(call.id, f"Unbanned {t_uid}")
            send_user_profile(call.message.chat.id, t_uid)

        elif sub_cmd == "ufiles":
            t_uid = int(parts[2])
            bot.answer_callback_query(call.id)
            send_user_files_list(call.message.chat.id, t_uid)

        elif sub_cmd == "urunning":
            t_uid = int(parts[2])
            u_files = get_user_uploaded_files(t_uid)
            run_list = [
                f["filename"]
                for f in u_files
                if f["filename"] in running_procs and running_procs[f["filename"]].poll() is None
            ]
            bot.answer_callback_query(call.id)
            if not run_list:
                bot.send_message(call.message.chat.id, f"Running Bots for User {t_uid}:\n\nNone running.")
                return
            mk = types.InlineKeyboardMarkup(row_width=1)
            for fn in run_list:
                pid_num = running_procs[fn].pid
                mk.add(types.InlineKeyboardButton(f"{fn} (PID {pid_num})", callback_data=f"adm:ufile_act:{t_uid}:{fn}"))
            bot.send_message(call.message.chat.id, f"Running Bots for User {t_uid} ({len(run_list)}):", reply_markup=mk)

        elif sub_cmd == "ufile_act":
            t_uid = int(parts[2])
            fname = os.path.basename(":".join(parts[3:]))
            bot.answer_callback_query(call.id)
            send_user_file_actions(call.message.chat.id, t_uid, fname)

        elif sub_cmd == "uf_start":
            t_uid = int(parts[2])
            fname = os.path.basename(":".join(parts[3:]))
            ok, msg, _ = start_bot_file(fname)
            bot.answer_callback_query(call.id, msg[:180], show_alert=not ok)
            if not ok:
                bot.send_message(call.message.chat.id, f"Start failed for {fname}:\n\n{msg[:3500]}")
            send_user_file_actions(call.message.chat.id, t_uid, fname)

        elif sub_cmd == "uf_stop":
            t_uid = int(parts[2])
            fname = os.path.basename(":".join(parts[3:]))
            _, msg = stop_bot_file(fname)
            bot.answer_callback_query(call.id, msg[:180])
            send_user_file_actions(call.message.chat.id, t_uid, fname)

        elif sub_cmd == "uf_backup":
            t_uid = int(parts[2])
            fname = os.path.basename(":".join(parts[3:]))
            src = os.path.join(BOTS_DIR, fname)
            if not os.path.exists(src):
                bot.answer_callback_query(call.id, "File not found!", show_alert=True)
                return
            bname = backup_bot_file(fname)
            bot.answer_callback_query(call.id, "Sending backup...")
            with open(src, "rb") as doc_f:
                bot.send_document(
                    call.message.chat.id,
                    doc_f,
                    visible_file_name=fname,
                    caption=f"Backup for User {t_uid}\nFile: {fname}\nSaved Backup: {bname}",
                )

        elif sub_cmd == "uf_del":
            t_uid = int(parts[2])
            fname = os.path.basename(":".join(parts[3:]))
            stop_bot_file(fname)
            p = os.path.join(BOTS_DIR, fname)
            if os.path.exists(p):
                os.remove(p)
            remove_user_file_record(fname)
            log_activity(uid, f"Deleted file {fname} of user {t_uid}")
            bot.answer_callback_query(call.id, f"Deleted {fname}")
            send_user_files_list(call.message.chat.id, t_uid)

        elif sub_cmd == "api":
            user_states[uid] = {"step": "set_api_key", "data": {}}
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "Send new API key/token value to save:")

        elif sub_cmd == "support":
            with get_db() as conn:
                msgs = conn.execute("SELECT * FROM support_messages ORDER BY id DESC LIMIT 10").fetchall()
            bot.answer_callback_query(call.id)
            if not msgs:
                bot.send_message(call.message.chat.id, "Support inbox is empty.")
            else:
                lines = ["Support Inbox:"] + [f"[{m['created_at']}] {m['user_id']}: {m['message']}" for m in msgs]
                bot.send_message(call.message.chat.id, "\n".join(lines)[:3800])

        elif sub_cmd == "server":
            load_1m, ram_pct = get_system_stats()
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, f"Server Monitor:\nLoad (1m): {load_1m}\nRAM: {ram_pct}%")

        elif sub_cmd == "logs":
            with get_db() as conn:
                logs = conn.execute("SELECT * FROM activity_logs ORDER BY id DESC LIMIT 15").fetchall()
            bot.answer_callback_query(call.id)
            if not logs:
                bot.send_message(call.message.chat.id, "No activity logs yet.")
            else:
                lines = ["Activity Logs:"] + [f"[{l['created_at']}] {l['actor_id']}: {l['action']}" for l in logs]
                bot.send_message(call.message.chat.id, "\n".join(lines)[:3800])


if __name__ == "__main__":
    init_db()
    print("FF PRO Hosting Bot running...")
    bot.infinity_polling(skip_pending=True)
