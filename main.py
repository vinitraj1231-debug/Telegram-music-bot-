"""
Telegram Music Assistant - compatibility hardened production file.
- Robust pytgcalls import and API detection
- Non-blocking yt-dlp extraction
- aiohttp health server (background)
- Clear logging and defensive error handling
"""

import asyncio
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, Optional, Any

import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

# Try to import pytgcalls variants / helpers in a safe way
try:
    from pytgcalls import PyTgCalls
    # preferred input stream classes for current py-tgcalls
    from pytgcalls.types.input_stream import AudioPiped
    from pytgcalls.types.input_stream.quality import HighQualityAudio
    PTC_INTEGRATION = True
except Exception:
    # Fallback: older/alternate packaging names or missing optional classes
    PyTgCalls = None
    AudioPiped = None
    HighQualityAudio = None
    PTC_INTEGRATION = False

# For compatibility with projects that still use MediaStream/AudioQuality:
try:
    from pytgcalls.types import StreamType  # not required, but available on some versions
except Exception:
    StreamType = None

# aiohttp health server (optional but recommended for Render)
from aiohttp import web

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("music-assistant")

# ---------------- Environment & validation ----------------
def get_env(name: str, required: bool = True, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(name, default)
    if required and (val is None or val == ""):
        logger.error("Missing required env variable: %s", name)
        if required:
            raise SystemExit(1)
    return val

API_ID = get_env("API_ID")
API_HASH = get_env("API_HASH")
BOT_TOKEN = get_env("BOT_TOKEN")
SESSION_STRING = get_env("SESSION_STRING")
OWNER_ID = os.getenv("OWNER_ID", "0")

# Ensure API_ID is int
try:
    API_ID = int(API_ID)
except Exception:
    logger.error("API_ID must be an integer.")
    raise SystemExit(1)

try:
    OWNER_ID = int(OWNER_ID)
except Exception:
    OWNER_ID = 0

# ---------------- Pyrogram clients ----------------
bot = Client(
    "music_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=8
)

assistant = Client(
    "assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    workers=4
)

# ---------------- pytgcalls instance (safe init) ----------------
if PyTgCalls is None:
    logger.warning("pytgcalls not importable or partially available. Some voice features may fail.")
    calls = None
else:
    calls = PyTgCalls(assistant)

# ---------------- Simple queue/state manager ----------------
class QueueManager:
    def __init__(self):
        self.queues: Dict[int, list] = defaultdict(list)
        self.current: Dict[int, Optional[dict]] = {}
        self.paused: Dict[int, bool] = defaultdict(bool)
        self.last_command: Dict[int, datetime] = {}

    def add(self, chat_id: int, track: dict):
        self.queues[chat_id].append(track)

    def get_queue(self, chat_id: int) -> list:
        return self.queues.get(chat_id, [])

    def pop(self, chat_id: int) -> Optional[dict]:
        return self.queues[chat_id].pop(0) if self.queues[chat_id] else None

    def clear(self, chat_id: int):
        self.queues[chat_id] = []
        self.current[chat_id] = None

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

# ---------------- yt-dlp options and extractor ----------------
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
            if "entries" in info:
                # playlist -> take first entry
                info = info["entries"][0] or info
            # find a direct stream URL
            stream_url = None
            if "url" in info and info["url"]:
                stream_url = info["url"]
            elif "formats" in info and info["formats"]:
                # prefer highest-quality audio with codec != none
                audio_formats = [f for f in info["formats"] if f.get("acodec") != "none"]
                if audio_formats:
                    # formats often sorted low->high; take last
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

# ---------------- Compatibility helpers for playing streams ----------------
async def _play_via_audiopiped(chat_id: int, track: dict) -> bool:
    """
    Preferred method: AudioPiped (py-tgcalls modern).
    """
    if calls is None or AudioPiped is None:
        return False
    try:
        # instantiate AudioPiped with URL and quality wrapper if available
        if HighQualityAudio is not None:
            stream = AudioPiped(track["url"], HighQualityAudio())
        else:
            stream = AudioPiped(track["url"])
        # newer py-tgcalls exposes play or change_stream; try both
        if hasattr(calls, "play"):
            await calls.play(chat_id, stream)
        elif hasattr(calls, "change_stream"):
            await calls.change_stream(chat_id, stream)
        else:
            # some older APIs use start/join semantics - try join/start patterns
            if hasattr(calls, "start") and hasattr(calls, "join_group_call"):
                # start should already be called, but attempt join
                await calls.join_group_call(chat_id, stream)
            else:
                raise RuntimeError("No supported play API on calls instance")
        return True
    except Exception as e:
        logger.exception("AudioPiped play failed: %s", e)
        return False

async def _play_via_generic(chat_id: int, track: dict) -> bool:
    """
    Generic fallback: attempt to call a generic play or join API with URL directly.
    This is best-effort to support older or forked APIs.
    """
    if calls is None:
        return False
    try:
        # try convenience methods in order
        for method_name in ("play", "change_stream", "join_group_call", "start"):
            method = getattr(calls, method_name, None)
            if callable(method):
                try:
                    # some methods expect different args; try:

                    # 1) play(chat_id, stream-like) handled earlier
                    # 2) start(chat_id) may require no stream - skip here
                    await method(chat_id) if method_name == "start" else await method(chat_id, track["url"])
                    return True
                except TypeError:
                    # try with only chat_id if method signature differs
                    try:
                        await method(chat_id)
                        return True
                    except Exception:
                        continue
                except Exception:
                    continue
        return False
    except Exception as e:
        logger.exception("Generic play fallback failed: %s", e)
        return False

async def join_and_play(chat_id: int, track: dict) -> bool:
    """
    Top-level helper to join a voice chat and start playing.
    - tries modern AudioPiped path first, then generic fallback.
    """
    # Attempt preferred modern path
    ok = await _play_via_audiopiped(chat_id, track)
    if ok:
        logger.info("Played via AudioPiped in chat %s", chat_id)
        return True
    # fallback generic approach
    ok2 = await _play_via_generic(chat_id, track)
    if ok2:
        logger.info("Played via generic method in chat %s", chat_id)
        return True
    # unable to play
    logger.warning("Unable to start playback in chat %s (no supported API)", chat_id)
    return False

# ---------------- Playback control (play/skip/stop) ----------------
async def play_track(chat_id: int, track: dict):
    try:
        started = await join_and_play(chat_id, track)
        if started:
            queue_manager.set_current(chat_id, track)
            queue_manager.paused[chat_id] = False
            logger.info("Now playing in %s: %s", chat_id, track.get("title"))
        else:
            # if couldn't play, try skip to next
            await skip_track(chat_id)
    except Exception as e:
        logger.exception("play_track error: %s", e)
        await skip_track(chat_id)

async def skip_track(chat_id: int):
    nxt = queue_manager.pop(chat_id)
    if nxt:
        await play_track(chat_id, nxt)
    else:
        # Try to leave call if supported
        try:
            if calls is not None and hasattr(calls, "leave_call"):
                await calls.leave_call(chat_id)
            queue_manager.clear(chat_id)
            logger.info("Queue ended and left call for chat %s", chat_id)
        except Exception as e:
            logger.exception("Error while leaving call for chat %s: %s", chat_id, e)

# ---------------- Stream ended hook (best-effort) ----------------
# Modern py-tgcalls exposes event decorators, but signatures vary.
if calls is not None:
    try:
        # preferred decorator name
        @calls.on_stream_end()
        async def _on_stream_end(_, chat_id):
            logger.info("Stream ended in %s -> auto-skip", chat_id)
            await skip_track(chat_id)
    except Exception:
        # second attempt with alternate signature
        try:
            @calls.on_stream_end()
            async def _on_stream_end_alt(_, update):
                # best-effort to extract chat id
                cid = getattr(update, "chat_id", None)
                if cid is None:
                    cid = getattr(update, "group_id", None)
                if cid:
                    logger.info("Stream ended (alt) in %s -> auto-skip", cid)
                    await skip_track(cid)
        except Exception:
            logger.debug("Could not register on_stream_end handler; stream-end auto-skip may not work.")

# ---------------- Keyboards & UI ----------------
def get_welcome_keyboard(bot_username: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Add to Group", url=f"https://t.me/{bot_username}?startgroup=true"),
                InlineKeyboardButton("Help", callback_data="help")
            ],
            [
                InlineKeyboardButton("Support Channel", url="https://t.me/your_channel"),
                InlineKeyboardButton("Support Group", url="https://t.me/your_support")
            ],
            [InlineKeyboardButton("Owner", url="https://t.me/your_owner")]
        ]
    )

def get_playback_keyboard(chat_id: int):
    paused = queue_manager.paused.get(chat_id, False)
    pause_text = "Resume" if paused else "Pause"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(pause_text, callback_data=f"pause_{chat_id}"),
             InlineKeyboardButton("Skip", callback_data=f"skip_{chat_id}")],
            [InlineKeyboardButton("Queue", callback_data=f"queue_{chat_id}"),
             InlineKeyboardButton("Stop", callback_data=f"stop_{chat_id}")]
        ]
    )

# ---------------- Command handlers ----------------
@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    me = await client.get_me()
    text = (
        "Welcome to Music Assistant\n"
        "• Add to your group and start a voice chat\n"
        "• Use /play <query> to stream audio"
    )
    await message.reply_photo(photo="https://telegra.ph/file/29f784eb49d230ab62e9e.png",
                              caption=text,
                              reply_markup=get_welcome_keyboard(me.username))

@bot.on_message(filters.command("help"))
async def help_handler(client: Client, message: Message):
    help_text = (
        "Commands:\n"
        "/play <query> - Play track\n"
        "/skip - Skip current\n"
        "/pause - Pause\n"
        "/resume - Resume\n"
        "/stop - Stop and clear\n"
        "/queue - Show queue\n"
    )
    await message.reply_text(help_text)

@bot.on_message(filters.command("play") & filters.group)
async def play_handler(client: Client, message: Message):
    chat_id = message.chat.id
    if not queue_manager.check_cooldown(chat_id):
        await message.reply_text("Please wait before sending another command.")
        return
    if len(message.command) < 2:
        await message.reply_text("Usage: /play <song name or URL>")
        return
    query = message.text.split(None, 1)[1]
    status = await message.reply_text(f"Searching: {query} ...")
    start = time.time()
    track = await extract_info(query)
    if not track:
        await status.edit_text("Could not find/prepare the track.")
        return
    current = queue_manager.get_current(chat_id)
    # If currently playing and there's a call, queue; otherwise play immediately
    if current:
        queue_manager.add(chat_id, track)
        pos = len(queue_manager.get_queue(chat_id))
        await status.edit_text(f"Added to queue #{pos}: {track['title']}", reply_markup=get_playback_keyboard(chat_id))
    else:
        await status.edit_text("Joining voice chat and starting playback...")
        await play_track(chat_id, track)
        elapsed = time.time() - start
        await status.edit_text(f"Now playing: {track['title']} (started in {elapsed:.2f}s)", reply_markup=get_playback_keyboard(chat_id))

@bot.on_message(filters.command("pause") & filters.group)
async def pause_handler(client: Client, message: Message):
    chat_id = message.chat.id
    try:
        # Many pytgcalls variants use pause_stream / pause
        if calls is not None:
            if hasattr(calls, "pause_stream"):
                await calls.pause_stream(chat_id)
            elif hasattr(calls, "pause"):
                await calls.pause(chat_id)
        queue_manager.paused[chat_id] = True
        await message.reply_text("Paused playback")
    except Exception as e:
        logger.exception("Pause failed: %s", e)
        await message.reply_text(f"Error pausing: {e}")

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
        await message.reply_text("Resumed playback")
    except Exception as e:
        logger.exception("Resume failed: %s", e)
        await message.reply_text(f"Error resuming: {e}")

@bot.on_message(filters.command("skip") & filters.group)
async def skip_handler(client: Client, message: Message):
    chat_id = message.chat.id
    await message.reply_text("Skipping...")
    await skip_track(chat_id)

@bot.on_message(filters.command("stop") & filters.group)
async def stop_handler(client: Client, message: Message):
    chat_id = message.chat.id
    try:
        if calls is not None and hasattr(calls, "leave_call"):
            await calls.leave_call(chat_id)
        queue_manager.clear(chat_id)
        await message.reply_text("Stopped and cleared queue")
    except Exception as e:
        logger.exception("Stop failed: %s", e)
        await message.reply_text(f"Error stopping: {e}")

@bot.on_message(filters.command("queue") & filters.group)
async def queue_handler(client: Client, message: Message):
    chat_id = message.chat.id
    curr = queue_manager.get_current(chat_id)
    q = queue_manager.get_queue(chat_id)
    if not curr and not q:
        return await message.reply_text("Queue is empty")
    text = ""
    if curr:
        text += f"Now playing: {curr.get('title')}\n\n"
    if q:
        text += "Up next:\n"
        for i, t in enumerate(q[:10], 1):
            text += f"{i}. {t.get('title')}\n"
        if len(q) > 10:
            text += f"... and {len(q)-10} more"
    await message.reply_text(text)

@bot.on_callback_query()
async def callback_handler(client: Client, callback: CallbackQuery):
    data = callback.data or ""
    await callback.answer()  # acknowledge
    if data == "help":
        await callback.message.edit_caption("Quick help: use /play in a chat with a voice chat started.",
                                            reply_markup=get_welcome_keyboard((await client.get_me()).username))
        return
    parts = data.split("_")
    action = parts[0] if parts else ""
    chat_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else callback.message.chat.id
    if action == "pause":
        # toggle
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

# ---------------- Health server (aiohttp) ----------------
HEALTH_START = time.time()

async def root_handler(request):
    uptime = time.time() - HEALTH_START
    return web.Response(text=f"Telegram Music Bot - Running\nUptime: {uptime:.0f}s", content_type="text/plain")

async def health_json_handler(request):
    return web.json_response({"status": "healthy", "uptime": f"{time.time() - HEALTH_START:.2f}s", "ts": time.time()})

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/health", health_json_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health server started on port %s", port)
    return runner

# ---------------- Startup and shutdown ----------------
async def main():
    logger.info("Starting bot and assistant...")
    # start health server in background
    health_runner = None
    try:
        health_runner = await start_health_server()
    except Exception as e:
        logger.exception("Health server failed to start: %s", e)

    try:
        await bot.start()
        await assistant.start()
        if calls is not None:
            try:
                await calls.start()
            except Exception:
                # some versions don't require or expose start; ignore if not needed
                logger.debug("calls.start() failed or not required - continuing")
        logger.info("Bot and assistant started successfully.")
        # Keep running until cancelled
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down due to interrupt.")
    except Exception as e:
        logger.exception("Fatal error during main runtime: %s", e)
    finally:
        try:
            await bot.stop()
            await assistant.stop()
            if calls is not None and hasattr(calls, "stop"):
                try:
                    await calls.stop()
                except Exception:
                    pass
            if health_runner is not None:
                await health_runner.cleanup()
        except Exception as e:
            logger.exception("Error during shutdown: %s", e)
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.exception("Unhandled exception in __main__: %s", e)
        sys.exit(1)
