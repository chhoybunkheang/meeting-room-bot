# meeting_bot_clean_v2.py
"""
Meeting Room Bot (Clean Version)
- Sorted schedule in all announcements (old → new)
- /schedule command removed
- Features: book, cancel, end, announce, clean, stats, upload/download docs
- Timezone: Asia/Phnom_Penh
- Auto-clean every 1 hour
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
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, JobQueue
)

# ===================== CONFIG =====================
TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1vvBRrL-qXx0jp5-ZRR4xVpOi5ejxE8DtxrHOrel7F78"
GROUP_CHAT_ID = -1003073406158
ADMIN_ID = 171208804
DATE, TIME, CANCEL_SELECT = range(3)
ANNOUNCE_MESSAGE = range(1)
DOC_SELECT = 100
UPLOAD_DOC = 101
TZ = "Asia/Phnom_Penh"
AUTO_CLEAN_INTERVAL = 3600

if not TOKEN or not GOOGLE_CREDENTIALS:
    raise RuntimeError("Missing BOT_TOKEN or GOOGLE_CREDENTIALS environment variable.")

# ===================== GOOGLE SHEETS =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
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

# ===================== DOCS FOLDER =====================
os.makedirs("docs", exist_ok=True)
if not os.listdir("docs"):
    open("docs/.keep", "w").close()

# ===================== HELPERS =====================
def now_phnom_penh():
    return datetime.now(ZoneInfo(TZ))

def log_user_action(user, command):
    try:
        now = now_phnom_penh()
        stats_sheet.append_row([str(user.id), user.first_name, command, now.strftime("%d/%m/%Y %H:%M:%S")])
    except Exception as e:
        print(f"⚠️ Could not log action: {e}")

def time_to_minutes(time_str):
    h, m = map(int, time_str.split(":"))
    return h * 60 + m

def is_overlapping(existing_start, existing_end, new_start, new_end):
    return not (new_end <= existing_start or new_start >= existing_end)

def sort_records_old_to_new(records):
    def sort_key(row):
        try:
            date_obj = datetime.strptime(row["Date"], "%d/%m/%Y")
            t = row["Time"].split("-")[0].strip()
            return (date_obj, datetime.strptime(t, "%H:%M"))
        except:
            return (datetime.max, datetime.max)
    return sorted(records, key=sort_key)

def save_booking(date_str, time_str, name, telegram_id):
    try:
        new_start_str, new_end_str = time_str.split("-")
        new_start = time_to_minutes(new_start_str.strip())
        new_end = time_to_minutes(new_end_str.strip())
    except Exception:
        return "invalid"
    for row in sheet.get_all_records():
        if row.get("Date") == date_str:
            try:
                s, e = row["Time"].split("-")
                if is_overlapping(time_to_minutes(s.strip()), time_to_minutes(e.strip()), new_start, new_end):
                    return "overlap"
            except:
                continue
    sheet.append_row([date_str, time_str, name, str(telegram_id)])
    return "success"

# ===================== BOT COMMANDS =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/start")
    await update.message.reply_text(
        "👋 Welcome to the Meeting Room Bot!\n\n"
        "Commands:\n"
        "/book - Book the meeting room\n"
        "/cancel - Cancel your booking\n"
        "/end - End your meeting\n"
        "/docs - Download available documents\n"
        "\n(Admin) /announce /stats /clean /uploaddoc"
    )

# ---------- BOOK ----------
async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user_action(update.message.from_user, "/book")
    await update.message.reply_text("📅 Please enter the date (e.g. 30/10/2025 or 30/10):")
    return DATE

async def get_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not re.match(r"^\d{1,2}/\d{1,2}(/?\d{2,4})?$", text):
        await update.message.reply_text("❌ Format must be DD/MM or DD/MM/YYYY.")
        return DATE
    if len(text.split("/")) == 2:
        text = f"{text}/{datetime.now().year}"
    date_obj = dateparser.parse(text, settings={"DATE_ORDER": "DMY"})
    if not date_obj or date_obj.date() < datetime.now().date():
        await update.message.reply_text("⚠️ Invalid or past date.")
        return DATE
    context.user_data["date"] = date_obj.strftime("%d/%m/%Y")
    await update.message.reply_text("⏰ Enter the time range (e.g. 14:00-15:00):")
    return TIME

async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_input = update.message.text.strip()
    user = update.message.from_user
    date_str = context.user_data.get("date")
    if not re.match(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$", time_input):
        await update.message.reply_text("❌ Use HH:MM-HH:MM format.")
        return TIME
    start_str, end_str = [t.strip() for t in time_input.split("-")]
    try:
        start, end = datetime.strptime(start_str, "%H:%M"), datetime.strptime(end_str, "%H:%M")
    except:
        await update.message.reply_text("❌ Invalid time values.")
        return TIME
    if end <= start:
        await update.message.reply_text("⚠️ End time must be later than start.")
        return TIME
    result = save_booking(date_str, time_input, user.first_name, user.id)
    if result == "overlap":
        await update.message.reply_text("⚠️ That time overlaps with another booking.")
        return TIME
    elif result == "invalid":
        await update.message.reply_text("❌ Could not save booking.")
        return TIME
    await update.message.reply_text(f"✅ Booking confirmed for {date_str} at {time_input}.")
    try:
        recs = sort_records_old_to_new(sheet.get_all_records())
        msg = (
            f"📢 *New Booking Added!*\n\n"
            f"👤 {user.first_name}\n"
            f"🗓 {date_str} | ⏰ {time_input}\n\n"
            f"📋 *Current Schedule (old → new):*\n"
        )
        for r in recs:
            msg += f"{r['Date']} | {r['Time']} | {r['Name']}\n"
        await context.bot.send_message(GROUP_CHAT_ID, msg, parse_mode="Markdown")
    except Exception as e:
        print(f"⚠️ Send group msg failed: {e}")
    return ConversationHandler.END

# ---------- CANCEL ----------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/cancel")
    recs = sheet.get_all_records()
    user_bookings = [(i + 2, r) for i, r in enumerate(recs) if str(r.get("TelegramID")) == str(user.id)]
    if not user_bookings:
        await update.message.reply_text("❌ You don’t have any bookings.")
        return ConversationHandler.END
    msg = "🗓 *Your Bookings:*\n\n"
    for i, (idx, r) in enumerate(user_bookings, 1):
        msg += f"{i}. {r['Date']} | {r['Time']}\n"
    msg += "\nReply with the *number* to cancel:"
    await update.message.reply_text(msg, parse_mode="Markdown")
    context.user_data["user_bookings"] = user_bookings
    return CANCEL_SELECT

async def delete_booking_by_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        choice = int(update.message.text)
    except:
        await update.message.reply_text("❌ Invalid number.")
        return CANCEL_SELECT
    user = update.message.from_user
    bookings = context.user_data.get("user_bookings", [])
    if not (1 <= choice <= len(bookings)):
        await update.message.reply_text("❌ Invalid choice.")
        return CANCEL_SELECT
    row_idx, booking = bookings[choice - 1]
    sheet.delete_rows(row_idx)
    await update.message.reply_text(f"✅ Canceled {booking['Date']} at {booking['Time']}.")
    recs = sort_records_old_to_new(sheet.get_all_records())
    message = (
        f"❌ {user.first_name} *CANCEL* booking:\n"
        f"📅 {booking['Date']} | ⏰ {booking['Time']}\n\n"
        f"📋 *Updated Schedule:*\n"
    )
    for r in recs:
        message += f"{r['Date']} | {r['Time']} | {r['Name']}\n"
    await context.bot.send_message(GROUP_CHAT_ID, message, parse_mode="Markdown")
    return ConversationHandler.END

# ---------- END MEETING ----------
async def end_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    log_user_action(user, "/end")
    recs = sheet.get_all_records()
    user_recs = [(i + 2, r) for i, r in enumerate(recs) if str(r.get("TelegramID")) == str(user.id)]
    if not user_recs:
        await update.message.reply_text("❌ No active meetings.")
        return
    tz = pytz.timezone(TZ)
    now = datetime.now(tz)
    active = None
    for i, r in user_recs:
        try:
            s, e = [t.strip() for t in r["Time"].split("-")]
            sdt, edt = tz.localize(datetime.strptime(f"{r['Date']} {s}", "%d/%m/%Y %H:%M")), tz.localize(datetime.strptime(f"{r['Date']} {e}", "%d/%m/%Y %H:%M"))
            if sdt <= now <= edt + timedelta(minutes=30):
                active = (i, r)
                break
        except:
            continue
    if not active:
        await update.message.reply_text("⏰ No current or recent meeting to end.")
        return
    idx, data = active
    sheet.delete_rows(idx)
    msg = (
        f"🏁 *Meeting Ended!*\n"
        f"👤 {user.first_name}\n"
        f"📅 {data['Date']} | ⏰ {data['Time']}\n"
    )
    await context.bot.send_message(GROUP_CHAT_ID, msg, parse_mode="Markdown")
    await update.message.reply_text("✅ Meeting ended and announced.")

# ---------- CLEANUP ----------
async def auto_cleanup(update: Update = None, context: ContextTypes.DEFAULT_TYPE = None):
    now = datetime.now(pytz.timezone(TZ))
    recs = sheet.get_all_records()
    removed, updated = [], []
    for r in recs:
        try:
            ds, ts = r["Date"], r["Time"]
            s, e = ts.split("-")
            end_dt = pytz.timezone(TZ).localize(datetime.strptime(f"{ds} {e.strip()}", "%d/%m/%Y %H:%M"))
            if end_dt < now: removed.append(f"{ds} | {ts} | {r['Name']}")
            else: updated.append(r)
        except: continue
    if removed:
        sheet.clear()
        sheet.update("A1", [["Date","Time","Name","TelegramID"]] + [[r["Date"],r["Time"],r["Name"],r["TelegramID"]] for r in updated])
        msg = "🧹 *Expired Meetings Removed:*\n"
        for r in removed: msg += f"• {r}\n"
        if updated:
            msg += "\n📋 *Updated Schedule (old → new):*\n"
            for r in sort_records_old_to_new(updated):
                msg += f"{r['Date']} | {r['Time']} | {r['Name']}\n"
        else: msg += "\n✅ No meetings left."
        await context.bot.send_message(GROUP_CHAT_ID, msg, parse_mode="Markdown")
        if update: await update.message.reply_text("✅ Cleanup done.")
    elif update:
        await update.message.reply_text("✨ No expired bookings to clean.")

# ---------- DOCS ----------
async def upload_doc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Unauthorized.")
        return ConversationHandler.END
    await update.message.reply_text("📤 Send the document to upload:", reply_markup=ReplyKeyboardRemove())
    return UPLOAD_DOC

async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("⚠️ Please send a file.")
        return UPLOAD_DOC
    try:
        path = os.path.join("docs", doc.file_name)
        await (await doc.get_file()).download_to_drive(path)
        await update.message.reply_text(f"✅ Uploaded: {doc.file_name}")
    except Exception as e:
        await update.message.reply_text("⚠️ Upload failed.")
        print(e)
    return ConversationHandler.END

async def docs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = [f for f in os.listdir("docs") if f != ".keep"]
    if not files:
        await update.message.reply_text("📂 No documents available.")
        return ConversationHandler.END
    keys = [[f"📄 {f}"] for f in files]
    await update.message.reply_text("📁 Choose a document:", reply_markup=ReplyKeyboardMarkup(keys, one_time_keyboard=True))
    return DOC_SELECT

async def send_selected_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.replace("📄 ", "").strip()
    path = os.path.join("docs", name)
    if not os.path.exists(path):
        await update.message.reply_text("⚠️ File not found.")
        return ConversationHandler.END
    await update.message.reply_document(document=InputFile(open(path, "rb"), filename=name), caption=f"📘 {name}")
    return ConversationHandler.END

# ---------- ADMIN ----------
async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Unauthorized.")
        return ConversationHandler.END
    await update.message.reply_text("📝 Type your announcement:")
    return ANNOUNCE_MESSAGE

async def send_announcement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Unauthorized.")
        return ConversationHandler.END
    if not msg:
        await update.message.reply_text("⚠️ Empty message.")
        return ANNOUNCE_MESSAGE
    await context.bot.send_message(GROUP_CHAT_ID, f"📢 *Announcement:*\n\n{msg}", parse_mode="Markdown")
    await update.message.reply_text("✅ Announcement sent.")
    return ConversationHandler.END

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Unauthorized.")
        return
    recs = stats_sheet.get_all_records()
    if not recs:
        await update.message.reply_text("📊 No user activity.")
        return
    summary = {}
    for r in recs:
        n, c, t = r["Name"], r["Command"], r["DateTime"]
        s = summary.setdefault(n, {"total":0,"actions":{}, "last":t})
        s["total"]+=1
        s["actions"][c]=s["actions"].get(c,0)+1
        s["last"]=t
    msg = "📊 *User Activity:*\n\n"
    for n,v in summary.items():
        acts = ", ".join([f"{a}({c})" for a,c in v["actions"].items()])
        msg += f"👤 *{n}*\n🕒 {v['last']}\n📈 {v['total']}\n📝 {acts}\n\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ---------- MAIN ----------
def main():
    request = HTTPXRequest(connect_timeout=15, read_timeout=30)
    app = ApplicationBuilder().token(TOKEN).request(request).build()
    jq = getattr(app, "job_queue", None) or JobQueue()
    jq.set_application(app)
    jq.start()
    async def setup(app):
        await app.bot.set_my_commands([
            BotCommand("start","Start"), BotCommand("book","Book room"),
            BotCommand("cancel","Cancel booking"), BotCommand("end","End meeting"),
            BotCommand("docs","Download docs")
        ])
        await app.bot.set_my_commands([
            BotCommand("announce","Announce"), BotCommand("stats","Stats"),
            BotCommand("clean","Clean expired"), BotCommand("uploaddoc","Upload doc")
        ], scope={"type":"chat","chat_id":ADMIN_ID})
    app.post_init = setup

    # Conversations
    book_conv = ConversationHandler(entry_points=[CommandHandler("book", book)], states={
        DATE:[MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
        TIME:[MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)]}, fallbacks=[])
    cancel_conv = ConversationHandler(entry_points=[CommandHandler("cancel", cancel)], states={
        CANCEL_SELECT:[MessageHandler(filters.TEXT & ~filters.COMMAND, delete_booking_by_number)]}, fallbacks=[])
    announce_conv = ConversationHandler(entry_points=[CommandHandler("announce", announce)], states={
        ANNOUNCE_MESSAGE:[MessageHandler(filters.TEXT & ~filters.COMMAND, send_announcement)]}, fallbacks=[])
    upload_conv = ConversationHandler(entry_points=[CommandHandler("uploaddoc", upload_doc_start)], states={
        UPLOAD_DOC:[MessageHandler(filters.Document.ALL, receive_document)]}, fallbacks=[])
    docs_conv = ConversationHandler(entry_points=[CommandHandler("docs", docs_menu)], states={
        DOC_SELECT:[MessageHandler(filters.TEXT & ~filters.COMMAND, send_selected_doc)]}, fallbacks=[])

    # Register
    app.add_handler(CommandHandler("start", start))
    app.add_handler(book_conv)
    app.add_handler(cancel_conv)
    app.add_handler(CommandHandler("end", end_meeting))
    app.add_handler(CommandHandler("clean", auto_cleanup))
    app.add_handler(announce_conv)
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(upload_conv)
    app.add_handler(docs_conv)

    jq.run_repeating(auto_cleanup, interval=AUTO_CLEAN_INTERVAL, first=10)
    print("✅ Bot running with auto-clean every 1 hour.")
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

