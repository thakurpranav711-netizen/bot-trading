# app/market/snapshot.py

from app.market.analyzer import MarketState
from app.utils.time import format_timestamp


class MarketSnapshot:
    """
    Converts MarketState into human-readable messages
    """

    @staticmethod
    def build(state: MarketState) -> str:
        trend_emoji = {
            "bullish": "📈",
            "bearish": "📉",
            "sideways": "➡️",
        }.get(state.trend, "")

        volatility_emoji = {
            "low": "😴",
            "medium": "⚖️",
            "high": "🔥",
        }.get(state.volatility, "")

        message = (
            "📊 *Market Snapshot*\n\n"
            f"🪙 *Symbol:* {state.symbol}\n"
            f"💰 *Price:* {state.price:.2f}\n\n"
            f"{trend_emoji} *Trend:* {state.trend.capitalize()}\n"
            f"📐 *EMA 20:* {state.ema_20}\n"
            f"📐 *EMA 50:* {state.ema_50}\n"
            f"📊 *RSI:* {state.rsi}\n"
            f"{volatility_emoji} *Volatility:* {state.volatility.capitalize()}\n\n"
            f"🕒 _Updated at:_ {format_timestamp()}"
        )

        return message