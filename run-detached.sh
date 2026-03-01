#!/bin/bash
# 🚀 Run Trading Bot in Detached TMux Session
# This script starts the bot in a tmux session that persists after disconnection

set -e

echo "════════════════════════════════════════════════════════════════"
echo "         🚀 STARTING TRADING BOT IN DETACHED MODE 🚀"
echo "════════════════════════════════════════════════════════════════"

# Configuration
BOT_DIR="${HOME}/bot-trading"
SESSION_NAME="trading-bot"
LOG_DIR="${BOT_DIR}/logs"
LOG_FILE="${LOG_DIR}/bot_$(date +%Y%m%d_%H%M%S).log"

# Check if bot directory exists
if [ ! -d "$BOT_DIR" ]; then
    echo "❌ Error: Bot directory not found at $BOT_DIR"
    echo "Please run setup-cloud.sh first"
    exit 1
fi

# Create logs directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Kill existing bot session if running
echo "Checking for existing sessions..."
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "⚠️  Found existing session. Killing it..."
    tmux kill-session -t "$SESSION_NAME"
    sleep 2
fi

# Create new detached tmux session
echo "📍 Creating detached tmux session: $SESSION_NAME"
tmux new-session -d -s "$SESSION_NAME" -x 200 -y 50

# Send startup commands to the session
tmux send-keys -t "$SESSION_NAME" "cd $BOT_DIR" Enter
tmux send-keys -t "$SESSION_NAME" "source venv/bin/activate" Enter
tmux send-keys -t "$SESSION_NAME" "python -m app.main 2>&1 | tee -a $LOG_FILE" Enter

# Wait a moment for the bot to start
sleep 3

# Check if session is running
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo ""
    echo "✅ Trading bot started successfully!"
    echo ""
    echo "📊 Session Details:"
    echo "   Session Name: $SESSION_NAME"
    echo "   Bot Directory: $BOT_DIR"
    echo "   Log File: $LOG_FILE"
    echo ""
    echo "📝 Useful Commands:"
    echo "   • View logs:        tmux capture-pane -t $SESSION_NAME -p"
    echo "   • Attach to session: tmux attach-session -t $SESSION_NAME"
    echo "   • Detach (Ctrl+B then D)"
    echo "   • Kill session:     tmux kill-session -t $SESSION_NAME"
    echo "   • Monitor logs:     tail -f $LOG_FILE"
    echo ""
    echo "🔄 Bot is now running 24/7 in the background!"
else
    echo "❌ Failed to start bot session"
    exit 1
fi
