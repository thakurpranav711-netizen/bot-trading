# TradingBot Safe Update Workflow

**Purpose:** Update code locally, push to GitHub, deploy on VPS, and restart bot safely without Telegram conflicts.

---

## A) LOCAL MACHINE - Push Changes to GitHub

### Step 1: Check Status
```bash
cd ~/Desktop/TradingBot
git status
```
**Expected:** Shows modified files in red/green.

### Step 2: Stage Changes
```bash
# Stage all changes
git add .

# OR stage specific files
git add app/ config/ requirements.txt
```

### Step 3: Verify Staged Changes
```bash
git status
```
**Expected:** Files shown in green (staged).

### Step 4: Commit Changes
```bash
git commit -m "Update trading bot: fix Telegram conflicts, improve stability"
```

### Step 5: Push to GitHub
```bash
git push origin main
```
**Expected:** Output shows "X files changed, Y insertions, Z deletions".

### Optional: Verify Push
```bash
git log --oneline -5
```

---

## B) VPS - Deploy & Restart Bot

### Prerequisites (One-Time)
Ensure bot runs under systemd OR manually. Choose your method below.

---

## Option 1: Systemd Service (Recommended for Production)

### Step 1: SSH to VPS
```bash
ssh user@your_vps_ip
```

### Step 2: Navigate to Project
```bash
cd /path/to/TradingBot
```

### Step 3: Pull Latest Code
```bash
git pull origin main
```
**Expected:** "Already up to date" OR "X files changed, Y insertions".

### Step 4: Activate Virtual Environment
```bash
source venv/bin/activate
```
**Expected:** Prompt shows `(venv)`.

### Step 5: Update Dependencies
```bash
pip install -r requirements.txt
```
**Expected:** "Successfully installed X" or "Requirement already satisfied".

### Step 6: Kill Old Process (Extra Safety)
```bash
pkill -9 -f "python3 -m app.main"
sleep 2
```
**Explanation:** Force-kill any hanging processes. Wait 2 seconds for cleanup.

### Step 7: Remove Stale Lock File
```bash
rm -f .bot_lock
```
**Explanation:** Prevents "lock file from old process" errors.

### Step 8: Restart Bot via Systemd
```bash
sudo systemctl restart tradingbot
```

### Step 9: Check Service Status
```bash
sudo systemctl status tradingbot
```
**Expected Output:**
```
● tradingbot.service - Trading Bot
   Loaded: loaded (/etc/systemd/system/tradingbot.service; enabled)
   Active: active (running) since ...
```

### Step 10: Verify Process is Running
```bash
ps aux | grep "python3 -m app.main" | grep -v grep
```
**Expected:** Shows single line with bot process.

### Step 11: Check Logs (Last 50 Lines)
```bash
sudo journalctl -u tradingbot -n 50 -f
```
**Explanation:** `-n 50` = last 50 lines, `-f` = follow (live tail). Press `Ctrl+C` to exit.

### Step 12: Check for Startup Errors
```bash
sudo journalctl -u tradingbot --since "5 minutes ago" | grep -i error
```
**Expected:** No error lines (if bot started cleanly).

---

## Option 2: Manual Start (For Testing/Development)

### Step 1-7: Same as Above (Pull, venv, pip install, kill old, remove lock)
```bash
cd /path/to/TradingBot
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
pkill -9 -f "python3 -m app.main"
sleep 2
rm -f .bot_lock
```

### Step 8: Start Bot in Background (Detached)
```bash
nohup python3 -m app.main > logs/bot.log 2>&1 &
```
**Explanation:** 
- `nohup` = ignore hangup signals
- `> logs/bot.log` = redirect stdout
- `2>&1` = redirect stderr to stdout
- `&` = run in background

### Step 9: Verify Process
```bash
ps aux | grep "python3 -m app.main" | grep -v grep
```
**Expected:** Shows bot process.

### Step 10: Check Logs (Live Tail)
```bash
tail -f logs/bot.log
```
**Expected:** Shows initialization messages, trading cycles. Press `Ctrl+C` to exit.

### Step 11: Check for Errors (Last 50 Lines)
```bash
tail -50 logs/bot.log | grep -i error
```

---

## C) Post-Deployment Verification Checklist

### Critical Checks
```bash
# 1. Verify single process exists
ps aux | grep "python3 -m app.main" | grep -v grep

# 2. Check lock file exists and is current
cat .bot_lock

# 3. Watch logs for no Telegram Conflict spam
tail -f logs/bot.log | head -100

# 4. Verify market analyzers initialized
grep "analyzers ready" logs/bot.log

# 5. Check scheduler is running
grep "Scheduler state: running" logs/bot.log
```

### Expected Log Output
```
✅ Lock acquired
✅ StateManager ready | Balance=$40.01 | Positions=0
✅ Connected: PAPER (PAPER)
✅ BTC/USDT and ETH/USDT analyzers ready
✅ 4brain_scalping strategy configured
✅ Controller initialized
✅ Telegram bot started
📋 Scheduler state: running
```

---

## D) Quick Reference Commands

### Systemd Commands
```bash
# Start bot
sudo systemctl start tradingbot

# Stop bot
sudo systemctl stop tradingbot

# Restart bot
sudo systemctl restart tradingbot

# Check status
sudo systemctl status tradingbot

# View logs (real-time)
sudo journalctl -u tradingbot -f

# View logs (last 100 lines)
sudo journalctl -u tradingbot -n 100

# Check if service is enabled on boot
sudo systemctl is-enabled tradingbot
```

### Process Management Commands
```bash
# Kill bot gracefully
pkill -TERM -f "python3 -m app.main"

# Force kill bot
pkill -9 -f "python3 -m app.main"

# Check if bot is running
pgrep -f "python3 -m app.main"

# Kill and wait before restart
pkill -9 -f "python3 -m app.main" && sleep 2 && nohup python3 -m app.main > logs/bot.log 2>&1 &
```

### Log Commands
```bash
# See recent bot activity
tail -100 logs/bot.log

# Follow logs in real-time
tail -f logs/bot.log

# Search for errors
grep "ERROR" logs/bot.log | tail -20

# Search for Telegram issues
grep -i "telegram\|conflict" logs/bot.log | tail -20

# Count warnings
grep "WARNING" logs/bot.log | wc -l

# See last 5 minutes of logs
tail -f logs/bot.log | grep "$(date +%H:%M)"
```

### Git Commands (VPS)
```bash
# Check current branch
git branch

# See last commits
git log --oneline -5

# Check for uncommitted changes
git status

# Pull latest
git pull origin main

# See what changed since last pull
git diff HEAD~1
```

---

## E) Troubleshooting

### Bot won't start - Lock file error
```bash
rm -f .bot_lock
# Restart bot
```

### Bot starts but Telegram Conflict errors
```bash
# This is normal during recovery. Give it 30 seconds, then:
pkill -9 -f "python3 -m app.main"
sleep 2
rm -f .bot_lock
# Restart bot (systemd or manual)
```

### Bot starts but can't connect to exchange
```bash
# Check .env file has correct API keys
cat config/.env | grep -i "alpaca\|binance"

# Verify venv is activated
which python3

# Check pip packages installed
pip list | grep -i "alpaca\|binance\|requests"
```

### Process doesn't show as running
```bash
# Check full process list
ps aux | grep python

# Check if port is already in use
netstat -tlnp | grep -i python

# Check systemd service errors
sudo systemctl status tradingbot
```

### Logs show no errors but bot not trading
```bash
# Check market data feed is working
tail -50 logs/bot.log | grep -i "snapshot\|analyzer"

# Check strategy is activated
tail -50 logs/bot.log | grep -i "signal\|entry\|decision"

# Verify scheduler is cycling
tail -50 logs/bot.log | grep -i "cycle\|run_cycle"
```

---

## F) Complete Update Script (One Copy-Paste for VPS)

Save this as `vps_update.sh` and run: `bash vps_update.sh`

```bash
#!/bin/bash
set -e

echo "🤖 Trading Bot Update & Restart"
echo "================================"

# Step 1: Navigate to project
cd /path/to/TradingBot || exit 1
echo "✅ Navigated to project directory"

# Step 2: Pull code
echo "📥 Pulling latest code from GitHub..."
git pull origin main
echo "✅ Code pulled"

# Step 3: Activate venv and update dependencies
echo "📦 Activating venv and updating dependencies..."
source venv/bin/activate
pip install -r requirements.txt -q
echo "✅ Dependencies updated"

# Step 4: Kill old processes
echo "🛑 Stopping old bot process..."
pkill -9 -f "python3 -m app.main" || true
sleep 2
echo "✅ Old process killed"

# Step 5: Remove lock file
echo "🔓 Removing stale lock file..."
rm -f .bot_lock
echo "✅ Lock file removed"

# Step 6: Restart via systemd
echo "🚀 Restarting bot via systemd..."
sudo systemctl restart tradingbot
sleep 3
echo "✅ Bot restarted"

# Step 7: Verify running
echo "🔍 Verifying bot is running..."
if pgrep -f "python3 -m app.main" > /dev/null; then
    echo "✅ Bot is running"
else
    echo "❌ Bot failed to start - checking logs..."
    sudo journalctl -u tradingbot -n 30
    exit 1
fi

# Step 8: Check for startup errors
echo "📋 Checking for startup errors..."
ERROR_COUNT=$(sudo journalctl -u tradingbot --since "2 minutes ago" | grep -c "ERROR" || true)
if [ "$ERROR_COUNT" -eq 0 ]; then
    echo "✅ No errors detected"
else
    echo "⚠️ Found $ERROR_COUNT errors - review logs:"
    sudo journalctl -u tradingbot -n 50
fi

echo ""
echo "🎉 Update Complete!"
echo ""
echo "Next steps:"
echo "  • Monitor logs: sudo journalctl -u tradingbot -f"
echo "  • Check status: sudo systemctl status tradingbot"
echo "  • View logs: tail -f logs/bot.log"
```

---

## Summary

**Local:** `git add . → git commit → git push`

**VPS:** `git pull → pip install -r requirements.txt → kill old → restart → verify`

**Key Safety Features:**
- ✅ Kill old process before starting new
- ✅ Remove stale lock file
- ✅ Single instance protection (systemd or manual)
- ✅ Verify process started
- ✅ Check logs for errors
- ✅ No Telegram conflicts (only one bot instance)
