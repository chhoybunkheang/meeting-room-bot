import asyncio
import importlib
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
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


def test_admin_user_statistics_is_read_only_and_renders_existing_data(
    mini_app_module, monkeypatch
):
    now = datetime(2026, 9, 1, 9, 30)
    rows = [
        [{"user_name": "Dara", "total_actions": 5,
          "first_activity": now - timedelta(days=3), "last_activity": now}],
        [{"command": "/book", "action_count": 3},
         {"command": "/start", "action_count": 2}],
        [{"command": "/book", "created_at": now}],
        [{"total_bookings": 3, "cancelled_bookings": 1,
          "user_name": "Dara"}],
        [{"booking_date": now.date(),
          "start_time": now.time().replace(second=0, microsecond=0),
          "end_time": (now + timedelta(hours=1)).time().replace(second=0, microsecond=0),
          "status": "BOOKED"}],
    ]
    statements = []

    class Result:
        def __init__(self, values):
            self.values = values
        def mappings(self):
            return self
        def first(self):
            return self.values[0]
        def all(self):
            return self.values

    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def execute(self, statement, params):
            statements.append((str(statement), params))
            return Result(rows.pop(0))

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(mini_app_module, "engine", Engine())
    monkeypatch.setattr(
        mini_app_module, "authenticate_admin", lambda _data: {"id": 42}
    )
    response = asyncio.run(mini_app_module.admin_user_statistics(
        request_for("/admin/reports/users/7"), 7, "signed-data"
    ))

    assert response.status_code == 200
    assert b"Dara" in response.body
    assert b"Telegram user ID: 7" in response.body
    assert b"Back to User Statistics" in response.body
    assert len(statements) == 5
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in statements)
    assert all(params == {"telegram_user_id": 7} for _, params in statements)


def test_shared_user_header_shows_room_status_below_title(mini_app_module):
    template = mini_app_module.templates.env.get_template("_user_header.html")
    html = template.render(
        get_user_room_status=lambda: {
            "type": "available",
            "label": "Available",
            "detail": "Ready to book",
        }
    )

    assert html.index("B03 Meeting Room") < html.index("user-room-status available")
    assert "Room Status" in html
    assert "Available" in html
    assert "Ready to book" in html


def test_exchange_rate_page_keeps_help_navigation_active(mini_app_module):
    template = mini_app_module.templates.env.get_template("exchange_rate.html")
    html = template.render(
        exchange_years={
            2025: {
                "annual": 4050,
                "toi_rate_available": True,
                "annual_method": "gdt_year_end",
                "annual_source_url": "https://www.tax.gov.kh/en/exchange-rate?for_year=2025&for_month=12",
                "annual_published_at": "2025-12-31",
                "months": [
                    {
                        "number": 1,
                        "month": "Jan",
                        "purchase": 4018,
                        "sale": 4032,
                        "midpoint": 4025,
                        "official": 4024,
                    }
                ],
            }
        },
        selected_year=2025,
        current_year=2026,
        rate_status={"stale": False, "cached": False},
        last_updated=None,
        latest_official_rate={
            "rate": 4047,
            "published_at": datetime(2026, 9, 2),
        },
    )

    assert 'href="/tools/exchange-rate"' not in html
    assert 'href="/?panel=help#helpPanel"' in html
    assert html.count('class="active"') == 1
    assert 'class="active" href="/?panel=help#helpPanel"' in html
    assert 'aria-current="page"' in html
    assert "Exchange Rate" in html
    assert "Latest Official Rate" in html
    assert "Official rate from" in html
    assert "GDT Annual TOI Exchange Rate" in html
    assert "Year-end official rate · published 2025-12-31" in html
    assert "4,047" in html
    assert "Export" not in html


def test_exchange_rate_summary_is_sticky_and_mobile_compact():
    stylesheet = (Path(__file__).resolve().parents[1] / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert ".exchange-summary-grid { position: sticky;" in stylesheet
    assert "grid-template-columns: minmax(118px,.8fr) minmax(0,1.2fr)" in stylesheet


@pytest.mark.parametrize("query, selected, force", [
    (b"year=2024&refresh=1", 2024, True),
    (b"year=invalid", 2026, False),
    (b"year=1900", 2026, False),
])
def test_exchange_route_selection_refresh_and_coverage(mini_app_module, monkeypatch, query, selected, force):
    calls = []
    def fetch(force=False):
        calls.append(force)
        return {"value": None, "checked_at": None, "attempted_at": None,
                "stale": True, "cached": False, "refresh_throttled": False}
    monkeypatch.setattr(mini_app_module, "fetch_latest_gdt_rate", fetch)
    monkeypatch.setattr(mini_app_module, "EXCHANGE_RATE_WORKBOOK",
                        Path(__file__).resolve().parents[1] / "data" / "Exchange Rate.xlsx")
    request = request_for("/tools/exchange-rate")
    request.scope["query_string"] = query
    response = asyncio.run(mini_app_module.exchange_rate(request))
    html = response.body.decode()
    assert calls == [force]
    assert response.context["selected_year"] == selected
    assert response.headers["cache-control"] == "no-store"
    assert 'value="%s" selected' % selected in html
    assert "Update failed." in html
    assert "Workbook updated" not in html
    if selected == 2026:
        assert "Through Mar 2026 · 3 of 12 months" in html
    else:
        assert "published 2024-12-31" in html


def test_exchange_route_exposes_saved_rate_and_successful_check_time(mini_app_module, monkeypatch):
    from datetime import timezone
    checked = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(mini_app_module, "fetch_latest_gdt_rate", lambda force=False: {
        "value": {"rate": 4048, "published_at": datetime(2026, 9, 4),
                  "source_url": "https://www.tax.gov.kh/en/exchange-rate"},
        "checked_at": checked, "attempted_at": checked + timedelta(hours=7),
        "stale": True, "cached": True, "refresh_throttled": False,
    })
    response = asyncio.run(mini_app_module.exchange_rate(request_for("/tools/exchange-rate")))
    html = response.body.decode()
    assert "Last Known Official Rate" in html
    assert "4,048" in html
    assert "Showing the last saved rate" in html
    assert "04 Sep 2026, 17:00:00 GMT+7" in html
    assert "05 Sep 2026, 00:00:00 GMT+7" in html


@pytest.mark.parametrize("year, rate, method", [
    (2014, "4,038", "Annual average"), (2015, "4,060", "Annual average"),
    (2016, "4,037", "Annual average"), (2017, "4,045", "Annual average"),
    (2018, "4,045", "Annual average"), (2019, "4,052", "Annual average"),
    (2020, "4,045", "Year-end rate"), (2021, "4,074", "Year-end rate"),
])
def test_reported_annual_rates_show_evidence_without_official_publication_claim(
    mini_app_module, monkeypatch, year, rate, method
):
    monkeypatch.setattr(mini_app_module, "fetch_latest_gdt_rate", lambda force=False: {
        "value": None, "checked_at": None, "attempted_at": None,
        "stale": True, "cached": False, "refresh_throttled": False,
    })
    monkeypatch.setattr(mini_app_module, "EXCHANGE_RATE_WORKBOOK",
                        Path(__file__).resolve().parents[1] / "data" / "Exchange Rate.xlsx")
    request = request_for("/tools/exchange-rate")
    request.scope["query_string"] = f"year={year}".encode()
    html = asyncio.run(mini_app_module.exchange_rate(request)).body.decode()
    assert f'id="annualRate">{rate}</span>' in html
    assert f'id="annualRateLabel">Annual TOI Exchange Rate ({year})</span>' in html
    assert f'{method} - Corroborated by financial report; original notice not verified</small>' in html
    assert 'published None' not in html
    assert 'published null' not in html
    assert f'id="annualRateUnit">KHR/USD</small>' in html
