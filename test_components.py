#!/usr/bin/env python3
"""
Simple test to verify all components are working
"""
import sys
import os
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

print("=" * 60)
print("🧪 TRADING BOT COMPONENT TEST")
print("=" * 60)

# Clean up state file before testing
state_file = "app/state/state.json"
if os.path.exists(state_file):
    os.remove(state_file)
    print("\n[*] Cleaned up previous state file")
else:
    print("\n[*] Starting with fresh state")

tests_passed = 0
tests_failed = 0

# Test 1: Import all modules
print("\n[1] Testing imports...")
try:
    from app.utils.logger import get_logger
    from app.state.manager import StateManager
    from app.orchestrator.controller import BotController
    from app.exchange.paper import PaperExchange
    from app.strategies.scalping import ScalpingStrategy
    from app.risk.trade_limiter import TradeLimiter
    from app.risk.kill_switch import KillSwitch
    from app.risk.loss_guard import LossGuard
    from app.tg.auth import is_authorized
    print("✅ All imports successful")
    tests_passed += 1
except Exception as e:
    print(f"❌ Import failed: {e}")
    tests_failed += 1

# Test 2: Test StateManager
print("\n[2] Testing StateManager...")
try:
    state = StateManager()
    state.set("test_key", "test_value")
    assert state.get("test_key") == "test_value"
    assert state.get("balance") == 100000
    print("✅ StateManager working correctly")
    tests_passed += 1
except Exception as e:
    print(f"❌ StateManager test failed: {e}")
    tests_failed += 1

# Test 3: Test BotController
print("\n[3] Testing BotController...")
try:
    state = StateManager()
    controller = BotController(state)
    
    # Test start
    result = controller.start_bot()
    assert result == True
    assert controller.is_active() == True
    
    # Test stop
    result = controller.stop_bot()
    assert result == True
    assert controller.is_active() == False
    
    print("✅ BotController working correctly")
    tests_passed += 1
except Exception as e:
    print(f"❌ BotController test failed: {e}")
    tests_failed += 1

# Test 4: Test PaperExchange
print("\n[4] Testing PaperExchange...")
try:
    state = StateManager()
    exchange = PaperExchange(state)
    
    # Test get_price
    price = exchange.get_price("BTCUSDT")
    assert isinstance(price, float)
    assert price > 0
    
    # Test buy
    order = exchange.buy("BTCUSDT", 0.001)
    assert order["side"] == "BUY"
    
    # Test balance decreased
    balance = exchange.get_balance()
    assert balance < 100000
    
    print("✅ PaperExchange working correctly")
    tests_passed += 1
except Exception as e:
    print(f"❌ PaperExchange test failed: {e}")
    tests_failed += 1

# Test 5: Test ScalpingStrategy
print("\n[5] Testing ScalpingStrategy...")
try:
    state = StateManager()
    exchange = PaperExchange(state)
    strategy = ScalpingStrategy(exchange, state)
    
    # Test get_symbol
    symbol = strategy.get_symbol()
    assert symbol is not None
    
    # Test get_quantity
    qty = strategy.get_quantity()
    assert isinstance(qty, float)
    
    # Test analyze
    decision = strategy.analyze()
    assert decision in ["BUY", "SELL", "HOLD"]
    
    print("✅ ScalpingStrategy working correctly")
    tests_passed += 1
except Exception as e:
    print(f"❌ ScalpingStrategy test failed: {e}")
    tests_failed += 1

# Test 6: Test TradeLimiter
print("\n[6] Testing TradeLimiter...")
try:
    state = StateManager()
    limiter = TradeLimiter(state)
    
    # Test can_trade when limit not reached
    can_trade = limiter.can_trade()
    assert can_trade == True
    
    # Set trades to max
    state.set("max_trades_per_day", 5)
    state.set("trades_done_today", 5)
    can_trade = limiter.can_trade()
    assert can_trade == False
    
    print("✅ TradeLimiter working correctly")
    tests_passed += 1
except Exception as e:
    print(f"❌ TradeLimiter test failed: {e}")
    tests_failed += 1

# Test 7: Test KillSwitch
print("\n[7] Testing KillSwitch...")
try:
    state = StateManager()
    kill_switch = KillSwitch(state)
    
    assert kill_switch.is_active() == False
    kill_switch.activate()
    assert kill_switch.is_active() == True
    kill_switch.deactivate()
    assert kill_switch.is_active() == False
    
    print("✅ KillSwitch working correctly")
    tests_passed += 1
except Exception as e:
    print(f"❌ KillSwitch test failed: {e}")
    tests_failed += 1

# Test 8: Test LossGuard
print("\n[8] Testing LossGuard...")
try:
    state = StateManager()
    loss_guard = LossGuard(state, max_daily_loss=500)
    
    # Test when loss not exceeded
    state.set("daily_pnl", -100)
    can_trade = loss_guard.can_trade()
    assert can_trade == True
    
    # Test when loss exceeded
    state.set("daily_pnl", -600)
    can_trade = loss_guard.can_trade()
    assert can_trade == False
    
    print("✅ LossGuard working correctly")
    tests_passed += 1
except Exception as e:
    print(f"❌ LossGuard test failed: {e}")
    tests_failed += 1

# Summary
print("\n" + "=" * 60)
print(f"Tests Passed: {tests_passed}")
print(f"Tests Failed: {tests_failed}")
print("=" * 60)

if tests_failed == 0:
    print("✅ All tests passed! Bot is ready to run.")
    sys.exit(0)
else:
    print(f"❌ {tests_failed} test(s) failed. Please fix errors above.")
    sys.exit(1)
