import asyncio
import gspread
import dateparser
from datetime import datetime
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from telegram.request import HTTPXRequest

# ===================== CONFIG =====================
TOKEN = "7963509731:AAFSStEeAQT_mLYnb1EfCzzCA7nra93papg"
SPREADSHEET_NAME = "MeetingRoomBookings"
BOOKING_SHEET_RANGE = "Sheet1"  # Name of the first sheet
CREDENTIALS_FILE = "credentials.json"  # Path to your Google API JSON file

# Telegram conversation states
DATE, TIME = range(2)

# ===================== GOOGLE SHEETS =====================
from google.oauth2.service_account import Credentials
import gspread

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

import os, json
creds_json = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
creds = Credentials.from_service_account_info(creds_json, scopes=SCOPES)
client = gspread.authorize(creds)

# ✅ use your sheet URL, not name
sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/1vvBRrL-qXx0jp5-ZRR4xVpOi5ejxE8DtxrHOrel7F78").sheet1

# ===================== HELPER FUNCTIONS =====================
def is_slot_taken(date_str, time_str):
    records = sheet.get_all_records()
    for row in records:
        if row["Date"] == date_str and row["Time"] == time_str:
            return True
    return False

def add_booking(date_str, time_str, name, telegram_id):
    sheet.append_row([date_str, time_str, name, str(telegram_id)])

def cancel_booking(telegram_id, date_str, time_str):
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):  # row 1 is headers
        if row["TelegramID"] == str(telegram_id) and row["Date"] == date_str and row["Time"] == time_str:
            sheet.delete_rows(i)
            return True
    return False

def save_booking(date_str, time_str, name, telegram_id):
    """Save a booking if the date and time are available."""
    records = sheet.get_all_records()
    for row in records:
        if row.get("Date") == date_str and row.get("Time") == time_str:
            return False  # Already booked
    
    # Otherwise, add a new booking
    sheet.append_row([date_str, time_str, name, str(telegram_id)])
    return True


# ===================== COMMAND HANDLERS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to the Meeting Room Bot!\n\n"
        "Commands:\n"
        "/book - Book the meeting room\n"
        "/show - Show all bookings\n"
        "/available - Check available times\n"
        "/cancel - Cancel your booking"
    )

async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📅 Please enter the date for your booking (e.g. 25/10/2025):")
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

async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_input = update.message.text
    user = update.message.from_user
    date_str = context.user_data["date"]

    success = save_booking(date_str, time_input, user.first_name, user.id)
    if success:
        await update.message.reply_text(
            f"✅ Booking confirmed for {date_str} at {time_input}. Thank you, {user.first_name}!"
        )
    else:
        await update.message.reply_text(
            "❌ Sorry, this date and time are already booked. Please choose another slot."
        )

    return ConversationHandler.END


    add_booking(date_str, time_input, user.first_name, user.id)
    await update.message.reply_text(f"✅ Booking confirmed for {date_str} at {time_input}. Thank you, {user.first_name}!")
    return ConversationHandler.END

async def show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    records = sheet.get_all_records()
    if not records:
        await update.message.reply_text("No bookings yet.")
        return

    message = "📋 *Current Bookings:*\n"
    for row in records:
        message += f"{row['Date']} | {row['Time']} | {row['Name']}\n"
    await update.message.reply_text(message)

async def available(update: Update, context: ContextTypes.DEFAULT_TYPE):
    records = sheet.get_all_records()
    booked = [f"{r['Date']} {r['Time']}" for r in records]
    await update.message.reply_text("📅 Currently booked slots:\n" + "\n".join(booked))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🗓 Enter the date of the booking you want to cancel:")
    return DATE

async def confirm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_input = update.message.text
    date_obj = dateparser.parse(date_input)
    if not date_obj:
        await update.message.reply_text("❌ Invalid date. Try again.")
        return DATE
    context.user_data["cancel_date"] = date_obj.strftime("%d/%m/%Y")
    await update.message.reply_text("⏰ Enter the time range of the booking to cancel:")
    return TIME

async def do_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_input = update.message.text
    user = update.message.from_user
    date_str = context.user_data["cancel_date"]

    if cancel_booking(user.id, date_str, time_input):
        await update.message.reply_text("✅ Booking canceled successfully.")
    else:
        await update.message.reply_text("❌ No matching booking found.")
    return ConversationHandler.END

# ===================== MAIN APP =====================
def main():
    from telegram.request import HTTPXRequest

# Increase connection and read timeouts
request = HTTPXRequest(
    connect_timeout=15.0,  # wait up to 15 seconds to connect
    read_timeout=30.0      # wait up to 30 seconds for Telegram to respond
)

app = ApplicationBuilder().token(TOKEN).request(request).build()
book_conv = ConversationHandler(entry_points=[CommandHandler("book",book)],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)],
        },
        fallbacks=[],
    )
cancel_conv = ConversationHandler(
        entry_points=[CommandHandler("cancel", cancel)],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_cancel)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, do_cancel)],
        },
        fallbacks=[],
    )
app.add_handler(CommandHandler("start", start))
app.add_handler(book_conv)
app.add_handler(cancel_conv)
app.add_handler(CommandHandler("show", show))
app.add_handler(CommandHandler("available", available))
print("✅ Meeting Room Bot is running...")
# --- Keep Alive Web Server for Render Free Plan ---
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Meeting Room Bot is running on Render!"

def run_flask():
    app.run(host='0.0.0.0', port=10000)

# Start the Flask server in a background thread
threading.Thread(target=run_flask).start()
# --- End Keep Alive Section ---
import time

while True:
    try:
        app.run_polling()
    except Exception as e:
        print(f"⚠️ Bot error: {e}")
        print("🔁 Restarting in 5 seconds...")
        time.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
