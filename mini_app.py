import logging
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from telegram import Bot
from booking_validation import validate_booking_interval
from exchange_rates import fetch_latest_gdt_rate, format_rate, load_exchange_rates
from telegram_auth import validate_telegram_init_data as validate_signed_init_data

# =========================================================
# CONFIG
# =========================================================

load_dotenv(override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TELEGRAM_INIT_DATA_MAX_AGE = int(os.getenv("TELEGRAM_INIT_DATA_MAX_AGE", "3600"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured")

if not GROUP_CHAT_ID:
    raise RuntimeError("GROUP_CHAT_ID is not configured")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is not configured")


logger = logging.getLogger(__name__)

BOT_USERNAME_CACHE = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
BOT_USERNAME_CACHE_EXPIRES_AT = 0.0
BOT_USERNAME_CACHE_SECONDS = 3600
BOT_USERNAME_LOCK = None

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

app = FastAPI(title="Meeting Room Mini App")


@app.middleware("http")
async def add_performance_headers(request: Request, call_next):
    """Expose server processing time for monitoring and load tests."""
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.2f}"
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"
    return response

BASE_DIR = Path(__file__).resolve().parent
EXCHANGE_RATE_WORKBOOK = Path(
    os.getenv("EXCHANGE_RATE_WORKBOOK", BASE_DIR / "data" / "Exchange Rate.xlsx")
)

app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static",
)

templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["rate"] = format_rate

BOOKINGS_PER_PAGE = 10
SCHEDULE_NOTIFICATION_LIMIT = 20
MEETING_TIMEZONE = ZoneInfo(
    os.getenv("MEETING_TIMEZONE", "Asia/Phnom_Penh")
)
ROOM_BLOCK_PREFIX = "🔒 Room Blocked"


def current_meeting_datetime() -> datetime:
    """Return the current time in the configured meeting-room timezone."""
    return datetime.now(MEETING_TIMEZONE)


def notification_first_name(user_name: str) -> str:
    """Return a privacy-friendly name for Telegram group messages."""
    normalized_name = (user_name or "").strip()
    if normalized_name.startswith(ROOM_BLOCK_PREFIX):
        return ROOM_BLOCK_PREFIX
    return normalized_name.split(maxsplit=1)[0] if normalized_name else "User"


templates.env.globals["first_name"] = notification_first_name


def get_user_room_status() -> dict:
    """Return the live room status displayed in the shared user header."""
    meeting_now = current_meeting_datetime()
    current_date = meeting_now.date()
    current_time = meeting_now.time().replace(tzinfo=None)

    try:
        with engine.connect() as conn:
            next_slot = (
                conn.execute(
                    text("""
                        SELECT user_name, booking_date, start_time, end_time
                        FROM bookings
                        WHERE status = 'BOOKED'
                          AND (
                              booking_date > :current_date
                              OR (
                                  booking_date = :current_date
                                  AND end_time > :current_time
                              )
                          )
                        ORDER BY booking_date, start_time
                        LIMIT 1
                    """),
                    {
                        "current_date": current_date,
                        "current_time": current_time,
                    },
                )
                .mappings()
                .first()
            )
    except Exception:
        logger.exception("Could not load room status for the user header")
        return {
            "type": "unknown",
            "label": "Status unavailable",
            "detail": "Please check the schedule below",
        }

    if (
        next_slot
        and next_slot["booking_date"] == current_date
        and next_slot["start_time"] <= current_time
    ):
        is_blocked = next_slot["user_name"].startswith(ROOM_BLOCK_PREFIX)
        return {
            "type": "blocked" if is_blocked else "booked",
            "label": "Blocked" if is_blocked else "Currently Booked",
            "detail": f"Until {next_slot['end_time'].strftime('%H:%M')}",
        }

    if next_slot and next_slot["booking_date"] == current_date:
        detail = f"Available until {next_slot['start_time'].strftime('%H:%M')}"
    else:
        detail = "Ready to book"

    return {"type": "available", "label": "Available", "detail": detail}


templates.env.globals["get_user_room_status"] = get_user_room_status


async def get_bot_username() -> str:
    """Return the current Telegram bot username with a short-lived cache."""
    global BOT_USERNAME_CACHE, BOT_USERNAME_CACHE_EXPIRES_AT, BOT_USERNAME_LOCK

    now = time.monotonic()
    if BOT_USERNAME_CACHE and now < BOT_USERNAME_CACHE_EXPIRES_AT:
        return BOT_USERNAME_CACHE

    if BOT_USERNAME_LOCK is None:
        import asyncio
        BOT_USERNAME_LOCK = asyncio.Lock()

    async with BOT_USERNAME_LOCK:
        now = time.monotonic()
        if BOT_USERNAME_CACHE and now < BOT_USERNAME_CACHE_EXPIRES_AT:
            return BOT_USERNAME_CACHE
        try:
            async with Bot(token=BOT_TOKEN) as bot:
                bot_user = await bot.get_me()
            if bot_user.username:
                BOT_USERNAME_CACHE = bot_user.username
                BOT_USERNAME_CACHE_EXPIRES_AT = now + BOT_USERNAME_CACHE_SECONDS
        except Exception:
            logger.exception("Could not refresh Telegram bot username")
            BOT_USERNAME_CACHE_EXPIRES_AT = now + 300

    return BOT_USERNAME_CACHE or "TelegramBot"


@app.get("/health")
async def health():
    """Check application and database readiness without blocking the event loop."""
    started = time.perf_counter()

    def check_database():
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    try:
        await run_in_threadpool(check_database)
    except Exception:
        logger.exception("Database health check failed")
        raise HTTPException(status_code=503, detail="Database unavailable")

    return {
        "status": "ok",
        "database": "ok",
        "database_latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def authenticate_admin(init_data: str) -> dict:
    """Validate Telegram Mini App data and require the configured admin."""
    try:
        user = validate_telegram_init_data(init_data)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    if int(user.get("id", 0)) != ADMIN_ID:
        raise HTTPException(status_code=403, detail="Admin access required")

    return user


def record_activity(user: dict, action: str) -> None:
    """Reuse the existing user_activity table for Mini App admin actions."""
    try:
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
                    "telegram_user_id": user["id"],
                    "user_name": user.get("first_name", "Admin"),
                    "command": action,
                },
            )
    except Exception:
        logger.exception("Could not record admin activity")


def parse_booking_datetime_values(
    booking_date: str,
    start_time: str,
    end_time: str,
):
    try:
        booking_date_value = datetime.strptime(booking_date, "%Y-%m-%d").date()
        start_time_value = datetime.strptime(start_time, "%H:%M").time()
        end_time_value = datetime.strptime(end_time, "%H:%M").time()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date or time") from exc

    try:
        validate_booking_interval(
            booking_date_value,
            start_time_value,
            end_time_value,
            current_meeting_datetime(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return booking_date_value, start_time_value, end_time_value


def find_overlapping_booking(
    conn,
    booking_date,
    start_time,
    end_time,
    exclude_booking_id: int = 0,
):
    return (
        conn.execute(
            text("""
                SELECT id, user_name, start_time, end_time
                FROM bookings
                WHERE booking_date = :booking_date
                  AND status = 'BOOKED'
                  AND start_time < :end_time
                  AND end_time > :start_time
                  AND (:exclude_booking_id = 0 OR id != :exclude_booking_id)
                ORDER BY start_time
                LIMIT 1
            """),
            {
                "booking_date": booking_date,
                "start_time": start_time,
                "end_time": end_time,
                "exclude_booking_id": exclude_booking_id,
            },
        )
        .mappings()
        .first()
    )


def insert_booking(conn, values: dict) -> bool:
    """Insert a booking and report a database-enforced overlap conflict."""
    try:
        with conn.begin_nested():
            conn.execute(
                text("""
                    INSERT INTO bookings (
                        telegram_user_id, user_name, booking_date,
                        start_time, end_time, status
                    ) VALUES (
                        :telegram_user_id, :user_name, :booking_date,
                        :start_time, :end_time, 'BOOKED'
                    )
                """),
                values,
            )
    except IntegrityError as exc:
        if "bookings_no_active_overlap" in str(exc.orig):
            return False
        raise
    return True


def cancel_active_booking(
    booking_id: int,
    allowed_user_id: int | None = None,
    block_requirement: bool | None = None,
):
    """Shared cancellation transition for users and admin actions."""
    with engine.begin() as conn:
        booking = (
            conn.execute(
                text("""
                    SELECT id, telegram_user_id, user_name, booking_date,
                           start_time, end_time, status
                    FROM bookings
                    WHERE id = :booking_id
                """),
                {"booking_id": booking_id},
            )
            .mappings()
            .first()
        )

        if not booking:
            return "not_found", None
        if allowed_user_id is not None and booking["telegram_user_id"] != allowed_user_id:
            return "forbidden", booking
        if booking["status"] != "BOOKED":
            return "inactive", booking

        is_block = booking["user_name"].startswith(ROOM_BLOCK_PREFIX)
        if block_requirement is not None and is_block != block_requirement:
            return "wrong_type", booking

        result = conn.execute(
            text("""
                UPDATE bookings
                SET status = 'CANCELLED'
                WHERE id = :booking_id
                  AND status = 'BOOKED'
            """),
            {"booking_id": booking_id},
        )
        if result.rowcount != 1:
            return "inactive", booking

    return "success", booking


def format_current_schedule() -> str:
    """Build a compact upcoming schedule for group notifications."""
    current_date = current_meeting_datetime().date()
    with engine.connect() as conn:
        total_bookings = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM bookings
                WHERE status = 'BOOKED'
                  AND booking_date >= :current_date
            """),
            {"current_date": current_date},
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
                      AND booking_date >= :current_date
                    ORDER BY booking_date, start_time
                    LIMIT :limit
                """),
                {
                    "current_date": current_date,
                    "limit": SCHEDULE_NOTIFICATION_LIMIT,
                },
            )
            .mappings()
            .all()
        )

    if not bookings:
        return "📋 Current Schedule\nNo upcoming bookings."

    schedule_lines = ["📋 Current Schedule"]
    schedule_lines.extend(
        f"{booking['booking_date'].strftime('%d/%m/%Y')} | "
        f"{booking['start_time'].strftime('%H:%M')}–"
        f"{booking['end_time'].strftime('%H:%M')} | "
        f"{notification_first_name(booking['user_name'])}"
        for booking in bookings
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

    return validate_signed_init_data(
        init_data,
        BOT_TOKEN,
        max_age_seconds=TELEGRAM_INIT_DATA_MAX_AGE,
    )


# =========================================================
# HOME
# =========================================================


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    page: int = 1,
    view: str = "all",
):

    page = max(page, 1)
    schedule_filter = view if view in {"today", "tomorrow", "all"} else "all"
    today = current_meeting_datetime().date()
    filter_date = today + timedelta(days=1) if schedule_filter == "tomorrow" else today
    query_parameters = {
        "today": today,
        "schedule_filter": schedule_filter,
        "filter_date": filter_date,
    }

    with engine.connect() as conn:
        total_bookings = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM bookings
                WHERE status = 'BOOKED'
                  AND booking_date >= :today
                  AND (
                      :schedule_filter = 'all'
                      OR booking_date = :filter_date
                  )
            """),
            query_parameters,
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
                      AND booking_date >= :today
                      AND (
                          :schedule_filter = 'all'
                          OR booking_date = :filter_date
                      )
                    ORDER BY booking_date, start_time
                    LIMIT :limit OFFSET :offset
                """),
                {
                    **query_parameters,
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
            "schedule_filter": schedule_filter,
            "admin_id": ADMIN_ID,
            "bot_username": await get_bot_username(),
        },
    )


@app.get("/tools/exchange-rate", response_class=HTMLResponse)
async def exchange_rate(request: Request):
    """Display annual and available monthly rates from the supplied workbook."""
    try:
        exchange_data = await run_in_threadpool(
            load_exchange_rates, EXCHANGE_RATE_WORKBOOK
        )
    except (OSError, ValueError):
        logger.exception("Could not load exchange-rate workbook")
        raise HTTPException(status_code=503, detail="Exchange-rate data unavailable")

    if not exchange_data["years"]:
        raise HTTPException(status_code=503, detail="Exchange-rate data unavailable")

    rate_status = await run_in_threadpool(
        fetch_latest_gdt_rate, force=request.query_params.get("refresh") == "1"
    )
    for field in ("checked_at", "attempted_at"):
        value = rate_status.get(field)
        rate_status[field + "_label"] = (
            value.astimezone(ZoneInfo("Asia/Phnom_Penh")).strftime("%d %b %Y, %H:%M:%S GMT+7")
            if value else None
        )
    requested_year = request.query_params.get("year", "")
    selected_year = next(
        (year for year in exchange_data["years"] if str(year) == requested_year),
        next(iter(exchange_data["years"])),
    )

    return templates.TemplateResponse(
        request=request,
        name="exchange_rate.html",
        context={
            "exchange_years": exchange_data["years"],
            "selected_year": selected_year,
            "current_year": datetime.now(ZoneInfo("Asia/Phnom_Penh")).year,
            "last_updated": exchange_data["last_updated"],
            "latest_official_rate": rate_status["value"],
            "rate_status": rate_status,
            "bot_username": await get_bot_username(),
        },
        headers={"Cache-Control": "no-store"},
    )


# =========================================================
# CREATE BOOKING
# =========================================================


@app.post("/book")
async def create_booking(
    request: Request,
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

    bot_username = await get_bot_username()

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
        return templates.TemplateResponse(
            request=request,
            name="booking_error.html",
            context={
                "error_title": "Invalid booking information",
                "error_message": "Please check the selected date and time.",
                "bot_username": bot_username,
            },
            status_code=400,
        )

    try:
        validate_booking_interval(
            booking_date_value,
            start_time_value,
            end_time_value,
            current_meeting_datetime(),
        )
    except ValueError as exc:
        if str(exc) == "End time must be after start time":
            error_title = "Invalid time"
            error_message = "End time must be later than start time."
        elif str(exc) == "Date cannot be in the past":
            error_title = "Invalid date"
            error_message = "You cannot book a date in the past."
        else:
            error_title = "Invalid booking time"
            error_message = str(exc)
        return templates.TemplateResponse(
            request=request,
            name="booking_error.html",
            context={
                "error_title": error_title,
                "error_message": error_message,
                "bot_username": bot_username,
            },
            status_code=400,
        )

    # -----------------------------------------------------
    # Save booking
    # -----------------------------------------------------

    with engine.begin() as conn:
        overlap_booking = (
            conn.execute(
                text("""
                    SELECT
                        telegram_user_id,
                        user_name,
                        start_time,
                        end_time
                    FROM bookings
                    WHERE booking_date = :booking_date
                      AND status = 'BOOKED'
                      AND start_time < :end_time
                      AND end_time > :start_time
                    ORDER BY start_time
                    LIMIT 1
                """),
                {
                    "booking_date": booking_date_value,
                    "start_time": start_time_value,
                    "end_time": end_time_value,
                },
            )
            .mappings()
            .first()
        )

        if overlap_booking:
            is_repeated_submission = (
                overlap_booking["telegram_user_id"] == telegram_user_id
                and overlap_booking["start_time"] == start_time_value
                and overlap_booking["end_time"] == end_time_value
            )

            if is_repeated_submission:
                return RedirectResponse(
                    url="/",
                    status_code=303,
                )

            return templates.TemplateResponse(
                request=request,
                name="booking_conflict.html",
                context={
                    "booking_date": booking_date_value,
                    "requested_start_time": start_time_value,
                    "requested_end_time": end_time_value,
                    "conflicting_user_name": overlap_booking["user_name"],
                    "conflicting_start_time": overlap_booking["start_time"],
                    "conflicting_end_time": overlap_booking["end_time"],
                    "bot_username": bot_username,
                },
                status_code=409,
            )

        inserted = insert_booking(
            conn,
            {
                "telegram_user_id": telegram_user_id,
                "user_name": user_name,
                "booking_date": booking_date_value,
                "start_time": start_time_value,
                "end_time": end_time_value,
            },
        )
        if not inserted:
            return HTMLResponse(
                "<h2>Booking conflict</h2><p>That time was just booked by another user.</p>"
                "<a href='/'>Back</a>",
                status_code=409,
            )

    await notify_group(
        "📢 New booking\n\n"
        f"👤 {notification_first_name(user_name)}\n"
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
    meeting_now = current_meeting_datetime()

    with engine.connect() as conn:
        total_bookings = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM bookings
                WHERE telegram_user_id = :telegram_user_id
                  AND status = 'BOOKED'
                  AND booking_date >= :current_date
            """),
            {
                "telegram_user_id": telegram_user_id,
                "current_date": meeting_now.date(),
            },
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
                      AND booking_date >= :current_date
                    ORDER BY booking_date, start_time
                    LIMIT :limit OFFSET :offset
                """),
                {
                    "telegram_user_id": telegram_user_id,
                    "current_date": meeting_now.date(),
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
            "current_date": meeting_now.date(),
            "current_time": meeting_now.time().replace(tzinfo=None),
            "bot_username": await get_bot_username(),
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

    cancel_result, booking = cancel_active_booking(
        booking_id_value,
        allowed_user_id=telegram_user_id,
        block_requirement=False,
    )
    if cancel_result == "not_found":
        return HTMLResponse(
            "<h2>Booking not found</h2><a href='/'>Back</a>",
            status_code=404,
        )
    if cancel_result in {"forbidden", "wrong_type"}:
        return HTMLResponse(
            "<h2>Not allowed</h2><p>You can only cancel your own booking.</p>"
            "<a href='/'>Back</a>",
            status_code=403,
        )
    if cancel_result != "success":
        return HTMLResponse(
            "<h2>Booking already cancelled</h2><a href='/'>Back</a>",
            status_code=400,
        )

    await notify_group(
        "🗑️ Booking cancelled\n\n"
        f"👤 {notification_first_name(booking['user_name'])}\n"
        f"📅 {booking['booking_date'].strftime('%d/%m/%Y')}\n"
        f"⏰ {booking['start_time'].strftime('%H:%M')}–"
        f"{booking['end_time'].strftime('%H:%M')}\n\n"
        f"{format_current_schedule()}"
    )

    return RedirectResponse(
        url="/",
        status_code=303,
    )


@app.post("/end-booking")
async def end_booking(
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
    meeting_now = current_meeting_datetime()
    current_date = meeting_now.date()
    current_time = meeting_now.time().replace(tzinfo=None)

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
                {"booking_id": booking_id_value},
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
                <p>You can only end your own meeting.</p>
                <a href="/">Back</a>
                """,
                status_code=403,
            )

        if booking["status"] != "BOOKED":
            return HTMLResponse(
                """
                <h2>Meeting is no longer active</h2>
                <a href="/">Back</a>
                """,
                status_code=400,
            )

        is_active_meeting = (
            booking["booking_date"] == current_date
            and booking["start_time"] <= current_time
            and current_time < booking["end_time"]
        )
        if not is_active_meeting:
            return HTMLResponse(
                """
                <h2>Meeting cannot be ended now</h2>
                <p>This action is only available during the booked time.</p>
                <a href="/">Back</a>
                """,
                status_code=400,
            )

        conn.execute(
            text("""
                UPDATE bookings
                SET status = 'ENDED'
                WHERE id = :booking_id
                  AND status = 'BOOKED'
            """),
            {"booking_id": booking_id_value},
        )

    await notify_group(
        "✅ Meeting ended early — the room is now available\n\n"
        f"👤 {notification_first_name(booking['user_name'])}\n"
        f"📅 {booking['booking_date'].strftime('%d/%m/%Y')}\n"
        f"⏰ Ended at {current_time.strftime('%H:%M')} "
        f"(scheduled until {booking['end_time'].strftime('%H:%M')})\n\n"
        f"{format_current_schedule()}"
    )

    return RedirectResponse(
        url="/",
        status_code=303,
    )


# =========================================================
# ADMIN MINI APP
# =========================================================


def render_admin_dashboard(
    request: Request,
    admin_user: dict,
    telegram_init_data: str,
    feedback: str = "",
    feedback_type: str = "success",
    initial_tab: str = "dashboard",
):
    now = current_meeting_datetime()
    today = now.date()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)
    month_start = today.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    block_pattern = f"{ROOM_BLOCK_PREFIX}%"

    with engine.connect() as conn:
        stats = (
            conn.execute(
                text("""
                    SELECT
                        COUNT(*) FILTER (
                            WHERE booking_date = :today
                              AND user_name NOT LIKE :block_pattern
                        ) AS today_count,
                        COUNT(*) FILTER (
                            WHERE booking_date >= :week_start
                              AND booking_date < :week_end
                              AND user_name NOT LIKE :block_pattern
                        ) AS week_count,
                        COUNT(*) FILTER (
                            WHERE booking_date >= :month_start
                              AND booking_date < :next_month
                              AND user_name NOT LIKE :block_pattern
                        ) AS month_count,
                        COUNT(*) FILTER (
                            WHERE booking_date = :today
                              AND start_time <= :current_time
                              AND end_time > :current_time
                              AND user_name NOT LIKE :block_pattern
                        ) AS current_count
                    FROM bookings
                    WHERE status = 'BOOKED'
                """),
                {
                    "today": today,
                    "week_start": week_start,
                    "week_end": week_end,
                    "month_start": month_start,
                    "next_month": next_month,
                    "current_time": now.time().replace(tzinfo=None),
                    "block_pattern": block_pattern,
                },
            )
            .mappings()
            .first()
        )

        active_slot = (
            conn.execute(
                text("""
                    SELECT user_name, end_time
                    FROM bookings
                    WHERE status = 'BOOKED'
                      AND booking_date = :today
                      AND start_time <= :current_time
                      AND end_time > :current_time
                    ORDER BY start_time
                    LIMIT 1
                """),
                {
                    "today": today,
                    "current_time": now.time().replace(tzinfo=None),
                },
            )
            .mappings()
            .first()
        )

        upcoming_bookings = (
            conn.execute(
                text("""
                    SELECT id, telegram_user_id, user_name,
                           booking_date, start_time, end_time
                    FROM bookings
                    WHERE status = 'BOOKED'
                      AND booking_date >= :today
                      AND user_name NOT LIKE :block_pattern
                    ORDER BY booking_date, start_time
                """),
                {"today": today, "block_pattern": block_pattern},
            )
            .mappings()
            .all()
        )

        room_blocks = (
            conn.execute(
                text("""
                    SELECT id, user_name, booking_date, start_time, end_time
                    FROM bookings
                    WHERE status = 'BOOKED'
                      AND booking_date >= :today
                      AND user_name LIKE :block_pattern
                    ORDER BY booking_date, start_time
                """),
                {"today": today, "block_pattern": block_pattern},
            )
            .mappings()
            .all()
        )

        try:
            recent_activity = (
                conn.execute(
                    text("""
                        SELECT user_name, command, created_at
                        FROM user_activity
                        ORDER BY created_at DESC
                        LIMIT 10
                    """)
                )
                .mappings()
                .all()
            )
            active_users = (
                conn.execute(
                    text("""
                        SELECT telegram_user_id,
                               (ARRAY_AGG(user_name ORDER BY created_at DESC))[1] AS user_name,
                               COUNT(*) AS action_count
                        FROM user_activity
                        GROUP BY telegram_user_id
                        ORDER BY action_count DESC, user_name
                        LIMIT 10
                    """)
                )
                .mappings()
                .all()
            )
        except Exception:
            logger.exception("Could not load recent admin activity")
            recent_activity = []
            active_users = []

    if active_slot:
        is_blocked = active_slot["user_name"].startswith(ROOM_BLOCK_PREFIX)
        room_status = "Blocked" if is_blocked else "Currently Booked"
        room_status_detail = (
            f"Until {active_slot['end_time'].strftime('%H:%M')}"
        )
        room_status_type = "blocked" if is_blocked else "booked"
    else:
        room_status = "Available"
        next_booking = upcoming_bookings[0] if upcoming_bookings else None
        if next_booking and next_booking["booking_date"] == today:
            room_status_detail = (
                f"Until {next_booking['start_time'].strftime('%H:%M')}"
            )
        else:
            room_status_detail = "Ready to book"
        room_status_type = "available"

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "admin_name": admin_user.get("first_name", "Admin"),
            "telegram_init_data": telegram_init_data,
            "stats": stats,
            "room_status": room_status,
            "room_status_detail": room_status_detail,
            "room_status_type": room_status_type,
            "upcoming_bookings": upcoming_bookings,
            "room_blocks": room_blocks,
            "recent_activity": recent_activity,
            "active_users": active_users,
            "today": today,
            "feedback": feedback,
            "feedback_type": feedback_type,
            "bot_username": BOT_USERNAME_CACHE or "TelegramBot",
            "initial_tab": initial_tab if initial_tab in {
                "dashboard", "bookings", "rooms", "reports", "settings"
            } else "dashboard",
        },
    )


@app.post("/admin/reports/users/{telegram_user_id}", response_class=HTMLResponse)
async def admin_user_statistics(
    request: Request,
    telegram_user_id: int,
    telegram_init_data: str = Form(...),
):
    """Render read-only activity and booking analytics for one Telegram user."""
    authenticate_admin(telegram_init_data)
    params = {"telegram_user_id": telegram_user_id}

    with engine.connect() as conn:
        activity_summary = conn.execute(text("""
            SELECT (ARRAY_AGG(user_name ORDER BY created_at DESC))[1] AS user_name,
                   COUNT(*) AS total_actions, MIN(created_at) AS first_activity,
                   MAX(created_at) AS last_activity
            FROM user_activity WHERE telegram_user_id = :telegram_user_id
        """), params).mappings().first()
        command_counts = conn.execute(text("""
            SELECT command, COUNT(*) AS action_count
            FROM user_activity WHERE telegram_user_id = :telegram_user_id
            GROUP BY command ORDER BY action_count DESC, command
        """), params).mappings().all()
        recent_activity = conn.execute(text("""
            SELECT command, created_at FROM user_activity
            WHERE telegram_user_id = :telegram_user_id
            ORDER BY created_at DESC LIMIT 20
        """), params).mappings().all()
        booking_summary = conn.execute(text("""
            SELECT COUNT(*) AS total_bookings,
                   COUNT(*) FILTER (WHERE status = 'CANCELLED') AS cancelled_bookings,
                   (ARRAY_AGG(user_name ORDER BY created_at DESC))[1] AS user_name
            FROM bookings WHERE telegram_user_id = :telegram_user_id
        """), params).mappings().first()
        recent_bookings = conn.execute(text("""
            SELECT booking_date, start_time, end_time, status FROM bookings
            WHERE telegram_user_id = :telegram_user_id
            ORDER BY booking_date DESC, start_time DESC LIMIT 10
        """), params).mappings().all()

    total_actions = activity_summary["total_actions"] or 0
    total_bookings = booking_summary["total_bookings"] or 0
    if not total_actions and not total_bookings:
        raise HTTPException(status_code=404, detail="User statistics not found")

    return templates.TemplateResponse(
        request=request,
        name="admin_user_statistics.html",
        context={
            "telegram_init_data": telegram_init_data,
            "telegram_user_id": telegram_user_id,
            "user_name": activity_summary["user_name"] or booking_summary["user_name"],
            "total_actions": total_actions,
            "total_bookings": total_bookings,
            "cancelled_bookings": booking_summary["cancelled_bookings"] or 0,
            "first_activity": activity_summary["first_activity"],
            "last_activity": activity_summary["last_activity"],
            "command_counts": command_counts,
            "recent_activity": recent_activity,
            "recent_bookings": recent_bookings,
            "bot_username": BOT_USERNAME_CACHE or "TelegramBot",
        },
    )


@app.post("/admin", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    telegram_init_data: str = Form(...),
    initial_tab: str = Form("dashboard"),
):
    admin_user = authenticate_admin(telegram_init_data)
    return render_admin_dashboard(
        request, admin_user, telegram_init_data, initial_tab=initial_tab
    )


@app.post("/admin/bookings/add", response_class=HTMLResponse)
async def admin_add_booking(
    request: Request,
    telegram_init_data: str = Form(...),
    user_name: str = Form("Admin"),
    booking_date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
):
    admin_user = authenticate_admin(telegram_init_data)
    date_value, start_value, end_value = parse_booking_datetime_values(
        booking_date, start_time, end_time
    )
    booking_name = user_name.strip() or "Admin"

    with engine.begin() as conn:
        if find_overlapping_booking(conn, date_value, start_value, end_value):
            return render_admin_dashboard(
                request,
                admin_user,
                telegram_init_data,
                "That time overlaps with an existing booking or room block.",
                "error",
            )
        inserted = insert_booking(
            conn,
            {
                "telegram_user_id": ADMIN_ID,
                "user_name": booking_name,
                "booking_date": date_value,
                "start_time": start_value,
                "end_time": end_value,
            },
        )
        if not inserted:
            return render_admin_dashboard(
                request, admin_user, telegram_init_data,
                "That time was just booked by another user.", "error"
            )

    record_activity(admin_user, "/admin_add_booking")
    await notify_group(
        "📢 Booking added by Admin\n\n"
        f"👤 {notification_first_name(booking_name)}\n"
        f"📅 {date_value.strftime('%d/%m/%Y')}\n"
        f"⏰ {start_value.strftime('%H:%M')}–{end_value.strftime('%H:%M')}\n\n"
        f"{format_current_schedule()}"
    )
    return render_admin_dashboard(
        request, admin_user, telegram_init_data, "Booking added successfully."
    )


@app.post("/admin/bookings/edit", response_class=HTMLResponse)
async def admin_edit_booking(
    request: Request,
    telegram_init_data: str = Form(...),
    booking_id: int = Form(...),
    booking_date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
):
    admin_user = authenticate_admin(telegram_init_data)
    date_value, start_value, end_value = parse_booking_datetime_values(
        booking_date, start_time, end_time
    )

    with engine.begin() as conn:
        booking = (
            conn.execute(
                text("""
                    SELECT id, user_name, status
                    FROM bookings
                    WHERE id = :booking_id
                """),
                {"booking_id": booking_id},
            )
            .mappings()
            .first()
        )
        if not booking or booking["status"] != "BOOKED":
            raise HTTPException(status_code=404, detail="Active booking not found")
        if booking["user_name"].startswith(ROOM_BLOCK_PREFIX):
            raise HTTPException(status_code=400, detail="Use unblock and create a new block")
        if find_overlapping_booking(
            conn, date_value, start_value, end_value, booking_id
        ):
            return render_admin_dashboard(
                request,
                admin_user,
                telegram_init_data,
                "The edited time overlaps with another booking or room block.",
                "error",
            )
        conn.execute(
            text("""
                UPDATE bookings
                SET booking_date = :booking_date,
                    start_time = :start_time,
                    end_time = :end_time
                WHERE id = :booking_id AND status = 'BOOKED'
            """),
            {
                "booking_id": booking_id,
                "booking_date": date_value,
                "start_time": start_value,
                "end_time": end_value,
            },
        )

    record_activity(admin_user, "/admin_edit_booking")
    await notify_group(
        "✏️ Booking updated by Admin\n\n"
        f"👤 {notification_first_name(booking['user_name'])}\n"
        f"📅 {date_value.strftime('%d/%m/%Y')}\n"
        f"⏰ {start_value.strftime('%H:%M')}–{end_value.strftime('%H:%M')}\n\n"
        f"{format_current_schedule()}"
    )
    return render_admin_dashboard(
        request, admin_user, telegram_init_data, "Booking updated successfully."
    )


@app.post("/admin/bookings/cancel", response_class=HTMLResponse)
async def admin_cancel_booking(
    request: Request,
    telegram_init_data: str = Form(...),
    booking_id: int = Form(...),
):
    admin_user = authenticate_admin(telegram_init_data)
    cancel_result, booking = cancel_active_booking(
        booking_id,
        block_requirement=False,
    )
    if cancel_result == "wrong_type":
        raise HTTPException(status_code=400, detail="Use Unblock Room")
    if cancel_result != "success":
        raise HTTPException(status_code=404, detail="Active booking not found")

    record_activity(admin_user, "/admin_cancel_booking")
    await notify_group(
        "🗑️ Booking cancelled by Admin\n\n"
        f"👤 {notification_first_name(booking['user_name'])}\n"
        f"📅 {booking['booking_date'].strftime('%d/%m/%Y')}\n"
        f"⏰ {booking['start_time'].strftime('%H:%M')}–"
        f"{booking['end_time'].strftime('%H:%M')}\n\n"
        f"{format_current_schedule()}"
    )
    return render_admin_dashboard(
        request, admin_user, telegram_init_data, "Booking cancelled."
    )


@app.post("/admin/blocks/add", response_class=HTMLResponse)
async def admin_block_room(
    request: Request,
    telegram_init_data: str = Form(...),
    booking_date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    reason: str = Form(""),
):
    admin_user = authenticate_admin(telegram_init_data)
    date_value, start_value, end_value = parse_booking_datetime_values(
        booking_date, start_time, end_time
    )
    block_name = ROOM_BLOCK_PREFIX
    if reason.strip():
        block_name = f"{ROOM_BLOCK_PREFIX}: {reason.strip()[:80]}"

    with engine.begin() as conn:
        if find_overlapping_booking(conn, date_value, start_value, end_value):
            return render_admin_dashboard(
                request,
                admin_user,
                telegram_init_data,
                "The room cannot be blocked because that time is already occupied.",
                "error",
            )
        inserted = insert_booking(
            conn,
            {
                "telegram_user_id": ADMIN_ID,
                "user_name": block_name,
                "booking_date": date_value,
                "start_time": start_value,
                "end_time": end_value,
            },
        )
        if not inserted:
            return render_admin_dashboard(
                request, admin_user, telegram_init_data,
                "That time was just occupied by another user.", "error"
            )

    record_activity(admin_user, "/admin_block_room")
    await notify_group(
        "🔒 Meeting room blocked by Admin\n\n"
        f"📅 {date_value.strftime('%d/%m/%Y')}\n"
        f"⏰ {start_value.strftime('%H:%M')}–{end_value.strftime('%H:%M')}\n"
        f"📝 {reason.strip() or 'No reason provided'}\n\n"
        f"{format_current_schedule()}"
    )
    return render_admin_dashboard(
        request, admin_user, telegram_init_data, "Room blocked successfully."
    )


@app.post("/admin/blocks/remove", response_class=HTMLResponse)
async def admin_unblock_room(
    request: Request,
    telegram_init_data: str = Form(...),
    booking_id: int = Form(...),
):
    admin_user = authenticate_admin(telegram_init_data)
    cancel_result, block = cancel_active_booking(
        booking_id,
        block_requirement=True,
    )
    if cancel_result != "success":
        raise HTTPException(status_code=404, detail="Active room block not found")

    record_activity(admin_user, "/admin_unblock_room")
    await notify_group(
        "🔓 Meeting room unblocked by Admin\n\n"
        f"📅 {block['booking_date'].strftime('%d/%m/%Y')}\n"
        f"⏰ {block['start_time'].strftime('%H:%M')}–"
        f"{block['end_time'].strftime('%H:%M')}\n\n"
        f"{format_current_schedule()}"
    )
    return render_admin_dashboard(
        request, admin_user, telegram_init_data, "Room block removed."
    )


@app.post("/admin/notice", response_class=HTMLResponse)
async def admin_send_notice(
    request: Request,
    telegram_init_data: str = Form(...),
    message: str = Form(...),
):
    admin_user = authenticate_admin(telegram_init_data)
    message_text = message.strip()
    if not message_text:
        return render_admin_dashboard(
            request,
            admin_user,
            telegram_init_data,
            "Notice message cannot be empty.",
            "error",
        )
    await notify_group(f"📣 Admin Notice\n\n{message_text[:3500]}")
    record_activity(admin_user, "/admin_notice")
    return render_admin_dashboard(
        request, admin_user, telegram_init_data, "Group notice sent."
    )
