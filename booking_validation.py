"""Shared validation for booking date and time intervals."""

from datetime import date, datetime, time


def validate_booking_interval(
    booking_date: date,
    start_time: time,
    end_time: time,
    current: datetime,
) -> None:
    """Raise ValueError when a booking interval is not currently bookable."""
    if booking_date < current.date():
        raise ValueError("Date cannot be in the past")
    if end_time <= start_time:
        raise ValueError("End time must be after start time")

    current_time = current.time().replace(tzinfo=None)
    if booking_date == current.date() and end_time <= current_time:
        raise ValueError("Booking must end after the current time")
