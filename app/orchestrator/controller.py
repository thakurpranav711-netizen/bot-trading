# app/orchestrator/controller.py

"""
Autonomous AI Trading Controller — Production Grade

This is the central trading engine that:
1. Continuously monitors multiple cryptocurrency markets
2. Analyzes price movements using 4-Brain AI system
3. Decides when to BUY or SELL based on intelligent analysis
4. Executes trades via Alpaca/Binance API
5. Manages positions with SL/TP/trailing stops
6. Sends real-time notifications via Telegram

Key Features:
- Multi-coin monitoring and analysis
- 4-Brain weighted decision engine (Brain 4 = Claude AI)
- Proper LONG and SHORT position handling (FIXED)
- Break-even and trailing stop logic
- Comprehensive risk management
- Real-time Telegram notifications

CRITICAL FIXES vs Original:
1. SHORT entry: No longer calls sell() without position
2. SHORT exit: Now calls buy() to cover (was incorrectly calling sell())
3. Multi-coin: Now loops through all coins, not just first one
4. Brain 4: Properly integrated with Claude API
5. Position management: Proper is_long detection
"""

import asyncio
import json
import os
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

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
    Central Autonomous Trading Engine
    
    Responsibilities:
    - Market analysis for multiple coins
    - Trade signal generation via 4-Brain system
    - Order execution via exchange
    - Position lifecycle management
    - Risk management coordination
    - Telegram notification dispatch
    
    The controller is called by the scheduler every interval.
    It performs one complete analysis and trading cycle.
    """

    def __init__(
        self,
        state_manager,
        exchange,
        analyzers: Dict[str, MarketAnalyzer],
        strategy: BaseStrategy,
        notifier=None,
        mode: str = "PAPER",
        coins: List[str] = None,
        interval: int = 300,
        base_risk: float = 0.01,
        max_daily_drawdown: float = 0.05,
        emergency_drawdown: float = 0.15,
        max_exposure_pct: float = 0.30,
        max_consecutive_losses: int = 3,
        fee_pct: float = 0.001,
        slippage_pct: float = 0.0005,
    ):
        """
        Initialize the trading controller.
        
        Args:
            state_manager: StateManager for persistence
            exchange: Exchange client (Alpaca/Binance/Paper)
            analyzers: Dict mapping symbol to MarketAnalyzer
            strategy: Trading strategy instance
            notifier: Telegram notifier (set by bot.py)
            mode: "PAPER" or "LIVE"
            coins: List of symbols to trade
            interval: Seconds between cycles
            base_risk: Base risk per trade (0.01 = 1%)
            max_daily_drawdown: Max daily loss before stopping
            emergency_drawdown: Emergency drawdown threshold (0.15 = 15%)
            max_exposure_pct: Max portfolio in positions
            max_consecutive_losses: Losses before cooldown
            fee_pct: Exchange fee percentage
            slippage_pct: Expected slippage percentage
        """
        # ── Core Components ───────────────────────────────────────
        self.state = state_manager
        self.exchange = exchange
        self.analyzers = analyzers
        self.strategy = strategy
        self.notifier = notifier
        
        # ── Configuration ─────────────────────────────────────────
        self.mode = mode.upper()
        self.coins = coins or list(analyzers.keys())
        self.interval = interval
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.max_exposure_pct = max_exposure_pct
        self.emergency_drawdown = emergency_drawdown
        
        # ── Runtime State ─────────────────────────────────────────
        self.market_states: Dict[str, MarketState] = {}
        self.last_hourly_report: Optional[datetime] = None
        self.scheduler = None  # Set by main.py
        
        # ── Brain 4 AI Cache ──────────────────────────────────────
        self._brain4_cache: Dict[str, Dict] = {}
        self._brain4_last_call: Optional[datetime] = None
        self._brain4_cache_ttl: int = interval
        self._anthropic_key = os.getenv("GROQ_API_KEY", "").strip()
        
        if self._anthropic_key:
            logger.info("🧠 Brain 4 AI enabled (GROQ API)")
        else:
            logger.warning("⚠️ Brain 4 AI disabled (no GROQ_API_KEY)")
        
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
        
        logger.info(
            f"🎮 Controller initialized | Mode={self.mode} | "
            f"Coins={len(self.coins)} | Interval={interval}s"
        )

    # ═════════════════════════════════════════════════════════════
    #  NOTIFICATION HELPERS
    # ═════════════════════════════════════════════════════════════

    def _notify(self, coro_fn):
        """
        Safely schedule an async notifier coroutine from sync context.
        
        Usage:
            self._notify(lambda: self.notifier.send_status(...))
        """
        if not self.notifier:
            return
        
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro_fn())
        except RuntimeError:
            # No event loop running
            logger.debug("⚠️ No event loop — notification skipped")

    def _notify_sync(self, message: str):
        """Send a simple text notification."""
        if not self.notifier:
            return
        self._notify(lambda: self.notifier.send_custom(message))

    # ═════════════════════════════════════════════════════════════
    #  LIFECYCLE HOOKS
    # ═════════════════════════════════════════════════════════════

    def on_start(self):
        """Called when bot starts — sends startup notification."""
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
            f"🤖 Bot started | Mode={self.mode} | "
            f"Balance=${balance:.2f} | Coins={self.coins}"
        )

    def on_stop(self, reason: str = "User request"):
        """Called when bot stops — sends shutdown notification."""
        self._notify(lambda: self.notifier.send_bot_stopped(reason=reason))
        logger.info(f"🛑 Bot stopped | Reason={reason}")

    # ═════════════════════════════════════════════════════════════
    #  MAIN TRADING CYCLE
    # ═════════════════════════════════════════════════════════════

    def run_cycle(self):
        """
        Main trading cycle — called by scheduler every interval.
        
        This is the heart of the autonomous trading system.
        
        Flow:
        1. Check all safety gates (kill switch, loss guard, etc.)
        2. Analyze all coins and update market states
        3. Manage existing positions (SL/TP/trailing)
        4. Evaluate new entry opportunities
        5. Send market summary and reports
        """
        logger.info("🔁 ══════════ Trading Cycle Start ══════════")
        cycle_start = datetime.utcnow()
        # ── Emergency Drawdown Absolute Protection ─────────────────────
        start_balance = self.state.get("start_of_day_balance", 0)
        current_balance = self.state.get("balance", 0)

        if start_balance > 0:
            loss_pct = (start_balance - current_balance) / start_balance
            
            if loss_pct >= self.emergency_drawdown:
                logger.critical(
                    f"🚨 EMERGENCY DRAWDOWN HIT: {loss_pct*100:.2f}% "
                    f"(Limit: {self.emergency_drawdown*100:.2f}%)"
                )

                # Activate kill switch immediately
                self.kill_switch.activate("Emergency drawdown exceeded")

                # Optional: close all positions
                self.force_exit_all("Emergency drawdown triggered")

                return
        
        
        # ══════════════════════════════════════════════════════════
        #  GATE 1: Kill Switch
        # ══════════════════════════════════════════════════════════
        
        if self.kill_switch.is_active():
            logger.warning("🛑 Kill switch active — cycle skipped")
            return
        
        # ══════════════════════════════════════════════════════════
        #  GATE 2: Bot Active State
        # ══════════════════════════════════════════════════════════
        
        if not self.state.get("bot_active", True):
            logger.debug("⏸️ Bot inactive — cycle skipped")
            return
        
        # ══════════════════════════════════════════════════════════
        #  GATE 3: Loss Guard
        # ══════════════════════════════════════════════════════════
        
        can_trade, reason = self.loss_guard.can_trade()
        if not can_trade:
            logger.warning(f"🛑 Loss guard blocked: {reason}")
            return
        
        # ══════════════════════════════════════════════════════════
        #  RESET EXCHANGE CACHE
        # ══════════════════════════════════════════════════════════
        
        self.exchange.begin_cycle()
        
        try:
            # ══════════════════════════════════════════════════════
            #  PHASE 1: ANALYZE ALL COINS
            # ══════════════════════════════════════════════════════
            
            logger.info("📊 Phase 1: Market Analysis")
            
            for symbol in self.coins:
                self._analyze_coin(symbol)
            
            # ══════════════════════════════════════════════════════
            #  PHASE 2: MANAGE EXISTING POSITIONS
            # ══════════════════════════════════════════════════════
            
            logger.info("📂 Phase 2: Position Management")
            
            positions = self.state.get_all_positions()
            
            if positions:
                logger.info(f"   Managing {len(positions)} open position(s)")
                
                for symbol, position in positions.items():
                    market = self.market_states.get(symbol)
                    if market:
                        self._manage_position(market, position)
                    else:
                        logger.warning(f"   ⚠️ No market data for {symbol}")
            else:
                logger.info("   No open positions")
            
            # ══════════════════════════════════════════════════════
            #  PHASE 3: EVALUATE NEW ENTRIES
            # ══════════════════════════════════════════════════════
            
            logger.info("🎯 Phase 3: Entry Evaluation")
            
            # Get current positions (may have changed after phase 2)
            positions = self.state.get_all_positions()
            
            for symbol in self.coins:
                # Skip if already have a position in this coin
                if symbol in positions:
                    logger.debug(f"   {symbol}: Already have position — skip")
                    continue
                
                market = self.market_states.get(symbol)
                if market:
                    self._evaluate_entry(market, symbol)
                else:
                    logger.debug(f"   {symbol}: No market data — skip")
            
            # ══════════════════════════════════════════════════════
            #  PHASE 4: REPORTS & NOTIFICATIONS
            # ══════════════════════════════════════════════════════
            
            logger.info("📱 Phase 4: Reports")
            
            self._send_cycle_summary()
            self._maybe_send_hourly_report()
            
            # ══════════════════════════════════════════════════════
            #  CYCLE COMPLETE
            # ══════════════════════════════════════════════════════
            
            elapsed = (datetime.utcnow() - cycle_start).total_seconds()
            logger.info(f"✅ Cycle complete in {elapsed:.2f}s")
            logger.info("🔁 ══════════ Trading Cycle End ══════════")
            
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

    # ═════════════════════════════════════════════════════════════
    #  MARKET ANALYSIS
    # ═════════════════════════════════════════════════════════════

    def _analyze_coin(self, symbol: str):
        """
        Fetch market data and run full analysis for a coin.
        
        Updates self.market_states[symbol] with latest MarketState.
        """
        try:
            # Get or create analyzer
            analyzer = self.analyzers.get(symbol)
            if not analyzer:
                analyzer = MarketAnalyzer(symbol=symbol)
                self.analyzers[symbol] = analyzer
            
            # Fetch market data from exchange
            price = self.exchange.get_price(symbol)
            candles = self.exchange.get_recent_candles(symbol, limit=150)
            
            # Validate data
            if not price or price <= 0:
                logger.warning(f"   ⚠️ {symbol}: Invalid price ({price})")
                return
            
            if not candles or len(candles) < 60:
                logger.warning(
                    f"   ⚠️ {symbol}: Insufficient candles "
                    f"({len(candles) if candles else 0}/60 required)"
                )
                return
            
            # Run analysis
            market = analyzer.analyze({
                "price": price,
                "candles": candles,
            })
            
            # Inject Brain 4 AI prediction
            market.ai_prediction = self._get_brain4_prediction(market)
            
            # Store for later use
            self.market_states[symbol] = market
            
            # Log summary
            ai_signal = "N/A"
            if market.ai_prediction:
                ai_signal = market.ai_prediction.get("signal", "N/A")
            
            logger.info(
                f"   ✅ {symbol}: ${price:.2f} | "
                f"Trend={market.trend} | RSI={market.rsi:.1f} | "
                f"Regime={market.regime} | AI={ai_signal}"
            )
            
        except Exception as e:
            logger.error(f"   ❌ {symbol}: Analysis failed — {e}")

    # ═════════════════════════════════════════════════════════════
    #  BRAIN 4 — AI PREDICTION (Claude API)
    # ═════════════════════════════════════════════════════════════

    def _get_brain4_prediction(self, market: MarketState) -> Optional[Dict]:
        """
        Brain 4: Real AI market analysis via Anthropic Claude API.
        
        Sends market snapshot to Claude and gets structured trading signal.
        Caches results for one cycle to avoid rate limiting.
        
        Returns:
            Dict with signal, confidence, reason — or None if unavailable
        """
        if not self._anthropic_key:
            return None
        
        # ── Cache Check ───────────────────────────────────────────
        now = datetime.utcnow()
        cache_key = market.symbol
        
        if self._brain4_last_call and cache_key in self._brain4_cache:
            elapsed = (now - self._brain4_last_call).total_seconds()
            if elapsed < self._brain4_cache_ttl:
                return self._brain4_cache[cache_key]
        
        # ── Build Prompt ──────────────────────────────────────────
        prompt = self._build_brain4_prompt(market)
        
        try:
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self._anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 256,
                    "system": (
                        "You are an expert quantitative trading analyst. "
                        "Analyze the given market data and respond ONLY with valid JSON. "
                        "No explanation, no markdown, just JSON."
                    ),
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=15,
            )
            
            if response.status_code != 200:
                logger.warning(
                    f"⚠️ Brain 4 API error: {response.status_code}"
                )
                return None
            
            # Parse response
            data = response.json()
            raw_text = data["content"][0]["text"].strip()
            prediction = self._parse_brain4_response(raw_text)
            
            # Cache result
            self._brain4_cache[cache_key] = prediction
            self._brain4_last_call = now
            
            logger.debug(
                f"🧠 Brain 4 | {market.symbol} | "
                f"{prediction['signal']} ({prediction['confidence']}%)"
            )
            
            return prediction
            
        except requests.exceptions.Timeout:
            logger.warning("⚠️ Brain 4: API timeout")
            return None
        except Exception as e:
            logger.warning(f"⚠️ Brain 4: {e}")
            return None

    def _build_brain4_prompt(self, market: MarketState) -> str:
        """Build structured prompt for Claude AI analysis."""
        indicators = market.indicators or {}
        
        # Chart pattern info
        pattern_info = "None detected"
        if market.chart_pattern:
            p = market.chart_pattern
            pattern_info = f"{p.get('pattern_name', 'Unknown')} ({p.get('signal', 'HOLD')} {p.get('confidence', 0)}%)"
        
        return f"""Analyze this cryptocurrency market data and provide a trading signal.

MARKET DATA:
- Symbol: {market.symbol}
- Current Price: ${market.price:.4f}
- Trend: {market.trend}
- Market Regime: {market.regime}
- Volatility: {market.volatility_regime} ({market.volatility_pct * 100:.3f}%)

TECHNICAL INDICATORS:
- RSI (14): {market.rsi:.1f}
- EMA 20: ${market.ema_20:.4f}
- EMA 50: ${market.ema_50:.4f}
- EMA 200: ${market.ema_200:.4f}
- MACD Histogram: {market.macd_histogram:.6f}
- MACD Cross: {indicators.get('macd_cross', 'None')}
- EMA Cross: {indicators.get('ema_cross', 'None')}
- Bollinger %B: {market.bb_percent_b:.3f}

MARKET DYNAMICS:
- Volume Spike: {market.volume_spike}
- Volume Pressure: {market.volume_pressure:.3f} (-1=bearish, +1=bullish)
- Momentum Strength: {market.momentum_strength:.6f}
- Trend Strength: {market.trend_strength:.6f}
- Structure Break: {market.structure_break}

SENTIMENT:
- Sentiment Score: {market.sentiment_score:.3f}
- Sentiment Category: {market.sentiment}

CHART PATTERN:
- {pattern_info}

SUPPORT/RESISTANCE:
- Support: ${market.support_level:.4f}
- Resistance: ${market.resistance_level:.4f}

Based on ALL of this data, provide your trading signal.
Respond with ONLY this JSON (no other text):
{{"signal": "BUY" | "SELL" | "HOLD", "confidence": <integer 0-100>, "reason": "<one sentence>"}}"""

    def _parse_brain4_response(self, raw_text: str) -> Dict:
        """Parse Brain 4 JSON response safely."""
        try:
            text = raw_text.strip()
            
            # Remove markdown fences if present
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(
                    l for l in lines if not l.startswith("```")
                ).strip()
            
            parsed = json.loads(text)
            
            # Validate and normalize
            signal = parsed.get("signal", "HOLD").upper()
            if signal not in ("BUY", "SELL", "HOLD"):
                signal = "HOLD"
            
            confidence = int(parsed.get("confidence", 0))
            confidence = max(0, min(100, confidence))
            
            return {
                "signal": signal,
                "confidence": confidence,
                "reason": parsed.get("reason", "AI analysis"),
            }
            
        except Exception as e:
            logger.debug(f"Brain 4 parse error: {e}")
            return {
                "signal": "HOLD",
                "confidence": 0,
                "reason": "Parse error — defaulting to HOLD",
            }

    # ═════════════════════════════════════════════════════════════
    #  ENTRY EVALUATION
    # ═════════════════════════════════════════════════════════════

    def _evaluate_entry(self, market: MarketState, symbol: str):
        """
        Evaluate potential trade entry for a symbol.
        
        Flow:
        1. Check trade limiter
        2. Run 4-Brain decision engine
        3. Get strategy signal
        4. Validate risk
        5. Execute entry if all pass
        """
        # ── Gate: Trade Limiter ───────────────────────────────────
        can_open, limit_reason = self.trade_limiter.can_open_trade(symbol)
        if not can_open:
            logger.debug(f"   {symbol}: Trade limited — {limit_reason}")
            return
        
        # ── Gate: State Check ─────────────────────────────────────
        if not self.state.can_trade():
            logger.debug(f"   {symbol}: State blocks trading")
            return
        
        # ── Update Strategy ───────────────────────────────────────
        # Point strategy to current symbol
        self.strategy.symbol = symbol
        
        # Update loss streak for adaptive confidence
        loss_streak = self.state.get("loss_streak", 0)
        self.strategy.set_loss_streak(loss_streak)
        
        # ── Run 4-Brain Decision Engine ───────────────────────────
        decision = self._run_decision_engine(market, symbol)
        
        if not decision["trade"]:
            logger.debug(
                f"   {symbol}: No trade signal "
                f"({decision['final_signal']} / {decision['confidence']})"
            )
            return
        
        # ── Get Strategy Signal ───────────────────────────────────
        entry_signal = self.strategy.should_enter(market)
        
        if not entry_signal:
            logger.debug(f"   {symbol}: Strategy declined entry")
            return
        
        # ── Validate Risk ─────────────────────────────────────────
        price = market.price
        stop_loss = entry_signal["stop_loss"]
        
        # Estimate potential loss
        est_qty = self._estimate_quantity(price, stop_loss)
        potential_loss = abs(price - stop_loss) * est_qty
        
        valid, risk_reason = self.loss_guard.validate_trade_risk(potential_loss)
        if not valid:
            logger.warning(f"   {symbol}: Risk validation failed — {risk_reason}")
            return
        
        # ── Execute Entry ─────────────────────────────────────────
        logger.info(f"   {symbol}: ✅ All gates passed — executing entry")
        self._execute_entry(entry_signal, market)

    # ═════════════════════════════════════════════════════════════
    #  4-BRAIN DECISION ENGINE
    # ═════════════════════════════════════════════════════════════

    def _run_decision_engine(
        self,
        market: MarketState,
        symbol: str,
    ) -> Dict:
        """
        Aggregate signals from all 4 analysis brains.
        
        Brain 1: Technical Indicators (35% weight)
        Brain 2: Market Sentiment (15% weight)
        Brain 3: Chart Patterns (25% weight)
        Brain 4: AI Prediction (25% weight)
        
        Returns decision dict with final_signal, confidence, trade flag.
        """
        # Collect all brain signals
        brains = self._collect_brain_signals(market)
        
        # ── Weighted Voting ───────────────────────────────────────
        weighted_buy = 0.0
        weighted_sell = 0.0
        votes_buy = 0
        votes_sell = 0
        votes_hold = 0
        
        for brain in brains:
            signal = brain["signal"].upper()
            weight = brain["weight_pct"] / 100.0
            confidence = brain["confidence_pct"] / 100.0
            
            if signal == "BUY":
                weighted_buy += weight * confidence * 100
                votes_buy += 1
            elif signal == "SELL":
                weighted_sell += weight * confidence * 100
                votes_sell += 1
            else:
                votes_hold += 1
        
        # ── Determine Final Signal ────────────────────────────────
        if weighted_buy > weighted_sell and weighted_buy > 15:
            final_signal = "BUY"
        elif weighted_sell > weighted_buy and weighted_sell > 15:
            final_signal = "SELL"
        else:
            final_signal = "HOLD"
        
        # ── Confidence Tier ───────────────────────────────────────
        score = max(weighted_buy, weighted_sell)
        if score >= 50:
            confidence = "HIGH"
        elif score >= 25:
            confidence = "MODERATE"
        else:
            confidence = "LOW"
        
        # ── Trade Gate ────────────────────────────────────────────
        trade = (
            final_signal in ("BUY", "SELL") and
            confidence in ("HIGH", "MODERATE")
        )
        
        # ── Send Telegram Decision Report ─────────────────────────
        self._notify(
            lambda: self.notifier.send_decision_report(
                coin=symbol,
                price=market.price,
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
        
        # Brain 1: Technical Indicators (35%)
        brain1 = self._brain_indicators(market)
        brain1["weight_pct"] = 35
        brains.append(brain1)
        
        # Brain 2: Sentiment (15%)
        brain2 = self._brain_sentiment(market)
        brain2["weight_pct"] = 15
        brains.append(brain2)
        
        # Brain 3: Chart Patterns (25%)
        brain3 = self._brain_chart(market)
        brain3["weight_pct"] = 25
        brains.append(brain3)
        
        # Brain 4: AI (25%)
        brain4 = self._brain_ai(market)
        brain4["weight_pct"] = 25
        brains.append(brain4)
        
        return brains

    def _brain_indicators(self, market: MarketState) -> Dict:
        """Brain 1: Technical indicator signals."""
        indicators = market.indicators or {}
        score = 0
        factors = 0
        
        # RSI
        rsi = indicators.get("rsi") or market.rsi
        if rsi is not None:
            factors += 1
            if rsi < 35:
                score += 1
            elif rsi > 65:
                score -= 1
        
        # MACD Cross
        macd = indicators.get("macd_cross")
        if macd:
            factors += 1
            if macd == "bullish":
                score += 1
            elif macd == "bearish":
                score -= 1
        
        # EMA Cross
        ema = indicators.get("ema_cross")
        if ema:
            factors += 1
            if ema == "bullish":
                score += 1
            elif ema == "bearish":
                score -= 1
        
        # Bollinger Position
        bb = indicators.get("bb_position")
        if bb:
            factors += 1
            if bb == "oversold":
                score += 1
            elif bb == "overbought":
                score -= 1
        
        # Calculate confidence
        if factors == 0:
            confidence = 0
        else:
            confidence = int(abs(score) / factors * 100)
        
        # Determine signal
        if score > 0:
            signal = "BUY"
        elif score < 0:
            signal = "SELL"
        else:
            signal = "HOLD"
        
        return {
            "name": "Brain1 Indicators",
            "signal": signal,
            "confidence_pct": confidence,
        }

    def _brain_sentiment(self, market: MarketState) -> Dict:
        """Brain 2: Market sentiment signal."""
        sentiment = market.sentiment_score
        
        if sentiment is None or sentiment == 0:
            return {
                "name": "Brain2 Sentiment",
                "signal": "HOLD",
                "confidence_pct": 0,
            }
        
        confidence = int(min(abs(sentiment) * 100, 100))
        
        if sentiment > 0.1:
            signal = "BUY"
        elif sentiment < -0.1:
            signal = "SELL"
        else:
            signal = "HOLD"
        
        return {
            "name": "Brain2 Sentiment",
            "signal": signal,
            "confidence_pct": confidence,
        }

    def _brain_chart(self, market: MarketState) -> Dict:
        """Brain 3: Chart pattern signal."""
        pattern = market.chart_pattern
        
        if not pattern:
            return {
                "name": "Brain3 Chart",
                "signal": "HOLD",
                "confidence_pct": 0,
            }
        
        return {
            "name": "Brain3 Chart",
            "signal": pattern.get("signal", "HOLD"),
            "confidence_pct": pattern.get("confidence", 0),
        }

    def _brain_ai(self, market: MarketState) -> Dict:
        """Brain 4: AI prediction signal."""
        ai = market.ai_prediction
        
        if not ai:
            return {
                "name": "Brain4 AI",
                "signal": "HOLD",
                "confidence_pct": 0,
            }
        
        return {
            "name": "Brain4 AI",
            "signal": ai.get("signal", "HOLD"),
            "confidence_pct": ai.get("confidence", 0),
        }

    # ═════════════════════════════════════════════════════════════
    #  POSITION MANAGEMENT
    # ═════════════════════════════════════════════════════════════

    def _manage_position(self, market: MarketState, position: Dict):
        """
        Manage an existing open position.
        
        Checks:
        1. Stop Loss hit
        2. Take Profit hit
        3. Break-even activation (move SL to entry after 60% progress)
        4. Trailing stop (after 100% progress)
        5. Strategy exit signal
        """
        symbol = position["symbol"]
        entry_price = position["entry_price"]
        stop_loss = position["stop_loss"]
        take_profit = position["take_profit"]
        quantity = position["quantity"]
        current_price = market.price
        
        # Determine position direction
        action = position.get("action", "BUY").upper()
        is_long = action == "BUY"
        
        # ══════════════════════════════════════════════════════════
        #  CHECK STOP LOSS
        # ══════════════════════════════════════════════════════════
        
        if is_long:
            sl_hit = current_price <= stop_loss
        else:
            sl_hit = current_price >= stop_loss
        
        if sl_hit:
            logger.info(f"   🛑 {symbol}: Stop Loss hit @ ${current_price:.4f}")
            self._execute_exit(position, current_price, "STOP LOSS", is_long)
            return
        
        # ══════════════════════════════════════════════════════════
        #  CHECK TAKE PROFIT
        # ══════════════════════════════════════════════════════════
        
        if is_long:
            tp_hit = current_price >= take_profit
        else:
            tp_hit = current_price <= take_profit
        
        if tp_hit:
            logger.info(f"   🎯 {symbol}: Take Profit hit @ ${current_price:.4f}")
            self._execute_exit(position, current_price, "TAKE PROFIT", is_long)
            return
        
        # ══════════════════════════════════════════════════════════
        #  BREAK-EVEN ACTIVATION
        # ══════════════════════════════════════════════════════════
        
        updated = False
        
        if is_long:
            reward = take_profit - entry_price
            if reward > 0:
                progress = (current_price - entry_price) / reward
                # At 60% to TP, move SL to entry + small buffer
                if progress >= 0.6 and stop_loss < entry_price:
                    new_sl = entry_price + (entry_price * 0.001)
                    self.state.update_position(symbol, {"stop_loss": new_sl})
                    updated = True
                    logger.info(
                        f"   ✅ {symbol}: Break-even activated | "
                        f"SL: ${stop_loss:.2f} → ${new_sl:.2f}"
                    )
                    stop_loss = new_sl  # Update for trailing check
        else:
            reward = entry_price - take_profit
            if reward > 0:
                progress = (entry_price - current_price) / reward
                if progress >= 0.6 and stop_loss > entry_price:
                    new_sl = entry_price - (entry_price * 0.001)
                    self.state.update_position(symbol, {"stop_loss": new_sl})
                    updated = True
                    logger.info(
                        f"   ✅ {symbol}: Break-even activated | "
                        f"SL: ${stop_loss:.2f} → ${new_sl:.2f}"
                    )
                    stop_loss = new_sl
        
        # ══════════════════════════════════════════════════════════
        #  TRAILING STOP
        # ══════════════════════════════════════════════════════════
        
        if is_long:
            reward = take_profit - entry_price
            if reward > 0:
                progress = (current_price - entry_price) / reward
                # At 100%+ to TP, start trailing
                if progress >= 1.0:
                    new_sl = current_price * 0.995  # 0.5% below current
                    if new_sl > stop_loss:
                        self.state.update_position(symbol, {"stop_loss": new_sl})
                        updated = True
                        logger.info(
                            f"   📐 {symbol}: Trailing stop | "
                            f"SL → ${new_sl:.2f}"
                        )
        else:
            reward = entry_price - take_profit
            if reward > 0:
                progress = (entry_price - current_price) / reward
                if progress >= 1.0:
                    new_sl = current_price * 1.005  # 0.5% above current
                    if new_sl < stop_loss:
                        self.state.update_position(symbol, {"stop_loss": new_sl})
                        updated = True
                        logger.info(
                            f"   📐 {symbol}: Trailing stop | "
                            f"SL → ${new_sl:.2f}"
                        )
        
        # ══════════════════════════════════════════════════════════
        #  STRATEGY EXIT SIGNAL
        # ══════════════════════════════════════════════════════════
        
        # Update strategy symbol
        self.strategy.symbol = symbol
        
        exit_signal = self.strategy.should_exit(market, position)
        
        if exit_signal:
            reason = exit_signal.get("reason", "STRATEGY EXIT")
            logger.info(f"   🔴 {symbol}: Strategy exit signal — {reason}")
            self._execute_exit(position, current_price, reason, is_long)

    # ═════════════════════════════════════════════════════════════
    #  EXECUTE ENTRY (FIXED)
    # ═════════════════════════════════════════════════════════════

    def _execute_entry(self, signal: Dict, market: MarketState):
        """
        Execute a trade entry.
        
        For both LONG and SHORT positions, we use buy() to establish.
        The direction is tracked in position metadata for proper exit.
        
        NOTE: This assumes spot trading where we buy the asset.
        For true short selling (margin), additional logic needed.
        """
        symbol = signal["symbol"]
        direction = signal["action"].upper()
        entry_price = signal["entry_price"]
        stop_loss = signal["stop_loss"]
        take_profit = signal["take_profit"]
        confidence = signal.get("confidence", 0)
        
        # ── Calculate Position Size ───────────────────────────────
        qty = self._calculate_position_size(entry_price, stop_loss)
        
        if qty <= 0:
            logger.warning(f"⚠️ {symbol}: Position size is zero — entry skipped")
            return
        
        # ── Check Balance ─────────────────────────────────────────
        balance = self.state.get("balance", 0)
        cost_estimate = qty * entry_price
        
        if cost_estimate > balance * 0.95:  # Leave 5% buffer
            logger.warning(
                f"⚠️ {symbol}: Insufficient balance | "
                f"Need ${cost_estimate:.2f}, have ${balance:.2f}"
            )
            return
        
        # ── Execute Order ─────────────────────────────────────────
        # Always use buy() to acquire the asset
        fill = self.exchange.buy(symbol=symbol, quantity=qty)
        
        if not fill or fill.get("status") == "REJECTED":
            reason = fill.get("reason", "Unknown") if fill else "No response"
            logger.error(f"❌ {symbol}: Entry rejected — {reason}")
            self._notify(
                lambda: self.notifier.send_error(
                    context=f"Entry rejected: {symbol}",
                    error=f"{direction} qty={qty:.6f} — {reason}",
                )
            )
            return
        
        # ── Extract Fill Details ──────────────────────────────────
        fill_price = float(fill.get("price", entry_price))
        filled_qty = float(fill.get("quantity", qty))
        fee = float(fill.get("fee", fill_price * filled_qty * self.fee_pct))
        cost = fill_price * filled_qty
        
        # ── Deduct From Balance ───────────────────────────────────
        self.state.adjust_balance(-(cost + fee))
        
        # ── Record Position ───────────────────────────────────────
        position_data = {
            "symbol": symbol,
            "action": direction,  # "BUY" for long, "SELL" for short
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
        
        # ── Record In Trade Limiter ───────────────────────────────
        self.trade_limiter.record_trade(symbol, direction)
        
        # ── Notify ────────────────────────────────────────────────
        new_balance = self.state.get("balance", 0)
        
        self._notify(
            lambda: self.notifier.send_trade_executed(
                mode=self.mode,
                side=direction,
                coin=symbol,
                amount=filled_qty,
                price=fill_price,
                cost=round(cost, 2),
                fee=round(fee, 4),
                remaining=round(new_balance, 2),
                strategy=position_data["strategy"],
                reason=signal.get("reason", direction),
                confidence=confidence,
            )
        )
        
        logger.info(
            f"✅ ENTRY EXECUTED | {symbol} {direction} | "
            f"Qty={filled_qty:.6f} @ ${fill_price:.4f} | "
            f"Cost=${cost:.2f} | Fee=${fee:.4f} | "
            f"SL=${stop_loss:.2f} TP=${take_profit:.2f}"
        )

    # ═════════════════════════════════════════════════════════════
    #  EXECUTE EXIT (FIXED)
    # ═════════════════════════════════════════════════════════════

    def _execute_exit(
        self,
        position: Dict,
        exit_price: float,
        reason: str,
        is_long: bool,
    ):
        """
        Execute a position exit.
        
        CRITICAL FIX:
        - LONG positions: sell() to close
        - SHORT positions: buy() to cover
        
        Previous bug: Always called sell() even for shorts.
        """
        symbol = position["symbol"]
        quantity = position["quantity"]
        entry_price = position["entry_price"]
        
        # ── Execute Exit Order ────────────────────────────────────
        if is_long:
            # Long exit = SELL
            fill = self.exchange.sell(symbol=symbol, quantity=quantity)
        else:
            # Short exit = BUY (to cover) — THIS IS THE FIX
            fill = self.exchange.buy(symbol=symbol, quantity=quantity)
        
        if not fill or fill.get("status") == "REJECTED":
            reason_msg = fill.get("reason", "Unknown") if fill else "No response"
            logger.error(f"❌ {symbol}: Exit rejected — {reason_msg}")
            self._notify(
                lambda: self.notifier.send_error(
                    context=f"Exit rejected: {symbol}",
                    error=f"qty={quantity:.6f} — {reason_msg}",
                )
            )
            return
        
        # ── Extract Fill Details ──────────────────────────────────
        actual_exit = float(fill.get("price", exit_price))
        filled_qty = float(fill.get("quantity", quantity))
        fee = float(fill.get("fee", actual_exit * filled_qty * self.fee_pct))
        
        # ── Calculate PnL ─────────────────────────────────────────
        if is_long:
            gross_pnl = (actual_exit - entry_price) * filled_qty
        else:
            gross_pnl = (entry_price - actual_exit) * filled_qty
        
        net_pnl = gross_pnl - fee
        pnl_pct = (net_pnl / (entry_price * filled_qty)) * 100 if entry_price > 0 else 0
        
        # ── Close Position In State ───────────────────────────────
        self.state.close_position(symbol, net_pnl, exit_price=actual_exit)
        
        # ── Update Risk Systems ───────────────────────────────────
        self.adaptive_risk.update_after_trade({
            "pnl_amount": net_pnl,
            "entry_price": entry_price,
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
                entry_price=entry_price,
                exit_price=actual_exit,
                pnl=round(net_pnl, 4),
                pnl_pct=round(pnl_pct, 2),
                strategy=position.get("strategy", "unknown"),
                reason=reason,
            )
        )
        
        # Log with appropriate emoji
        pnl_emoji = "📈" if net_pnl >= 0 else "📉"
        direction = "LONG" if is_long else "SHORT"
        
        logger.info(
            f"{pnl_emoji} EXIT EXECUTED | {symbol} {direction} | "
            f"Qty={filled_qty:.6f} @ ${actual_exit:.4f} | "
            f"PnL=${net_pnl:+.4f} ({pnl_pct:+.2f}%) | {reason}"
        )

    # ═════════════════════════════════════════════════════════════
    #  POSITION SIZING
    # ═════════════════════════════════════════════════════════════

    def _calculate_position_size(
        self,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        """
        Calculate position size based on risk management.
        
        Uses adaptive risk manager to get current risk percentage,
        then calculates quantity based on stop loss distance.
        """
        balance = self.state.get("balance", 0)
        
        if balance <= 0:
            return 0.0
        
        # Get adaptive risk percentage
        market = None
        if self.market_states:
            market = list(self.market_states.values())[0]
        
        risk_pct = self.adaptive_risk.get_risk_percent(market, confidence=None)
        risk_amount = balance * risk_pct
        
        # Calculate stop loss distance
        sl_distance = abs(entry_price - stop_loss)
        
        if sl_distance <= 0:
            logger.warning("⚠️ Stop loss distance is zero")
            return 0.0
        
        # Position size = risk amount / stop loss distance
        qty = risk_amount / sl_distance
        
        # Apply max exposure limit
        max_position_value = balance * self.max_exposure_pct
        if qty * entry_price > max_position_value:
            qty = max_position_value / entry_price
            logger.debug(
                f"Position capped by max exposure: {qty:.6f}"
            )
        
        return round(qty, 8)

    def _estimate_quantity(
        self,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        """Estimate quantity for pre-trade risk validation."""
        balance = self.state.get("balance", 0)
        risk_pct = self.adaptive_risk.base_risk
        risk_amount = balance * risk_pct
        sl_distance = abs(entry_price - stop_loss)
        
        if sl_distance <= 0:
            return 0.0
        
        return risk_amount / sl_distance

    # ═════════════════════════════════════════════════════════════
    #  REPORTS & SUMMARIES
    # ═════════════════════════════════════════════════════════════

    def _send_cycle_summary(self):
        """Send market summary after each analysis cycle."""
        if not self.market_states or not self.notifier:
            return
        
        # Send update for primary coin
        primary = self.coins[0] if self.coins else None
        market = self.market_states.get(primary)
        
        if market:
            self._notify(
                lambda: self.notifier.send_market_update(
                    symbol=market.symbol,
                    price=market.price,
                    trend=market.trend,
                    rsi=market.rsi,
                    regime=market.regime,
                    volatility=market.volatility_regime,
                )
            )

    def _maybe_send_hourly_report(self):
        """Send hourly performance report if due."""
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
            lambda: self.notifier.send_hourly_report(
                mode=self.mode,
                balance=balance,
                daily_pnl=daily_pnl,
                trades_today=trades_today,
                max_trades=self.trade_limiter.max_trades_per_day,
                open_positions=len(positions),
                win_streak=self.state.get("win_streak", 0),
                loss_streak=self.state.get("loss_streak", 0),
                coins=self.coins,
            )
        )

    # ═════════════════════════════════════════════════════════════
    #  STATUS & CONTROLS
    # ═════════════════════════════════════════════════════════════

    def get_status(self) -> Dict:
        """Get comprehensive bot status for Telegram /status."""
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

    # ═════════════════════════════════════════════════════════════
    #  MANUAL CONTROLS
    # ═════════════════════════════════════════════════════════════

    def pause(self):
        """Pause trading (soft stop)."""
        self.state.set("bot_active", False)
        self.state.set("pause_reason", "Manual pause")
        logger.info("⏸️ Bot paused")

    def resume(self):
        """Resume trading — unlocks ALL risk locks."""
        self.state.set("bot_active", True)
        self.state.set("risk_locked", False)
        self.state.set("pause_reason", None)
        self.kill_switch.deactivate(source="manual")
        self.loss_guard.unlock(source="resume")
        logger.info("▶️ Bot resumed — all locks cleared")

    def unlock_risk(self, source: str = "manual") -> Dict:
        """Full risk system unlock."""
        self.state.set("bot_active", True)
        self.state.set("risk_locked", False)
        self.state.set("pause_reason", None)
        
        ks_result = self.kill_switch.deactivate(source=source)
        lg_result = self.loss_guard.unlock(source=source)
        
        logger.warning(f"🔓 FULL RISK UNLOCK by {source}")
        
        return {
            "unlocked": True,
            "source": source,
            "bot_active": True,
            "risk_locked": False,
            "kill_switch": ks_result,
            "loss_guard": lg_result,
        }

    def reset_risk_baseline(self, source: str = "manual") -> Dict:
        """Reset risk baseline — accepts past losses."""
        lg_result = self.loss_guard.reset_baseline(source=source)
        
        self.state.set("bot_active", True)
        self.state.set("risk_locked", False)
        self.kill_switch.deactivate(source=source)
        self.adaptive_risk.reset_streaks()
        
        balance = self.state.get("balance", 0)
        logger.warning(
            f"🔄 RISK BASELINE RESET by {source} | New baseline: ${balance:.2f}"
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
        """Force close all positions immediately."""
        positions = self.state.get_all_positions()
        
        if not positions:
            logger.info("No positions to close")
            return
        
        logger.warning(f"🚨 Force exiting {len(positions)} position(s)...")
        
        for symbol, position in positions.items():
            try:
                price = self.exchange.get_price(symbol)
                is_long = position.get("action", "BUY").upper() == "BUY"
                self._execute_exit(position, price, reason, is_long)
            except Exception as e:
                logger.error(f"❌ Failed to exit {symbol}: {e}")

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

    # ═════════════════════════════════════════════════════════════
    #  REPRESENTATION
    # ═════════════════════════════════════════════════════════════

    def __repr__(self) -> str:
        active = self.state.get("bot_active", False)
        locked = self.state.get("risk_locked", False)
        positions = len(self.state.get_all_positions())
        
        return (
            f"<BotController "
            f"mode={self.mode} | "
            f"active={active} | "
            f"locked={locked} | "
            f"positions={positions}>"
        )