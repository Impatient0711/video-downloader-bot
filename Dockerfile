# ربات دانلود از لینک — ایمیج سبک
# فقط دو ابزار دانلود نصب می‌شود: aria2c (لینک مستقیم) و yt-dlp (سایت‌ها) + ffmpeg برای ادغام/تبدیل
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Tehran \
    DOWNLOAD_DIR=/tmp/downloads

RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
      aria2 ffmpeg ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# کاربر غیرروت برای امنیت
RUN useradd -m -u 10001 bot && mkdir -p /tmp/downloads && chown -R bot:bot /tmp/downloads /app
USER bot

CMD ["python", "bot.py"]
