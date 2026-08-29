import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from telegram_auth import validate_telegram_init_data


TOKEN = "123456:test-token"


def signed_init_data(*, auth_date=1_000, user=None, token=TOKEN):
    values = {
        "auth_date": str(auth_date),
        "query_id": "test-query",
        "user": json.dumps(user or {"id": 42, "first_name": "Test"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_accepts_recent_valid_data():
    user = validate_telegram_init_data(signed_init_data(), TOKEN, now=1_100)
    assert user["id"] == 42


def test_rejects_expired_data():
    with pytest.raises(ValueError, match="expired"):
        validate_telegram_init_data(
            signed_init_data(), TOKEN, max_age_seconds=60, now=1_100
        )


def test_rejects_tampered_data():
    data = signed_init_data().replace("Test", "Other")
    with pytest.raises(ValueError, match="Invalid Telegram initData"):
        validate_telegram_init_data(data, TOKEN, now=1_100)


def test_rejects_future_auth_date():
    with pytest.raises(ValueError, match="future"):
        validate_telegram_init_data(signed_init_data(auth_date=2_000), TOKEN, now=1_000)
