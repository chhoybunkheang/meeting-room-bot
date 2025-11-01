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
    filters, ContextTypes, ConversationHandler, JobQueue
)
from telegram.request import HTTPXRequest
from telegram import BotCommand

# ===================== CONFIG =====================
TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1vvBRrL-qXx0jp5-ZRR4xVpOi5ejxE8DtxrHOrel7F78"
GROUP_CHAT_ID = -1003073406158  
DATE, TIME, CANCEL_SELECT = range(3)
ANNOUNCE_MESSAGE = range(1)

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
        "/sort - Show sorted bookings\n"
        "/available - Check booked times\n"
        "/cancel - Cancel your booking"
    )

async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/book")
    await update.message.reply_text("📅 Please enter the date (e.g. 30/10/2025 or 30/10):")
    return DATE

#============================================== Get Date ()===========================================================

async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_input = update.message.text.strip()

    # ✅ Validate format using regex before parsing
    if not re.match(r"^\d{1,2}/\d{1,2}(/?\d{2,4})?$", date_input):
        await update.message.reply_text("❌ Please enter date in format DD/MM or DD/MM/YYYY.")
        return DATE

    # Add current year if user omits it
    if len(date_input.split("/")) == 2:
        date_input = f"{date_input}/{datetime.now().year}"

    # Parse with DMY order to ensure correct format
    date_obj = dateparser.parse(date_input, settings={"DATE_ORDER": "DMY"})

    if not date_obj:
        await update.message.reply_text("❌ Invalid date. Try again (example: 25/10 or 25/10/2025).")
        return DATE

    # Check if date is in the past
    if date_obj.date() < datetime.now().date():
        await update.message.reply_text("⚠️ The date you entered is in the past. Please choose a future date.")
        return DATE

    # Save valid date
    context.user_data["date"] = date_obj.strftime("%d/%m/%Y")
    await update.message.reply_text("⏰ Great! Now enter the time range (e.g. 14:00-15:00):")
    return TIME


#==================================================== Get_time()======================================================================

# ✅ When user books — announce to group
import re
from datetime import datetime

async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_input = update.message.text.strip()
    user = update.message.from_user
    date_str = context.user_data.get("date")

    # ✅ Validate time format (HH:MM-HH:MM)
    if not re.match(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$", time_input):
        await update.message.reply_text("❌ Invalid time format. Use HH:MM-HH:MM (e.g. 09:00-10:30).")
        return TIME

    # Parse and validate time range
    start_str, end_str = [t.strip() for t in time_input.split("-")]
    try:
        start_time = datetime.strptime(start_str, "%H:%M")
        end_time = datetime.strptime(end_str, "%H:%M")
    except ValueError:
        await update.message.reply_text("❌ Invalid time values. Please check your input again.")
        return TIME

    # Check logical order (start < end)
    if end_time <= start_time:
        await update.message.reply_text("⚠️ End time must be later than start time.")
        return TIME

    # Check overlap and save
    result = save_booking(date_str, time_input, user.first_name, user.id)

    if result == "overlap":
        await update.message.reply_text("⚠️ That time overlaps with another booking. Please choose another slot.")
        return TIME

    elif result == "invalid":
        await update.message.reply_text("❌ Could not save booking. Please try again.")
        return TIME

    elif result == "success":
        await update.message.reply_text(f"✅ Booking confirmed for {date_str} at {time_input}.")

        # Announce to group
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

            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=message, parse_mode="Markdown")
        except Exception as e:
            print(f"⚠️ Could not send group message: {e}")

    return ConversationHandler.END

async def show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/sort")
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
        
#==================================== announcement===========================================================================================
async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Admin starts the announce command."""
    user = update.message.from_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return ConversationHandler.END

    await update.message.reply_text("📝 Please type your announcement message:")
    return ANNOUNCE_MESSAGE


async def send_announcement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: Send the admin’s message to the group."""
    user = update.message.from_user
    message_text = update.message.text.strip()

    if user.id != ADMIN_ID:
        await update.message.reply_text("🚫 You are not authorized to use this command.")
        return ConversationHandler.END

    if not message_text:
        await update.message.reply_text("⚠️ Empty message, please type something or /cancel.")
        return ANNOUNCE_MESSAGE

    # --- Send to group ---
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

 # ===================== AUTO CLEANUP OLD BOOKINGS ==================================================================
    
async def auto_cleanup(context: ContextTypes.DEFAULT_TYPE):
    """Automatically remove expired meetings, rebuild the sheet safely, and announce updates."""
    now = datetime.now(pytz.timezone("Asia/Phnom_Penh"))
    records = sheet.get_all_records()

    removed = []
    updated_records = []

    # --- Identify expired and valid records ---
    for row in records:
        try:
            date_str = row["Date"]
            time_str = row["Time"]
            name = row["Name"]

            # Parse meeting end time
            start_time_str, end_time_str = time_str.split("-")
            meeting_end = datetime.strptime(f"{date_str} {end_time_str.strip()}", "%d/%m/%Y %H:%M")
            meeting_end = pytz.timezone("Asia/Phnom_Penh").localize(meeting_end)

            # Compare to current time
            if meeting_end < now:
                removed.append(f"{date_str} | {time_str} | {name}")
            else:
                updated_records.append(row)
        except Exception as e:
            print(f"⚠️ Error parsing record: {e}")

    # --- If expired meetings found, rebuild the sheet ---
    if removed:
        try:
            headers = ["Date", "Time", "Name", "TelegramID"]
            sheet.clear()

            # Prepare new data: headers + valid rows
            new_data = [headers]
            for r in updated_records:
                date_val = r.get("Date", "")
                time_val = r.get("Time", "")
                name_val = r.get("Name", "")
                id_val = r.get("TelegramID", "")
                new_data.append([date_val, time_val, name_val, id_val])

            # ✅ Write all at once (faster and cleaner)
            sheet.update("A1", new_data)
            print("✅ Sheet successfully rewritten with updated records.")

            # --- Announce in group ---
            message = "🕒 *Expired Meetings Removed:*\n"
            for r in removed:
                message += f"🧹 {r}\n"

            if updated_records:
                message += "\n📋 *Updated Schedule:*\n"
                for row in updated_records:
                    message += f"{row['Date']} | {row['Time']} | {row['Name']}\n"
            else:
                message += "\n✅ No meetings left in the schedule."

            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=message,
                parse_mode="Markdown"
            )

        except Exception as e:
            print(f"⚠️ Error rewriting sheet: {e}")
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text="⚠️ Cleanup failed due to a sheet update error.",
                parse_mode="Markdown"
            )
    else:
        print("✅ No expired meetings found during cleanup.")
        
# ================= CLEAR WEBHOOK =============================================================================================================
async def clear_webhook(bot_token):
    """Ensure the bot is in polling mode (not webhook)."""
    bot = Bot(bot_token)
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Webhook cleared successfully!")

#======================================== Notify_admin when stop or crash ===================================  
     
async def notify_admin(bot, message: str):
    """Send a notification message to the admin."""
    try:
        await bot.send_message(chat_id=ADMIN_ID, text=f"⚠️ [Bot Alert]\n\n{message}")
        print(f"✅ Sent alert to admin: {message}")
    except Exception as e:
        print(f"⚠️ Failed to notify admin: {e}")

# ================================================== MAIN ==========================================================================================
def main():
    request = HTTPXRequest(connect_timeout=15.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TOKEN).request(request).build()

    # ✅ Initialize job queue safely
    job_queue = getattr(app, "job_queue", None)
    if not job_queue:
        try:
            job_queue = JobQueue()
            job_queue.set_application(app)
            job_queue.start()
            print("✅ Job queue manually initialized.")
        except Exception as e:
            print(f"⚠️ Could not initialize job queue: {e}")

    # --- Define commands for user and admin ---
    user_commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("book", "Book the room"),
        BotCommand("sort", "Show sorted booking "),
        BotCommand("cancel", "Cancel booking"),
    ]

    admin_commands = user_commands + [
        BotCommand("announce", "Send announcement to group"),
        BotCommand("stats", "View all user activity"),
        BotCommand("clean", "Clean up expired bookings"),
    ]

    # --- Set different menus for user vs admin ---
    async def set_commands(application):
        # Normal users
        await application.bot.set_my_commands(user_commands, scope={"type": "default"})
        # Admin only
        await application.bot.set_my_commands(admin_commands, scope={"type": "chat", "chat_id": ADMIN_ID})
        print("✅ Command menus set for users and admin.")

        # Clear webhook safely
        await clear_webhook(TOKEN)

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
    announce_conv = ConversationHandler(
    entry_points=[CommandHandler("announce", announce)],
    states={
        ANNOUNCE_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_announcement)],
        },
        fallbacks=[],
        per_user=True,
        per_chat=True,
    )



    # --- Handlers ---
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(book_conv)
    app.add_handler(cancel_conv)
    app.add_handler(CommandHandler("sort", show))
    app.add_handler(announce_conv)
    app.add_handler(CommandHandler("clean", auto_cleanup))

    # --- Schedule auto cleanup ---
    job_queue.run_repeating(auto_cleanup, interval=3600, first=10)
    print("🕒 Auto-cleanup scheduled every 1 hour.")
    print("✅ Meeting Room Bot is running...")

    # ✅ Run the polling (now webhook cleared safely inside loop)
    app.run_polling()


if __name__ == "__main__":
    import asyncio

    async def start_and_monitor():
        bot = Bot(token=TOKEN)
        try:
            # ✅ Notify admin that bot is starting
            await notify_admin(bot, "✅ Bot has started successfully and is now running.")
            print("✅ Admin notified: bot started.")

            # Run bot (this blocks until stopped or crashed)
            main()

        except Exception as e:
            print(f"❌ BOT ERROR: {e}")
            try:
                await notify_admin(bot, f"🚨 Bot stopped or crashed!\nError: {e}")
            except Exception as inner_e:
                print(f"⚠️ Failed to send crash alert: {inner_e}")

    # ✅ Properly run in async environment
    asyncio.run(start_and_monitor())


    







































