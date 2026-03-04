# app/utils/__init__.py

"""
Utilities Module — Common Helpers v2

This module provides shared utility functions used across the trading bot:

Components:
───────────
• logger     - Enhanced logging with colors, JSON, trade logs, context, masking
• time       - Time formatting, market status, cooldowns, stopwatch, rate limiting

Usage:
──────
    # Logger
    from app.utils import get_logger
    
    logger = get_logger(__name__)
    logger.info("Hello from my module!")
    
    # Context-aware logging
    ctx = logger.with_context(symbol="BTC/USD", trade_id="T123")
    ctx.info("Order filled at $67,000")
    
    # Trade logging
    logger.trade("OPEN", "BTC/USD", "BUY", qty=0.5, price=67000)
    
    # Performance timing
    with logger.perf("data_fetch"):
        data = await fetch_data()
    
    # Time utilities
    from app.utils import format_timestamp, get_utc_now, market_status
    
    formatted = format_timestamp()              # "02:30 PM"
    now = get_utc_now()                          # datetime (UTC-aware)
    status = market_status("us_stock")           # MarketStatus dataclass
    
    # Cooldowns & timers
    from app.utils import Cooldown, Stopwatch
    
    cd = Cooldown(seconds=60)
    if cd.try_acquire():
        execute_trade()
    
    sw = Stopwatch()
    do_work()
    print(f"Took {sw.elapsed_str()}")

Logger Features:
────────────────
• Colorized console output (green=INFO, yellow=WARNING, red=ERROR)
• Rotating file logs (5 MB max, 5 backups)
• Structured JSON log file (bot_json.log)
• Dedicated trade log file (trades.log)
• Context-aware logging (.with_context())
• Sensitive data auto-masking (API keys, tokens)
• Rate-limited duplicate suppression
• In-memory ring buffer for diagnostics
• Critical alert buffer for Telegram
• Performance timing (.perf(), @log_timing)
• Millisecond timestamps with timezone
• Thread-safe and async-safe

Time Features:
──────────────
• UTC-aware timestamps with zoneinfo support
• Market status for crypto / US stocks / forex
• Trading session detection (pre-market, regular, after-hours)
• US market holiday calendar (2024–2026)
• Cooldown timers (thread-safe)
• Stopwatch with lap tracking
• Rate limiter (token-bucket)
• Duration parsing ("1h 30m" → 5400)
• Interval alignment for trading cycles
• Async sleep helpers with jitter
• Trading day navigation
• Bot uptime tracking
"""

# ═════════════════════════════════════════════════════════════════
#  LOGGER EXPORTS
# ═════════════════════════════════════════════════════════════════

from app.utils.logger import (
    # Core
    get_logger,
    log_trade,
    log_timing,
    # Configuration
    set_console_level,
    set_file_level,
    enable_debug_mode,
    disable_debug_mode,
    disable_colors,
    # Diagnostics
    get_log_file_path,
    get_log_stats,
    get_recent_logs,
    get_pending_alerts,
    get_alert_count,
    rotate_logs_now,
    clear_memory_buffer,
)

# ═════════════════════════════════════════════════════════════════
#  TIME EXPORTS
# ═════════════════════════════════════════════════════════════════

from app.utils.time import (
    # Current time
    get_utc_now,
    get_local_now,
    get_et_now,
    get_display_now,
    get_timestamp,
    get_timestamp_ms,
    get_monotonic,
    # Parsing & normalization
    parse_timestamp,
    ensure_utc,
    # Formatting
    format_timestamp,
    format_timestamp_utc,
    format_date,
    format_duration,
    parse_duration,
    # ISO conversion
    timestamp_to_iso,
    iso_to_timestamp,
    iso_to_datetime,
    # Relative time
    time_ago,
    time_since,
    # Market status
    market_status,
    is_market_hours,
    MarketStatus,
    TradingSession,
    ExchangeType,
    # Interval alignment
    seconds_until_next_interval,
    sleep_until_next_interval,
    next_interval_time,
    # Trading days
    next_trading_day,
    prev_trading_day,
    trading_days_between,
    # Timers & tools
    Cooldown,
    Stopwatch,
    RateLimiter,
    # Uptime
    get_uptime,
    get_uptime_str,
    get_start_time,
)

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═════════════════════════════════════════════════════════════════

__all__ = [
    # ── Logger ──────────────────────────────────────────────────
    "get_logger",
    "log_trade",
    "log_timing",
    "set_console_level",
    "set_file_level",
    "enable_debug_mode",
    "disable_debug_mode",
    "disable_colors",
    "get_log_file_path",
    "get_log_stats",
    "get_recent_logs",
    "get_pending_alerts",
    "get_alert_count",
    "rotate_logs_now",
    "clear_memory_buffer",

    # ── Time — Current ──────────────────────────────────────────
    "get_utc_now",
    "get_local_now",
    "get_et_now",
    "get_display_now",
    "get_timestamp",
    "get_timestamp_ms",
    "get_monotonic",

    # ── Time — Parsing ──────────────────────────────────────────
    "parse_timestamp",
    "ensure_utc",

    # ── Time — Formatting ───────────────────────────────────────
    "format_timestamp",
    "format_timestamp_utc",
    "format_date",
    "format_duration",
    "parse_duration",

    # ── Time — ISO ──────────────────────────────────────────────
    "timestamp_to_iso",
    "iso_to_timestamp",
    "iso_to_datetime",

    # ── Time — Relative ─────────────────────────────────────────
    "time_ago",
    "time_since",

    # ── Time — Market ───────────────────────────────────────────
    "market_status",
    "is_market_hours",
    "MarketStatus",
    "TradingSession",
    "ExchangeType",

    # ── Time — Intervals ────────────────────────────────────────
    "seconds_until_next_interval",
    "sleep_until_next_interval",
    "next_interval_time",

    # ── Time — Trading Days ─────────────────────────────────────
    "next_trading_day",
    "prev_trading_day",
    "trading_days_between",

    # ── Time — Timers & Tools ───────────────────────────────────
    "Cooldown",
    "Stopwatch",
    "RateLimiter",

    # ── Time — Uptime ───────────────────────────────────────────
    "get_uptime",
    "get_uptime_str",
    "get_start_time",
]

__version__ = "2.0.0"


# ═════════════════════════════════════════════════════════════════
#  MODULE INFO
# ═════════════════════════════════════════════════════════════════

def get_utils_info() -> dict:
    """
    Get comprehensive information about the utils module.

    Returns:
        Dict with version, component details, log stats, and uptime
    """
    log_stats = get_log_stats()
    mkt = market_status("us_stock")

    return {
        "version": __version__,
        "components": {
            "logger": {
                "description": "Enhanced logging with colors, JSON, trade logs",
                "features": [
                    "colored_console",
                    "rotating_files",
                    "json_structured",
                    "trade_log",
                    "context_logging",
                    "sensitive_masking",
                    "rate_limiting",
                    "memory_buffer",
                    "alert_buffer",
                    "performance_timing",
                ],
            },
            "time": {
                "description": "Time utilities, market status, timers",
                "features": [
                    "utc_aware",
                    "market_status",
                    "trading_sessions",
                    "holiday_calendar",
                    "cooldown_timer",
                    "stopwatch",
                    "rate_limiter",
                    "duration_parsing",
                    "interval_alignment",
                    "uptime_tracking",
                ],
            },
        },
        "log_stats": log_stats,
        "uptime": get_uptime_str(),
        "market": {
            "us_stock_open": mkt.is_open,
            "session": mkt.session.value,
            "next_change": mkt.next_change,
        },
        "exports_count": len(__all__),
    }


# ═════════════════════════════════════════════════════════════════
#  MODULE SELF-TEST
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Run utils module diagnostics.

    Usage:
        python -m app.utils
    """
    print("=" * 64)
    print("  Utils Module v2 — Diagnostics")
    print("=" * 64)

    info = get_utils_info()

    print(f"\n  Version:  {info['version']}")
    print(f"  Exports:  {info['exports_count']} symbols")
    print(f"  Uptime:   {info['uptime']}")

    # ── Components ──
    print(f"\n  ── Components ──")
    for name, comp in info["components"].items():
        print(f"\n  📦 {name}: {comp['description']}")
        features = ", ".join(comp["features"][:5])
        remaining = len(comp["features"]) - 5
        extra = f" (+{remaining} more)" if remaining > 0 else ""
        print(f"     Features: {features}{extra}")

    # ── Log Stats ──
    print(f"\n  ── Log Stats ──")
    stats = info["log_stats"]
    print(f"  Directory:      {stats.get('log_dir', 'N/A')}")
    print(f"  Total size:     {stats.get('total_size_mb', 0)} MB")
    print(f"  Console level:  {stats.get('console_level', 'N/A')}")
    print(f"  File level:     {stats.get('file_level', 'N/A')}")
    print(f"  JSON logging:   {stats.get('json_logging', False)}")
    print(f"  Memory buffer:  {stats.get('memory_buffer_size', 0)} entries")
    print(f"  Pending alerts: {stats.get('pending_alerts', 0)}")
    for fname, finfo in stats.get("files", {}).items():
        print(f"    {fname}: {finfo['size_mb']} MB ({finfo['backups']} backups)")

    # ── Market Status ──
    print(f"\n  ── Market Status ──")
    for exchange in ["crypto", "us_stock", "forex"]:
        s = market_status(exchange)
        icon = "🟢" if s.is_open else "🔴"
        hol = f" [{s.holiday_name}]" if s.is_holiday else ""
        print(
            f"  {icon} {exchange:10} | {s.session.value:12} | "
            f"{s.current_time_et} | {s.next_change}{hol}"
        )

    # ── Time Functions ──
    print(f"\n  ── Time Functions ──")
    print(f"  UTC now:        {get_utc_now()}")
    print(f"  Formatted:      {format_timestamp()}")
    print(f"  ISO:            {timestamp_to_iso()}")
    print(f"  Uptime:         {get_uptime_str()}")
    print(f"  Next 5m:        {seconds_until_next_interval(300)}s")

    # ── Logger Test ──
    print(f"\n  ── Logger Test ──")
    logger = get_logger("utils_test")
    logger.info("Utils module v2 is fully operational!")

    ctx = logger.with_context(symbol="BTC/USD", strategy="scalp")
    ctx.info("Context logging working")

    logger.trade("TEST", "BTC/USD", "BUY", qty=0.01, price=67000.0)

    # ── Cooldown Test ──
    print(f"\n  ── Cooldown Test ──")
    cd = Cooldown(seconds=5)
    print(f"  Ready: {cd.ready()}, Remaining: {cd.remaining_str}")
    acquired = cd.try_acquire()
    print(f"  Acquired: {acquired}, Remaining: {cd.remaining_str}")

    # ── Stopwatch Test ──
    print(f"\n  ── Stopwatch Test ──")
    import time as _t
    sw = Stopwatch()
    _t.sleep(0.02)
    sw.lap("init")
    _t.sleep(0.01)
    sw.stop()
    print(f"  Elapsed: {sw.elapsed:.3f}s, Laps: {len(sw.laps)}")

    # ── Rate Limiter Test ──
    print(f"\n  ── Rate Limiter Test ──")
    rl = RateLimiter(max_calls=3, period=10)
    results = [rl.allow() for _ in range(5)]
    print(f"  5 calls: {['✅' if r else '❌' for r in results]}")
    print(f"  Remaining: {rl.remaining}")

    print("\n" + "=" * 64)
    print("  ✅ Utils Module v2 — All systems operational")
    print("=" * 64)