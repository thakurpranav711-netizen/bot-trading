# app/market/snapshot.py

"""
Market Snapshot — Production Grade

Converts MarketState into human-readable Telegram messages.

Provides multiple snapshot formats:
- build()          Full market snapshot (all indicators)
- build_short()    Compact one-liner for quick status
- build_position() Position snapshot with PnL
- build_brain()    4-Brain decision summary

FIXES vs original:
- format_timestamp() imported from non-existent module → crash
- All MarketState field accesses were unguarded → crash on None
- Only showed 5 fields — missing RSI, MACD, BB, sentiment, patterns
- No position snapshot builder
- No Brain 4 AI section
"""

from datetime import datetime
from typing import Optional, Dict, Any
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────
#  SAFE FIELD HELPERS
# ─────────────────────────────────────────────────────────

def _f(value, decimals: int = 2, prefix: str = "", suffix: str = "") -> str:
    """
    Format a float safely.

    Returns "N/A" if value is None, 0, or invalid.
    Never crashes on missing MarketState fields.
    """
    if value is None:
        return "N/A"
    try:
        return f"{prefix}{float(value):.{decimals}f}{suffix}"
    except (ValueError, TypeError):
        return "N/A"


def _pct(value, decimals: int = 2) -> str:
    """Format as percentage string."""
    if value is None:
        return "N/A"
    try:
        return f"{float(value) * 100:.{decimals}f}%"
    except (ValueError, TypeError):
        return "N/A"


def _s(value, fallback: str = "N/A") -> str:
    """Safely convert any value to string."""
    if value is None:
        return fallback
    return str(value)


def _now() -> str:
    """Current UTC time formatted for Telegram messages."""
    return datetime.utcnow().strftime("%H:%M:%S UTC")


# ─────────────────────────────────────────────────────────
#  EMOJI MAPS
# ─────────────────────────────────────────────────────────

_TREND_EMOJI = {
    "bullish":       "📈",
    "strong_bull":   "🚀",
    "bearish":       "📉",
    "strong_bear":   "🩸",
    "sideways":      "➡️",
    "neutral":       "➡️",
}

_VOLATILITY_EMOJI = {
    "low":     "😴",
    "medium":  "⚖️",
    "normal":  "⚖️",
    "high":    "🔥",
    "extreme": "💥",
}

_SENTIMENT_EMOJI = {
    "bullish":  "🟢",
    "bearish":  "🔴",
    "neutral":  "⚪",
}

_SIGNAL_EMOJI = {
    "BUY":  "🟢",
    "SELL": "🔴",
    "HOLD": "⚪",
}

_REGIME_EMOJI = {
    "trending":   "➡️",
    "ranging":    "↔️",
    "volatile":   "🌪️",
    "breakout":   "💥",
}


# ─────────────────────────────────────────────────────────
#  MAIN SNAPSHOT CLASS
# ─────────────────────────────────────────────────────────

class MarketSnapshot:
    """
    Converts MarketState into human-readable Telegram messages.

    All methods are static and handle None fields gracefully —
    a missing field shows "N/A" instead of crashing.
    """

    # ═════════════════════════════════════════════════════
    #  FULL SNAPSHOT
    # ═════════════════════════════════════════════════════

    @staticmethod
    def build(state) -> str:
        """
        Full market snapshot — all indicators, sentiment, patterns.

        Used for /status and hourly reports.

        Args:
            state: MarketState instance (or None → returns error message)

        Returns:
            Formatted Telegram markdown string
        """
        if state is None:
            return "⚠️ *Market Snapshot*\n\n_No market data available yet._"

        try:
            # ── Emoji lookups ─────────────────────────────────────
            trend_str = _s(state.trend, "unknown").lower()
            trend_emoji = _TREND_EMOJI.get(trend_str, "📊")

            vol_str = _s(
                getattr(state, "volatility_regime", None)
                or getattr(state, "volatility", None),
                "unknown"
            ).lower()
            vol_emoji = _VOLATILITY_EMOJI.get(vol_str, "⚖️")

            sentiment_str = _s(
                getattr(state, "sentiment", None), "neutral"
            ).lower()
            sent_emoji = _SENTIMENT_EMOJI.get(sentiment_str, "⚪")

            regime_str = _s(
                getattr(state, "regime", None), ""
            ).lower()
            regime_emoji = _REGIME_EMOJI.get(regime_str, "📊")

            # ── Price ─────────────────────────────────────────────
            price = getattr(state, "price", None)
            symbol = _s(getattr(state, "symbol", None), "Unknown")

            # ── Indicators ────────────────────────────────────────
            rsi = getattr(state, "rsi", None)
            rsi_str = _f(rsi, 1)
            rsi_emoji = (
                "🔴" if rsi and rsi > 70
                else "🟢" if rsi and rsi < 30
                else "⚪"
            )

            macd_hist = getattr(state, "macd_histogram", None)
            macd_emoji = (
                "🟢" if macd_hist and macd_hist > 0
                else "🔴" if macd_hist and macd_hist < 0
                else "⚪"
            )

            bb_pct = getattr(state, "bb_percent_b", None)
            bb_str = _f(bb_pct, 3)

            # ── Volume ────────────────────────────────────────────
            vol_spike = getattr(state, "volume_spike", False)
            vol_pressure = getattr(state, "volume_pressure", None)
            vol_spike_str = "⚡ YES" if vol_spike else "No"

            # ── Support / Resistance ──────────────────────────────
            support = getattr(state, "support_level", None)
            resistance = getattr(state, "resistance_level", None)

            # ── Chart Pattern ─────────────────────────────────────
            pattern = getattr(state, "chart_pattern", None)
            pattern_str = "None detected"
            if pattern and isinstance(pattern, dict):
                p_name = pattern.get("pattern_name", "Unknown")
                p_sig = pattern.get("signal", "HOLD")
                p_conf = pattern.get("confidence", 0)
                p_emoji = _SIGNAL_EMOJI.get(p_sig.upper(), "⚪")
                pattern_str = f"{p_emoji} {p_name} ({p_sig} {p_conf}%)"

            # ── AI Prediction ─────────────────────────────────────
            ai = getattr(state, "ai_prediction", None)
            ai_str = "No prediction"
            if ai and isinstance(ai, dict):
                a_sig = ai.get("signal", "HOLD")
                a_conf = ai.get("confidence", 0)
                a_reason = ai.get("reason", "")[:50]
                a_emoji = _SIGNAL_EMOJI.get(a_sig.upper(), "⚪")
                ai_str = f"{a_emoji} {a_sig} ({a_conf}%) — {a_reason}"

            # ── Sentiment ─────────────────────────────────────────
            sent_score = getattr(state, "sentiment_score", None)
            sent_score_str = _f(sent_score, 3)

            # ── Build message ─────────────────────────────────────
            lines = [
                "📊 *Market Snapshot*",
                "━━━━━━━━━━━━━━━━━━━━━",
                f"🪙 *Symbol:* `{symbol}`",
                f"💵 *Price:* `${_f(price, 4)}`",
                "",
                "📐 *Trend & Regime*",
                f"  {trend_emoji} Trend: {trend_str.capitalize()}",
                f"  {regime_emoji} Regime: {regime_str.capitalize() or 'N/A'}",
                f"  {vol_emoji} Volatility: {vol_str.capitalize()}",
                f"  Vol %: {_pct(getattr(state, 'volatility_pct', None), 3)}",
                "",
                "📈 *Technical Indicators*",
                f"  {rsi_emoji} RSI(14): `{rsi_str}`",
                f"  EMA 20: `${_f(getattr(state, 'ema_20', None), 4)}`",
                f"  EMA 50: `${_f(getattr(state, 'ema_50', None), 4)}`",
                f"  EMA 200: `${_f(getattr(state, 'ema_200', None), 4)}`",
                f"  {macd_emoji} MACD Hist: `{_f(macd_hist, 6)}`",
                f"  BB %B: `{bb_str}`",
                "",
                "💹 *Volume & Momentum*",
                f"  Volume Spike: {vol_spike_str}",
                f"  Vol Pressure: `{_f(vol_pressure, 3)}`",
                f"  Momentum: `{_f(getattr(state, 'momentum_strength', None), 6)}`",
                f"  Trend Strength: `{_f(getattr(state, 'trend_strength', None), 4)}`",
                "",
                "🏦 *Support / Resistance*",
                f"  Support: `${_f(support, 4)}`",
                f"  Resistance: `${_f(resistance, 4)}`",
                f"  Liquidity: `{_f(getattr(state, 'liquidity_score', None), 3)}`",
                "",
                "🧩 *Signals*",
                f"  {sent_emoji} Sentiment: {sentiment_str.capitalize()} ({sent_score_str})",
                f"  📐 Pattern: {pattern_str}",
                f"  🤖 AI (Brain 4): {ai_str}",
                "",
                f"🕒 _Updated: {_now()}_",
            ]

            return "\n".join(lines)

        except Exception as e:
            logger.exception(f"❌ MarketSnapshot.build() error: {e}")
            return (
                "⚠️ *Market Snapshot*\n\n"
                f"_Error building snapshot: {e}_"
            )

    # ═════════════════════════════════════════════════════
    #  SHORT SNAPSHOT (one-liner)
    # ═════════════════════════════════════════════════════

    @staticmethod
    def build_short(state) -> str:
        """
        Compact single-line market summary.

        Used inline in trade notifications and status headers.

        Example:
            BTC/USDT $65,432 | 📈 Bullish | RSI 45.2 | 🔥 High Vol
        """
        if state is None:
            return "No market data"

        try:
            symbol = _s(getattr(state, "symbol", None), "???")
            price = getattr(state, "price", None)
            trend = _s(getattr(state, "trend", None), "unknown")
            trend_emoji = _TREND_EMOJI.get(trend.lower(), "📊")

            rsi = getattr(state, "rsi", None)
            rsi_str = f"RSI {_f(rsi, 1)}" if rsi else ""

            vol = _s(
                getattr(state, "volatility_regime", None)
                or getattr(state, "volatility", None),
                ""
            )
            vol_emoji = _VOLATILITY_EMOJI.get(vol.lower(), "")
            vol_str = f"{vol_emoji} {vol.capitalize()}" if vol else ""

            parts = [
                f"`{symbol}` ${_f(price, 4)}",
                f"{trend_emoji} {trend.capitalize()}",
            ]
            if rsi_str:
                parts.append(rsi_str)
            if vol_str:
                parts.append(vol_str)

            return " | ".join(parts)

        except Exception as e:
            logger.warning(f"⚠️ MarketSnapshot.build_short() error: {e}")
            return "Market data unavailable"

    # ═════════════════════════════════════════════════════
    #  POSITION SNAPSHOT
    # ═════════════════════════════════════════════════════

    @staticmethod
    def build_position(position: Dict, current_price: float = None) -> str:
        """
        Format an open position with live PnL.

        Args:
            position:      Position dict from StateManager
            current_price: Current market price (for unrealized PnL)

        Returns:
            Formatted position string for Telegram
        """
        if not position:
            return "_No open position_"

        try:
            symbol = _s(position.get("symbol"), "Unknown")
            action = _s(position.get("action"), "BUY").upper()
            entry = position.get("entry_price") or position.get("avg_price", 0)
            qty = position.get("quantity", 0)
            sl = position.get("stop_loss")
            tp = position.get("take_profit")
            strategy = _s(position.get("strategy"), "N/A")
            entry_time = _s(position.get("entry_time"), "")

            action_emoji = "🟢" if action == "BUY" else "🔴"

            # ── Unrealized PnL ────────────────────────────────────
            upnl_str = "N/A"
            upnl_pct_str = ""
            if current_price and entry and qty:
                try:
                    if action == "BUY":
                        upnl = (float(current_price) - float(entry)) * float(qty)
                    else:
                        upnl = (float(entry) - float(current_price)) * float(qty)
                    upnl_pct = (upnl / (float(entry) * float(qty))) * 100
                    upnl_emoji = "🟢" if upnl >= 0 else "🔴"
                    upnl_str = f"{upnl_emoji} ${upnl:+.4f}"
                    upnl_pct_str = f" ({upnl_pct:+.2f}%)"
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

            # ── R:R progress ──────────────────────────────────────
            rr_str = "N/A"
            if entry and sl and tp:
                try:
                    risk = abs(float(entry) - float(sl))
                    reward = abs(float(tp) - float(entry))
                    if risk > 0:
                        rr = reward / risk
                        rr_str = f"{rr:.2f}R"
                except (TypeError, ValueError, ZeroDivisionError):
                    pass

            lines = [
                f"{action_emoji} *Position: {symbol}*",
                "━━━━━━━━━━━━━━━━━━━━━",
                f"  Side: `{action}`",
                f"  Entry: `${_f(entry, 4)}`",
                f"  Qty: `{_f(qty, 8)}`",
                f"  Current: `${_f(current_price, 4)}`",
                f"  Unrealized PnL: {upnl_str}{upnl_pct_str}",
                "",
                f"  Stop Loss: `${_f(sl, 4)}`",
                f"  Take Profit: `${_f(tp, 4)}`",
                f"  R:R: `{rr_str}`",
                "",
                f"  Strategy: `{strategy}`",
            ]

            if entry_time:
                # Format entry time nicely
                try:
                    dt = datetime.fromisoformat(entry_time)
                    time_str = dt.strftime("%m/%d %H:%M UTC")
                    lines.append(f"  Opened: `{time_str}`")
                except ValueError:
                    lines.append(f"  Opened: `{entry_time[:16]}`")

            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"⚠️ MarketSnapshot.build_position() error: {e}")
            return f"_Position data error: {e}_"

    # ═════════════════════════════════════════════════════
    #  BRAIN DECISION SNAPSHOT
    # ═════════════════════════════════════════════════════

    @staticmethod
    def build_brain(
        state,
        decision: Dict = None,
    ) -> str:
        """
        Format the 4-Brain decision engine output.

        Args:
            state:    MarketState instance
            decision: Decision dict from controller._run_decision_engine()

        Returns:
            Formatted brain summary string for Telegram
        """
        if state is None:
            return "_No market state for brain summary_"

        try:
            symbol = _s(getattr(state, "symbol", None), "???")
            price = getattr(state, "price", None)

            lines = [
                "🧠 *4-Brain Decision Engine*",
                "━━━━━━━━━━━━━━━━━━━━━",
                f"🪙 Symbol: `{symbol}`",
                f"💵 Price: `${_f(price, 4)}`",
            ]

            # ── Brain signals from MarketState ────────────────────
            lines.append("")
            lines.append("*Brain Signals:*")

            # Brain 1: Indicators
            indicators = getattr(state, "indicators", None) or {}
            rsi = getattr(state, "rsi", None)
            lines.append(
                f"  Brain1 indicators: "
                f"RSI={_f(rsi, 1)} | "
                f"MACD={_s(indicators.get('macd_cross'), 'N/A')} | "
                f"EMA={_s(indicators.get('ema_cross'), 'N/A')}"
            )

            # Brain 2: Sentiment
            sent_score = getattr(state, "sentiment_score", None)
            sentiment = _s(getattr(state, "sentiment", None), "neutral")
            lines.append(
                f"  Brain2 sentiment: "
                f"{_SENTIMENT_EMOJI.get(sentiment.lower(), '⚪')} "
                f"{sentiment.capitalize()} ({_f(sent_score, 3)})"
            )

            # Brain 3: Chart Pattern
            pattern = getattr(state, "chart_pattern", None)
            if pattern and isinstance(pattern, dict):
                p_name = pattern.get("pattern_name", "Unknown")
                p_sig = pattern.get("signal", "HOLD")
                p_conf = pattern.get("confidence", 0)
                p_emoji = _SIGNAL_EMOJI.get(p_sig.upper(), "⚪")
                lines.append(
                    f"  Brain3 chart: "
                    f"{p_emoji} {p_name} ({p_sig} {p_conf}%)"
                )
            else:
                lines.append("  Brain3 chart: ⚪ No pattern")

            # Brain 4: AI
            ai = getattr(state, "ai_prediction", None)
            if ai and isinstance(ai, dict):
                a_sig = ai.get("signal", "HOLD")
                a_conf = ai.get("confidence", 0)
                a_emoji = _SIGNAL_EMOJI.get(a_sig.upper(), "⚪")
                lines.append(
                    f"  Brain4 ai: "
                    f"{a_emoji} {a_sig} ({a_conf}%)"
                )
            else:
                lines.append("  Brain4 ai: ⚪ No prediction")

            # ── Decision summary ──────────────────────────────────
            if decision:
                lines.append("")
                lines.append("*Decision:*")

                final = _s(decision.get("final_signal"), "HOLD").upper()
                confidence = _s(decision.get("confidence"), "LOW")
                trade = decision.get("trade", False)
                w_buy = decision.get("weighted_buy", 0)
                w_sell = decision.get("weighted_sell", 0)

                final_emoji = _SIGNAL_EMOJI.get(final, "⚪")
                trade_str = "✅ YES" if trade else "❌ NO"

                lines += [
                    f"  Weighted Buy: `{_f(w_buy, 1)}`",
                    f"  Weighted Sell: `{_f(w_sell, 1)}`",
                    f"  Signal: {final_emoji} `{final}`",
                    f"  Confidence: `{confidence}`",
                    f"  Execute: {trade_str}",
                ]

            lines.append("")
            lines.append(f"🕒 _Updated: {_now()}_")

            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"⚠️ MarketSnapshot.build_brain() error: {e}")
            return f"_Brain snapshot error: {e}_"

    # ═════════════════════════════════════════════════════
    #  RISK SNAPSHOT
    # ═════════════════════════════════════════════════════

    @staticmethod
    def build_risk(risk_report: Dict) -> str:
        """
        Format risk management report.

        Args:
            risk_report: Dict from AdaptiveRiskManager.get_risk_report()

        Returns:
            Formatted risk summary string for Telegram
        """
        if not risk_report:
            return "_No risk data available_"

        try:
            lines = [
                "⚠️ *Risk Management Report*",
                "━━━━━━━━━━━━━━━━━━━━━",
            ]

            for key, value in risk_report.items():
                label = key.replace("_", " ").capitalize()
                if isinstance(value, float):
                    val_str = f"`{value:.4f}`"
                elif isinstance(value, bool):
                    val_str = "✅ Yes" if value else "❌ No"
                else:
                    val_str = f"`{_s(value)}`"
                lines.append(f"  {label}: {val_str}")

            lines.append(f"\n🕒 _Updated: {_now()}_")
            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"⚠️ MarketSnapshot.build_risk() error: {e}")
            return f"_Risk snapshot error: {e}_"