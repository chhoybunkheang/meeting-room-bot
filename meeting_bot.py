# meeting_bot_clean.py
"""
Clean single-file Meeting Room Bot
- All group announcements (book / cancel / cleanup) list schedule old -> new (earliest first)
- Features: book, cancel, end, schedule (sort), stats, announce, auto-cleanup, upload/download docs
- Timezone: Asia/Phnom_Penh
- Auto-cleanup: every 1 hour
- Uses Google Sheets for persistence (spreadsheet URL must be accessible by service account)
"""

import os
import json
import re
import asyncio
from datetime import datetime, timedelta
import pytz
from zoneinfo import ZoneInfo

import gspread
import dateparser
from google.oauth2.service_account import Credentials

from telegram import Bot, Update, BotCommand, InputFile, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
    JobQueue,
)

# ===================== CONFIG =====================
TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1vvBRrL-qXx0jp5-ZRR4xVpOi5ejxE8DtxrHOrel7F78"
GROUP_CHAT_ID = -1003073406158  # update if needed
ADMIN_ID = 171208804  # update with your Telegram ID

# Conversation states
DATE, TIME, CANCEL_SELECT = range(3)
ANNOUNCE_MESSAGE = range(1)
DOC_SELECT = 100
UPLOAD_DOC = 101

# Timezone constant
TZ = "Asia/Phnom_Penh"

# Auto-cleanup interval (seconds) — 1 hour
AUTO_CLEAN_INTERVAL = 3600

# ===================== PRECHECKS =====================
if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")
if not GOOGLE_CREDENTIALS:
    raise RuntimeError("Missing GOOGLE_CREDENTIALS environment variable.")

# ===================== GOOGLE SHEETS SETUP =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds_json = json.loads(GOOGLE_CREDENTIALS)
creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
client = gspread.authorize(creds)
sheet = client.open_by_url(SPREADSHEET_URL).sheet1
spreadsheet = client.open_by_url(SPREADSHEET_URL)

try:
    stats_sheet = spreadsheet.worksheet("UserStats")
except gspread.exceptions.WorksheetNotFound:
    stats_sheet = spreadsheet.add_worksheet(title="UserStats", rows="1000", cols="4")
    stats_sheet.append_row(["TelegramID", "Name", "Command", "DateTime"])

# ===================== FILES FOLDER =====================
os.makedirs("docs", exist_ok=True)
if not os.listdir("docs"):
    open("docs/.keep", "w").close()
print("✅ 'docs' folder ready (auto-created if missing).")

# ===================== HELPERS =====================
def now_phnom_penh():
    return datetime.now(ZoneInfo(TZ))

def log_user_action(user, command):
    """Append user action to UserStats (Phnom Penh time)."""
    try:
        now = now_phnom_penh()
        now_str = now.strftime("%d/%m/%Y %H:%M:%S")
        stats_sheet.append_row([str(user.id), user.first_name, command, now_str])
        print(f"✅ Logged {command} by {user.first_name} at {now_str}")
    except Exception as e:
        print(f"⚠️ Could not log action: {e}")

def time_to_minutes(time_str):
    h, m = map(int, time_str.split(":"))
    return h * 60 + m

def is_overlapping(existing_start, existing_end, new_start, new_end):
    return not (new_end <= existing_start or new_start >= existing_end)

def sort_records_old_to_new(records):
    """Return a new list sorted by date then start time (earliest first)."""
    def sort_key(row):
        try:
            date_obj = datetime.strptime(row["Date"], "%d/%m/%Y")
            time_start = row["Time"].split("-")[0].strip() if "-" in row["Time"] else row["Time"].strip()
            time_obj = datetime.strptime(time_start, "%H:%M")
            return (date_obj, time_obj)
        except Exception:
            return (datetime.max, datetime.max)
    try:
        return sorted(records, key=sort_key)
    except Exception:
        return records

def save_booking(date_str, time_str, name, telegram_id):
    """Save booking if no time overlap for same date."""
    try:
        new_start_str, new_end_str = time_str.split("-")
        new_start = time_to_minutes(new_start_str.strip())
        new_end = time_to_minutes(new_end_str.strip())
    except Exception:
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
        "/schedule - Show sorted booking schedule\n"
        "/cancel - Cancel your booking\n"
        "/end - End the active meeting\n"
        "/docs - Download available documents\n"
        "\n(Admin) /announce /stats /clean /uploaddoc"
    )

# ---------- BOOK ----------
async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/book")
    await update.message.reply_text("📅 Please enter the date (e.g. 30/10/2025 or 30/10):")
    return DATE

async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_input = update.message.text.strip()
    # Validate basic dd/mm or dd/mm/yyyy
    if not re.match(r"^\d{1,2}/\d{1,2}(/?\d{2,4})?$", date_input):
        await update.message.reply_text("❌ Please enter date in format DD/MM or DD/MM/YYYY.")
        return DATE

    if len(date_input.split("/")) == 2:
        date_input = f"{date_input}/{datetime.now().year}"

    date_obj = dateparser.parse(date_input, settings={"DATE_ORDER": "DMY"})
    if not date_obj:
        await update.message.reply_text("❌ Invalid date. Try again (example: 25/10 or 25/10/2025).")
        return DATE

    if date_obj.date() < datetime.now().date():
        await update.message.reply_text("⚠️ The date you entered is in the past. Please choose a future date.")
        return DATE

    context.user_data["date"] = date_obj.strftime("%d/%m/%Y")
    await update.message.reply_text("⏰ Great! Now enter the time range (e.g. 14:00-15:00):")
    return TIME

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

    # success
    await update.message.reply_text(f"✅ Booking confirmed for {date_str} at {time_input}.")

    # Announce to group (old -> new)
    try:
        records = sheet.get_all_records()
        records_sorted = sort_records_old_to_new(records)
        message = (
            f"📢 *New Booking Added!*\n\n"
            f"👤 {user.first_name}\n"
            f"🗓 {date_str} | ⏰ {time_input}\n\n"
            f"📋 *Current Schedule (old → new):*\n"
        )
        for row in records_sorted:
            message += f"{row.get('Date','')} | {row.get('Time','')} | {row.get('Name','')}\n"

        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=message, parse_mode="Markdown")
    except Exception as e:
        print(f"⚠️ Could not send group message: {e}")

    return ConversationHandler.END

# ---------- SHOW / SCHEDULE ----------
async def show_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/schedule")
    records = sheet.get_all_records()
    if not records:
        await update.message.reply_text("📋 No bookings yet.")
        return

    records_sorted = sort_records_old_to_new(records)
    message = "📋 *Current Schedule (old → new):*\n\n"
    for row in records_sorted:
        message += f"{row.get('Date','')} | {row.get('Time','')} | {row.get('Name','')}\n"
    await update.message.reply_text(message, parse_mode="Markdown")

# ---------- CANCEL ----------
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

    # Build updated schedule (old->new)
    records = sheet.get_all_records()
    if records:
        records_sorted = sort_records_old_to_new(records)
        message = "📋 *Updated Schedule:*\n"
        for row in records_sorted:
            message += f"{row.get('Date','')} | {row.get('Time','')} | {row.get('Name','')}\n"
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

# ---------- END MEETING ----------
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

    tz = pytz.timezone(TZ)
    now = datetime.now(tz)
    active_meeting = None
    active_row_index = None

    for row_index, booking in user_bookings:
        date_str = booking.get("Date", "")
        time_str = booking.get("Time", "")
        try:
            start_str, end_str = [t.strip() for t in time_str.split("-")]
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

# ---------- STATS ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return

    try:
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
                summary[name] = {"total": 0, "actions": {}, "last_action": last_time}
            summary[name]["total"] += 1
            summary[name]["last_action"] = last_time
            summary[name]["actions"][action] = summary[name]["actions"].get(action, 0) + 1

        def sort_key(item):
            try:
                return datetime.strptime(item[1]["last_action"], "%d/%m/%Y %H:%M:%S")
            except:
                return datetime.min

        sorted_users = sorted(summary.items(), key=sort_key, reverse=True)

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

# ---------- ANNOUNCE ----------
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
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=f"📢 *Announcement:*\n\n{message_text}", parse_mode="Markdown")
        await update.message.reply_text("✅ Announcement sent successfully!")
        print(f"✅ Admin sent announcement: {message_text}")
    except Exception as e:
        await update.message.reply_text("⚠️ Failed to send announcement.")
        print(f"⚠️ Announcement error: {e}")

    return ConversationHandler.END

# ---------- AUTO CLEANUP ----------
async def auto_cleanup(update: Update = None, context: ContextTypes.DEFAULT_TYPE = None):
    """
    Removes expired meetings. If called manually (via /clean), will reply to the user.
    Always announces updated schedule to group (old -> new) if any removed.
    """
    now = datetime.now(pytz.timezone(TZ))
    records = sheet.get_all_records()

    removed = []
    updated_records = []

    for row in records:
        try:
            date_str = row.get("Date", "")
            time_str = row.get("Time", "")
            name = row.get("Name", "")

            start_time_str, end_time_str = time_str.split("-")
            meeting_end = datetime.strptime(f"{date_str} {end_time_str.strip()}", "%d/%m/%Y %H:%M")
            meeting_end = pytz.timezone(TZ).localize(meeting_end)

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
                new_data.append([r.get("Date", ""), r.get("Time", ""), r.get("Name", ""), r.get("TelegramID", "")])
            sheet.update("A1", new_data)
            print("✅ Sheet successfully rewritten with updated records.")

            # Build message (old -> new)
            records_sorted = sort_records_old_to_new(updated_records)
            message = "🧹 *Expired Meetings Removed:*\n"
            for r in removed:
                message += f"• {r}\n"

            if records_sorted:
                message += "\n📋 *Updated Schedule (old → new):*\n"
                for row in records_sorted:
                    message += f"{row.get('Date','')} | {row.get('Time','')} | {row.get('Name','')}\n"
            else:
                message += "\n✅ No meetings left."

            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=message, parse_mode="Markdown")

            if update and update.message:
                await update.message.reply_text("✅ Cleanup completed and group updated!")

        except Exception as e:
            print(f"⚠️ Error rewriting sheet: {e}")
            if update and update.message:
                await update.message.reply_text("⚠️ Cleanup failed due to a sheet update error.")
            else:
                try:
                    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text="⚠️ Cleanup failed due to a sheet update error.", parse_mode="Markdown")
                except Exception:
                    pass
    else:
        print("✅ No expired meetings found during cleanup.")
        if update and update.message:
            await update.message.reply_text("✨ There are no expired bookings to clean up.")

# ---------- CLEAR WEBHOOK ----------
async def clear_webhook(bot_token):
    bot = Bot(bot_token)
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Webhook cleared successfully!")

# ---------- NOTIFY ADMIN ----------
async def notify_admin(bot, message: str):
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ [Bot Alert]\n\n{message}")
        print(f"✅ Sent alert to admin: {message}")
    except Exception as e:
        print(f"⚠️ Failed to notify admin: {e}")

# ---------- DOCS: UPLOAD & DOWNLOAD ----------
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

    # Optional: file size limit (10 MB)
    try:
        if getattr(document, "file_size", None) and document.file_size > 10 * 1024 * 1024:
            await update.message.reply_text("⚠️ File too large (max 10 MB).")
            return UPLOAD_DOC
    except Exception:
        pass

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
            await update.message.reply_document(document=InputFile(f, filename=choice), caption=f"📘 Here’s your document: {choice}")
        print(f"✅ Sent {choice} to {update.message.from_user.first_name}")
    except Exception as e:
        await update.message.reply_text("⚠️ Failed to send the document.")
        print(f"⚠️ Error sending document: {e}")
    return ConversationHandler.END

# ===================== MAIN =====================
def main():
    request = HTTPXRequest(connect_timeout=15.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TOKEN).request(request).build()

    # Initialize job queue safely (works across versions)
    job_queue = getattr(app, "job_queue", None)
    if not job_queue:
        try:
            job_queue = JobQueue()
            job_queue.set_application(app)
            job_queue.start()
            print("✅ Job queue manually initialized.")
        except Exception as e:
            print(f"⚠️ Could not initialize job queue: {e}")

    # Bot commands
    user_commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("book", "Book the room"),
        BotCommand("schedule", "Show sorted booking schedule"),
        BotCommand("cancel", "Cancel booking"),
        BotCommand("end", "End the active meeting"),
        BotCommand("docs", "Download available documents"),
    ]
    admin_commands = user_commands + [
        BotCommand("announce", "Send announcement to group"),
        BotCommand("stats", "View all user activity"),
        BotCommand("clean", "Clean up expired bookings"),
        BotCommand("uploaddoc", "Upload file"),
    ]

    async def set_commands(application):
        await application.bot.set_my_commands(user_commands, scope={"type": "default"})
        await application.bot.set_my_commands(admin_commands, scope={"type": "chat", "chat_id": ADMIN_ID})
        print("✅ Command menus set for users and admin.")
        await clear_webhook(TOKEN)

    app.post_init = set_commands

    # Conversation handlers
    book_conv = ConversationHandler(
        entry_points=[CommandHandler("book", book)],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)],
        },
        fallbacks=[],
        per_chat=True,
        per_user=True,
    )

    cancel_conv = ConversationHandler(
        entry_points=[CommandHandler("cancel", cancel)],
        states={
            CANCEL_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_booking_by_number)],
        },
        fallbacks=[],
        per_chat=True,
        per_user=True,
    )

    announce_conv = ConversationHandler(
        entry_points=[CommandHandler("announce", announce)],
        states={
            ANNOUNCE_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_announcement)],
        },
        fallbacks=[],
        per_user=True,
        per_chat=True,
    )

    upload_conv = ConversationHandler(
        entry_points=[CommandHandler("uploaddoc", upload_doc_start)],
        states={
            UPLOAD_DOC: [MessageHandler(filters.Document.ALL, receive_document)],
        },
        fallbacks=[],
        per_user=True,
        per_chat=True,
    )

    docs_conv = ConversationHandler(
        entry_points=[CommandHandler("docs", docs_menu)],
        states={
            DOC_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_selected_doc)],
        },
        fallbacks=[],
        per_user=True,
        per_chat=True,
    )

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(book_conv)
    app.add_handler(cancel_conv)
    app.add_handler(CommandHandler("schedule", show_schedule))
    app.add_handler(CommandHandler("end", end_meeting))
    app.add_handler(announce_conv)
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("clean", auto_cleanup))
    app.add_handler(upload_conv)
    app.add_handler(docs_conv)

    # Schedule auto cleanup job
    try:
        if job_queue:
            job_queue.run_repeating(auto_cleanup, interval=AUTO_CLEAN_INTERVAL, first=10)
            print(f"🕒 Auto-cleanup scheduled every {AUTO_CLEAN_INTERVAL / 3600:.1f} hours.")
    except Exception as e:
        print(f"⚠️ Failed to schedule auto-cleanup: {e}")

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
            loop.run_until_complete(notify_admin(bot, f"⚠️ [Bot Alert]\n\nBot stopped or crashed.\nError: {e}"))
        except Exception as inner_e:
            print(f"⚠️ Failed to send crash alert: {inner_e}")
