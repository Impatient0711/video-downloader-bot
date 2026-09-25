#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات دانلود از لینک — با منوی کیفیت و پشتیبانی تا ۲ گیگابایت
=============================================================
جریان کار:
  ۱) لینک می‌فرستی → ربات صفحه را بررسی می‌کند و **کیفیت‌های موجود + حجم** را
     با دکمه نشان می‌دهد.
  ۲) کیفیت را انتخاب می‌کنی → دانلود با **نوار پیشرفتِ مربعی سبز** شروع می‌شود.
  ۳) اگر فایل mp4 نبود (مثلاً ts/mkv/webm)، با ffmpeg و بدون افت کیفیت
     (`-c copy`) به mp4 تبدیل می‌شود تا به‌شکل **ویدیو** (نه داکیومنت) بیاید.
  ۴) آپلود هم با نوار مربعی نشان داده می‌شود و در پایان فیلم با **کپشن
     (عنوان/توضیح + لینک اصلی)** تحویل داده می‌شود.

سقف حجم:
  • Bot API ابری (api.telegram.org) → ۵۰MB (سقف خود تلگرام برای ربات‌ها)
  • سرور Bot API محلی (--local)      → تا ۲۰۰۰MB
  تشخیص خودکار است؛ اگر BOT_API_BASE را روی سرور محلی بگذاری، سقف ۲GB می‌شود.
  (برای گرفتن حجم واقعیِ فایل هم `MAX_UPLOAD_MB` را دسـتی نمی‌خواهد بگذاری.)

هیچ‌کدام از این‌ها در پروژه نیست: سشنِ کاربر، وب‌سرور/پورت عمومی، کتابخانهٔ
اشتراک فایل همتا، یوزنت، دانلودر مدیریت‌شدهٔ خارجی. فقط Bot API رسمی.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import json
import os
import re
import secrets
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp

START_TS = time.time()
VERSION = "2.2"


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


# ─────────────────────────── تنظیمات ───────────────────────────

BOT_TOKEN = _env("BOT_TOKEN")
# اگر خودت BOT_API_BASE نگذاری ولی api_id/api_hash باشد، یعنی سرور محلی
# (start.sh آن را بالا می‌آورد) → همان آدرس لوکال.
_def_base = ("http://127.0.0.1:%s" % _env("TELEGRAM_HTTP_PORT", "8081")
             if (_env("TELEGRAM_API_ID") and _env("TELEGRAM_API_HASH"))
             else "https://api.telegram.org")
API_BASE = _env("BOT_API_BASE", _def_base).rstrip("/")
LOCAL_API = "api.telegram.org" not in API_BASE.lower()

OWNER_ID = int(_env("OWNER_ID", "0") or 0)
ALLOWED_USERS = {
    int(x) for x in re.split(r"[,\s]+", _env("ALLOWED_USERS", "")) if x.strip().isdigit()
}

DOWNLOAD_DIR = Path(_env("DOWNLOAD_DIR", "/tmp/downloads"))
# سقف ارسال: اگر سرور محلی Bot API باشد، پیش‌فرض ۲۰۰۰MB؛ وگرنه ۵۰MB (سقف تلگرام)
_DEF_CAP = 2000 if LOCAL_API else 49
MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB", "0") or 0) or _DEF_CAP
MAX_DOWNLOAD_MB = int(_env("MAX_DOWNLOAD_MB", str(max(2200, _DEF_CAP + 400))))
MAX_CONCURRENT = max(1, int(_env("MAX_CONCURRENT", "2")))
DISK_LIMIT_MB = int(_env("DISK_LIMIT_MB", "6000"))

DOWNLOADER = _env("DOWNLOADER", "auto").lower()      # auto | ytdlp | aria2
AUTO_FALLBACK = _env("AUTO_FALLBACK", "1") not in ("0", "false", "no", "off")
KEEP_FILES = _env("KEEP_FILES", "0") in ("1", "true", "yes", "on")
COOKIES_FILE = _env("COOKIES_FILE")
FORCE_MP4 = _env("FORCE_MP4", "1") not in ("0", "false", "no", "off")   # ارسال به‌شکل ویدیو
REENCODE = _env("REENCODE", "0") in ("1", "true", "yes", "on")         # تبدیل کدک (کند)
SESSION_TTL = int(_env("MENU_TTL_S", "900"))
MAX_OPTIONS = int(_env("MAX_OPTIONS", "6"))

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts", ".m2ts",
             ".mpg", ".mpeg", ".wmv", ".ogv"}
AUDIO_EXT = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac"}
FILE_EXT = {".zip", ".rar", ".7z", ".pdf", ".apk"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
# کدک‌هایی که تلگرام داخل mp4 به‌شکل ویدیو نشان می‌دهد
OK_VCODEC = {"h264", "hevc", "h265", "mpeg4", "vp9", "av1"}
OK_ACODEC = {"aac", "mp3", "ac3", "eac3", "opus", "vorbis", "none", ""}


# ─────────────────────────── کمکی‌ها ───────────────────────────


def human(n: float) -> str:
    n = float(n or 0)
    if n <= 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024
    return "%.1fTB" % n


def hhmmss(sec: float) -> str:
    sec = int(max(0, sec or 0))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


def cut(s: str, n: int) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def tree_size_mb(p: Path) -> float:
    total = 0
    for f in p.glob("**/*"):
        with contextlib.suppress(OSError):
            if f.is_file():
                total += f.stat().st_size
    return total / 1048576


def bar(pct: float, width: int = 12) -> str:
    """نوار مربعی: مربع‌های سبز به‌تعداد درصد، بقیه سفید."""
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(pct / 100.0 * width))
    return "🟩" * filled + "⬜️" * (width - filled)


def progress_text(stage: str, pct: float, done: float, total: float,
                  speed: str = "", eta: str = "", note: str = "") -> str:
    if stage == "download":
        head = "⬇️ *دانلود*  %s %d%%" % (bar(pct), int(pct))
        line2 = []
        if total > 0:
            line2.append("%s / %s" % (human(done), human(total)))
        elif done > 0:
            line2.append(human(done))
        if speed:
            line2.append(speed)
        if eta:
            line2.append("باقی‌مانده " + eta)
        txt = head + ("\n\u200e" + "  ·  ".join(line2) if line2 else "")
    elif stage == "remux":
        txt = "🎞 *آماده‌سازی برای ارسال به‌شکل ویدیو*\n\u200e%s %d%%" % (bar(pct), int(pct))
    elif stage == "upload":
        txt = "⬆️ *آپلود به تلگرام*  %s %d%%" % (bar(pct), int(pct))
        detail = []
        if total > 0:
            detail.append("%s / %s" % (human(done), human(total)))
        if eta:
            detail.append("باقی‌مانده " + eta)
        if detail:
            txt += "\n\u200e" + "  ·  ".join(detail)
    else:
        txt = stage
    if note:
        txt += "\n" + note
    return txt


def parse_progress_line(line: str, st: dict):
    """درصد/حجم/سرعت/ETA را از خروجی yt-dlp یا aria2c می‌کشد."""
    m = re.search(r"\[download\]\s+([\d.]+)%", line)
    if m:
        st["pct"] = float(m.group(1))
        m2 = re.search(r"of\s+~?\s*([\d.]+)(B|KiB|MiB|GiB)", line)
        if m2:
            st["total"] = float(m2.group(1)) * {"B": 1, "KiB": 1024, "MiB": 1048576, "GiB": 1073741824}[m2.group(2)]
        m3 = re.search(r"at\s+([\d.]+\s*\w+B/s)", line)
        if m3:
            st["speed"] = m3.group(1).replace(" ", "")
        m4 = re.search(r"ETA\s+([\d:]+)", line)
        if m4:
            st["eta"] = m4.group(1)
        if st.get("total"):
            st["done"] = st["total"] * st["pct"] / 100.0
        return
    m = re.search(r"\((\d+)%\)", line)
    if m:
        st["pct"] = float(m.group(1))
        m3 = re.search(r"DL:([\d.]+\w+)", line)
        if m3:
            st["speed"] = m3.group(1)
        m4 = re.search(r"ETA:(\S+?)\]", line)
        if m4:
            st["eta"] = m4.group(1)
        m2 = re.search(r"\s([\d.]+\w+)/([\d.]+\w+)\(", line)

        def to_bytes(s):
            mm = re.match(r"([\d.]+)(\w+)", s or "")
            if not mm:
                return 0.0
            return float(mm.group(1)) * {"B": 1, "KiB": 1024, "MiB": 1048576,
                                         "GiB": 1073741824}.get(mm.group(2), 1)

        if m2:
            st["done"], st["total"] = to_bytes(m2.group(1)), to_bytes(m2.group(2))


async def run_proc(cmd: list[str], st: dict, watch_dir: Optional[Path] = None,
                   on_line=None) -> tuple[int, str]:
    """اجرای پروسه با خواندن خط‌به‌خط (برای نوار پیشرفت و لغو)."""
    _log(st, "cmd", " ".join(str(c) for c in cmd))
    if not shutil.which(cmd[0]):
        _log(st, "error", "برنامه نصب نیست: %s" % cmd[0])
        return 127, "not installed: %s" % cmd[0]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    trailing: list[str] = []
    oversize = False
    queue: asyncio.Queue = asyncio.Queue()
    assert proc.stdout is not None

    async def reader():
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                await queue.put(raw.decode("utf-8", "replace").rstrip())
        finally:
            await queue.put(None)

    rt = asyncio.create_task(reader())
    try:
        while True:
            try:
                line = await asyncio.wait_for(queue.get(), timeout=5)
            except asyncio.TimeoutError:
                if (proc.returncode is None and watch_dir is not None
                        and tree_size_mb(watch_dir) > MAX_DOWNLOAD_MB * 1.05):
                    oversize = True
                    with contextlib.suppress(Exception):
                        proc.kill()
                continue
            if line is None:
                break
            if line:
                trailing.append(line)
                if len(trailing) > 40:
                    trailing.pop(0)
                parse_progress_line(line, st)
                if on_line:
                    on_line(line)
    finally:
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.kill()
        rt.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(rt, timeout=10)
    code = await proc.wait()
    if oversize:
        _log(st, "error", "حجم از سقف %dMB گذشت — پروسه کشته شد" % MAX_DOWNLOAD_MB)
        return 99, "MAX_FILESIZE"
    _log(st, "exit", "کد خروج %d" % code)
    if trailing:
        _log(st, "output", "\n".join(trailing[-25:]))
    return code, "\n".join(trailing)


# ─────────────────────────── لاگ ───────────────────────────
#  هر دانلود یک لاگِ کامل دارد (دستورها + خروجی + خطاها).
#  • با شروع هر دانلود، لاگِ قبلی پاک می‌شود.
#  • دانلودِ موفق ⇒ لاگ دور ریخته می‌شود (خواستهٔ کاربر: بعد از هر دانلود پاک شود).
#  • دانلودِ ناموفق ⇒ لاگ نگه داشته می‌شود تا با /log ببینی مشکل کجاست.

MAX_LOG_LINES = int(_env("MAX_LOG_LINES", "400"))
LOG_KEEP_ON_SUCCESS = _env("LOG_KEEP_ON_SUCCESS", "0") in ("1", "true", "yes", "on")
LAST_LOG: dict = {"text": "", "ok": None, "url": "", "uid": 0, "at": 0.0, "stage": ""}


class JobLog:
    """لاگِ یک دانلود: هر مرحله با زمان و برچسب ثبت می‌شود."""

    def __init__(self, uid: int, url: str):
        self.uid, self.url = uid, url
        self.t0 = time.time()
        self.parts: list[str] = []

    def add(self, tag: str, text: str = ""):
        ts = time.strftime("%H:%M:%S", time.localtime())
        body = str(text).replace("\r", "")
        lines = body.split("\n")
        if len(lines) > 60:
            lines = lines[:30] + ["… (%d خط حذف شد) …" % (len(lines) - 60)] + lines[-30:]
        head = "%s [%-10s] " % (ts, tag)
        for i, ln in enumerate(lines):
            self.parts.append((head if i == 0 else " " * len(head)) + ln)
        if len(self.parts) > MAX_LOG_LINES:
            self.parts = self.parts[: MAX_LOG_LINES // 2] + \
                ["… (%d خط حذف شد) …" % (len(self.parts) - MAX_LOG_LINES)] + \
                self.parts[-(MAX_LOG_LINES // 2):]

    @property
    def elapsed(self) -> str:
        return hhmmss(time.time() - self.t0)

    def dump(self, ok: bool, stage: str = "", extra: str = "") -> str:
        head = [
            "# لاگ ربات دانلود — نسخه %s" % VERSION,
            "نتیجه: %s" % ("موفق ✅" if ok else "ناموفق ❌"),
            "زمان اجرا: %s   |   مدت: %s" % (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.t0)), self.elapsed),
            "کاربر: %s" % self.uid,
            "لینک: %s" % self.url,
            "سرور API: %s (%s)" % (API_BASE, "محلی" if LOCAL_API else "ابر تلگرام"),
            "سقف ارسال: %dMB   |   سقف دانلود: %dMB" % (MAX_UPLOAD_MB, MAX_DOWNLOAD_MB),
            "مرحلهٔ خطا: %s" % (stage or "—"),
        ]
        if extra:
            head.append("توضیح: %s" % extra)
        head.append("-" * 64)
        return "\n".join(head + self.parts) + "\n" + "-" * 64


def _log(st: dict, tag: str, text: str = ""):
    lg = (st or {}).get("log")
    if lg:
        lg.add(tag, text)


def _set_last_log(lg: Optional[JobLog], ok: bool, stage: str = "", extra: str = ""):
    """ذخیره/پاک‌کردنِ لاگ برای /log."""
    global LAST_LOG
    if ok and not LOG_KEEP_ON_SUCCESS:
        LAST_LOG = {"text": "", "ok": True, "url": lg.url if lg else "", "uid": 0,
                    "at": time.time(), "stage": ""}
        return
    LAST_LOG = {"text": lg.dump(ok, stage, extra) if lg else "(لاگی ثبت نشد)",
                "ok": ok, "url": lg.url if lg else "", "uid": lg.uid if lg else 0,
                "at": time.time(), "stage": stage}


# ─────────────────────────── تلگرام ───────────────────────────


class ProgressFile(io.RawIOBase):
    """فایل با شمارش بایت‌های خوانده‌شده — برای نوار پیشرفتِ آپلود."""

    def __init__(self, path: Path, on_read):
        self._f = open(path, "rb")
        self._size = path.stat().st_size
        self._n = 0
        self._cb = on_read

    @property
    def n(self) -> int:
        return self._n

    @property
    def size(self) -> int:
        return self._size

    def readable(self):
        return True

    def read(self, n=-1):
        b = self._f.read(n)
        self._n += len(b)
        self._cb(self._n, self._size)
        return b

    def readinto(self, b):
        data = self._f.read(len(b))
        b[: len(data)] = data
        self._n += len(data)
        self._cb(self._n, self._size)
        return len(data)

    def close(self):
        with contextlib.suppress(Exception):
            self._f.close()
        super().close()


class Telegram:
    def __init__(self, token: str, base: str):
        self.token = token
        self.url = "%s/bot%s" % (base, token)
        self.session: Optional[aiohttp.ClientSession] = None
        self.me: dict = {}

    async def start(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=30, sock_read=120))
        self.me = await self.call("getMe") or {}
        with contextlib.suppress(Exception):
            await self.call("deleteWebhook", {"drop_pending_updates": False})

    async def close(self):
        if self.session:
            await self.session.close()

    async def call(self, method: str, data=None, form=None, timeout: float = 90):
        url = "%s/%s" % (self.url, method)
        kwargs = {}
        if form is not None:
            kwargs["data"] = form
        elif data is not None:
            kwargs["json"] = data
        kwargs["timeout"] = aiohttp.ClientTimeout(total=None, connect=30, sock_read=timeout)
        for attempt in range(3):
            try:
                async with self.session.post(url, **kwargs) as r:
                    body = await r.json(content_type=None)
                    if body.get("ok"):
                        return body.get("result")
                    desc = str(body.get("description") or body)
                    if r.status == 429 and attempt < 2:
                        wait = int((body.get("parameters") or {}).get("retry_after", 3))
                        await asyncio.sleep(min(wait, 20))
                        continue
                    raise RuntimeError(desc)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError("network: %s" % e)
        return None

    async def send(self, chat_id: int, text: str, reply_to: int = 0, kb=None):
        data = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "Markdown",
                "disable_web_page_preview": True}
        if reply_to:
            data["reply_to_message_id"] = reply_to
        if kb:
            data["reply_markup"] = kb
        # (kb در پیام‌های خطا هم لازم است)
        try:
            return await self.call("sendMessage", data)
        except RuntimeError:
            data.pop("parse_mode", None)
            return await self.call("sendMessage", data)

    async def edit(self, chat_id: int, message_id: int, text: str, kb=None):
        data = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000],
                "parse_mode": "Markdown", "disable_web_page_preview": True}
        if kb:
            data["reply_markup"] = kb
        with contextlib.suppress(Exception):
            await self.call("editMessageText", data)

    async def edit_html(self, chat_id: int, message_id: int, text: str):
        with contextlib.suppress(Exception):
            await self.call("editMessageText",
                            {"chat_id": chat_id, "message_id": message_id, "text": text[:4000],
                             "parse_mode": "HTML", "disable_web_page_preview": True})

    async def answer_cb(self, cb_id: str, text: str = ""):
        with contextlib.suppress(Exception):
            await self.call("answerCallbackQuery", {"callback_query_id": cb_id,
                                                    "text": text[:200], "show_alert": False})

    async def action(self, chat_id: int, what: str = "upload_video"):
        with contextlib.suppress(Exception):
            await self.call("sendChatAction", {"chat_id": chat_id, "action": what})

    async def send_doc(self, chat_id: int, name: str, data: bytes, caption: str = "",
                       reply_to: int = 0):
        """ارسال یک فایل کوچک از حافظه (برای لاگ)."""
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if reply_to:
            form.add_field("reply_to_message_id", str(reply_to))
        if caption:
            form.add_field("caption", caption[:1024])
        form.add_field("document", io.BytesIO(data), filename=name,
                       content_type="text/plain; charset=utf-8")
        return await self.call("sendDocument", form=form, timeout=300)

    async def send_media(self, chat_id: int, path: Path, kind: str, caption: str,
                         reply_to: int = 0, meta: Optional[dict] = None):
        """ارسال فایل؛ برای فایل‌های بزرگ به‌صورت جریانی (رم پر نمی‌شود)."""
        size = path.stat().st_size
        field = {"video": "video", "audio": "audio", "document": "document"}[kind]
        method = {"video": "sendVideo", "audio": "sendAudio",
                  "document": "sendDocument"}[kind]
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if reply_to:
            form.add_field("reply_to_message_id", str(reply_to))
        if caption:
            form.add_field("caption", caption[:1024])
            form.add_field("parse_mode", "HTML")
        if kind == "video":
            form.add_field("supports_streaming", "true")
            for k in ("duration", "width", "height"):
                if meta and meta.get(k):
                    form.add_field(k, str(int(meta[k])))
        stream = ProgressFile(path, lambda n, t: None)
        form.add_field(field, stream, filename=path.name,
                       content_type="application/octet-stream")
        try:
            return await self.call(method, form=form, timeout=7200)
        finally:
            with contextlib.suppress(Exception):
                stream.close()


# ─────────────────────────── بررسی لینک (پروپ) ───────────────────────────


class Option:
    __slots__ = ("key", "label", "selector", "est", "audio", "direct")

    def __init__(self, key: str, label: str, selector: str, est: int = 0,
                 audio: bool = False, direct: bool = False):
        self.key, self.label, self.selector = key, label, selector
        self.est, self.audio, self.direct = est, audio, direct


class Probe:
    def __init__(self, url: str, title: str, description: str, duration: int,
                 options: list[Option], direct: bool = False, thumb: str = ""):
        self.url, self.title, self.description = url, title, description
        self.duration, self.options, self.direct, self.thumb = duration, options, direct, thumb


def _fmt_size(f: dict) -> int:
    for k in ("filesize", "filesize_approx"):
        v = f.get(k)
        if v:
            try:
                return int(v)
            except Exception:
                pass
    return 0


def _options_from_info(info: dict, url: str) -> Probe:
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    by_h: dict[int, dict] = {}
    audio_best = 0
    muxed: dict[int, int] = {}
    for f in formats:
        vc = (f.get("vcodec") or "none").lower()
        ac = (f.get("acodec") or "none").lower()
        h = int(f.get("height") or 0)
        size = _fmt_size(f)
        if vc != "none" and ac != "none" and h:
            muxed[h] = max(muxed.get(h, 0), size)          # فایل آماده (ویدیو+صدا)
        if vc != "none" and ac == "none" and h:
            cur = by_h.get(h) or {"v": 0}
            if size > cur.get("v", 0):
                cur["v"] = size
            by_h[h] = cur
        if vc == "none" and ac != "none":
            audio_best = max(audio_best, size)

    heights = sorted(by_h.keys(), reverse=True) or sorted(muxed.keys(), reverse=True)
    heights = [h for h in heights if h >= 144][:MAX_OPTIONS]
    options: list[Option] = []
    for h in heights:
        if h in muxed and (h not in by_h or muxed[h] >= by_h[h].get("v", 0)):
            est = muxed[h]
            sel = "bv*[height<=%d]+ba/b[height<=%d]/b" % (h, h)
        else:
            est = by_h.get(h, {}).get("v", 0) + audio_best
            sel = "bv*[height<=%d]+ba/b[height<=%d]/b" % (h, h)
        options.append(Option("h%d" % h, "%dp" % h, sel, est))
    if not options and muxed:
        h = max(muxed)
        options.append(Option("h%d" % h, "%dp" % h, "b", muxed[h]))
    dur = int(info.get("duration") or 0)
    if dur or audio_best or formats:
        options.append(Option("audio", "فقط صدا (mp3)", "ba/b", audio_best, audio=True))
    title = str(info.get("title") or "").strip()
    descr = str(info.get("description") or "").strip()
    thumb = str(info.get("thumbnail") or "")
    return Probe(str(info.get("webpage_url") or url), title, descr, dur, options,
                 direct=False, thumb=thumb)


async def probe_url(url: str) -> tuple[Optional[Probe], str]:
    """بررسی لینک: با yt-dlp اطلاعات می‌گیریم؛ اگر نشد، لینک مستقیم را امتحان می‌کنیم."""
    if DOWNLOADER in ("auto", "ytdlp"):
        st: dict = {}
        cmd = [sys.executable, "-m", "yt_dlp", "-J", "--no-playlist", "--no-warnings",
               "--socket-timeout", "25", "--user-agent", UA]
        if COOKIES_FILE and Path(COOKIES_FILE).exists():
            cmd += ["--cookies", COOKIES_FILE]
        cmd += ["--", url]
        try:
            code, out = await run_proc(cmd, st)
            if code == 0:
                # آخرین خطِ JSON (بعضی سایت‌ها هشدار هم چاپ می‌کنند)
                js = None
                for line in reversed(out.splitlines()):
                    line = line.strip()
                    if line.startswith("{"):
                        with contextlib.suppress(Exception):
                            js = json.loads(line)
                        if js:
                            break
                if js and (js.get("formats") or js.get("url")):
                    p = _options_from_info(js, url)
                    if p.options:
                        return p, ""
        except Exception:
            pass

    # فال‌بک: لینک مستقیم
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
            async with s.head(url, allow_redirects=True, headers={"User-Agent": UA}) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                size = int(r.headers.get("Content-Length") or 0)
                disp = (r.headers.get("Content-Disposition") or "")
                if (ctype.startswith(("video/", "audio/", "application/octet-stream"))
                        or "attachment" in disp.lower() or url.split("?")[0].lower().endswith(
                            tuple(VIDEO_EXT | AUDIO_EXT | FILE_EXT))):
                    name = os.path.basename(url.split("?")[0].rstrip("/")) or "file"
                    opt = Option("direct", "%s" % cut(name, 40), "", size, direct=True)
                    return Probe(url, name, "", 0, [opt], direct=True), ""
                return None, "unsupported"
    except Exception:
        return None, "unsupported"


# ─────────────────────────── دانلود ───────────────────────────


async def download_ytdlp(url: str, option: Option, folder: Path, st: dict
                         ) -> tuple[Optional[Path], str]:
    folder.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "yt_dlp",
           "--no-playlist", "--no-warnings", "--newline",
           "--retries", "3", "--fragment-retries", "3", "--socket-timeout", "30",
           "--max-filesize", "%dM" % MAX_DOWNLOAD_MB,
           "--write-info-json", "--no-part",
           "--user-agent", UA, "-o", str(folder / "out.%(ext)s")]
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        cmd += ["--cookies", COOKIES_FILE]
    if option.selector:
        cmd += ["-f", option.selector]
    if option.audio:
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        # فرمت اصلی حفظ می‌شود (mp4/mkv/webm/ts/…) — تبدیل بعداً انجام می‌شود
        cmd += ["--merge-output-format", "mp4/mkv"]
    cmd += ["--", url]

    code, out = await run_proc(cmd, st, watch_dir=folder)
    if code == 99:
        return None, "big"
    path = pick_output(folder)
    if path is None:
        low = out.lower()
        if "larger than max" in low or "max-filesize" in low:
            return None, "big"
        if "private" in low or "login" in low or "sign in" in low or "cookies" in low:
            return None, "login"
        if "unsupported url" in low or "no video" in low:
            return None, "unsupported"
        if "timed out" in low or "timeout" in low:
            return None, "timeout"
        return None, "failed"
    return path, ""


async def download_aria2(url: str, folder: Path, st: dict) -> tuple[Optional[Path], str]:
    folder.mkdir(parents=True, exist_ok=True)
    raw_name = os.path.basename(url.split("?")[0].rstrip("/"))
    name = re.sub(r"[^\w.\-() ]+", "_", raw_name)[:120] or "file"
    if "." not in name:
        name += ".bin"
    cmd = ["aria2c", "-x", "8", "-s", "8", "-k", "1M",
           "--continue=true", "--max-tries=3", "--retry-wait=2",
           "--connect-timeout=20", "--timeout=60",
           "--auto-file-renaming=false", "--allow-overwrite=true",
           "--summary-interval=1", "--console-log-level=warn",
           "--file-allocation=none", "--user-agent", UA,
           "--max-file-not-found=1", "-d", str(folder), "-o", name, url]
    code, out = await run_proc(cmd, st, watch_dir=folder)
    if code == 99:
        return None, "big"
    p = folder / name
    if code == 0 and p.exists() and p.stat().st_size > 0:
        return p, ""
    low = out.lower()
    if "max-file-not-found" in low or "not found" in low:
        return None, "missing"
    return None, "failed"


def pick_output(folder: Path) -> Optional[Path]:
    bad = {".part", ".ytdl", ".json", ".temp", ".tmp"}
    best, best_t = None, 0.0
    for f in folder.glob("out.*"):
        with contextlib.suppress(OSError):
            if f.is_file() and f.suffix.lower() not in bad:
                t = f.stat().st_mtime
                if t >= best_t:
                    best, best_t = f, t
    if best is None:
        for f in folder.glob("*"):
            with contextlib.suppress(OSError):
                if f.is_file() and f.suffix.lower() not in bad:
                    t = f.stat().st_mtime
                    if t >= best_t:
                        best, best_t = f, t
    return best


def read_info(folder: Path) -> dict:
    p = folder / "out.info.json"
    out = {}
    with contextlib.suppress(Exception):
        if p.exists():
            out = json.loads(p.read_text("utf-8", "replace")) or {}
            p.unlink()
    return out


# ─────────────────── تبدیل به ویدیوی قابل‌ارسال ───────────────────


async def ffprobe_codecs(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        return {}
    st: dict = {}
    code, out = await run_proc(["ffprobe", "-v", "quiet", "-print_format", "json",
                                "-show_streams", "-show_format", str(path)], st)
    if code != 0:
        return {}
    with contextlib.suppress(Exception):
        return json.loads(out or "{}")
    return {}


def codecs_of(probe: dict) -> tuple[str, str, int, int, int]:
    v = a = ""
    w = h = dur = 0
    for s in (probe.get("streams") or []):
        if s.get("codec_type") == "video" and not v:
            v = (s.get("codec_name") or "").lower()
            w, h = int(s.get("width") or 0), int(s.get("height") or 0)
        elif s.get("codec_type") == "audio" and not a:
            a = (s.get("codec_name") or "").lower()
    with contextlib.suppress(Exception):
        dur = int(float((probe.get("format") or {}).get("duration") or 0))
    return v, a, w, h, dur


async def to_sendable_video(path: Path, folder: Path, st: dict) -> tuple[Path, str, dict]:
    """اگر لازم باشد، فایل را بدون افت کیفیت به mp4 تبدیل می‌کند تا ویدیو بماند."""
    meta: dict = {}
    if not FORCE_MP4:
        return path, "", meta
    if path.suffix.lower() in (".mp4", ".m4v"):
        pr = await ffprobe_codecs(path)
        v, a, w, h, dur = codecs_of(pr)
        meta = {"width": w, "height": h, "duration": dur}
        return path, "", meta
    if path.suffix.lower() not in VIDEO_EXT:
        return path, "", meta                      # فایل عادی (zip/pdf/…) — داکیومنت
    if not shutil.which("ffmpeg"):
        return path, "no-ffmpeg", meta

    st["note"] = "🎞 تبدیل به mp4 (بدون افت کیفیت)…"
    pr = await ffprobe_codecs(path)
    v, a, w, h, dur = codecs_of(pr)
    meta = {"width": w, "height": h, "duration": dur}
    if v and (v not in OK_VCODEC or (a and a not in OK_ACODEC)):
        # کدک ناسازگار: یا با کپی نمی‌شود، یا تلگرام ویدیو نمی‌بیند
        if not REENCODE:
            return path, "codec", meta
    out_path = folder / (path.stem + "_v.mp4")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path)]
    if REENCODE and v and v not in OK_VCODEC:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", "-bsf:a", "aac_adtstoasc", str(out_path)]
    code, out = await run_proc(cmd, st)
    if code == 0 and out_path.exists() and out_path.stat().st_size > 0:
        with contextlib.suppress(Exception):
            path.unlink()
        st["note"] = ""
        return out_path, "", meta
    # اگر bsf باعث خطا شد، بدون آن یک‌بار دیگر
    cmd2 = [c for c in cmd if c != "aac_adtstoasc"]
    code2, _ = await run_proc(cmd2, st)
    if code2 == 0 and out_path.exists() and out_path.stat().st_size > 0:
        with contextlib.suppress(Exception):
            path.unlink()
        st["note"] = ""
        return out_path, "", meta
    return path, "remux-failed", meta


# ─────────────────────────── کپشن ───────────────────────────


def build_caption(title: str, description: str, url: str, size: int,
                  meta: dict, extra: str = "") -> str:
    head = html.escape(cut(title or "ویدیو", 150))
    bits = []
    if meta.get("duration"):
        bits.append("⏱ " + hhmmss(meta["duration"]))
    if meta.get("height"):
        bits.append("🎬 %dp" % meta["height"])
    if size:
        bits.append("💾 " + human(size))
    lines = ["<b>%s</b>" % head]
    if bits:
        lines.append(" · ".join(bits))
    if description:
        lines.append("")
        lines.append(html.escape(cut(description, 380)))
    if extra:
        lines.append("")
        lines.append(html.escape(extra))
    lines.append("")
    lines.append('🔗 لینک اصلی: <a href="%s">%s</a>' % (html.escape(url, quote=True),
                                                        html.escape(cut(url, 60))))
    cap = "\n".join(lines)
    if len(cap) > 1024:                       # کپشن تلگرام حداکثر ۱۰۲۴ کاراکتر
        keep = 1024 - 90
        cap = cap[:keep] + "…\n" + '🔗 <a href="%s">لینک اصلی</a>' % html.escape(url, quote=True)
    return cap


def menu_text(p: Probe) -> str:
    lines = ["🎬 *%s*" % cut(p.title or "لینک", 80).replace("*", "")]
    if p.duration:
        lines.append("⏱ %s" % hhmmss(p.duration))
    if p.description:
        lines.append("")
        lines.append(cut(p.description, 220))
    lines.append("")
    lines.append("یک کیفیت را انتخاب کن:")
    for i, o in enumerate(p.options, 1):
        sz = human(o.est) if o.est else "حجم نامعلوم"
        lines.append("%d. %s — %s" % (i, o.label, sz))
    return "\n".join(lines)


def menu_keyboard(sid: str, p: Probe) -> dict:
    rows = []
    for o in p.options:
        sz = human(o.est) if o.est else "؟"
        rows.append([{"text": "%s · %s" % (o.label, sz),
                      "callback_data": "q:%s:%s" % (sid, o.key)}])
    return {"inline_keyboard": rows}


# ─────────────────────────── کار ───────────────────────────


class Job:
    def __init__(self, uid: int, chat_id: int, reply_to: int, msg_id: int):
        self.uid, self.chat_id = uid, chat_id
        self.reply_to, self.msg_id = reply_to, msg_id
        self.cancelled = False
        self.task: Optional[asyncio.Task] = None
        self.st: dict = {"pct": 0.0, "done": 0.0, "total": 0.0, "speed": "", "eta": "",
                         "stage": "download", "note": ""}
        self.log: Optional[JobLog] = None
        self.st["log"] = None


class Bot:
    def __init__(self, tg: Telegram):
        self.tg = tg
        self.sem = asyncio.Semaphore(MAX_CONCURRENT)
        self.busy: dict[int, Job] = {}
        self.active: set[Job] = set()
        self.sessions: dict[str, dict] = {}

    # ── دسترسی ──
    def allowed(self, uid: int) -> bool:
        if not ALLOWED_USERS and not OWNER_ID:
            return True
        return uid == OWNER_ID or uid in ALLOWED_USERS

    # ── حلقهٔ اصلی ──
    async def run(self):
        offset = 0
        while True:
            try:
                res = await self.tg.call("getUpdates", {
                    "offset": offset, "timeout": 30,
                    "allowed_updates": ["message", "callback_query"]}, timeout=60) or []
                for upd in res:
                    offset = max(offset, int(upd.get("update_id", 0)) + 1)
                    if upd.get("message"):
                        asyncio.create_task(self.on_message(upd["message"]))
                    elif upd.get("callback_query"):
                        asyncio.create_task(self.on_callback(upd["callback_query"]))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print("[poll] %s" % e, flush=True)
                await asyncio.sleep(3)

    # ── پیام متنی ──
    async def on_message(self, msg: dict):
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        uid = int((msg.get("from") or {}).get("id") or 0)
        text = (msg.get("text") or msg.get("caption") or "").strip()
        mid = int(msg.get("message_id") or 0)
        if not chat_id or not text:
            return
        if not self.allowed(uid):
            await self.tg.send(chat_id, "⛔️ دسترسی نداری.\nآیدی عددی تو: `%d`\n"
                                        "(این را برای ادمین بفرست.)" % uid, reply_to=mid)
            return

        low = text.lower()
        if low.startswith(("/start", "/help")):
            await self.tg.send(chat_id, self.help_text(), reply_to=mid)
            return
        if low.startswith("/id"):
            await self.tg.send(chat_id, "آیدی عددی تو: `%d`" % uid, reply_to=mid)
            return
        if low.startswith("/status"):
            await self.tg.send(chat_id, await self.status_text(), reply_to=mid)
            return
        if low.startswith("/log"):
            await self.send_log(chat_id, uid, mid)
            return
        if low.startswith("/cancel"):
            job = self.busy.get(uid)
            if job:
                job.cancelled = True
                if job.task:
                    job.task.cancel()
                self.busy.pop(uid, None)
                self.active.discard(job)
                await self.tg.send(chat_id, "🛑 لغو شد.", reply_to=mid)
            else:
                await self.tg.send(chat_id, "چیزی برای لغو نبود.", reply_to=mid)
            return

        # ── دستور لیچ: ‎/leech لینک‎ — یا ریپلای روی پیامی که لینک دارد ──
        if low.startswith("/leech"):
            parts = text.split(None, 1)
            rest = parts[1].strip() if len(parts) > 1 else ""
            if not rest:
                rep = msg.get("reply_to_message") or {}
                rest = (rep.get("text") or rep.get("caption") or "").strip()
            if not rest:
                await self.tg.send(
                    chat_id,
                    "🔗 *دستور لیچ*\n"
                    "───────────────\n"
                    "اینجوری بزن:  `/leech لینک`\n"
                    "یا روی پیامی که لینک دارد ریپلای کن و `/leech` بفرست.\n\n"
                    "فقط لینک هم بفرستی، همان کار را می‌کند. 🙂",
                    reply_to=mid)
                return
            text = rest

        found = URL_RE.search(text)
        if not found:
            await self.tg.send(chat_id, "یک لینک بفرست تا کیفیت‌های موجودش را نشانت بدهم. "
                                        "راهنما: /help", reply_to=mid)
            return
        url = found.group(0).rstrip(").,»\"'")

        if uid in self.busy:
            await self.tg.send(chat_id, "⏳ همین حالا یک دانلود برایت در جریان است. "
                                        "صبر کن تمام شود یا /cancel بزن.", reply_to=mid)
            return

        status = await self.tg.send(chat_id, "🔎 در حال بررسی لینک و کیفیت‌ها…", reply_to=mid)
        sid = secrets.token_hex(4)
        probe, err = await probe_url(url)
        smid = int((status or {}).get("message_id") or 0)
        if not probe or not probe.options:
            msg = {
                "unsupported": "🤷‍♂️ از این لینک چیزی برای دانلود پیدا نکردم.",
                "login": "🔒 این لینک خصوصی است یا ورود می‌خواهد.",
            }.get(err, "❌ نتوانستم این لینک را بررسی کنم.")
            await self.tg.edit(chat_id, smid, msg)
            return
        self.sessions[sid] = {"probe": probe, "uid": uid, "at": time.time(),
                              "chat_id": chat_id, "mid": smid, "reply_to": mid}
        self._gc_sessions()
        await self.tg.edit(chat_id, smid, menu_text(probe), kb=menu_keyboard(sid, probe))

    # ── انتخاب کیفیت (دکمه) ──
    async def on_callback(self, cb: dict):
        data = str(cb.get("data") or "")
        cid = str(cb.get("id") or "")
        msg = cb.get("message") or {}
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        mid = int(msg.get("message_id") or 0)
        uid = int((cb.get("from") or {}).get("id") or 0)
        if data == "log:last":
            await self.tg.answer_cb(cid, "می‌فرستم…")
            await self.send_log(chat_id, uid, 0)
            return
        if not data.startswith("q:") or not chat_id:
            await self.tg.answer_cb(cid)
            return
        _, sid, key = (data.split(":", 2) + ["", ""])[:3]
        sess = self.sessions.get(sid)
        if not sess or sess["uid"] != uid:
            await self.tg.answer_cb(cid, "این منو منقضی شده — لینک را دوباره بفرست.")
            return
        option = next((o for o in sess["probe"].options if o.key == key), None)
        if not option:
            await self.tg.answer_cb(cid, "گزینه پیدا نشد.")
            return
        if not self.allowed(uid) :
            await self.tg.answer_cb(cid, "دسترسی نداری.")
            return
        if uid in self.busy:
            await self.tg.answer_cb(cid, "یک دانلود دیگر در جریان است.")
            return
        await self.tg.answer_cb(cid, "شروع شد ✅")
        self.sessions.pop(sid, None)
        job = Job(uid, chat_id, sess["reply_to"], mid)
        self.busy[uid] = job
        job.task = asyncio.create_task(self._work(job, sess["probe"], option))

    def _gc_sessions(self):
        now = time.time()
        for k in [k for k, v in self.sessions.items() if now - v["at"] > SESSION_TTL]:
            self.sessions.pop(k, None)
        while len(self.sessions) > 500:
            self.sessions.pop(next(iter(self.sessions)))

    # ── متن‌ها ──
    def help_text(self) -> str:
        cap = "%dMB" % MAX_UPLOAD_MB if MAX_UPLOAD_MB < 1024 else "%dMB (≈%.1fGB)" % (
            MAX_UPLOAD_MB, MAX_UPLOAD_MB / 1024)
        extra = "" if (ALLOWED_USERS or OWNER_ID) else \
            "\n\n⚠️ بدون ALLOWED_USERS/OWNER_ID اجرا شده — هر کسی می‌تواند استفاده کند."
        return (
            "🎬 *ربات دانلود از لینک*\n"
            "───────────────\n"
            "لینک را بفرست؛ کیفیت‌های موجود با حجم نشان داده می‌شود، "
            "انتخاب کن و دانلود با نوار پیشرفت شروع می‌شود. آخرش فیلم با "
            "کپشن و لینک اصلی تحویل داده می‌شود.\n\n"
            "• هر لینکی: صفحهٔ سایت، لینک مستقیم، `.mp4`، `.ts`، `m3u8` و…\n"
            "• فایل غیر mp4 خودکار (بدون افت کیفیت) به mp4 تبدیل می‌شود تا ویدیو بماند\n"
            "• /leech لینک — همان دانلود، ولی با دستور (مثل ربات‌های لیچ)\n"
            "• سقف ارسال فعلی: *%s*\n"
            "• /status وضعیت · /cancel لغو · /log لاگِ آخرین خطا · /id آیدی تو" % cap
        ) + extra

    async def status_text(self) -> str:
        cap = "%dMB" % MAX_UPLOAD_MB if MAX_UPLOAD_MB < 1024 else "%dMB (≈%.2fGB)" % (
            MAX_UPLOAD_MB, MAX_UPLOAD_MB / 1024)
        lines = ["📊 *وضعیت*",
                 "───────────────",
                 "• سرور API: %s" % ("محلی" if LOCAL_API else "ابر تلگرام"),
                 "• سقف ارسال: %s" % cap,
                 "• در جریان: %d (هم‌زمان: %d)" % (len(self.active), MAX_CONCURRENT),
                 "• پوشهٔ موقت: %.1fMB" % tree_size_mb(DOWNLOAD_DIR),
                 "• نسخه: %s · زمان اجرا: %s" % (VERSION, hhmmss(time.time() - START_TS)),
                 "• لاگِ خطا: %s" % ("ذخیره شده (با /log بفرست)" if (LAST_LOG.get("text") or "").strip()
                                     else "خالی — بعد از هر دانلود موفق پاک می‌شود")]
        for j in list(self.active):
            lines.append("  – %s" % cut(j.st.get("title") or "?", 50))
        return "\n".join(lines)

    async def send_log(self, chat_id: int, uid: int, reply_to: int):
        """لاگ آخرین دانلود را می‌فرستد (کوتاه ⇒ متن، بلند ⇒ فایل txt)."""
        text = (LAST_LOG.get("text") or "").strip()
        if not text:
            await self.tg.send(chat_id,
                              "📄 لاگی برای فرستادن نیست.\n"
                              "(بعد از هر دانلودِ **موفق** لاگ پاک می‌شود؛ اگر لینکی "
                              "فایل نداد، خودش لاگ نگه می‌دارد و همین‌جا می‌فرستم.)",
                              reply_to=reply_to)
            return
        if uid not in (OWNER_ID, LAST_LOG.get("uid")) and not self.allowed(uid):
            await self.tg.send(chat_id, "⛔️ دسترسی نداری.", reply_to=reply_to)
            return
        head = ("📄 لاگ %s\nلینک: %s\nزمان: %s" % (
            "آخرین دانلود ناموفق" if LAST_LOG.get("ok") is False else "آخرین دانلود",
            cut(LAST_LOG.get("url") or "—", 70),
            time.strftime("%H:%M:%S", time.localtime(LAST_LOG.get("at") or time.time()))))
        if len(text) <= 3500:
            await self.tg.send(chat_id, head + "\n\n`" + text.replace("`", "'") + "`",
                              reply_to=reply_to)
        else:
            data = text.encode("utf-8", "replace")
            name = "log-%s.txt" % time.strftime("%m%d-%H%M%S")
            await self.tg.send_doc(chat_id, name, data, head, reply_to)

    # ── بدنهٔ کار ──
    async def _work(self, job: Job, probe: Probe, option: Option):
        st = job.st
        st["title"] = probe.title or option.label
        # لاگِ قبلی پاک می‌شود؛ از این لحظه فقط لاگِ همین دانلود ثبت می‌گردد
        st["log"] = JobLog(job.uid, probe.url)
        lg = st["log"]
        lg.add("start", "کیفیت انتخابی: %s | تخمین: %s" % (option.label, human(option.est)))
        lg.add("probe", "عنوان: %s | مدت: %s | گزینه‌ها: %s" % (
            cut(probe.title, 80) or "—", hhmmss(probe.duration) if probe.duration else "—",
            "، ".join("%s(%s)" % (o.label, human(o.est)) for o in probe.options)))

        async def ticker():
            while True:
                await asyncio.sleep(2.5)
                if job.msg_id:
                    stage = st.get("stage", "download")
                    await self.tg.edit(job.chat_id, job.msg_id,
                                       progress_text(stage, st.get("pct", 0), st.get("done", 0),
                                                     st.get("total", 0), st.get("speed", ""),
                                                     st.get("eta", ""), st.get("note", "")))

        tick = asyncio.create_task(ticker())
        folder = DOWNLOAD_DIR / ("u%d_%d" % (job.uid, int(time.time())))
        try:
            async with self.sem:
                self.active.add(job)
                if job.msg_id:
                    await self.tg.edit(job.chat_id, job.msg_id,
                                       "⏳ انتخاب: *%s* — شروع دانلود…" % option.label)
                await self.tg.action(job.chat_id, "upload_video")

                # ── دانلود ──
                st.update({"stage": "download", "pct": 0, "done": 0, "total": option.est or 0,
                           "speed": "", "eta": "", "note": ""})
                if job.msg_id:      # نوار از همان لحظهٔ اول دیده شود
                    await self.tg.edit(job.chat_id, job.msg_id,
                                       progress_text("download", 0, 0, option.est or 0,
                                                     "", "", st.get("note", "")))
                if option.direct or DOWNLOADER == "aria2":
                    path, err = await download_aria2(probe.url, folder, st)
                    if not path:
                        path, err = await download_ytdlp(probe.url, option, folder, st)
                else:
                    path, err = await download_ytdlp(probe.url, option, folder, st)
                    if not path and AUTO_FALLBACK and err in ("unsupported", "failed"):
                        st["note"] = "yt-dlp نشد — با aria2c تلاش می‌کنم"
                        path, err = await download_aria2(probe.url, folder, st)
                if job.cancelled:
                    lg.add("cancel", "کاربر دانلود را لغو کرد")
                    _set_last_log(lg, False, "لغو توسط کاربر")
                    return
                if not path:
                    lg.add("download", "ناموفق — کد خطا: %s" % err)
                    _set_last_log(lg, False, "دانلود", err)
                    await self._error(job, err)
                    return
                size = path.stat().st_size
                lg.add("download", "موفق: %s (%s) در %s" % (path.name, human(size), lg.elapsed))
                if size > MAX_DOWNLOAD_MB * 1048576:
                    await self._say(job, "❌ فایل %s شد — بیشتر از سقف %dMB."
                                    % (human(size), MAX_DOWNLOAD_MB))
                    return
                info = read_info(folder)
                title = str(info.get("title") or probe.title or path.stem)
                descr = str(info.get("description") or probe.description or "")

                # ── آماده‌سازی ویدیو (تبدیل کانتینر) ──
                st.update({"stage": "remux", "pct": 1, "done": size, "total": size,
                           "speed": "", "eta": "", "note": ""})
                if job.msg_id:
                    await self.tg.edit(job.chat_id, job.msg_id,
                                       progress_text("remux", 1, size, size))
                path, why, meta = await to_sendable_video(path, folder, st)
                lg.add("remux", "خروجی: %s | وضعیت: %s | ابعاد: %sx%s | مدت: %ss" % (
                    path.name, why or "بدون تبدیل", meta.get("width"), meta.get("height"),
                    meta.get("duration")))
                if job.cancelled:
                    lg.add("cancel", "لغو بعد از تبدیل")
                    _set_last_log(lg, False, "لغو توسط کاربر")
                    return
                size = path.stat().st_size
                kind = "video" if (path.suffix.lower() in VIDEO_EXT and why != "codec") else \
                       ("audio" if path.suffix.lower() in AUDIO_EXT else "document")

                # ── سقف ارسال ──
                cap = MAX_UPLOAD_MB * 1048576
                if size > cap:
                    lg.add("upload", "متوقف: حجم %s از سقف %dMB بیشتر است" % (human(size), MAX_UPLOAD_MB))
                    _set_last_log(lg, False, "سقف ارسال",
                                  "حجم %s > سقف %dMB" % (human(size), MAX_UPLOAD_MB))
                    await self._say(job,
                                    "📦 فایل %s شد و سقف ارسال فعلی *%s* است.\n"
                                    "• کیفیت پایین‌تر انتخاب کن\n"
                                    "• یا برای ۲GB، سرور Bot API محلی را وصل کن "
                                    "(`BOT_API_BASE` → راهنمای README)"
                                    % (human(size), ("%dMB" % MAX_UPLOAD_MB) if MAX_UPLOAD_MB < 1024
                                       else ("%dMB ≈ %.1fGB" % (MAX_UPLOAD_MB, MAX_UPLOAD_MB / 1024))))
                    return

                # ── آپلود با نوار مربعی ──
                st.update({"stage": "upload", "pct": 0, "done": 0, "total": size,
                           "speed": "", "eta": "", "note": ""})
                holder = {"n": 0.0, "t0": time.time()}
                if job.msg_id:
                    await self.tg.edit(job.chat_id, job.msg_id,
                                       progress_text("upload", 0, 0, size))

                def on_read(n, total):
                    holder["n"] = n
                    el = max(0.001, time.time() - holder["t0"])
                    holder["speed"] = "%.1fMB/s" % (n / el / 1048576) if n > 0 else ""

                stream = ProgressFile(path, on_read)

                async def up_ticker():
                    last = 0
                    while True:
                        await asyncio.sleep(1.0)
                        n = holder["n"]
                        pct = (n / size * 100.0) if size else 0.0
                        if n == last and n < size:            # اگر شمارش نرسید ⇒ انیمیشن زمانی
                            el = time.time() - holder["t0"]
                            pct = min(95.0, el * 3.0)
                        last = n
                        st.update({"pct": pct, "done": n})
                        if job.msg_id:
                            await self.tg.edit(job.chat_id, job.msg_id,
                                               progress_text("upload", pct, n, size,
                                                             holder.get("speed", "")))

                utick = asyncio.create_task(up_ticker())
                extra_note = ""
                if kind == "document" and path.suffix.lower() in VIDEO_EXT:
                    extra_note = ("ℹ️ کدک این فایل داخل mp4 پشتیبانی نمی‌شود، پس به‌شکل "
                                  "فایل (نه ویدیو) فرستاده شد.")
                caption = build_caption(title, descr, probe.url, size, meta, extra_note)
                form = aiohttp.FormData()
                form.add_field("chat_id", str(job.chat_id))
                if job.reply_to:
                    form.add_field("reply_to_message_id", str(job.reply_to))
                form.add_field("caption", caption)
                form.add_field("parse_mode", "HTML")
                field = {"video": "video", "audio": "audio", "document": "document"}[kind]
                method = {"video": "sendVideo", "audio": "sendAudio",
                          "document": "sendDocument"}[kind]
                if kind == "video":
                    form.add_field("supports_streaming", "true")
                    for k in ("duration", "width", "height"):
                        if meta.get(k):
                            form.add_field(k, str(int(meta[k])))
                form.add_field(field, stream, filename=path.name,
                               content_type="application/octet-stream")
                try:
                    await self.tg.action(job.chat_id,
                                         "upload_video" if kind == "video" else "upload_document")
                    await self.tg.call(method, form=form, timeout=7200)
                finally:
                    utick.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await utick
                    with contextlib.suppress(Exception):
                        stream.close()
                lg.add("upload", "موفق: %s به‌شکل %s در %s" % (
                    human(size), kind, hhmmss(time.time() - holder["t0"])))
                lg.add("done", "کل زمان: %s" % lg.elapsed)
                _set_last_log(lg, True)          # ✅ موفق ⇒ لاگ پاک می‌شود
                if job.msg_id:
                    await self.tg.edit(job.chat_id, job.msg_id,
                                       "✅ ارسال شد  ·  %s  ·  %s" % (human(size),
                                                                      hhmmss(time.time() - holder["t0"])))
        except asyncio.CancelledError:
            if job.msg_id:
                asyncio.create_task(self.tg.edit(job.chat_id, job.msg_id, "🛑 لغو شد."))
            raise
        except Exception as e:
            msg = str(e)
            lg = st.get("log")
            if lg:
                lg.add("error", msg[:500])
            if "too big" in msg.lower() or "413" in msg or "file is too large" in msg.lower():
                _set_last_log(lg, False, "آپلود", "تلگرام فایل را بزرگ‌تر از حد مجاز دانست")
                await self._say(job, "❌ تلگرام فایل را بزرگ‌تر از حد مجاز دانست.\n"
                                     "کیفیت پایین‌تر را انتخاب کن یا سرور Bot API محلی را وصل کن.")
            else:
                _set_last_log(lg, False, "خطای غیرمنتظره", msg[:200])
                await self._say(job, "❌ خطا: %s\n\n📄 برای جزئیات /log را بزن." % cut(msg, 250))
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tick
            self.active.discard(job)
            if self.busy.get(job.uid) is job:
                self.busy.pop(job.uid, None)
            if not KEEP_FILES:
                shutil.rmtree(folder, ignore_errors=True)

    async def _say(self, job: Job, text: str):
        if job.msg_id:
            await self.tg.edit(job.chat_id, job.msg_id, text)
        else:
            await self.tg.send(job.chat_id, text, reply_to=job.reply_to)

    async def _error(self, job: Job, err: str):
        table = {
            "big": "❌ فایل از سقف %dMB بزرگ‌تر است." % MAX_DOWNLOAD_MB,
            "login": "🔒 این لینک خصوصی است یا ورود می‌خواهد.",
            "unsupported": "🤷‍♂️ از این سایت پشتیبانی نمی‌کنم یا لینک، ویدیو نیست.",
            "timeout": "⌛️ زمان اتصال تمام شد — دوباره امتحان کن.",
            "missing": "❌ فایل روی سرور پیدا نشد (لینک منقضی شده؟).",
            "failed": "❌ دانلود نشد. لینک را چک کن یا کمی بعد دوباره بفرست.",
        }
        base = table.get(err, "❌ دانلود نشد: %s" % cut(err, 200))
        text = base + "\n\n📄 برای دیدن مشکل، /log را بزن:"
        kb = {"inline_keyboard": [[{"text": "📄 لاگ", "callback_data": "log:last"}]]}
        if job.msg_id:
            await self.tg.edit(job.chat_id, job.msg_id, text, kb=kb)
        else:
            await self.tg.send(job.chat_id, text, reply_to=job.reply_to)


# ─────────────────────────── نگهبان دیسک ───────────────────────────


async def sweeper():
    while True:
        await asyncio.sleep(600)
        try:
            now = time.time()
            for f in DOWNLOAD_DIR.glob("**/*"):
                with contextlib.suppress(OSError):
                    if f.is_file() and now - f.stat().st_mtime > 3600:
                        f.unlink()
            for d in DOWNLOAD_DIR.glob("u*"):
                with contextlib.suppress(OSError):
                    if d.is_dir() and not any(d.iterdir()) and now - d.stat().st_mtime > 1800:
                        d.rmdir()
            if tree_size_mb(DOWNLOAD_DIR) > DISK_LIMIT_MB:
                print("[sweeper] over disk limit — clearing", flush=True)
                for d in DOWNLOAD_DIR.glob("u*"):
                    shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            print("[sweeper] %s" % e, flush=True)


# ─────────────────────────── اجرا ───────────────────────────


async def main() -> int:
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN ست نشده. در Railway → Variables مقدارش را بگذار.", flush=True)
        return 2
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tg = Telegram(BOT_TOKEN, API_BASE)
    await tg.start()
    print("✅ ربات @%s آماده است  (نسخه %s)" % ((tg.me or {}).get("username") or "?", VERSION), flush=True)
    cap_txt = ("%dMB" % MAX_UPLOAD_MB) if MAX_UPLOAD_MB < 1024 else ("%dMB ≈ %.2fGB" % (
        MAX_UPLOAD_MB, MAX_UPLOAD_MB / 1024))
    print("   سرور API: %s → سقف ارسال %s" % (API_BASE, cap_txt), flush=True)
    print("   پوشه: %s · سقف دانلود: %dMB · هم‌زمان: %d · تبدیل به mp4: %s"
          % (DOWNLOAD_DIR, MAX_DOWNLOAD_MB, MAX_CONCURRENT, "روشن" if FORCE_MP4 else "خاموش"),
          flush=True)
    if not LOCAL_API:
        print("   ℹ️ برای ارسال تا ۲GB، سرور Bot API محلی را وصل کن (README → بخش ۲GB).", flush=True)
    if not ALLOWED_USERS and not OWNER_ID:
        print("   ⚠️ بدون لیست دسترسی — ربات برای همه باز است.", flush=True)

    with contextlib.suppress(Exception):
        await tg.call("setMyCommands", {"commands": [
            {"command": "leech", "description": "دانلود فیلم از لینک (لیچ)"},
            {"command": "start", "description": "راهنما و شروع"},
            {"command": "status", "description": "وضعیت ربات و سقف ارسال"},
            {"command": "log", "description": "لاگ آخرین خطا"},
            {"command": "cancel", "description": "لغو دانلود در جریان"},
            {"command": "id", "description": "آیدی عددی من"},
        ]})
        print("   منوی دستورها ثبت شد (leech/start/status/log/cancel/id)", flush=True)
    bot = Bot(tg)
    tasks = [asyncio.create_task(bot.run()), asyncio.create_task(sweeper())]
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    for t in tasks:
        t.cancel()
    await tg.close()
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    print("👋 خاموش شد.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()) or 0)
    except KeyboardInterrupt:
        sys.exit(0)
