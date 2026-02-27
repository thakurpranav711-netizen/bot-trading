# app/utils/time.py

from datetime import datetime


def format_timestamp(ts: int | None = None) -> str:
    """
    Returns formatted time string for logs / Telegram messages
    """
    if ts:
        dt = datetime.fromtimestamp(ts)
    else:
        dt = datetime.now()

    return dt.strftime("%I:%M %p")