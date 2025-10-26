import os
import json
import gspread
import dateparser
import asyncio
from datetime import datetime
from google.oauth2.service_account import Credentials
from telegram import Update
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

# ===================== HELPER FUNCTIONS =====================
def is_slot_taken(date_str, time_str):
    records = sheet.get_all_records()
    for row in records:
        if row["Date"] == date_str and row["Time"] == time_str:
            return True
    return False

def save_booking(date_str, time_str, name, telegram_id):
    """Save a booking if the slot is free."""
    if is_slot_taken(date_str, time_str):
        return False
    sheet.append_row([date_str, time_str, name, str(telegram_id)])
    return True

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
    await update.message.reply_text(
        "👋 Welcome to the Meeting Room Bot!\n\n"
        "Commands:\n"
        "/book - Book the meeting room\n"
        "/show - Show all bookings\n"
        "/available - Check booked times\n"
        "/cancel - Cancel your booking"
    )

async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📅 Please enter the date (e.g. 25/10/2025):")
    return DATE

async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_input = update.message.text
    date_obj = dateparser.parse(date_input)
    if not date_obj:
        await update.message.reply_text("❌ Invalid date format. Try again (e.g. 25/10/2025).")
        return DATE
    context.user_data["date"] = date_obj.strftime("%d/%m/%Y")
    await update.message.reply_text("⏰ Now enter the time range (e.g. 14:00-15:00):")
    return TIME

# ✅ When user books — announce to group
async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_input = update.message.text
    user = update.message.from_user
    date_str = context.user_data["date"]

    success = save_booking(date_str, time_input, user.first_name, user.id)
    if success:
        await update.message.reply_text(f"✅ Booking confirmed for {date_str} at {time_input}.")

        # --- Send group announcement ---
        records = sheet.get_all_records()
        message = (
            f"📢 *New Booking Added!*\n\n"
            f"👤 {user.first_name}\n"
            f"🗓 {date_str} | {time_input}\n\n"
            f"📋 *Current Schedule:*\n"
        )

        for row in records:
            message += f"{row['Date']} | {row['Time']} | {row['Name']}\n"

        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=message,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ That slot is already booked.")

    return ConversationHandler.END

async def show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    records = sheet.get_all_records()
    if not records:
        await update.message.reply_text("📋 No bookings yet.")
        return

    message = "📋 *Current Bookings:*\n"
    for row in records:
        message += f"{row['Date']} | {row['Time']} | {row['Name']}\n"
    await update.message.reply_text(message, parse_mode="Markdown")

async def available(update: Update, context: ContextTypes.DEFAULT_TYPE):
    records = sheet.get_all_records()
    if not records:
        await update.message.reply_text("✅ All time slots are available.")
        return
    booked = [f"{r['Date']} {r['Time']}" for r in records]
    await update.message.reply_text("📅 Booked slots:\n" + "\n".join(booked))

# ✅ Cancel by number (private only)
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        f"❌ *{user.first_name}* canceled the booking:\n"
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

# ===================== MAIN =====================
def main():
    request = HTTPXRequest(connect_timeout=15.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TOKEN).request(request).build()

     # --- Set Bot Menu Commands ---
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("book", "Book the room"),
        BotCommand("show", "Show all bookings"),
        BotCommand("available", "Check avai-times"),
        BotCommand("cancel", "Cancel booking"),
    ]

    async def set_commands(application):
        await application.bot.set_my_commands(commands)

    # ✅ Properly register post_init handler
    app.post_init = set_commands
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(book_conv)
    app.add_handler(cancel_conv)
    app.add_handler(CommandHandler("show", show))
    app.add_handler(CommandHandler("available", available))

    print("✅ Meeting Room Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()



