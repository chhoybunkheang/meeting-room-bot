import hashlib
import hmac
import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text
from telegram import Bot

# =========================================================
# CONFIG
# =========================================================

load_dotenv(override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured")

if not GROUP_CHAT_ID:
    raise RuntimeError("GROUP_CHAT_ID is not configured")


logger = logging.getLogger(__name__)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

app = FastAPI(title="Meeting Room Mini App")

BASE_DIR = Path(__file__).resolve().parent

app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static",
)

templates = Jinja2Templates(directory=BASE_DIR / "templates")

BOOKINGS_PER_PAGE = 10
SCHEDULE_NOTIFICATION_LIMIT = 20


def format_current_schedule() -> str:
    """Build a compact upcoming schedule for group notifications."""
    with engine.connect() as conn:
        total_bookings = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM bookings
                WHERE status = 'BOOKED'
                  AND booking_date >= CURRENT_DATE
            """)
        ).scalar_one()

        bookings = (
            conn.execute(
                text("""
                    SELECT
                        user_name,
                        booking_date,
                        start_time,
                        end_time
                    FROM bookings
                    WHERE status = 'BOOKED'
                      AND booking_date >= CURRENT_DATE
                    ORDER BY booking_date, start_time
                    LIMIT :limit
                """),
                {"limit": SCHEDULE_NOTIFICATION_LIMIT},
            )
            .mappings()
            .all()
        )

    if not bookings:
        return "📋 Current Schedule\nNo upcoming bookings."

    schedule_lines = ["📋 Current Schedule"]
    schedule_lines.extend(
        f"{position}. {booking['booking_date'].strftime('%d/%m/%Y')} | "
        f"{booking['start_time'].strftime('%H:%M')}–"
        f"{booking['end_time'].strftime('%H:%M')} | "
        f"{booking['user_name']}"
        for position, booking in enumerate(bookings, start=1)
    )

    remaining = total_bookings - len(bookings)
    if remaining > 0:
        schedule_lines.append(f"…and {remaining} more upcoming booking(s).")

    return "\n".join(schedule_lines)


async def notify_group(message: str) -> None:
    """Send a Mini App booking update without failing the user action."""
    try:
        async with Bot(token=BOT_TOKEN) as bot:
            await bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=message,
            )
    except Exception:
        logger.exception("Could not send booking notification to the group")


# =========================================================
# TELEGRAM AUTHENTICATION
# =========================================================


def validate_telegram_init_data(init_data: str):
    """
    Validate Telegram Mini App initData.

    Returns the authenticated Telegram user dictionary
    if the signature is valid.
    """

    if not init_data:
        raise ValueError("Missing Telegram initData")

    parsed = dict(
        parse_qsl(
            init_data,
            keep_blank_values=True,
        )
    )

    received_hash = parsed.pop("hash", None)

    if not received_hash:
        raise ValueError("Missing Telegram hash")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(parsed.items())
    )

    secret_key = hmac.new(
        key=b"WebAppData",
        msg=BOT_TOKEN.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        calculated_hash,
        received_hash,
    ):
        raise ValueError("Invalid Telegram initData")

    user_json = parsed.get("user")

    if not user_json:
        raise ValueError("Telegram user not found")

    try:
        return json.loads(user_json)

    except json.JSONDecodeError as exc:
        raise ValueError("Invalid Telegram user data") from exc


# =========================================================
# HOME
# =========================================================


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, page: int = 1):

    page = max(page, 1)

    with engine.connect() as conn:
        total_bookings = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM bookings
                WHERE status = 'BOOKED'
                  AND booking_date >= CURRENT_DATE
            """)
        ).scalar_one()
        total_pages = max(
            1,
            math.ceil(total_bookings / BOOKINGS_PER_PAGE),
        )
        page = min(page, total_pages)

        bookings = (
            conn.execute(
                text("""
                    SELECT
                        id,
                        telegram_user_id,
                        user_name,
                        booking_date,
                        start_time,
                        end_time
                    FROM bookings
                    WHERE status = 'BOOKED'
                      AND booking_date >= CURRENT_DATE
                    ORDER BY booking_date, start_time
                    LIMIT :limit OFFSET :offset
                """),
                {
                    "limit": BOOKINGS_PER_PAGE,
                    "offset": (page - 1) * BOOKINGS_PER_PAGE,
                },
            )
            .mappings()
            .all()
        )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "bookings": bookings,
            "page": page,
            "total_pages": total_pages,
        },
    )


# =========================================================
# CREATE BOOKING
# =========================================================


@app.post("/book")
async def create_booking(
    telegram_init_data: str = Form(...),
    booking_date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
):

    # -----------------------------------------------------
    # Authenticate Telegram user
    # -----------------------------------------------------

    try:
        telegram_user = validate_telegram_init_data(telegram_init_data)

    except ValueError as e:
        return HTMLResponse(
            f"""
            <h2>Telegram authentication failed</h2>
            <p>{e}</p>
            <a href="/">Back</a>
            """,
            status_code=401,
        )

    telegram_user_id = telegram_user["id"]

    user_name = " ".join(
        [
            telegram_user.get("first_name", ""),
            telegram_user.get("last_name", ""),
        ]
    ).strip()

    if not user_name:
        user_name = telegram_user.get(
            "username",
            "Telegram User",
        )

    # -----------------------------------------------------
    # Validate booking date/time
    # -----------------------------------------------------

    try:
        booking_date_value = datetime.strptime(
            booking_date,
            "%Y-%m-%d",
        ).date()

        start_time_value = datetime.strptime(
            start_time,
            "%H:%M",
        ).time()

        end_time_value = datetime.strptime(
            end_time,
            "%H:%M",
        ).time()

    except ValueError:
        return HTMLResponse(
            """
            <h2>Invalid booking information</h2>
            <p>Please check the date and time.</p>
            <a href="/">Back</a>
            """,
            status_code=400,
        )

    if end_time_value <= start_time_value:
        return HTMLResponse(
            """
            <h2>Invalid Time</h2>
            <p>End time must be later than start time.</p>
            <a href="/">Back</a>
            """,
            status_code=400,
        )

    if booking_date_value < datetime.now().date():
        return HTMLResponse(
            """
            <h2>Invalid Date</h2>
            <p>You cannot book a past date.</p>
            <a href="/">Back</a>
            """,
            status_code=400,
        )

    # -----------------------------------------------------
    # Save booking
    # -----------------------------------------------------

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
                "booking_date": booking_date_value,
                "start_time": start_time_value,
                "end_time": end_time_value,
            },
        ).scalar()

        if overlap:
            return HTMLResponse(
                """
                <h2>⚠️ Booking Conflict</h2>

                <p>
                    This time overlaps with
                    another booking.
                </p>

                <a href="/">
                    Back to schedule
                </a>
                """,
                status_code=409,
            )

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
                "telegram_user_id": telegram_user_id,
                "user_name": user_name,
                "booking_date": booking_date_value,
                "start_time": start_time_value,
                "end_time": end_time_value,
            },
        )

    await notify_group(
        "📢 New booking\n\n"
        f"👤 {user_name}\n"
        f"📅 {booking_date_value.strftime('%d/%m/%Y')}\n"
        f"⏰ {start_time_value.strftime('%H:%M')}–"
        f"{end_time_value.strftime('%H:%M')}\n\n"
        f"{format_current_schedule()}"
    )

    return RedirectResponse(
        url="/",
        status_code=303,
    )


@app.post("/my-bookings", response_class=HTMLResponse)
async def my_bookings(
    request: Request,
    telegram_init_data: str = Form(...),
    page: int = Form(1),
):
    try:
        telegram_user = validate_telegram_init_data(telegram_init_data)

    except ValueError as e:
        return HTMLResponse(
            f"""
            <h2>Telegram authentication failed</h2>
            <p>{e}</p>
            <a href="/">Back</a>
            """,
            status_code=401,
        )

    telegram_user_id = telegram_user["id"]
    page = max(page, 1)

    with engine.connect() as conn:
        total_bookings = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM bookings
                WHERE telegram_user_id = :telegram_user_id
                  AND status = 'BOOKED'
                  AND booking_date >= CURRENT_DATE
            """),
            {"telegram_user_id": telegram_user_id},
        ).scalar_one()
        total_pages = max(
            1,
            math.ceil(total_bookings / BOOKINGS_PER_PAGE),
        )
        page = min(page, total_pages)

        bookings = (
            conn.execute(
                text("""
                    SELECT
                        id,
                        user_name,
                        booking_date,
                        start_time,
                        end_time
                    FROM bookings
                    WHERE telegram_user_id = :telegram_user_id
                      AND status = 'BOOKED'
                      AND booking_date >= CURRENT_DATE
                    ORDER BY booking_date, start_time
                    LIMIT :limit OFFSET :offset
                """),
                {
                    "telegram_user_id": telegram_user_id,
                    "limit": BOOKINGS_PER_PAGE,
                    "offset": (page - 1) * BOOKINGS_PER_PAGE,
                },
            )
            .mappings()
            .all()
        )

    return templates.TemplateResponse(
        request=request,
        name="my_bookings.html",
        context={
            "bookings": bookings,
            "page": page,
            "total_pages": total_pages,
        },
    )


@app.post("/cancel-booking")
async def cancel_booking(
    telegram_init_data: str = Form(""),
    booking_id: str = Form(""),
):

    if not telegram_init_data:
        return HTMLResponse(
            """
            <h2>Authentication data missing</h2>
            <p>Telegram initData was not submitted.</p>
            <a href="/">Back</a>
            """,
            status_code=400,
        )

    try:
        booking_id_value = int(booking_id)
    except (TypeError, ValueError):
        booking_id_value = 0

    if booking_id_value <= 0:
        return HTMLResponse(
            """
            <h2>Booking ID missing</h2>
            <p>The booking ID was not submitted.</p>
            <a href="/">Back</a>
            """,
            status_code=400,
        )

    try:
        telegram_user = validate_telegram_init_data(telegram_init_data)

    except ValueError as e:
        return HTMLResponse(
            f"""
            <h2>Telegram authentication failed</h2>
            <p>{e}</p>
            <a href="/">Back</a>
            """,
            status_code=401,
        )

    telegram_user_id = telegram_user["id"]

    with engine.begin() as conn:
        booking = (
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
                    WHERE id = :booking_id
                """),
                {
                    "booking_id": booking_id_value,
                },
            )
            .mappings()
            .first()
        )

        if not booking:
            return HTMLResponse(
                """
                <h2>Booking not found</h2>
                <a href="/">Back</a>
                """,
                status_code=404,
            )

        if booking["telegram_user_id"] != telegram_user_id:
            return HTMLResponse(
                """
                <h2>Not allowed</h2>
                <p>You can only cancel your own booking.</p>
                <a href="/">Back</a>
                """,
                status_code=403,
            )

        if booking["status"] != "BOOKED":
            return HTMLResponse(
                """
                <h2>Booking already cancelled</h2>
                <a href="/">Back</a>
                """,
                status_code=400,
            )

        conn.execute(
            text("""
                UPDATE bookings
                SET status = 'CANCELLED'
                WHERE id = :booking_id
            """),
            {
                "booking_id": booking_id_value,
            },
        )

    await notify_group(
        "🗑️ Booking cancelled\n\n"
        f"👤 {booking['user_name']}\n"
        f"📅 {booking['booking_date'].strftime('%d/%m/%Y')}\n"
        f"⏰ {booking['start_time'].strftime('%H:%M')}–"
        f"{booking['end_time'].strftime('%H:%M')}\n\n"
        f"{format_current_schedule()}"
    )

    return RedirectResponse(
        url="/",
        status_code=303,
    )
