# app/utils/logger.py

"""
Enhanced Logger — Production Grade v2

Features:
- Colorized console output (INFO=green, WARNING=yellow, ERROR=red)
- Rotating file logs (5 MB max, 5 backups)
- Structured JSON log file for machine parsing
- Millisecond timestamps with timezone awareness
- Module name tracking with context (trade_id, symbol, etc.)
- Thread-safe and async-safe
- Sensitive data masking (API keys, secrets)
- Performance timing decorators
- Rate-limited logging (prevent log spam)
- In-memory ring buffer for recent logs (diagnostics)
- Trade-specific structured logging
- Telegram-ready critical log buffering
- Works on Windows and Unix

Log Levels:
    DEBUG:    Detailed diagnostic info (file only by default)
    INFO:     General operational messages (green)
    WARNING:  Something unexpected but not critical (yellow)
    ERROR:    Something failed (red)
    CRITICAL: System is unusable (magenta)

Usage:
    from app.utils.logger import get_logger

    logger = get_logger(__name__)

    logger.debug("Detailed info for debugging")
    logger.info("Bot started successfully")
    logger.warning("Connection retry needed")
    logger.error("Trade execution failed")
    logger.critical("System shutdown required")

    # Context-aware logging
    ctx_logger = get_logger(__name__).with_context(symbol="BTC/USD", trade_id="T123")
    ctx_logger.info("Order filled")

    # Trade logging
    from app.utils.logger import log_trade
    log_trade("BUY", "BTC/USD", qty=0.5, price=67000.0)

    # Performance timing
    from app.utils.logger import log_timing
    @log_timing("fetch_bars")
    async def fetch_bars(): ...

Log Files:
    logs/bot.log           - Human-readable current log
    logs/bot.log.1-.5      - Rotated backups
    logs/bot_json.log      - Structured JSON log (machine-readable)
    logs/trades.log        - Trade-only log
"""

import asyncio
import functools
import json
import logging
import os
import re
import sys
import time
import threading
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler, MemoryHandler
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Union


# ═════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═════════════════════════════════════════════════════════════════

LOG_DIR = Path("logs")
LOG_FILE = "bot.log"
JSON_LOG_FILE = "bot_json.log"
TRADE_LOG_FILE = "trades.log"

# File settings
MAX_FILE_SIZE = 5 * 1024 * 1024   # 5 MB
BACKUP_COUNT = 5

# Read from environment or use defaults
DEFAULT_FILE_LEVEL = getattr(
    logging, os.getenv("LOG_FILE_LEVEL", "DEBUG").upper(), logging.DEBUG
)
DEFAULT_CONSOLE_LEVEL = getattr(
    logging, os.getenv("LOG_CONSOLE_LEVEL", "INFO").upper(), logging.INFO
)
JSON_LOGGING_ENABLED = os.getenv("JSON_LOGGING", "true").lower() == "true"

# In-memory ring buffer size (for diagnostics endpoint)
MEMORY_BUFFER_SIZE = 500

# Rate-limit: max identical messages per window
RATE_LIMIT_WINDOW = 60        # seconds
RATE_LIMIT_MAX_DUPES = 5      # max identical messages per window

# Ensure logs directory exists
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ═════════════════════════════════════════════════════════════════
#  SENSITIVE DATA PATTERNS (auto-masked in logs)
# ═════════════════════════════════════════════════════════════════

_SENSITIVE_PATTERNS = [
    # API keys / secrets (generic)
    (re.compile(r'(?i)(api[_-]?key|secret|password|token|authorization)["\s:=]+\S+'),
     r'\1=***REDACTED***'),
    # Alpaca keys (PK... / SK...)
    (re.compile(r'\b(PK[A-Za-z0-9]{16,})\b'), '***ALPACA_KEY***'),
    (re.compile(r'\b(SK[A-Za-z0-9]{16,})\b'), '***ALPACA_SECRET***'),
    # Telegram bot token
    (re.compile(r'\b(\d{8,}:[A-Za-z0-9_-]{30,})\b'), '***TG_TOKEN***'),
    # Generic long hex/base64 strings (likely keys)
    (re.compile(r'\b([A-Fa-f0-9]{40,})\b'), '***HEX_REDACTED***'),
]


def _mask_sensitive(msg: str) -> str:
    """Mask sensitive data in log messages."""
    if not isinstance(msg, str):
        return msg
    for pattern, replacement in _SENSITIVE_PATTERNS:
        msg = pattern.sub(replacement, msg)
    return msg


# ═════════════════════════════════════════════════════════════════
#  COLOR CODES (ANSI)
# ═════════════════════════════════════════════════════════════════

class Colors:
    """ANSI color codes for terminal output."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"

    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"


def _supports_color() -> bool:
    """Check if the terminal supports ANSI colors."""
    if not hasattr(sys.stdout, "isatty"):
        return False
    if not sys.stdout.isatty():
        return False

    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(
                kernel32.GetStdHandle(-11), 7
            )
            return True
        except Exception:
            try:
                import colorama
                colorama.init()
                return True
            except ImportError:
                return False

    return True


COLORS_ENABLED = _supports_color()


# ═════════════════════════════════════════════════════════════════
#  LEVEL MAPPINGS
# ═════════════════════════════════════════════════════════════════

LEVEL_COLORS = {
    logging.DEBUG:    Colors.CYAN,
    logging.INFO:     Colors.GREEN,
    logging.WARNING:  Colors.YELLOW,
    logging.ERROR:    Colors.RED,
    logging.CRITICAL: Colors.BRIGHT_MAGENTA + Colors.BOLD,
}

LEVEL_ICONS = {
    logging.DEBUG:    "🔍",
    logging.INFO:     "ℹ️ ",
    logging.WARNING:  "⚠️ ",
    logging.ERROR:    "❌",
    logging.CRITICAL: "🚨",
}


# ═════════════════════════════════════════════════════════════════
#  RATE LIMITER (prevent log spam)
# ═════════════════════════════════════════════════════════════════

class _RateLimitFilter(logging.Filter):
    """
    Suppresses repeated identical messages within a time window.
    After RATE_LIMIT_MAX_DUPES identical messages, further copies
    are suppressed until the window resets, then a summary is logged.
    """

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        # key = (logger_name, msg) → [count, first_seen, suppressed_count]
        self._seen: Dict[tuple, list] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.name, record.getMessage())
        now = time.monotonic()

        with self._lock:
            if key not in self._seen:
                self._seen[key] = [1, now, 0]
                return True

            entry = self._seen[key]
            elapsed = now - entry[1]

            # Window expired — reset
            if elapsed > RATE_LIMIT_WINDOW:
                suppressed = entry[2]
                entry[0] = 1
                entry[1] = now
                entry[2] = 0
                if suppressed > 0:
                    record.msg = (
                        f"{record.msg}  "
                        f"[+{suppressed} identical messages suppressed "
                        f"in last {RATE_LIMIT_WINDOW}s]"
                    )
                return True

            entry[0] += 1

            if entry[0] <= RATE_LIMIT_MAX_DUPES:
                return True

            # Suppress
            entry[2] += 1
            return False


_rate_filter = _RateLimitFilter()


# ═════════════════════════════════════════════════════════════════
#  MASKING FILTER
# ═════════════════════════════════════════════════════════════════

class _SensitiveFilter(logging.Filter):
    """Automatically masks sensitive data in all log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _mask_sensitive(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _mask_sensitive(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    _mask_sensitive(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
        return True


_sensitive_filter = _SensitiveFilter()


# ═════════════════════════════════════════════════════════════════
#  IN-MEMORY RING BUFFER (for /logs endpoint or diagnostics)
# ═════════════════════════════════════════════════════════════════

class _RingBufferHandler(logging.Handler):
    """
    Keeps the last N log records in memory.
    Useful for a /logs API endpoint or Telegram diagnostics command.
    """

    def __init__(self, capacity: int = MEMORY_BUFFER_SIZE):
        super().__init__()
        self._buffer: Deque[Dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])

        with self._lock:
            self._buffer.append(entry)

    def get_records(
        self,
        level: Optional[str] = None,
        last_n: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Retrieve recent log records.

        Args:
            level: Filter by level name (e.g. "ERROR")
            last_n: Number of recent records to return
        """
        with self._lock:
            records = list(self._buffer)

        if level:
            records = [r for r in records if r["level"] == level.upper()]

        return records[-last_n:]

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


_ring_buffer = _RingBufferHandler(MEMORY_BUFFER_SIZE)


# ═════════════════════════════════════════════════════════════════
#  CRITICAL LOG BUFFER (for Telegram notifications)
# ═════════════════════════════════════════════════════════════════

class _CriticalBuffer:
    """
    Collects ERROR and CRITICAL messages for batch notification
    via Telegram or other alert channels.
    """

    def __init__(self, max_size: int = 100):
        self._buffer: Deque[Dict[str, Any]] = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def add(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        with self._lock:
            self._buffer.append(entry)

    def drain(self) -> List[Dict[str, Any]]:
        """Return all buffered alerts and clear the buffer."""
        with self._lock:
            items = list(self._buffer)
            self._buffer.clear()
            return items

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._buffer)


_critical_buffer = _CriticalBuffer()


class _CriticalBufferHandler(logging.Handler):
    """Forwards ERROR+ records to the critical buffer."""

    def __init__(self):
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        _critical_buffer.add(record)


# ═════════════════════════════════════════════════════════════════
#  CUSTOM FORMATTERS
# ═════════════════════════════════════════════════════════════════

class ColoredFormatter(logging.Formatter):
    """
    Colorized console formatter.
    Format: [HH:MM:SS.mmm] [LEVEL] [short_module] message
    """

    def __init__(
        self,
        fmt: Optional[str] = None,
        datefmt: Optional[str] = None,
        use_colors: bool = True,
        use_icons: bool = True,
    ):
        super().__init__(fmt, datefmt)
        self.use_colors = use_colors and COLORS_ENABLED
        self.use_icons = use_icons

    def format(self, record: logging.LogRecord) -> str:
        original_levelname = record.levelname
        original_msg = record.msg
        original_name = record.name

        # Shorten module name: app.exchange.alpaca → alpaca
        parts = record.name.split(".")
        record.name = parts[-1] if len(parts) > 1 else record.name

        if self.use_colors:
            color = LEVEL_COLORS.get(record.levelno, Colors.RESET)
            record.levelname = f"{color}{record.levelname:<8}{Colors.RESET}"
            record.name = f"{Colors.DIM}{record.name}{Colors.RESET}"
        else:
            record.levelname = f"{record.levelname:<8}"

        if self.use_icons:
            icon = LEVEL_ICONS.get(record.levelno, "  ")
            record.msg = f"{icon} {record.msg}"

        result = super().format(record)

        record.levelname = original_levelname
        record.msg = original_msg
        record.name = original_name

        return result


class FileFormatter(logging.Formatter):
    """
    Plain-text file formatter with full detail.
    Format: [YYYY-MM-DD HH:MM:SS.mmm] [LEVEL] [module] message
    """

    def __init__(self):
        super().__init__(
            fmt=(
                "[%(asctime)s.%(msecs)03d] [%(levelname)-8s] "
                "[%(name)s] %(message)s"
            ),
            datefmt="%Y-%m-%d %H:%M:%S",
        )


class JSONFormatter(logging.Formatter):
    """
    Structured JSON formatter for machine parsing.
    Each log line is a valid JSON object.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }

        # Include extra context fields if present
        for key in ("symbol", "trade_id", "side", "qty", "price",
                     "strategy", "action", "pnl", "correlation_id"):
            val = getattr(record, key, None)
            if val is not None:
                log_obj[key] = val

        if record.exc_info and record.exc_info[1]:
            log_obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_obj, default=str)


class TradeFormatter(logging.Formatter):
    """Formatter specifically for the trades log file."""

    def __init__(self):
        super().__init__(
            fmt=(
                "[%(asctime)s.%(msecs)03d] %(message)s"
            ),
            datefmt="%Y-%m-%d %H:%M:%S",
        )


# ═════════════════════════════════════════════════════════════════
#  CONTEXT-AWARE LOGGER ADAPTER
# ═════════════════════════════════════════════════════════════════

class ContextLogger(logging.LoggerAdapter):
    """
    Logger adapter that prepends context (symbol, trade_id, etc.)
    to every log message and injects extra fields for JSON logging.

    Usage:
        ctx = get_logger(__name__).with_context(symbol="BTC/USD")
        ctx.info("Price updated")
        # Output: [BTC/USD] Price updated
    """

    def process(
        self, msg: str, kwargs: Dict[str, Any]
    ) -> tuple:
        # Build prefix from context
        ctx_parts = []
        for key in ("symbol", "trade_id", "strategy"):
            if key in self.extra:
                ctx_parts.append(str(self.extra[key]))

        prefix = " | ".join(ctx_parts)
        if prefix:
            msg = f"[{prefix}] {msg}"

        # Inject context fields into log record for JSON formatter
        if "extra" not in kwargs:
            kwargs["extra"] = {}
        kwargs["extra"].update(self.extra)

        return msg, kwargs


# ═════════════════════════════════════════════════════════════════
#  ENHANCED LOGGER (wraps standard logger)
# ═════════════════════════════════════════════════════════════════

class EnhancedLogger:
    """
    Wrapper around logging.Logger that adds:
    - .with_context() for contextual logging
    - .trade() for structured trade logging
    - .perf() for performance timing
    """

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    # ── Proxy standard methods ──────────────────────────────────

    def debug(self, msg: str, *args, **kwargs) -> None:
        self._logger.debug(msg, *args, **kwargs)

    def info(self, msg: str, *args, **kwargs) -> None:
        self._logger.info(msg, *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs) -> None:
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg: str, *args, **kwargs) -> None:
        self._logger.error(msg, *args, **kwargs)

    def critical(self, msg: str, *args, **kwargs) -> None:
        self._logger.critical(msg, *args, **kwargs)

    def exception(self, msg: str, *args, **kwargs) -> None:
        self._logger.exception(msg, *args, **kwargs)

    def log(self, level: int, msg: str, *args, **kwargs) -> None:
        self._logger.log(level, msg, *args, **kwargs)

    @property
    def name(self) -> str:
        return self._logger.name

    @property
    def level(self) -> int:
        return self._logger.level

    @property
    def handlers(self) -> list:
        return self._logger.handlers

    # ── Context logging ─────────────────────────────────────────

    def with_context(self, **context) -> ContextLogger:
        """
        Create a context-aware logger.

        Args:
            **context: Key-value pairs (symbol, trade_id, strategy, etc.)

        Returns:
            ContextLogger with context injected into every message

        Example:
            ctx = logger.with_context(symbol="ETH/USD", trade_id="T456")
            ctx.info("Order submitted")
            # → [ETH/USD | T456] Order submitted
        """
        return ContextLogger(self._logger, extra=context)

    # ── Structured trade logging ────────────────────────────────

    def trade(
        self,
        action: str,
        symbol: str,
        side: str = "",
        qty: float = 0.0,
        price: float = 0.0,
        pnl: Optional[float] = None,
        **extra,
    ) -> None:
        """
        Log a trade event with structured data.

        Args:
            action: OPEN, CLOSE, FILL, REJECT, etc.
            symbol: Trading symbol
            side: BUY or SELL
            qty: Quantity
            price: Execution price
            pnl: Profit/loss (for close events)
            **extra: Additional fields

        Example:
            logger.trade("OPEN", "BTC/USD", "BUY", qty=0.5, price=67000)
        """
        parts = [f"TRADE {action}"]
        parts.append(f"symbol={symbol}")
        if side:
            parts.append(f"side={side}")
        if qty:
            parts.append(f"qty={qty}")
        if price:
            parts.append(f"price={price:.2f}")
        if pnl is not None:
            pnl_str = f"+{pnl:.2f}" if pnl >= 0 else f"{pnl:.2f}"
            parts.append(f"pnl={pnl_str}")
        for k, v in extra.items():
            parts.append(f"{k}={v}")

        msg = " | ".join(parts)

        # Log to main logger
        self._logger.info(
            msg,
            extra={
                "action": action,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "pnl": pnl,
                **extra,
            },
        )

        # Also log to dedicated trade logger
        _trade_logger.info(msg)

    # ── Performance logging ─────────────────────────────────────

    @contextmanager
    def perf(self, operation: str):
        """
        Context manager that logs execution duration.

        Usage:
            with logger.perf("fetch_bars"):
                bars = await fetch_bars()
            # → [PERF] fetch_bars completed in 0.234s
        """
        start = time.perf_counter()
        try:
            yield
        except Exception:
            elapsed = time.perf_counter() - start
            self._logger.error(
                f"[PERF] {operation} FAILED after {elapsed:.3f}s"
            )
            raise
        else:
            elapsed = time.perf_counter() - start
            level = logging.WARNING if elapsed > 5.0 else logging.DEBUG
            self._logger.log(
                level,
                f"[PERF] {operation} completed in {elapsed:.3f}s",
            )


# ═════════════════════════════════════════════════════════════════
#  HANDLER FACTORY
# ═════════════════════════════════════════════════════════════════

_loggers: Dict[str, EnhancedLogger] = {}

_file_handler: Optional[logging.Handler] = None
_console_handler: Optional[logging.Handler] = None
_json_handler: Optional[logging.Handler] = None
_critical_handler: Optional[logging.Handler] = None

# Dedicated trade logger (separate file)
_trade_logger = logging.getLogger("trades")
_trade_logger.setLevel(logging.DEBUG)
_trade_logger.propagate = False

_trade_file_handler: Optional[logging.Handler] = None


def _create_file_handler() -> logging.Handler:
    """Create rotating plain-text file handler."""
    handler = RotatingFileHandler(
        filename=LOG_DIR / LOG_FILE,
        maxBytes=MAX_FILE_SIZE,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(DEFAULT_FILE_LEVEL)
    handler.setFormatter(FileFormatter())
    handler.addFilter(_sensitive_filter)
    return handler


def _create_console_handler() -> logging.Handler:
    """Create colored console handler."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(DEFAULT_CONSOLE_LEVEL)
    handler.setFormatter(
        ColoredFormatter(
            fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
            use_colors=True,
            use_icons=True,
        )
    )
    handler.addFilter(_sensitive_filter)
    handler.addFilter(_rate_filter)
    return handler


def _create_json_handler() -> logging.Handler:
    """Create rotating JSON file handler."""
    handler = RotatingFileHandler(
        filename=LOG_DIR / JSON_LOG_FILE,
        maxBytes=MAX_FILE_SIZE,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(DEFAULT_FILE_LEVEL)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(_sensitive_filter)
    return handler


def _create_trade_handler() -> logging.Handler:
    """Create dedicated trade log file handler."""
    handler = RotatingFileHandler(
        filename=LOG_DIR / TRADE_LOG_FILE,
        maxBytes=MAX_FILE_SIZE,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(TradeFormatter())
    return handler


def _setup_trade_logger() -> None:
    """Initialize the dedicated trade logger."""
    global _trade_file_handler
    if _trade_file_handler is None:
        _trade_file_handler = _create_trade_handler()
        _trade_logger.addHandler(_trade_file_handler)


# ═════════════════════════════════════════════════════════════════
#  PUBLIC API — get_logger()
# ═════════════════════════════════════════════════════════════════

def get_logger(name: str) -> EnhancedLogger:
    """
    Get a configured EnhancedLogger instance.

    Loggers are cached, handlers are shared to prevent
    duplicate entries.

    Args:
        name: Logger name (typically __name__)

    Returns:
        EnhancedLogger instance with context, trade, and perf support

    Example:
        from app.utils.logger import get_logger

        logger = get_logger(__name__)
        logger.info("Hello!")

        # With context
        ctx = logger.with_context(symbol="BTC/USD")
        ctx.info("Price updated to 67000")

        # Trade logging
        logger.trade("OPEN", "BTC/USD", "BUY", qty=0.5, price=67000)

        # Performance timing
        with logger.perf("data_fetch"):
            data = fetch_data()
    """
    global _file_handler, _console_handler, _json_handler, _critical_handler

    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)

    # Prevent duplicate handlers on re-import
    if logger.handlers:
        enhanced = EnhancedLogger(logger)
        _loggers[name] = enhanced
        return enhanced

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # ── Create shared handlers once ──
    if _file_handler is None:
        _file_handler = _create_file_handler()

    if _console_handler is None:
        _console_handler = _create_console_handler()

    if _json_handler is None and JSON_LOGGING_ENABLED:
        _json_handler = _create_json_handler()

    if _critical_handler is None:
        _critical_handler = _CriticalBufferHandler()

    # ── Attach handlers ──
    logger.addHandler(_file_handler)
    logger.addHandler(_console_handler)
    logger.addHandler(_ring_buffer)
    logger.addHandler(_critical_handler)

    if _json_handler:
        logger.addHandler(_json_handler)

    # ── Ensure trade logger is set up ──
    _setup_trade_logger()

    enhanced = EnhancedLogger(logger)
    _loggers[name] = enhanced
    return enhanced


# ═════════════════════════════════════════════════════════════════
#  CONVENIENCE FUNCTIONS
# ═════════════════════════════════════════════════════════════════

def log_trade(
    side: str,
    symbol: str,
    qty: float = 0.0,
    price: float = 0.0,
    pnl: Optional[float] = None,
    **extra,
) -> None:
    """
    Module-level convenience for trade logging.

    Usage:
        from app.utils.logger import log_trade
        log_trade("BUY", "BTC/USD", qty=0.5, price=67000.0)
    """
    logger = get_logger("trade_events")
    action = "OPEN" if side.upper() in ("BUY", "SELL") else "CLOSE"
    logger.trade(action, symbol, side, qty, price, pnl, **extra)


def log_timing(operation: str) -> Callable:
    """
    Decorator that logs function execution time.
    Works with both sync and async functions.

    Usage:
        @log_timing("fetch_bars")
        async def fetch_bars():
            ...

        @log_timing("compute_signal")
        def compute_signal():
            ...
    """
    def decorator(func: Callable) -> Callable:
        logger = get_logger(func.__module__ or "timing")

        if asyncio.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                start = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                    elapsed = time.perf_counter() - start
                    lvl = logging.WARNING if elapsed > 5.0 else logging.DEBUG
                    logger.log(
                        lvl,
                        f"[PERF] {operation} completed in {elapsed:.3f}s",
                    )
                    return result
                except Exception:
                    elapsed = time.perf_counter() - start
                    logger.error(
                        f"[PERF] {operation} FAILED after {elapsed:.3f}s",
                    )
                    raise
            return async_wrapper
        else:
            @functools.wraps(func)
            def sync_wrapper(*args, **kwargs):
                start = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    elapsed = time.perf_counter() - start
                    lvl = logging.WARNING if elapsed > 5.0 else logging.DEBUG
                    logger.log(
                        lvl,
                        f"[PERF] {operation} completed in {elapsed:.3f}s",
                    )
                    return result
                except Exception:
                    elapsed = time.perf_counter() - start
                    logger.error(
                        f"[PERF] {operation} FAILED after {elapsed:.3f}s",
                    )
                    raise
            return sync_wrapper

    return decorator


# ═════════════════════════════════════════════════════════════════
#  RUNTIME CONFIGURATION
# ═════════════════════════════════════════════════════════════════

def set_console_level(level: int) -> None:
    """Change console log level at runtime."""
    global _console_handler
    if _console_handler:
        _console_handler.setLevel(level)


def set_file_level(level: int) -> None:
    """Change file log level at runtime."""
    global _file_handler
    if _file_handler:
        _file_handler.setLevel(level)


def enable_debug_mode() -> None:
    """Enable debug logging to console."""
    set_console_level(logging.DEBUG)


def disable_debug_mode() -> None:
    """Restore console to INFO level."""
    set_console_level(logging.INFO)


def disable_colors() -> None:
    """Disable colored output (useful for CI/CD or piping)."""
    global _console_handler
    if _console_handler:
        _console_handler.setFormatter(
            ColoredFormatter(
                fmt="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
                datefmt="%H:%M:%S",
                use_colors=False,
                use_icons=False,
            )
        )


# ═════════════════════════════════════════════════════════════════
#  DIAGNOSTIC / QUERY APIs
# ═════════════════════════════════════════════════════════════════

def get_log_file_path() -> Path:
    """Get the current log file path."""
    return LOG_DIR / LOG_FILE


def get_recent_logs(
    level: Optional[str] = None,
    last_n: int = 50,
) -> List[Dict[str, Any]]:
    """
    Retrieve recent log records from the in-memory ring buffer.

    Args:
        level: Filter by level (e.g. "ERROR", "WARNING")
        last_n: Number of recent records

    Returns:
        List of log record dicts

    Example:
        errors = get_recent_logs(level="ERROR", last_n=10)
    """
    return _ring_buffer.get_records(level=level, last_n=last_n)


def get_pending_alerts() -> List[Dict[str, Any]]:
    """
    Drain buffered ERROR/CRITICAL logs for alert dispatch.
    Returns and clears the buffer.

    Designed for the Telegram notification loop:
        alerts = get_pending_alerts()
        for alert in alerts:
            await tg_bot.send(format_alert(alert))
    """
    return _critical_buffer.drain()


def get_alert_count() -> int:
    """Number of pending alert messages."""
    return _critical_buffer.count


def rotate_logs_now() -> None:
    """Force log rotation immediately."""
    global _file_handler
    if _file_handler and hasattr(_file_handler, "doRollover"):
        _file_handler.doRollover()


def get_log_stats() -> Dict[str, Any]:
    """
    Get statistics about current log files.

    Returns:
        Dict with log file sizes, backup counts, etc.
    """
    stats = {
        "log_dir": str(LOG_DIR.absolute()),
        "files": {},
        "total_size_mb": 0.0,
        "memory_buffer_size": len(_ring_buffer._buffer),
        "pending_alerts": _critical_buffer.count,
        "colors_enabled": COLORS_ENABLED,
        "json_logging": JSON_LOGGING_ENABLED,
        "console_level": logging.getLevelName(
            _console_handler.level if _console_handler else logging.INFO
        ),
        "file_level": logging.getLevelName(
            _file_handler.level if _file_handler else logging.DEBUG
        ),
    }

    total = 0.0
    for fname in [LOG_FILE, JSON_LOG_FILE, TRADE_LOG_FILE]:
        fpath = LOG_DIR / fname
        if fpath.exists():
            size_mb = round(fpath.stat().st_size / 1024 / 1024, 3)
            stats["files"][fname] = {
                "size_mb": size_mb,
                "backups": sum(
                    1 for i in range(1, BACKUP_COUNT + 1)
                    if (LOG_DIR / f"{fname}.{i}").exists()
                ),
            }
            total += size_mb

            # Add backup sizes
            for i in range(1, BACKUP_COUNT + 1):
                bp = LOG_DIR / f"{fname}.{i}"
                if bp.exists():
                    total += bp.stat().st_size / 1024 / 1024

    stats["total_size_mb"] = round(total, 3)
    return stats


def clear_memory_buffer() -> None:
    """Clear the in-memory log ring buffer."""
    _ring_buffer.clear()


# ═════════════════════════════════════════════════════════════════
#  MODULE SELF-TEST
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Logger Module v2 — Diagnostics & Demo")
    print("=" * 60)

    # Test color support
    print(
        f"\n  Color Support: "
        f"{'✅ Enabled' if COLORS_ENABLED else '❌ Disabled'}"
    )
    print(f"  JSON Logging:  "
          f"{'✅ Enabled' if JSON_LOGGING_ENABLED else '❌ Disabled'}")

    # Log stats
    stats = get_log_stats()
    print(f"\n  Log Directory: {stats['log_dir']}")
    print(f"  Total Size:    {stats['total_size_mb']} MB")
    print(f"  Console Level: {stats['console_level']}")
    print(f"  File Level:    {stats['file_level']}")
    for fname, finfo in stats.get("files", {}).items():
        print(f"    {fname}: {finfo['size_mb']} MB ({finfo['backups']} backups)")

    # Demo logging
    print(f"\n  Demo Log Messages:")
    print("  " + "-" * 40)

    logger = get_logger("demo")
    original_level = (
        _console_handler.level if _console_handler else logging.INFO
    )
    set_console_level(logging.DEBUG)

    logger.debug("This is a DEBUG message")
    logger.info("This is an INFO message")
    logger.warning("This is a WARNING message")
    logger.error("This is an ERROR message")
    logger.critical("This is a CRITICAL message")

    # Context logging demo
    print("\n  Context Logging:")
    print("  " + "-" * 40)
    ctx = logger.with_context(symbol="BTC/USD", trade_id="T001")
    ctx.info("Order submitted at $67,000")

    # Trade logging demo
    print("\n  Trade Logging:")
    print("  " + "-" * 40)
    logger.trade("OPEN", "BTC/USD", "BUY", qty=0.5, price=67000.0)
    logger.trade(
        "CLOSE", "BTC/USD", "SELL", qty=0.5, price=67500.0, pnl=250.0
    )

    # Sensitive data masking demo
    print("\n  Sensitive Data Masking:")
    print("  " + "-" * 40)
    logger.info("Connecting with api_key=PKabcdef1234567890xyz")
    logger.info("Token: 123456789:ABCdefGHI_jklMNOpqrSTUvwxYZ12345")

    # Performance timing demo
    print("\n  Performance Timing:")
    print("  " + "-" * 40)
    with logger.perf("demo_operation"):
        time.sleep(0.1)

    # Ring buffer demo
    print("\n  Memory Buffer:")
    print("  " + "-" * 40)
    recent = get_recent_logs(last_n=3)
    for r in recent:
        print(f"    [{r['level']}] {r['message'][:60]}")

    # Alert buffer demo
    alerts = get_pending_alerts()
    print(f"\n  Pending alerts drained: {len(alerts)}")

    set_console_level(original_level)

    print("\n" + "=" * 60)
    print("  ✅ Logger v2 fully operational")
    print("=" * 60)