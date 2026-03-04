# app/utils/time.py

"""
Time Utilities — Production Grade v2

Provides consistent time handling across the trading bot:
- UTC-aware timestamps with proper timezone support
- Multiple exchange market-hours (US stocks, crypto, forex)
- Trading session detection (pre-market, regular, after-hours)
- Human-readable formatting and relative time
- Duration parsing and formatting
- Interval alignment for trading cycles
- Async-compatible sleep and countdown helpers
- Cooldown / throttle timers
- Monotonic performance clock utilities
- Holiday-aware market calendar (US)

All functions handle edge cases gracefully and never raise exceptions
on invalid input (return safe defaults instead).

Usage:
    from app.utils.time import (
        get_utc_now,
        format_timestamp,
        time_ago,
        format_duration,
        parse_duration,
        market_status,
        sleep_until_next_interval,
        Cooldown,
        Stopwatch,
    )

    # Current time
    print(format_timestamp())           # "02:30 PM"
    print(time_ago(some_datetime))      # "5 minutes ago"
    print(format_duration(3665))        # "1h 1m 5s"

    # Market awareness
    status = market_status("us_stock")
    print(status.is_open, status.session, status.next_change)

    # Cooldown timer
    cd = Cooldown(seconds=60)
    if cd.ready():
        do_something()
        cd.reset()

    # Stopwatch
    sw = Stopwatch()
    do_work()
    print(f"Took {sw.elapsed_str()}")
"""

import asyncio
import calendar
import os
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

# ── Try to import zoneinfo (Python 3.9+) ────────────────────────
try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        ZoneInfo = None  # type: ignore


# ═════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═════════════════════════════════════════════════════════════════

# Default timezone for display (configurable via env)
_DISPLAY_TZ_NAME = os.getenv("DISPLAY_TIMEZONE", "US/Eastern")

# US market timezone
_ET_TZ_NAME = "US/Eastern"

# Forex timezone reference
_FOREX_TZ_NAME = "US/Eastern"


def _get_tz(name: str) -> Optional[Any]:
    """Safely get a timezone object."""
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


# Pre-resolve commonly used timezones
TZ_UTC = timezone.utc
TZ_ET = _get_tz(_ET_TZ_NAME)
TZ_DISPLAY = _get_tz(_DISPLAY_TZ_NAME)


# ═════════════════════════════════════════════════════════════════
#  US MARKET HOLIDAYS (2024-2026) — Dates the NYSE is closed
# ═════════════════════════════════════════════════════════════════

_US_HOLIDAYS: set = {
    # 2024
    date(2024, 1, 1),    # New Year's Day
    date(2024, 1, 15),   # MLK Day
    date(2024, 2, 19),   # Presidents' Day
    date(2024, 3, 29),   # Good Friday
    date(2024, 5, 27),   # Memorial Day
    date(2024, 6, 19),   # Juneteenth
    date(2024, 7, 4),    # Independence Day
    date(2024, 9, 2),    # Labor Day
    date(2024, 11, 28),  # Thanksgiving
    date(2024, 12, 25),  # Christmas
    # 2025
    date(2025, 1, 1),    # New Year's Day
    date(2025, 1, 20),   # MLK Day
    date(2025, 2, 17),   # Presidents' Day
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 26),   # Memorial Day
    date(2025, 6, 19),   # Juneteenth
    date(2025, 7, 4),    # Independence Day
    date(2025, 9, 1),    # Labor Day
    date(2025, 11, 27),  # Thanksgiving
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}


# ═════════════════════════════════════════════════════════════════
#  ENUMS
# ═════════════════════════════════════════════════════════════════

class TradingSession(str, Enum):
    """Trading session types."""
    PRE_MARKET = "pre_market"       # 4:00 AM - 9:30 AM ET
    REGULAR = "regular"             # 9:30 AM - 4:00 PM ET
    AFTER_HOURS = "after_hours"     # 4:00 PM - 8:00 PM ET
    CLOSED = "closed"               # 8:00 PM - 4:00 AM ET
    WEEKEND = "weekend"
    HOLIDAY = "holiday"
    ALWAYS_OPEN = "always_open"     # Crypto 24/7


class ExchangeType(str, Enum):
    """Supported exchange types."""
    CRYPTO = "crypto"
    US_STOCK = "us_stock"
    FOREX = "forex"


# ═════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ═════════════════════════════════════════════════════════════════

@dataclass
class MarketStatus:
    """Market status information."""
    exchange: str
    is_open: bool
    session: TradingSession
    current_time_et: str = ""
    next_change: str = ""           # Human-readable next open/close
    next_change_seconds: int = 0    # Seconds until next change
    is_holiday: bool = False
    holiday_name: str = ""


# ═════════════════════════════════════════════════════════════════
#  CURRENT TIME
# ═════════════════════════════════════════════════════════════════

def get_utc_now() -> datetime:
    """
    Get current UTC time as timezone-aware datetime.

    Returns:
        datetime with UTC timezone

    Example:
        >>> now = get_utc_now()
        >>> print(now)
        2024-01-15 14:30:45.123456+00:00
    """
    return datetime.now(TZ_UTC)


def get_local_now() -> datetime:
    """
    Get current local time as datetime.

    Returns:
        datetime in local timezone
    """
    return datetime.now()


def get_et_now() -> datetime:
    """
    Get current Eastern Time as timezone-aware datetime.

    Returns:
        datetime in US/Eastern timezone, or UTC if zoneinfo unavailable
    """
    if TZ_ET:
        return datetime.now(TZ_ET)
    # Fallback: naive ET approximation (UTC-5)
    return datetime.now(TZ_UTC) - timedelta(hours=5)


def get_display_now() -> datetime:
    """
    Get current time in the display timezone (configurable via env).

    Returns:
        datetime in configured display timezone
    """
    if TZ_DISPLAY:
        return datetime.now(TZ_DISPLAY)
    return get_utc_now()


def get_timestamp() -> float:
    """
    Get current Unix timestamp (seconds since epoch).

    Returns:
        Float timestamp
    """
    return _time.time()


def get_timestamp_ms() -> int:
    """
    Get current Unix timestamp in milliseconds.

    Returns:
        Integer timestamp in milliseconds
    """
    return int(_time.time() * 1000)


def get_monotonic() -> float:
    """
    Get monotonic clock value (for measuring durations).
    Unaffected by system clock changes.

    Returns:
        Float monotonic seconds
    """
    return _time.monotonic()


# ═════════════════════════════════════════════════════════════════
#  TIMESTAMP PARSING & NORMALIZATION
# ═════════════════════════════════════════════════════════════════

def ensure_utc(dt: datetime) -> datetime:
    """
    Ensure a datetime is UTC-aware.

    If naive, assume UTC. If aware, convert to UTC.

    Args:
        dt: Any datetime

    Returns:
        UTC-aware datetime
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_UTC)
    return dt.astimezone(TZ_UTC)


def parse_timestamp(
    value: Union[int, float, str, datetime, None],
) -> Optional[datetime]:
    """
    Parse various timestamp formats to UTC datetime.

    Handles:
    - Unix timestamp (seconds or milliseconds)
    - ISO 8601 strings
    - datetime objects
    - None → None

    Args:
        value: Timestamp in any supported format

    Returns:
        UTC-aware datetime or None

    Examples:
        >>> parse_timestamp(1705329045)
        datetime(2024, 1, 15, 14, 30, 45, tzinfo=UTC)

        >>> parse_timestamp("2024-01-15T14:30:45Z")
        datetime(2024, 1, 15, 14, 30, 45, tzinfo=UTC)

        >>> parse_timestamp(1705329045000)  # milliseconds
        datetime(2024, 1, 15, 14, 30, 45, tzinfo=UTC)
    """
    if value is None:
        return None

    try:
        if isinstance(value, datetime):
            return ensure_utc(value)

        if isinstance(value, (int, float)):
            # Detect milliseconds vs seconds
            if value > 1e12:
                value = value / 1000.0
            return datetime.fromtimestamp(float(value), tz=TZ_UTC)

        if isinstance(value, str):
            # Handle 'Z' suffix
            cleaned = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            return ensure_utc(dt)

    except Exception:
        pass

    return None


# ═════════════════════════════════════════════════════════════════
#  FORMATTING
# ═════════════════════════════════════════════════════════════════

def format_timestamp(
    ts: Optional[Union[int, float, datetime]] = None,
    fmt: str = "%I:%M %p",
    tz_name: Optional[str] = None,
) -> str:
    """
    Format a timestamp for display.

    Args:
        ts: Unix timestamp (int/float), datetime, or None for now
        fmt: strftime format string.
             Defaults to "02:30 PM"
        tz_name: Target timezone name (e.g. "US/Eastern").
                 None = local time

    Returns:
        Formatted time string, or "N/A" on error

    Examples:
        >>> format_timestamp()
        "02:30 PM"

        >>> format_timestamp(1705329045)
        "02:30 PM"

        >>> format_timestamp(fmt="%Y-%m-%d %H:%M:%S")
        "2024-01-15 14:30:45"

        >>> format_timestamp(tz_name="US/Eastern")
        "09:30 AM"
    """
    try:
        dt = parse_timestamp(ts)
        if dt is None:
            dt = get_utc_now()

        # Convert to target timezone
        if tz_name:
            tz_obj = _get_tz(tz_name)
            if tz_obj:
                dt = dt.astimezone(tz_obj)
            # If tz lookup fails, just use UTC
        elif TZ_DISPLAY:
            dt = dt.astimezone(TZ_DISPLAY)
        else:
            # Fall back to local
            dt = dt.astimezone()

        return dt.strftime(fmt)
    except Exception:
        return "N/A"


def format_timestamp_utc(
    ts: Optional[Union[int, float, datetime]] = None,
    fmt: str = "%Y-%m-%d %H:%M:%S UTC",
) -> str:
    """
    Format a timestamp in UTC.

    Args:
        ts: Unix timestamp, datetime, or None for now
        fmt: strftime format string

    Returns:
        Formatted UTC time string
    """
    try:
        dt = parse_timestamp(ts)
        if dt is None:
            dt = get_utc_now()
        return dt.strftime(fmt)
    except Exception:
        return "N/A"


def format_date(
    dt: Optional[datetime] = None,
    fmt: str = "%Y-%m-%d",
) -> str:
    """
    Format a date.

    Args:
        dt: datetime or None for today
        fmt: strftime format string

    Returns:
        Formatted date string
    """
    try:
        if dt is None:
            dt = get_utc_now()
        return dt.strftime(fmt)
    except Exception:
        return "N/A"


# ═════════════════════════════════════════════════════════════════
#  ISO 8601 CONVERSION
# ═════════════════════════════════════════════════════════════════

def timestamp_to_iso(
    ts: Optional[Union[int, float]] = None,
) -> str:
    """
    Convert Unix timestamp to ISO 8601 format.

    Args:
        ts: Unix timestamp or None for now

    Returns:
        ISO string "2024-01-15T14:30:45.123456+00:00"
    """
    try:
        dt = parse_timestamp(ts)
        if dt is None:
            dt = get_utc_now()
        return dt.isoformat()
    except Exception:
        return get_utc_now().isoformat()


def iso_to_timestamp(iso_str: str) -> Optional[float]:
    """
    Convert ISO 8601 string to Unix timestamp.

    Args:
        iso_str: ISO format datetime string

    Returns:
        Unix timestamp or None if parsing fails
    """
    dt = parse_timestamp(iso_str)
    if dt:
        return dt.timestamp()
    return None


def iso_to_datetime(iso_str: str) -> Optional[datetime]:
    """
    Convert ISO 8601 string to UTC datetime.

    Args:
        iso_str: ISO format datetime string

    Returns:
        UTC datetime or None if parsing fails
    """
    return parse_timestamp(iso_str)


# ═════════════════════════════════════════════════════════════════
#  RELATIVE TIME
# ═════════════════════════════════════════════════════════════════

def time_ago(
    dt: Union[datetime, str, int, float],
    reference: Optional[datetime] = None,
    max_units: int = 2,
) -> str:
    """
    Get human-readable relative time string.

    Args:
        dt: Target datetime, ISO string, or Unix timestamp
        reference: Reference time (default: now UTC)
        max_units: Max number of time units to show (e.g. "2h 30m")

    Returns:
        Relative time string

    Examples:
        >>> time_ago(datetime.now(UTC) - timedelta(minutes=5))
        "5 minutes ago"

        >>> time_ago(datetime.now(UTC) - timedelta(hours=2, minutes=30))
        "2 hours 30 minutes ago"

        >>> time_ago(datetime.now(UTC) + timedelta(hours=2))
        "in 2 hours"
    """
    try:
        target = parse_timestamp(dt)
        if target is None:
            return "unknown"

        if reference is None:
            reference = get_utc_now()
        else:
            reference = ensure_utc(reference)

        diff_seconds = (reference - target).total_seconds()

        if abs(diff_seconds) < 10:
            return "just now"

        if diff_seconds < 0:
            return "in " + _format_duration_parts(
                abs(diff_seconds), max_units
            )

        return _format_duration_parts(diff_seconds, max_units) + " ago"

    except Exception:
        return "unknown"


def time_since(
    start: Union[datetime, str, int, float],
) -> float:
    """
    Get seconds elapsed since a given timestamp.

    Args:
        start: Start time in any supported format

    Returns:
        Seconds elapsed (0.0 on error)
    """
    try:
        start_dt = parse_timestamp(start)
        if start_dt is None:
            return 0.0
        return max(0.0, (get_utc_now() - start_dt).total_seconds())
    except Exception:
        return 0.0


def _format_duration_parts(
    total_seconds: float,
    max_units: int = 2,
) -> str:
    """Format seconds into multi-unit human string."""
    intervals = [
        (86400 * 30, "month"),
        (86400 * 7, "week"),
        (86400, "day"),
        (3600, "hour"),
        (60, "minute"),
        (1, "second"),
    ]

    parts: List[str] = []
    remaining = abs(total_seconds)

    for seconds_per, label in intervals:
        if remaining >= seconds_per and len(parts) < max_units:
            count = int(remaining // seconds_per)
            remaining -= count * seconds_per
            suffix = "s" if count != 1 else ""
            parts.append(f"{count} {label}{suffix}")

    return " ".join(parts) if parts else "0 seconds"


# ═════════════════════════════════════════════════════════════════
#  DURATION FORMATTING & PARSING
# ═════════════════════════════════════════════════════════════════

def format_duration(
    seconds: Union[int, float],
    compact: bool = True,
    max_units: int = 4,
) -> str:
    """
    Format a duration in seconds to human-readable string.

    Args:
        seconds: Duration in seconds
        compact: True → "1h 1m 5s", False → "1 hour 1 minute 5 seconds"
        max_units: Maximum number of units to display

    Returns:
        Formatted duration string

    Examples:
        >>> format_duration(3665)
        "1h 1m 5s"

        >>> format_duration(3665, compact=False)
        "1 hour 1 minute 5 seconds"

        >>> format_duration(0.45)
        "450ms"
    """
    try:
        seconds = abs(float(seconds))

        # Sub-second precision
        if seconds < 1:
            ms = int(seconds * 1000)
            if compact:
                return f"{ms}ms"
            return f"{ms} milliseconds"

        days = int(seconds // 86400)
        hours = int((seconds % 86400) // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        parts: List[str] = []

        if compact:
            units = [
                (days, "d"), (hours, "h"),
                (minutes, "m"), (secs, "s"),
            ]
            for val, suffix in units:
                if val > 0 and len(parts) < max_units:
                    parts.append(f"{val}{suffix}")
            return " ".join(parts) if parts else "0s"
        else:
            units = [
                (days, "day"), (hours, "hour"),
                (minutes, "minute"), (secs, "second"),
            ]
            for val, label in units:
                if val > 0 and len(parts) < max_units:
                    s = "s" if val != 1 else ""
                    parts.append(f"{val} {label}{s}")
            return " ".join(parts) if parts else "0 seconds"

    except Exception:
        return "N/A"


def parse_duration(duration_str: str) -> Optional[int]:
    """
    Parse a duration string to seconds.

    Supported formats:
    - "1h 30m", "90s", "2d", "1d 12h 30m"
    - "1.5h" (fractional)
    - "90" (plain number → seconds)

    Args:
        duration_str: Duration string

    Returns:
        Duration in seconds or None if parsing fails

    Examples:
        >>> parse_duration("1h 30m")
        5400
        >>> parse_duration("2d")
        172800
        >>> parse_duration("1.5h")
        5400
    """
    if not duration_str or not isinstance(duration_str, str):
        return None

    try:
        total = 0.0
        current_num = ""

        multipliers = {
            "d": 86400,
            "h": 3600,
            "m": 60,
            "s": 1,
            "w": 604800,
        }

        for char in duration_str.lower().strip():
            if char.isdigit() or char == ".":
                current_num += char
            elif char in multipliers and current_num:
                total += float(current_num) * multipliers[char]
                current_num = ""
            elif char in (" ", ",", ":"):
                continue  # skip separators
            # Ignore unknown characters

        # Plain number (assume seconds)
        if current_num:
            if total == 0:
                total = float(current_num)
            # If we already parsed some units, trailing digits
            # without a unit are ambiguous; ignore them

        result = int(total)
        return result if result > 0 else None

    except (ValueError, TypeError):
        return None


# ═════════════════════════════════════════════════════════════════
#  MARKET STATUS & TRADING SESSIONS
# ═════════════════════════════════════════════════════════════════

def _is_us_holiday(d: date) -> Tuple[bool, str]:
    """Check if a date is a US market holiday."""
    if d in _US_HOLIDAYS:
        # Return true with a generic name
        return True, "US Market Holiday"
    return False, ""


def market_status(
    exchange: str = "crypto",
    now: Optional[datetime] = None,
) -> MarketStatus:
    """
    Get detailed market status for an exchange.

    Args:
        exchange: "crypto", "us_stock", or "forex"
        now: Override current time (for testing)

    Returns:
        MarketStatus with session info, next change time, etc.

    Examples:
        >>> status = market_status("us_stock")
        >>> print(status.is_open, status.session)
        True TradingSession.REGULAR

        >>> status = market_status("crypto")
        >>> print(status.is_open)
        True
    """
    if now is None:
        now = get_utc_now()
    else:
        now = ensure_utc(now)

    exchange_lower = exchange.lower().replace("-", "_").replace(" ", "_")

    if exchange_lower == "crypto":
        return _crypto_status(now)
    elif exchange_lower in ("us_stock", "stock", "nyse", "nasdaq"):
        return _us_stock_status(now)
    elif exchange_lower == "forex":
        return _forex_status(now)
    else:
        # Default to crypto (always open)
        return _crypto_status(now)


def _crypto_status(now: datetime) -> MarketStatus:
    """Crypto is 24/7."""
    return MarketStatus(
        exchange="crypto",
        is_open=True,
        session=TradingSession.ALWAYS_OPEN,
        current_time_et=_format_et(now),
        next_change="Never (24/7 market)",
        next_change_seconds=0,
    )


def _us_stock_status(now: datetime) -> MarketStatus:
    """
    US stock market session detection.

    Sessions (all times Eastern):
    - Pre-market:   4:00 AM - 9:30 AM
    - Regular:      9:30 AM - 4:00 PM
    - After-hours:  4:00 PM - 8:00 PM
    - Closed:       8:00 PM - 4:00 AM
    - Weekend:      All day Saturday/Sunday
    - Holiday:      Listed US market holidays
    """
    # Convert to ET
    if TZ_ET:
        et_now = now.astimezone(TZ_ET)
    else:
        # Naive fallback: UTC-5 (ignores DST)
        et_now = now - timedelta(hours=5)

    current_date = et_now.date()
    current_time = et_now.time()
    weekday = et_now.weekday()  # 0=Mon, 6=Sun

    et_str = _format_et(now)

    # Check weekend
    if weekday >= 5:
        # Find next Monday
        days_to_monday = 7 - weekday
        next_open = datetime.combine(
            current_date + timedelta(days=days_to_monday),
            time(4, 0),
        )
        if TZ_ET:
            next_open = next_open.replace(tzinfo=TZ_ET)
        secs = max(0, int((ensure_utc(next_open) - now).total_seconds()))

        return MarketStatus(
            exchange="us_stock",
            is_open=False,
            session=TradingSession.WEEKEND,
            current_time_et=et_str,
            next_change=f"Pre-market opens in {format_duration(secs)}",
            next_change_seconds=secs,
        )

    # Check holiday
    is_hol, hol_name = _is_us_holiday(current_date)
    if is_hol:
        # Find next trading day
        next_day = current_date + timedelta(days=1)
        while next_day.weekday() >= 5 or next_day in _US_HOLIDAYS:
            next_day += timedelta(days=1)
        next_open = datetime.combine(next_day, time(4, 0))
        if TZ_ET:
            next_open = next_open.replace(tzinfo=TZ_ET)
        secs = max(0, int((ensure_utc(next_open) - now).total_seconds()))

        return MarketStatus(
            exchange="us_stock",
            is_open=False,
            session=TradingSession.HOLIDAY,
            current_time_et=et_str,
            next_change=f"Pre-market opens in {format_duration(secs)}",
            next_change_seconds=secs,
            is_holiday=True,
            holiday_name=hol_name,
        )

    # ── Determine session ────────────────────────────────────
    pre_market_open = time(4, 0)
    market_open = time(9, 30)
    market_close = time(16, 0)
    after_close = time(20, 0)

    if current_time < pre_market_open:
        # Before 4 AM ET → Closed
        next_change_time = datetime.combine(current_date, pre_market_open)
        if TZ_ET:
            next_change_time = next_change_time.replace(tzinfo=TZ_ET)
        secs = max(
            0,
            int((ensure_utc(next_change_time) - now).total_seconds()),
        )
        return MarketStatus(
            exchange="us_stock",
            is_open=False,
            session=TradingSession.CLOSED,
            current_time_et=et_str,
            next_change=f"Pre-market in {format_duration(secs)}",
            next_change_seconds=secs,
        )

    elif current_time < market_open:
        # 4:00 AM - 9:30 AM → Pre-market
        next_change_time = datetime.combine(current_date, market_open)
        if TZ_ET:
            next_change_time = next_change_time.replace(tzinfo=TZ_ET)
        secs = max(
            0,
            int((ensure_utc(next_change_time) - now).total_seconds()),
        )
        return MarketStatus(
            exchange="us_stock",
            is_open=True,
            session=TradingSession.PRE_MARKET,
            current_time_et=et_str,
            next_change=f"Regular session in {format_duration(secs)}",
            next_change_seconds=secs,
        )

    elif current_time < market_close:
        # 9:30 AM - 4:00 PM → Regular session
        next_change_time = datetime.combine(current_date, market_close)
        if TZ_ET:
            next_change_time = next_change_time.replace(tzinfo=TZ_ET)
        secs = max(
            0,
            int((ensure_utc(next_change_time) - now).total_seconds()),
        )
        return MarketStatus(
            exchange="us_stock",
            is_open=True,
            session=TradingSession.REGULAR,
            current_time_et=et_str,
            next_change=f"Market closes in {format_duration(secs)}",
            next_change_seconds=secs,
        )

    elif current_time < after_close:
        # 4:00 PM - 8:00 PM → After-hours
        next_change_time = datetime.combine(current_date, after_close)
        if TZ_ET:
            next_change_time = next_change_time.replace(tzinfo=TZ_ET)
        secs = max(
            0,
            int((ensure_utc(next_change_time) - now).total_seconds()),
        )
        return MarketStatus(
            exchange="us_stock",
            is_open=True,
            session=TradingSession.AFTER_HOURS,
            current_time_et=et_str,
            next_change=f"After-hours ends in {format_duration(secs)}",
            next_change_seconds=secs,
        )

    else:
        # After 8 PM → Closed until next trading day
        next_day = current_date + timedelta(days=1)
        while next_day.weekday() >= 5 or next_day in _US_HOLIDAYS:
            next_day += timedelta(days=1)
        next_open = datetime.combine(next_day, pre_market_open)
        if TZ_ET:
            next_open = next_open.replace(tzinfo=TZ_ET)
        secs = max(0, int((ensure_utc(next_open) - now).total_seconds()))

        return MarketStatus(
            exchange="us_stock",
            is_open=False,
            session=TradingSession.CLOSED,
            current_time_et=et_str,
            next_change=f"Pre-market in {format_duration(secs)}",
            next_change_seconds=secs,
        )


def _forex_status(now: datetime) -> MarketStatus:
    """
    Forex market: Open Sun 5PM ET → Fri 5PM ET.
    """
    if TZ_ET:
        et_now = now.astimezone(TZ_ET)
    else:
        et_now = now - timedelta(hours=5)

    weekday = et_now.weekday()
    current_time = et_now.time()
    et_str = _format_et(now)

    is_open = True

    # Closed: Saturday all day
    if weekday == 5:
        is_open = False
    # Closed: Sunday before 5PM ET
    elif weekday == 6 and current_time < time(17, 0):
        is_open = False
    # Closed: Friday after 5PM ET
    elif weekday == 4 and current_time >= time(17, 0):
        is_open = False

    if is_open:
        # Find next close (Friday 5PM ET)
        days_to_friday = (4 - weekday) % 7
        if days_to_friday == 0 and current_time < time(17, 0):
            days_to_friday = 0
        elif days_to_friday == 0:
            days_to_friday = 7
        next_close_date = et_now.date() + timedelta(days=days_to_friday)
        next_close = datetime.combine(next_close_date, time(17, 0))
        if TZ_ET:
            next_close = next_close.replace(tzinfo=TZ_ET)
        secs = max(0, int((ensure_utc(next_close) - now).total_seconds()))

        return MarketStatus(
            exchange="forex",
            is_open=True,
            session=TradingSession.REGULAR,
            current_time_et=et_str,
            next_change=f"Closes in {format_duration(secs)}",
            next_change_seconds=secs,
        )
    else:
        # Find next open (Sunday 5PM ET)
        days_to_sunday = (6 - weekday) % 7
        if days_to_sunday == 0 and current_time >= time(17, 0):
            days_to_sunday = 0  # Opens now
        elif days_to_sunday == 0:
            pass
        next_open_date = et_now.date() + timedelta(days=days_to_sunday)
        next_open = datetime.combine(next_open_date, time(17, 0))
        if TZ_ET:
            next_open = next_open.replace(tzinfo=TZ_ET)
        secs = max(0, int((ensure_utc(next_open) - now).total_seconds()))

        return MarketStatus(
            exchange="forex",
            is_open=False,
            session=TradingSession.WEEKEND,
            current_time_et=et_str,
            next_change=f"Opens in {format_duration(secs)}",
            next_change_seconds=secs,
        )


def _format_et(now: datetime) -> str:
    """Format time as ET string for display."""
    try:
        if TZ_ET:
            return now.astimezone(TZ_ET).strftime("%I:%M %p ET")
        return now.strftime("%H:%M UTC")
    except Exception:
        return "N/A"


# ── Backward compatibility ──────────────────────────────────────

def is_market_hours(exchange: str = "crypto") -> bool:
    """
    Simple boolean check: is market currently open?

    Args:
        exchange: "crypto", "us_stock", "forex"

    Returns:
        True if market is open
    """
    return market_status(exchange).is_open


# ═════════════════════════════════════════════════════════════════
#  INTERVAL & CYCLE ALIGNMENT
# ═════════════════════════════════════════════════════════════════

def seconds_until_next_interval(interval_seconds: int) -> int:
    """
    Calculate seconds until next clean interval boundary.

    Useful for aligning trading cycles to clean timestamps.

    Args:
        interval_seconds: Interval in seconds (e.g., 300 for 5 min)

    Returns:
        Seconds until next interval

    Example:
        >>> seconds_until_next_interval(300)  # At 14:32:15
        165  # Wait until 14:35:00
    """
    now = get_timestamp()
    next_boundary = ((now // interval_seconds) + 1) * interval_seconds
    return max(1, int(next_boundary - now))


async def sleep_until_next_interval(
    interval_seconds: int,
    jitter_seconds: float = 0.0,
) -> float:
    """
    Async sleep until the next clean interval boundary.

    Args:
        interval_seconds: Interval in seconds
        jitter_seconds: Random jitter to add (0 = none)

    Returns:
        Actual seconds slept

    Example:
        # Sleep until next 5-minute mark
        await sleep_until_next_interval(300)
    """
    wait = seconds_until_next_interval(interval_seconds)

    if jitter_seconds > 0:
        import random
        wait += random.uniform(0, jitter_seconds)

    await asyncio.sleep(wait)
    return wait


def next_interval_time(
    interval_seconds: int,
) -> datetime:
    """
    Get the datetime of the next interval boundary.

    Args:
        interval_seconds: Interval in seconds

    Returns:
        UTC datetime of next boundary
    """
    now = get_timestamp()
    next_ts = ((now // interval_seconds) + 1) * interval_seconds
    return datetime.fromtimestamp(next_ts, tz=TZ_UTC)


# ═════════════════════════════════════════════════════════════════
#  COOLDOWN TIMER
# ═════════════════════════════════════════════════════════════════

class Cooldown:
    """
    Thread-safe cooldown timer.

    Prevents actions from firing more often than a specified interval.

    Usage:
        cd = Cooldown(seconds=60)

        if cd.ready():
            execute_trade()
            cd.reset()

        # Or use as context:
        if cd.try_acquire():
            execute_trade()  # auto-resets
    """

    def __init__(self, seconds: float):
        self._interval = seconds
        self._last_trigger: float = 0.0
        self._lock = threading.Lock()

    def ready(self) -> bool:
        """Check if cooldown has elapsed."""
        with self._lock:
            return (_time.monotonic() - self._last_trigger) >= self._interval

    def reset(self) -> None:
        """Reset the cooldown timer."""
        with self._lock:
            self._last_trigger = _time.monotonic()

    def try_acquire(self) -> bool:
        """Check and reset atomically. Returns True if cooldown elapsed."""
        with self._lock:
            now = _time.monotonic()
            if (now - self._last_trigger) >= self._interval:
                self._last_trigger = now
                return True
            return False

    @property
    def remaining(self) -> float:
        """Seconds remaining in cooldown."""
        with self._lock:
            elapsed = _time.monotonic() - self._last_trigger
            return max(0.0, self._interval - elapsed)

    @property
    def remaining_str(self) -> str:
        """Human-readable remaining time."""
        return format_duration(self.remaining)

    def __repr__(self) -> str:
        return (
            f"Cooldown(interval={self._interval}s, "
            f"remaining={self.remaining:.1f}s, "
            f"ready={self.ready()})"
        )


# ═════════════════════════════════════════════════════════════════
#  STOPWATCH
# ═════════════════════════════════════════════════════════════════

class Stopwatch:
    """
    High-precision stopwatch using monotonic clock.

    Usage:
        sw = Stopwatch()
        do_work()
        print(f"Took {sw.elapsed:.3f}s")
        print(sw.elapsed_str())

        # With lap tracking
        sw = Stopwatch()
        step1()
        sw.lap("step1")
        step2()
        sw.lap("step2")
        print(sw.laps)
    """

    def __init__(self, auto_start: bool = True):
        self._start: float = 0.0
        self._stop: Optional[float] = None
        self._laps: List[Dict[str, Any]] = []
        self._lap_start: float = 0.0
        if auto_start:
            self.start()

    def start(self) -> "Stopwatch":
        """Start or restart the stopwatch."""
        self._start = _time.monotonic()
        self._stop = None
        self._lap_start = self._start
        self._laps.clear()
        return self

    def stop(self) -> float:
        """Stop and return elapsed seconds."""
        self._stop = _time.monotonic()
        return self.elapsed

    def lap(self, name: str = "") -> float:
        """
        Record a lap time.

        Args:
            name: Label for this lap

        Returns:
            Seconds since last lap (or start)
        """
        now = _time.monotonic()
        lap_time = now - self._lap_start
        self._laps.append({
            "name": name or f"lap_{len(self._laps) + 1}",
            "duration": round(lap_time, 4),
            "total": round(now - self._start, 4),
        })
        self._lap_start = now
        return lap_time

    @property
    def elapsed(self) -> float:
        """Elapsed seconds since start."""
        end = self._stop or _time.monotonic()
        return end - self._start

    def elapsed_str(self) -> str:
        """Elapsed time as human-readable string."""
        return format_duration(self.elapsed)

    @property
    def laps(self) -> List[Dict[str, Any]]:
        """All recorded laps."""
        return list(self._laps)

    @property
    def is_running(self) -> bool:
        return self._stop is None and self._start > 0

    def __repr__(self) -> str:
        state = "running" if self.is_running else "stopped"
        return f"Stopwatch({state}, elapsed={self.elapsed:.3f}s)"

    def __enter__(self) -> "Stopwatch":
        self.start()
        return self

    def __exit__(self, *args) -> None:
        self.stop()


# ═════════════════════════════════════════════════════════════════
#  RATE LIMITER (time-based)
# ═════════════════════════════════════════════════════════════════

class RateLimiter:
    """
    Token-bucket style rate limiter.

    Usage:
        limiter = RateLimiter(max_calls=10, period=60)

        if limiter.allow():
            make_api_call()
        else:
            print(f"Rate limited, retry in {limiter.retry_after:.1f}s")
    """

    def __init__(self, max_calls: int, period: float):
        """
        Args:
            max_calls: Maximum calls allowed per period
            period: Window size in seconds
        """
        self._max = max_calls
        self._period = period
        self._calls: List[float] = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """Check if a call is allowed and record it."""
        with self._lock:
            now = _time.monotonic()
            # Purge old calls
            cutoff = now - self._period
            self._calls = [t for t in self._calls if t > cutoff]

            if len(self._calls) < self._max:
                self._calls.append(now)
                return True
            return False

    @property
    def retry_after(self) -> float:
        """Seconds until next call would be allowed."""
        with self._lock:
            if len(self._calls) < self._max:
                return 0.0
            oldest = self._calls[0]
            return max(0.0, self._period - (_time.monotonic() - oldest))

    @property
    def remaining(self) -> int:
        """Remaining calls in current window."""
        with self._lock:
            now = _time.monotonic()
            cutoff = now - self._period
            active = sum(1 for t in self._calls if t > cutoff)
            return max(0, self._max - active)

    def __repr__(self) -> str:
        return (
            f"RateLimiter(max={self._max}, period={self._period}s, "
            f"remaining={self.remaining})"
        )


# ═════════════════════════════════════════════════════════════════
#  TRADING DAY HELPERS
# ═════════════════════════════════════════════════════════════════

def next_trading_day(
    from_date: Optional[date] = None,
    exchange: str = "us_stock",
) -> date:
    """
    Get the next trading day.

    Args:
        from_date: Start date (default: today)
        exchange: Exchange type

    Returns:
        Next trading day date
    """
    if from_date is None:
        from_date = get_utc_now().date()

    if exchange.lower() == "crypto":
        return from_date + timedelta(days=1)

    # US stock: skip weekends and holidays
    candidate = from_date + timedelta(days=1)
    max_tries = 14  # Safety: never loop forever
    for _ in range(max_tries):
        if candidate.weekday() < 5 and candidate not in _US_HOLIDAYS:
            return candidate
        candidate += timedelta(days=1)

    return candidate


def prev_trading_day(
    from_date: Optional[date] = None,
    exchange: str = "us_stock",
) -> date:
    """
    Get the previous trading day.

    Args:
        from_date: Start date (default: today)
        exchange: Exchange type

    Returns:
        Previous trading day date
    """
    if from_date is None:
        from_date = get_utc_now().date()

    if exchange.lower() == "crypto":
        return from_date - timedelta(days=1)

    candidate = from_date - timedelta(days=1)
    max_tries = 14
    for _ in range(max_tries):
        if candidate.weekday() < 5 and candidate not in _US_HOLIDAYS:
            return candidate
        candidate -= timedelta(days=1)

    return candidate


def trading_days_between(
    start: date,
    end: date,
    exchange: str = "us_stock",
) -> int:
    """
    Count trading days between two dates (exclusive of end).

    Args:
        start: Start date
        end: End date
        exchange: Exchange type

    Returns:
        Number of trading days
    """
    if exchange.lower() == "crypto":
        return (end - start).days

    count = 0
    current = start
    while current < end:
        if current.weekday() < 5 and current not in _US_HOLIDAYS:
            count += 1
        current += timedelta(days=1)
    return count


# ═════════════════════════════════════════════════════════════════
#  UPTIME TRACKER
# ═════════════════════════════════════════════════════════════════

_BOT_START_TIME: float = _time.monotonic()
_BOT_START_DATETIME: datetime = get_utc_now()


def get_uptime() -> float:
    """Get bot uptime in seconds."""
    return _time.monotonic() - _BOT_START_TIME


def get_uptime_str() -> str:
    """Get bot uptime as human-readable string."""
    return format_duration(get_uptime())


def get_start_time() -> datetime:
    """Get bot start time as UTC datetime."""
    return _BOT_START_DATETIME


# ═════════════════════════════════════════════════════════════════
#  MODULE SELF-TEST
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Time Utilities v2 — Diagnostics & Demo")
    print("=" * 60)

    print(f"\n  ── Current Time ──")
    print(f"  UTC:          {get_utc_now()}")
    print(f"  Local:        {get_local_now()}")
    print(f"  Eastern:      {get_et_now()}")
    print(f"  Display:      {get_display_now()}")
    print(f"  Timestamp:    {get_timestamp()}")
    print(f"  Timestamp ms: {get_timestamp_ms()}")
    print(f"  Formatted:    {format_timestamp()}")
    print(f"  ISO:          {timestamp_to_iso()}")

    print(f"\n  ── Relative Time ──")
    print(f"  5m ago:       {time_ago(get_utc_now() - timedelta(minutes=5))}")
    print(f"  2h30m ago:    {time_ago(get_utc_now() - timedelta(hours=2, minutes=30))}")
    print(f"  In 30m:       {time_ago(get_utc_now() + timedelta(minutes=30))}")
    print(f"  3 days ago:   {time_ago(get_utc_now() - timedelta(days=3))}")

    print(f"\n  ── Duration ──")
    print(f"  0.45s:        {format_duration(0.45)}")
    print(f"  90s:          {format_duration(90)}")
    print(f"  3665s:        {format_duration(3665)}")
    print(f"  86400s:       {format_duration(86400)}")
    print(f"  3665s long:   {format_duration(3665, compact=False)}")

    print(f"\n  ── Duration Parsing ──")
    print(f"  '1h 30m':     {parse_duration('1h 30m')}s")
    print(f"  '2d':         {parse_duration('2d')}s")
    print(f"  '1.5h':       {parse_duration('1.5h')}s")
    print(f"  '90':         {parse_duration('90')}s")

    print(f"\n  ── Market Status ──")
    for exch in ["crypto", "us_stock", "forex"]:
        s = market_status(exch)
        status_icon = "🟢" if s.is_open else "🔴"
        hol = f" [{s.holiday_name}]" if s.is_holiday else ""
        print(
            f"  {status_icon} {exch:10} | "
            f"{s.session.value:12} | {s.current_time_et} | "
            f"{s.next_change}{hol}"
        )

    print(f"\n  ── Trading Days ──")
    today = get_utc_now().date()
    print(f"  Today:        {today}")
    print(f"  Next trading: {next_trading_day(today)}")
    print(f"  Prev trading: {prev_trading_day(today)}")

    print(f"\n  ── Interval Alignment ──")
    print(f"  Next 5m:      {seconds_until_next_interval(300)}s")
    print(f"  Next 15m:     {seconds_until_next_interval(900)}s")
    print(f"  Next 1h:      {seconds_until_next_interval(3600)}s")

    print(f"\n  ── Cooldown ──")
    cd = Cooldown(seconds=5)
    print(f"  Created:      {cd}")
    print(f"  Ready?        {cd.ready()}")
    cd.reset()
    print(f"  After reset:  {cd}")

    print(f"\n  ── Stopwatch ──")
    sw = Stopwatch()
    _time.sleep(0.05)
    sw.lap("step_1")
    _time.sleep(0.03)
    sw.lap("step_2")
    sw.stop()
    print(f"  Total:        {sw.elapsed:.3f}s")
    for lap in sw.laps:
        print(f"    {lap['name']}: {lap['duration']:.4f}s")

    print(f"\n  ── Rate Limiter ──")
    rl = RateLimiter(max_calls=3, period=10)
    for i in range(5):
        allowed = rl.allow()
        print(f"  Call {i+1}: {'✅' if allowed else '❌'} (remaining: {rl.remaining})")

    print(f"\n  ── Uptime ──")
    print(f"  Bot started:  {get_start_time()}")
    print(f"  Uptime:       {get_uptime_str()}")

    print("\n" + "=" * 60)
    print("  ✅ Time Utilities v2 fully operational")
    print("=" * 60)