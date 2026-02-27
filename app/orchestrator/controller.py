import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict

from app.utils.logger import get_logger
from app.market.analyzer import MarketAnalyzer, MarketState
from app.strategies.base import BaseStrategy

logger = get_logger(__name__)


class BotController:
    """
    Central Trade Orchestrator (Autonomous Brain)
    """

    def __init__(
        self,
        state_manager,
        exchange,
        analyzer: MarketAnalyzer,
        strategy: BaseStrategy,
        notifier=None,
    ):
        self.state = state_manager
        self.exchange = exchange
        self.analyzer = analyzer
        self.strategy = strategy
        self.notifier = notifier

        self.latest_market_state: Optional[MarketState] = None
        self.last_hourly_report: Optional[datetime] = None

        self.loop_count: int = 0
        self.start_time: datetime = datetime.utcnow()

    # =====================================================
    # SAFE NOTIFY
    # =====================================================
    def _notify(self, message: str):
        if not self.notifier:
            return

        try:
            asyncio.create_task(self.notifier.send(message))
        except RuntimeError:
            logger.warning("⚠️ Event loop not running. Skipping notification.")

    # =====================================================
    # BOT CONTROL
    # =====================================================
    def start_bot(self):
        if self.state.get("bot_active"):
            return False

        self.state.activate_bot()
        self.start_time = datetime.utcnow()
        self.loop_count = 0
        self.last_hourly_report = None

        self._notify(
            "🚀 BOT STARTED\n\n"
            "🤖 Paper Trading: ACTIVE\n"
            "📡 Auto Trading: ENABLED\n"
            "⏱ Hourly Reports: ENABLED\n"
            f"💰 Starting Balance: ${round(self.state.get('balance'),2)}"
        )

        return True

    def stop_bot(self):
        if not self.state.get("bot_active"):
            return False

        self.state.deactivate_bot()
        self._notify("🛑 Bot stopped safely")
        return True

    def is_active(self):
        return self.state.get("bot_active")

    # =====================================================
    # MAIN CYCLE
    # =====================================================
    def run_cycle(self):
        if not self.is_active():
            return

        self.loop_count += 1
        self._reset_daily_limits_if_needed()

        symbol = self.strategy.get_symbol()

        price = self.exchange.get_price(symbol)
        candles = self.exchange.get_recent_candles(symbol, limit=60)

        if not price or not candles:
            logger.warning("⚠️ Market data unavailable")
            return

        market_data = {"price": price, "candles": candles}
        market = self.analyzer.analyze(market_data)
        self.latest_market_state = market

        position = self.state.get_position(symbol)

        # EXIT FIRST
        if position:
            exit_signal = self.strategy.should_exit(market, position)
            if exit_signal:
                self._execute_exit(market, exit_signal)
                return

        # ENTRY
        if not position and self.state.can_trade():
            entry_signal = self.strategy.should_enter(market)
            if entry_signal:
                self._execute_entry(market, entry_signal)

        # HOURLY REPORT
        self._maybe_send_hourly_report(market)

    # =====================================================
    # EXECUTION
    # =====================================================
    def _execute_entry(self, market: MarketState, signal: Dict):
        qty = self.strategy.get_quantity()
        price = market.price

        self.exchange.buy(symbol=market.symbol, quantity=qty)

        self.enter_trade(
            symbol=market.symbol,
            quantity=qty,
            price=price,
            meta=signal,
        )

        self._notify(
            f"🟢 BUY EXECUTED\n\n"
            f"Coin: {market.symbol}\n"
            f"Entry Price: ${round(price,2)}\n"
            f"Quantity: {qty}\n"
            f"Investment: ${round(price*qty,2)}\n"
            f"Balance Left: ${round(self.state.get('balance'),2)}\n"
            f"Strategy: {self.strategy.__class__.__name__}\n"
            f"Reason: {signal.get('reason')}"
        )

    def _execute_exit(self, market: MarketState, signal: Dict):
        position = self.state.get_position(market.symbol)
        qty = position["quantity"]
        price = market.price

        self.exchange.sell(symbol=market.symbol, quantity=qty)

        summary = self.exit_trade(
            symbol=market.symbol,
            quantity=qty,
            price=price,
            meta=signal,
        )

        self._notify(
            f"🔴 SELL EXECUTED\n\n"
            f"Coin: {market.symbol}\n"
            f"Exit Price: ${round(price,2)}\n"
            f"P&L: ${summary['pnl_amount']} ({summary['pnl_pct']}%)\n"
            f"Updated Balance: ${round(self.state.get('balance'),2)}\n"
            f"Total Trades Today: {self.state.get('trades_done_today')}\n"
            f"Strategy: {self.strategy.__class__.__name__}\n"
            f"Reason: {signal.get('reason')}"
        )

    # =====================================================
    # TRADE MANAGEMENT
    # =====================================================
    def enter_trade(self, symbol, quantity, price, meta=None):
        if self.state.get_position(symbol):
            return False

        position = {
            "symbol": symbol,
            "quantity": quantity,
            "avg_price": price,
            "entry_time": datetime.utcnow().isoformat(),
            "meta": meta or {},
        }

        self.state.add_position(symbol, quantity, price, position)
        return True

    def exit_trade(self, symbol, quantity, price, meta=None):
        position = self.state.get_position(symbol)
        if not position:
            return False

        entry_price = position["avg_price"]
        pnl_pct = (price - entry_price) / entry_price
        pnl_amount = pnl_pct * quantity * entry_price

        trade = {
            "symbol": symbol,
            "entry_price": entry_price,
            "exit_price": price,
            "quantity": quantity,
            "pnl_pct": round(pnl_pct * 100, 3),
            "pnl_amount": round(pnl_amount, 4),
            "entry_time": position["entry_time"],
            "exit_time": datetime.utcnow().isoformat(),
            "meta": meta or {},
        }

        self.state.remove_position(symbol)
        self.state.record_trade(trade)
        self.state.increment_trade_count()

        return trade

    # =====================================================
    # HOURLY PERFORMANCE REPORT
    # =====================================================
    def _maybe_send_hourly_report(self, market: MarketState):
        now = datetime.utcnow()

        if self.last_hourly_report and now - self.last_hourly_report < timedelta(hours=1):
            return

        self.last_hourly_report = now

        balance = self.state.get("balance")
        positions = self.state.get("positions") or {}

        total_unrealized = 0.0
        position_details = ""

        for sym, pos in positions.items():
            entry = pos["avg_price"]
            qty = pos["quantity"]
            current_price = market.price
            pnl = (current_price - entry) * qty
            pnl_pct = ((current_price - entry) / entry) * 100

            total_unrealized += pnl

            position_details += (
                f"\n📈 {sym}\n"
                f"Entry: ${round(entry,2)}\n"
                f"Current: ${round(current_price,2)}\n"
                f"Unrealized: ${round(pnl,2)} ({round(pnl_pct,2)}%)\n"
            )

        status = "PROFIT ✅" if total_unrealized >= 0 else "LOSS ❌"

        message = (
            f"📊 HOURLY PERFORMANCE REPORT\n\n"
            f"💰 Balance: ${round(balance,2)}\n"
            f"📈 Unrealized PnL: ${round(total_unrealized,2)}\n"
            f"📊 Status: {status}\n"
            f"🔁 Trades Today: {self.state.get('trades_done_today')}/"
            f"{self.state.get('max_trades_per_day')}\n"
            f"📡 Market: {market.symbol} @ ${round(market.price,2)}\n"
            f"{position_details if position_details else 'No Open Positions'}"
        )

        self._notify(message)

    # =====================================================
    # DAILY RESET
    # =====================================================
    def _reset_daily_limits_if_needed(self):
        today = str(datetime.utcnow().date())
        last_day = self.state.get("last_trade_date")

        if last_day != today:
            self.state.set("trades_done_today", 0)
            self.state.set("daily_pnl", 0.0)
            self.state.set("last_trade_date", today)

    # =====================================================
    # STATUS ACCESS
    # =====================================================
    def get_status(self):
        return {
            "bot_active": self.state.get("bot_active"),
            "balance": self.state.get("balance"),
            "trades_done_today": self.state.get("trades_done_today"),
            "max_trades_per_day": self.state.get("max_trades_per_day"),
            "open_positions": self.state.get("positions"),
            "loop_count": self.loop_count,
        }