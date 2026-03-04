# app/state/manager.py

import json
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from copy import deepcopy
from app.utils.logger import get_logger

logger = get_logger(__name__)


class StateManager:
    """
    Thread-Safe Persistent State Manager for Autonomous Trading Bot

    Provides:
    - Key-value store with JSON file persistence
    - Position tracking (open / close / partial close with PnL)
    - Balance management with audit trail
    - Trade history recording (capped at 500)
    - Daily auto-reset (PnL, trade count, cooldowns)
    - Win/Loss streak tracking with automatic updates
    - Equity curve management for performance analysis
    - Performance metrics calculation
    - Defaults loaded from defaults.json, overlaid by state.json
    - Thread-safe via threading.Lock
    - Deep-copy returns prevent external mutation

    Balance Accounting:
        Entry:  controller calls adjust_balance(-(cost + entry_fee))
        Exit:   close_position credits entry_price * qty + net_pnl
        Result: balance = original + gross_pnl - all_fees  ✓
    """

    MAX_HISTORY = 500
    MAX_EQUITY_POINTS = 1000  # Limit equity history size

    def __init__(
        self,
        state_file: Optional[str] = None,
        defaults_file: Optional[str] = None,
        initial_balance: float = 100.0,
    ):
        self._lock = threading.Lock()

        base_dir = Path(__file__).parent
        self._state_file = (
            Path(state_file) if state_file else base_dir / "state.json"
        )
        self._defaults_file = (
            Path(defaults_file) if defaults_file else base_dir / "defaults.json"
        )

        # Load defaults first, then overlay persisted state
        self._data: Dict[str, Any] = self._load_defaults()
        self._load_persisted_state()

        # Fill any missing keys with sane values
        self._ensure_critical_keys(initial_balance)

        # Reset daily counters if date rolled over
        self._reset_if_new_day()

        logger.info(
            f"✅ StateManager ready | "
            f"Balance=${self.get('balance', 0):.2f} | "
            f"Positions={len(self.get('positions', {}))} | "
            f"Active={self.get('bot_active')}"
        )

    # ═══════════════════════════════════════════════════════════════
    #  CORE GET / SET / INCREMENT / DELETE
    # ═══════════════════════════════════════════════════════════════

    def get(self, key: str, default: Any = None) -> Any:
        """
        Thread-safe key lookup.
        Returns deep copy of mutable types (dict, list).
        """
        with self._lock:
            value = self._data.get(key, default)
            if isinstance(value, (dict, list)):
                return deepcopy(value)
            return value

    def set(self, key: str, value: Any) -> None:
        """Thread-safe key write with auto-persist."""
        with self._lock:
            self._data[key] = value
            self._persist()

    def increment(self, key: str, amount: int = 1) -> int:
        """Atomic increment of a numeric key. Returns new value."""
        with self._lock:
            current = self._data.get(key, 0) or 0
            new_val = current + amount
            self._data[key] = new_val
            self._persist()
            return new_val

    def delete(self, key: str) -> None:
        """Remove a key from state."""
        with self._lock:
            self._data.pop(key, None)
            self._persist()

    def update_multiple(self, updates: Dict[str, Any]) -> None:
        """Atomic update of multiple keys at once."""
        with self._lock:
            self._data.update(updates)
            self._persist()

    # ═══════════════════════════════════════════════════════════════
    #  BALANCE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def adjust_balance(self, delta: float) -> float:
        """
        Add (positive) or subtract (negative) from balance.
        Returns the new balance.
        """
        with self._lock:
            balance = self._data.get("balance", 0.0) or 0.0
            new_balance = round(balance + delta, 8)
            self._data["balance"] = new_balance
            self._persist()

            logger.debug(
                f"💰 Balance: ${balance:.4f} → ${new_balance:.4f} "
                f"(delta={delta:+.4f})"
            )
            return new_balance

    def get_balance(self) -> float:
        """Quick balance getter."""
        with self._lock:
            return self._data.get("balance", 0.0) or 0.0

    def get_available_balance(self) -> float:
        """
        Balance minus value locked in open positions.
        Used for position sizing.
        """
        with self._lock:
            balance = self._data.get("balance", 0.0) or 0.0
            positions = self._data.get("positions", {})
            
            locked = sum(
                pos.get("entry_price", 0) * pos.get("quantity", 0)
                for pos in positions.values()
            )
            
            return max(0.0, balance - locked)

    def get_total_equity(self, current_prices: Optional[Dict[str, float]] = None) -> float:
        """
        Total equity = balance + unrealized PnL of open positions.
        If current_prices not provided, uses entry prices (no unrealized PnL).
        """
        with self._lock:
            balance = self._data.get("balance", 0.0) or 0.0
            positions = self._data.get("positions", {})
            
            if not positions:
                return balance
            
            unrealized_pnl = 0.0
            for symbol, pos in positions.items():
                qty = pos.get("quantity", 0)
                entry = pos.get("entry_price", 0)
                
                if current_prices and symbol in current_prices:
                    current = current_prices[symbol]
                    unrealized_pnl += (current - entry) * qty
            
            return round(balance + unrealized_pnl, 8)

    # ═══════════════════════════════════════════════════════════════
    #  POSITION MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def get_position(self, symbol: str) -> Optional[Dict]:
        """Get one open position by symbol."""
        with self._lock:
            pos = self._data.get("positions", {}).get(symbol)
            return deepcopy(pos) if pos else None

    def get_all_positions(self) -> Dict[str, Dict]:
        """Get all open positions keyed by symbol."""
        with self._lock:
            return deepcopy(self._data.get("positions", {}))

    def has_position(self, symbol: str) -> bool:
        """Check if position exists for symbol."""
        with self._lock:
            return symbol in self._data.get("positions", {})

    def get_position_count(self) -> int:
        """Get number of open positions."""
        with self._lock:
            return len(self._data.get("positions", {}))

    def add_position(
        self,
        symbol: str,
        quantity: float,
        entry_price: float,
        side: str = "long",
        metadata: Optional[Dict] = None,
    ) -> Dict:
        """
        Open a new position or average into existing.
        Returns the position dict.
        """
        with self._lock:
            positions = self._data.setdefault("positions", {})
            now = datetime.utcnow().isoformat()

            if symbol in positions:
                # Average into existing position
                existing = positions[symbol]
                old_qty = existing.get("quantity", 0)
                old_price = existing.get("entry_price", entry_price)

                new_qty = round(old_qty + quantity, 8)
                new_avg = (
                    round(
                        ((old_price * old_qty) + (entry_price * quantity)) / new_qty,
                        8
                    )
                    if new_qty > 0
                    else entry_price
                )

                existing["quantity"] = new_qty
                existing["entry_price"] = new_avg
                existing["avg_price"] = new_avg
                existing["updated_at"] = now
                existing["add_count"] = existing.get("add_count", 1) + 1

                if metadata:
                    protected = {"quantity", "entry_price", "avg_price", "opened_at", "symbol", "side"}
                    for k, v in metadata.items():
                        if k not in protected:
                            existing[k] = v

                logger.info(
                    f"📈 Position averaged | {symbol} | "
                    f"Qty {old_qty}→{new_qty} | Avg ${new_avg:.6f}"
                )
                position = existing
            else:
                # New position
                position = {
                    "symbol": symbol,
                    "side": side,
                    "quantity": round(quantity, 8),
                    "entry_price": round(entry_price, 8),
                    "avg_price": round(entry_price, 8),
                    "opened_at": now,
                    "updated_at": now,
                    "add_count": 1,
                    "highest_price": entry_price,
                    "lowest_price": entry_price,
                }
                if metadata:
                    position.update(metadata)
                positions[symbol] = position

                logger.info(
                    f"📈 Position opened | {symbol} {side.upper()} | "
                    f"Qty={quantity} @ ${entry_price:.6f}"
                )

            # Update trade counters
            self._data["trades_today"] = (self._data.get("trades_today", 0) or 0) + 1
            self._data["trades_done_today"] = self._data["trades_today"]
            self._data["total_trades"] = (self._data.get("total_trades", 0) or 0) + 1
            self._data["last_trade_time"] = now

            self._persist()
            return deepcopy(position)

    def close_position(
        self,
        symbol: str,
        net_pnl: float,
        exit_price: float = 0.0,
        partial_qty: Optional[float] = None,
        reason: str = "manual",
    ) -> Optional[Dict]:
        """
        Close (or partially close) a position.
        Updates streaks, balance, equity history.
        Returns trade record or None.
        """
        with self._lock:
            positions = self._data.get("positions", {})
            position = positions.get(symbol)

            if not position:
                logger.warning(f"⚠️ close_position: no position for {symbol}")
                return None

            pos_qty = position.get("quantity", 0)
            closed_qty = min(partial_qty or pos_qty, pos_qty)
            remaining = round(pos_qty - closed_qty, 8)
            entry_price = position.get("entry_price", position.get("avg_price", 0))
            now = datetime.utcnow().isoformat()

            # Credit balance
            credit = round(entry_price * closed_qty + net_pnl, 8)
            balance = self._data.get("balance", 0.0) or 0.0
            new_balance = round(balance + credit, 8)
            self._data["balance"] = new_balance

            # Update daily PnL
            daily_pnl = self._data.get("daily_pnl", 0.0) or 0.0
            self._data["daily_pnl"] = round(daily_pnl + net_pnl, 8)

            # Update win/loss streaks
            self._update_streaks(net_pnl)

            # Record equity point
            self._record_equity_point(new_balance, net_pnl)

            # Calculate trade metrics
            pnl_percent = round((net_pnl / (entry_price * closed_qty)) * 100, 2) if entry_price * closed_qty > 0 else 0
            hold_duration = self._calculate_hold_duration(position.get("opened_at", now))

            # Create trade record
            trade_record = {
                "symbol": symbol,
                "side": position.get("side", "long"),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": closed_qty,
                "pnl_amount": round(net_pnl, 8),
                "pnl_percent": pnl_percent,
                "closed_at": now,
                "opened_at": position.get("opened_at", ""),
                "hold_duration_minutes": hold_duration,
                "strategy": position.get("strategy", "unknown"),
                "reason": reason,
                "mode": position.get("mode", "PAPER"),
                "confidence": position.get("confidence", 0),
            }

            # Add to history
            history: list = self._data.get("trade_history") or []
            history.append(trade_record)
            if len(history) > self.MAX_HISTORY:
                history = history[-self.MAX_HISTORY:]
            self._data["trade_history"] = history

            # Remove or reduce position
            if remaining <= 1e-8:
                del positions[symbol]
                log_msg = f"📉 Position closed | {symbol} | PnL=${net_pnl:+.4f} ({pnl_percent:+.2f}%) | Balance=${new_balance:.2f}"
            else:
                position["quantity"] = remaining
                position["updated_at"] = now
                log_msg = f"📉 Partial close | {symbol} | Closed={closed_qty} Remaining={remaining} | PnL=${net_pnl:+.4f}"

            logger.info(log_msg)
            self._persist()
            return trade_record

    def update_position(self, symbol: str, updates: Dict) -> bool:
        """Update fields on an open position."""
        with self._lock:
            positions = self._data.get("positions", {})
            if symbol not in positions:
                return False

            positions[symbol].update(updates)
            positions[symbol]["updated_at"] = datetime.utcnow().isoformat()
            self._persist()

            logger.debug(f"✏️ Position updated | {symbol} | Fields: {list(updates.keys())}")
            return True

    def update_position_extremes(self, symbol: str, current_price: float) -> None:
        """Track highest/lowest price for trailing stops."""
        with self._lock:
            positions = self._data.get("positions", {})
            if symbol not in positions:
                return

            pos = positions[symbol]
            changed = False

            if current_price > pos.get("highest_price", 0):
                pos["highest_price"] = current_price
                changed = True

            if current_price < pos.get("lowest_price", float('inf')):
                pos["lowest_price"] = current_price
                changed = True

            if changed:
                self._persist()

    # ═══════════════════════════════════════════════════════════════
    #  WIN/LOSS STREAK TRACKING
    # ═══════════════════════════════════════════════════════════════

    def _update_streaks(self, pnl: float) -> None:
        """Update win/loss streaks based on trade result (called inside lock)."""
        if pnl > 0:
            # Winner
            self._data["total_wins"] = (self._data.get("total_wins", 0) or 0) + 1
            self._data["win_streak"] = (self._data.get("win_streak", 0) or 0) + 1
            self._data["loss_streak"] = 0
            self._data["consecutive_losses"] = 0
            
            # Track best win streak
            best_win = self._data.get("best_win_streak", 0) or 0
            if self._data["win_streak"] > best_win:
                self._data["best_win_streak"] = self._data["win_streak"]

            logger.debug(f"🟢 Win streak: {self._data['win_streak']}")

        elif pnl < 0:
            # Loser
            self._data["total_losses"] = (self._data.get("total_losses", 0) or 0) + 1
            self._data["loss_streak"] = (self._data.get("loss_streak", 0) or 0) + 1
            self._data["consecutive_losses"] = self._data["loss_streak"]
            self._data["win_streak"] = 0

            # Track worst loss streak
            worst_loss = self._data.get("worst_loss_streak", 0) or 0
            if self._data["loss_streak"] > worst_loss:
                self._data["worst_loss_streak"] = self._data["loss_streak"]

            logger.debug(f"🔴 Loss streak: {self._data['loss_streak']}")

        # Track largest win/loss
        if pnl > 0:
            largest_win = self._data.get("largest_win", 0) or 0
            if pnl > largest_win:
                self._data["largest_win"] = pnl
        elif pnl < 0:
            largest_loss = self._data.get("largest_loss", 0) or 0
            if pnl < largest_loss:
                self._data["largest_loss"] = pnl

    def get_streak_info(self) -> Dict:
        """Get current streak information."""
        with self._lock:
            return {
                "win_streak": self._data.get("win_streak", 0),
                "loss_streak": self._data.get("loss_streak", 0),
                "consecutive_losses": self._data.get("consecutive_losses", 0),
                "best_win_streak": self._data.get("best_win_streak", 0),
                "worst_loss_streak": self._data.get("worst_loss_streak", 0),
                "total_wins": self._data.get("total_wins", 0),
                "total_losses": self._data.get("total_losses", 0),
            }

    # ═══════════════════════════════════════════════════════════════
    #  EQUITY CURVE & PERFORMANCE
    # ═══════════════════════════════════════════════════════════════

    def _record_equity_point(self, balance: float, pnl: float) -> None:
        """Record equity point after trade (called inside lock)."""
        equity_history = self._data.get("equity_history") or []
        
        point = {
            "timestamp": datetime.utcnow().isoformat(),
            "balance": round(balance, 4),
            "pnl": round(pnl, 4),
            "trade_number": self._data.get("total_trades", 0),
        }
        
        equity_history.append(point)
        
        # Limit size
        if len(equity_history) > self.MAX_EQUITY_POINTS:
            equity_history = equity_history[-self.MAX_EQUITY_POINTS:]
        
        self._data["equity_history"] = equity_history

        # Calculate equity momentum (trend of last N trades)
        self._update_equity_momentum()

    def _update_equity_momentum(self) -> None:
        """Calculate momentum based on recent equity changes (inside lock)."""
        history = self._data.get("equity_history") or []
        
        if len(history) < 3:
            self._data["equity_momentum"] = 0.0
            return

        # Look at last 10 trades
        recent = history[-10:]
        pnls = [p.get("pnl", 0) for p in recent]
        
        # Simple momentum: weighted average of recent PnLs
        weights = list(range(1, len(pnls) + 1))  # More recent = higher weight
        weighted_sum = sum(p * w for p, w in zip(pnls, weights))
        total_weight = sum(weights)
        
        momentum = weighted_sum / total_weight if total_weight > 0 else 0
        self._data["equity_momentum"] = round(momentum, 4)

    def get_equity_momentum(self) -> float:
        """Get current equity momentum (positive = winning, negative = losing)."""
        with self._lock:
            return self._data.get("equity_momentum", 0.0)

    def get_performance_metrics(self) -> Dict:
        """Calculate comprehensive performance metrics."""
        with self._lock:
            history = self._data.get("trade_history") or []
            
            if not history:
                return {
                    "total_trades": 0,
                    "win_rate": 0.0,
                    "profit_factor": 0.0,
                    "avg_win": 0.0,
                    "avg_loss": 0.0,
                    "expectancy": 0.0,
                    "total_pnl": 0.0,
                    "max_drawdown": 0.0,
                }

            wins = [t["pnl_amount"] for t in history if t.get("pnl_amount", 0) > 0]
            losses = [t["pnl_amount"] for t in history if t.get("pnl_amount", 0) < 0]
            
            total_trades = len(history)
            total_wins = len(wins)
            total_losses = len(losses)
            
            win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
            
            gross_profit = sum(wins) if wins else 0
            gross_loss = abs(sum(losses)) if losses else 0
            profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf') if gross_profit > 0 else 0
            
            avg_win = (gross_profit / total_wins) if total_wins > 0 else 0
            avg_loss = (gross_loss / total_losses) if total_losses > 0 else 0
            
            # Expectancy: (Win% × AvgWin) - (Loss% × AvgLoss)
            win_prob = total_wins / total_trades if total_trades > 0 else 0
            loss_prob = total_losses / total_trades if total_trades > 0 else 0
            expectancy = (win_prob * avg_win) - (loss_prob * avg_loss)
            
            total_pnl = sum(t.get("pnl_amount", 0) for t in history)
            
            # Max drawdown from equity curve
            max_drawdown = self._calculate_max_drawdown()
            
            # Average hold time
            hold_times = [t.get("hold_duration_minutes", 0) for t in history if t.get("hold_duration_minutes")]
            avg_hold_time = sum(hold_times) / len(hold_times) if hold_times else 0

            return {
                "total_trades": total_trades,
                "total_wins": total_wins,
                "total_losses": total_losses,
                "win_rate": round(win_rate, 2),
                "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else 999.99,
                "avg_win": round(avg_win, 4),
                "avg_loss": round(avg_loss, 4),
                "expectancy": round(expectancy, 4),
                "total_pnl": round(total_pnl, 4),
                "max_drawdown": round(max_drawdown, 2),
                "avg_hold_time_minutes": round(avg_hold_time, 1),
                "largest_win": self._data.get("largest_win", 0),
                "largest_loss": self._data.get("largest_loss", 0),
                "best_win_streak": self._data.get("best_win_streak", 0),
                "worst_loss_streak": self._data.get("worst_loss_streak", 0),
            }

    def _calculate_max_drawdown(self) -> float:
        """Calculate maximum drawdown percentage from equity history."""
        equity_history = self._data.get("equity_history") or []
        
        if len(equity_history) < 2:
            return 0.0

        balances = [p.get("balance", 0) for p in equity_history]
        
        peak = balances[0]
        max_dd = 0.0
        
        for balance in balances:
            if balance > peak:
                peak = balance
            
            if peak > 0:
                dd = ((peak - balance) / peak) * 100
                max_dd = max(max_dd, dd)
        
        return max_dd

    def _calculate_hold_duration(self, opened_at: str) -> int:
        """Calculate hold duration in minutes."""
        try:
            opened = datetime.fromisoformat(opened_at.replace('Z', '+00:00'))
            duration = datetime.utcnow() - opened.replace(tzinfo=None)
            return int(duration.total_seconds() / 60)
        except:
            return 0

    # ═══════════════════════════════════════════════════════════════
    #  TRADING GATES
    # ═══════════════════════════════════════════════════════════════

    def can_trade(self) -> Tuple[bool, str]:
        """
        Master trade gate. Returns (can_trade, reason).
        """
        with self._lock:
            if not self._data.get("bot_active", True):
                return False, "Bot is deactivated"

            if self._data.get("kill_switch", False):
                return False, "Kill switch is active"

            # Daily trade limit
            done = self._data.get("trades_today", 0) or 0
            limit = self._data.get("max_trades_per_day", 10) or 10
            if done >= limit:
                return False, f"Daily limit reached ({done}/{limit})"

            # Cooldown check
            cooldown = self._data.get("cooldown_until")
            if cooldown:
                try:
                    cooldown_time = datetime.fromisoformat(cooldown)
                    if datetime.utcnow() < cooldown_time:
                        remaining = (cooldown_time - datetime.utcnow()).seconds
                        return False, f"Cooldown active ({remaining}s remaining)"
                except (ValueError, TypeError):
                    pass

            # Max consecutive losses check
            max_losses = self._data.get("max_consecutive_losses", 5)
            current_losses = self._data.get("consecutive_losses", 0)
            if current_losses >= max_losses:
                return False, f"Max consecutive losses reached ({current_losses})"

            return True, "OK"

    def set_cooldown(self, seconds: int) -> None:
        """Set a trading cooldown period."""
        with self._lock:
            cooldown_until = datetime.utcnow() + timedelta(seconds=seconds)
            self._data["cooldown_until"] = cooldown_until.isoformat()
            self._persist()
            logger.info(f"⏸️ Cooldown set for {seconds} seconds")

    def clear_cooldown(self) -> None:
        """Clear any active cooldown."""
        with self._lock:
            self._data["cooldown_until"] = None
            self._persist()
            logger.info("▶️ Cooldown cleared")

    def trigger_kill_switch(self, reason: str = "manual") -> None:
        """Activate kill switch to stop all trading."""
        with self._lock:
            self._data["kill_switch"] = True
            self._data["kill_switch_reason"] = reason
            self._data["kill_switch_time"] = datetime.utcnow().isoformat()
            self._persist()
        logger.critical(f"🛑 KILL SWITCH ACTIVATED: {reason}")

    def reset_kill_switch(self) -> None:
        """Deactivate kill switch."""
        with self._lock:
            self._data["kill_switch"] = False
            self._data["kill_switch_reason"] = None
            self._data["kill_switch_time"] = None
            self._persist()
        logger.info("✅ Kill switch reset")

    def deactivate_bot(self, reason: str = "manual") -> None:
        """Emergency: disable all trading."""
        with self._lock:
            self._data["bot_active"] = False
            self._data["deactivation_reason"] = reason
            self._data["deactivation_time"] = datetime.utcnow().isoformat()
            self._persist()
        logger.critical(f"🛑 Bot DEACTIVATED: {reason}")

    def activate_bot(self) -> None:
        """Re-enable trading and clear kill switch."""
        with self._lock:
            self._data["bot_active"] = True
            self._data["kill_switch"] = False
            self._data["deactivation_reason"] = None
            self._persist()
        logger.info("✅ Bot ACTIVATED")

    def reset_consecutive_losses(self) -> None:
        """Reset loss streak (e.g., after manual review)."""
        with self._lock:
            self._data["consecutive_losses"] = 0
            self._data["loss_streak"] = 0
            self._persist()
        logger.info("🔄 Loss streak reset")

    # ═══════════════════════════════════════════════════════════════
    #  DAILY RESET
    # ═══════════════════════════════════════════════════════════════

    def _reset_if_new_day(self) -> None:
        """Auto-reset daily counters when date rolls over."""
        today = str(date.today())
        last_reset = self._data.get("last_daily_reset")

        if last_reset == today:
            return

        balance = self._data.get("balance", 0)

        # Store yesterday's stats before reset
        if last_reset:
            daily_history = self._data.get("daily_history") or []
            daily_history.append({
                "date": last_reset,
                "pnl": self._data.get("daily_pnl", 0),
                "trades": self._data.get("trades_today", 0),
                "end_balance": balance,
            })
            # Keep last 30 days
            if len(daily_history) > 30:
                daily_history = daily_history[-30:]
            self._data["daily_history"] = daily_history

        self._data.update({
            "daily_pnl": 0.0,
            "trades_today": 0,
            "trades_done_today": 0,
            "start_of_day_balance": balance,
            "last_daily_reset": today,
            "last_loss_guard_reset": today,
            "cooldown_until": None,
            "daily_high_balance": balance,
            "daily_low_balance": balance,
        })

        self._persist()
        logger.info(f"🔄 Daily reset | {today} | Start balance=${balance:.2f}")

    def force_daily_reset(self) -> None:
        """Force daily reset (useful for testing)."""
        with self._lock:
            self._data["last_daily_reset"] = None
            self._reset_if_new_day()

    # ═══════════════════════════════════════════════════════════════
    #  PERSISTENCE LAYER
    # ═══════════════════════════════════════════════════════════════

    def _load_defaults(self) -> Dict:
        """Load base values from defaults.json."""
        if not self._defaults_file.exists():
            return {}
        try:
            with open(self._defaults_file, "r") as f:
                data = json.load(f)
            logger.debug(f"📂 Defaults loaded: {self._defaults_file}")
            return data
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"⚠️ defaults.json load failed: {e}")
            return {}

    def _load_persisted_state(self) -> None:
        """Overlay saved state on top of defaults."""
        if not self._state_file.exists():
            return
        try:
            with open(self._state_file, "r") as f:
                persisted = json.load(f)
            self._data.update(persisted)
            logger.debug(f"📂 State loaded: {self._state_file}")
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"⚠️ state.json load failed: {e}")

    def _persist(self) -> None:
        """Write current state to disk (called inside lock)."""
        try:
            safe = self._make_serializable(self._data)
            tmp = self._state_file.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(safe, f, indent=2, default=str)
            tmp.replace(self._state_file)  # Atomic rename
        except (IOError, TypeError) as e:
            logger.error(f"❌ Persist failed: {e}")

    def _make_serializable(self, obj: Any) -> Any:
        """Recursively sanitize for JSON."""
        if isinstance(obj, dict):
            return {k: self._make_serializable(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._make_serializable(v) for v in obj]
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, float):
            if obj != obj:  # NaN guard
                return 0.0
            if obj == float('inf'):
                return 999999.99
            if obj == float('-inf'):
                return -999999.99
        return obj

    def _ensure_critical_keys(self, initial_balance: float) -> None:
        """Guarantee every required key exists."""
        defaults = {
            "balance": initial_balance,
            "initial_balance": initial_balance,
            "start_of_day_balance": initial_balance,
            "bot_active": True,
            "kill_switch": False,
            "positions": {},
            "trade_history": [],
            "daily_pnl": 0.0,
            "trades_today": 0,
            "trades_done_today": 0,
            "max_trades_per_day": 10,
            "max_consecutive_losses": 5,
            "total_trades": 0,
            "total_wins": 0,
            "total_losses": 0,
            "win_streak": 0,
            "loss_streak": 0,
            "consecutive_losses": 0,
            "best_win_streak": 0,
            "worst_loss_streak": 0,
            "largest_win": 0,
            "largest_loss": 0,
            "equity_history": [],
            "daily_history": [],
            "rr_history": [],
            "equity_momentum": 0.0,
            "last_risk": None,
            "cooldown_until": None,
            "last_daily_reset": None,
            "last_loss_guard_reset": None,
            "last_trade_time": None,
        }

        changed = False
        for key, value in defaults.items():
            if key not in self._data:
                self._data[key] = value
                changed = True

        if changed:
            self._persist()

    def reset_state(self, keep_balance: bool = False) -> None:
        """
        Full state reset. Use with caution.
        If keep_balance=True, preserves current balance.
        """
        with self._lock:
            current_balance = self._data.get("balance", 100) if keep_balance else 100
            
            self._data = self._load_defaults()
            self._data["balance"] = current_balance
            self._data["initial_balance"] = current_balance
            self._ensure_critical_keys(current_balance)
            self._persist()
            
        logger.warning(f"🔄 State RESET | Balance=${current_balance:.2f}")

    # ═══════════════════════════════════════════════════════════════
    #  DEBUG / EXPORT
    # ═══════════════════════════════════════════════════════════════

    def export(self) -> Dict:
        """Full snapshot of current state."""
        with self._lock:
            return deepcopy(self._data)

    def summary(self) -> Dict:
        """Compact summary for status commands."""
        with self._lock:
            return {
                "balance": round(self._data.get("balance", 0), 2),
                "daily_pnl": round(self._data.get("daily_pnl", 0), 4),
                "positions": len(self._data.get("positions", {})),
                "trades_today": self._data.get("trades_today", 0),
                "total_trades": self._data.get("total_trades", 0),
                "win_rate": self._calculate_quick_win_rate(),
                "win_streak": self._data.get("win_streak", 0),
                "loss_streak": self._data.get("loss_streak", 0),
                "equity_momentum": self._data.get("equity_momentum", 0),
                "bot_active": self._data.get("bot_active", False),
                "kill_switch": self._data.get("kill_switch", False),
            }

    def _calculate_quick_win_rate(self) -> float:
        """Quick win rate calculation."""
        wins = self._data.get("total_wins", 0) or 0
        losses = self._data.get("total_losses", 0) or 0
        total = wins + losses
        return round((wins / total * 100), 1) if total > 0 else 0.0

    def get_daily_stats(self) -> Dict:
        """Get today's trading statistics."""
        with self._lock:
            start_balance = self._data.get("start_of_day_balance", 0)
            current_balance = self._data.get("balance", 0)
            daily_pnl = self._data.get("daily_pnl", 0)
            
            pnl_percent = (daily_pnl / start_balance * 100) if start_balance > 0 else 0
            
            return {
                "date": str(date.today()),
                "start_balance": round(start_balance, 2),
                "current_balance": round(current_balance, 2),
                "daily_pnl": round(daily_pnl, 4),
                "daily_pnl_percent": round(pnl_percent, 2),
                "trades_today": self._data.get("trades_today", 0),
                "positions_open": len(self._data.get("positions", {})),
            }

    def __repr__(self) -> str:
        return (
            f"<StateManager | "
            f"${self.get('balance', 0):.2f} | "
            f"{len(self.get('positions', {}))} positions | "
            f"WR={self._calculate_quick_win_rate()}% | "
            f"Active={self.get('bot_active')}>"
        )