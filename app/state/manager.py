import json
import os
import tempfile
from datetime import date
from ..utils.logger import get_logger

logger = get_logger(__name__)


class StateManager:
    def __init__(self, file_path=None):
        if file_path is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            file_path = os.path.join(base_dir, "state", "state.json")

        self.file_path = file_path
        self.state = {}

        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        self.load()

    # =====================================================
    # LOAD / SAVE
    # =====================================================
    def load(self):
        if not os.path.exists(self.file_path):
            logger.warning("⚠️ State file not found, creating default state")
            self._create_default_state()
            self._atomic_save()
        else:
            try:
                with open(self.file_path, "r") as f:
                    self.state = json.load(f)
            except Exception:
                logger.exception("❌ Failed to load state file, recreating")
                self._create_default_state()
                self._atomic_save()

        self._daily_reset_if_needed()
        logger.info("🧠 State loaded successfully")

    def _atomic_save(self):
        tmp_fd, tmp_path = tempfile.mkstemp()
        with os.fdopen(tmp_fd, "w") as tmp_file:
            json.dump(self.state, tmp_file, indent=4, default=str)
        os.replace(tmp_path, self.file_path)

    def _create_default_state(self):
        self.state = {
            "bot_active": False,
            "balance": 100000.0,
            "initial_balance": 100000.0,
            "positions": {},
            "trade_history": [],
            "max_trades_per_day": 50,
            "trades_done_today": 0,
            "last_trade_date": str(date.today()),
            "symbol": "BTCUSDT",
            "trade_quantity": 0.001,
            "daily_pnl": 0.0,
            "total_pnl": 0.0,
            "wins": 0,
            "losses": 0,
            "telegram_chat_id": None,
        }

    # =====================================================
    # BASIC GET / SET
    # =====================================================
    def get(self, key, default=None):
        return self.state.get(key, default)

    def set(self, key, value):
        self.state[key] = value
        self._atomic_save()

    def increment(self, key, amount=1):
        self.state[key] = self.state.get(key, 0) + amount
        self._atomic_save()

    # =====================================================
    # BOT STATUS
    # =====================================================
    def activate_bot(self):
        self.state["bot_active"] = True
        self._atomic_save()

    def deactivate_bot(self):
        self.state["bot_active"] = False
        self._atomic_save()

    # =====================================================
    # DAILY RESET
    # =====================================================
    def _daily_reset_if_needed(self):
        today = str(date.today())
        if self.state.get("last_trade_date") != today:
            logger.info("🔄 New day detected, resetting counters")
            self.state["trades_done_today"] = 0
            self.state["daily_pnl"] = 0.0
            self.state["last_trade_date"] = today
            self._atomic_save()

    # =====================================================
    # BALANCE
    # =====================================================
    def update_balance(self, amount):
        self.state["balance"] += amount
        self._atomic_save()

    # =====================================================
    # POSITIONS
    # =====================================================
    def add_position(self, symbol, qty, price, extra=None):
        investment = qty * price

        if self.state["balance"] < investment:
            logger.warning("❌ Not enough balance to open position")
            return False

        # Deduct capital
        self.state["balance"] -= investment

        self.state["positions"][symbol] = {
            "quantity": qty,
            "avg_price": price,
            "invested": investment,
            **(extra or {})
        }

        self._atomic_save()
        return True

    def remove_position(self, symbol):
        if symbol in self.state["positions"]:
            del self.state["positions"][symbol]
            self._atomic_save()

    def get_position(self, symbol):
        return self.state["positions"].get(symbol)

    # =====================================================
    # TRADE RECORDING
    # =====================================================
    def record_trade(self, trade: dict):
        """
        trade must contain:
        - pnl_amount
        - quantity
        - exit_price
        - entry_price
        """

        invested = trade["quantity"] * trade["entry_price"]
        returned_capital = trade["quantity"] * trade["exit_price"]

        pnl = trade["pnl_amount"]

        # Return full capital
        self.state["balance"] += returned_capital

        # Track history
        self.state["trade_history"].append(trade)

        # Stats
        self.state["daily_pnl"] += pnl
        self.state["total_pnl"] += pnl

        if pnl > 0:
            self.state["wins"] += 1
        else:
            self.state["losses"] += 1

        self._atomic_save()

    # =====================================================
    # PERFORMANCE METRICS
    # =====================================================
    def get_win_rate(self):
        total = self.state["wins"] + self.state["losses"]
        if total == 0:
            return 0.0
        return round((self.state["wins"] / total) * 100, 2)

    def get_total_trades(self):
        return self.state["wins"] + self.state["losses"]

    def get_equity(self):
        return self.state["balance"]

    # =====================================================
    # TRADE LIMITS
    # =====================================================
    def can_trade(self) -> bool:
        return self.state["trades_done_today"] < self.state["max_trades_per_day"]

    def increment_trade_count(self):
        if not self.can_trade():
            logger.warning("⚠️ Max trades per day reached")
            return False

        self.state["trades_done_today"] += 1
        self._atomic_save()
        return True