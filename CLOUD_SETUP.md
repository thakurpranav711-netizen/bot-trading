# 🚀 Cloud Bot Setup Guide

## Step 1: SSH into Your Cloud Server
```bash
ssh your_cloud_user@your_cloud_ip
```

---

## Step 2: Delete Old Bot Completely
```bash
# Kill all bot processes
pkill -9 -f "python.*app.main" || true
pkill -9 -f "Trading" || true

# Remove old bot directory
rm -rf ~/bot-trading
rm -rf ~/TradingBot
rm -rf ~/trading-bot

echo "✅ Old bot removed completely"
```

---

## Step 3: Create Fresh Directory & Clone From GitHub
```bash
# Navigate to home directory
cd ~

# Clone the new bot from GitHub
git clone https://github.com/thakurpranav711-netizen/bot-trading.git

# Navigate into bot folder
cd bot-trading

echo "✅ New bot cloned successfully"
```

---

## Step 4: Set Up Python Virtual Environment
```bash
# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate

# Verify activation (you should see (venv) prefix)
python3 --version

echo "✅ Virtual environment created and activated"
```

---

## Step 5: Install All Requirements
```bash
# Make sure you're in the bot directory and venv is activated
cd ~/bot-trading
source venv/bin/activate

# Install all dependencies
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

echo "✅ All requirements installed"
```

---

## Step 6: Create .env File
```bash
# Navigate to config directory
cd ~/bot-trading/config

# Create .env file with template
cat > .env << 'EOF'
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

EOF

echo "✅ .env file created in config/"
echo ""
echo "⚠️  IMPORTANT: Edit the .env file with your actual credentials:"
echo "   nano ~/bot-trading/config/.env"
```

---

## Step 7: Configure Your Credentials
```bash
# Edit the .env file with your actual settings
nano ~/bot-trading/config/.env

# Or use vi/vim
vim ~/bot-trading/config/.env
```

**Update these fields:**
- `BINANCE_API_KEY` - Your Binance API key
- `BINANCE_API_SECRET` - Your Binance API secret
- `TELEGRAM_BOT_TOKEN` - Your Telegram bot token (optional)
- `TELEGRAM_CHAT_ID` - Your Telegram chat ID (optional)
- `COINS` - Trading pairs (default: BTC/USDT,ETH/USDT)

Press `Ctrl + O`, then `Enter`, then `Ctrl + X` to save in nano.

---

## Step 8: Test the Bot
```bash
cd ~/bot-trading
source venv/bin/activate

# Run bot in foreground to test (Ctrl+C to stop)
python3 -m app.main

# If it starts successfully without errors, proceed to Step 9
```

---

## Step 9: Run Bot in Background (Permanently)
```bash
cd ~/bot-trading
source venv/bin/activate

# Option A: Using nohup (simple)
nohup python3 -m app.main > ~/bot-trading/logs/bot.log 2>&1 &

# Option B: Using screen (recommended - allows easy reconnection)
screen -S trading-bot -d -m bash -c "source venv/bin/activate && python3 -m app.main"

# Option C: Using systemd service (most robust) - See Step 10

echo "✅ Bot is running in background"
```

---

## Step 10: (OPTIONAL) Set Up Systemd Service for Auto-Start
```bash
# Create systemd service file
sudo tee /etc/systemd/system/trading-bot.service > /dev/null << 'EOF'
[Unit]
Description=4-Brain Trading Bot
After=network.target

[Service]
Type=simple
User=your_cloud_username
WorkingDirectory=/home/your_cloud_username/bot-trading
Environment="PATH=/home/your_cloud_username/bot-trading/venv/bin"
ExecStart=/home/your_cloud_username/bot-trading/venv/bin/python3 -m app.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Replace 'your_cloud_username' with your actual username!

# Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable trading-bot
sudo systemctl start trading-bot

# Check status
sudo systemctl status trading-bot

echo "✅ Bot service installed and running"
```

---

## Useful Commands After Setup

### Check if bot is running:
```bash
# If using nohup/screen background
ps aux | grep -i "python.*app.main"

# If using systemd
sudo systemctl status trading-bot
```

### View live logs:
```bash
# For nohup
tail -f ~/bot-trading/logs/bot.log

# For systemd
sudo journalctl -u trading-bot -f
```

### Stop the bot:
```bash
# If using nohup
pkill -f "python.*app.main"

# If using screen
screen -S trading-bot -X quit

# If using systemd
sudo systemctl stop trading-bot
```

### Restart the bot:
```bash
# For systemd
sudo systemctl restart trading-bot

# For manual restart
pkill -f "python.*app.main"
source venv/bin/activate && nohup python3 -m app.main > logs/bot.log 2>&1 &
```

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'app'"
```bash
cd ~/bot-trading
source venv/bin/activate
python3 -m app.main  # Use -m flag
```

### "Permission denied" on systemd file
```bash
sudo chown root:root /etc/systemd/system/trading-bot.service
sudo chmod 644 /etc/systemd/system/trading-bot.service
```

### Bot crashes on startup
```bash
# Check logs for errors
cat ~/bot-trading/logs/bot.log

# Test with verbose output
python3 -m app.main 2>&1 | head -50
```

### State file issues
```bash
# Clear state to start fresh
rm ~/bot-trading/app/state/state.json

# Bot will recreate it on next start
```

---

## 📋 Quick Summary
1. ✅ Delete old bot: `rm -rf ~/bot-trading`
2. ✅ Clone new: `git clone https://github.com/thakurpranav711-netizen/bot-trading.git`
3. ✅ Setup venv: `python3 -m venv venv && source venv/bin/activate`
4. ✅ Install: `pip install -r requirements.txt`
5. ✅ Configure: `nano config/.env` (add your keys)
6. ✅ Test: `python3 -m app.main`
7. ✅ Run: `nohup python3 -m app.main > logs/bot.log 2>&1 &`

---

## 🔐 Security Notes
- Never commit your `.env` file to git
- Store API keys securely on your cloud server
- Use strong passwords for cloud SSH access
- Monitor bot logs regularly for errors
- GitHub token will auto-disable after 1 hour of exposure

---

**Need help?** Check the logs:
```bash
tail -f ~/bot-trading/logs/bot.log
```
