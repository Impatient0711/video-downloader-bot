#!/bin/sh
# ════════════════════════════════════════════════════════════════════════════
#  نقطهٔ ورود ربات: سرور Bot API محلی + خودِ ربات، هر دو در همین کانتینر
#
#  چرا: سقف ۵۰ مگابایتِ api.telegram.org با سرور Bot API محلی (--local) به
#  ۲۰۰۰ مگابایت می‌رسد. سرور محلی را همین‌جا کنار ربات بالا می‌آوریم تا
#  نیازی به سرویس دوم، آدرس داخلی یا ست‌کردن BOT_API_BASE نباشد.
#
#  متغیرهای لازم (همان‌هایی که برای سرور محلی لازم است):
#     TELEGRAM_API_ID · TELEGRAM_API_HASH · TELEGRAM_LOCAL=1
#  اختیاری: TELEGRAM_HTTP_PORT (پیش‌فرض 8081) · TELEGRAM_WORK_DIR/TEMP_DIR
#  اگر BOT_API_BASE را دستی ست کنی، سرور محلی اجرا نمی‌شود.
# ════════════════════════════════════════════════════════════════════════════
set -u

PORT="${TELEGRAM_HTTP_PORT:-8081}"
WORK="${TELEGRAM_WORK_DIR:-/data}"
TEMP="${TELEGRAM_TEMP_DIR:-/data/tmp}"
DL="${DOWNLOAD_DIR:-/data/downloads}"
API_BIN="/usr/local/bin/telegram-bot-api"
API_PID=""
BOT_PID=""

say() { echo "[entry] $*"; }

mkdir -p "$WORK" "$TEMP" "$DL" 2>/dev/null || true

port_open() {
  python3 - "$1" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(2)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

if [ -n "${BOT_API_BASE:-}" ]; then
  say "ℹ️  BOT_API_BASE دستی ست شده ($BOT_API_BASE) — سرور محلی اجرا نمی‌شود."
elif [ -z "${TELEGRAM_API_ID:-}" ] || [ -z "${TELEGRAM_API_HASH:-}" ]; then
  say "⚠️  TELEGRAM_API_ID یا TELEGRAM_API_HASH ست نشده → سقف ارسال ۵۰MB (API ابری)."
  say "    برای ۲GB این دو را در Variables بگذار و سرویس را Redeploy کن."
elif [ ! -x "$API_BIN" ]; then
  say "⚠️  باینری سرور Bot API پیدا نشد ($API_BIN) → API ابری."
else
  say "🚀 اجرای سرور Bot API محلی روی پورت $PORT (پوشه: $WORK)"
  "$API_BIN" \
    --api-id="$TELEGRAM_API_ID" \
    --api-hash="$TELEGRAM_API_HASH" \
    --local \
    --http-port="$PORT" \
    --dir="$WORK" \
    --temp-dir="$TEMP" \
    --verbosity="${TELEGRAM_VERBOSITY:-1}" &
  API_PID=$!

  i=0
  while [ "$i" -lt 45 ]; do
    if ! kill -0 "$API_PID" 2>/dev/null; then
      say "❌ سرور محلی خروج کرد (api_id/api_hash را چک کن)."
      API_PID=""
      break
    fi
    if port_open "$PORT"; then
      say "✅ سرور Bot API محلی آماده است → سقف ارسال ۲۰۰۰MB"
      export BOT_API_BASE="http://127.0.0.1:$PORT"
      break
    fi
    i=$((i + 1))
    sleep 1
  done

  if [ -z "${BOT_API_BASE:-}" ] && [ -n "$API_PID" ]; then
    say "⚠️  سرور محلی در ۴۵ ثانیه آماده نشد → ادامه با API ابری (۵۰MB)."
    kill "$API_PID" 2>/dev/null || true
    API_PID=""
  fi
fi

cleanup() {
  [ -n "$BOT_PID" ] && kill "$BOT_PID" 2>/dev/null || true
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

python3 /app/bot.py &
BOT_PID=$!
wait "$BOT_PID"
CODE=$?
cleanup
exit "$CODE"
