from app.market.data_feed import MarketDataFeed
from app.market.analyzer import MarketAnalyzer
from app.market.snapshot import MarketSnapshot

print("🚀 Running market pipeline test...")

def run_test():
    feed = MarketDataFeed("BTCUSDT")
    data = feed.fetch_market_data()

    analyzer = MarketAnalyzer("BTCUSDT")
    state = analyzer.analyze(data)

    message = MarketSnapshot.build(state)

    print("\n===== MARKET STATE =====")
    print(state)

    print("\n===== SNAPSHOT MESSAGE =====")
    print(message)

if __name__ == "__main__":
    run_test()