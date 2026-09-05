from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from booking_validation import validate_booking_interval


NOW = datetime(2026, 9, 5, 14, 30, tzinfo=ZoneInfo("Asia/Phnom_Penh"))


def test_rejects_booking_that_ended_today():
    with pytest.raises(ValueError, match="current time"):
        validate_booking_interval(NOW.date(), time(13, 0), time(14, 0), NOW)


def test_accepts_booking_that_starts_later_today():
    validate_booking_interval(NOW.date(), time(15, 0), time(16, 0), NOW)


def test_rejects_past_date_before_time_checks():
    with pytest.raises(ValueError, match="past"):
        validate_booking_interval(
            NOW.date().replace(day=4), time(15, 0), time(16, 0), NOW
        )