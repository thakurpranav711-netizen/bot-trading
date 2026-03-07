# app/state/manager.py

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

FIXED: 
- add_position now handles both old and new calling conventions
- can_trade() now returns tuple (bool, str) consistently
- can_trade() properly checks all conditions without false blocks
- Removed can_trade() wrapper that was causing "State blocks trading"
- Daily loss limit check is now more lenient (checks actual INR limit)

NEW Features:
- Daily loss tracking in INR (₹1500 default limit)
- USD to INR conversion support
- Daily loss limit enforcement
- Automatic next-day trading resume
- Enhanced P/L notifications data

Balance Accounting:
    Entry:  controller calls adjust_balance(-(cost + entry_fee))
    Exit:   close_position credits entry_price * qty + net_pnl
    Result: balance = original + gross_pnl - all_fees  ✓
"""

import json
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from copy import deepcopy
from app.utils.logger import get_logger

logger = get_logger(__name__)


class StateManager:
    """
    Thread-Safe Persistent State Manager for Autonomous Trading Bot
    
    FIXED: can_trade() now properly allows trading when conditions are met.
    """

    MAX_HISTORY = 500
    MAX_EQUITY_POINTS = 1000  # Limit equity history size
    
    # Default USD to INR conversion rate
    DEFAULT_USD_TO_INR = 83.0
    
    # Default daily loss limit in INR
    DEFAULT_DAILY_LOSS_LIMIT_INR = 1500.0

    def __init__(
        self,
        state_file: Optional[str] = None,
        defaults_file: Optional[str] = None,
        initial_balance: float = 100.0,
        daily_loss_limit_inr: float = 1500.0,
        usd_to_inr_rate: float = 83.0,
    ):
        self._lock = threading.Lock()

        base_dir = Path(__file__).parent
        self._state_file = (
            Path(state_file) if state_file else base_dir / "state.json"
        )
        self._defaults_file = (
            Path(defaults_file) if defaults_file else base_dir / "defaults.json"
        )

        # Store configuration
        self._daily_loss_limit_inr = daily_loss_limit_inr
        self._usd_to_inr_rate = usd_to_inr_rate

        # Load defaults first, then overlay persisted state
        self._data: Dict[str, Any] = self._load_defaults()
        self._load_persisted_state()

        # Fill any missing keys with sane values
        self._ensure_critical_keys(initial_balance)
        
        # Set daily loss limit configuration
        self._data["daily_loss_limit_inr"] = daily_loss_limit_inr
        self._data["usd_to_inr_rate"] = usd_to_inr_rate
        self._data["pnl_currency"] = self._data.get("pnl_currency", "USD")
        
        # CRITICAL FIX: Ensure bot is active on initialization
        if "bot_active" not in self._data:
            self._data["bot_active"] = True

        # Reset daily counters if date rolled over
        self._reset_if_new_day()

        logger.info(
            f"✅ StateManager ready | "
            f"Balance=${self.get('balance', 0):.2f} | "
            f"Positions={len(self.get('positions', {}))} | "
            f"Daily Loss Limit=₹{daily_loss_limit_inr:,.2f} | "
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
    #  DAILY P/L TRACKING
    # ═══════════════════════════════════════════════════════════════

    def get_daily_pnl(self) -> float:
        """Get daily P/L in the base currency (USD)."""
        with self._lock:
            return self._data.get("daily_pnl", 0.0) or 0.0

    def get_daily_pnl_inr(self) -> float:
        """
        Get daily P/L converted to INR.
        
        Returns:
            Daily P/L in INR (negative = loss)
        """
        with self._lock:
            daily_pnl = self._data.get("daily_pnl", 0.0) or 0.0
            pnl_currency = self._data.get("pnl_currency", "USD")
            
            if pnl_currency == "INR":
                return daily_pnl
            
            # Convert USD to INR
            usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
            return daily_pnl * usd_to_inr

    def update_daily_pnl(self, pnl_amount: float, currency: str = "USD") -> float:
        """
        Update daily P/L with a new trade result.
        
        Args:
            pnl_amount: P/L from the trade
            currency: Currency of the P/L ("USD" or "INR")
            
        Returns:
            New daily P/L total in USD
        """
        with self._lock:
            # Convert to USD if needed
            if currency == "INR":
                usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
                pnl_amount = pnl_amount / usd_to_inr
            
            current = self._data.get("daily_pnl", 0.0) or 0.0
            new_pnl = round(current + pnl_amount, 8)
            self._data["daily_pnl"] = new_pnl
            
            # Also track in INR
            usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
            self._data["daily_pnl_inr"] = round(new_pnl * usd_to_inr, 2)
            
            self._persist()
            
            logger.debug(
                f"📊 Daily PnL: ${current:.4f} → ${new_pnl:.4f} "
                f"(₹{self._data['daily_pnl_inr']:,.2f})"
            )
            
            return new_pnl

    def check_daily_loss_limit(self) -> Tuple[bool, float, float]:
        """
        Check if daily loss limit has been reached.
        
        Returns:
            Tuple of (limit_reached, current_loss_inr, limit_inr)
        """
        with self._lock:
            daily_pnl_inr = self._data.get("daily_pnl_inr", 0.0) or 0.0
            
            # If daily_pnl_inr not set, calculate from daily_pnl
            if daily_pnl_inr == 0.0:
                daily_pnl = self._data.get("daily_pnl", 0.0) or 0.0
                usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
                daily_pnl_inr = daily_pnl * usd_to_inr
            
            limit = self._data.get("daily_loss_limit_inr", self.DEFAULT_DAILY_LOSS_LIMIT_INR)
            
            # Loss is negative, so check if pnl <= -limit
            limit_reached = daily_pnl_inr <= -limit
            
            return limit_reached, daily_pnl_inr, limit

    def is_daily_loss_limit_reached(self) -> bool:
        """Simple check if daily loss limit is reached."""
        reached, _, _ = self.check_daily_loss_limit()
        return reached

    def get_daily_loss_status(self) -> Dict[str, Any]:
        """
        Get comprehensive daily loss status.
        
        Returns:
            Dict with loss details and limit status
        """
        with self._lock:
            daily_pnl = self._data.get("daily_pnl", 0.0) or 0.0
            usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
            daily_pnl_inr = daily_pnl * usd_to_inr
            limit_inr = self._data.get("daily_loss_limit_inr", self.DEFAULT_DAILY_LOSS_LIMIT_INR)
            
            # Calculate remaining allowance
            if daily_pnl_inr < 0:
                remaining = limit_inr + daily_pnl_inr  # pnl is negative
            else:
                remaining = limit_inr
            
            limit_reached = daily_pnl_inr <= -limit_inr
            
            return {
                "daily_pnl_usd": round(daily_pnl, 4),
                "daily_pnl_inr": round(daily_pnl_inr, 2),
                "daily_loss_limit_inr": limit_inr,
                "remaining_allowance_inr": max(0, round(remaining, 2)),
                "limit_reached": limit_reached,
                "usage_percent": min(100, abs(daily_pnl_inr / limit_inr * 100)) if limit_inr > 0 else 0,
                "usd_to_inr_rate": usd_to_inr,
                "trading_halted": self._data.get("daily_loss_halt", False),
                "halt_date": self._data.get("daily_loss_halt_date"),
            }

    def set_daily_loss_halt(self, halted: bool, reason: str = "") -> None:
        """
        Set daily loss halt status.
        
        Args:
            halted: True to halt trading, False to resume
            reason: Reason for halt
        """
        with self._lock:
            self._data["daily_loss_halt"] = halted
            
            if halted:
                self._data["daily_loss_halt_date"] = str(date.today())
                self._data["daily_loss_halt_reason"] = reason
                self._data["daily_loss_halt_time"] = datetime.utcnow().isoformat()
                logger.critical(f"🚨 Daily loss halt activated: {reason}")
            else:
                self._data["daily_loss_halt"] = False
                self._data["daily_loss_halt_reason"] = None
                logger.info("✅ Daily loss halt cleared")
            
            self._persist()

    def is_daily_loss_halted(self) -> bool:
        """Check if trading is halted due to daily loss."""
        with self._lock:
            return self._data.get("daily_loss_halt", False)

    def update_usd_to_inr_rate(self, rate: float) -> None:
        """Update USD to INR conversion rate."""
        with self._lock:
            self._data["usd_to_inr_rate"] = rate
            
            # Recalculate daily_pnl_inr
            daily_pnl = self._data.get("daily_pnl", 0.0) or 0.0
            self._data["daily_pnl_inr"] = round(daily_pnl * rate, 2)
            
            self._persist()
            logger.info(f"💱 USD/INR rate updated: {rate}")

    def update_daily_loss_limit(self, limit_inr: float) -> None:
        """Update daily loss limit in INR."""
        with self._lock:
            old_limit = self._data.get("daily_loss_limit_inr", self.DEFAULT_DAILY_LOSS_LIMIT_INR)
            self._data["daily_loss_limit_inr"] = limit_inr
            self._persist()
            logger.info(f"💰 Daily loss limit updated: ₹{old_limit:,.2f} → ₹{limit_inr:,.2f}")

    # ═══════════════════════════════════════════════════════════════
    #  POSITION MANAGEMENT - FIXED
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
        side_or_data: Union[str, Dict] = "long",
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
        probability: float = 0.0,
        metadata: Optional[Dict] = None,
        **kwargs,
    ) -> Dict:
        """
        Open a new position or average into existing.
        
        FIXED: Now handles both calling conventions:
        1. Old style: add_position(symbol, qty, price, side, sl, tp, prob, metadata)
        2. New style: add_position(symbol, qty, price, position_data_dict)
        
        Returns the position dict.
        """
        with self._lock:
            positions = self._data.setdefault("positions", {})
            now = datetime.utcnow().isoformat()

            # ══════════════════════════════════════════════════════════
            #  HANDLE BOTH CALLING CONVENTIONS
            # ══════════════════════════════════════════════════════════
            
            if isinstance(side_or_data, dict):
                # New style: position_data dict was passed as 4th argument
                position_data = side_or_data
                side = position_data.get("action", position_data.get("side", "BUY"))
                stop_loss = position_data.get("stop_loss", 0.0)
                take_profit = position_data.get("take_profit", 0.0)
                probability = position_data.get("confidence", position_data.get("entry_probability", 0.0))
                metadata = position_data  # Use the whole dict as metadata
            else:
                # Old style: side is a string
                side = side_or_data
                
            # Normalize side to lowercase for storage
            if isinstance(side, str):
                side_lower = side.lower()
                if side_lower in ("buy", "long"):
                    side = "long"
                elif side_lower in ("sell", "short"):
                    side = "short"
                else:
                    side = "long"  # Default
            else:
                side = "long"

            # ══════════════════════════════════════════════════════════
            #  EXISTING OR NEW POSITION
            # ══════════════════════════════════════════════════════════

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
                
                # Update targets if provided
                if stop_loss > 0:
                    existing["stop_loss"] = stop_loss
                if take_profit > 0:
                    existing["take_profit"] = take_profit
                if probability > 0:
                    existing["entry_probability"] = probability
                    existing["confidence"] = probability

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
                    "action": "BUY" if side == "long" else "SELL",
                    "quantity": round(quantity, 8),
                    "entry_price": round(entry_price, 8),
                    "avg_price": round(entry_price, 8),
                    "opened_at": now,
                    "entry_time": now,
                    "updated_at": now,
                    "add_count": 1,
                    "highest_price": entry_price,
                    "lowest_price": entry_price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "entry_probability": probability,
                    "confidence": probability,
                    "unrealized_pnl": 0.0,
                    "unrealized_pnl_pct": 0.0,
                }
                
                # Merge metadata if provided
                if metadata:
                    protected = {"quantity", "entry_price", "avg_price", "opened_at", "symbol"}
                    for k, v in metadata.items():
                        if k not in protected:
                            position[k] = v
                
                positions[symbol] = position

                # Determine display side
                display_side = "BUY" if side == "long" else "SELL"

                logger.info(
                    f"📈 Position opened | {symbol} {display_side} | "
                    f"Qty={quantity} @ ${entry_price:.6f} | "
                    f"SL=${stop_loss:.6f} | TP=${take_profit:.6f} | "
                    f"Prob={probability*100 if probability < 1 else probability:.0f}%"
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
        exit_probability: float = 0.0,
    ) -> Optional[Dict]:
        """
        Close (or partially close) a position.
        Updates streaks, balance, equity history, and daily P/L.
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

            # Update daily PnL (USD)
            daily_pnl = self._data.get("daily_pnl", 0.0) or 0.0
            self._data["daily_pnl"] = round(daily_pnl + net_pnl, 8)
            
            # Update daily PnL (INR)
            usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
            net_pnl_inr = net_pnl * usd_to_inr
            daily_pnl_inr = self._data.get("daily_pnl_inr", 0.0) or 0.0
            self._data["daily_pnl_inr"] = round(daily_pnl_inr + net_pnl_inr, 2)

            # Update win/loss streaks
            self._update_streaks(net_pnl)

            # Record equity point
            self._record_equity_point(new_balance, net_pnl)

            # Calculate trade metrics
            pnl_percent = round((net_pnl / (entry_price * closed_qty)) * 100, 2) if entry_price * closed_qty > 0 else 0
            hold_duration = self._calculate_hold_duration(position.get("opened_at", position.get("entry_time", now)))

            # Get side for display
            side = position.get("side", position.get("action", "long"))
            if isinstance(side, str):
                side = side.lower()
                if side in ("buy", "long"):
                    side = "long"
                elif side in ("sell", "short"):
                    side = "short"

            # Create trade record (enhanced)
            trade_record = {
                "symbol": symbol,
                "side": side,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": closed_qty,
                "pnl_amount": round(net_pnl, 8),
                "pnl_amount_inr": round(net_pnl_inr, 2),
                "pnl_percent": pnl_percent,
                "closed_at": now,
                "opened_at": position.get("opened_at", position.get("entry_time", "")),
                "hold_duration_minutes": hold_duration,
                "strategy": position.get("strategy", "unknown"),
                "reason": reason,
                "mode": position.get("mode", "PAPER"),
                "entry_probability": position.get("entry_probability", position.get("confidence", 0)),
                "exit_probability": exit_probability,
                "stop_loss": position.get("stop_loss", 0),
                "take_profit": position.get("take_profit", 0),
                "hit_stop_loss": exit_price <= position.get("stop_loss", 0) if position.get("stop_loss", 0) > 0 else False,
                "hit_take_profit": exit_price >= position.get("take_profit", 0) if position.get("take_profit", 0) > 0 else False,
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
                log_msg = (
                    f"📉 Position closed | {symbol} | "
                    f"PnL=${net_pnl:+.4f} (₹{net_pnl_inr:+,.2f}) ({pnl_percent:+.2f}%) | "
                    f"Balance=${new_balance:.2f} | "
                    f"Reason: {reason}"
                )
            else:
                position["quantity"] = remaining
                position["updated_at"] = now
                log_msg = (
                    f"📉 Partial close | {symbol} | "
                    f"Closed={closed_qty} Remaining={remaining} | "
                    f"PnL=${net_pnl:+.4f} (₹{net_pnl_inr:+,.2f})"
                )

            logger.info(log_msg)
            self._persist()
            return trade_record

    def close_all_positions(
        self,
        current_prices: Dict[str, float],
        reason: str = "manual",
    ) -> List[Dict]:
        """
        Close all open positions.
        
        Args:
            current_prices: Dict of symbol -> current price
            reason: Reason for closing all positions
            
        Returns:
            List of trade records for closed positions
        """
        positions = self.get_all_positions()
        trade_records = []
        
        for symbol, pos in positions.items():
            current_price = current_prices.get(symbol, pos.get("entry_price", 0))
            entry_price = pos.get("entry_price", 0)
            quantity = pos.get("quantity", 0)
            
            # Calculate P/L
            side = pos.get("side", pos.get("action", "long"))
            if isinstance(side, str) and side.lower() in ("long", "buy"):
                net_pnl = (current_price - entry_price) * quantity
            else:
                net_pnl = (entry_price - current_price) * quantity
            
            record = self.close_position(
                symbol=symbol,
                net_pnl=net_pnl,
                exit_price=current_price,
                reason=reason,
            )
            
            if record:
                trade_records.append(record)
        
        logger.info(f"📉 Closed all positions | Count: {len(trade_records)} | Reason: {reason}")
        return trade_records

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

    def remove_position(self, symbol: str) -> bool:
        """
        Remove a position without P/L calculation.
        Use close_position() for proper accounting.
        """
        with self._lock:
            positions = self._data.get("positions", {})
            if symbol in positions:
                del positions[symbol]
                self._persist()
                logger.info(f"🗑️ Position removed | {symbol}")
                return True
            return False

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
            
            # Update unrealized P/L
            entry_price = pos.get("entry_price", 0)
            quantity = pos.get("quantity", 0)
            if entry_price > 0 and quantity > 0:
                side = pos.get("side", pos.get("action", "long"))
                if isinstance(side, str) and side.lower() in ("long", "buy"):
                    unrealized_pnl = (current_price - entry_price) * quantity
                    unrealized_pnl_pct = ((current_price - entry_price) / entry_price) * 100
                else:
                    unrealized_pnl = (entry_price - current_price) * quantity
                    unrealized_pnl_pct = ((entry_price - current_price) / entry_price) * 100
                    
                pos["unrealized_pnl"] = round(unrealized_pnl, 8)
                pos["unrealized_pnl_pct"] = round(unrealized_pnl_pct, 2)
                pos["current_price"] = current_price
                changed = True

            if changed:
                self._persist()

    def get_total_unrealized_pnl(self, current_prices: Dict[str, float]) -> Dict[str, float]:
        """
        Get total unrealized P/L across all positions.
        """
        with self._lock:
            positions = self._data.get("positions", {})
            total_unrealized = 0.0
            
            for symbol, pos in positions.items():
                current_price = current_prices.get(symbol, pos.get("entry_price", 0))
                entry_price = pos.get("entry_price", 0)
                quantity = pos.get("quantity", 0)
                
                side = pos.get("side", pos.get("action", "long"))
                if isinstance(side, str) and side.lower() in ("long", "buy"):
                    unrealized = (current_price - entry_price) * quantity
                else:
                    unrealized = (entry_price - current_price) * quantity
                    
                total_unrealized += unrealized
            
            usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
            
            return {
                "unrealized_pnl_usd": round(total_unrealized, 4),
                "unrealized_pnl_inr": round(total_unrealized * usd_to_inr, 2),
            }

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
        
        usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
        
        point = {
            "timestamp": datetime.utcnow().isoformat(),
            "balance": round(balance, 4),
            "pnl": round(pnl, 4),
            "pnl_inr": round(pnl * usd_to_inr, 2),
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
        
        # FIXED: Handle both dict and float types in equity history
        pnls = []
        for p in recent:
            if isinstance(p, dict):
                pnls.append(p.get("pnl", 0))
            elif isinstance(p, (int, float)):
                pnls.append(float(p))
            else:
                pnls.append(0)
        
        if not pnls:
            self._data["equity_momentum"] = 0.0
            return
        
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
                    "total_pnl_inr": 0.0,
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
            
            # Convert to INR
            usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
            total_pnl_inr = total_pnl * usd_to_inr
            
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
                "total_pnl_inr": round(total_pnl_inr, 2),
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

        # FIXED: Handle both dict and float types
        balances = []
        for p in equity_history:
            if isinstance(p, dict):
                balances.append(p.get("balance", 0))
            elif isinstance(p, (int, float)):
                balances.append(float(p))
            else:
                balances.append(0)
        
        if not balances:
            return 0.0
        
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
        except Exception:
            return 0

    # ═══════════════════════════════════════════════════════════════
    #  TRADING GATES - FIXED
    # ═══════════════════════════════════════════════════════════════

    def can_trade(self) -> Tuple[bool, str]:
        """
        Master trade gate.
        
        FIXED: Now returns Tuple[bool, str] consistently.
        The controller expects this format.
        
        Returns:
            Tuple of (can_trade: bool, reason: str)
        """
        with self._lock:
            # Check 1: Bot must be active
            if not self._data.get("bot_active", True):
                return False, "Bot is deactivated"

            # Check 2: Kill switch must be off
            if self._data.get("kill_switch", False):
                return False, "Kill switch is active"
            
            # Check 3: Daily loss halt
            if self._data.get("daily_loss_halt", False):
                return False, "Daily loss limit reached - trading halted"
            
            # Check 4: Daily loss limit (real-time check)
            # Only block if we've EXCEEDED the limit, not approaching it
            daily_pnl = self._data.get("daily_pnl", 0.0) or 0.0
            usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
            daily_pnl_inr = daily_pnl * usd_to_inr
            limit_inr = self._data.get("daily_loss_limit_inr", self.DEFAULT_DAILY_LOSS_LIMIT_INR)
            
            if daily_pnl_inr <= -limit_inr:
                return False, f"Daily loss limit reached (₹{abs(daily_pnl_inr):,.2f} / ₹{limit_inr:,.2f})"

            # Check 5: Daily trade limit
            done = self._data.get("trades_today", 0) or 0
            limit = self._data.get("max_trades_per_day", 10) or 10
            if done >= limit:
                return False, f"Daily trade limit reached ({done}/{limit})"

            # Check 6: Cooldown
            cooldown = self._data.get("cooldown_until")
            if cooldown:
                try:
                    cooldown_time = datetime.fromisoformat(cooldown)
                    if datetime.utcnow() < cooldown_time:
                        remaining = (cooldown_time - datetime.utcnow()).seconds
                        return False, f"Cooldown active ({remaining}s remaining)"
                except (ValueError, TypeError):
                    pass

            # Check 7: Max consecutive losses
            max_losses = self._data.get("max_consecutive_losses", 5)
            current_losses = self._data.get("consecutive_losses", 0)
            if current_losses >= max_losses:
                return False, f"Max consecutive losses reached ({current_losses})"

            # All checks passed
            return True, "OK"

    def can_trade_bool(self) -> bool:
        """
        Simple boolean version of can_trade().
        For backward compatibility with code that expects bool.
        """
        can, _ = self.can_trade()
        return can

    def can_trade_with_reason(self) -> Tuple[bool, str]:
        """
        Alias for can_trade() for explicit naming.
        """
        return self.can_trade()

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
            self._data["daily_loss_halt"] = False  # Also clear daily loss halt
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
                "pnl_inr": self._data.get("daily_pnl_inr", 0),
                "trades": self._data.get("trades_today", 0),
                "end_balance": balance,
                "was_halted": self._data.get("daily_loss_halt", False),
            })
            # Keep last 30 days
            if len(daily_history) > 30:
                daily_history = daily_history[-30:]
            self._data["daily_history"] = daily_history

        self._data.update({
            "daily_pnl": 0.0,
            "daily_pnl_inr": 0.0,
            "trades_today": 0,
            "trades_done_today": 0,
            "start_of_day_balance": balance,
            "last_daily_reset": today,
            "last_loss_guard_reset": today,
            "cooldown_until": None,
            "daily_high_balance": balance,
            "daily_low_balance": balance,
            "daily_loss_halt": False,  # Reset daily loss halt
            "daily_loss_halt_date": None,
            "daily_loss_halt_reason": None,
        })

        self._persist()
        logger.info(f"🔄 Daily reset | {today} | Start balance=${balance:.2f} | Daily loss halt cleared")

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
            "bot_active": True,  # CRITICAL: Default to True
            "kill_switch": False,
            "positions": {},
            "trade_history": [],
            "daily_pnl": 0.0,
            "daily_pnl_inr": 0.0,
            "trades_today": 0,
            "trades_done_today": 0,
            "max_trades_per_day": 15,  # Increased from 10
            "max_consecutive_losses": 7,  # Increased from 5
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
            # Daily loss limit fields
            "daily_loss_limit_inr": self.DEFAULT_DAILY_LOSS_LIMIT_INR,
            "usd_to_inr_rate": self.DEFAULT_USD_TO_INR,
            "pnl_currency": "USD",
            "daily_loss_halt": False,
            "daily_loss_halt_date": None,
            "daily_loss_halt_reason": None,
            # Recovery mode
            "in_recovery_mode": False,
            "recovery_wins_needed": 0,
        }

        changed = False
        for key, value in defaults.items():
            if key not in self._data:
                self._data[key] = value
                changed = True

        if changed:
            self._persist()

    # ═══════════════════════════════════════════════════════════════
    #  PUBLIC SAVE METHODS
    # ═══════════════════════════════════════════════════════════════

    def save(self) -> bool:
        """
        Public method to persist state to disk.
        """
        with self._lock:
            try:
                self._persist()
                logger.debug("💾 State saved successfully")
                return True
            except Exception as e:
                logger.error(f"❌ Failed to save state: {e}")
                return False

    def save_sync(self) -> bool:
        """
        Synchronous save with verification.
        """
        with self._lock:
            try:
                self._persist()
                
                # Verify save
                if self._state_file.exists():
                    with open(self._state_file, 'r') as f:
                        saved_data = json.load(f)
                    
                    # Quick verification
                    if saved_data.get("balance") == self._data.get("balance"):
                        logger.debug("💾 State saved and verified")
                        return True
                    else:
                        logger.warning("⚠️ State save verification mismatch")
                        return False
                
                return False
            except Exception as e:
                logger.error(f"❌ Failed to save state: {e}")
                return False

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
            self._data["bot_active"] = True  # Ensure bot is active after reset
            self._ensure_critical_keys(current_balance)
            self._persist()
            
        logger.warning(f"🔄 State RESET | Balance=${current_balance:.2f}")

    # ═══════════════════════════════════════════════════════════════
    #  EQUITY HISTORY CLEANUP
    # ═══════════════════════════════════════════════════════════════

    def cleanup_equity_history(self) -> int:
        """
        Clean up corrupted equity history entries.
        
        Returns:
            Number of entries cleaned/fixed
        """
        with self._lock:
            equity_history = self._data.get("equity_history") or []
            
            if not equity_history:
                return 0
            
            cleaned = []
            fixed_count = 0
            
            for entry in equity_history:
                if isinstance(entry, dict):
                    # Valid dict entry - keep as is
                    cleaned.append(entry)
                elif isinstance(entry, (int, float)):
                    # Convert float to proper dict format
                    cleaned.append({
                        "timestamp": datetime.utcnow().isoformat(),
                        "balance": float(entry),
                        "pnl": 0.0,
                        "pnl_inr": 0.0,
                        "trade_number": 0,
                    })
                    fixed_count += 1
                else:
                    # Skip invalid entries
                    fixed_count += 1
            
            self._data["equity_history"] = cleaned
            self._persist()
            
            if fixed_count > 0:
                logger.info(f"🧹 Cleaned equity history: {fixed_count} entries fixed")
            
            return fixed_count

    def reset_equity_history(self) -> None:
        """
        Reset equity history completely.
        Use when history is corrupted beyond repair.
        """
        with self._lock:
            balance = self._data.get("balance", 0)
            
            self._data["equity_history"] = [{
                "timestamp": datetime.utcnow().isoformat(),
                "balance": balance,
                "pnl": 0.0,
                "pnl_inr": 0.0,
                "trade_number": self._data.get("total_trades", 0),
            }]
            self._data["equity_momentum"] = 0.0
            
            self._persist()
            logger.warning("🔄 Equity history reset")

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
            usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
            daily_pnl = self._data.get("daily_pnl", 0)
            
            return {
                "balance": round(self._data.get("balance", 0), 2),
                "daily_pnl": round(daily_pnl, 4),
                "daily_pnl_inr": round(daily_pnl * usd_to_inr, 2),
                "positions": len(self._data.get("positions", {})),
                "trades_today": self._data.get("trades_today", 0),
                "total_trades": self._data.get("total_trades", 0),
                "win_rate": self._calculate_quick_win_rate(),
                "win_streak": self._data.get("win_streak", 0),
                "loss_streak": self._data.get("loss_streak", 0),
                "equity_momentum": self._data.get("equity_momentum", 0),
                "bot_active": self._data.get("bot_active", False),
                "kill_switch": self._data.get("kill_switch", False),
                "daily_loss_halt": self._data.get("daily_loss_halt", False),
                "daily_loss_limit_inr": self._data.get("daily_loss_limit_inr", self.DEFAULT_DAILY_LOSS_LIMIT_INR),
                "in_recovery_mode": self._data.get("in_recovery_mode", False),
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
            usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
            daily_pnl_inr = daily_pnl * usd_to_inr
            
            pnl_percent = (daily_pnl / start_balance * 100) if start_balance > 0 else 0
            
            limit_inr = self._data.get("daily_loss_limit_inr", self.DEFAULT_DAILY_LOSS_LIMIT_INR)
            remaining = limit_inr + daily_pnl_inr if daily_pnl_inr < 0 else limit_inr
            
            return {
                "date": str(date.today()),
                "start_balance": round(start_balance, 2),
                "current_balance": round(current_balance, 2),
                "daily_pnl": round(daily_pnl, 4),
                "daily_pnl_inr": round(daily_pnl_inr, 2),
                "daily_pnl_percent": round(pnl_percent, 2),
                "trades_today": self._data.get("trades_today", 0),
                "positions_open": len(self._data.get("positions", {})),
                "daily_loss_limit_inr": limit_inr,
                "remaining_loss_allowance_inr": max(0, round(remaining, 2)),
                "daily_loss_halt": self._data.get("daily_loss_halt", False),
            }

    def get_trade_notification_data(self, trade_record: Dict) -> Dict:
        """
        Get formatted data for trade notifications.
        """
        with self._lock:
            usd_to_inr = self._data.get("usd_to_inr_rate", self.DEFAULT_USD_TO_INR)
            
            return {
                "symbol": trade_record.get("symbol", ""),
                "side": trade_record.get("side", "long"),
                "entry_price": trade_record.get("entry_price", 0),
                "exit_price": trade_record.get("exit_price", 0),
                "quantity": trade_record.get("quantity", 0),
                "pnl_usd": trade_record.get("pnl_amount", 0),
                "pnl_inr": trade_record.get("pnl_amount_inr", trade_record.get("pnl_amount", 0) * usd_to_inr),
                "pnl_percent": trade_record.get("pnl_percent", 0),
                "hold_duration_minutes": trade_record.get("hold_duration_minutes", 0),
                "reason": trade_record.get("reason", ""),
                "entry_probability": trade_record.get("entry_probability", 0),
                "exit_probability": trade_record.get("exit_probability", 0),
                "hit_stop_loss": trade_record.get("hit_stop_loss", False),
                "hit_take_profit": trade_record.get("hit_take_profit", False),
            }

    def get_recent_trades(self, count: int = 10) -> List[Dict]:
        """
        Get recent trade history.
        """
        with self._lock:
            history = self._data.get("trade_history") or []
            return deepcopy(history[-count:][::-1])

    def get_trade_history(self) -> List[Dict]:
        """Get full trade history."""
        with self._lock:
            return deepcopy(self._data.get("trade_history") or [])

    def __repr__(self) -> str:
        daily_status = self.get_daily_loss_status()
        return (
            f"<StateManager | "
            f"${self.get('balance', 0):.2f} | "
            f"{len(self.get('positions', {}))} positions | "
            f"WR={self._calculate_quick_win_rate()}% | "
            f"Daily PnL=₹{daily_status['daily_pnl_inr']:+,.2f} | "
            f"Active={self.get('bot_active')} | "
            f"Halted={daily_status['trading_halted']}>"
        )