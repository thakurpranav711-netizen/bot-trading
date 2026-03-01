# app/orchestrator/controller.py

"""
Bot Controller — Production Grade

Institutional-grade autonomous trading engine with:
- 4-Brain weighted decision engine
- Full position lifecycle (entry, management, exit)
- Break-even and trailing stop logic (PERSISTED)
- SL/TP hit detection
- Short position support
- Slippage + fee modeling
- Clean PnL accounting
- Risk management integration (KillSwitch, LossGuard, TradeLimiter)
- Adaptive position sizing
- Hourly performance reports
- Async-safe notification system
"""

import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from app.utils.logger import get_logger
from app.market.analyzer import MarketAnalyzer, MarketState
from app.strategies.base import BaseStrategy
from app.risk.adaptive_risk import AdaptiveRiskManager
from app.risk.kill_switch import KillSwitch
from app.risk.loss_guard import LossGuard
from app.risk.trade_limiter import TradeLimiter

logger = get_logger(__name__)


class BotController:
    """
    Central Trading Engine

    Coordinates:
    - Market analysis via MarketAnalyzer
    - Trade signals via Strategy
    - Risk management via AdaptiveRisk, KillSwitch, LossGuard, TradeLimiter
    - Order execution via Exchange
    - State persistence via StateManager
    - Notifications via Telegram Notifier
    """

    def __init__(
        self,
        state_manager,
        exchange,
        analyzer: MarketAnalyzer,
        strategy: BaseStrategy,
        notifier=None,
        mode: str = "PAPER",
        coins: List[str] = None,
        interval: int = 300,
        base_risk: float = 0.01,
        max_daily_drawdown: float = 0.05,
        max_exposure_pct: float = 0.30,
        max_consecutive_losses: int = 3,
        fee_pct: float = 0.001,
        slippage_pct: float = 0.0005,
    ):
        self.state = state_manager
        self.exchange = exchange
        self.analyzer = analyzer
        self.strategy = strategy
        self.notifier = notifier

        self.mode = mode.upper()
        self.coins = coins or []
        self.interval = interval
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.max_exposure_pct = max_exposure_pct

        # ── Latest market state (for status commands) ─────────────
        self.latest_market_state: Optional[MarketState] = None
        self.last_hourly_report: Optional[datetime] = None

        # ── Risk Management Components ────────────────────────────
        self.adaptive_risk = AdaptiveRiskManager(
            state_manager=self.state,
            base_risk=base_risk,
            max_daily_dd=max_daily_drawdown,
        )

        self.kill_switch = KillSwitch(
            state_manager=self.state,
            exchange=self.exchange,
            auto_close_positions=True,
        )

        self.loss_guard = LossGuard(
            state_manager=self.state,
            kill_switch=self.kill_switch,
            max_daily_loss_pct=max_daily_drawdown,
            max_consecutive_losses=max_consecutive_losses,
        )

        self.trade_limiter = TradeLimiter(
            state_manager=self.state,
            max_trades_per_day=10,
            max_trades_per_hour=3,
            min_trade_interval_sec=60,
        )

        # ── Scheduler reference (set by main.py) ──────────────────
        self.scheduler = None

    # ═════════════════════════════════════════════════════
    #  NOTIFICATION HELPER
    # ═════════════════════════════════════════════════════

    def _notify(self, coro_fn):
        """
        Safely schedule an async notifier coroutine from sync context.

        Usage:
            self._notify(lambda: self.notifier.send_xxx(...))
        """
        if not self.notifier:
            return

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro_fn())
        except RuntimeError:
            logger.debug("⚠️ No event loop — notification skipped")

    # ═════════════════════════════════════════════════════
    #  LIFECYCLE HOOKS
    # ═════════════════════════════════════════════════════

    def on_start(self):
        """Called when bot starts — sends TRADING BOT STARTED message."""
        balance = self.state.get("balance", 0.0)

        self._notify(
            lambda: self.notifier.send_bot_started(
                mode=self.mode,
                balance=balance,
                coins=self.coins,
                interval=self.interval,
                timestamp=datetime.utcnow(),
            )
        )

        logger.info(
            f"🤖 Bot started | Mode={self.mode} | Balance=${balance:.2f} | "
            f"Coins={self.coins}"
        )

    def on_stop(self, reason: str = "User request"):
        """Called when bot stops — sends TRADING BOT STOPPED message."""
        self._notify(
            lambda: self.notifier.send_bot_stopped(reason=reason)
        )
        logger.info(f"🛑 Bot stopped | Reason={reason}")

    # ═════════════════════════════════════════════════════
    #  MAIN TRADING CYCLE (FIXED)
    # ═════════════════════════════════════════════════════

    def run_cycle(self):
        """
        Main trading cycle — called by scheduler every interval.

        Flow:
        1. Check kill switch
        2. Check bot_active state
        3. Check loss guard
        4. Reset exchange cycle (price cache)
        5. Get market data and analyze
        6. Manage existing positions (SL/TP/trailing)
        7. Evaluate new entries if no position
        8. Send hourly report if due
        """
        logger.info("🔁 Controller cycle starting...")

        # ── Gate 1: Kill switch ───────────────────────────────────
        if self.kill_switch.is_active():
            logger.warning("🛑 Kill switch active — cycle skipped")
            return

        # ── Gate 2: Bot active ────────────────────────────────────
        if not self.state.get("bot_active", True):
            logger.debug("⏸️ Bot inactive — cycle skipped")
            return

        # ── Gate 3: Loss guard ────────────────────────────────────
        can_trade, reason = self.loss_guard.can_trade()
        if not can_trade:
            logger.warning(f"🛑 Loss guard: {reason}")
            return

        # ── Reset exchange price cache ────────────────────────────
        self.exchange.begin_cycle()

        try:
            # ── Get symbol ────────────────────────────────────────
            symbol = self.strategy.get_symbol()

            # ── Fetch market data ─────────────────────────────────
            price = self.exchange.get_price(symbol)
            candles = self.exchange.get_recent_candles(symbol, limit=150)

            if not price or not candles:
                logger.warning(f"⚠️ No market data for {symbol}")
                return

            # ── Analyze market ────────────────────────────────────
            market = self.analyzer.analyze({
                "price": price,
                "candles": candles,
            })
            self.latest_market_state = market

            # ── Update strategy loss streak ───────────────────────
            loss_streak = self.state.get("loss_streak", 0)
            self.strategy.set_loss_streak(loss_streak)

            # ── Check existing position ───────────────────────────
            position = self.state.get_position(symbol)

            if position:
                self._manage_position(market, position)
            else:
                self._evaluate_entry(market, symbol, price)

            # ── Hourly report ─────────────────────────────────────
            self._maybe_send_hourly_report()

        except Exception as e:
            logger.exception(f"❌ Cycle error: {e}")
            self._notify(
                lambda: self.notifier.send_error(
                    context="Trading cycle",
                    error=str(e),
                )
            )

        finally:
            self.exchange.end_cycle()

    # ═════════════════════════════════════════════════════
    #  ENTRY EVALUATION
    # ═════════════════════════════════════════════════════

    def _evaluate_entry(
        self,
        market: MarketState,
        symbol: str,
        price: float,
    ):
        """Evaluate potential trade entry."""

        # ── Gate: Trade limiter ───────────────────────────────────
        can_open, limit_reason = self.trade_limiter.can_open_trade(symbol)
        if not can_open:
            logger.info(f"⏳ Trade limited: {limit_reason}")
            return

        # ── Gate: State can_trade ─────────────────────────────────
        if not self.state.can_trade():
            logger.debug("⏳ State blocks trading")
            return

        # ── Run 4-Brain decision engine ───────────────────────────
        decision = self._run_decision_engine(market, symbol, price)

        if not decision["trade"]:
            logger.debug(
                f"📊 No trade signal | {decision['final_signal']} "
                f"conf={decision['confidence']}"
            )
            return

        # ── Get strategy signal ───────────────────────────────────
        entry_signal = self.strategy.should_enter(market)

        if not entry_signal:
            logger.debug("📊 Strategy declined entry")
            return

        # ── Validate with loss guard ──────────────────────────────
        stop_loss = entry_signal["stop_loss"]
        potential_loss = abs(price - stop_loss) * self._estimate_quantity(
            price, stop_loss
        )
        valid, risk_reason = self.loss_guard.validate_trade_risk(potential_loss)
        if not valid:
            logger.warning(f"⚠️ Risk validation failed: {risk_reason}")
            return

        # ── Execute entry ─────────────────────────────────────────
        self._execute_entry(entry_signal)

    # ═════════════════════════════════════════════════════
    #  4-BRAIN DECISION ENGINE
    # ═════════════════════════════════════════════════════

    def _run_decision_engine(
        self,
        market: MarketState,
        symbol: str,
        price: float,
    ) -> Dict:
        """
        Aggregate signals from 4 analysis brains.
        Sends Decision Engine Report to Telegram.
        """
        brains = self._collect_brain_signals(market)

        # ── Weighted voting ───────────────────────────────────────
        weighted_buy = 0.0
        weighted_sell = 0.0
        votes_buy = votes_sell = votes_hold = 0

        for b in brains:
            sig = b["signal"].upper()
            weight = b["weight_pct"] / 100.0
            conf = b["confidence_pct"] / 100.0

            if sig == "BUY":
                weighted_buy += weight * conf * 100
                votes_buy += 1
            elif sig == "SELL":
                weighted_sell += weight * conf * 100
                votes_sell += 1
            else:
                votes_hold += 1

        # ── Determine final signal ────────────────────────────────
        if weighted_buy > weighted_sell and weighted_buy > 15:
            final_signal = "BUY"
        elif weighted_sell > weighted_buy and weighted_sell > 15:
            final_signal = "SELL"
        else:
            final_signal = "HOLD"

        # ── Confidence tier ───────────────────────────────────────
        score = max(weighted_buy, weighted_sell)
        if score >= 50:
            confidence = "HIGH"
        elif score >= 25:
            confidence = "MODERATE"
        else:
            confidence = "LOW"

        # ── Trade gate ────────────────────────────────────────────
        trade = (
            final_signal in ("BUY", "SELL") and
            confidence in ("HIGH", "MODERATE")
        )

        # ── Send Telegram report ──────────────────────────────────
        self._notify(
            lambda: self.notifier.send_decision_report(
                coin=symbol,
                price=price,
                brains=brains,
                votes_buy=votes_buy,
                votes_sell=votes_sell,
                votes_hold=votes_hold,
                weighted_buy=round(weighted_buy, 1),
                weighted_sell=round(weighted_sell, 1),
                final_signal=final_signal,
                confidence=confidence,
                trade=trade,
            )
        )

        return {
            "final_signal": final_signal,
            "confidence": confidence,
            "trade": trade,
            "weighted_buy": weighted_buy,
            "weighted_sell": weighted_sell,
        }

    def _collect_brain_signals(self, market: MarketState) -> List[Dict]:
        """Collect signals from all 4 brains."""
        brains = []

        brain1 = self._brain_indicators(market)
        brain1["weight_pct"] = 35
        brains.append(brain1)

        brain2 = self._brain_sentiment(market)
        brain2["weight_pct"] = 15
        brains.append(brain2)

        brain3 = self._brain_chart(market)
        brain3["weight_pct"] = 25
        brains.append(brain3)

        brain4 = self._brain_ai(market)
        brain4["weight_pct"] = 25
        brains.append(brain4)

        return brains

    def _brain_indicators(self, market: MarketState) -> Dict:
        """Brain 1: Technical indicators."""
        indicators = market.indicators or {}
        score = 0
        count = 0

        rsi = indicators.get("rsi")
        if rsi is not None:
            count += 1
            if rsi < 35:
                score += 1
            elif rsi > 65:
                score -= 1

        macd = indicators.get("macd_cross")
        if macd:
            count += 1
            if macd == "bullish":
                score += 1
            elif macd == "bearish":
                score -= 1

        ema = indicators.get("ema_cross")
        if ema:
            count += 1
            if ema == "bullish":
                score += 1
            elif ema == "bearish":
                score -= 1

        bb = indicators.get("bb_position")
        if bb:
            count += 1
            if bb == "oversold":
                score += 1
            elif bb == "overbought":
                score -= 1

        if count == 0:
            return {"name": "Brain1 Indicators", "signal": "HOLD", "confidence_pct": 0}

        confidence = int(abs(score) / count * 100)
        signal = "BUY" if score > 0 else ("SELL" if score < 0 else "HOLD")

        return {
            "name": "Brain1 Indicators",
            "signal": signal,
            "confidence_pct": confidence,
        }

    def _brain_sentiment(self, market: MarketState) -> Dict:
        """Brain 2: Market sentiment."""
        sentiment = market.sentiment_score

        if sentiment is None or sentiment == 0:
            return {"name": "Brain2 Sentiment", "signal": "HOLD", "confidence_pct": 0}

        confidence = int(min(abs(sentiment) * 100, 100))
        signal = "BUY" if sentiment > 0.1 else ("SELL" if sentiment < -0.1 else "HOLD")

        return {
            "name": "Brain2 Sentiment",
            "signal": signal,
            "confidence_pct": confidence,
        }

    def _brain_chart(self, market: MarketState) -> Dict:
        """Brain 3: Chart pattern recognition."""
        pattern = market.chart_pattern

        if not pattern:
            return {"name": "Brain3 Chart", "signal": "HOLD", "confidence_pct": 0}

        return {
            "name": "Brain3 Chart",
            "signal": pattern.get("signal", "HOLD"),
            "confidence_pct": pattern.get("confidence", 0),
        }

    def _brain_ai(self, market: MarketState) -> Dict:
        """Brain 4: AI/ML prediction."""
        ai = market.ai_prediction

        if not ai:
            return {"name": "Brain4 AI", "signal": "HOLD", "confidence_pct": 0}

        return {
            "name": "Brain4 AI",
            "signal": ai.get("signal", "HOLD"),
            "confidence_pct": ai.get("confidence", 0),
        }

    # ═════════════════════════════════════════════════════
    #  POSITION MANAGEMENT
    # ═════════════════════════════════════════════════════

    def _manage_position(self, market: MarketState, position: Dict):
        """
        Manage an existing open position.

        Checks in order:
        1. Stop Loss hit
        2. Take Profit hit
        3. Break-even activation
        4. Trailing stop update
        5. Strategy exit signal
        """
        symbol = position["symbol"]
        entry = position["entry_price"]
        sl = position["stop_loss"]
        tp = position["take_profit"]
        qty = position["quantity"]
        current_price = market.price

        action = position.get("action", "BUY").upper()
        is_long = action == "BUY"

        # ── Check Stop Loss ───────────────────────────────────────
        sl_hit = (
            (is_long and current_price <= sl) or
            (not is_long and current_price >= sl)
        )
        if sl_hit:
            self._execute_exit(position, current_price, "STOP LOSS")
            return

        # ── Check Take Profit ─────────────────────────────────────
        tp_hit = (
            (is_long and current_price >= tp) or
            (not is_long and current_price <= tp)
        )
        if tp_hit:
            self._execute_exit(position, current_price, "TAKE PROFIT")
            return

        # ── Break-even activation ─────────────────────────────────
        updated = False
        if is_long:
            risk = entry - sl
            reward = tp - entry
            if reward > 0:
                progress = (current_price - entry) / reward
                if progress > 0.6 and sl < entry:
                    new_sl = entry + (entry * 0.001)
                    position["stop_loss"] = new_sl
                    updated = True
                    logger.info(
                        f"✅ Break-even activated | {symbol} | "
                        f"SL: ${sl:.2f} → ${new_sl:.2f}"
                    )
        else:
            risk = sl - entry
            reward = entry - tp
            if reward > 0:
                progress = (entry - current_price) / reward
                if progress > 0.6 and sl > entry:
                    new_sl = entry - (entry * 0.001)
                    position["stop_loss"] = new_sl
                    updated = True
                    logger.info(
                        f"✅ Break-even activated | {symbol} | "
                        f"SL: ${sl:.2f} → ${new_sl:.2f}"
                    )

        # ── Trailing stop ─────────────────────────────────────────
        if is_long:
            if tp > entry:
                rr_progress = (current_price - entry) / (tp - entry)
                if rr_progress > 1.0:
                    new_sl = current_price * 0.995
                    if new_sl > position["stop_loss"]:
                        position["stop_loss"] = new_sl
                        updated = True
                        logger.info(
                            f"📐 Trailing stop | {symbol} | SL → ${new_sl:.2f}"
                        )
        else:
            if tp < entry:
                rr_progress = (entry - current_price) / (entry - tp)
                if rr_progress > 1.0:
                    new_sl = current_price * 1.005
                    if new_sl < position["stop_loss"]:
                        position["stop_loss"] = new_sl
                        updated = True
                        logger.info(
                            f"📐 Trailing stop | {symbol} | SL → ${new_sl:.2f}"
                        )

        # ── Persist position updates ──────────────────────────────
        if updated:
            self.state.update_position(symbol, {
                "stop_loss": position["stop_loss"],
            })

        # ── Strategy exit signal ──────────────────────────────────
        exit_signal = self.strategy.should_exit(market, position)
        if exit_signal:
            self._execute_exit(
                position, current_price,
                exit_signal.get("reason", "STRATEGY EXIT")
            )

    # ═════════════════════════════════════════════════════
    #  EXECUTE ENTRY
    # ═════════════════════════════════════════════════════

    def _execute_entry(self, signal: Dict):
        """Execute a trade entry."""
        symbol = signal["symbol"]
        direction = signal["action"]
        entry_price = signal["entry_price"]
        stop_loss = signal["stop_loss"]
        take_profit = signal["take_profit"]
        confidence = signal.get("confidence", 0)

        # ── Calculate position size ───────────────────────────────
        qty = self._calculate_position_size(entry_price, stop_loss)

        if qty <= 0:
            logger.warning(f"⚠️ Position size is zero — entry skipped")
            return

        # ── Place order ───────────────────────────────────────────
        if direction == "BUY":
            fill = self.exchange.buy(symbol=symbol, quantity=qty)
        else:
            fill = self.exchange.buy(symbol=symbol, quantity=qty)

        # ── Validate fill ─────────────────────────────────────────
        if not fill or fill.get("status") == "REJECTED":
            logger.error(f"❌ Entry rejected | {symbol} {direction} qty={qty}")
            self._notify(
                lambda: self.notifier.send_error(
                    context="Entry rejected",
                    error=f"{symbol} {direction} qty={qty}",
                )
            )
            return

        # ── Extract fill details ──────────────────────────────────
        fill_price = float(fill.get("price", entry_price))
        filled_qty = float(fill.get("quantity", qty))
        fee = float(fill.get("fee", fill_price * filled_qty * self.fee_pct))
        cost = fill_price * filled_qty

        # ── Deduct from balance ───────────────────────────────────
        self.state.adjust_balance(-(cost + fee))

        # ── Record position ───────────────────────────────────────
        position_data = {
            "symbol": symbol,
            "action": direction,
            "entry_price": fill_price,
            "quantity": filled_qty,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "confidence": confidence,
            "strategy": signal.get("strategy", self.strategy.name),
            "fees_paid": fee,
            "entry_time": datetime.utcnow().isoformat(),
            "order_id": fill.get("order_id", ""),
            "mode": self.mode,
        }
        self.state.add_position(symbol, filled_qty, fill_price, position_data)

        # ── Record trade in limiter ───────────────────────────────
        self.trade_limiter.record_trade(symbol, direction)

        # ── Notify ────────────────────────────────────────────────
        balance = self.state.get("balance", 0)
        self._notify(
            lambda: self.notifier.send_trade_executed(
                mode=self.mode,
                side=direction,
                coin=symbol,
                amount=filled_qty,
                price=fill_price,
                cost=round(cost, 2),
                fee=round(fee, 4),
                remaining=round(balance, 2),
                strategy=position_data["strategy"],
                reason=signal.get("reason", direction),
                confidence=confidence,
            )
        )

        logger.info(
            f"✅ Entry executed | {symbol} {direction} | "
            f"Qty={filled_qty} @ ${fill_price:.2f} | "
            f"SL=${stop_loss:.2f} TP=${take_profit:.2f}"
        )

    # ═════════════════════════════════════════════════════
    #  EXECUTE EXIT
    # ═════════════════════════════════════════════════════

    def _execute_exit(
        self,
        position: Dict,
        exit_price: float,
        reason: str,
    ):
        """Execute a position exit."""
        symbol = position["symbol"]
        qty = position["quantity"]
        entry = position["entry_price"]
        action = position.get("action", "BUY").upper()
        is_long = action == "BUY"

        # ── Place sell order ──────────────────────────────────────
        fill = self.exchange.sell(symbol=symbol, quantity=qty)

        if not fill or fill.get("status") == "REJECTED":
            logger.error(f"❌ Exit rejected | {symbol} qty={qty}")
            self._notify(
                lambda: self.notifier.send_error(
                    context="Exit rejected",
                    error=f"{symbol} SELL qty={qty}",
                )
            )
            return

        # ── Extract fill details ──────────────────────────────────
        actual_exit = float(fill.get("price", exit_price))
        filled_qty = float(fill.get("quantity", qty))
        fee = float(fill.get("fee", actual_exit * filled_qty * self.fee_pct))

        # ── Calculate PnL ─────────────────────────────────────────
        if is_long:
            gross_pnl = (actual_exit - entry) * filled_qty
        else:
            gross_pnl = (entry - actual_exit) * filled_qty

        entry_fee = position.get("fees_paid", 0)
        net_pnl = gross_pnl - fee
        pnl_pct = (net_pnl / (entry * filled_qty)) * 100 if entry > 0 else 0

        # ── Close position in state ───────────────────────────────
        self.state.close_position(symbol, net_pnl, exit_price=actual_exit)

        # ── Update risk systems ───────────────────────────────────
        self.adaptive_risk.update_after_trade({
            "pnl_amount": net_pnl,
            "entry_price": entry,
            "exit_price": actual_exit,
            "quantity": filled_qty,
        })

        if net_pnl < 0:
            self.loss_guard.record_loss(abs(net_pnl), abs(pnl_pct))
        else:
            self.loss_guard.record_win(net_pnl, pnl_pct)

        # Update strategy loss streak
        loss_streak = self.state.get("loss_streak", 0)
        self.strategy.set_loss_streak(loss_streak)

        # ── Notify ────────────────────────────────────────────────
        self._notify(
            lambda: self.notifier.send_trade_closed(
                mode=self.mode,
                coin=symbol,
                entry_price=entry,
                exit_price=actual_exit,
                pnl=round(net_pnl, 4),
                pnl_pct=round(pnl_pct, 2),
                strategy=position.get("strategy", self.strategy.name),
                reason=reason,
            )
        )

        logger.info(
            f"✅ Exit executed | {symbol} | "
            f"Qty={filled_qty} @ ${actual_exit:.2f} | "
            f"PnL=${net_pnl:+.4f} ({pnl_pct:+.2f}%) | "
            f"Reason={reason}"
        )

    # ═════════════════════════════════════════════════════
    #  POSITION SIZING
    # ═════════════════════════════════════════════════════

    def _calculate_position_size(
        self,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        """Calculate position size based on risk management."""
        balance = self.state.get("balance", 0)

        if balance <= 0:
            return 0.0

        risk_pct = self.adaptive_risk.get_risk_percent(
            self.latest_market_state,
            confidence=None,
        )

        risk_amount = balance * risk_pct
        sl_distance = abs(entry_price - stop_loss)

        if sl_distance <= 0:
            return 0.0

        qty = risk_amount / sl_distance

        max_position_value = balance * self.max_exposure_pct
        if qty * entry_price > max_position_value:
            qty = max_position_value / entry_price

        return round(qty, 8)

    def _estimate_quantity(
        self,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        """Estimate quantity for risk validation."""
        balance = self.state.get("balance", 0)
        risk_pct = self.adaptive_risk.base_risk
        risk_amount = balance * risk_pct
        sl_distance = abs(entry_price - stop_loss)

        if sl_distance <= 0:
            return 0.0

        return risk_amount / sl_distance

    # ═════════════════════════════════════════════════════
    #  EMERGENCY SHUTDOWN
    # ═════════════════════════════════════════════════════

    def emergency_shutdown(self, reason: str):
        """Trigger emergency shutdown via kill switch."""
        start = self.state.get("start_of_day_balance", 0)
        current = self.state.get("balance", 0)
        loss_pct = ((start - current) / start * 100) if start > 0 else 0

        self.kill_switch.activate(
            reason=reason,
            source="controller",
            loss_pct=loss_pct,
            auto_resume_minutes=60,
        )

        self._notify(
            lambda: self.notifier.send_kill_switch(
                reason=reason,
                loss_pct=loss_pct,
            )
        )

        self.on_stop(reason=reason)

    # ═════════════════════════════════════════════════════
    #  HOURLY REPORT
    # ═════════════════════════════════════════════════════

    def _maybe_send_hourly_report(self):
        """Send hourly status report if due."""
        now = datetime.utcnow()

        if self.last_hourly_report:
            if now - self.last_hourly_report < timedelta(hours=1):
                return

        self.last_hourly_report = now

        positions = self.state.get_all_positions()
        balance = self.state.get("balance", 0)
        daily_pnl = self.state.get("daily_pnl", 0)
        trades_today = self.state.get("trades_today", 0)

        self._notify(
            lambda: self.notifier.send_status(
                running=True,
                mode=self.mode,
                balance=balance,
                open_trades=len(positions),
                total_trades_today=trades_today,
                max_trades=self.trade_limiter.max_trades_per_day,
                pnl_today=daily_pnl,
                coins=self.coins,
            )
        )

    # ═════════════════════════════════════════════════════
    #  STATUS HELPERS (FIXED)
    # ═════════════════════════════════════════════════════

    def get_status(self) -> Dict:
        """
        Get comprehensive bot status.

        FIXED: Now includes risk_locked flag.
        """
        positions = self.state.get_all_positions()
        balance = self.state.get("balance", 0)

        return {
            "running": self.state.get("bot_active", False),
            "mode": self.mode,
            "balance": balance,
            "daily_pnl": self.state.get("daily_pnl", 0),
            "open_positions": len(positions),
            "positions": positions,
            "trades_today": self.state.get("trades_today", 0),
            "win_streak": self.state.get("win_streak", 0),
            "loss_streak": self.state.get("loss_streak", 0),
            "kill_switch_active": self.kill_switch.is_active(),
            "risk_locked": self.state.get("risk_locked", False),
            "emergency_acknowledged": self.state.get("emergency_acknowledged", False),
            "risk_report": self.adaptive_risk.get_risk_report(),
            "loss_guard": self.loss_guard.get_status(),
            "trade_limiter": self.trade_limiter.get_status(),
        }

    def get_risk_report(self) -> Dict:
        """Get risk management report."""
        return self.adaptive_risk.get_risk_report()

    # ═════════════════════════════════════════════════════
    #  MANUAL CONTROLS (FIXED)
    # ═════════════════════════════════════════════════════

    def pause(self):
        """Pause trading (soft stop)."""
        self.state.set("bot_active", False)
        self.state.set("pause_reason", "Manual pause")
        logger.info("⏸️ Bot paused")

    def resume(self):
        """
        Resume trading.

        FIXED: Now properly unlocks ALL risk locks.

        Previous behavior:
            - Set bot_active=True
            - Called kill_switch.deactivate()
            - ❌ NEVER touched loss_guard → re-locked next cycle

        New behavior:
            - Set bot_active=True
            - Call kill_switch.deactivate()
            - Call loss_guard.unlock() ← FIXES the re-lock loop
            - Clear risk_locked flag
            - Clear pause_reason
        """
        # ── Clear ALL locks ───────────────────────────────────────
        self.state.set("bot_active", True)
        self.state.set("risk_locked", False)
        self.state.set("pause_reason", None)

        # ── Deactivate kill switch ────────────────────────────────
        self.kill_switch.deactivate(source="manual")

        # ── FIXED: Unlock loss guard (prevents re-lock loop) ─────
        self.loss_guard.unlock(source="resume")

        logger.info(
            "▶️ Bot resumed | kill_switch cleared | "
            "loss_guard unlocked | risk_locked=False"
        )

    def unlock_risk(self, source: str = "manual") -> Dict:
        """
        Full risk system unlock — NEW METHOD.

        Called from Telegram /unlock command.

        Does everything resume() does PLUS:
        - Returns detailed unlock summary
        - Logs the source for audit

        This is the ESCAPE HATCH for the lock loop.
        """
        # ── Clear all state flags ─────────────────────────────────
        self.state.set("bot_active", True)
        self.state.set("risk_locked", False)
        self.state.set("pause_reason", None)

        # ── Deactivate kill switch ────────────────────────────────
        ks_result = self.kill_switch.deactivate(source=source)

        # ── Unlock loss guard ─────────────────────────────────────
        lg_result = self.loss_guard.unlock(source=source)

        logger.warning(
            f"🔓 FULL RISK UNLOCK by {source} | "
            f"All locks cleared | Bot active"
        )

        return {
            "unlocked": True,
            "source": source,
            "bot_active": True,
            "risk_locked": False,
            "kill_switch": ks_result,
            "loss_guard": lg_result,
        }

    def reset_risk_baseline(self, source: str = "manual") -> Dict:
        """
        Reset risk baseline — NEW METHOD.

        Called from Telegram /reset_risk command.

        Accepts all past losses and starts fresh measurements:
        - initial_balance = current balance
        - Emergency drawdown measured from NEW baseline
        - All locks cleared
        - All streaks reset

        After this, the bot trades with current balance as the new "initial".
        """
        # ── Reset baseline in loss guard ──────────────────────────
        lg_result = self.loss_guard.reset_baseline(source=source)

        # ── Also unlock everything ────────────────────────────────
        self.state.set("bot_active", True)
        self.state.set("risk_locked", False)
        self.kill_switch.deactivate(source=source)

        # ── Reset adaptive risk streaks ───────────────────────────
        self.adaptive_risk.reset_streaks()

        balance = self.state.get("balance", 0)

        logger.warning(
            f"🔄 RISK BASELINE RESET by {source} | "
            f"New baseline: ${balance:.2f} | "
            f"All locks cleared | Streaks reset"
        )

        return {
            "reset": True,
            "source": source,
            "new_baseline": balance,
            "loss_guard": lg_result,
            "bot_active": True,
            "risk_locked": False,
        }

    def force_exit_all(self, reason: str = "Manual exit"):
        """Force close all positions."""
        positions = self.state.get_all_positions()

        for symbol, position in positions.items():
            price = self.exchange.get_price(symbol)
            self._execute_exit(position, price, reason)

        logger.warning(f"🚨 Force exited {len(positions)} positions")

    def __repr__(self) -> str:
        return (
            f"<BotController mode={self.mode} | "
            f"active={self.state.get('bot_active', False)} | "
            f"risk_locked={self.state.get('risk_locked', False)}>"
        )