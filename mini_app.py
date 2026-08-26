import os
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text

load_dotenv(override=True)

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured")


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

app = FastAPI(title="Meeting Room Mini App")

templates = Jinja2Templates(directory="templates")


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


@app.post("/book")
async def create_booking(
    telegram_user_id: int = Form(...),
    user_name: str = Form(...),
    booking_date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
):

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
            "Invalid date or time.",
            status_code=400,
        )

    if end_time_value <= start_time_value:
        return HTMLResponse(
            "End time must be later than start time.",
            status_code=400,
        )

    if booking_date_value < datetime.now().date():
        return HTMLResponse(
            "You cannot book a past date.",
            status_code=400,
        )

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
                <p>This time overlaps with another booking.</p>
                <a href="/">Back to schedule</a>
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
                "user_name": user_name.strip(),
                "booking_date": booking_date_value,
                "start_time": start_time_value,
                "end_time": end_time_value,
            },
        )

    return RedirectResponse(
        url="/",
        status_code=303,
    )
