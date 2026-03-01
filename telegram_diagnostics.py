#!/usr/bin/env python3
"""
🔍 Telegram Bot Diagnostics
Find why your Telegram bot is not running
"""

import os
import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ═════════════════════════════════════════════════════════════════
#  LOAD ENVIRONMENT
# ═════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "config" / ".env"

print("=" * 70)
print("🔍 TELEGRAM BOT DIAGNOSTICS")
print("=" * 70)

# Load .env file
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
    print(f"\n✅ .env file found: {ENV_FILE}")
else:
    print(f"\n❌ .env file NOT found: {ENV_FILE}")
    sys.exit(1)

# ═════════════════════════════════════════════════════════════════
#  CHECK REQUIRED ENVIRONMENT VARIABLES
# ═════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("📋 ENVIRONMENT VARIABLES CHECK")
print("=" * 70)

token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
allowed_users = os.getenv("TELEGRAM_ALLOWED_USERS", "").strip()
bot_username = os.getenv("TELEGRAM_BOT_USERNAME", "").strip()

checks = [
    ("TELEGRAM_BOT_TOKEN", token, True),
    ("TELEGRAM_CHAT_ID", chat_id, True),
    ("TELEGRAM_BOT_USERNAME", bot_username, False),
    ("TELEGRAM_ALLOWED_USERS", allowed_users, False),
]

print("\nRequired Variables:")
print("─" * 70)

missing_required = []
for var_name, value, is_required in checks:
    if value:
        # Mask sensitive values
        if "TOKEN" in var_name or var_name == "TELEGRAM_CHAT_ID":
            masked = f"{value[:10]}...{value[-4:]}" if len(value) > 14 else "***"
        else:
            masked = value
        print(f"  ✅ {var_name:30} = {masked}")
    else:
        status = "❌ MISSING (REQUIRED)" if is_required else "⚠️ MISSING (optional)"
        print(f"  {status:30} {var_name}")
        if is_required:
            missing_required.append(var_name)

# ═════════════════════════════════════════════════════════════════
#  CHECK PYTHON TELEGRAM LIBRARY
# ═════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("📦 TELEGRAM LIBRARY CHECK")
print("=" * 70)

try:
    import telegram
    ver = telegram.__version__
    print(f"  ✅ python-telegram-bot installed | Version: {ver}")
    
    # Check specific imports
    try:
        from telegram import Bot, Update
        from telegram.ext import Application, CommandHandler
        print(f"  ✅ Core Telegram imports working")
    except ImportError as e:
        print(f"  ❌ Import error: {e}")
except ImportError:
    print("  ❌ python-telegram-bot NOT installed")
    print("  Install with: pip install python-telegram-bot>=20.0")
    missing_required.append("python-telegram-bot")

# ═════════════════════════════════════════════════════════════════
#  ROOT CAUSE ANALYSIS
# ═════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("🔍 ROOT CAUSE ANALYSIS")
print("=" * 70)

if missing_required:
    print(f"\n🚨 FOUND {len(missing_required)} BLOCKING ISSUE(S):\n")
    for i, var in enumerate(missing_required, 1):
        print(f"  {i}. ❌ {var} is missing\n")
else:
    print("\n✅ All required variables are configured!")

# ═════════════════════════════════════════════════════════════════
#  RECOMMENDATIONS
# ═════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("💡 RECOMMENDATIONS")
print("=" * 70)

if not token:
    print("\n  ❌ TELEGRAM_BOT_TOKEN is missing:")
    print("     1. Create bot with @BotFather on Telegram")
    print("     2. Copy the token")
    print("     3. Add to config/.env: TELEGRAM_BOT_TOKEN=<your_token>")

if not chat_id:
    print("\n  ❌ TELEGRAM_CHAT_ID is missing (THIS IS BLOCKING YOUR BOT!):")
    print("     1. Message your bot: @ai_trading_pranav_bot")
    print("     2. Send: /start")
    print("     3. Bot will reply with your chat ID")
    print("     4. Add to config/.env: TELEGRAM_CHAT_ID=<your_id>")
    print("\n     OR use @userinfobot to get your chat ID")

if allowed_users and not chat_id:
    print("\n  ⚠️  You have TELEGRAM_ALLOWED_USERS but need TELEGRAM_CHAT_ID")
    print(f"     Your user ID appears to be: {allowed_users}")
    print(f"     Try setting: TELEGRAM_CHAT_ID={allowed_users}")

if not missing_required or (token and chat_id):
    print("\n  ✅ Configuration looks good!")
    print("  Next steps:")
    print("     1. Restart the bot: python -m app.main")
    print("     2. Open Telegram and message your bot")
    print("     3. You should see connection messages")

# ═════════════════════════════════════════════════════════════════
#  SUMMARY
# ═════════════════════════════════════════════════════════════════

print("\n" + "=" * 70)
print("📊 SUMMARY")
print("=" * 70)

status = "✅ READY" if not missing_required else "❌ BLOCKED"
print(f"\nStatus: {status}")
print(f"Blocking Issues: {len(missing_required)}")
print(f"Configuration File: {ENV_FILE}\n")

sys.exit(0 if not missing_required else 1)
