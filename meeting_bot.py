import asyncio
import calendar
import os
import re
import shutil
import subprocess
import tempfile
import warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
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
    WebAppInfo,
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
from telegram.warnings import PTBUserWarning
from booking_validation import validate_booking_interval

# Load environment variables from .env file
load_dotenv(override=True)
warnings.simplefilter("ignore", PTBUserWarning)

# ===================== CONFIG =====================
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
MINI_APP_URL = os.getenv("MINI_APP_URL")
# Validate environment variables
if not MINI_APP_URL:
    raise ValueError("❌ MINI_APP_URL environment variable is not set!")
if not os.getenv("GROUP_CHAT_ID"):
    raise ValueError("❌ GROUP_CHAT_ID environment variable is not set!")
if not os.getenv("ADMIN_ID"):
    raise ValueError("❌ ADMIN_ID environment variable is not set!")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable is not set!")
if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL environment variable is not set!")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID"))
ADMIN_ID = int(os.getenv("ADMIN_ID"))
MEETING_TIMEZONE = ZoneInfo(os.getenv("MEETING_TIMEZONE", "Asia/Phnom_Penh"))

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

# In-memory store for cancellation details (keyed by timestamp string)
cancel_details_store: dict = {}

# Conversation states
SELECT_MONTH, SELECT_DAY, TIME, CANCEL_SELECT = range(4)
ANNOUNCE_MESSAGE = 200

# State for docs upload
UPLOAD_DOC = 101
PDF_NAME_INPUT = 300
CONVERT_TO_PDF = 301

# Ensure docs folder exists
os.makedirs("docs", exist_ok=True)
if not os.listdir("docs"):
    open("docs/.keep", "w").close()
print("✅ 'docs' folder ready (auto-created if missing).")

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


async def log_user_action(user, command):
    """Log user activity to PostgreSQL."""

    def save_log():
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO user_activity (
                        telegram_user_id,
                        user_name,
                        command
                    )
                    VALUES (
                        :telegram_user_id,
                        :user_name,
                        :command
                    )
                """),
                {
                    "telegram_user_id": user.id,
                    "user_name": user.first_name,
                    "command": command,
                },
            )

    try:
        await asyncio.to_thread(save_log)

        now = datetime.now(MEETING_TIMEZONE)
        now_str = now.strftime("%d/%m/%Y %H:%M:%S")

        print(f"✅ Logged {command} by {user.first_name} ({user.id}) at {now_str}")

    except Exception as e:
        print(f"⚠️ Could not log action: {e}")


def time_to_minutes(time_str):
    """Convert 'HH:MM' to total minutes for easy comparison."""
    h, m = map(int, time_str.split(":"))
    return h * 60 + m


def is_overlapping(existing_start, existing_end, new_start, new_end):
    """Check if two time ranges overlap."""
    return not (new_end <= existing_start or new_start >= existing_end)


async def save_booking(date_str, time_str, name, telegram_id):
    """Save booking to PostgreSQL if there is no time overlap."""

    try:
        start_str, end_str = [t.strip() for t in time_str.split("-")]

        booking_date = datetime.strptime(date_str, "%d/%m/%Y").date()
        start_time = datetime.strptime(start_str, "%H:%M").time()
        end_time = datetime.strptime(end_str, "%H:%M").time()

    except ValueError:
        return "invalid"

    try:
        validate_booking_interval(
            booking_date,
            start_time,
            end_time,
            datetime.now(MEETING_TIMEZONE),
        )
    except ValueError:
        return "invalid"

    def save_to_db():
        try:
            with engine.begin() as conn:
                overlap = conn.execute(
                    text("""
                        SELECT EXISTS (
                            SELECT 1
                            FROM bookings
                            WHERE booking_date = :booking_date
                              AND status = 'BOOKED'
                              AND start_time < :end_time
                              AND end_time > :start_time
                        )
                    """),
                    {
                        "booking_date": booking_date,
                        "start_time": start_time,
                        "end_time": end_time,
                    },
                ).scalar()

                if overlap:
                    return "overlap"

                conn.execute(
                    text("""
                        INSERT INTO bookings (
                            telegram_user_id,
                            user_name,
                            booking_date,
                            start_time,
                            end_time,
                            status
                        )
                        VALUES (
                            :telegram_user_id,
                            :user_name,
                            :booking_date,
                            :start_time,
                            :end_time,
                            'BOOKED'
                        )
                    """),
                    {
                        "telegram_user_id": telegram_id,
                        "user_name": name,
                        "booking_date": booking_date,
                        "start_time": start_time,
                        "end_time": end_time,
                    },
                )

                return "success"
        except IntegrityError as exc:
            if "bookings_no_active_overlap" in str(exc.orig):
                return "overlap"
            raise

    return await asyncio.to_thread(save_to_db)


async def get_all_bookings():
    """Read all active bookings from PostgreSQL."""

    def read_from_db():
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    text("""
                    SELECT
                        id,
                        telegram_user_id,
                        user_name,
                        booking_date,
                        start_time,
                        end_time,
                        status
                    FROM bookings
                    WHERE status = 'BOOKED'
                    ORDER BY booking_date, start_time
                """)
                )
                .mappings()
                .all()
            )

            return [dict(row) for row in rows]

    return await asyncio.to_thread(read_from_db)


async def get_user_bookings(telegram_id):
    """Get active bookings for one Telegram user from PostgreSQL."""

    def read_from_db():
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    text("""
                    SELECT
                        id,
                        telegram_user_id,
                        user_name,
                        booking_date,
                        start_time,
                        end_time,
                        status
                    FROM bookings
                    WHERE telegram_user_id = :telegram_user_id
                      AND status = 'BOOKED'
                    ORDER BY booking_date, start_time
                """),
                    {
                        "telegram_user_id": telegram_id,
                    },
                )
                .mappings()
                .all()
            )

            return [dict(row) for row in rows]

    return await asyncio.to_thread(read_from_db)


# ===================== BOT COMMANDS =====================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    await log_user_action(user, "/start")

    try:
        admin = await context.bot.get_chat(ADMIN_ID)
        admin_name = admin.first_name

        admin_username = f"@{admin.username}" if admin.username else admin_name

    except Exception:
        admin_username = "the admin"

    # Always create the Mini App keyboard
    mini_app_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🏢 Open Meeting Room",
                    web_app=WebAppInfo(url=MINI_APP_URL),
                )
            ]
        ]
    )

    await update.message.reply_text(
        "👋 Welcome to the Meeting Room Bot!\n\n"
        "Use the Meeting Room app to:\n"
        "• View the current room schedule\n"
        "• Filter bookings by Today, Tomorrow, or All\n"
        "• Book and manage your meetings\n"
        "• Cancel a booking or end a meeting early\n\n"
        "Tap the button below to get started. You can return here anytime "
        "with /start.\n\n"
        f"ℹ️ Created by {admin_username}",
        reply_markup=mini_app_keyboard,
    )


async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    await log_user_action(user, "/book")
    tz = MEETING_TIMEZONE
    now_pp = datetime.now(tz)
    keyboard = _build_month_keyboard(now_pp)

    prompt_message = await update.message.reply_text(
        "📅 Choose a month to book:",
        reply_markup=keyboard,
    )
    _remember_booking_prompt(prompt_message, context)
    return SELECT_MONTH


async def handle_month_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    tz = MEETING_TIMEZONE

    if data == "month:choose":
        now_pp = datetime.now(tz)
        edited_message = await query.edit_message_text(
            "📅 Choose a month to book:",
            reply_markup=_build_month_keyboard(now_pp),
        )
        _remember_booking_prompt(edited_message, context)
        return SELECT_MONTH

    if not data.startswith("month:"):
        return SELECT_MONTH

    try:
        year_month = data.split(":", 1)[1]
        year, month = map(int, year_month.split("-"))
    except Exception:
        edited_message = await query.edit_message_text(
            "⚠️ Could not read that month. Please choose again."
        )
        _remember_booking_prompt(edited_message, context)
        return SELECT_MONTH

    day_keyboard = _build_day_keyboard(year, month, tz)

    # If no days are available (e.g., all past), show month picker again
    if len(day_keyboard.inline_keyboard) <= 1:  # only the back button exists
        now_pp = datetime.now(tz)
        edited_message = await query.edit_message_text(
            "⚠️ No future days left in that month. Pick another month:",
            reply_markup=_build_month_keyboard(now_pp),
        )
        _remember_booking_prompt(edited_message, context)
        return SELECT_MONTH

    edited_message = await query.edit_message_text(
        f"📅 {datetime(year, month, 1).strftime('%B %Y')}\nChoose a day:",
        reply_markup=day_keyboard,
    )
    _remember_booking_prompt(edited_message, context)
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
        edited_message = await query.edit_message_text(
            "⚠️ Could not read that day. Please choose again."
        )
        _remember_booking_prompt(edited_message, context)
        return SELECT_DAY

    selected_date = datetime(year, month, day)
    context.user_data["date"] = selected_date.strftime("%d/%m/%Y")

    edited_message = await query.edit_message_text(
        f"📅 Selected: {context.user_data['date']}\n⏰ Now enter the time range (e.g. 14:00-15:00):"
    )
    _remember_booking_prompt(edited_message, context)
    return TIME

# ----------------- Get Date -----------------


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply with the numeric ID of the group where the command was sent."""
    chat = update.effective_chat
    message = update.effective_message

    if chat is None or message is None:
        return

    if chat.type not in {"group", "supergroup"}:
        await message.reply_text("Send /chatid inside the Telegram group.")
        return

    await message.reply_text(f"Group ID: {chat.id}")


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
        months.append(
            InlineKeyboardButton(
                label, callback_data=f"month:{month_dt.strftime('%Y-%m')}"
            )
        )

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
        row.append(
            InlineKeyboardButton(
                str(day), callback_data=f"day:{year}-{month:02d}-{day:02d}"
            )
        )
        if len(row) == 7:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("🔙 Choose month", callback_data="month:choose")])
    return InlineKeyboardMarkup(rows)


def _remember_booking_prompt(message, context: ContextTypes.DEFAULT_TYPE):
    """Track the latest booking prompt message so it can be cleaned up if cancelled."""
    if not message:
        return
    context.user_data["booking_prompt_message"] = {
        "chat_id": message.chat_id,
        "message_id": message.message_id,
    }


def _clear_booking_prompt(context: ContextTypes.DEFAULT_TYPE):
    """Forget any stored booking prompt reference once the flow is complete."""
    context.user_data.pop("booking_prompt_message", None)


def _normalize_pdf_source_name(original_name: str, fallback: str = "file") -> str:
    """Sanitize an incoming filename and provide a safe fallback."""
    name = os.path.basename(original_name or "").strip()
    if not name:
        name = fallback
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _normalize_output_pdf_name(raw_name: str) -> str | None:
    """Normalize user-provided output PDF filename."""
    base = _normalize_pdf_source_name(raw_name or "", fallback="")
    if not base:
        return None

    # Keep only file stem from any extension the user typed.
    stem = os.path.splitext(base)[0].strip("._-")
    if not stem:
        return None
    return f"{stem}.pdf"


def _convert_image_to_pdf(source_path: str, output_pdf_path: str):
    """Convert a local image file to PDF (RGB)."""
    with Image.open(source_path) as img:
        if img.mode in ("RGBA", "P") or img.mode != "RGB":
            img = img.convert("RGB")
        img.save(output_pdf_path, "PDF", resolution=100.0)


def _convert_text_to_pdf(source_path: str, output_pdf_path: str):
    """Convert plain text-like files to a simple PDF layout."""
    c = canvas.Canvas(output_pdf_path, pagesize=A4)
    width, height = A4
    y = height - 40
    line_height = 14

    with open(source_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            text = line.rstrip("\n")
            if not text:
                text = " "

            chunks = [text[i : i + 100] for i in range(0, len(text), 100)] or [" "]
            for chunk in chunks:
                if y < 40:
                    c.showPage()
                    y = height - 40
                c.drawString(40, y, chunk)
                y -= line_height

    c.save()


def _try_convert_with_libreoffice(source_path: str, out_dir: str) -> str | None:
    """Try converting office docs using LibreOffice if available."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None

    subprocess.run(
        [
            soffice,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            out_dir,
            source_path,
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    base_name = os.path.splitext(os.path.basename(source_path))[0]
    candidate = os.path.join(out_dir, f"{base_name}.pdf")
    if os.path.exists(candidate):
        return candidate
    return None


def _convert_to_pdf_sync(
    source_path: str, source_name: str, mime_type: str | None, out_dir: str
) -> tuple[str | None, str | None]:
    """Convert supported files to PDF. Returns (pdf_path, error_message)."""
    ext = os.path.splitext(source_name)[1].lower()
    output_pdf = os.path.join(out_dir, f"{os.path.splitext(source_name)[0]}.pdf")

    image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
    text_exts = {".txt", ".md", ".csv", ".log"}
    office_exts = {
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
    }

    try:
        if ext == ".pdf":
            return source_path, None

        if ext in image_exts or (mime_type and mime_type.startswith("image/")):
            _convert_image_to_pdf(source_path, output_pdf)
            return output_pdf, None

        if ext in text_exts or (mime_type and mime_type.startswith("text/")):
            _convert_text_to_pdf(source_path, output_pdf)
            return output_pdf, None

        if ext in office_exts:
            converted = _try_convert_with_libreoffice(source_path, out_dir)
            if converted:
                return converted, None
            return None, "Office conversion needs LibreOffice on the server."

        return None, "Unsupported file type for PDF conversion."
    except Exception as e:
        return None, f"Conversion failed: {e}"


async def _delete_booking_prompt(context: ContextTypes.DEFAULT_TYPE):
    """Remove the stored booking prompt message when the user cancels the flow."""
    prompt_info = context.user_data.pop("booking_prompt_message", None)
    if not prompt_info:
        return

    chat_id = prompt_info.get("chat_id")
    message_id = prompt_info.get("message_id")
    if chat_id is None or message_id is None:
        return

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(f"⚠️ Could not delete booking prompt: {e}")


# ----------------- Get Time & Save -----------------


async def get_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_input = update.message.text.strip()
    user = update.message.from_user
    date_str = context.user_data.get("date")

    if not re.match(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$", time_input):
        await update.message.reply_text(
            "❌ Invalid time format. Use HH:MM-HH:MM (e.g. 09:00-10:30)."
        )
        return TIME

    start_str, end_str = [t.strip() for t in time_input.split("-")]
    try:
        start_time = datetime.strptime(start_str, "%H:%M")
        end_time = datetime.strptime(end_str, "%H:%M")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid time values. Please check your input again."
        )
        return TIME

    if end_time <= start_time:
        await update.message.reply_text("⚠️ End time must be later than start time.")
        return TIME

    result = await save_booking(date_str, time_input, user.first_name, user.id)

    if result == "overlap":
        await update.message.reply_text(
            "⚠️ That time overlaps with another booking. Please choose another slot."
        )
        return TIME
    elif result == "invalid":
        await update.message.reply_text("❌ Could not save booking. Please try again.")
        return TIME
    elif result == "success":
        await update.message.reply_text(
            f"✅ Booking confirmed for {date_str} at {time_input}."
        )

        _clear_booking_prompt(context)

        # Announce to group with sorted schedule
        try:
            records = await get_all_bookings()

            message = (
                f"📢 *New Booking Added!*\n\n"
                f"👤 {user.first_name}\n"
                f"🗓 {date_str} | ⏰ {time_input}\n\n"
                f"📋 *Current Schedule:*\n"
            )

            for row in records:
                date_text = row["booking_date"].strftime("%d/%m/%Y")
                time_text = (
                    f"{row['start_time'].strftime('%H:%M')}-"
                    f"{row['end_time'].strftime('%H:%M')}"
                )

                message += f"{date_text} | {time_text} | {row['user_name']}\n"
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID, text=message, parse_mode="Markdown"
            )
            print("✅ Group message with sorted schedule sent.")
        except Exception as e:
            print(f"⚠️ Could not send group message: {e}")

        return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    await log_user_action(user, "/cancel")

    user_bookings = await get_user_bookings(user.id)

    if not user_bookings:
        await update.message.reply_text("❌ You don’t have any bookings to cancel.")
        return ConversationHandler.END

    message = "🗓 *Your Bookings:*\n\n"

    for idx, booking in enumerate(user_bookings, start=1):
        date_text = booking["booking_date"].strftime("%d/%m/%Y")
        time_text = (
            f"{booking['start_time'].strftime('%H:%M')}-"
            f"{booking['end_time'].strftime('%H:%M')}"
        )

        message += f"{idx}. {date_text} | {time_text}\n"

    message += "\nReply with the *number* of the booking you want to delete:"

    await update.message.reply_text(message, parse_mode="Markdown")

    context.user_data["user_bookings"] = user_bookings

    return CANCEL_SELECT


async def delete_booking_by_number(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
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

    booking = user_bookings[choice - 1]

    booking_id = booking["id"]
    canceled_date = booking["booking_date"].strftime("%d/%m/%Y")
    canceled_time = (
        f"{booking['start_time'].strftime('%H:%M')}-"
        f"{booking['end_time'].strftime('%H:%M')}"
    )

    def delete_from_db():
        with engine.begin() as conn:
            result = conn.execute(
                text("""
                    DELETE FROM bookings
                    WHERE id = :booking_id
                      AND telegram_user_id = :telegram_user_id
                    RETURNING id
                """),
                {
                    "booking_id": booking_id,
                    "telegram_user_id": user.id,
                },
            ).first()

            return result is not None

    deleted = await asyncio.to_thread(delete_from_db)

    if not deleted:
        await update.message.reply_text(
            "⚠️ Booking could not be found or was already cancelled."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"✅ Canceled booking on {canceled_date} at {canceled_time}."
    )

    records = await get_all_bookings()

    if records:
        message = "📋 *Updated Schedule:*\n"

        for row in records:
            date_text = row["booking_date"].strftime("%d/%m/%Y")
            time_text = (
                f"{row['start_time'].strftime('%H:%M')}-"
                f"{row['end_time'].strftime('%H:%M')}"
            )

            message += f"{date_text} | {time_text} | {row['user_name']}\n"
    else:
        message = "📋 No bookings left."

    detail_key = str(int(datetime.now(MEETING_TIMEZONE).timestamp() * 1000))

    cancel_details_store[detail_key] = {
        "name": user.first_name,
        "user_id": user.id,
        "date": canceled_date,
        "time": canceled_time,
    }

    detail_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"📅 {canceled_date} | ⏰ {canceled_time}",
                    callback_data=f"cancel_info:{detail_key}",
                )
            ]
        ]
    )

    announcement = f"{message}\n🗑️ *Booking Cancelled:*"

    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=announcement,
            parse_mode="Markdown",
            reply_markup=detail_keyboard,
        )
    except Exception as e:
        print(f"⚠️ Could not send group message: {e}")

    context.user_data.pop("user_bookings", None)

    return ConversationHandler.END


# ----------------- End meeting -----------------


async def end_meeting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    await log_user_action(user, "/end")

    user_bookings = await get_user_bookings(user.id)

    if not user_bookings:
        await update.message.reply_text("❌ You don’t have any active meetings to end.")
        return

    tz = MEETING_TIMEZONE
    now = datetime.now(tz)

    active_meeting = None

    for booking in user_bookings:
        try:
            booking_date = booking["booking_date"]
            start_time = booking["start_time"]
            end_time = booking["end_time"]

            start_dt = datetime.combine(booking_date, start_time).replace(tzinfo=tz)

            end_dt = datetime.combine(booking_date, end_time).replace(tzinfo=tz)

        except Exception as e:
            print(f"⚠️ Error parsing booking time: {e}")
            continue

        if start_dt <= now <= end_dt + timedelta(minutes=30):
            active_meeting = booking
            break

    if not active_meeting:
        await update.message.reply_text(
            "⏰ It’s not meeting time now or your meeting ended too long ago.\n"
            "You can only end meetings during or within 30 minutes after the scheduled time."
        )
        return

    booking_id = active_meeting["id"]

    def delete_from_db():
        with engine.begin() as conn:
            result = conn.execute(
                text("""
                    DELETE FROM bookings
                    WHERE id = :booking_id
                      AND telegram_user_id = :telegram_user_id
                    RETURNING id
                """),
                {
                    "booking_id": booking_id,
                    "telegram_user_id": user.id,
                },
            ).first()

            return result is not None

    deleted = await asyncio.to_thread(delete_from_db)

    if not deleted:
        await update.message.reply_text(
            "⚠️ Meeting could not be found or was already ended."
        )
        return

    ended_date = active_meeting["booking_date"].strftime("%d/%m/%Y")
    ended_time = (
        f"{active_meeting['start_time'].strftime('%H:%M')}-"
        f"{active_meeting['end_time'].strftime('%H:%M')}"
    )

    message = (
        f"🏁 *Meeting Ended!*\n"
        f"👤 {user.first_name}\n"
        f"📅 {ended_date} | ⏰ {ended_time}\n\n"
        f"✅ The meeting has officially ended."
    )

    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=message,
            parse_mode="Markdown",
        )

        await update.message.reply_text("✅ Meeting ended and announced to the group.")

        print(f"✅ Meeting ended for {user.first_name}: {ended_date} {ended_time}")

    except Exception as e:
        print(f"⚠️ Could not send group message: {e}")

        await update.message.reply_text(
            "⚠️ Meeting ended but could not announce to group."
        )


# ----------------- Stats -----------------


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    if user.id != ADMIN_ID:
        await update.message.reply_text(
            "🚫 You are not authorized to use this command."
        )
        return

    def read_stats():
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    text("""
                    SELECT
                        telegram_user_id,
                        user_name,
                        command,
                        created_at
                    FROM user_activity
                    ORDER BY created_at DESC
                """)
                )
                .mappings()
                .all()
            )

            return [dict(row) for row in rows]

    try:
        records = await asyncio.to_thread(read_stats)

        if not records:
            await update.message.reply_text("📊 No user activity data yet.")
            return

        summary = {}

        for row in records:
            name = row["user_name"]
            action = row["command"]
            created_at = row["created_at"]

            if name not in summary:
                summary[name] = {
                    "total": 0,
                    "actions": {},
                    "last_action": created_at,
                }

            summary[name]["total"] += 1
            summary[name]["actions"][action] = (
                summary[name]["actions"].get(action, 0) + 1
            )
            summary[name]["last_action"] = max(summary[name]["last_action"], created_at)

        sorted_users = sorted(
            summary.items(),
            key=lambda item: item[1]["last_action"],
            reverse=True,
        )

        def escape_md(text_value: str) -> str:
            for ch in ["_", "*", "`", "["]:
                text_value = text_value.replace(ch, f"\\{ch}")
            return text_value

        message = "📊 *All User Activity Summary:*\n\n"

        for name, info in sorted_users:
            actions_text = ", ".join(
                [f"{escape_md(cmd)}({count})" for cmd, count in info["actions"].items()]
            )

            last_action = info["last_action"]

            if last_action.tzinfo is None:
                last_action = last_action.replace(tzinfo=MEETING_TIMEZONE)
            else:
                last_action = last_action.astimezone(MEETING_TIMEZONE)

            last_text = last_action.strftime("%d/%m/%Y %H:%M:%S")

            message += (
                f"👤 *{escape_md(name)}*\n"
                f"🕒 Last: {last_text}\n"
                f"📈 Total: {info['total']}\n"
                f"📝 Actions: {actions_text}\n\n"
            )

        await update.message.reply_text(
            message,
            parse_mode="Markdown",
        )

    except Exception as e:
        print(f"⚠️ Error generating stats: {e}")
        await update.message.reply_text("⚠️ Could not retrieve stats.")


async def log_user_action(user, command):
    """Log user activity to PostgreSQL."""

    def save_log():
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO user_activity (
                        telegram_user_id,
                        user_name,
                        command
                    )
                    VALUES (
                        :telegram_user_id,
                        :user_name,
                        :command
                    )
                """),
                {
                    "telegram_user_id": user.id,
                    "user_name": user.first_name,
                    "command": command,
                },
            )

    try:
        await asyncio.to_thread(save_log)

        now = datetime.now(MEETING_TIMEZONE)
        now_str = now.strftime("%d/%m/%Y %H:%M:%S")

        print(f"✅ Logged {command} by {user.first_name} ({user.id}) at {now_str}")

    except Exception as e:
        print(f"⚠️ Could not log action: {e}")


# ----------------- Announce (admin) -----------------


async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != ADMIN_ID:
        await update.message.reply_text(
            "🚫 You are not authorized to use this command."
        )
        return ConversationHandler.END

    await update.message.reply_text("📝 Please type your announcement message:")
    return ANNOUNCE_MESSAGE


async def send_announcement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    message_text = update.message.text.strip()

    if user.id != ADMIN_ID:
        await update.message.reply_text(
            "🚫 You are not authorized to use this command."
        )
        return ConversationHandler.END

    if not message_text:
        await update.message.reply_text(
            "⚠️ Empty message, please type something or /cancel."
        )
        return ANNOUNCE_MESSAGE

    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID, text=message_text, parse_mode="Markdown"
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
        await update.message.reply_text(
            "🚫 You are not authorized to upload documents."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "📤 Please send the document file you want to upload (e.g., .docx, .pdf, .xlsx).",
        reply_markup=ReplyKeyboardRemove(),
    )
    return UPLOAD_DOC


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    document = update.message.document

    if not document:
        await update.message.reply_text("⚠️ Please send a valid document file.")
        return UPLOAD_DOC

    # Sanitize filename to prevent path traversal
    safe_name = os.path.basename(document.file_name or "")
    if not safe_name:
        await update.message.reply_text("⚠️ Invalid file name.")
        return UPLOAD_DOC

    try:
        os.makedirs("docs", exist_ok=True)
        docs_dir = os.path.realpath("docs")
        file_path = os.path.realpath(os.path.join("docs", safe_name))
        if os.path.commonpath([docs_dir, file_path]) != docs_dir:
            await update.message.reply_text("⚠️ Invalid file name.")
            return UPLOAD_DOC
        file = await document.get_file()
        await file.download_to_drive(file_path)
        await update.message.reply_text(
            f"✅ File saved: {safe_name}\nUsers can now access it with /docs."
        )
        print(f"✅ Admin uploaded {safe_name} to docs/")
    except Exception as e:
        await update.message.reply_text("⚠️ Failed to save the file.")
        print(f"⚠️ Error saving file: {e}")

    return ConversationHandler.END


async def topdf_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    await log_user_action(user, "/topdf")
    await update.message.reply_text(
        "📝 Please type the output PDF file name first (example: meeting_report).\n"
        "I will add .pdf automatically.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PDF_NAME_INPUT


async def receive_pdf_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return PDF_NAME_INPUT

    if not message.text:
        await message.reply_text("⚠️ Please type a valid file name.")
        return PDF_NAME_INPUT

    normalized_name = _normalize_output_pdf_name(message.text.strip())
    if not normalized_name:
        await message.reply_text(
            "⚠️ Invalid file name. Use letters, numbers, dot, dash, or underscore only."
        )
        return PDF_NAME_INPUT

    context.user_data["topdf_output_name"] = normalized_name
    await message.reply_text(
        f"✅ Output name set to: {normalized_name}\n"
        "📎 Now send an image or Word file (.doc/.docx) to convert.",
    )
    return CONVERT_TO_PDF


async def receive_file_for_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return CONVERT_TO_PDF

    telegram_file = None
    source_name = None
    mime_type = None
    allowed_image_exts = {
        ".jpg",
        ".jpeg",
        ".png",
        ".bmp",
        ".gif",
        ".webp",
        ".tif",
        ".tiff",
    }
    allowed_word_exts = {".doc", ".docx"}
    allowed_word_mimes = {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }

    if message.document:
        doc = message.document
        raw_name = doc.file_name or "document"
        source_name = _normalize_pdf_source_name(raw_name)
        ext = os.path.splitext(source_name)[1].lower()
        mime_type = doc.mime_type

        is_image_file = ext in allowed_image_exts or (
            mime_type and mime_type.startswith("image/")
        )
        is_word_file = ext in allowed_word_exts or (mime_type in allowed_word_mimes)
        if not (is_image_file or is_word_file):
            await message.reply_text(
                "⚠️ Unsupported file type. Please upload an image file or a Word file (.doc/.docx) only."
            )
            return CONVERT_TO_PDF

        telegram_file = await doc.get_file()
    elif message.photo:
        photo = message.photo[-1]
        telegram_file = await photo.get_file()
        source_name = f"photo_{photo.file_unique_id}.jpg"
        mime_type = "image/jpeg"
    else:
        await message.reply_text("⚠️ Please send an image or Word file (.doc/.docx).")
        return CONVERT_TO_PDF

    with tempfile.TemporaryDirectory(prefix="pdf_convert_") as temp_dir:
        source_path = os.path.join(temp_dir, source_name)
        await telegram_file.download_to_drive(source_path)

        pdf_path, error = await asyncio.to_thread(
            _convert_to_pdf_sync,
            source_path,
            source_name,
            mime_type,
            temp_dir,
        )

        if error:
            await message.reply_text(f"⚠️ {error}")
            return CONVERT_TO_PDF

        if not pdf_path or not os.path.exists(pdf_path):
            await message.reply_text("⚠️ Conversion failed. Please try another file.")
            return CONVERT_TO_PDF

        pdf_name = (
            context.user_data.get("topdf_output_name")
            or f"{os.path.splitext(source_name)[0]}.pdf"
        )
        with open(pdf_path, "rb") as f:
            await message.reply_document(
                document=InputFile(f, filename=pdf_name),
            )

    context.user_data.pop("topdf_output_name", None)
    return ConversationHandler.END


# ----------------- User Download Docs (Inline Keyboard) -----------------


async def docs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    await log_user_action(user, "/docs")

    try:
        files = [f for f in os.listdir("docs") if f != ".keep"]
    except Exception:
        files = []

    if not files:
        await update.message.reply_text(
            "📂 No documents available yet. Ask the admin to upload some."
        )
        return

    keyboard = [
        [InlineKeyboardButton(f"📄 {f}", callback_data=f"docs:{f}")] for f in files
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📁 Please choose a document to download:", reply_markup=reply_markup
    )


async def handle_docs_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("docs:"):
        return

    # Sanitize filename to prevent path traversal
    raw_name = data.split("docs:", 1)[1]
    filename = os.path.basename(raw_name)
    if not filename:
        await query.message.reply_text("⚠️ I couldn’t find that file. Try /docs again.")
        return

    docs_dir = os.path.realpath("docs")
    file_path = os.path.realpath(os.path.join("docs", filename))
    if os.path.commonpath([docs_dir, file_path]) != docs_dir:
        await query.message.reply_text("⚠️ I couldn’t find that file. Try /docs again.")
        return

    if not os.path.exists(file_path):
        await query.message.reply_text("⚠️ I couldn’t find that file. Try /docs again.")
        return

    try:
        with open(file_path, "rb") as f:
            await query.message.reply_document(
                document=InputFile(f, filename=filename),
                caption=f"📘 Here’s your document: {filename}",
            )
        print(f"✅ Sent {filename} to {query.from_user.first_name}")
    except Exception as e:
        await query.message.reply_text("⚠️ Failed to send the document.")
        print(f"⚠️ Error sending document: {e}")


# ----------------- Auto Cleanup -----------------


async def auto_cleanup(
    update: Update = None,
    context: ContextTypes.DEFAULT_TYPE = None,
):
    """
    Remove expired bookings from PostgreSQL.

    Works when called manually with /clean
    and when called automatically by JobQueue.
    """

    # JobQueue may pass context as the first positional argument
    if context is None and update is not None and not hasattr(update, "message"):
        context = update
        update = None

    tz = MEETING_TIMEZONE
    now = datetime.now(tz)

    def cleanup_db():
        with engine.begin() as conn:
            rows = (
                conn.execute(
                    text("""
                    SELECT
                        id,
                        booking_date,
                        start_time,
                        end_time,
                        user_name
                    FROM bookings
                    WHERE status = 'BOOKED'
                    ORDER BY booking_date, start_time
                """)
                )
                .mappings()
                .all()
            )

            expired_ids = []
            removed = []

            for row in rows:
                meeting_end = datetime.combine(
                    row["booking_date"],
                    row["end_time"],
                ).replace(tzinfo=tz)

                if meeting_end < now:
                    expired_ids.append(row["id"])

                    date_text = row["booking_date"].strftime("%d/%m/%Y")
                    time_text = (
                        f"{row['start_time'].strftime('%H:%M')}-"
                        f"{row['end_time'].strftime('%H:%M')}"
                    )

                    removed.append(f"{date_text} | {time_text} | {row['user_name']}")

            if expired_ids:
                conn.execute(
                    text("""
                        DELETE FROM bookings
                        WHERE id = ANY(:expired_ids)
                    """),
                    {
                        "expired_ids": expired_ids,
                    },
                )

            remaining_rows = (
                conn.execute(
                    text("""
                    SELECT
                        id,
                        telegram_user_id,
                        user_name,
                        booking_date,
                        start_time,
                        end_time,
                        status
                    FROM bookings
                    WHERE status = 'BOOKED'
                    ORDER BY booking_date, start_time
                """)
                )
                .mappings()
                .all()
            )

            return removed, [dict(row) for row in remaining_rows]

    try:
        removed, remaining_records = await asyncio.to_thread(cleanup_db)

    except Exception as e:
        print(f"⚠️ auto_cleanup database error: {e}")

        if update and getattr(update, "message", None):
            await update.message.reply_text("⚠️ Cleanup failed due to a database error.")

        return

    if not removed:
        print("✅ No expired meetings found during cleanup.")

        if update and getattr(update, "message", None):
            await update.message.reply_text(
                "✨ There are no expired bookings to clean up."
            )

        return

    message = "📋 *Current Schedule:*\n"
    if remaining_records:
        for row in remaining_records:
            date_text = row["booking_date"].strftime("%d/%m/%Y")
            time_text = (
                f"{row['start_time'].strftime('%H:%M')}-"
                f"{row['end_time'].strftime('%H:%M')}"
            )
            user_name = (row["user_name"] or "User").strip()
            first_name = user_name.split(maxsplit=1)[0]

            message += f"{date_text} | {time_text} | {first_name}\n"

    else:
        message += "✅ No upcoming meetings."

    if context:
        try:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=message,
                parse_mode="Markdown",
            )

        except Exception as e:
            print(f"⚠️ Could not send cleanup message: {e}")

    if update and getattr(update, "message", None):
        await update.message.reply_text(
            "✅ Cleanup completed and expired bookings were removed."
        )

    print(f"✅ Auto cleanup removed {len(removed)} expired booking(s).")


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
    await _delete_booking_prompt(context)
    await update.message.reply_text(
        "↩️ Conversation cancelled.", reply_markup=ReplyKeyboardRemove()
    )
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


async def handle_cancel_info_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send cancellation details privately to whoever taps the 'Who cancelled?' button."""
    query = update.callback_query
    data = query.data or ""

    if not data.startswith("cancel_info:"):
        await query.answer()
        return

    detail_key = data.split("cancel_info:", 1)[1]
    details = cancel_details_store.get(detail_key)

    if not details:
        await query.answer("ℹ️ Details are no longer available.", show_alert=True)
        return

    tapper = query.from_user
    await log_user_action(tapper, "/cancel_info")
    private_message = (
        f"🗑️ *Cancellation Details:*\n\n"
        f"👤 Cancelled by: *{details['name']}*\n"
        f"📅 Date: {details['date']}\n"
        f"⏰ Time: {details['time']}"
    )

    take_slot_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🙋 Take this slot", callback_data=f"take_slot:{detail_key}"
                )
            ]
        ]
    )

    try:
        await context.bot.send_message(
            chat_id=tapper.id,
            text=private_message,
            parse_mode="Markdown",
            reply_markup=take_slot_keyboard,
        )
        await query.answer("✅ Details sent to your private chat!", show_alert=False)
    except Exception as e:
        await query.answer(
            "⚠️ Please start the bot privately first, then try again.",
            show_alert=True,
        )
        print(f"⚠️ Could not send cancel detail to {tapper.first_name}: {e}")


async def handle_take_slot_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Book the cancelled slot for the user who taps 'Take this slot'."""
    query = update.callback_query
    data = query.data or ""

    if not data.startswith("take_slot:"):
        await query.answer()
        return

    detail_key = data.split("take_slot:", 1)[1]
    details = cancel_details_store.get(detail_key)

    if not details:
        await query.answer("ℹ️ This slot is no longer available.", show_alert=True)
        return

    taker = query.from_user
    date_str = details["date"]
    time_str = details["time"]

    # Check if the slot has already expired
    try:
        tz = MEETING_TIMEZONE
        start_str = time_str.split("-")[0].strip()
        slot_start = datetime.strptime(
            f"{date_str} {start_str}", "%d/%m/%Y %H:%M"
        ).replace(tzinfo=tz)
        if slot_start < datetime.now(tz):
            await query.answer(
                "⏰ This slot has already expired and cannot be booked.",
                show_alert=True,
            )
            return
    except Exception:
        pass

    result = await save_booking(date_str, time_str, taker.first_name, taker.id)

    if result == "overlap":
        # Slot is taken — remove from store so no one else can attempt via this button
        cancel_details_store.pop(detail_key, None)
        await query.answer(
            "⚠️ This slot has already been taken by someone else.", show_alert=True
        )
        return
    elif result == "invalid":
        await query.answer(
            "❌ Could not book this slot. Invalid time format.", show_alert=True
        )
        return

    await log_user_action(taker, "/take_slot")

    # Remove slot from store so it can't be double-booked via this button
    cancel_details_store.pop(detail_key, None)

    # Edit the private message to confirm
    try:
        await query.edit_message_text(
            f"✅ *Slot booked successfully!*\n\n📅 {date_str} | ⏰ {time_str}",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await query.answer("✅ Slot booked!", show_alert=False)

    # Announce new booking to group with updated schedule
    try:
        records = await get_all_bookings()

        group_message = (
            f"📢 *New Booking Added!*\n\n"
            f"👤 {taker.first_name}\n"
            f"🗓 {date_str} | ⏰ {time_str}\n\n"
            f"📋 *Current Schedule:*\n"
        )
        for row in records:
            date_text = row["booking_date"].strftime("%d/%m/%Y")
            time_text = (
                f"{row['start_time'].strftime('%H:%M')}-"
                f"{row['end_time'].strftime('%H:%M')}"
            )

            group_message += f"{date_text} | {time_text} | {row['user_name']}\n"
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=group_message,
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"⚠️ Could not send group message for taken slot: {e}")


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

    # Keep the Telegram command menu minimal. Other handlers remain available
    # when users type their commands directly.
    user_commands = [
        BotCommand("start", "Start"),
    ]

    # Reset the previous admin-specific menu so it inherits the default menu.
    async def set_commands(application):
        await application.bot.set_my_commands(
            user_commands, scope=BotCommandScopeDefault()
        )
        await application.bot.delete_my_commands(
            scope=BotCommandScopeChat(ADMIN_ID)
        )
        print("✅ Command menu set to /start only.")

        # Clear webhook safely
        await clear_webhook(TOKEN)

    app.post_init = set_commands

    # Conversations
    fallback_list = [CommandHandler("cancel", conv_cancel)]

    book_conv = ConversationHandler(
        entry_points=[CommandHandler("book", book)],
        states={
            SELECT_MONTH: [
                CallbackQueryHandler(handle_month_selection, pattern="^month:")
            ],
            SELECT_DAY: [
                CallbackQueryHandler(handle_day_selection, pattern="^day:"),
                CallbackQueryHandler(handle_month_selection, pattern="^month:"),
            ],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_time)],
        },
        fallbacks=fallback_list,
        per_chat=True,
        per_user=True,
    )

    cancel_conv = ConversationHandler(
        entry_points=[CommandHandler("cancel", cancel)],
        states={
            CANCEL_SELECT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, delete_booking_by_number
                )
            ],
        },
        fallbacks=fallback_list,
        per_chat=True,
        per_user=True,
    )

    announce_conv = ConversationHandler(
        entry_points=[CommandHandler("announce", announce)],
        states={
            ANNOUNCE_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, send_announcement)
            ],
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

    topdf_conv = ConversationHandler(
        entry_points=[CommandHandler("topdf", topdf_start)],
        states={
            PDF_NAME_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pdf_name)
            ],
            CONVERT_TO_PDF: [
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO, receive_file_for_pdf
                )
            ],
        },
        fallbacks=fallback_list,
        per_user=True,
        per_chat=True,
    )

    # Register handlers
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(book_conv)
    app.add_handler(cancel_conv)
    app.add_handler(CommandHandler("end", end_meeting))
    app.add_handler(announce_conv)
    app.add_handler(CommandHandler("clean", auto_cleanup))
    app.add_handler(upload_conv)
    app.add_handler(topdf_conv)
    app.add_handler(CommandHandler("docs", docs_menu))
    app.add_handler(CallbackQueryHandler(handle_docs_button, pattern="^docs:"))
    app.add_handler(
        CallbackQueryHandler(handle_cancel_info_button, pattern="^cancel_info:")
    )
    app.add_handler(
        CallbackQueryHandler(handle_take_slot_button, pattern="^take_slot:")
    )
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member)
    )

    # Schedule auto cleanup every hour
    job_queue.run_repeating(auto_cleanup, interval=3600, first=10)
    print("🕒 Auto-cleanup scheduled every 1 hour.")

    use_webhook = os.getenv("USE_WEBHOOK", "false").lower() == "true"
    if use_webhook:
        webhook_url = os.getenv("WEBHOOK_URL")
        webapp_host = os.getenv("WEBAPP_HOST", "0.0.0.0")
        # Railway provides PORT, fallback to WEBAPP_PORT for other platforms
        webapp_port = int(os.getenv("PORT") or os.getenv("WEBAPP_PORT", "8080"))
        secret_token = os.getenv("WEBHOOK_SECRET_TOKEN")

        if not webhook_url:
            raise RuntimeError("USE_WEBHOOK=true but WEBHOOK_URL is not set in .env")

        print(f"✅ Starting webhook at {webapp_host}:{webapp_port} -> {webhook_url}")

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
                    "⚠️ Conflict: Another bot instance is polling. Please stop other running processes and run a single instance."
                )
                print(
                    "Hint: In PowerShell, run: Get-Process python* | Stop-Process -Force"
                )
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
                    bot, f"⚠️ [Bot Alert]\n\nBot stopped or crashed.\nError: {e}"
                )
            )
        except Exception as inner_e:
            print(f"⚠️ Failed to send crash alert: {inner_e}")
