from abc import ABC, abstractmethod


class ExchangeClient(ABC):
    """
    Abstract Exchange Interface
    Every exchange (paper / binance / bybit) must follow this
    """

    @abstractmethod
    def get_price(self, symbol: str) -> float:
        """
        Get latest market price
        """
        pass

    @abstractmethod
    def buy(self, symbol: str, quantity: float) -> dict:
        """
        Place buy order
        """
        pass

    @abstractmethod
    def sell(self, symbol: str, quantity: float) -> dict:
        """
        Place sell order
        """
        pass

    @abstractmethod
    def get_balance(self) -> float:
        """
        Get available balance
        """
        pass