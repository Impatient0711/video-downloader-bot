# ════════════════════════════════════════════════════════════════════════════
#  ربات دانلود از لینک + سرور Bot API محلی (برای سقف ۲۰۰۰ مگابایت)
#
#  پایه: ایمیج رسمیِ سرور Bot API (Alpine 3.21 + باینری + کتابخانه‌هایش)
#  رویش: پایتون، ffmpeg (ادغام/تبدیل بدون افت کیفیت)، aria2 (لینک مستقیم)
#
#  نتیجه: یک سرویس، یک مجموعه متغیر — سقف ارسال/دانلود ۲۰۰۰MB.
#  اگر TELEGRAM_API_ID/TELEGRAM_API_HASH را نگذاری، ربات مثل قبل روی
#  api.telegram.org کار می‌کند (سقف ۵۰MB) و بقیه‌چیز یکسان است.
# ════════════════════════════════════════════════════════════════════════════
FROM aiogram/telegram-bot-api:latest

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Tehran \
    TELEGRAM_HTTP_PORT=8081 \
    TELEGRAM_WORK_DIR=/data \
    TELEGRAM_TEMP_DIR=/data/tmp \
    DOWNLOAD_DIR=/data/downloads

RUN apk add --no-cache \
        python3 \
        py3-pip \
        ffmpeg \
        aria2 \
        ca-certificates \
        tzdata \
 && ln -sf /usr/share/zoneinfo/Asia/Tehran /etc/localtime \
 && mkdir -p /data /data/tmp /data/downloads

WORKDIR /app
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY bot.py start.sh ./
RUN chmod +x /app/start.sh

# ایمیج پایه entrypoint خودش را دارد (مخصوص اجرای فقط-سرور) — ما نقطهٔ ورود خودمان را داریم.
ENTRYPOINT []
CMD ["/bin/sh", "/app/start.sh"]
