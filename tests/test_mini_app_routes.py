import asyncio
import importlib
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import dotenv
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse


@pytest.fixture(scope="module")
def mini_app_module():
    """Import the web app without reading developer or production secrets."""
    original_load_dotenv = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    environment = {
        "BOT_TOKEN": "123456:test-token",
        "DATABASE_URL": "postgresql+psycopg2://test:test@localhost/test",
        "GROUP_CHAT_ID": "-100123456",
        "ADMIN_ID": "42",
        "TELEGRAM_INIT_DATA_MAX_AGE": "3600",
    }
    previous = {}
    import os

    for key, value in environment.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value

    sys.modules.pop("mini_app", None)
    try:
        yield importlib.import_module("mini_app")
    finally:
        sys.modules.pop("mini_app", None)
        dotenv.load_dotenv = original_load_dotenv
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def request_for(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "scheme": "http",
        }
    )


def future_date(days: int = 2) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


def test_admin_authentication_rejects_non_admin(mini_app_module, monkeypatch):
    monkeypatch.setattr(
        mini_app_module,
        "validate_telegram_init_data",
        lambda _init_data: {"id": 7, "first_name": "User"},
    )

    with pytest.raises(HTTPException) as error:
        mini_app_module.authenticate_admin("signed-data")

    assert error.value.status_code == 403
    assert error.value.detail == "Admin access required"


def test_admin_authentication_accepts_configured_admin(mini_app_module, monkeypatch):
    expected_user = {"id": mini_app_module.ADMIN_ID, "first_name": "Admin"}
    monkeypatch.setattr(
        mini_app_module,
        "validate_telegram_init_data",
        lambda _init_data: expected_user,
    )

    assert mini_app_module.authenticate_admin("signed-data") == expected_user


@pytest.mark.parametrize(
    ("booking_date", "start_time", "end_time", "message"),
    [
        ("not-a-date", "09:00", "10:00", "Invalid date or time"),
        (future_date(), "10:00", "09:00", "End time must be after start time"),
    ],
)
def test_admin_booking_datetime_validation(
    mini_app_module, booking_date, start_time, end_time, message
):
    with pytest.raises(HTTPException) as error:
        mini_app_module.parse_booking_datetime_values(
            booking_date, start_time, end_time
        )

    assert error.value.status_code == 400
    assert error.value.detail == message


def test_booking_date_validation_uses_configured_meeting_timezone(
    mini_app_module, monkeypatch
):
    configured_now = datetime(
        2026, 8, 30, 0, 15, tzinfo=ZoneInfo("Asia/Phnom_Penh")
    )
    monkeypatch.setattr(
        mini_app_module, "current_meeting_datetime", lambda: configured_now
    )

    with pytest.raises(HTTPException, match="Date cannot be in the past"):
        mini_app_module.parse_booking_datetime_values(
            "2026-08-29", "09:00", "10:00"
        )

    booking_date, _, _ = mini_app_module.parse_booking_datetime_values(
        "2026-08-30", "09:00", "10:00"
    )
    assert booking_date.isoformat() == "2026-08-30"


def test_booking_route_rejects_invalid_time_before_database_write(
    mini_app_module, monkeypatch
):
    monkeypatch.setattr(
        mini_app_module,
        "validate_telegram_init_data",
        lambda _init_data: {"id": 7, "first_name": "Dara"},
    )

    async def bot_username():
        return "test_bot"

    monkeypatch.setattr(mini_app_module, "get_bot_username", bot_username)
    response = asyncio.run(
        mini_app_module.create_booking(
            request_for("/book"),
            telegram_init_data="signed-data",
            booking_date=future_date(),
            start_time="10:00",
            end_time="09:00",
        )
    )

    assert isinstance(response, HTMLResponse)
    assert response.status_code == 400
    assert b"End time must be later than start time" in response.body


def test_cancel_route_rejects_booking_owned_by_another_user(
    mini_app_module, monkeypatch
):
    monkeypatch.setattr(
        mini_app_module,
        "validate_telegram_init_data",
        lambda _init_data: {"id": 7, "first_name": "Dara"},
    )
    monkeypatch.setattr(
        mini_app_module,
        "cancel_active_booking",
        lambda booking_id, **kwargs: ("forbidden", {"id": booking_id}),
    )

    response = asyncio.run(
        mini_app_module.cancel_booking(
            telegram_init_data="signed-data", booking_id="12"
        )
    )

    assert isinstance(response, HTMLResponse)
    assert response.status_code == 403
    assert b"only cancel your own booking" in response.body


def test_cancel_route_preserves_success_redirect_and_notification(
    mini_app_module, monkeypatch
):
    booking = {
        "id": 12,
        "user_name": "Dara Sok",
        "booking_date": datetime.now().date(),
        "start_time": datetime.strptime("09:00", "%H:%M").time(),
        "end_time": datetime.strptime("10:00", "%H:%M").time(),
    }
    notifications = []
    monkeypatch.setattr(
        mini_app_module,
        "validate_telegram_init_data",
        lambda _init_data: {"id": 7, "first_name": "Dara"},
    )
    monkeypatch.setattr(
        mini_app_module,
        "cancel_active_booking",
        lambda booking_id, **kwargs: ("success", booking),
    )
    monkeypatch.setattr(
        mini_app_module, "format_current_schedule", lambda: "Current schedule"
    )

    async def capture_notification(message):
        notifications.append(message)

    monkeypatch.setattr(mini_app_module, "notify_group", capture_notification)
    response = asyncio.run(
        mini_app_module.cancel_booking(
            telegram_init_data="signed-data", booking_id="12"
        )
    )

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert len(notifications) == 1
    assert "Dara" in notifications[0]
    assert "Current schedule" in notifications[0]


def test_admin_template_has_five_true_tab_panels_with_dashboard_default(
    mini_app_module,
):
    template = mini_app_module.templates.env.get_template("admin.html")
    html = template.render(
        feedback="",
        feedback_type="success",
        admin_name="Admin",
        room_status_type="available",
        room_status="Available",
        room_status_detail="Ready to book",
        stats={"today_count": 0, "week_count": 0, "month_count": 0},
        upcoming_bookings=[],
        room_blocks=[],
        recent_activity=[],
        today=datetime.now().date(),
        telegram_init_data="signed-data",
        bot_username="test_bot",
    )

    assert len(re.findall(r"<[^>]+data-admin-panel=", html)) == 5
    assert 'data-admin-panel="dashboard"' in html
    assert 'data-admin-panel="dashboard" hidden' not in html
    for tab in ("bookings", "rooms", "reports", "settings"):
        assert f'data-admin-panel="{tab}"' in html
    assert html.count('data-admin-tab="dashboard" aria-current="page"') == 1
    assert html.index('id="roomTitle">B03 Meeting Room') < html.index(
        'id="roomStatus"'
    )
