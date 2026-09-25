#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات دانلود از لینک — نسخهٔ سبک برای Railway
============================================
کاری که می‌کند: یک لینک می‌فرستی، فایل/ویدیو را دانلود می‌کند و همان‌جا در همین چت
تحویل می‌دهد.

طراحی عمداً کوچک است:
  • فقط Bot API رسمی تلگرام (بدون سشنِ کاربر).
  • فقط دانلود HTTP/HTTPS: aria2c برای لینک مستقیم، yt-dlp برای سایت‌ها.
  • هیچ سرور فایلِ عمومی و هیچ پورتِ ورودیِ بازی ندارد.
  • هر کاربر هم‌زمان یک دانلود؛ کل ربات چند دانلود موازی (MAX_CONCURRENT).

همهٔ تنظیمات از متغیرهای محیطی می‌آید — توضیح کامل در README.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp

# ─────────────────────────── تنظیمات ───────────────────────────

START_TS = time.time()


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


BOT_TOKEN = _env("BOT_TOKEN")
API_BASE = _env("BOT_API_BASE", "https://api.telegram.org").rstrip("/")

OWNER_ID = int(_env("OWNER_ID", "0") or 0)
ALLOWED_USERS = {
    int(x) for x in re.split(r"[,\s]+", _env("ALLOWED_USERS", "")) if x.strip().isdigit()
}

DOWNLOAD_DIR = Path(_env("DOWNLOAD_DIR", "/tmp/downloads"))
MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB", "49"))        # سقف ارسال در تلگرام برای ربات‌ها ۵۰MB است
MAX_DOWNLOAD_MB = int(_env("MAX_DOWNLOAD_MB", "400"))   # سقف فایلِ ورودی
MAX_CONCURRENT = max(1, int(_env("MAX_CONCURRENT", "2")))
DISK_LIMIT_MB = int(_env("DISK_LIMIT_MB", "2000"))

DOWNLOADER = _env("DOWNLOADER", "auto").lower()          # auto | ytdlp | aria2
AUTO_FALLBACK = _env("AUTO_FALLBACK", "1") not in ("0", "false", "no", "off")
KEEP_FILES = _env("KEEP_FILES", "0") in ("1", "true", "yes", "on")
COOKIES_FILE = _env("COOKIES_FILE")                      # کوکی برای سایت‌هایی که ورود می‌خواهند

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts", ".3gp"}
AUDIO_EXT = {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".aac", ".flac"}
FILE_EXT = {".zip", ".rar", ".7z", ".pdf", ".apk"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


# ─────────────────────────── کمکی‌ها ───────────────────────────


def human(n: float) -> str:
    n = float(n or 0)
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


# ─────────────────────────── تلگرام ───────────────────────────


class Telegram:
    def __init__(self, token: str, base: str):
        self.token = token
        self.base = base
        self.url = "%s/bot%s" % (base, token)
        self.session: Optional[aiohttp.ClientSession] = None
        self.me: dict = {}

    async def start(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
        )
        self.me = await self.call("getMe") or {}
        with contextlib.suppress(Exception):
            await self.call("deleteWebhook", {"drop_pending_updates": False})

    async def close(self):
        if self.session:
            await self.session.close()

    async def call(self, method: str, data=None, form=None, timeout: float = 90):
        """Bot API؛ خطاها را با پیام روشن بالا می‌برد."""
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

    async def send(self, chat_id: int, text: str, reply_to: int = 0):
        data = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if reply_to:
            data["reply_to_message_id"] = reply_to
        return await self.call("sendMessage", data)

    async def edit(self, chat_id: int, message_id: int, text: str):
        with contextlib.suppress(Exception):
            await self.call("editMessageText",
                            {"chat_id": chat_id, "message_id": message_id,
                             "text": text[:4000], "disable_web_page_preview": True})

    async def action(self, chat_id: int, what: str = "upload_video"):
        with contextlib.suppress(Exception):
            await self.call("sendChatAction", {"chat_id": chat_id, "action": what})

    async def send_file(self, chat_id: int, path: Path, kind: str, caption: str,
                        reply_to: int = 0):
        size = path.stat().st_size
        field = {"video": "video", "audio": "audio", "document": "document"}[kind]
        method = {"video": "sendVideo", "audio": "sendAudio", "document": "sendDocument"}[kind]
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if reply_to:
            form.add_field("reply_to_message_id", str(reply_to))
        if caption:
            form.add_field("caption", caption[:1000])
        if kind == "video":
            form.add_field("supports_streaming", "true")
        small = size <= 64 * 1048576
        fh = None
        if small:
            form.add_field(field, path.read_bytes(),
                           filename=path.name, content_type="application/octet-stream")
        else:
            fh = open(path, "rb")
            form.add_field(field, fh, filename=path.name,
                           content_type="application/octet-stream")
        try:
            return await self.call(method, form=form, timeout=3600)
        finally:
            if fh is not None:
                with contextlib.suppress(Exception):
                    fh.close()


# ─────────────────────────── پیشرفت ───────────────────────────


class Progress:
    def __init__(self):
        self.pct = 0.0
        self.total = 0.0
        self.done = 0.0
        self.speed = ""
        self.eta = ""
        self.stage = "download"
        self.note = ""
        self.t0 = time.time()

    def text(self) -> str:
        if self.stage == "upload":
            return "⬆️ در حال فرستادن به تلگرام…  (%s)" % human(self.done)
        head = "⬇️ دانلود"
        if self.pct > 0:
            head += " %d%%" % int(self.pct)
        parts = []
        if self.total:
            parts.append("%s / %s" % (human(self.done), human(self.total)))
        elif self.done:
            parts.append(human(self.done))
        if self.speed:
            parts.append(self.speed)
        if self.eta:
            parts.append("ETA " + self.eta)
        line = head + ("  ·  " + "  ·  ".join(parts) if parts else "")
        if self.note:
            line += "\n" + self.note
        return line


def parse_progress(line: str, pr: Progress):
    """درصد/سرعت/ETA را از خروجی yt-dlp یا aria2c بیرون می‌کشد."""
    m = re.search(r"\[download\]\s+([\d.]+)%", line)
    if m:
        pr.pct = float(m.group(1))
        m2 = re.search(r"of\s+~?\s*([\d.]+)(B|KiB|MiB|GiB)", line)
        if m2:
            pr.total = float(m2.group(1)) * {"B": 1, "KiB": 1024, "MiB": 1048576, "GiB": 1073741824}[m2.group(2)]
        m3 = re.search(r"at\s+([\d.]+\s*\w+B/s)", line)
        if m3:
            pr.speed = m3.group(1).replace(" ", "")
        m4 = re.search(r"ETA\s+([\d:]+)", line)
        if m4:
            pr.eta = m4.group(1)
        if pr.total:
            pr.done = pr.total * pr.pct / 100.0
        return
    m = re.search(r"\((\d+)%\)", line)
    if m:
        pr.pct = float(m.group(1))
        m3 = re.search(r"DL:([\d.]+\w+)", line)
        if m3:
            pr.speed = m3.group(1)
        m4 = re.search(r"ETA:(\S+?)\]", line)
        if m4:
            pr.eta = m4.group(1)
        m2 = re.search(r"\s([\d.]+\w+)/([\d.]+\w+)\(", line)

        def to_bytes(s):
            mm = re.match(r"([\d.]+)(\w+)", s or "")
            if not mm:
                return 0.0
            return float(mm.group(1)) * {"B": 1, "KiB": 1024, "MiB": 1048576, "GiB": 1073741824}.get(mm.group(2), 1)

        if m2:
            pr.done, pr.total = to_bytes(m2.group(1)), to_bytes(m2.group(2))


async def run_proc(cmd: list[str], pr: Progress, watch_dir: Path):
    """پروسه را اجرا و خط‌به‌خط می‌خواند؛ اگر حجم از سقف رد شود می‌کُشد."""
    if not shutil.which(cmd[0]):
        return 127, "not installed: %s" % cmd[0]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        cwd=str(DOWNLOAD_DIR),
    )
    tail: list[str] = []
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

    read_task = asyncio.create_task(reader())
    try:
        while True:
            try:
                line = await asyncio.wait_for(queue.get(), timeout=5)
            except asyncio.TimeoutError:
                if proc.returncode is None and tree_size_mb(watch_dir) > MAX_DOWNLOAD_MB * 1.05:
                    oversize = True
                    with contextlib.suppress(Exception):
                        proc.kill()
                continue
            if line is None:
                break
            if line:
                tail.append(line)
                if len(tail) > 40:
                    tail.pop(0)
                parse_progress(line, pr)
    finally:
        # لغو کارِ کاربر ⇒ پروسهٔ دانلود هم نباید بماند
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.kill()
        read_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(read_task, timeout=10)
    code = await proc.wait()
    if oversize:
        return 99, "MAX_FILESIZE"
    return code, "\n".join(tail)


def pick_output(folder: Path, stem: str) -> Optional[Path]:
    """جدیدترین فایلِ خروجی با نام stem.* (بدون فایل‌های موقت/متادیتا)."""
    bad = {".part", ".ytdl", ".json", ".temp", ".tmp"}
    best, best_t = None, 0.0
    for f in folder.glob(stem + ".*"):
        with contextlib.suppress(OSError):
            if f.is_file() and f.suffix.lower() not in bad:
                t = f.stat().st_mtime
                if t >= best_t:
                    best, best_t = f, t
    return best


def read_info(folder: Path, stem: str) -> dict:
    p = folder / (stem + ".info.json")
    out = {}
    with contextlib.suppress(Exception):
        if p.exists():
            out = json.loads(p.read_text("utf-8", "replace")) or {}
            p.unlink()
    return out


# ─────────────────────────── دانلود ───────────────────────────


async def download_ytdlp(url: str, mode: str, folder: Path, pr: Progress):
    """دانلود با yt-dlp (سایت‌ها: یوتیوب، اینستاگرام، توییتر، تیک‌تاک و…)."""
    folder.mkdir(parents=True, exist_ok=True)
    out_tpl = str(folder / "out.%(ext)s")
    cmd = [sys.executable, "-m", "yt_dlp",
           "--no-playlist", "--no-warnings", "--newline",
           "--retries", "3", "--fragment-retries", "3", "--socket-timeout", "30",
           "--merge-output-format", "mp4",
           "--max-filesize", "%dM" % MAX_DOWNLOAD_MB,
           "--write-info-json",
           "--user-agent", UA,
           "-o", out_tpl]
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        cmd += ["--cookies", COOKIES_FILE]
    if mode == "audio":
        cmd += ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    elif mode == "low":
        cmd += ["-f", "bv*[height<=480]+ba/b[height<=480]/b"]
    else:
        cmd += ["-f", "bv*[height<=720]+ba/b[height<=720]/b"]
    cmd += ["--", url]

    code, out = await run_proc(cmd, pr, folder)
    if code == 99:
        return None, "", "big"
    path = pick_output(folder, "out")
    if path is None:
        low = out.lower()
        if "larger than max" in low or "max-filesize" in low:
            return None, "", "big"
        if "private" in low or "login" in low or "sign in" in low or "cookies" in low:
            return None, "", "login"
        if "unsupported url" in low or "no video" in low:
            return None, "", "unsupported"
        if "timed out" in low or "timeout" in low:
            return None, "", "timeout"
        return None, "", "failed"
    info = read_info(folder, "out")
    pr.done = path.stat().st_size
    pr.pct = 100.0
    return path, str(info.get("title") or ""), ""


async def download_aria2(url: str, folder: Path, pr: Progress):
    """دانلود لینک مستقیم با aria2c (چند اتصاله)."""
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
           "-d", str(folder), "-o", name, url]
    code, out = await run_proc(cmd, pr, folder)
    if code == 99:
        return None, "", "big"
    p = folder / name
    if code == 0 and p.exists() and p.stat().st_size > 0:
        pr.done, pr.pct = p.stat().st_size, 100.0
        return p, name, ""
    low = out.lower()
    if "no such file" in low or "not found" in low:
        return None, "", "missing"
    return None, "", "failed"


async def is_direct_link(url: str) -> bool:
    """لینک خامِ فایل (⇒ aria2c) یا صفحهٔ سایت (⇒ yt-dlp)؟"""
    if DOWNLOADER == "aria2":
        return True
    if DOWNLOADER == "ytdlp":
        return False
    path = url.split("?")[0].lower()
    if any(path.endswith(e) for e in VIDEO_EXT | AUDIO_EXT | FILE_EXT):
        return True
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.head(url, allow_redirects=True, headers={"User-Agent": UA}) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                disp = (r.headers.get("Content-Disposition") or "").lower()
                return (ctype.startswith(("video/", "audio/", "application/octet-stream"))
                        or "attachment" in disp)
    except Exception:
        return False


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "document"


# ─────────────────────────── کار ───────────────────────────


class Job:
    def __init__(self, uid: int, url: str, mode: str, chat_id: int, reply_to: int, msg_id: int):
        self.uid = uid
        self.url = url
        self.mode = mode
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.msg_id = msg_id
        self.progress = Progress()
        self.task: Optional[asyncio.Task] = None
        self.cancelled = False


class Bot:
    def __init__(self, tg: Telegram):
        self.tg = tg
        self.sem = asyncio.Semaphore(MAX_CONCURRENT)
        self.busy: dict[int, Job] = {}
        self.active: set[Job] = set()

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
                    "offset": offset, "timeout": 30, "allowed_updates": ["message"]}, timeout=60) or []
                for upd in res:
                    offset = max(offset, int(upd.get("update_id", 0)) + 1)
                    msg = upd.get("message")
                    if msg:
                        asyncio.create_task(self.on_message(msg))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print("[poll] %s" % e, flush=True)
                await asyncio.sleep(3)

    # ── پیام ──
    async def on_message(self, msg: dict):
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        uid = int((msg.get("from") or {}).get("id") or 0)
        text = (msg.get("text") or msg.get("caption") or "").strip()
        mid = int(msg.get("message_id") or 0)
        if not chat_id or not text:
            return
        if not self.allowed(uid):
            await self.tg.send(chat_id, "⛔️ دسترسی نداری.\nآیدی عددی تو: %d\n"
                                        "(این را برای ادمین بفرست تا اضافه کند.)" % uid, reply_to=mid)
            return

        low = text.lower()
        if low.startswith(("/start", "/help")):
            await self.tg.send(chat_id, self.help_text(), reply_to=mid)
            return
        if low.startswith("/id"):
            await self.tg.send(chat_id, "آیدی عددی تو: %d" % uid, reply_to=mid)
            return
        if low.startswith("/status"):
            await self.tg.send(chat_id, await self.status_text(), reply_to=mid)
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

        mode, body = "video", text
        for prefix, m in (("/audio", "audio"), ("/low", "low"), ("/dl", "video"), ("/video", "video")):
            if low.startswith(prefix):
                mode, body = m, text[len(prefix):].strip()
                break

        found = URL_RE.search(body)
        if not found:
            await self.tg.send(chat_id, "یک لینک بفرست تا دانلودش کنم. راهنما: /help", reply_to=mid)
            return
        url = found.group(0).rstrip(").,»\"'")

        if uid in self.busy:
            await self.tg.send(chat_id, "⏳ همین حالا یک دانلود برایت در جریان است. "
                                        "صبر کن تمام شود یا /cancel بزن.", reply_to=mid)
            return

        st = await self.tg.send(chat_id, "🔎 بررسی لینک…", reply_to=mid)
        job = Job(uid, url, mode, chat_id, mid, int((st or {}).get("message_id") or 0))
        self.busy[uid] = job
        job.task = asyncio.create_task(self._work(job))

    # ── متن‌ها ──
    def help_text(self) -> str:
        extra = "" if (ALLOWED_USERS or OWNER_ID) else \
            "\n\n⚠️ بدون ALLOWED_USERS/OWNER_ID اجرا شده — هر کسی می‌تواند استفاده کند."
        return (
            "🎬 ربات دانلود از لینک\n"
            "───────────────\n"
            "لینک را بفرست؛ اگر صفحهٔ سایت باشد خودم با yt-dlp بازش می‌کنم و "
            "اگر فایل مستقیم باشد با aria2c می‌گیرم.\n\n"
            "• /video لینک — دانلود تا ۷۲۰p (پیش‌فرض)\n"
            "• /low لینک — نسخهٔ کم‌حجم تا ۴۸۰p\n"
            "• /audio لینک — فقط صدا (mp3)\n"
            "• /status — وضعیت\n"
            "• /cancel — لغو دانلود خودت\n"
            "• /id — آیدی عددی تو\n\n"
            "📦 سقف ارسال فایل در تلگرام برای ربات‌ها ۵۰ مگابایت است. اگر فایل بزرگ‌تر "
            "شد، خودم نسخهٔ ۴۸۰p را می‌گیرم؛ اگر باز بزرگ بود از /audio استفاده کن." + extra
        )

    async def status_text(self) -> str:
        lines = ["📊 وضعیت",
                 "───────────────",
                 "• در جریان: %d  (سقف هم‌زمان: %d)" % (len(self.active), MAX_CONCURRENT),
                 "• پوشهٔ موقت: %.1fMB" % tree_size_mb(DOWNLOAD_DIR),
                 "• سقف ارسال: %dMB" % MAX_UPLOAD_MB,
                 "• زمان اجرا: %s" % hhmmss(time.time() - START_TS)]
        for j in list(self.active):
            lines.append("  – %s | %s" % (cut(j.progress.text(), 60), cut(j.url, 40)))
        return "\n".join(lines)

    # ── بدنهٔ کار ──
    async def _work(self, job: Job):
        pr = job.progress

        async def ticker():
            while True:
                await asyncio.sleep(4)
                if job.msg_id:
                    await self.tg.edit(job.chat_id, job.msg_id, pr.text())

        tick = asyncio.create_task(ticker())
        folder = DOWNLOAD_DIR / ("u%d_%d" % (job.uid, int(time.time())))
        try:
            async with self.sem:
                self.active.add(job)
                if job.msg_id:
                    await self.tg.edit(job.chat_id, job.msg_id,
                                       "⏳ در نوبت… (%d در جریان)" % len(self.active))
                direct = await is_direct_link(job.url)
                pr.note = "فایل مستقیم · aria2c" if direct else "سایت · yt-dlp"
                path, title, err = await self._download(job, folder, direct, pr)
                if job.cancelled:
                    return
                if not path:
                    await self._error(job, err)
                    return

                size = path.stat().st_size
                cap = MAX_UPLOAD_MB * 1048576
                if size > cap and job.mode == "video" and AUTO_FALLBACK:
                    await self._say(job, "📦 فایل %s شد — بیشتر از سقف %dMB تلگرام.\n"
                                         "نسخهٔ کم‌حجم (۴۸۰p) را می‌گیرم…" % (human(size), MAX_UPLOAD_MB))
                    shutil.rmtree(folder, ignore_errors=True)
                    folder.mkdir(parents=True, exist_ok=True)
                    pr.pct, pr.total, pr.done, pr.speed, pr.eta = 0, 0, 0, "", ""
                    pr.note = "تلاش دوم · ۴۸۰p"
                    path, title, err = await self._download(job, folder, False, pr, mode="low")
                    if not path:
                        await self._error(job, err)
                        return
                    size = path.stat().st_size

                if size > cap:
                    await self._say(job, "❌ حجم فایل %s شد و تلگرام برای ربات‌ها سقف %dMB دارد.\n"
                                         "راه‌ها: /audio (فقط صدا) یا /low (نسخهٔ کم‌حجم)."
                                    % (human(size), MAX_UPLOAD_MB))
                    return

                pr.stage, pr.done, pr.note = "upload", size, ""
                if job.msg_id:
                    await self.tg.edit(job.chat_id, job.msg_id, pr.text())
                await self.tg.action(job.chat_id, "upload_video")
                kind = kind_of(path)
                cap_txt = cut(title or path.stem, 100)
                caption = "%s  ·  %s" % (cap_txt, human(size))
                await self.tg.send_file(job.chat_id, path, kind, caption, reply_to=job.reply_to)
                if job.msg_id:
                    await self.tg.edit(job.chat_id, job.msg_id,
                                       "✅ ارسال شد  ·  %s  ·  %s" % (human(size), hhmmss(time.time() - pr.t0)))
        except asyncio.CancelledError:
            if job.msg_id:
                # تسکِ جدا: داخل مسیر لغو، هر await بلافاصله لغو می‌شود
                asyncio.create_task(self.tg.edit(job.chat_id, job.msg_id, "🛑 لغو شد."))
            raise
        except Exception as e:
            msg = str(e)
            if "too big" in msg.lower() or "413" in msg:
                await self._say(job, "❌ تلگرام فایل را بزرگ‌تر از حد مجاز دانست. "
                                     "با /low یا /audio دوباره امتحان کن.")
            else:
                await self._say(job, "❌ خطا: %s" % cut(msg, 300))
        finally:
            tick.cancel()
            # ⚠️ نکتهٔ مهم: انتظار برای یک تسکِ کنسل‌شده «CancelledError» می‌دهد
            #    که زیرکلاس Exception نیست؛ اگر مهار نشود، خطوط بعدی (آزادکردن
            #    کاربر و پاک‌کردن پوشهٔ موقت) هرگز اجرا نمی‌شوند.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tick
            self.active.discard(job)
            if self.busy.get(job.uid) is job:
                self.busy.pop(job.uid, None)
            if not KEEP_FILES:
                shutil.rmtree(folder, ignore_errors=True)

    async def _download(self, job: Job, folder: Path, direct: bool, pr: Progress,
                        mode: Optional[str] = None):
        mode = mode or job.mode
        if direct:
            p, t, err = await download_aria2(job.url, folder, pr)
            if p:
                return p, t, ""
            pr.note = "aria2c نگرفت — با yt-dlp تلاش می‌کنم"
            return await download_ytdlp(job.url, mode, folder, pr)
        p, t, err = await download_ytdlp(job.url, mode, folder, pr)
        if p:
            return p, t, ""
        if err in ("unsupported", "failed") and DOWNLOADER == "auto":
            pr.note = "yt-dlp نگرفت — با aria2c تلاش می‌کنم"
            return await download_aria2(job.url, folder, pr)
        return None, "", err

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
        await self._say(job, table.get(err, "❌ دانلود نشد: %s" % cut(err, 200)))


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
    print("✅ ربات @%s آماده است  (API: %s)" % ((tg.me or {}).get("username") or "?", API_BASE), flush=True)
    print("   پوشه: %s · سقف ارسال: %dMB · سقف دانلود: %dMB · هم‌زمان: %d"
          % (DOWNLOAD_DIR, MAX_UPLOAD_MB, MAX_DOWNLOAD_MB, MAX_CONCURRENT), flush=True)
    if not ALLOWED_USERS and not OWNER_ID:
        print("   ⚠️ بدون لیست دسترسی — ربات برای همه باز است.", flush=True)

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
