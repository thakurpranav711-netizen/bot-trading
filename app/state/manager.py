# app/state/manager.py

import json
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional
from copy import deepcopy
from app.utils.logger import get_logger

logger = get_logger(__name__)


class StateManager:
    """
    Thread-Safe Persistent State Manager

    Provides:
    - Key-value store with JSON file persistence
    - Position tracking (open / close / partial close with PnL)
    - Balance management with audit trail
    - Trade history recording (capped at 500)
    - Daily auto-reset (PnL, trade count, cooldowns)
    - Defaults loaded from defaults.json, overlaid by state.json
    - Thread-safe via threading.Lock (sync exchange calls won't corrupt)
    - Deep-copy returns prevent external mutation of internal data

    Balance Accounting:
        Entry:  controller calls adjust_balance(-(cost + entry_fee))
        Exit:   close_position credits entry_price * qty + net_pnl
                which equals exit_price * qty - exit_fee
        Result: balance = original + gross_pnl - all_fees  ✓
    """

    # Max trade history entries kept in memory / on disk
    MAX_HISTORY = 500

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

    # ═════════════════════════════════════════════════════
    #  CORE GET / SET / INCREMENT / DELETE
    # ═════════════════════════════════════════════════════

    def get(self, key: str, default: Any = None) -> Any:
        """
        Thread-safe key lookup.
        Returns deep copy of mutable types (dict, list)
        so callers cannot accidentally mutate internal state.
        """
        with self._lock:
            value = self._data.get(key, default)
            if isinstance(value, (dict, list)):
                return deepcopy(value)
            return value

    def set(self, key: str, value: Any) -> None:
        """Thread-safe key write with auto-persist to disk."""
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

    # ═════════════════════════════════════════════════════
    #  BALANCE MANAGEMENT
    # ═════════════════════════════════════════════════════

    def adjust_balance(self, delta: float) -> float:
        """
        Add (positive delta) or subtract (negative delta) from balance.
        Returns the new balance.

        Called by controller on entry: adjust_balance(-(cost + fee))
        NOT called on exit — close_position handles exit credit.
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

    # ═════════════════════════════════════════════════════
    #  POSITION MANAGEMENT
    # ═════════════════════════════════════════════════════

    def get_position(self, symbol: str) -> Optional[Dict]:
        """Get one open position by symbol. Returns None if absent."""
        with self._lock:
            pos = self._data.get("positions", {}).get(symbol)
            return deepcopy(pos) if pos else None

    def get_all_positions(self) -> Dict[str, Dict]:
        """Get all open positions keyed by symbol."""
        with self._lock:
            return deepcopy(self._data.get("positions", {}))

    def add_position(
        self,
        symbol: str,
        quantity: float,
        entry_price: float,
        metadata: Optional[Dict] = None,
    ) -> None:
        """
        Open a new position or average into an existing one.

        Metadata dict typically contains:
            stop_loss, take_profit, confidence, strategy, fees_paid, etc.
        """
        with self._lock:
            positions = self._data.setdefault("positions", {})

            if symbol in positions:
                # ── Average into existing position ────────────────
                existing = positions[symbol]
                old_qty = existing.get("quantity", 0)
                old_price = existing.get("entry_price",
                                         existing.get("avg_price", entry_price))

                new_qty = round(old_qty + quantity, 8)
                new_avg = (
                    round(
                        ((old_price * old_qty) + (entry_price * quantity))
                        / new_qty, 8
                    )
                    if new_qty > 0
                    else entry_price
                )

                existing["quantity"] = new_qty
                existing["entry_price"] = new_avg
                existing["avg_price"] = new_avg
                existing["updated_at"] = datetime.utcnow().isoformat()

                # Merge metadata without overwriting core fields
                if metadata:
                    protected = {"quantity", "entry_price", "avg_price",
                                 "opened_at", "symbol"}
                    for k, v in metadata.items():
                        if k not in protected:
                            existing[k] = v

                logger.info(
                    f"📈 Position averaged | {symbol} | "
                    f"Qty {old_qty}→{new_qty} | Avg ${new_avg:.6f}"
                )
            else:
                # ── New position ──────────────────────────────────
                position = {
                    "symbol": symbol,
                    "quantity": round(quantity, 8),
                    "entry_price": round(entry_price, 8),
                    "avg_price": round(entry_price, 8),
                    "opened_at": datetime.utcnow().isoformat(),
                }
                if metadata:
                    position.update(metadata)
                positions[symbol] = position

                logger.info(
                    f"📈 Position opened | {symbol} | "
                    f"Qty={quantity} @ ${entry_price:.6f}"
                )

            # Bump trade counters
            self._data["trades_today"] = (
                (self._data.get("trades_today", 0) or 0) + 1
            )
            self._data["trades_done_today"] = self._data["trades_today"]
            self._data["total_trades"] = (
                (self._data.get("total_trades", 0) or 0) + 1
            )

            self._persist()

    def close_position(
        self,
        symbol: str,
        net_pnl: float,
        exit_price: float = 0.0,
        partial_qty: Optional[float] = None,
    ) -> Optional[Dict]:
        """
        Close (or partially close) a position.

        Accounting:
            credit = entry_price * closed_qty + net_pnl
            This restores the original capital plus net profit/loss.

        Updates: balance, daily_pnl, trade_history, removes position.
        Returns the trade record dict, or None if position not found.
        """
        with self._lock:
            positions = self._data.get("positions", {})
            position = positions.get(symbol)

            if not position:
                logger.warning(
                    f"⚠️ close_position failed — no position for {symbol}"
                )
                return None

            pos_qty = position.get("quantity", 0)
            closed_qty = partial_qty if partial_qty else pos_qty
            closed_qty = min(closed_qty, pos_qty)  # Safety clamp
            remaining = round(pos_qty - closed_qty, 8)

            entry_price = position.get(
                "entry_price", position.get("avg_price", 0)
            )

            # ── Credit balance ────────────────────────────────────
            # Restores locked capital + applies profit/loss
            credit = round(entry_price * closed_qty + net_pnl, 8)
            balance = self._data.get("balance", 0.0) or 0.0
            self._data["balance"] = round(balance + credit, 8)

            # ── Daily PnL ─────────────────────────────────────────
            daily_pnl = self._data.get("daily_pnl", 0.0) or 0.0
            self._data["daily_pnl"] = round(daily_pnl + net_pnl, 8)

            # ── Trade record ──────────────────────────────────────
            trade_record = {
                "symbol": symbol,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": closed_qty,
                "pnl_amount": round(net_pnl, 8),
                "closed_at": datetime.utcnow().isoformat(),
                "opened_at": position.get("opened_at", ""),
                "strategy": position.get("strategy", "unknown"),
                "mode": position.get("mode", "PAPER"),
            }

            history: list = self._data.get("trade_history") or []
            history.append(trade_record)
            if len(history) > self.MAX_HISTORY:
                history = history[-self.MAX_HISTORY:]
            self._data["trade_history"] = history

            # ── Remove or reduce position ─────────────────────────
            if remaining <= 1e-8:
                del positions[symbol]
                logger.info(
                    f"📉 Position closed | {symbol} | "
                    f"PnL=${net_pnl:+.4f} | "
                    f"Balance=${self._data['balance']:.2f}"
                )
            else:
                position["quantity"] = remaining
                position["updated_at"] = datetime.utcnow().isoformat()
                logger.info(
                    f"📉 Partial close | {symbol} | "
                    f"Closed={closed_qty} Remaining={remaining} | "
                    f"PnL=${net_pnl:+.4f}"
                )

            self._persist()
            return trade_record

    def update_position(self, symbol: str, updates: Dict) -> bool:
        """
        Update fields on an open position in-place.

        Used by controller for:
        - Break-even stop adjustment
        - Trailing stop updates
        - Any SL/TP modification

        Returns True if position existed and was updated.
        """
        with self._lock:
            positions = self._data.get("positions", {})
            if symbol not in positions:
                return False

            positions[symbol].update(updates)
            positions[symbol]["updated_at"] = datetime.utcnow().isoformat()
            self._persist()

            logger.debug(
                f"✏️ Position updated | {symbol} | "
                f"Fields: {list(updates.keys())}"
            )
            return True

    # ═════════════════════════════════════════════════════
    #  TRADING GATES
    # ═════════════════════════════════════════════════════

    def can_trade(self) -> bool:
        """
        Master trade gate. Returns False if ANY condition blocks trading:
        - bot_active is False
        - kill_switch is True
        - Daily trade limit reached
        - Cooldown timer active
        """
        with self._lock:
            if not self._data.get("bot_active", True):
                return False

            if self._data.get("kill_switch", False):
                return False

            # Daily trade limit
            done = self._data.get("trades_today", 0) or 0
            limit = self._data.get("max_trades_per_day", 10) or 10
            if done >= limit:
                logger.warning(f"🚫 Daily limit: {done}/{limit}")
                return False

            # Cooldown
            cooldown = self._data.get("cooldown_until")
            if cooldown:
                try:
                    if datetime.utcnow() < datetime.fromisoformat(cooldown):
                        return False
                except (ValueError, TypeError):
                    pass

            return True

    def deactivate_bot(self) -> None:
        """Emergency: disable all trading immediately."""
        with self._lock:
            self._data["bot_active"] = False
            self._persist()
        logger.critical("🛑 Bot DEACTIVATED")

    def activate_bot(self) -> None:
        """Re-enable trading and clear kill switch."""
        with self._lock:
            self._data["bot_active"] = True
            self._data["kill_switch"] = False
            self._persist()
        logger.info("✅ Bot ACTIVATED")

    # ═════════════════════════════════════════════════════
    #  DAILY RESET
    # ═════════════════════════════════════════════════════

    def _reset_if_new_day(self) -> None:
        """Auto-reset daily counters when date rolls over."""
        today = str(date.today())
        last_reset = self._data.get("last_daily_reset")

        if last_reset == today:
            return

        balance = self._data.get("balance", 0)

        self._data.update({
            "daily_pnl": 0.0,
            "trades_today": 0,
            "trades_done_today": 0,
            "start_of_day_balance": balance,
            "last_daily_reset": today,
            "last_loss_guard_reset": today,
            "cooldown_until": None,
        })

        self._persist()
        logger.info(
            f"🔄 Daily reset | {today} | "
            f"Start balance=${balance:.2f}"
        )

    # ═════════════════════════════════════════════════════
    #  PERSISTENCE LAYER
    # ═════════════════════════════════════════════════════

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
        """Recursively sanitise for JSON."""
        if isinstance(obj, dict):
            return {k: self._make_serializable(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._make_serializable(v) for v in obj]
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, float):
            if obj != obj:  # NaN guard
                return 0.0
        return obj

    def _ensure_critical_keys(self, initial_balance: float) -> None:
        """
        Guarantee every key the system needs exists.
        Only sets keys that are MISSING — never overwrites
        persisted values from a previous session.
        """
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
            "total_trades": 0,
            "total_wins": 0,
            "total_losses": 0,
            "win_streak": 0,
            "loss_streak": 0,
            "consecutive_losses": 0,
            "equity_history": [],
            "rr_history": [],
            "equity_momentum": 0.0,
            "last_risk": None,
            "cooldown_until": None,
            "last_daily_reset": None,
            "last_loss_guard_reset": None,
        }

        changed = False
        for key, value in defaults.items():
            if key not in self._data:
                self._data[key] = value
                changed = True

        if changed:
            self._persist()

    # ═════════════════════════════════════════════════════
    #  DEBUG / EXPORT
    # ═════════════════════════════════════════════════════

    def export(self) -> Dict:
        """Full snapshot of current state (for debugging / Telegram)."""
        with self._lock:
            return deepcopy(self._data)

    def summary(self) -> Dict:
        """Compact summary for status commands."""
        with self._lock:
            return {
                "balance": self._data.get("balance", 0),
                "daily_pnl": self._data.get("daily_pnl", 0),
                "positions": len(self._data.get("positions", {})),
                "trades_today": self._data.get("trades_today", 0),
                "win_streak": self._data.get("win_streak", 0),
                "loss_streak": self._data.get("loss_streak", 0),
                "bot_active": self._data.get("bot_active", False),
                "kill_switch": self._data.get("kill_switch", False),
            }

    def __repr__(self) -> str:
        return (
            f"<StateManager | "
            f"${self.get('balance', 0):.2f} | "
            f"{len(self.get('positions', {}))} positions | "
            f"Active={self.get('bot_active')}>"
        )