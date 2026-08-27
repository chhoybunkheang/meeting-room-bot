import hashlib
import hmac
import json
import os
from datetime import datetime
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text

# =========================================================
# CONFIG
# =========================================================

load_dotenv(override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured")


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

app = FastAPI(title="Meeting Room Mini App")

templates = Jinja2Templates(directory="templates")


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
async def home(request: Request):

    with engine.connect() as conn:
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
                    ORDER BY booking_date, start_time
                """)
            )
            .mappings()
            .all()
        )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "bookings": bookings,
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

    return RedirectResponse(
        url="/",
        status_code=303,
    )


@app.post("/my-bookings", response_class=HTMLResponse)
async def my_bookings(
    request: Request,
    telegram_init_data: str = Form(...),
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

    with engine.connect() as conn:
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
                    ORDER BY booking_date, start_time
                """),
                {
                    "telegram_user_id": telegram_user_id,
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

    return RedirectResponse(
        url="/",
        status_code=303,
    )
