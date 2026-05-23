#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# AI Voice Studio MVP — Termux Launcher
# Туннель: cloudflared (первый выбор) → localtunnel (резерв)
# Использование: bash start.sh
# ============================================================

set -e

# ── Цвета ────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

log()  { echo -e "${CYAN}[VS]${RESET} $1"; }
ok()   { echo -e "${GREEN}[OK]${RESET} $1"; }
warn() { echo -e "${YELLOW}[!!]${RESET} $1"; }
die()  { echo -e "${RED}[ERR]${RESET} $1"; exit 1; }

# ── Конфиг ───────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
PID_FILE="$SCRIPT_DIR/logs/server.pid"
TUNNEL_URL_FILE="$SCRIPT_DIR/logs/tunnel.url"
LOG_FILE="$SCRIPT_DIR/logs/session.log"
PORT="${PORT:-8000}"
TUNNEL_PID=""
TUNNEL_MODE=""   # "cloudflared" или "localtunnel"

mkdir -p "$SCRIPT_DIR/logs"

echo ""
echo -e "${BOLD}◈ AI Voice Studio MVP${RESET}"
echo -e "  Termux mobile server + cloudflared tunnel"
echo "────────────────────────────────────"

# ── Проверки ─────────────────────────────────────────────────
log "Проверяю окружение..."

[ -f "$ENV_FILE" ] || die ".env не найден. Скопируй: cp .env.example .env"

python3 -c "import fastapi" 2>/dev/null \
    || die "fastapi не установлен. Запусти: pip install -r requirements.txt"
ok "Python OK"

command -v ffmpeg &>/dev/null || die "ffmpeg не найден: pkg install ffmpeg"
ok "FFmpeg OK"

# ── Определяем туннель ────────────────────────────────────────
if command -v cloudflared &>/dev/null; then
    ok "cloudflared найден"
    TUNNEL_MODE="cloudflared"
elif command -v lt &>/dev/null; then
    warn "cloudflared не найден, используем localtunnel"
    TUNNEL_MODE="localtunnel"
else
    warn "Туннель не найден. Устанавливаю cloudflared..."
    warn "Попытка: pkg install cloudflared"
    pkg install cloudflared -y 2>/dev/null && TUNNEL_MODE="cloudflared" || {
        warn "pkg не сработал. Пробую npm localtunnel..."
        command -v node &>/dev/null || die "Нет ни cloudflared, ни Node.js.\nУстанови: pkg install cloudflared"
        npm install -g localtunnel 2>/dev/null && TUNNEL_MODE="localtunnel" \
            || die "Не удалось установить туннель.\nВручную: pkg install cloudflared"
    }
fi
ok "Туннель: $TUNNEL_MODE"

# ── Убиваем старые процессы ───────────────────────────────────
if [ -f "$PID_FILE" ]; then
    while IFS= read -r pid; do
        kill "$pid" 2>/dev/null || true
    done < "$PID_FILE"
    rm -f "$PID_FILE"
fi

pkill -f "cloudflared tunnel" 2>/dev/null || true
pkill -f "lt --port"          2>/dev/null || true
sleep 0.5

# ── Запуск uvicorn ────────────────────────────────────────────
log "Запускаю FastAPI на порту $PORT..."

cd "$SCRIPT_DIR"
uvicorn main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-level info \
    >> "$LOG_FILE" 2>&1 &

SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

TRIES=0
until curl -s "http://localhost:$PORT/" > /dev/null 2>&1; do
    sleep 1
    TRIES=$((TRIES + 1))
    [ $TRIES -ge 15 ] && die "Сервер не поднялся. Смотри: $LOG_FILE"
done
ok "FastAPI запущен (PID $SERVER_PID)"

# ── Запуск туннеля ────────────────────────────────────────────
rm -f "$TUNNEL_URL_FILE.raw"
touch "$TUNNEL_URL_FILE.raw"

if [ "$TUNNEL_MODE" = "cloudflared" ]; then
    log "Запускаю cloudflared..."
    # cloudflared пишет в stderr, формат:
    # INF | https://random-name.trycloudflare.com |
    cloudflared tunnel --url "http://localhost:$PORT" \
        --no-autoupdate \
        2> "$TUNNEL_URL_FILE.raw" &
    TUNNEL_PID=$!
    URL_PATTERN='https://[a-zA-Z0-9-]+\.trycloudflare\.com'
else
    log "Запускаю localtunnel..."
    lt --port "$PORT" > "$TUNNEL_URL_FILE.raw" 2>&1 &
    TUNNEL_PID=$!
    URL_PATTERN='https://[a-zA-Z0-9._-]+\.loca\.lt'
fi

echo "$TUNNEL_PID" >> "$PID_FILE"

# ── Ждём URL ─────────────────────────────────────────────────
TRIES=0
TUNNEL_URL=""
until [ -n "$TUNNEL_URL" ]; do
    sleep 1
    TRIES=$((TRIES + 1))
    TUNNEL_URL=$(grep -oE "$URL_PATTERN" "$TUNNEL_URL_FILE.raw" 2>/dev/null | head -1 || true)

    if [ $TRIES -ge 35 ]; then
        warn "Туннель не вернул URL за 35 секунд."
        echo "--- raw output ---"
        cat "$TUNNEL_URL_FILE.raw" 2>/dev/null || echo "(пусто)"
        echo "------------------"
        warn "Сервер работает локально: http://localhost:$PORT"
        TUNNEL_URL="(туннель недоступен)"
        break
    fi
done

echo "$TUNNEL_URL" > "$TUNNEL_URL_FILE"
ok "Туннель: $TUNNEL_URL"

# ── Обновляем CORS в .env ─────────────────────────────────────
if grep -q "^CORS_ORIGINS=" "$ENV_FILE" && [ "$TUNNEL_URL" != "(туннель недоступен)" ]; then
    EXISTING=$(grep "^CORS_ORIGINS=" "$ENV_FILE" | cut -d= -f2-)
    # Убираем старые tunnel-домены
    CLEAN=$(echo "$EXISTING" | tr ',' '\n' \
        | grep -vE '(loca\.lt|trycloudflare\.com)' \
        | grep -v '^$' \
        | tr '\n' ',' | sed 's/,$//')
    NEW_CORS="${CLEAN:+${CLEAN},}${TUNNEL_URL}"
    sed -i "s|^CORS_ORIGINS=.*|CORS_ORIGINS=${NEW_CORS}|" "$ENV_FILE"
    log "CORS_ORIGINS обновлён"
fi

# ── Записываем PUBLIC_URL в .env ──────────────────────────────
# Сервер использует это для формирования download_url в /api/jobs/claim
# Без этого Colab получает http://0.0.0.0:8000/... и падает с Connection refused
if [ "$TUNNEL_URL" != "(туннель недоступен)" ]; then
    if grep -q "^PUBLIC_URL=" "$ENV_FILE"; then
        sed -i "s|^PUBLIC_URL=.*|PUBLIC_URL=${TUNNEL_URL}|" "$ENV_FILE"
    else
        echo "PUBLIC_URL=${TUNNEL_URL}" >> "$ENV_FILE"
    fi
    log "PUBLIC_URL → $TUNNEL_URL"
fi

# ── Вывод ─────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════"
echo -e "${BOLD}  ✓ Voice Studio запущен!${RESET}"
echo "════════════════════════════════════"
echo -e "  ${CYAN}Локальный UI:${RESET}   http://localhost:$PORT"
echo -e "  ${GREEN}Публичный URL:${RESET}  $TUNNEL_URL"
echo ""
echo -e "  ${YELLOW}Вставь в Colab:${RESET}"
echo -e "  ${BOLD}STUDIO_URL = \"$TUNNEL_URL\"${RESET}"
echo "────────────────────────────────────"
echo -e "  Туннель:  $TUNNEL_MODE"
echo -e "  Логи:     tail -f $LOG_FILE"
echo -e "  Стоп:     bash stop.sh"
echo "════════════════════════════════════"
echo ""

# ── Ctrl+C ───────────────────────────────────────────────────
cleanup() {
    echo ""
    warn "Остановка..."
    kill "$SERVER_PID"  2>/dev/null || true
    kill "$TUNNEL_PID"  2>/dev/null || true
    kill "$TAIL_PID"    2>/dev/null || true
    rm -f "$PID_FILE" "$TUNNEL_URL_FILE" "$TUNNEL_URL_FILE.raw"
    log "Остановлено."
    exit 0
}
trap cleanup INT TERM

# ── Мониторинг + логи ─────────────────────────────────────────
tail -f "$LOG_FILE" &
TAIL_PID=$!

while true; do
    sleep 5
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        warn "Сервер упал! Перезапускаю..."
        cd "$SCRIPT_DIR"
        uvicorn main:app --host 0.0.0.0 --port "$PORT" \
            --log-level info >> "$LOG_FILE" 2>&1 &
        SERVER_PID=$!
        echo "$SERVER_PID" > "$PID_FILE"
    fi
done
