import os
import json
import gspread
import dateparser
import asyncio
import pytz
from datetime import datetime
from google.oauth2.service_account import Credentials
from telegram import Bot, Update
from zoneinfo import ZoneInfo
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from telegram.request import HTTPXRequest
from telegram import BotCommand

# ===================== CONFIG =====================
TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1vvBRrL-qXx0jp5-ZRR4xVpOi5ejxE8DtxrHOrel7F78"
GROUP_CHAT_ID = -1003073406158  
DATE, TIME, CANCEL_SELECT = range(3)

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

# ===================== HELPER FUNCTIONS =====================
#====================== Statistics ==============================

def log_user_action(user, command):
    """Log each user command to the 'UserStats' sheet (Phnom Penh time)."""
    try:
        now = datetime.now(ZoneInfo("Asia/Phnom_Penh"))
        now_str = now.strftime("%d/%m/%Y %H:%M:%S")
        stats_sheet.append_row([str(user.id), user.first_name, command, now_str])
        print(f"✅ Logged {command} by {user.first_name} at {now_str}")
    except Exception as e:
        print(f"⚠️ Could not log action: {e}")

        
def is_slot_taken(date_str, time_str):
    records = sheet.get_all_records()
    for row in records:
        if row["Date"] == date_str and row["Time"] == time_str:
            return True
    return False

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
        "/show - Show all bookings\n"
        "/available - Check booked times\n"
        "/cancel - Cancel your booking"
    )

async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/book")
    await update.message.reply_text("📅 Please enter the date (e.g. 25/10/2025 or 25/10):")
    return DATE

async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_input = update.message.text.strip()

    # If user didn’t include a year, add current year
    if len(date_input.split("/")) == 2:
        current_year = datetime.now().year
        date_input = f"{date_input}/{current_year}"

    # ✅ Force day/month/year format
    date_obj = dateparser.parse(date_input, settings={"DATE_ORDER": "DMY"})

    if not date_obj:
        await update.message.reply_text("❌ Invalid date format. Try again (e.g. 25/10/2025 or 25/10).")
        return DATE

    # ✅ Save consistent dd/mm/yyyy format
    context.user_data["date"] = date_obj.strftime("%d/%m/%Y")
    await update.message.reply_text("⏰ Now enter the time range (e.g. 14:00-15:00):")
    return TIME


# ✅ When user books — announce to group
async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_input = update.message.text
    user = update.message.from_user
    date_str = context.user_data["date"]

    result = save_booking(date_str, time_input, user.first_name, user.id)

    if result == "invalid":
        await update.message.reply_text(
            "❌ Invalid time format.\nPlease use HH:MM-HH:MM (e.g. 14:00-15:00):"
        )
        return TIME  # 🔁 Ask again (don’t end conversation)

    elif result == "overlap":
        await update.message.reply_text(
            "⚠️ That time overlaps with another booking.\nPlease choose a different time range:"
        )
        return TIME  # 🔁 Ask again

    elif result == "success":
        await update.message.reply_text(
            f"✅ Booking confirmed for {date_str} at {time_input}."
        )

        # --- Send announcement to group ---
        try:
            records = sheet.get_all_records()
            message = (
                f"📢 *New Booking Added!*\n\n"
                f"👤 {user.first_name}\n"
                f"🗓 {date_str} | ⏰ {time_input}\n\n"
                f"📋 *Current Schedule:*\n"
            )
            for row in records:
                message += f"{row['Date']} | {row['Time']} | {row['Name']}\n"

            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=message,
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"⚠️ Could not send group message: {e}")

    return ConversationHandler.END

async def show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/show")
    records = sheet.get_all_records()

    if not records:
        await update.message.reply_text("📋 No bookings yet.")
        return

    # Sort by date + time
    def sort_key(row):
        try:
            date_obj = datetime.strptime(row["Date"], "%d/%m/%Y")
            time_start = row["Time"].split("-")[0] if "-" in row["Time"] else row["Time"]
            time_obj = datetime.strptime(time_start, "%H:%M")
            return (date_obj, time_obj)
        except Exception:
            return (datetime.max, datetime.max)

    records.sort(key=sort_key)

    # Build schedule message
    message = "📋 *Current Schedule (old → new):*\n\n"
    for row in records:
        message += f"{row['Date']} | {row['Time']} | {row['Name']}\n"

    await update.message.reply_text(message, parse_mode="Markdown")

async def available(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/available")
    records = sheet.get_all_records()
    if not records:
        await update.message.reply_text("✅ All time slots are available.")
        return
    booked = [f"{r['Date']} {r['Time']}" for r in records]
    await update.message.reply_text("📅 Booked slots:\n" + "\n".join(booked))

# ✅ Cancel by number (private only)
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/cancel")
    user = update.message.from_user
    records = sheet.get_all_records()

    # Find all bookings by this user
    user_bookings = [
        (i + 2, row) for i, row in enumerate(records)
        if str(row.get("TelegramID")) == str(user.id)
    ]

    if not user_bookings:
        await update.message.reply_text("❌ You don’t have any bookings to cancel.")
        return ConversationHandler.END

    # Show list of bookings to the user
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
        return TIME

    if not (1 <= choice <= len(user_bookings)):
        await update.message.reply_text("❌ Invalid choice. Try again.")
        return TIME

    # Find and delete the selected booking
    row_index, booking = user_bookings[choice - 1]
    canceled_date = booking["Date"]
    canceled_time = booking["Time"]
    sheet.delete_rows(row_index)

    # Confirm to user
    await update.message.reply_text(
        f"✅ Canceled booking on {canceled_date} at {canceled_time}."
    )

    # Get updated list of bookings
    records = sheet.get_all_records()

    if records:
        message = "📋 *Updated Schedule:*\n"
        for row in records:
            message += f"{row['Date']} | {row['Time']} | {row['Name']}\n"
    else:
        message = "📋 No bookings left."

    # Create group announcement
    announcement = (
        f"❌ {user.first_name} *CANCEL* the booking:\n"
        f"📅 {canceled_date} | ⏰ {canceled_time}\n\n"
        f"{message}"
    )

    # Send to group
    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=announcement,
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"⚠️ Could not send group message: {e}")

    return ConversationHandler.END

ADMIN_ID = 171208804  # Replace with your Telegram ID

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show summary of all users' actions."""

    # ✅ Only allow admin
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return

    try:
        spreadsheet = client.open_by_url(SPREADSHEET_URL)
        stats_sheet = spreadsheet.worksheet("UserStats")
        records = stats_sheet.get_all_records()

        if not records:
            await update.message.reply_text("📊 No user activity data yet.")
            return

        # --- Group by user ---
        summary = {}
        for row in records:
            name = row["Name"]
            action = row["Command"]
            summary.setdefault(name, {"total": 0, "actions": {}})
            summary[name]["total"] += 1
            summary[name]["actions"][action] = summary[name]["actions"].get(action, 0) + 1
            summary[name]["last_action"] = row["DateTime"]

        # --- Build reply message ---
        message = "📊 *All User Activity Summary:*\n\n"
        for name, info in summary.items():
            message += f"👤 *{name}*\n"
            message += f"🕒 Last Action: {info['last_action']}\n"
            message += f"📈 Total Actions: {info['total']}\n"
            for cmd, count in info["actions"].items():
                message += f"   • {cmd}: {count}\n"
            message += "\n"

        await update.message.reply_text(message, parse_mode="Markdown")

    except Exception as e:
        print(f"⚠️ Error generating stats: {e}")
        await update.message.reply_text("⚠️ Could not retrieve stats.")


async def auto_cleanup(context: ContextTypes.DEFAULT_TYPE):
    """Automatically remove expired meetings and announce updates."""
    now = datetime.now(pytz.timezone("Asia/Phnom_Penh"))
    records = sheet.get_all_records()

    removed = []
    updated_records = []

    for row in records:
        try:
            date_str = row["Date"]
            time_str = row["Time"]
            name = row["Name"]

            # Parse date/time from the record
            start_time_str = time_str.split("-")[0]
            end_time_str = time_str.split("-")[-1]

            meeting_end = datetime.strptime(f"{date_str} {end_time_str}", "%d/%m/%Y %H:%M")
            meeting_end = pytz.timezone("Asia/Phnom_Penh").localize(meeting_end)

            if meeting_end < now:
                removed.append(f"{date_str} | {time_str} | {name}")
            else:
                updated_records.append(row)
        except Exception as e:
            print(f"⚠️ Error parsing record: {e}")

    # If we found expired ones — update the sheet
    if removed:
        # Clear and re-write only valid rows
        sheet.clear()
        sheet.append_row(["Date", "Time", "Name", "TelegramID"])
        for r in updated_records:
            sheet.append_row([r["Date"], r["Time"], r["Name"], r["TelegramID"]])

        # Announce in group
        message = "🕒 *Expired meetings removed:*\n"
        for r in removed:
            message += f"🧹 {r}\n"

        if updated_records:
            message += "\n📋 *Updated Schedule:*\n"
            for row in updated_records:
                message += f"{row['Date']} | {row['Time']} | {row['Name']}\n"
        else:
            message += "\n✅ No more meetings left."

        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=message,
            parse_mode="Markdown"
        )        
#========================== Main =================================================================================================
        
def main():
    request = HTTPXRequest(connect_timeout=15.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TOKEN).request(request).build()

    # Initialize job queue safely
    job_queue = getattr(app, "job_queue", None)
    if not job_queue:
        try:
            from telegram.ext import JobQueue
            job_queue = JobQueue()
            job_queue.set_application(app)
            job_queue.start()
            print("✅ Job queue manually initialized.")
        except Exception as e:
            print(f"⚠️ Could not initialize job queue: {e}")

    # --- Set Bot Menu Commands ---
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("book", "Book the room"),
        BotCommand("show", "Show all bookings"),
        BotCommand("available", "Check available times"),
        BotCommand("cancel", "Cancel booking"),
    ]

    async def set_commands(application):
        await application.bot.set_my_commands(commands)

    app.post_init = set_commands

    # --- Conversations ---
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

    # --- Handlers ---
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(book_conv)
    app.add_handler(cancel_conv)
    app.add_handler(CommandHandler("show", show))
    app.add_handler(CommandHandler("available", available))

    # --- Schedule auto cleanup ---
    job_queue.run_repeating(auto_cleanup, interval=3600, first=10)
    print("🕒 Auto-cleanup scheduled every 1 hour.")
    print("✅ Meeting Room Bot is running...")
    app.run_polling()


if __name__ == "__main__":
        main()
    
























