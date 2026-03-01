#!/bin/bash
# 🚀 Automated Cloud Bot Setup Script
# Run this on your cloud server to completely set up the trading bot

set -e  # Exit on error

echo "════════════════════════════════════════════════════════════════"
echo "             🚀 TRADING BOT CLOUD SETUP SCRIPT 🚀"
echo "════════════════════════════════════════════════════════════════"

# ─────────────────────────────────────────────────────────────────
# STEP 1: Kill old bot processes
# ─────────────────────────────────────────────────────────────────
echo ""
echo "📍 STEP 1: Killing old bot processes..."
pkill -9 -f "python.*app.main" 2>/dev/null || true
pkill -9 -f "Trading" 2>/dev/null || true
sleep 2
echo "✅ Old processes killed"

# ─────────────────────────────────────────────────────────────────
# STEP 2: Remove old bot directories
# ─────────────────────────────────────────────────────────────────
echo ""
echo "📍 STEP 2: Removing old bot directories..."
rm -rf ~/bot-trading
rm -rf ~/TradingBot
rm -rf ~/trading-bot
echo "✅ Old directories removed"

# ─────────────────────────────────────────────────────────────────
# STEP 3: Clone new bot from GitHub
# ─────────────────────────────────────────────────────────────────
echo ""
echo "📍 STEP 3: Cloning new bot from GitHub..."
cd ~
git clone https://github.com/thakurpranav711-netizen/bot-trading.git
cd bot-trading
echo "✅ New bot cloned"

# ─────────────────────────────────────────────────────────────────
# STEP 4: Create and activate virtual environment
# ─────────────────────────────────────────────────────────────────
echo ""
echo "📍 STEP 4: Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
python3 --version
echo "✅ Virtual environment created and activated"

# ─────────────────────────────────────────────────────────────────
# STEP 5: Install dependencies
# ─────────────────────────────────────────────────────────────────
echo ""
echo "📍 STEP 5: Installing requirements (this may take a minute)..."
pip install --upgrade pip setuptools wheel > /tmp/pip_upgrade.log 2>&1
pip install -r requirements.txt > /tmp/pip_install.log 2>&1
echo "✅ All requirements installed"

# ─────────────────────────────────────────────────────────────────
# STEP 6: Create .env file
# ─────────────────────────────────────────────────────────────────
echo ""
echo "📍 STEP 6: Creating .env configuration file..."
mkdir -p ~/bot-trading/config

cat > ~/bot-trading/config/.env << 'ENVFILE'
# ═══════════════════════════════════════════════════════════
# TRADING BOT CONFIGURATION
# ═══════════════════════════════════════════════════════════

# ── EXCHANGE SETTINGS ─────────────────────────────────────
# Mode: PAPER (paper trading) or LIVE (real trading)
EXCHANGE_MODE=PAPER

# ── BINANCE API (for LIVE trading) ───────────────────────
BINANCE_API_KEY=your_binance_api_key_here
BINANCE_API_SECRET=your_binance_api_secret_here

# ── TRADING PARAMETERS ────────────────────────────────────
COINS=BTC/USDT,ETH/USDT
INTERVAL=300
BASE_RISK=0.01
MAX_DAILY_DRAWDOWN=0.05

# ── TELEGRAM BOT SETTINGS ─────────────────────────────────
# Leave empty to run without Telegram notifications
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here

# ── LOGGING ───────────────────────────────────────────────
LOG_LEVEL=INFO
ENVFILE

echo "✅ .env file created at ~/bot-trading/config/.env"

# ─────────────────────────────────────────────────────────────────
# STEP 7: Create logs directory
# ─────────────────────────────────────────────────────────────────
echo ""
echo "📍 STEP 7: Setting up log directory..."
mkdir -p ~/bot-trading/logs
touch ~/bot-trading/logs/bot.log
echo "✅ Log directory ready"

# ─────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "                    ✅ SETUP COMPLETE! ✅"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "📋 NEXT STEPS:"
echo ""
echo "1. CONFIGURE YOUR CREDENTIALS:"
echo "   nano ~/bot-trading/config/.env"
echo ""
echo "   Required fields:"
echo "   - BINANCE_API_KEY (if using LIVE mode)"
echo "   - BINANCE_API_SECRET (if using LIVE mode)"
echo "   - TELEGRAM_BOT_TOKEN (optional, for notifications)"
echo "   - TELEGRAM_CHAT_ID (optional, for notifications)"
echo ""
echo "2. TEST THE BOT:"
echo "   cd ~/bot-trading"
echo "   source venv/bin/activate"
echo "   python3 -m app.main"
echo "   (Press Ctrl+C to stop)"
echo ""
echo "3. RUN IN BACKGROUND:"
echo ""
echo "   Option A - Using nohup (simple):"
echo "   cd ~/bot-trading"
echo "   nohup python3 -m app.main > logs/bot.log 2>&1 &"
echo ""
echo "   Option B - Using screen (recommended):"
echo "   screen -S trading-bot -d -m bash -c 'cd ~/bot-trading && source venv/bin/activate && python3 -m app.main'"
echo ""
echo "4. CHECK BOT STATUS:"
echo "   ps aux | grep 'python.*app.main'"
echo "   tail -f ~/bot-trading/logs/bot.log"
echo ""
echo "════════════════════════════════════════════════════════════════"
echo ""
