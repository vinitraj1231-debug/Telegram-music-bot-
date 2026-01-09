#!/usr/bin/env python3
"""
Advanced production-ready Telegram Music Assistant - main.py
Features:
- Robust pytgcalls compatibility and fallbacks
- Non-blocking yt-dlp extraction
- aiohttp health server for Render/Docker
- Detailed logging + graceful shutdown
- Defensive error handling
"""

import asyncio
import logging
import os
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, Optional, Any

# Third-party imports
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiohttp import web

# ---- Try robust pytgcalls imports ----
PTC_AVAILABLE = False
AudioPiped = None
HighQualityAudio = None
MediaStream = None
AudioQuality = None

try:
    # Preferred (modern) py-tgcalls packaging
    from pytgcalls import PyTgCalls
    from pytgcalls.types.input_stream import AudioPiped
    try:
        from pytgcalls.types.input_stream.quality import HighQualityAudio
    except Exception:
        HighQualityAudio = None
    PTC_AVAILABLE = True
except Exception:
    # Try alternative fallback imports (older versions)
    try:
        from pytgcalls import PyTgCalls  # maybe available under same name
        PTC_AVAILABLE = True
    except Exception:
        PyTgCalls = None
        PTC_AVAILABLE = False

# Some versions expose MediaStream / AudioQuality. Try to import if present.
try:
    from pytgcalls.types import MediaStream, AudioQuality  # type: ignore
except Exception:
    MediaStream = None
    AudioQuality = None

# ---- Logging configuration ----
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "")  # optional log file
DEBUG_MODE = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")

handlers = [logging.StreamHandler(sys.stdout)]
if LOG_FILE:
    handlers.append(logging.FileHandler(LOG_FILE))

logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=handlers
)
logger = logging.getLogger("music-assistant")

# ---- Environment helpers and validation ----
def require_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        logger.critical("Missing required environment variable: %s", name)
        raise SystemExit(1)
    return v

try:
    API_ID = int(require_env("API_ID"))
except ValueError:
    logger.critical("API_ID must be an integer")
    raise SystemExit(1)

API_HASH = require_env("API_HASH")
BOT_TOKEN = require_env("BOT_TOKEN")
SESSION_STRING = require_env("SESSION_STRING")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

HEALTH_PORT = int(os.getenv("PORT", "8080"))

# ---- Pyrogram clients ----
bot = Client(
    "music_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=8
)

assistant = Client(
    "music_assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    workers=4
)

# ---- PyTgCalls instance (if available) ----
if PTC_AVAILABLE:
    try:
        calls = PyTgCalls(assistant)
        logger.info("PyTgCalls available: using assistant client for voice calls")
    except Exception as e:
        calls = None
        logger.warning("Failed to initialize PyTgCalls: %s", e)
else:
    calls = None
    logger.warning("PyTgCalls not available in this environment; voice features disabled")

# ---- Queue / state manager ----
class QueueManager:
    def __init__(self):
        self.queues: Dict[int, list] = defaultdict(list)
        self.current: Dict[int, Optional[dict]] = {}
        self.paused: Dict[int, bool] = defaultdict(bool)
        self.last_command: Dict[int, datetime] = {}

    def add(self, chat_id: int, track: dict):
        self.queues[chat_id].append(track)

    def get_queue(self, chat_id: int) -> list:
        return list(self.queues.get(chat_id, []))

    def pop(self, chat_id: int) -> Optional[dict]:
        return self.queues[chat_id].pop(0) if self.queues.get(chat_id) else None

    def clear(self, chat_id: int):
        self.queues[chat_id] = []
        self.current[chat_id] = None
        self.paused[chat_id] = False

    def set_current(self, chat_id: int, track: dict):
        self.current[chat_id] = track

    def get_current(self, chat_id: int) -> Optional[dict]:
        return self.current.get(chat_id)

    def check_cooldown(self, chat_id: int, cooldown: int = 2) -> bool:
        last = self.last_command.get(chat_id)
        if last and (datetime.now() - last).total_seconds() < cooldown:
            return False
        self.last_command[chat_id] = datetime.now()
        return True

queue_manager = QueueManager()

# ---- yt-dlp extraction (non-blocking) ----
YDL_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "nocheckcertificate": True,
    "noplaylist": True,
    "socket_timeout": 15,
    "retries": 2,
    "fragment_retries": 2,
}

async def extract_info(query: str) -> Optional[dict]:
    loop = asyncio.get_event_loop()
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))
            if not info:
                return None
            # handle playlist result and search results
            if "entries" in info and info["entries"]:
                # take first valid entry
                entry = next((e for e in info["entries"] if e), info["entries"][0])
                info = entry or info
            # get a usable stream URL
            stream_url = None
            if "url" in info and info["url"]:
                stream_url = info["url"]
            elif "formats" in info and info["formats"]:
                audio_formats = [f for f in info["formats"] if f.get("acodec") != "none"]
                if audio_formats:
                    stream_url = audio_formats[-1].get("url")
                else:
                    stream_url = info["formats"][-1].get("url")
            if not stream_url:
                return None
            return {
                "title": info.get("title", "Unknown"),
                "duration": info.get("duration", 0),
                "url": stream_url,
                "thumbnail": info.get("thumbnail", ""),
                "uploader": info.get("uploader", "Unknown"),
                "webpage_url": info.get("webpage_url", query),
            }
    except Exception as e:
        logger.exception("yt-dlp extraction failed for %s: %s", query, e)
        return None

# ---- Playback compatibility helpers ----
async def _play_audiopiped(chat_id: int, track: dict) -> bool:
    """Try modern AudioPiped path (preferred)."""
    if calls is None or AudioPiped is None:
        return False
    try:
        if HighQualityAudio is not None:
            stream = AudioPiped(track["url"], HighQualityAudio())
        else:
            stream = AudioPiped(track["url"])
        # try play/change_stream API
        if hasattr(calls, "play"):
            await calls.play(chat_id, stream)
            return True
        if hasattr(calls, "change_stream"):
            await calls.change_stream(chat_id, stream)
            return True
        # try join_group_call fallback
        if hasattr(calls, "join_group_call"):
            await calls.join_group_call(chat_id, stream)
            return True
    except Exception as e:
        logger.debug("AudioPiped play failed: %s", e)
    return False

async def _play_mediastream(chat_id: int, track: dict) -> bool:
    """Try MediaStream/AudioQuality path if available."""
    if calls is None or MediaStream is None or AudioQuality is None:
        return False
    try:
        stream = MediaStream(track["url"], audio_parameters=AudioQuality.HIGH)
        # modern API: calls.play(chat_id, stream)
        if hasattr(calls, "play"):
            await calls.play(chat_id, stream)
            return True
        if hasattr(calls, "change_stream"):
            await calls.change_stream(chat_id, stream)
            return True
    except Exception as e:
        logger.debug("MediaStream path failed: %s", e)
    return False

async def _generic_play(chat_id: int, track: dict) -> bool:
    """Generic best-effort fallback: try several method signatures."""
    if calls is None:
        return False
    for method_name in ("play", "change_stream", "join_group_call", "start"):
        method = getattr(calls, method_name, None)
        if callable(method):
            try:
                # attempt signature with (chat_id, url)
                await method(chat_id, track["url"])
                return True
            except TypeError:
                try:
                    await method(chat_id)
                    return True
                except Exception:
                    continue
            except Exception:
                continue
    return False

async def join_and_play(chat_id: int, track: dict) -> bool:
    """Top-level play helper that tries all available play methods."""
    # Preferred: AudioPiped
    if await _play_audiopiped(chat_id, track):
        logger.debug("Playback started via AudioPiped for chat %s", chat_id)
        return True
    # Try MediaStream path
    if await _play_mediastream(chat_id, track):
        logger.debug("Playback started via MediaStream for chat %s", chat_id)
        return True
    # Generic fallback
    if await _generic_play(chat_id, track):
        logger.debug("Playback started via generic method for chat %s", chat_id)
        return True
    logger.warning("Unable to start playback in chat %s — no supported API path", chat_id)
    return False

# ---- Playback control ----
async def play_track(chat_id: int, track: dict):
    """High-level: join and start playback; set current state or skip on failure."""
    try:
        ok = await join_and_play(chat_id, track)
        if ok:
            queue_manager.set_current(chat_id, track)
            queue_manager.paused[chat_id] = False
            logger.info("Now playing in %s: %s", chat_id, track.get("title"))
        else:
            logger.info("Failed to play track in %s; attempting skip", chat_id)
            await skip_track(chat_id)
    except Exception as e:
        logger.exception("play_track error for %s: %s", chat_id, e)
        await skip_track(chat_id)

async def skip_track(chat_id: int):
    """Pop next track and play; otherwise leave call and clear state."""
    nxt = queue_manager.pop(chat_id)
    if nxt:
        await play_track(chat_id, nxt)
    else:
        try:
            if calls is not None:
                # try best-known leave method names
                for leave_attr in ("leave_call", "leave_group_call", "stop"):
                    method = getattr(calls, leave_attr, None)
                    if callable(method):
                        try:
                            await method(chat_id)
                            break
                        except Exception:
                            continue
            queue_manager.clear(chat_id)
            logger.info("Queue empty for chat %s; cleared state and left call (if supported)", chat_id)
        except Exception as e:
            logger.exception("Error leaving call for chat %s: %s", chat_id, e)

# ---- Register stream-end hook (best-effort) ----
if calls is not None:
    try:
        @calls.on_stream_end()
        async def _on_stream_end(_, chat_id):
            logger.debug("Stream ended event received for chat %s", chat_id)
            await skip_track(chat_id)
    except Exception:
        try:
            @calls.on_stream_end()
            async def _on_stream_end_alt(_, update):
                cid = getattr(update, "chat_id", None) or getattr(update, "group_id", None)
                if cid:
                    logger.debug("Alt stream-end event for chat %s", cid)
                    await skip_track(cid)
        except Exception:
            logger.debug("Could not register on_stream_end handler — stream-end auto-skip may not work")

# ---- UI helpers ----
def get_welcome_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{bot_username}?startgroup=true"),
             InlineKeyboardButton("❓ Help", callback_data="help")],
            [InlineKeyboardButton("📢 Support Channel", url="https://t.me/your_channel"),
             InlineKeyboardButton("💬 Support Group", url="https://t.me/your_support")],
            [InlineKeyboardButton("👤 Owner", url="https://t.me/your_owner")]
        ]
    )

def get_playback_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    paused = queue_manager.paused.get(chat_id, False)
    pause_text = "▶️ Resume" if paused else "⏸ Pause"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(pause_text, callback_data=f"pause_{chat_id}"),
             InlineKeyboardButton("⏭ Skip", callback_data=f"skip_{chat_id}")],
            [InlineKeyboardButton("📋 Queue", callback_data=f"queue_{chat_id}"),
             InlineKeyboardButton("⏹ Stop", callback_data=f"stop_{chat_id}")]
        ]
    )

# ---- Command handlers ----
@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    me = await client.get_me()
    welcome = (
        "🎵 Welcome to Music Assistant\n\n"
        "• Add me to a group and start a voice chat\n"
        "• Use /play <query or url> to stream audio\n"
    )
    await message.reply_photo(
        photo="https://telegra.ph/file/29f784eb49d230ab62e9e.png",
        caption=welcome,
        reply_markup=get_welcome_keyboard(me.username)
    )

@bot.on_message(filters.command("help"))
async def help_handler(client: Client, message: Message):
    help_text = (
        "Commands:\n"
        "/play <query/url> - Play a track\n"
        "/pause - Pause playback\n"
        "/resume - Resume playback\n"
        "/skip - Skip current track\n"
        "/stop - Stop and clear queue\n"
        "/queue - Show queue\n"
        "/leave - Leave voice chat\n"
        "/ping - Check latency\n"
    )
    await message.reply_text(help_text)

@bot.on_message(filters.command("play") & filters.group)
async def play_handler(client: Client, message: Message):
    chat_id = message.chat.id
    if not queue_manager.check_cooldown(chat_id):
        await message.reply_text("⏳ Please wait before sending another command.")
        return
    if len(message.command) < 2:
        await message.reply_text("❌ Usage: /play <song name or URL>")
        return
    query = message.text.split(None, 1)[1]
    status_msg = await message.reply_text(f"🔍 Searching: `{query}`...")
    start_ts = time.time()
    try:
        track_info = await extract_info(query)
        if not track_info:
            await status_msg.edit_text("❌ Could not find the track. Try another query.")
            return

        current = queue_manager.get_current(chat_id)
        # if something is already playing, add to queue; else start playback
        if current:
            queue_manager.add(chat_id, track_info)
            pos = len(queue_manager.get_queue(chat_id))
            await status_msg.edit_text(
                f"📋 Added to Queue (#{pos})\n\n🎵 {track_info['title']}\n⏱ {track_info['duration']//60}:{track_info['duration']%60:02d}",
                reply_markup=get_playback_keyboard(chat_id)
            )
        else:
            await status_msg.edit_text("🔌 Joining voice chat and starting playback...")
            await play_track(chat_id, track_info)
            elapsed = time.time() - start_ts
            await status_msg.edit_text(
                f"🎵 Now Playing\n\n**{track_info['title']}**\n⏱ {track_info['duration']//60}:{track_info['duration']%60:02d}\n⚡ Started in {elapsed:.2f}s",
                reply_markup=get_playback_keyboard(chat_id)
            )
    except Exception as e:
        logger.exception("Error in play_handler: %s", e)
        await status_msg.edit_text(f"❌ Error: {e}")

@bot.on_message(filters.command("pause") & filters.group)
async def pause_handler(client: Client, message: Message):
    chat_id = message.chat.id
    try:
        if calls is not None:
            if hasattr(calls, "pause_stream"):
                await calls.pause_stream(chat_id)
            elif hasattr(calls, "pause"):
                await calls.pause(chat_id)
        queue_manager.paused[chat_id] = True
        await message.reply_text("⏸ Paused playback")
    except Exception as e:
        logger.exception("Pause failed: %s", e)
        await message.reply_text(f"❌ Error: {e}")

@bot.on_message(filters.command("resume") & filters.group)
async def resume_handler(client: Client, message: Message):
    chat_id = message.chat.id
    try:
        if calls is not None:
            if hasattr(calls, "resume_stream"):
                await calls.resume_stream(chat_id)
            elif hasattr(calls, "resume"):
                await calls.resume(chat_id)
        queue_manager.paused[chat_id] = False
        await message.reply_text("▶️ Resumed playback")
    except Exception as e:
        logger.exception("Resume failed: %s", e)
        await message.reply_text(f"❌ Error: {e}")

@bot.on_message(filters.command("skip") & filters.group)
async def skip_cmd_handler(client: Client, message: Message):
    chat_id = message.chat.id
    await message.reply_text("⏭ Skipping...")
    await skip_track(chat_id)

@bot.on_message(filters.command("stop") & filters.group)
async def stop_handler(client: Client, message: Message):
    chat_id = message.chat.id
    try:
        if calls is not None:
            for leave_meth in ("leave_call", "leave_group_call", "stop"):
                method = getattr(calls, leave_meth, None)
                if callable(method):
                    try:
                        await method(chat_id)
                        break
                    except Exception:
                        continue
        queue_manager.clear(chat_id)
        await message.reply_text("⏹ Stopped and cleared queue")
    except Exception as e:
        logger.exception("Stop failed: %s", e)
        await message.reply_text(f"❌ Error: {e}")

@bot.on_message(filters.command("queue") & filters.group)
async def queue_handler(client: Client, message: Message):
    chat_id = message.chat.id
    current = queue_manager.get_current(chat_id)
    q = queue_manager.get_queue(chat_id)
    if not current and not q:
        return await message.reply_text("📋 Queue is empty")
    text = "📋 **Current Queue**\n\n"
    if current:
        text += f"Now playing: {current.get('title')}\n\n"
    if q:
        text += "Up next:\n"
        for i, t in enumerate(q[:10], 1):
            text += f"{i}. {t.get('title')}\n"
        if len(q) > 10:
            text += f"... and {len(q)-10} more"
    await message.reply_text(text)

@bot.on_message(filters.command("leave") & filters.group)
async def leave_handler(client: Client, message: Message):
    chat_id = message.chat.id
    try:
        if calls is not None:
            for leave_meth in ("leave_call", "leave_group_call", "stop"):
                method = getattr(calls, leave_meth, None)
                if callable(method):
                    try:
                        await method(chat_id)
                        break
                    except Exception:
                        continue
        queue_manager.clear(chat_id)
        await message.reply_text("👋 Left voice chat")
    except Exception as e:
        logger.exception("Leave failed: %s", e)
        await message.reply_text(f"❌ Error: {e}")

@bot.on_message(filters.command("ping"))
async def ping_handler(client: Client, message: Message):
    t0 = time.time()
    m = await message.reply_text("🏓 Pinging...")
    t1 = time.time()
    await m.edit_text(f"🏓 Pong!\nLatency: {(t1-t0)*1000:.2f}ms")

@bot.on_callback_query()
async def callback_handler(client: Client, callback: CallbackQuery):
    data = (callback.data or "")
    await callback.answer()
    if data == "help":
        await callback.message.edit_caption("Quick help: start voice chat, use /play <query>", reply_markup=get_welcome_keyboard((await client.get_me()).username))
        return
    parts = data.split("_")
    action = parts[0] if parts else ""
    chat_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else callback.message.chat.id
    if action == "pause":
        if queue_manager.paused.get(chat_id):
            await resume_handler(client, callback.message)
        else:
            await pause_handler(client, callback.message)
        await callback.message.edit_reply_markup(get_playback_keyboard(chat_id))
    elif action == "skip":
        await skip_track(chat_id)
        await callback.message.edit_reply_markup(get_playback_keyboard(chat_id))
    elif action == "stop":
        await stop_handler(client, callback.message)
        await callback.message.edit_reply_markup(get_playback_keyboard(chat_id))
    elif action == "queue":
        await queue_handler(client, callback.message)

# ---- Health server (aiohttp) ----
HEALTH_STARTED_AT = time.time()

async def root_handler(request):
    uptime = int(time.time() - HEALTH_STARTED_AT)
    return web.Response(text=f"Telegram Music Bot - Running ✅\nUptime: {uptime}s", content_type="text/plain")

async def health_handler(request):
    return web.json_response({
        "status": "healthy",
        "uptime_s": f"{time.time() - HEALTH_STARTED_AT:.2f}",
        "ts": time.time()
    })

async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()
    logger.info("Health server started on port %s", HEALTH_PORT)
    return runner

# ---- Graceful startup/shutdown ----
stop_event = asyncio.Event()

def _shutdown_signal_handler():
    logger.info("Shutdown signal received.")
    stop_event.set()

async def main():
    # register signals
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _shutdown_signal_handler)
        except Exception:
            # platforms that don't support add_signal_handler (Windows in some contexts)
            pass

    health_runner = None
    try:
        # Health server
        try:
            health_runner = await start_health_server()
        except Exception as e:
            logger.exception("Health server failed to start: %s", e)

        # Start pyrogram clients
        logger.info("Starting bot client...")
        await bot.start()
        logger.info("Bot client started.")

        logger.info("Starting assistant client...")
        await assistant.start()
        logger.info("Assistant client started.")

        # Optional validation: get_me for both
        try:
            b = await bot.get_me()
            a = await assistant.get_me()
            logger.info("Bot validated: %s (@%s)", getattr(b, "first_name", None), getattr(b, "username", None))
            logger.info("Assistant validated: %s (@%s)", getattr(a, "first_name", None), getattr(a, "username", None))
        except Exception as e:
            logger.exception("Validation get_me failed: %s", e)

        # Start pytgcalls if available
        if calls is not None:
            try:
                await calls.start()
                logger.info("PyTgCalls started successfully.")
            except Exception as e:
                logger.exception("PyTgCalls failed to start (will continue without voice features): %s", e)

        logger.info("Bot initialization complete — entering run loop.")
        # Wait for shutdown signal
        await stop_event.wait()
    except Exception as exc:
        logger.exception("Fatal error in main: %s", exc)
    finally:
        logger.info("Shutting down: stopping clients and health server.")
        try:
            await bot.stop()
        except Exception:
            logger.exception("Error stopping bot")
        try:
            await assistant.stop()
        except Exception:
            logger.exception("Error stopping assistant")
        if calls is not None:
            try:
                if hasattr(calls, "stop"):
                    await calls.stop()
            except Exception:
                logger.exception("Error stopping pytgcalls")
        if health_runner is not None:
            try:
                await health_runner.cleanup()
            except Exception:
                logger.exception("Error cleaning up health server")
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.exception("Unhandled exception (top-level): %s", e)
        sys.exit(1)
