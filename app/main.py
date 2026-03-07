# app/main.py

"""
Autonomous AI Trading Bot — Production Grade Entry Point v2.1

This bot autonomously:
1. Monitors multiple cryptocurrency/stock markets continuously
2. Analyzes price movements, trends, and indicators using 4-Brain system
3. Decides when to BUY or SELL based on 60%+ probability threshold
4. Executes trades via Alpaca (stocks/crypto) or Binance (crypto) API
5. Manages risk with multi-layer protection (KillSwitch, LossGuard, Adaptive)
6. Enforces ₹1500 daily loss limit with automatic halt
7. Sends hourly market analysis notifications
8. Sends real-time trade entry/exit notifications via Telegram
9. Persists state atomically for crash recovery

NEW Features (v2.1):
- 60% minimum probability threshold for trade entry
- ₹1500 daily loss limit with automatic trading halt
- Automatic resume next day (UTC)
- Hourly market analysis notifications (even when not trading)
- Trade entry notifications (symbol, price, target, stop loss, probability)
- Trade exit notifications (exit price, P/L, duration)
- Combined hourly analysis (single message, no spam)

Usage:
    python -m app.main
    python app/main.py
    
    # With options
    python -m app.main --mode paper --interval 60 --debug
    python -m app.main --daily-limit 2000  # Custom daily loss limit

Environment:
    Configure via config/.env file
    See config/env.sample for all options
"""

import argparse
import asyncio
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ═════════════════════════════════════════════════════════════════
#  LOAD ENVIRONMENT FIRST (before any app imports)
# ═════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BASE_DIR / "config" / ".env"

# Try multiple locations for .env
_env_loaded = False
_env_path_used = None

for env_path in [ENV_FILE, BASE_DIR / ".env", Path.cwd() / ".env"]:
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
            _env_loaded = True
            _env_path_used = env_path
            break
        except ImportError:
            # dotenv not installed, try manual loading
            try:
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, _, value = line.partition("=")
                            os.environ.setdefault(key.strip(), value.strip())
                _env_loaded = True
                _env_path_used = env_path
                break
            except Exception:
                pass

# ═════════════════════════════════════════════════════════════════
#  APP IMPORTS (after env loaded)
# ═════════════════════════════════════════════════════════════════

from app.utils.logger import (
    get_logger,
    enable_debug_mode,
    get_log_stats,
    set_console_level,
)
from app.utils.time import (
    format_duration,
    get_uptime_str,
    get_utc_now,
    market_status,
    Stopwatch,
)
from app.state.manager import StateManager
from app.exchange import create_exchange, get_exchange_info
from app.market.analyzer import MarketAnalyzer
from app.strategies.scalping import ScalpingStrategy
from app.orchestrator.controller import BotController
from app.orchestrator.scheduler import TradeScheduler, SchedulerState

logger = get_logger(__name__)


# ═════════════════════════════════════════════════════════════════
#  VERSION & CONSTANTS
# ═════════════════════════════════════════════════════════════════

__version__ = "2.1.0"

LOCK_FILE = BASE_DIR / ".bot_lock"
STATE_FILE = BASE_DIR / "app" / "state" / "state.json"
DEFAULTS_FILE = BASE_DIR / "app" / "state" / "defaults.json"

# Minimum versions for dependencies
MIN_PYTHON_VERSION = (3, 9)

# Default values for new features
DEFAULT_MIN_PROBABILITY = 60.0          # 60% minimum for trade entry
DEFAULT_DAILY_LOSS_LIMIT_INR = 1500.0   # ₹1500 daily loss limit
DEFAULT_USD_TO_INR_RATE = 83.0          # Conversion rate
DEFAULT_HOURLY_ANALYSIS_INTERVAL = 3600  # 1 hour in seconds


# ═════════════════════════════════════════════════════════════════
#  CONFIGURATION HELPERS
# ═════════════════════════════════════════════════════════════════

def _env_str(key: str, default: str = "") -> str:
    """Get string from environment."""
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    """Get integer from environment."""
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    """Get float from environment."""
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    """Get boolean from environment."""
    val = os.getenv(key, "").lower().strip()
    if val in ("true", "1", "yes", "on"):
        return True
    if val in ("false", "0", "no", "off"):
        return False
    return default


def _env_list(key: str, default: str = "") -> List[str]:
    """Get comma-separated list from environment with symbol normalization."""
    raw = os.getenv(key, default)
    items = [s.strip() for s in raw.split(",") if s.strip()]

    # Normalize symbol format (ensure BASE/QUOTE format)
    normalized = []
    for item in items:
        item = item.upper()
        if "/" not in item:
            # Try to detect common patterns
            for quote in ["USDT", "USD", "USDC", "BUSD", "BTC", "ETH"]:
                if item.endswith(quote):
                    base = item[: -len(quote)]
                    item = f"{base}/{quote}"
                    break
        normalized.append(item)

    return normalized


# ═════════════════════════════════════════════════════════════════
#  CONFIGURATION LOADING & VALIDATION
# ═════════════════════════════════════════════════════════════════

def _load_config(cli_args: Optional[argparse.Namespace] = None) -> Dict[str, Any]:
    """
    Load configuration from environment with CLI overrides.

    Priority: CLI args > Environment > Defaults
    """
    config = {
        # ── Trading Mode & Exchange ───────────────────────────────
        "mode": _env_str("TRADING_MODE", "PAPER").upper(),
        "exchange": _env_str("EXCHANGE", "AUTO").upper(),

        # ── Trading Parameters ────────────────────────────────────
        "coins": _env_list("TRADING_COINS", "BTC/USDT,ETH/USDT"),
        "interval": _env_int("ANALYSIS_INTERVAL", 300),

        # ── Risk Management ───────────────────────────────────────
        "base_risk": _env_float("BASE_RISK", 0.02),  # 2% per trade
        "min_risk": _env_float("MIN_RISK", 0.005),
        "max_risk": _env_float("MAX_RISK", 0.03),
        "max_daily_drawdown": _env_float("MAX_DAILY_DRAWDOWN", 0.05),
        "emergency_drawdown": _env_float("EMERGENCY_DRAWDOWN", 0.15),
        "max_exposure_pct": _env_float("MAX_EXPOSURE_PCT", 0.30),
        "max_consecutive_losses": _env_int("MAX_CONSECUTIVE_LOSSES", 3),

        # ── NEW: Probability Threshold ────────────────────────────
        "min_trade_probability": _env_float(
            "MIN_TRADE_PROBABILITY", DEFAULT_MIN_PROBABILITY
        ),

        # ── NEW: Daily Loss Limit (INR) ───────────────────────────
        "daily_loss_limit_inr": _env_float(
            "DAILY_LOSS_LIMIT_INR", DEFAULT_DAILY_LOSS_LIMIT_INR
        ),
        "daily_loss_limit_enabled": _env_bool("DAILY_LOSS_LIMIT_ENABLED", True),
        "usd_to_inr_rate": _env_float("USD_TO_INR_RATE", DEFAULT_USD_TO_INR_RATE),

        # ── NEW: Hourly Analysis ──────────────────────────────────
        "hourly_analysis_enabled": _env_bool("HOURLY_ANALYSIS_ENABLED", True),
        "hourly_analysis_interval": _env_int(
            "HOURLY_ANALYSIS_INTERVAL", DEFAULT_HOURLY_ANALYSIS_INTERVAL
        ),

        # ── Fees & Slippage ───────────────────────────────────────
        "fee_pct": _env_float("FEE_PCT", 0.001),
        "slippage_pct": _env_float("SLIPPAGE_PCT", 0.0005),

        # ── Strategy Parameters ───────────────────────────────────
        "risk_reward_ratio": _env_float("RISK_REWARD_RATIO", 1.5),
        "atr_multiplier": _env_float("ATR_MULTIPLIER", 1.2),
        "min_confidence": _env_float("MIN_CONFIDENCE", 0.60),  # 60% default
        "min_volatility_pct": _env_float("MIN_VOLATILITY_PCT", 0.002),
        "max_volatility_pct": _env_float("MAX_VOLATILITY_PCT", 0.02),

        # ── Stop Loss & Take Profit ───────────────────────────────
        "default_stop_loss_pct": _env_float("DEFAULT_STOP_LOSS_PCT", 0.02),
        "default_take_profit_pct": _env_float("DEFAULT_TAKE_PROFIT_PCT", 0.03),
        "trailing_stop_enabled": _env_bool("TRAILING_STOP_ENABLED", True),
        "trailing_stop_pct": _env_float("TRAILING_STOP_PCT", 0.015),

        # ── Trade Limits ──────────────────────────────────────────
        "max_trades_per_day": _env_int("MAX_TRADES_PER_DAY", 10),
        "max_trades_per_hour": _env_int("MAX_TRADES_PER_HOUR", 3),
        "min_trade_interval": _env_int("MIN_TRADE_INTERVAL", 60),
        "max_open_positions": _env_int("MAX_OPEN_POSITIONS", 3),

        # ── Scheduler ─────────────────────────────────────────────
        "max_scheduler_errors": _env_int("MAX_SCHED_ERRORS", 5),
        "market_aware": _env_bool("MARKET_AWARE", False),
        "jitter_seconds": _env_float("JITTER_SECONDS", 5.0),  # ±5s jitter
        "adaptive_interval": _env_bool("ADAPTIVE_INTERVAL", False),

        # ── Initial Balance (Paper Trading) ───────────────────────
        "initial_balance": _env_float("INITIAL_BALANCE", 100.0),

        # ── Debug ─────────────────────────────────────────────────
        "debug_mode": _env_bool("DEBUG_MODE", False),

        # ── Telegram ──────────────────────────────────────────────
        "telegram_enabled": _env_bool("TELEGRAM_ENABLED", True),
        "telegram_retry_count": _env_int("TELEGRAM_RETRY_COUNT", 3),
        
        # ── Notification Settings ─────────────────────────────────
        "trade_notifications": _env_bool("TRADE_NOTIFICATIONS", True),
        "analysis_notifications": _env_bool("ANALYSIS_NOTIFICATIONS", True),
        "error_notifications": _env_bool("ERROR_NOTIFICATIONS", True),
        "daily_summary_enabled": _env_bool("DAILY_SUMMARY_ENABLED", True),
    }

    # Apply CLI overrides
    if cli_args:
        if cli_args.mode:
            config["mode"] = cli_args.mode.upper()
        if cli_args.interval:
            config["interval"] = cli_args.interval
        if cli_args.coins:
            config["coins"] = _env_list("", cli_args.coins)
        if cli_args.debug:
            config["debug_mode"] = True
        if cli_args.no_telegram:
            config["telegram_enabled"] = False
        if hasattr(cli_args, "daily_limit") and cli_args.daily_limit:
            config["daily_loss_limit_inr"] = cli_args.daily_limit
        if hasattr(cli_args, "min_probability") and cli_args.min_probability:
            config["min_trade_probability"] = cli_args.min_probability
        if hasattr(cli_args, "no_hourly") and cli_args.no_hourly:
            config["hourly_analysis_enabled"] = False

    return config


def _validate_config(config: Dict[str, Any]) -> tuple:
    """
    Validate configuration values.

    Returns:
        Tuple of (is_valid, errors, warnings)
    """
    errors: List[str] = []
    warnings: List[str] = []

    # ── Required checks ───────────────────────────────────────────
    if not config["coins"]:
        errors.append("TRADING_COINS is empty — at least one coin required")

    # ── Range checks ──────────────────────────────────────────────
    if not (0.001 <= config["base_risk"] <= 0.1):
        errors.append(
            f"BASE_RISK={config['base_risk']} out of range (0.001-0.1)"
        )

    if not (0.01 <= config["max_daily_drawdown"] <= 0.5):
        errors.append(
            f"MAX_DAILY_DRAWDOWN={config['max_daily_drawdown']} "
            f"out of range (0.01-0.5)"
        )

    if config["interval"] < 10:
        errors.append(
            f"ANALYSIS_INTERVAL={config['interval']} too short (min 10s)"
        )

    # ── NEW: Probability threshold validation ─────────────────────
    if not (50.0 <= config["min_trade_probability"] <= 95.0):
        warnings.append(
            f"MIN_TRADE_PROBABILITY={config['min_trade_probability']}% "
            f"outside recommended range (50-95%). Using anyway."
        )

    # ── NEW: Daily loss limit validation ──────────────────────────
    if config["daily_loss_limit_enabled"]:
        if config["daily_loss_limit_inr"] <= 0:
            warnings.append(
                "DAILY_LOSS_LIMIT_INR <= 0, disabling daily loss limit"
            )
            config["daily_loss_limit_enabled"] = False
        elif config["daily_loss_limit_inr"] < 100:
            warnings.append(
                f"DAILY_LOSS_LIMIT_INR=₹{config['daily_loss_limit_inr']} "
                f"is very low, consider increasing"
            )

    # ── Risk/Reward validation ────────────────────────────────────
    if config["risk_reward_ratio"] < 1.0:
        warnings.append(
            f"RISK_REWARD_RATIO={config['risk_reward_ratio']} < 1.0 "
            "(consider using at least 1.5)"
        )

    if config["emergency_drawdown"] <= config["max_daily_drawdown"]:
        warnings.append(
            f"EMERGENCY_DRAWDOWN ({config['emergency_drawdown']}) "
            f"should be greater than MAX_DAILY_DRAWDOWN "
            f"({config['max_daily_drawdown']})"
        )

    # ── Telegram check ────────────────────────────────────────────
    if config["telegram_enabled"]:
        telegram_token = _env_str("TELEGRAM_BOT_TOKEN")
        telegram_chat = _env_str("TELEGRAM_CHAT_ID")

        if not telegram_token or not telegram_chat:
            warnings.append(
                "Telegram enabled but not configured — "
                "bot will run without notifications"
            )
            config["telegram_enabled"] = False

    # ── Exchange check ────────────────────────────────────────────
    exchange_info = get_exchange_info()

    if config["mode"] == "LIVE":
        if (
            not exchange_info["alpaca_keys_configured"]
            and not exchange_info["binance_keys_configured"]
        ):
            errors.append(
                "LIVE mode requires API keys. "
                "Set ALPACA_API_KEY or BINANCE_API_KEY"
            )

    return len(errors) == 0, errors, warnings


# ═════════════════════════════════════════════════════════════════
#  INSTANCE LOCK
# ═════════════════════════════════════════════════════════════════

def _check_lock() -> Optional[Dict[str, str]]:
    """
    Check if another instance is running.

    Returns:
        Lock info dict if locked, None if clear
    """
    if not LOCK_FILE.exists():
        return None

    try:
        content = LOCK_FILE.read_text()
        info = {}
        for line in content.strip().split("\n"):
            if "=" in line:
                k, _, v = line.partition("=")
                info[k.strip()] = v.strip()
        return info
    except Exception:
        return {"error": "Could not read lock file"}


def _acquire_lock() -> bool:
    """
    Acquire instance lock.

    Returns:
        True if lock acquired, False if failed
    """
    try:
        import socket
        hostname = socket.gethostname()
    except Exception:
        hostname = "unknown"

    try:
        LOCK_FILE.write_text(
            f"PID={os.getpid()}\n"
            f"Started={datetime.now(timezone.utc).isoformat()}\n"
            f"Host={hostname}\n"
            f"Version={__version__}\n"
        )
        return True
    except Exception as e:
        logger.error(f"Failed to create lock file: {e}")
        return False


def _release_lock() -> None:
    """Release instance lock."""
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
            logger.debug("Lock file removed")
    except Exception as e:
        logger.warning(f"Failed to remove lock file: {e}")


# ═════════════════════════════════════════════════════════════════
#  STARTUP CHECKS
# ═════════════════════════════════════════════════════════════════

def _check_python_version() -> bool:
    """Check Python version meets minimum requirement."""
    current = sys.version_info[:2]
    if current < MIN_PYTHON_VERSION:
        print(
            f"❌ Python {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}+ "
            f"required, found {current[0]}.{current[1]}"
        )
        return False
    return True


def _check_dependencies() -> Dict[str, bool]:
    """Check required dependencies are installed."""
    deps = {
        "pandas": False,
        "numpy": False,
        "requests": False,
    }

    optional_deps = {
        "telegram": False,
        "alpaca_trade_api": False,
        "python-binance": False,
    }

    for pkg in deps:
        try:
            __import__(pkg)
            deps[pkg] = True
        except ImportError:
            deps[pkg] = False

    for pkg in optional_deps:
        try:
            # Handle package name variations
            import_name = pkg.replace("-", "_")
            if pkg == "python-binance":
                import_name = "binance"
            __import__(import_name)
            optional_deps[pkg] = True
        except ImportError:
            optional_deps[pkg] = False

    return {**deps, **optional_deps}


# ═════════════════════════════════════════════════════════════════
#  TELEGRAM INTEGRATION
# ═════════════════════════════════════════════════════════════════

async def _start_telegram(
    controller: BotController,
    max_retries: int = 3,
) -> bool:
    """
    Start Telegram bot with retry logic.

    Returns True if started successfully, False otherwise.
    Trading continues even if Telegram fails.
    """
    for attempt in range(max_retries):
        try:
            from app.tg.bot import start_telegram_bot

            await start_telegram_bot(
                controller=controller,
                drop_pending_updates=True,
            )
            logger.info("✅ Telegram bot started")
            return True

        except ImportError:
            logger.warning(
                "python-telegram-bot not installed — "
                "Telegram features disabled"
            )
            return False

        except Exception as e:
            error_msg = str(e)

            # Check for conflict error
            if "Conflict" in error_msg:
                logger.error(
                    "❌ Telegram Conflict Error!\n"
                    "   Another bot instance is using this token.\n"
                    "   Solution: pkill -f 'python.*app.main'"
                )
                return False

            logger.warning(
                f"Telegram start attempt {attempt + 1}/{max_retries} "
                f"failed: {e}"
            )

            if attempt < max_retries - 1:
                await asyncio.sleep(5 * (attempt + 1))

    logger.error(
        "❌ Telegram failed after all retries — "
        "continuing without notifications"
    )
    return False


# ═════════════════════════════════════════════════════════════════
#  COMPONENT FACTORIES
# ═════════════════════════════════════════════════════════════════

def _create_analyzers(
    coins: List[str],
    config: Dict[str, Any],
) -> Dict[str, MarketAnalyzer]:
    """Create market analyzer for each coin with configuration."""
    analyzers = {}
    for coin in coins:
        try:
            analyzers[coin] = MarketAnalyzer(
                symbol=coin,
                min_probability=config["min_trade_probability"],
            )
            logger.debug(f"Created analyzer for {coin}")
        except Exception as e:
            logger.error(f"Failed to create analyzer for {coin}: {e}")
    return analyzers


def _create_strategy(
    symbol: str,
    config: Dict[str, Any],
) -> ScalpingStrategy:
    """Create trading strategy with configuration."""
    return ScalpingStrategy(
        symbol=symbol,
        risk_reward_ratio=config["risk_reward_ratio"],
        atr_multiplier=config["atr_multiplier"],
        min_confidence=config["min_confidence"],
        min_volatility_pct=config["min_volatility_pct"],
        max_volatility_pct=config["max_volatility_pct"],
        min_probability=config["min_trade_probability"],
        stop_loss_pct=config["default_stop_loss_pct"],
        take_profit_pct=config["default_take_profit_pct"],
        trailing_stop_pct=config["trailing_stop_pct"],
    )


def _initialize_state_with_config(
    state: StateManager,
    config: Dict[str, Any],
) -> None:
    """Initialize state manager with configuration values."""
    # Set currency conversion
    state.set("pnl_currency", "INR")
    state.set("usd_to_inr_rate", config["usd_to_inr_rate"])
    
    # Set probability threshold
    state.set("min_trade_probability", config["min_trade_probability"])
    
    # Set daily loss limit
    state.set("daily_loss_limit_inr", config["daily_loss_limit_inr"])
    state.set("daily_loss_limit_enabled", config["daily_loss_limit_enabled"])
    
    # Set hourly analysis settings
    state.set("hourly_analysis_enabled", config["hourly_analysis_enabled"])
    state.set("hourly_analysis_interval", config["hourly_analysis_interval"])
    
    # Set notification preferences
    state.set("trade_notifications", config["trade_notifications"])
    state.set("analysis_notifications", config["analysis_notifications"])
    state.set("error_notifications", config["error_notifications"])
    state.set("daily_summary_enabled", config["daily_summary_enabled"])
    
    # Set trading limits
    state.set("max_trades_per_day", config["max_trades_per_day"])
    state.set("max_open_positions", config["max_open_positions"])
    
    # Set risk parameters
    state.set("risk_per_trade_pct", config["base_risk"] * 100)
    state.set("default_stop_loss_pct", config["default_stop_loss_pct"] * 100)
    state.set("default_take_profit_pct", config["default_take_profit_pct"] * 100)
    
    # Set watched symbols
    state.set("watched_symbols", config["coins"])
    
    # Update metadata
    state.set("version", __version__)
    state.set("updated_at", datetime.now(timezone.utc).isoformat())
    
    logger.debug("State initialized with configuration")


# ═════════════════════════════════════════════════════════════════
#  CLI ARGUMENT PARSER
# ═════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Autonomous AI Trading Bot v2.1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m app.main                        # Run with .env config
  python -m app.main --mode paper           # Force paper trading
  python -m app.main --interval 60          # 1-minute cycles
  python -m app.main --debug                # Enable debug logging
  python -m app.main --no-telegram          # Disable Telegram
  python -m app.main --daily-limit 2000     # Set ₹2000 daily loss limit
  python -m app.main --min-probability 65   # Set 65% min probability
  python -m app.main --no-hourly            # Disable hourly analysis

New Features:
  • 60% minimum probability threshold for trades
  • ₹1500 daily loss limit (configurable)
  • Hourly market analysis notifications
  • Trade entry/exit notifications with full details
        """,
    )

    parser.add_argument(
        "--mode",
        choices=["paper", "live", "PAPER", "LIVE"],
        help="Trading mode (overrides TRADING_MODE env)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        help="Analysis interval in seconds (overrides ANALYSIS_INTERVAL)",
    )
    parser.add_argument(
        "--coins",
        help="Comma-separated coin list (overrides TRADING_COINS)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Disable Telegram notifications",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Trading Bot v{__version__}",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check configuration and exit",
    )
    
    # NEW: Daily loss limit argument
    parser.add_argument(
        "--daily-limit",
        type=float,
        metavar="INR",
        help=f"Daily loss limit in INR (default: ₹{DEFAULT_DAILY_LOSS_LIMIT_INR})",
    )
    
    # NEW: Minimum probability argument
    parser.add_argument(
        "--min-probability",
        type=float,
        metavar="PCT",
        help=f"Minimum trade probability %% (default: {DEFAULT_MIN_PROBABILITY}%%)",
    )
    
    # NEW: Disable hourly analysis
    parser.add_argument(
        "--no-hourly",
        action="store_true",
        help="Disable hourly market analysis notifications",
    )

    return parser.parse_args()


# ═════════════════════════════════════════════════════════════════
#  STARTUP BANNER
# ═════════════════════════════════════════════════════════════════

def _print_banner(config: Dict[str, Any]) -> None:
    """Print startup banner."""
    logger.info("")
    logger.info("═" * 60)
    logger.info("🤖 AUTONOMOUS AI TRADING BOT")
    logger.info(f"   Version: {__version__}")
    logger.info("═" * 60)
    logger.info(f"📁 Base: {BASE_DIR}")
    logger.info(f"🕐 Time: {get_utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if _env_path_used:
        logger.info(f"📄 Env:  {_env_path_used}")
    logger.info("")


def _print_config_summary(config: Dict[str, Any]) -> None:
    """Print configuration summary."""
    logger.info("📋 Configuration:")
    logger.info(f"   Mode:              {config['mode']}")
    logger.info(f"   Exchange:          {config['exchange']}")
    logger.info(f"   Coins:             {', '.join(config['coins'])}")
    logger.info(
        f"   Interval:          {config['interval']}s "
        f"({format_duration(config['interval'])})"
    )
    logger.info("")
    
    # NEW: Probability & Risk settings
    logger.info("🎯 Trading Rules:")
    logger.info(f"   Min Probability:   {config['min_trade_probability']}%")
    logger.info(f"   Risk per Trade:    {config['base_risk']*100:.1f}%")
    logger.info(f"   Risk/Reward:       {config['risk_reward_ratio']}:1")
    logger.info(f"   Stop Loss:         {config['default_stop_loss_pct']*100:.1f}%")
    logger.info(f"   Take Profit:       {config['default_take_profit_pct']*100:.1f}%")
    logger.info("")
    
    # NEW: Daily limits
    logger.info("💰 Daily Limits:")
    if config["daily_loss_limit_enabled"]:
        logger.info(f"   Loss Limit:        ₹{config['daily_loss_limit_inr']:,.0f}")
    else:
        logger.info("   Loss Limit:        Disabled")
    logger.info(f"   Max Trades:        {config['max_trades_per_day']}/day")
    logger.info(f"   Max Positions:     {config['max_open_positions']}")
    logger.info(f"   USD→INR Rate:      ₹{config['usd_to_inr_rate']}")
    logger.info("")
    
    # NEW: Notifications
    logger.info("📱 Notifications:")
    logger.info(f"   Telegram:          {'✅ Enabled' if config['telegram_enabled'] else '❌ Disabled'}")
    logger.info(f"   Hourly Analysis:   {'✅ Enabled' if config['hourly_analysis_enabled'] else '❌ Disabled'}")
    logger.info(f"   Trade Alerts:      {'✅ Enabled' if config['trade_notifications'] else '❌ Disabled'}")
    logger.info("")
    
    # Risk management
    logger.info("🛡️ Risk Management:")
    logger.info(f"   Daily Drawdown:    {config['max_daily_drawdown']*100:.1f}%")
    logger.info(f"   Emergency DD:      {config['emergency_drawdown']*100:.1f}%")
    logger.info(f"   Max Losses:        {config['max_consecutive_losses']} consecutive")
    logger.info(f"   Market Aware:      {config['market_aware']}")
    logger.info("")


# ═════════════════════════════════════════════════════════════════
#  MAIN ASYNC ENTRY POINT
# ═════════════════════════════════════════════════════════════════

async def main(cli_args: Optional[argparse.Namespace] = None) -> int:
    """
    Main async entry point for the trading bot.

    Returns:
        Exit code (0 = success, 1 = error)
    """
    startup_timer = Stopwatch()

    # ══════════════════════════════════════════════════════════════
    #  LOAD & VALIDATE CONFIGURATION
    # ══════════════════════════════════════════════════════════════

    config = _load_config(cli_args)

    # Enable debug mode early if requested
    if config["debug_mode"]:
        enable_debug_mode()
        logger.debug("Debug mode enabled")

    # Print banner
    _print_banner(config)

    # Validate configuration
    logger.info("🔍 Validating configuration...")

    is_valid, errors, warnings = _validate_config(config)

    for warning in warnings:
        logger.warning(f"   ⚠️ {warning}")

    for error in errors:
        logger.error(f"   ❌ {error}")

    if not is_valid:
        logger.critical("Configuration invalid — cannot start")
        return 1

    logger.info("   ✅ Configuration valid")
    logger.info("")

    # Print config summary
    _print_config_summary(config)

    # Check-only mode
    if cli_args and cli_args.check:
        logger.info("✅ Configuration check passed")
        return 0

    # ══════════════════════════════════════════════════════════════
    #  INSTANCE LOCK
    # ══════════════════════════════════════════════════════════════

    logger.info("🔒 Checking instance lock...")

    existing_lock = _check_lock()
    if existing_lock:
        logger.error(
            f"❌ Another instance may be running!\n"
            f"   Lock file: {LOCK_FILE}\n"
            f"   PID: {existing_lock.get('PID', 'unknown')}\n"
            f"   Started: {existing_lock.get('Started', 'unknown')}\n"
            f"\n"
            f"   If this is a stale lock, delete it:\n"
            f"   rm {LOCK_FILE}"
        )
        return 1

    if not _acquire_lock():
        logger.error("Failed to acquire instance lock")
        return 1

    logger.info("   ✅ Lock acquired")
    logger.info("")

    # ══════════════════════════════════════════════════════════════
    #  INITIALIZE COMPONENTS
    # ══════════════════════════════════════════════════════════════

    # Initialize these outside try block for cleanup access
    scheduler = None
    exchange = None
    controller = None
    state = None

    try:
        # ── State Manager ─────────────────────────────────────────
        logger.info("💾 Initializing state manager...")

        state = StateManager(initial_balance=config["initial_balance"])
        
        # Initialize state with config values
        _initialize_state_with_config(state, config)

        balance = state.get("balance", 0)
        positions_count = len(state.get_all_positions())
        bot_active = state.get("bot_active", True)
        daily_pnl = state.get("daily_pnl", 0.0)
        daily_pnl_inr = daily_pnl * config["usd_to_inr_rate"]

        logger.info(f"   Balance:     ${balance:,.2f}")
        logger.info(f"   Positions:   {positions_count}")
        logger.info(f"   Active:      {bot_active}")
        logger.info(f"   Daily P/L:   ₹{daily_pnl_inr:+,.2f}")
        logger.info("")

        # ── Exchange ──────────────────────────────────────────────
        logger.info("💱 Initializing exchange...")

        exchange = create_exchange(
            mode=config["mode"],
            exchange_type=config["exchange"],
            state_manager=state,
        )

        if exchange.ping():
            logger.info(f"   ✅ Connected: {exchange}")
        else:
            logger.warning(f"   ⚠️ Ping failed: {exchange} (may still work)")
        logger.info("")

        # ── Market Analyzers ──────────────────────────────────────
        logger.info("📊 Initializing market analyzers...")

        analyzers = _create_analyzers(config["coins"], config)

        for coin in analyzers:
            logger.info(f"   ✅ {coin}")
        logger.info("")

        # ── Strategy ──────────────────────────────────────────────
        logger.info("🎯 Initializing strategy...")

        primary_symbol = config["coins"][0]
        strategy = _create_strategy(primary_symbol, config)

        logger.info(f"   Strategy:        {strategy.name}")
        logger.info(f"   Min Probability: {config['min_trade_probability']}%")
        logger.info(f"   R/R Ratio:       {config['risk_reward_ratio']}:1")
        logger.info("")

        # ── Controller ────────────────────────────────────────────
        logger.info("🎮 Initializing controller...")

        controller = BotController(
            state_manager=state,
            exchange=exchange,
            analyzers=analyzers,
            strategy=strategy,
            notifier=None,  # Set by Telegram
            mode=config["mode"],
            coins=config["coins"],
            interval=config["interval"],
            base_risk=config["base_risk"],
            max_daily_drawdown=config["max_daily_drawdown"],
            emergency_drawdown=config["emergency_drawdown"],
            max_exposure_pct=config["max_exposure_pct"],
            max_consecutive_losses=config["max_consecutive_losses"],
            fee_pct=config["fee_pct"],
            slippage_pct=config["slippage_pct"],
            daily_loss_limit_inr=config["daily_loss_limit_inr"],
            usd_to_inr_rate=config["usd_to_inr_rate"],
        )

        logger.info("   ✅ Controller ready")
        logger.info("")

        # ── Scheduler ─────────────────────────────────────────────
        logger.info("⏰ Initializing scheduler...")

        # Determine exchange type for market awareness
        exchange_type = "crypto"
        if "USD" in config["coins"][0] and "USDT" not in config["coins"][0]:
            exchange_type = "us_stock"

        scheduler = TradeScheduler(
            controller=controller,
            interval=config["interval"],
            idle_poll=2,
            max_consecutive_errors=config["max_scheduler_errors"],
            market_aware=config["market_aware"],
            exchange_type=exchange_type,
            jitter_seconds=config["jitter_seconds"],
            adaptive_interval=config["adaptive_interval"],
            # NEW: Pass new config values
            hourly_analysis_enabled=config["hourly_analysis_enabled"],
            daily_loss_limit_inr=config["daily_loss_limit_inr"],
        )

        controller.scheduler = scheduler

        logger.info(f"   Interval:        {config['interval']}s")
        logger.info(f"   Jitter:          ±{config['jitter_seconds']}s")
        logger.info(f"   Hourly Analysis: {'✅ Enabled' if config['hourly_analysis_enabled'] else '❌ Disabled'}")
        logger.info(f"   Daily Limit:     ₹{config['daily_loss_limit_inr']:,.0f}")
        logger.info(f"   Market Aware:    {config['market_aware']}")
        
        if config["market_aware"]:
            mkt = market_status(exchange_type)
            logger.info(
                f"   Market:          {'🟢 Open' if mkt.is_open else '🔴 Closed'} "
                f"({mkt.session.value})"
            )
        logger.info("")

        # ══════════════════════════════════════════════════════════
        #  SETUP SIGNAL HANDLERS
        # ══════════════════════════════════════════════════════════

        shutdown_event = asyncio.Event()

        def handle_shutdown(sig: signal.Signals) -> None:
            sig_name = sig.name if hasattr(sig, "name") else str(sig)
            logger.warning(f"⚠️ Received signal: {sig_name}")
            if scheduler:
                scheduler.stop(reason=f"Signal {sig_name}")
            shutdown_event.set()

        loop = asyncio.get_running_loop()
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(
                    sig, lambda s=sig: handle_shutdown(s)
                )
            logger.debug("Signal handlers registered")
        except NotImplementedError:
            logger.warning("Signal handlers not supported (Windows?)")

        # ══════════════════════════════════════════════════════════
        #  START TELEGRAM
        # ══════════════════════════════════════════════════════════

        telegram_task = None
        if config["telegram_enabled"]:
            logger.info("📱 Starting Telegram bot...")
            telegram_task = asyncio.create_task(
                _start_telegram(
                    controller,
                    max_retries=config["telegram_retry_count"],
                ),
                name="telegram-startup",
            )
        else:
            logger.info("📱 Telegram disabled")

        # ══════════════════════════════════════════════════════════
        #  START SCHEDULER
        # ══════════════════════════════════════════════════════════

        logger.info("🚀 Starting scheduler...")
        scheduler_task = asyncio.create_task(
            scheduler.start(),
            name="trade-scheduler",
        )

        # ══════════════════════════════════════════════════════════
        #  BOT IS LIVE
        # ══════════════════════════════════════════════════════════

        startup_timer.stop()

        logger.info("")
        logger.info("═" * 60)
        logger.info("✅ BOT IS LIVE!")
        logger.info(f"   Mode:            {config['mode']}")
        logger.info(f"   Coins:           {', '.join(config['coins'])}")
        logger.info(f"   Interval:        {format_duration(config['interval'])}")
        logger.info(f"   Balance:         ${balance:,.2f}")
        logger.info(f"   Min Probability: {config['min_trade_probability']}%")
        logger.info(f"   Daily Limit:     ₹{config['daily_loss_limit_inr']:,.0f}")
        logger.info(f"   Startup Time:    {startup_timer.elapsed:.2f}s")
        logger.info("═" * 60)
        logger.info("")
        logger.info("📌 Trading Rules Active:")
        logger.info(f"   • Only enter trades with ≥{config['min_trade_probability']}% probability")
        logger.info(f"   • Stop trading if daily loss reaches ₹{config['daily_loss_limit_inr']:,.0f}")
        logger.info(f"   • Auto-resume trading next day (00:00 UTC)")
        if config['hourly_analysis_enabled']:
            logger.info("   • Hourly market analysis notifications enabled")
        logger.info("")

        # ══════════════════════════════════════════════════════════
        #  RUN UNTIL SHUTDOWN
        # ══════════════════════════════════════════════════════════

        tasks = [scheduler_task]
        if telegram_task:
            tasks.append(telegram_task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Log any exceptions
        task_names = ["Scheduler", "Telegram"]
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                if not isinstance(result, asyncio.CancelledError):
                    logger.error(f"❌ {task_names[i]} error: {result}")

        return 0

    except asyncio.CancelledError:
        logger.info("Main tasks cancelled")
        return 0

    except Exception as e:
        logger.exception(f"❌ Fatal error: {e}")
        return 1

    finally:
        # ══════════════════════════════════════════════════════════
        #  CLEANUP
        # ══════════════════════════════════════════════════════════

        logger.info("")
        logger.info("🧹 Shutting down...")

        # Stop scheduler
        if scheduler and scheduler.is_running:
            scheduler.stop(reason="Shutdown")
            logger.info("   ✅ Scheduler stopped")

        # Close exchange
        if exchange:
            try:
                close_fn = getattr(exchange, "close", None)
                if close_fn:
                    if asyncio.iscoroutinefunction(close_fn):
                        await close_fn()
                    else:
                        close_fn()
                logger.info("   ✅ Exchange closed")
            except Exception as e:
                logger.warning(f"   ⚠️ Exchange close error: {e}")

        # Stop Telegram
        if controller:
            telegram_app = getattr(controller, "_telegram_app", None)
            if telegram_app:
                try:
                    from app.tg.bot import stop_telegram_bot
                    await stop_telegram_bot(telegram_app)
                    logger.info("   ✅ Telegram stopped")
                except Exception as e:
                    logger.warning(f"   ⚠️ Telegram stop error: {e}")

        # Save final state
        if state:
            try:
                state.set("last_stop_time", datetime.now(timezone.utc).isoformat())
                state.save()
                logger.info("   ✅ State saved")
            except Exception as e:
                logger.warning(f"   ⚠️ State save error: {e}")

        # Log final statistics
        if scheduler:
            stats = scheduler.get_stats()
            logger.info("")
            logger.info("📊 Session Statistics:")
            logger.info(f"   Cycles:          {stats['total_cycles']}")
            logger.info(f"   Errors:          {stats['total_errors']}")
            logger.info(f"   Uptime:          {stats['uptime_human']}")
            logger.info(f"   Avg Latency:     {stats['avg_latency_sec']:.2f}s")
            logger.info(f"   Trades:          {stats['recent_trades_executed']}")
            logger.info(f"   P/L:             ₹{stats['recent_pnl'] * config.get('usd_to_inr_rate', 83):+,.2f}")
            logger.info(f"   Hourly Reports:  {stats['hourly_analysis_count']}")
            if stats['daily_loss_halted']:
                logger.info(f"   ⚠️ Daily loss limit was reached")

        # Release lock
        _release_lock()

        logger.info("")
        logger.info("👋 Goodbye!")
        logger.info("═" * 60)


# ═════════════════════════════════════════════════════════════════
#  SYNCHRONOUS ENTRY POINT
# ═════════════════════════════════════════════════════════════════

def run() -> None:
    """Synchronous entry point for console script."""
    # Check Python version
    if not _check_python_version():
        sys.exit(1)

    # Parse CLI arguments
    cli_args = _parse_args()

    # Run async main
    try:
        exit_code = asyncio.run(main(cli_args))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n👋 Interrupted by user (Ctrl+C)")
        _release_lock()
        sys.exit(0)
    except Exception as e:
        print(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        _release_lock()
        sys.exit(1)


# ═════════════════════════════════════════════════════════════════
#  DIRECT EXECUTION
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    run()