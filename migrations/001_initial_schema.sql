BEGIN;

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE IF NOT EXISTS bookings (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    user_name TEXT NOT NULL,
    booking_date DATE NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    status TEXT NOT NULL DEFAULT 'BOOKED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT bookings_valid_time CHECK (end_time > start_time),
    CONSTRAINT bookings_valid_status CHECK (status IN ('BOOKED', 'CANCELLED', 'ENDED'))
);

CREATE TABLE IF NOT EXISTS user_activity (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    user_name TEXT NOT NULL,
    command TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_bookings_active_schedule
    ON bookings (booking_date, start_time)
    WHERE status = 'BOOKED';

CREATE INDEX IF NOT EXISTS ix_bookings_user_active
    ON bookings (telegram_user_id, booking_date, start_time)
    WHERE status = 'BOOKED';

CREATE INDEX IF NOT EXISTS ix_user_activity_recent
    ON user_activity (created_at DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'bookings_no_active_overlap'
          AND conrelid = 'bookings'::regclass
    ) THEN
        ALTER TABLE bookings
            ADD CONSTRAINT bookings_no_active_overlap
            EXCLUDE USING gist (
                tsrange(booking_date + start_time, booking_date + end_time, '[)') WITH &&
            )
            WHERE (status = 'BOOKED');
    END IF;
END $$;

COMMIT;
