import os
import json
import gspread
import dateparser
import asyncio
import pytz
import re
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from telegram import Bot, Update, BotCommand, InputFile
from telegram import BotCommandScopeDefault, BotCommandScopeChat
from zoneinfo import ZoneInfo
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, JobQueue
)
from telegram.request import HTTPXRequest
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove

# ===================== CONFIG =====================
TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1vvBRrL-qXx0jp5-ZRR4xVpOi5ejxE8DtxrHOrel7F78"
GROUP_CHAT_ID = -1003073406158
ADMIN_ID = 171208804  # Replace with your telegram id

# Conversation states
DATE, TIME, CANCEL_SELECT = range(3)
ANNOUNCE_MESSAGE = 200

# States for docs upload/download
DOC_SELECT = 100
UPLOAD_DOC = 101

# Ensure docs folder exists
os.makedirs("docs", exist_ok=True)
if not os.listdir("docs"):
    open("docs/.keep", "w").close()
print("✅ 'docs' folder ready (auto-created if missing).")

# ===================== GOOGLE SHEETS =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds_json = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
client = gspread.authorize(creds)
sheet = client.open_by_url(SPREADSHEET_URL).sheet1
spreadsheet = client.open_by_url(SPREADSHEET_URL)
try:
    stats_sheet = spreadsheet.worksheet("UserStats")
except gspread.exceptions.WorksheetNotFound:
    stats_sheet = spreadsheet.add_worksheet(title="UserStats", rows="1000", cols="4")
    stats_sheet.append_row(["TelegramID", "Name", "Command", "DateTime"])

# ===================== HELPERS =====================

def sort_key(row):
    """Reusable sort key: parse Date and start Time; fallback to max values."""
    try:
        date_obj = datetime.strptime(row["Date"], "%d/%m/%Y")
        time_start = row["Time"].split("-")[0] if "-" in row["Time"] else row["Time"]
        time_obj = datetime.strptime(time_start.strip(), "%H:%M")
        return (date_obj, time_obj)
    except Exception:
        return (datetime.max, datetime.max)

def log_user_action(user, command):
    """Log each user command to the 'UserStats' sheet (Phnom Penh time)."""
    try:
        now = datetime.now(ZoneInfo("Asia/Phnom_Penh"))
        now_str = now.strftime("%d/%m/%Y %H:%M:%S")
        stats_sheet.append_row([str(user.id), user.first_name, command, now_str])
        print(f"✅ Logged {command} by {user.first_name} at {now_str}")
    except Exception as e:
        print(f"⚠️ Could not log action: {e}")

def time_to_minutes(time_str):
    """Convert 'HH:MM' to total minutes for easy comparison."""
    h, m = map(int, time_str.split(":"))
    return h * 60 + m

def is_overlapping(existing_start, existing_end, new_start, new_end):
    """Check if two time ranges overlap."""
    return not (new_end <= existing_start or new_start >= existing_end)

def save_booking(date_str, time_str, name, telegram_id):
    """Save a booking only if the time range does not overlap with existing ones."""
    try:
        new_start_str, new_end_str = time_str.split("-")
        new_start = time_to_minutes(new_start_str.strip())
        new_end = time_to_minutes(new_end_str.strip())
    except ValueError:
        # Invalid time format
        return "invalid"

    records = sheet.get_all_records()
    for row in records:
        if row.get("Date") == date_str:
            try:
                exist_start_str, exist_end_str = row["Time"].split("-")
                exist_start = time_to_minutes(exist_start_str.strip())
                exist_end = time_to_minutes(exist_end_str.strip())

                if is_overlapping(exist_start, exist_end, new_start, new_end):
                    return "overlap"
            except Exception:
                continue

    # If no overlap → save
    sheet.append_row([date_str, time_str, name, str(telegram_id)])
    return "success"

def cancel_booking(telegram_id, date_str, time_str):
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):
        if (
            row.get("TelegramID") == str(telegram_id)
            and row.get("Date") == date_str
            and row.get("Time") == time_str
        ):
            sheet.delete_rows(i)
            return True
    return False

# ===================== BOT COMMANDS =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/start")
    await update.message.reply_text(
        "👋 Welcome to the Meeting Room Bot!\n\n"
        "Commands:\n"
        "/book - Book the meeting room\n"
        "/cancel - Cancel your booking\n"
        "/end - End the active meeting\n"
        "/docs - Download available documents"
    )

async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/book")
    await update.message.reply_text("📅 Please enter the date (e.g. 30/10/2025 or 30/10):")
    return DATE

# ----------------- Get Date -----------------
async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_input = update.message.text.strip()

    # Validate format using regex before parsing
    if not re.match(r"^\d{1,2}/\d{1,2}(/?\d{2,4})?$", date_input):
        await update.message.reply_text("❌ Please enter date in format DD/MM or DD/MM/YYYY.")
        return DATE

    # Add current year if user omits it
    if len(date_input.split("/")) == 2:
        date_input = f"{date_input}/{datetime.now().year}"

    # Parse with DMY order
    date_obj = dateparser.parse(date_input, settings={"DATE_ORDER": "DMY"})
    if not date_obj:
        await update.message.reply_text("❌ Invalid date. Try again (example: 25/10 or 25/10/2025).")
        return DATE

    # Check if date is in the past (compare dates)
    if date_obj.date() < datetime.now().date():
        await update.message.reply_text("⚠️ The date you entered is in the past. Please choose a future date.")
        return DATE

    context.user_data["date"] = date_obj.strftime("%d/%m/%Y")
    await update.message.reply_text("⏰ Great! Now enter the time range (e.g. 14:00-15:00):")
    return TIME

# ----------------- Get Time & Save -----------------
async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_input = update.message.text.strip()
    user = update.message.from_user
    date_str = context.user_data.get("date")

    if not re.match(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$", time_input):
        await update.message.reply_text("❌ Invalid time format. Use HH:MM-HH:MM (e.g. 09:00-10:30).")
        return TIME

    start_str, end_str = [t.strip() for t in time_input.split("-")]
    try:
        start_time = datetime.strptime(start_str, "%H:%M")
        end_time = datetime.strptime(end_str, "%H:%M")
    except ValueError:
        await update.message.reply_text("❌ Invalid time values. Please check your input again.")
        return TIME

    if end_time <= start_time:
        await update.message.reply_text("⚠️ End time must be later than start time.")
        return TIME

    result = save_booking(date_str, time_input, user.first_name, user.id)

    if result == "overlap":
        await update.message.reply_text("⚠️ That time overlaps with another booking. Please choose another slot.")
        return TIME
    elif result == "invalid":
        await update.message.reply_text("❌ Could not save booking. Please try again.")
        return TIME
    elif result == "success":
        await update.message.reply_text(f"✅ Booking confirmed for {date_str} at {time_input}.")

        # Announce to group with sorted schedule
        try:
            records = sheet.get_all_records()
            records.sort(key=sort_key)

            message = (
                f"📢 *New Booking Added!*\n\n"
                f"👤 {user.first_name}\n"
                f"🗓 {date_str} | ⏰ {time_input}\n\n"
                f"📋 *Current Schedule:*\n"
            )

            for row in records:
                message += f"{row['Date']} | {row['Time']} | {row['Name']}\n"

            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=message, parse_mode="Markdown")
            print("✅ Group message with sorted schedule sent.")
        except Exception as e:
            print(f"⚠️ Could not send group message: {e}")

# ----------------- Cancel flow -----------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/cancel")
    records = sheet.get_all_records()

    user_bookings = [
        (i + 2, row) for i, row in enumerate(records)
        if str(row.get("TelegramID")) == str(user.id)
    ]

    if not user_bookings:
        await update.message.reply_text("❌ You don’t have any bookings to cancel.")
        return ConversationHandler.END

    message = "🗓 *Your Bookings:*\n\n"
    for idx, (row_num, row) in enumerate(user_bookings, start=1):
        message += f"{idx}. {row['Date']} | {row['Time']}\n"

    message += "\nReply with the *number* of the booking you want to delete:"
    await update.message.reply_text(message, parse_mode="Markdown")

    context.user_data["user_bookings"] = user_bookings
    return CANCEL_SELECT

async def delete_booking_by_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    user_bookings = context.user_data.get("user_bookings", [])
    user = update.message.from_user

    try:
        choice = int(user_input)
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number.")
        return CANCEL_SELECT

    if not (1 <= choice <= len(user_bookings)):
        await update.message.reply_text("❌ Invalid choice. Try again.")
        return CANCEL_SELECT

    row_index, booking = user_bookings[choice - 1]
    canceled_date = booking["Date"]
    canceled_time = booking["Time"]
    sheet.delete_rows(row_index)

    await update.message.reply_text(f"✅ Canceled booking on {canceled_date} at {canceled_time}.")

    # Build updated schedule message
    records = sheet.get_all_records()
    if records:
        records.sort(key=sort_key)
        message = "📋 *Updated Schedule:*\n"
        for row in records:
            message += f"{row['Date']} | {row['Time']} | {row['Name']}\n"
    else:
        message = "📋 No bookings left."

    announcement = (
        f"❌ {user.first_name} *CANCEL* the booking:\n"
        f"📅 {canceled_date} | ⏰ {canceled_time}\n\n"
        f"{message}"
    )

    try:
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=announcement, parse_mode="Markdown")
    except Exception as e:
        print(f"⚠️ Could not send group message: {e}")

    return ConversationHandler.END

# ----------------- End meeting -----------------
async def end_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/end")

    records = sheet.get_all_records()
    user_bookings = [
        (i + 2, row) for i, row in enumerate(records)
        if str(row.get("TelegramID")) == str(user.id)
    ]

    if not user_bookings:
        await update.message.reply_text("❌ You don’t have any active meetings to end.")
        return

    tz = pytz.timezone("Asia/Phnom_Penh")
    now = datetime.now(tz)

    active_meeting = None
    active_row_index = None

    for row_index, booking in user_bookings:
        date_str = booking["Date"]
        time_str = booking["Time"]
        start_str, end_str = [t.strip() for t in time_str.split("-")]

        try:
            start_dt = tz.localize(datetime.strptime(f"{date_str} {start_str}", "%d/%m/%Y %H:%M"))
            end_dt = tz.localize(datetime.strptime(f"{date_str} {end_str}", "%d/%m/%Y %H:%M"))
        except Exception as e:
            print(f"⚠️ Error parsing time: {e}")
            continue

        if start_dt <= now <= end_dt + timedelta(minutes=30):
            active_meeting = booking
            active_row_index = row_index
            break

    if not active_meeting:
        await update.message.reply_text(
            "⏰ It’s not meeting time now or your meeting ended too long ago.\n"
            "You can only end meetings during or within 30 minutes after the scheduled time."
        )
        return

    sheet.delete_rows(active_row_index)
    ended_date = active_meeting["Date"]
    ended_time = active_meeting["Time"]

    message = (
        f"🏁 *Meeting Ended!*\n"
        f"👤 {user.first_name}\n"
        f"📅 {ended_date} | ⏰ {ended_time}\n\n"
        f"✅ The meeting has officially ended."
    )

    try:
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=message, parse_mode="Markdown")
        await update.message.reply_text("✅ Meeting ended and announced to the group.")
        print(f"✅ Meeting ended for {user.first_name}: {ended_date} {ended_time}")
    except Exception as e:
        print(f"⚠️ Could not send group message: {e}")
        await update.message.reply_text("⚠️ Meeting ended but could not announce to group.")

# ----------------- Stats -----------------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        spreadsheet = client.open_by_url(SPREADSHEET_URL)
        stats_sheet = spreadsheet.worksheet("UserStats")
        records = stats_sheet.get_all_records()

        if not records:
            await update.message.reply_text("📊 No user activity data yet.")
            return

        summary = {}
        for row in records:
            name = row["Name"]
            action = row["Command"]
            last_time = row["DateTime"]

            if name not in summary:
                summary[name] = {
                    "total": 0,
                    "actions": {},
                    "last_action": last_time
                }

            summary[name]["total"] += 1
            summary[name]["last_action"] = last_time
            summary[name]["actions"][action] = summary[name]["actions"].get(action, 0) + 1

        def sort_key_stats(item):
            try:
                return datetime.strptime(item[1]["last_action"], "%d/%m/%Y %H:%M:%S")
            except:
                return datetime.min

        sorted_users = sorted(summary.items(), key=sort_key_stats, reverse=True)

        message = "📊 *All User Activity Summary:*\n\n"
        for name, info in sorted_users:
            actions_text = ", ".join([f"{cmd}({count})" for cmd, count in info["actions"].items()])
            message += (
                f"👤 *{name}*\n"
                f"🕒 Last: {info['last_action']}\n"
                f"📈 Total: {info['total']}\n"
                f"📝 Actions: {actions_text}\n\n"
            )

        await update.message.reply_text(message, parse_mode="Markdown")

    except Exception as e:
        print(f"⚠️ Error generating stats: {e}")
        await update.message.reply_text("⚠️ Could not retrieve stats.")

# ----------------- Announce (admin) -----------------
async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return ConversationHandler.END

    await update.message.reply_text("📝 Please type your announcement message:")
    return ANNOUNCE_MESSAGE

async def send_announcement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    message_text = update.message.text.strip()

    if user.id != ADMIN_ID:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return ConversationHandler.END

    if not message_text:
        await update.message.reply_text("⚠️ Empty message, please type something or /cancel.")
        return ANNOUNCE_MESSAGE

    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=f"📢 *Announcement:*\n\n{message_text}",
            parse_mode="Markdown"
        )
        await update.message.reply_text("✅ Announcement sent successfully!")
        print(f"✅ Admin sent announcement: {message_text}")
    except Exception as e:
        await update.message.reply_text("⚠️ Failed to send announcement.")
        print(f"⚠️ Announcement error: {e}")

    return ConversationHandler.END

# ----------------- Admin Upload Docs -----------------
async def upload_doc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("🚫 You are not authorized to upload documents.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📤 Please send the document file you want to upload (e.g., .docx, .pdf, .xlsx).",
        reply_markup=ReplyKeyboardRemove()
    )
    return UPLOAD_DOC

async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    document = update.message.document

    if not document:
        await update.message.reply_text("⚠️ Please send a valid document file.")
        return UPLOAD_DOC

    try:
        os.makedirs("docs", exist_ok=True)
        file_path = os.path.join("docs", document.file_name)
        file = await document.get_file()
        await file.download_to_drive(file_path)
        await update.message.reply_text(f"✅ File saved: {document.file_name}\nUsers can now access it with /docs.")
        print(f"✅ Admin uploaded {document.file_name} to docs/")
    except Exception as e:
        await update.message.reply_text("⚠️ Failed to save the file.")
        print(f"⚠️ Error saving file: {e}")

    return ConversationHandler.END

# ----------------- User Download Docs -----------------
async def docs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/docs")

    try:
        files = [f for f in os.listdir("docs") if f != ".keep"]
    except Exception:
        files = []

    if not files:
        await update.message.reply_text("📂 No documents available yet. Ask the admin to upload some.")
        return ConversationHandler.END

    keyboard = []
    row = []
    for i, f in enumerate(files, 1):
        row.append(f"📄 {f}")
        if i % 2 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("📁 Please choose a document to download:", reply_markup=reply_markup)
    return DOC_SELECT

async def send_selected_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip().replace("📄 ", "")
    file_path = os.path.join("docs", choice)

    if not os.path.exists(file_path):
        await update.message.reply_text("⚠️ I couldn’t find that file. Try /docs again.")
        return ConversationHandler.END

    try:
        with open(file_path, "rb") as f:
            await update.message.reply_document(
                document=InputFile(f, filename=choice),
                caption=f"📘 Here’s your document: {choice}"
            )
        print(f"✅ Sent {choice} to {update.message.from_user.first_name}")
    except Exception as e:
        await update.message.reply_text("⚠️ Failed to send the document.")
        print(f"⚠️ Error sending document: {e}")

    return ConversationHandler.END

# ----------------- Auto Cleanup -----------------
async def auto_cleanup(update: Update = None, context: ContextTypes.DEFAULT_TYPE = None):
    """
    Works both when called manually (update + context) and when called by JobQueue
    (first arg will be the context object).
    """
    # Normalize args: if called by JobQueue the first positional arg will be context
    if context is None and update is not None and not hasattr(update, "message"):
        context = update
        update = None

    tz = pytz.timezone("Asia/Phnom_Penh")
    now = datetime.now(tz)
    records = sheet.get_all_records()

    removed = []
    updated_records = []

    for row in records:
        try:
            date_str = row["Date"]
            time_str = row["Time"]
            name = row["Name"]

            start_time_str, end_time_str = time_str.split("-")
            meeting_end = datetime.strptime(f"{date_str} {end_time_str.strip()}", "%d/%m/%Y %H:%M")
            meeting_end = tz.localize(meeting_end)

            if meeting_end < now:
                removed.append(f"{date_str} | {time_str} | {name}")
            else:
                updated_records.append(row)
        except Exception as e:
            print(f"⚠️ Error parsing record: {e}")

    if removed:
        try:
            headers = ["Date", "Time", "Name", "TelegramID"]
            sheet.clear()

            new_data = [headers]
            for r in updated_records:
                new_data.append([
                    r.get("Date", ""),
                    r.get("Time", ""),
                    r.get("Name", ""),
                    r.get("TelegramID", "")
                ])

            sheet.update("A1", new_data)
            print("✅ Sheet successfully rewritten with updated records.")

            message = "🧹 *Expired Meetings Removed:*\n"
            for r in removed:
                message += f"• {r}\n"

            if updated_records:
                updated_records.sort(key=sort_key)
                message += "\n📋 *Updated Schedule:*\n"
                for row in updated_records:
                    message += f"{row['Date']} | {row['Time']} | {row['Name']}\n"
            else:
                message += "\n✅ No meetings left."

            if context:
                await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=message, parse_mode="Markdown")

            if update and getattr(update, "message", None):
                await update.message.reply_text("✅ Cleanup completed and group updated!")

        except Exception as e:
            print(f"⚠️ Error rewriting sheet: {e}")
            if update and getattr(update, "message", None):
                await update.message.reply_text("⚠️ Cleanup failed due to a sheet update error.")
            elif context:
                await context.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text="⚠️ Cleanup failed due to a sheet update error.",
                    parse_mode="Markdown"
                )
    else:
        print("✅ No expired meetings found during cleanup.")
        if update and getattr(update, "message", None):
            await update.message.reply_text("✨ There are no expired bookings to clean up.")

# ----------------- Webhook utils & admin notify -----------------
async def clear_webhook(bot_token):
    """Ensure the bot is in polling mode (not webhook)."""
    bot = Bot(bot_token)
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Webhook cleared successfully!")

async def notify_admin(bot, message: str):
    """Send a notification message to the admin."""
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ [Bot Alert]\n\n{message}")
        print(f"✅ Sent alert to admin: {message}")
    except Exception as e:
        print(f"⚠️ Failed to notify admin: {e}")

# ----------------- Generic conversation cancel fallback -----------------
async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("↩️ Conversation cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ===================== MAIN =====================
def main():
    
    request = HTTPXRequest(connect_timeout=30.0, read_timeout=120.0)
    app = ApplicationBuilder().token(TOKEN).request(request).build()

    # Initialize job queue if needed
    job_queue = getattr(app, "job_queue", None)
    if not job_queue:
        try:
            job_queue = JobQueue()
            job_queue.set_application(app)
            job_queue.start()
            print("✅ Job queue manually initialized.")
        except Exception as e:
            print(f"⚠️ Could not initialize job queue: {e}")

    # Define commands
    user_commands = [
        BotCommand("start", "Start"),
        BotCommand("book", "Book room"),
        BotCommand("cancel", "Cancel booking"),
        BotCommand("end", "End the meeting"),
        BotCommand("docs", "Download documents"),
    ]

    admin_commands = user_commands + [
        BotCommand("announce", "Announcement"),
        BotCommand("stats", "Statistics"),
        BotCommand("clean", "Clean up expired"),
        BotCommand("uploaddoc", "Upload file"),
    ]

    # Set commands with proper scopes
    async def set_commands(application):
        await application.bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
        await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(ADMIN_ID))
        print("✅ Command menus set for users and admin.")

        # Clear webhook safely
        await clear_webhook(TOKEN)

    app.post_init = set_commands

    # Conversations
    fallback_list = [CommandHandler("cancel", conv_cancel)]

    book_conv = ConversationHandler(
        entry_points=[CommandHandler("book", book)],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)],
        },
        fallbacks=fallback_list,
        per_chat=True,
        per_user=True,
    )

    cancel_conv = ConversationHandler(
        entry_points=[CommandHandler("cancel", cancel)],
        states={
            CANCEL_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_booking_by_number)],
        },
        fallbacks=fallback_list,
        per_chat=True,
        per_user=True,
    )

    announce_conv = ConversationHandler(
        entry_points=[CommandHandler("announce", announce)],
        states={
            ANNOUNCE_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_announcement)],
        },
        fallbacks=fallback_list,
        per_user=True,
        per_chat=True,
    )

    upload_conv = ConversationHandler(
        entry_points=[CommandHandler("uploaddoc", upload_doc_start)],
        states={
            UPLOAD_DOC: [MessageHandler(filters.Document.ALL, receive_document)],
        },
        fallbacks=fallback_list,
        per_user=True,
        per_chat=True,
    )

    docs_conv = ConversationHandler(
        entry_points=[CommandHandler("docs", docs_menu)],
        states={
            DOC_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_selected_doc)],
        },
        fallbacks=fallback_list,
        per_user=True,
        per_chat=True,
    )

    # Register handlers
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(book_conv)
    app.add_handler(cancel_conv)
    app.add_handler(CommandHandler("end", end_meeting))
    app.add_handler(announce_conv)
    app.add_handler(CommandHandler("clean", auto_cleanup))
    app.add_handler(upload_conv)
    app.add_handler(docs_conv)

    # Schedule auto cleanup every hour
    job_queue.run_repeating(auto_cleanup, interval=3600, first=10)
    print("🕒 Auto-cleanup scheduled every 1 hour.")
    print("✅ Meeting Room Bot is running...")

    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ BOT ERROR: {e}")
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            bot = Bot(token=TOKEN)
            loop.run_until_complete(
                notify_admin(bot, f"⚠️ [Bot Alert]\n\nBot stopped or crashed.\nError: {e}")
            )
        except Exception as inner_e:
            print(f"⚠️ Failed to send crash alert: {inner_e}")


