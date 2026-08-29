import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


DEFAULT_MAX_AGE_SECONDS = 3600


def validate_telegram_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: int | None = None,
) -> dict:
    """Validate signed Telegram Mini App data and reject stale credentials."""
    if not init_data:
        raise ValueError("Missing Telegram initData")
    if not bot_token:
        raise ValueError("Bot token is not configured")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise ValueError("Missing Telegram hash")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(parsed.items())
    )
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    calculated_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError("Invalid Telegram initData")

    try:
        auth_date = int(parsed["auth_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid Telegram auth_date") from exc

    current_time = int(time.time()) if now is None else now
    if auth_date > current_time + 30:
        raise ValueError("Telegram initData has a future auth_date")
    if max_age_seconds > 0 and current_time - auth_date > max_age_seconds:
        raise ValueError("Telegram initData has expired")

    user_json = parsed.get("user")
    if not user_json:
        raise ValueError("Telegram user not found")
    try:
        user = json.loads(user_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid Telegram user data") from exc
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise ValueError("Invalid Telegram user data")
    return user
