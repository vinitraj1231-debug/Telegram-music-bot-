#!/bin/bash

echo "🚀 Starting Telegram Music Bot..."
echo "=================================="

# Check Python version
python_version=$(python --version 2>&1 | awk '{print $2}')
echo "✅ Python version: $python_version"

# Check environment variables
if [ -z "$API_ID" ]; then
    echo "❌ ERROR: API_ID not set!"
    exit 1
fi

if [ -z "$API_HASH" ]; then
    echo "❌ ERROR: API_HASH not set!"
    exit 1
fi

if [ -z "$BOT_TOKEN" ]; then
    echo "❌ ERROR: BOT_TOKEN not set!"
    exit 1
fi

if [ -z "$SESSION_STRING" ]; then
    echo "❌ ERROR: SESSION_STRING not set!"
    exit 1
fi

echo "✅ All environment variables present"

# Check FFmpeg
if command -v ffmpeg &> /dev/null; then
    ffmpeg_version=$(ffmpeg -version | head -n1 | awk '{print $3}')
    echo "✅ FFmpeg version: $ffmpeg_version"
else
    echo "⚠️  WARNING: FFmpeg not found! Installing..."
    apt-get update && apt-get install -y ffmpeg
fi

# Create logs directory
mkdir -p logs

# Start health check server in background (if exists)
if [ -f "health_check.py" ]; then
    python -m aiohttp.web -H 0.0.0.0 -P 8080 health_check:app &
    HEALTH_PID=$!
    echo "✅ Health check server started (PID: $HEALTH_PID)"
fi

# Start main bot
echo "🎵 Starting Music Bot..."
echo "=================================="
python -u main.py

# Cleanup on exit
if [ ! -z "$HEALTH_PID" ]; then
    kill $HEALTH_PID 2>/dev/null
fi
