import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time
from pathlib import Path

import psycopg2
import pytest


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)


def psycopg_url():
    return TEST_DATABASE_URL.replace("postgresql+psycopg2://", "postgresql://", 1)


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    migration = (
        Path(__file__).parents[1] / "migrations" / "001_initial_schema.sql"
    ).read_text(encoding="utf-8")
    with psycopg2.connect(psycopg_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(migration)
    yield


def insert_competing_booking(user_id):
    try:
        with psycopg2.connect(psycopg_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO bookings (
                        telegram_user_id, user_name, booking_date,
                        start_time, end_time, status
                    ) VALUES (%s, %s, %s, %s, %s, 'BOOKED')
                    """,
                    (user_id, f"Concurrency Test {user_id}", date(2099, 12, 30),
                     time(10, 0), time(11, 0)),
                )
        return "inserted"
    except psycopg2.errors.ExclusionViolation:
        return "conflict"


def test_database_allows_only_one_concurrent_booking():
    test_ids = (9_990_001, 9_990_002)
    with psycopg2.connect(psycopg_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM bookings WHERE telegram_user_id IN (%s, %s)", test_ids
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(insert_competing_booking, test_ids))
        assert sorted(results) == ["conflict", "inserted"]
    finally:
        with psycopg2.connect(psycopg_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM bookings WHERE telegram_user_id IN (%s, %s)",
                    test_ids,
                )
