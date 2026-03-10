import asyncio
import calendar
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import dateparser
import gspread
import pytz
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    JobQueue,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# Load environment variables from .env file
load_dotenv()

# ===================== CONFIG =====================
TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_URL = os.getenv("SPREADSHEET_URL")

# Validate environment variables
if not os.getenv("GROUP_CHAT_ID"):
    raise ValueError("❌ GROUP_CHAT_ID environment variable is not set!")
if not os.getenv("ADMIN_ID"):
    raise ValueError("❌ ADMIN_ID environment variable is not set!")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable is not set!")
if not SPREADSHEET_URL:
    raise ValueError("❌ SPREADSHEET_URL environment variable is not set!")

GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))
ADMIN_ID = int(os.getenv("ADMIN_ID"))

# Conversation states
SELECT_MONTH, SELECT_DAY, TIME, CANCEL_SELECT = range(4)
ANNOUNCE_MESSAGE = 200

# State for docs upload
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
    stats_sheet = spreadsheet.add_worksheet(
        title="UserStats", rows="1000", cols="4")
    stats_sheet.append_row(["TelegramID", "Name", "Command", "DateTime"])

# ===================== HELPERS =====================


def sort_key(row):
    """Reusable sort key: parse Date and start Time; fallback to max values."""
    try:
        date_obj = datetime.strptime(row["Date"], "%d/%m/%Y")
        time_start = row["Time"].split(
            "-")[0] if "-" in row["Time"] else row["Time"]
        time_obj = datetime.strptime(time_start.strip(), "%H:%M")
        return (date_obj, time_obj)
    except Exception:
        return (datetime.max, datetime.max)


def log_user_action(user, command):
    """Log each user command to the 'UserStats' sheet (Phnom Penh time)."""
    try:
        now = datetime.now(ZoneInfo("Asia/Phnom_Penh"))
        now_str = now.strftime("%d/%m/%Y %H:%M:%S")
        stats_sheet.append_row(
            [str(user.id), user.first_name, command, now_str])
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

    # Get admin info
    try:
        admin = await context.bot.get_chat(ADMIN_ID)
        admin_name = admin.first_name
        admin_username = f"@{admin.username}" if admin.username else admin_name
    except:
        admin_username = "the admin"

    await update.message.reply_text(
        "👋 Welcome to the Meeting Room Bot!\n\n"
        "Commands:\n"
        "/book - Book the meeting room\n"
        "/cancel - Cancel your booking\n"
        "/end - End the active meeting\n"
        "/docs - Download available documents\n\n"
        f"ℹ️ Created by {admin_username}"
    )


async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/book")
    tz = ZoneInfo("Asia/Phnom_Penh")
    now_pp = datetime.now(tz)
    keyboard = _build_month_keyboard(now_pp)

    await update.message.reply_text(
        "📅 Choose a month to book:",
        reply_markup=keyboard,
    )
    return SELECT_MONTH


async def handle_month_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    tz = ZoneInfo("Asia/Phnom_Penh")

    if data == "month:choose":
        now_pp = datetime.now(tz)
        await query.edit_message_text(
            "📅 Choose a month to book:",
            reply_markup=_build_month_keyboard(now_pp),
        )
        return SELECT_MONTH

    if not data.startswith("month:"):
        return SELECT_MONTH

    try:
        year_month = data.split(":", 1)[1]
        year, month = map(int, year_month.split("-"))
    except Exception:
        await query.edit_message_text("⚠️ Could not read that month. Please choose again.")
        return SELECT_MONTH

    day_keyboard = _build_day_keyboard(year, month, tz)

    # If no days are available (e.g., all past), show month picker again
    if len(day_keyboard.inline_keyboard) <= 1:  # only the back button exists
        now_pp = datetime.now(tz)
        await query.edit_message_text(
            "⚠️ No future days left in that month. Pick another month:",
            reply_markup=_build_month_keyboard(now_pp),
        )
        return SELECT_MONTH

    await query.edit_message_text(
        f"📅 {datetime(year, month, 1).strftime('%B %Y')}\nChoose a day:",
        reply_markup=day_keyboard,
    )
    return SELECT_DAY


async def handle_day_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("day:"):
        return SELECT_DAY

    try:
        _, date_str = data.split(":", 1)
        year, month, day = map(int, date_str.split("-"))
    except Exception:
        await query.edit_message_text("⚠️ Could not read that day. Please choose again.")
        return SELECT_DAY

    selected_date = datetime(year, month, day)
    context.user_data["date"] = selected_date.strftime("%d/%m/%Y")

    await query.edit_message_text(
        f"📅 Selected: {context.user_data['date']}\n⏰ Now enter the time range (e.g. 14:00-15:00):"
    )
    return TIME

# ----------------- Get Date -----------------


def _first_day_of_month(dt: datetime, add_months: int = 0) -> datetime:
    """Return the first day of the month offset by add_months."""
    year = dt.year + (dt.month - 1 + add_months) // 12
    month = (dt.month - 1 + add_months) % 12 + 1
    return datetime(year, month, 1, tzinfo=dt.tzinfo)


def _build_month_keyboard(now_pp: datetime) -> InlineKeyboardMarkup:
    """Show current and next month as inline buttons."""
    months = []
    for offset in (0, 1):
        month_dt = _first_day_of_month(now_pp, offset)
        label = month_dt.strftime("%B %Y")
        months.append(InlineKeyboardButton(label, callback_data=f"month:{month_dt.strftime('%Y-%m')}"))

    keyboard = [months]
    return InlineKeyboardMarkup(keyboard)


def _build_day_keyboard(year: int, month: int, tz: ZoneInfo) -> InlineKeyboardMarkup:
    """Inline keyboard for available days; skips past days of current month."""
    today = datetime.now(tz).date()
    _, last_day = calendar.monthrange(year, month)

    rows = []
    row = []
    for day in range(1, last_day + 1):
        date_obj = datetime(year, month, day, tzinfo=tz).date()
        if date_obj < today:
            continue
        row.append(InlineKeyboardButton(str(day), callback_data=f"day:{year}-{month:02d}-{day:02d}"))
        if len(row) == 7:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("🔙 Choose month", callback_data="month:choose")])
    return InlineKeyboardMarkup(rows)

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
            start_dt = tz.localize(datetime.strptime(
                f"{date_str} {start_str}", "%d/%m/%Y %H:%M"))
            end_dt = tz.localize(datetime.strptime(
                f"{date_str} {end_str}", "%d/%m/%Y %H:%M"))
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
        print(
            f"✅ Meeting ended for {user.first_name}: {ended_date} {ended_time}")
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
            summary[name]["actions"][action] = summary[name]["actions"].get(
                action, 0) + 1

        def sort_key_stats(item):
            try:
                return datetime.strptime(item[1]["last_action"], "%d/%m/%Y %H:%M:%S")
            except:
                return datetime.min

        sorted_users = sorted(
            summary.items(), key=sort_key_stats, reverse=True)

        message = "📊 *All User Activity Summary:*\n\n"
        for name, info in sorted_users:
            actions_text = ", ".join(
                [f"{cmd}({count})" for cmd, count in info["actions"].items()])
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

# ----------------- User Download Docs (Inline Keyboard) -----------------


async def docs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/docs")

    try:
        files = [f for f in os.listdir("docs") if f != ".keep"]
    except Exception:
        files = []

    if not files:
        await update.message.reply_text("📂 No documents available yet. Ask the admin to upload some.")
        return

    keyboard = [
        [InlineKeyboardButton(f"📄 {f}", callback_data=f"docs:{f}")]
        for f in files
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📁 Please choose a document to download:", reply_markup=reply_markup)


async def handle_docs_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("docs:"):
        return

    filename = data.split("docs:", 1)[1]
    file_path = os.path.join("docs", filename)

    if not os.path.exists(file_path):
        await query.message.reply_text("⚠️ I couldn’t find that file. Try /docs again.")
        return

    try:
        with open(file_path, "rb") as f:
            await query.message.reply_document(
                document=InputFile(f, filename=filename),
                caption=f"📘 Here’s your document: {filename}"
            )
        print(f"✅ Sent {filename} to {query.from_user.first_name}")
    except Exception as e:
        await query.message.reply_text("⚠️ Failed to send the document.")
        print(f"⚠️ Error sending document: {e}")

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
            meeting_end = datetime.strptime(
                f"{date_str} {end_time_str.strip()}", "%d/%m/%Y %H:%M")
            meeting_end = tz.localize(meeting_end)

            if meeting_end < now:
                removed.append(f"{date_str} | {time_str}")
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

            sheet.update(new_data, "A1")
            print("✅ Sheet successfully rewritten with updated records.")

            message = "🧹 *Expired Schedule:*\n"
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

# ----------------- Welcome new members -----------------


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome new members who join the group."""
    for new_member in update.message.new_chat_members:
        welcome_msg = (
            f"👋 Welcome to the group, {new_member.first_name}!\n\n"
            f"This is Meeting Room Booking Info.\n\n"
            f"Use /start to see available commands!"
        )
        try:
            await update.message.reply_text(welcome_msg)
            print(f"✅ Welcomed new member: {new_member.first_name}")
        except Exception as e:
            print(f"⚠️ Could not send welcome message: {e}")

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
            SELECT_MONTH: [CallbackQueryHandler(handle_month_selection, pattern="^month:")],
            SELECT_DAY: [
                CallbackQueryHandler(handle_day_selection, pattern="^day:"),
                CallbackQueryHandler(handle_month_selection, pattern="^month:"),
            ],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)],
        },
        fallbacks=fallback_list,
        per_chat=True,
        per_user=True,
        per_message=True,
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

    # Register handlers
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(book_conv)
    app.add_handler(cancel_conv)
    app.add_handler(CommandHandler("end", end_meeting))
    app.add_handler(announce_conv)
    app.add_handler(CommandHandler("clean", auto_cleanup))
    app.add_handler(upload_conv)
    app.add_handler(CommandHandler("docs", docs_menu))
    app.add_handler(CallbackQueryHandler(handle_docs_button, pattern="^docs:"))
    app.add_handler(MessageHandler(
        filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))

    # Schedule auto cleanup every hour
    job_queue.run_repeating(auto_cleanup, interval=3600, first=10)
    print("🕒 Auto-cleanup scheduled every 1 hour.")

    use_webhook = os.getenv("USE_WEBHOOK", "false").lower() == "true"
    if use_webhook:
        webhook_url = os.getenv("WEBHOOK_URL")
        webapp_host = os.getenv("WEBAPP_HOST", "0.0.0.0")
        # Railway provides PORT, fallback to WEBAPP_PORT for other platforms
        webapp_port = int(os.getenv("PORT")
                          or os.getenv("WEBAPP_PORT", "8080"))
        secret_token = os.getenv("WEBHOOK_SECRET_TOKEN")

        if not webhook_url:
            raise RuntimeError(
                "USE_WEBHOOK=true but WEBHOOK_URL is not set in .env")

        print(
            f"✅ Starting webhook at {webapp_host}:{webapp_port} -> {webhook_url}")

        app.run_webhook(
            listen=webapp_host,
            port=webapp_port,
            webhook_url=webhook_url,
            secret_token=secret_token,
        )
    else:
        print("✅ Meeting Room Bot is running (polling)...")
        try:
            # Drop any pending updates to avoid conflicts from previous runs
            app.run_polling(drop_pending_updates=True)
        except Exception as e:
            # Handle duplicate polling conflicts gracefully
            if "terminated by other getUpdates request" in str(e):
                print(
                    "⚠️ Conflict: Another bot instance is polling. Please stop other running processes and run a single instance.")
                print(
                    "Hint: In PowerShell, run: Get-Process python* | Stop-Process -Force")
            raise


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
                notify_admin(
                    bot, f"⚠️ [Bot Alert]\n\nBot stopped or crashed.\nError: {e}")
            )
        except Exception as inner_e:
            print(f"⚠️ Failed to send crash alert: {inner_e}")
