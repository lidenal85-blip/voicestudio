#!/data/data/com.termux/files/usr/bin/bash
# Останавливает сервер и туннель

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/logs/server.pid"

if [ -f "$PID_FILE" ]; then
    while IFS= read -r pid; do
        kill "$pid" 2>/dev/null && echo "Killed PID $pid" || true
    done < "$PID_FILE"
    rm -f "$PID_FILE"
fi

pkill -f "uvicorn main:app" 2>/dev/null || true
pkill -f "lt --port"        2>/dev/null || true
echo "Voice Studio остановлен."
