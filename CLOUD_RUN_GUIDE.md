# 🚀 Running Trading Bot on Cloud 24/7

## Quick Start - Run Bot in Detached Mode

### Option 1: Using the Automated Script (Recommended)

```bash
# SSH into your cloud server
ssh your_cloud_user@your_cloud_ip

# Navigate to bot directory
cd ~/bot-trading

# Run the bot in detached tmux session
bash run-detached.sh
```

The bot will now run 24/7 and continue running even if you disconnect.

---

## Monitoring Your Bot

### View Current Session Output
```bash
tmux capture-pane -t trading-bot -p
```

### Attach to Live Session (Watch in Real-Time)
```bash
tmux attach-session -t trading-bot
```

### Detach from Session
Press: `Ctrl + B` then `D`

### Monitor Log Files
```bash
# View latest log file
tail -f ~/bot-trading/logs/bot_*.log

# Follow logs in real-time
tail -f ~/bot-trading/logs/bot_*.log
```

### List All Active Sessions
```bash
tmux list-sessions
```

---

## Stopping the Bot

### Stop Gracefully
```bash
tmux send-keys -t trading-bot C-c
```

### Kill Session Immediately
```bash
tmux kill-session -t trading-bot
```

### Kill All Bot Processes
```bash
pkill -9 -f "python.*app.main"
```

---

## Restarting the Bot

### Full Restart
```bash
# Kill existing session
tmux kill-session -t trading-bot

# Start new session
bash ~/bot-trading/run-detached.sh
```

---

## Troubleshooting

### Bot Won't Start
```bash
# Check if venv is properly set up
cd ~/bot-trading
source venv/bin/activate
python -c "import app; print('✅ App imports work')"
```

### Check Environment Variables
```bash
cat ~/bot-trading/config/.env
# Make sure all required fields are set
```

### View Recent Errors
```bash
# Check the latest log file
cat ~/bot-trading/logs/bot_*.log | tail -100
```

### Manual Test Run (Not Detached)
```bash
cd ~/bot-trading
source venv/bin/activate
python -m app.main
# Press Ctrl+C to stop
```

---

## Advanced: Auto-Restart Bot on Boot Using Systemd

If you want the bot to automatically start when your cloud server reboots:

### Create Systemd Service
```bash
sudo nano /etc/systemd/system/trading-bot.service
```

Paste this content:
```ini
[Unit]
Description=Trading Bot Service
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/home/your_username/bot-trading
ExecStart=/home/your_username/bot-trading/venv/bin/python -m app.main
Restart=on-failure
RestartSec=10
StandardOutput=append:/home/your_username/bot-trading/logs/systemd.log
StandardError=append:/home/your_username/bot-trading/logs/systemd.log

[Install]
WantedBy=multi-user.target
```

### Enable and Start Service
```bash
sudo systemctl daemon-reload
sudo systemctl enable trading-bot
sudo systemctl start trading-bot
```

### Monitor Service
```bash
sudo systemctl status trading-bot
sudo journalctl -u trading-bot -f
```

---

## Full Cloud Deployment Workflow

1. **SSH to cloud server:**
   ```bash
   ssh your_cloud_user@your_cloud_ip
   ```

2. **Run initial setup (if first time):**
   ```bash
   bash ~/bot-trading/setup-cloud.sh
   ```

3. **Update .env file with your config:**
   ```bash
   nano ~/bot-trading/config/.env
   # Edit: BINANCE_API_KEY, TELEGRAM_BOT_TOKEN, etc.
   ```

4. **Start bot in detached mode:**
   ```bash
   bash ~/bot-trading/run-detached.sh
   ```

5. **Verify it's running:**
   ```bash
   tmux list-sessions
   tail -f ~/bot-trading/logs/bot_*.log
   ```

6. **Disconnect and let it run (bot continues running!):**
   ```bash
   exit
   ```

---

## Health Check Script

Create a script to monitor bot health:

```bash
#!/bin/bash
# health-check.sh

echo "Trading Bot Health Status"
echo "========================="

if tmux has-session -t trading-bot 2>/dev/null; then
    echo "✅ Bot Session: RUNNING"
    echo ""
    echo "Recent Logs:"
    tmux capture-pane -t trading-bot -p | tail -20
else
    echo "❌ Bot Session: NOT RUNNING"
fi

echo ""
echo "Latest Log File:"
ls -lh ~/bot-trading/logs/bot_*.log | tail -1
```

Run it with:
```bash
bash ~/bot-trading/health-check.sh
```

---

## Summary

- **Bot runs 24/7** even after you disconnect
- **Use `tmux attach-session -t trading-bot`** to see what's happening
- **Use `Ctrl+B then D`** to disconnect without stopping the bot
- **Use `tmux kill-session -t trading-bot`** to stop the bot
- **Logs are saved** to `~/bot-trading/logs/bot_*.log`
