"""
Meeting Room Bot (Render Ready Final Version)
✅ Sorted schedule (old → new)
✅ Admin + User command menus merged
✅ Auto webhook clear (fixes 'Conflict: getUpdates')
✅ Proper crash alert filtering
✅ Clean indentation & safe Render startup
"""

import os, json, re, asyncio, pytz, warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import gspread, dateparser
from google.oauth2.service_account import Credentials
from telegram import (
    Bot, Update, BotCommand, InputFile,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, JobQueue
)

# ===================== CONFIG =====================
warnings.filterwarnings("ignore", category=RuntimeWarning)

TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1vvBRrL-qXx0jp5-ZRR4xVpOi5ejxE8DtxrHOrel7F78"
GROUP_CHAT_ID = -1003073406158
ADMIN_ID = 171208804
DATE, TIME, CANCEL_SELECT = range(3)
ANNOUNCE_MESSAGE = range(1)
DOC_SELECT, UPLOAD_DOC = 100, 101
TZ = "Asia/Phnom_Penh"
AUTO_CLEAN_INTERVAL = 3600  # 1 hour

if not TOKEN or not GOOGLE_CREDENTIALS:
    raise RuntimeError("Missing BOT_TOKEN or GOOGLE_CREDENTIALS environment variable.")

# ===================== GOOGLE SHEETS =====================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS), scopes=SCOPES)
client = gspread.authorize(creds)
sheet = client.open_by_url(SPREADSHEET_URL).sheet1
spreadsheet = client.open_by_url(SPREADSHEET_URL)
try:
    stats_sheet = spreadsheet.worksheet("UserStats")
except gspread.exceptions.WorksheetNotFound:
    stats_sheet = spreadsheet.add_worksheet(title="UserStats", rows="1000", cols="4")
    stats_sheet.append_row(["TelegramID", "Name", "Command", "DateTime"])

# ===================== FILE SETUP =====================
os.makedirs("docs", exist_ok=True)
if not os.listdir("docs"):
    open("docs/.keep", "w").close()

# ===================== HELPERS =====================
def now_phnom_penh():
    return datetime.now(ZoneInfo(TZ))

def log_user_action(user, command):
    try:
        now = now_phnom_penh().strftime("%d/%m/%Y %H:%M:%S")
        stats_sheet.append_row([str(user.id), user.first_name, command, now])
    except Exception as e:
        print(f"⚠️ Log failed: {e}")

def time_to_minutes(t):
    h, m = map(int, t.split(":"))
    return h * 60 + m

def is_overlapping(s1, e1, s2, e2):
    return not (e2 <= s1 or s2 >= e1)

def sort_records_old_to_new(records):
    def key(r):
        try:
            d = datetime.strptime(r["Date"], "%d/%m/%Y")
            t = datetime.strptime(r["Time"].split("-")[0].strip(), "%H:%M")
            return (d, t)
        except:
            return (datetime.max, datetime.max)
    return sorted(records, key=key)

def save_booking(date_str, time_str, name, tid):
    try:
        s, e = [x.strip() for x in time_str.split("-")]
        ns, ne = time_to_minutes(s), time_to_minutes(e)
    except:
        return "invalid"
    for r in sheet.get_all_records():
        if r.get("Date") == date_str:
            try:
                es, ee = [x.strip() for x in r["Time"].split("-")]
                if is_overlapping(time_to_minutes(es), time_to_minutes(ee), ns, ne):
                    return "overlap"
            except:
                continue
    sheet.append_row([date_str, time_str, name, str(tid)])
    return "success"

# ===================== COMMANDS =====================
async def start(update, context):
    u = update.message.from_user
    log_user_action(u, "/start")
    await update.message.reply_text(
        "👋 Welcome!\n\nCommands:\n"
        "/book - Book meeting room\n"
        "/cancel - Cancel booking\n"
        "/end - End your meeting\n"
        "/docs - Download files\n\n(Admin) /announce /stats /clean /uploaddoc"
    )

# --- BOOK ---
async def book(update, context):
    log_user_action(update.message.from_user, "/book")
    await update.message.reply_text("📅 Enter date (e.g. 30/10 or 30/10/2025):")
    return DATE

async def get_date(update, context):
    t = update.message.text.strip()
    if not re.match(r"^\d{1,2}/\d{1,2}(/?\d{2,4})?$", t):
        await update.message.reply_text("❌ Format: DD/MM or DD/MM/YYYY")
        return DATE
    if len(t.split("/")) == 2:
        t = f"{t}/{datetime.now().year}"
    d = dateparser.parse(t, settings={"DATE_ORDER": "DMY"})
    if not d or d.date() < datetime.now().date():
        await update.message.reply_text("⚠️ Invalid or past date.")
        return DATE
    context.user_data["date"] = d.strftime("%d/%m/%Y")
    await update.message.reply_text("⏰ Enter time range (e.g. 14:00-15:00):")
    return TIME

async def get_time(update, context):
    time_input = update.message.text.strip()
    u = update.message.from_user
    date_str = context.user_data["date"]
    if not re.match(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$", time_input):
        await update.message.reply_text("❌ Format: HH:MM-HH:MM")
        return TIME
    s, e = [t.strip() for t in time_input.split("-")]
    if datetime.strptime(e, "%H:%M") <= datetime.strptime(s, "%H:%M"):
        await update.message.reply_text("⚠️ End must be after start.")
        return TIME
    res = save_booking(date_str, time_input, u.first_name, u.id)
    if res == "overlap":
        await update.message.reply_text("⚠️ Overlaps another booking.")
        return TIME
    if res == "invalid":
        await update.message.reply_text("❌ Invalid time.")
        return TIME
    await update.message.reply_text(f"✅ Booking confirmed for {date_str} at {time_input}.")
    recs = sort_records_old_to_new(sheet.get_all_records())
    msg = f"📢 *New Booking Added!*\n\n👤 {u.first_name}\n🗓 {date_str} | ⏰ {time_input}\n\n📋 *Current Schedule (old → new):*\n"
    for r in recs:
        msg += f"{r['Date']} | {r['Time']} | {r['Name']}\n"
    await context.bot.send_message(GROUP_CHAT_ID, msg, parse_mode="Markdown")
    return ConversationHandler.END

# --- CANCEL ---
async def cancel(update, context):
    u = update.message.from_user
    log_user_action(u, "/cancel")
    recs = sheet.get_all_records()
    own = [(i + 2, r) for i, r in enumerate(recs) if str(r.get("TelegramID")) == str(u.id)]
    if not own:
        await update.message.reply_text("❌ No bookings.")
        return ConversationHandler.END
    msg = "🗓 *Your Bookings:*\n\n" + "\n".join(
        [f"{i+1}. {r['Date']} | {r['Time']}" for i, (_, r) in enumerate(own)]
    )
    msg += "\n\nReply with number to cancel:"
    await update.message.reply_text(msg, parse_mode="Markdown")
    context.user_data["user_bookings"] = own
    return CANCEL_SELECT

async def delete_booking_by_number(update, context):
    try:
        c = int(update.message.text)
    except:
        await update.message.reply_text("❌ Invalid number.")
        return CANCEL_SELECT
    u = update.message.from_user
    b = context.user_data.get("user_bookings", [])
    if not (1 <= c <= len(b)):
        await update.message.reply_text("❌ Invalid choice.")
        return CANCEL_SELECT
    idx, data = b[c - 1]
    sheet.delete_rows(idx)
    recs = sort_records_old_to_new(sheet.get_all_records())
    msg = (
        f"❌ {u.first_name} *CANCEL* booking:\n📅 {data['Date']} | ⏰ {data['Time']}\n\n📋 *Updated Schedule:*\n"
        + "".join([f"{r['Date']} | {r['Time']} | {r['Name']}\n" for r in recs])
    )
    await context.bot.send_message(GROUP_CHAT_ID, msg, parse_mode="Markdown")
    await update.message.reply_text("✅ Booking canceled.")
    return ConversationHandler.END

# --- END MEETING ---
async def end_meeting(update, context):
    u = update.message.from_user
    log_user_action(u, "/end")
    recs = sheet.get_all_records()
    own = [(i + 2, r) for i, r in enumerate(recs) if str(r.get("TelegramID")) == str(u.id)]
    if not own:
        await update.message.reply_text("❌ No meetings.")
        return
    tz = pytz.timezone(TZ)
    now = datetime.now(tz)
    for i, r in own:
        try:
            s, e = [t.strip() for t in r["Time"].split("-")]
            sdt, edt = [tz.localize(datetime.strptime(f"{r['Date']} {x}", "%d/%m/%Y %H:%M")) for x in (s, e)]
            if sdt <= now <= edt + timedelta(minutes=30):
                sheet.delete_rows(i)
                msg = f"🏁 *Meeting Ended!*\n👤 {u.first_name}\n📅 {r['Date']} | ⏰ {r['Time']}"
                await context.bot.send_message(GROUP_CHAT_ID, msg, parse_mode="Markdown")
                await update.message.reply_text("✅ Meeting ended.")
                return
        except:
            continue
    await update.message.reply_text("⏰ No active or recent meeting to end.")

# --- CLEANUP ---
async def auto_cleanup(update=None, context=None):
    try:
        now = datetime.now(pytz.timezone(TZ))
        recs = sheet.get_all_records()
        rem, kept = [], []
        for r in recs:
            try:
                s, e = r["Time"].split("-")
                end = pytz.timezone(TZ).localize(datetime.strptime(f"{r['Date']} {e.strip()}", "%d/%m/%Y %H:%M"))
                (rem if end < now else kept).append(r)
            except:
                continue
        if rem:
            sheet.clear()
            sheet.update(
                "A1",
                [["Date", "Time", "Name", "TelegramID"]]
                + [[r["Date"], r["Time"], r["Name"], r["TelegramID"]] for r in kept],
            )
            msg = "🧹 *Expired Meetings Removed:*\n" + "".join(
                [f"• {r['Date']} | {r['Time']} | {r['Name']}\n" for r in rem]
            )
            if kept:
                msg += "\n📋 *Updated Schedule (old → new):*\n"
                for r in sort_records_old_to_new(kept):
                    msg += f"{r['Date']} | {r['Time']} | {r['Name']}\n"
            else:
                msg += "\n✅ No meetings left."
            await context.bot.send_message(GROUP_CHAT_ID, msg, parse_mode="Markdown")
            if update:
                await update.message.reply_text("✅ Cleanup done.")
        elif update:
            await update.message.reply_text("✨ No expired bookings to clean.")
    except Exception as e:
        print(f"⚠️ Cleanup warning: {e}")
        return

# --- DOCS ---
async def upload_doc_start(update, context):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Unauthorized.")
        return ConversationHandler.END
    await update.message.reply_text("📤 Send document:", reply_markup=ReplyKeyboardRemove())
    return UPLOAD_DOC

async def receive_document(update, context):
    doc = update.message.document
    if not doc:
        await update.message.reply_text("⚠️ Send a file.")
        return UPLOAD_DOC
    p = os.path.join("docs", doc.file_name)
    await (await doc.get_file()).download_to_drive(p)
    await update.message.reply_text(f"✅ Uploaded {doc.file_name}")
    return ConversationHandler.END

async def docs_menu(update, context):
    files = [f for f in os.listdir("docs") if f != ".keep"]
    if not files:
        await update.message.reply_text("📂 No documents.")
        return ConversationHandler.END
    keys = [[f"📄 {f}"] for f in files]
    await update.message.reply_text(
        "📁 Choose document:", reply_markup=ReplyKeyboardMarkup(keys, one_time_keyboard=True)
    )
    return DOC_SELECT

async def send_selected_doc(update, context):
    f = update.message.text.replace("📄 ", "").strip()
    p = os.path.join("docs", f)
    if not os.path.exists(p):
        await update.message.reply_text("⚠️ File not found.")
        return ConversationHandler.END
    await update.message.reply_document(InputFile(open(p, "rb"), filename=f), caption=f"📘 {f}")
    return ConversationHandler.END

# --- ADMIN ---
async def announce(update, context):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Unauthorized.")
        return ConversationHandler.END
    await update.message.reply_text("📝 Type announcement:")
    return ANNOUNCE_MESSAGE

async def send_announcement(update, context):
    msg = update.message.text.strip()
    if not msg:
        await update.message.reply_text("⚠️ Empty message.")
        return ANNOUNCE_MESSAGE
    await context.bot.send_message(GROUP_CHAT_ID, f"📢 *Announcement:*\n\n{msg}", parse_mode="Markdown")
    await update.message.reply_text("✅ Announcement sent.")
    return ConversationHandler.END

async def stats(update, context):
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 Unauthorized.")
        return
    recs = stats_sheet.get_all_records()
    if not recs:
        await update.message.reply_text("📊 No data.")
        return
    summary = {}
    for r in recs:
        n, c, t = r["Name"], r["Command"], r["DateTime"]
        s = summary.setdefault(n, {"total": 0, "actions": {}, "last": t})
        s["total"] += 1
        s["actions"][c] = s["actions"].get(c, 0) + 1
        s["last"] = t
    msg = "📊 *User Activity:*\n\n"
    for n, v in summary.items():
        acts = ", ".join([f"{a}({c})" for a, c in v["actions"].items()])
        msg += f"👤 *{n}*\n🕒 {v['last']}\n📈 {v['total']}\n📝 {acts}\n\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# --- UTILS ---
async def clear_webhook(bot_token):
    bot = Bot(bot_token)
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Webhook cleared successfully!")

async def notify_admin(bot, msg):
    try:
        await bot.send_message(ADMIN_ID, f"⚠️ [Bot Alert]\n\n{msg}")
    except Exception as e:
        print("⚠️ Notify fail:", e)

# --- MAIN ---
def main():
    asyncio.run(clear_webhook(TOKEN))

    request = HTTPXRequest(connect_timeout=15.0, read_timeout=30.0)
    app = ApplicationBuilder().token(TOKEN).request(request).build()

    job_queue = getattr(app, "job_queue", None)
    if job_queue is None:
        job_queue = JobQueue()
        job_queue.set_application(app)
        job_queue.start()
        print("✅ Job queue manually initialized.")

    user_cmds = [
        BotCommand("start", "Start bot"),
        BotCommand("book", "Book room"),
        BotCommand("cancel", "Cancel booking"),
        BotCommand("end", "End meeting"),
        BotCommand("docs", "Download documents"),
    ]
    admin_cmds = [
        BotCommand("announce", "Send announcement"),
        BotCommand("stats", "View user stats"),
        BotCommand("clean", "Clean expired"),
        BotCommand("uploaddoc", "Upload document"),
    ]

       async def setup(app):
        # Set commands for everyone
        await app.bot.set_my_commands(user_cmds, scope={"type": "default"})

        # Merge user + admin commands (remove duplicates)
        all_admin_cmds = []
        seen = set()
        for cmd in user_cmds + admin_cmds:
            if cmd.command not in seen:
                all_admin_cmds.append(cmd)
                seen.add(cmd.command)

        # ✅ Apply merged commands for the admin (private chat or any group)
        await app.bot.set_my_commands(
            all_admin_cmds,
            scope={
                "type": "chat_member",
                "chat_id": ADMIN_ID,
                "user_id": ADMIN_ID,
            },
        )

        print("✅ Command menus set for users and admin.")


    app.post_init = setup

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("end", end_meeting))
    app.add_handler(CommandHandler("clean", auto_cleanup))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("announce", announce))
    app.add_handler(CommandHandler("uploaddoc", upload_doc_start))
    app.add_handler(CommandHandler("docs", docs_menu))

    book_conv = ConversationHandler(
        entry_points=[CommandHandler("book", book)],
        states={
            DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_date)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)],
        },
        fallbacks=[],
    )
    cancel_conv = ConversationHandler(
        entry_points=[CommandHandler("cancel", cancel)],
        states={
            CANCEL_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_booking_by_number)],
        },
        fallbacks=[],
    )
    app.add_handler(book_conv)
    app.add_handler(cancel_conv)

    job_queue.run_repeating(auto_cleanup, interval=AUTO_CLEAN_INTERVAL, first=10)
    print("✅ Bot running (auto-clean every 1h).")
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err_text = str(e)
        harmless = [
            "There is no current event loop",
            "Event loop is closed",
            "coroutine was never awaited",
            "KeyboardInterrupt",
        ]
        if any(msg in err_text for msg in harmless):
            print(f"⚠️ Ignored harmless error: {err_text}")
        else:
            print(f"❌ BOT ERROR: {err_text}")
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                bot = Bot(token=TOKEN)
                loop.run_until_complete(
                    notify_admin(bot, f"⚠️ [Bot Alert]\n\nBot stopped or crashed.\nError: {err_text}")
                )
                loop.close()
                print("⚠️ Admin notified successfully.")
            except Exception as inner_e:
                print(f"⚠️ Failed to alert admin: {inner_e}")

