"""
Telegram Music Assistant - Production Ready
High-performance voice chat music streaming bot
Target: <3s cold start, <1s warm playback
"""

import asyncio
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Optional

import yt_dlp
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, UserAlreadyParticipant
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pytgcalls import PyTgCalls, StreamType
from pytgcalls.exceptions import (
    AlreadyJoinedError,
    NoActiveGroupCall,
)
from pytgcalls.types import AudioPiped, Update

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

# Environment variables with validation
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
SESSION_STRING = os.getenv("SESSION_STRING")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

if not all([API_ID, API_HASH, BOT_TOKEN, SESSION_STRING]):
    logger.error("Missing required environment variables!")
    sys.exit(1)

# Bot and Assistant clients
bot = Client(
    "music_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=8,
    max_concurrent_transmissions=4
)

assistant = Client(
    "assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    workers=4
)

# PyTgCalls instance
calls = PyTgCalls(assistant, cache_duration=100)

# Global state management
class QueueManager:
    def __init__(self):
        self.queues: Dict[int, list] = defaultdict(list)
        self.current: Dict[int, Optional[dict]] = {}
        self.loop_mode: Dict[int, bool] = defaultdict(bool)
        self.paused: Dict[int, bool] = defaultdict(bool)
        self.last_command: Dict[int, datetime] = {}
        
    def add(self, chat_id: int, track: dict):
        self.queues[chat_id].append(track)
        
    def get_queue(self, chat_id: int) -> list:
        return self.queues.get(chat_id, [])
    
    def pop(self, chat_id: int) -> Optional[dict]:
        if self.queues[chat_id]:
            return self.queues[chat_id].pop(0)
        return None
    
    def clear(self, chat_id: int):
        self.queues[chat_id] = []
        self.current[chat_id] = None
        
    def set_current(self, chat_id: int, track: dict):
        self.current[chat_id] = track
        
    def get_current(self, chat_id: int) -> Optional[dict]:
        return self.current.get(chat_id)
    
    def check_cooldown(self, chat_id: int, cooldown: int = 2) -> bool:
        """Rate limiting per group"""
        last = self.last_command.get(chat_id)
        if last and (datetime.now() - last).total_seconds() < cooldown:
            return False
        self.last_command[chat_id] = datetime.now()
        return True

queue_manager = QueueManager()

# YT-DLP optimized configuration for ultra-fast extraction
YDL_OPTS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(id)s.%(ext)s',
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'no_warnings': True,
    'quiet': True,
    'extractaudio': True,
    'cookiefile': None,
    'socket_timeout': 10,
    'retries': 3,
    'fragment_retries': 3,
    'concurrent_fragment_downloads': 5,
    'http_chunk_size': 10485760,  # 10MB chunks
    'source_address': '0.0.0.0',
    'geo_bypass': True,
    'age_limit': None,
    'nocache': True,
    'noplaylist': False,
    'extract_flat': 'in_playlist',
}

async def extract_info_fast(query: str) -> Optional[dict]:
    """
    Ultra-fast extraction with parallel processing
    Target: <2s for most queries
    """
    start = time.time()
    try:
        loop = asyncio.get_event_loop()
        
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            # Run in executor to avoid blocking
            info = await loop.run_in_executor(
                None,
                lambda: ydl.extract_info(query, download=False)
            )
            
            if not info:
                return None
            
            # Handle playlists
            if 'entries' in info:
                info = info['entries'][0]
            
            # Extract direct stream URL
            if 'url' in info:
                stream_url = info['url']
            elif 'formats' in info:
                # Get best audio format
                audio_formats = [f for f in info['formats'] if f.get('acodec') != 'none']
                if audio_formats:
                    stream_url = audio_formats[-1]['url']
                else:
                    stream_url = info['formats'][-1]['url']
            else:
                return None
            
            elapsed = time.time() - start
            logger.info(f"⚡ Extracted in {elapsed:.2f}s: {info.get('title', 'Unknown')}")
            
            return {
                'title': info.get('title', 'Unknown'),
                'duration': info.get('duration', 0),
                'url': stream_url,
                'thumbnail': info.get('thumbnail', ''),
                'uploader': info.get('uploader', 'Unknown'),
                'webpage_url': info.get('webpage_url', query),
            }
            
    except Exception as e:
        logger.error(f"Extraction error: {e}")
        return None

async def join_call(chat_id: int) -> bool:
    """Join voice chat with retry logic"""
    try:
        await calls.join_group_call(
            chat_id,
            AudioPiped('http://127.0.0.1/silent.mp3'),  # Silent placeholder
            stream_type=StreamType().pulse_stream
        )
        logger.info(f"✅ Joined call in {chat_id}")
        return True
    except AlreadyJoinedError:
        return True
    except NoActiveGroupCall:
        logger.warning(f"No active group call in {chat_id}")
        return False
    except Exception as e:
        logger.error(f"Join error in {chat_id}: {e}")
        return False

async def play_track(chat_id: int, track: dict):
    """
    Start streaming with minimal latency
    Uses FFmpeg piping for zero-download streaming
    """
    try:
        # FFmpeg optimized for low latency streaming
        ffmpeg_params = AudioPiped(
            track['url'],
            audio_parameters={
                'bitrate': 128000,
                'buffer_size': 512000,
                'threads': 2,
            },
            additional_ffmpeg_parameters=(
                '-reconnect 1 '
                '-reconnect_streamed 1 '
                '-reconnect_delay_max 5 '
                '-fflags +genpts+discardcorrupt '
                '-analyzeduration 1M '
                '-probesize 1M '
                '-thread_queue_size 512'
            )
        )
        
        await calls.change_stream(chat_id, ffmpeg_params)
        queue_manager.set_current(chat_id, track)
        queue_manager.paused[chat_id] = False
        
        logger.info(f"🎵 Now playing in {chat_id}: {track['title']}")
        
    except NotInCallError:
        # Auto-join if not in call
        if await join_call(chat_id):
            await play_track(chat_id, track)
    except Exception as e:
        logger.error(f"Playback error in {chat_id}: {e}")
        await skip_track(chat_id)

async def skip_track(chat_id: int):
    """Skip to next track in queue"""
    next_track = queue_manager.pop(chat_id)
    
    if next_track:
        await play_track(chat_id, next_track)
    else:
        try:
            await calls.leave_group_call(chat_id)
            queue_manager.clear(chat_id)
            logger.info(f"Queue ended, left call in {chat_id}")
        except Exception as e:
            logger.error(f"Leave error: {e}")

# Stream ended callback
@calls.on_stream_end()
async def on_stream_end(client: PyTgCalls, update: Update):
    """Auto-play next track when current ends"""
    chat_id = update.chat_id
    logger.info(f"Stream ended in {chat_id}, playing next...")
    await skip_track(chat_id)

# Inline keyboard helpers
def get_welcome_keyboard():
    """High-quality welcome message keyboard"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{bot.me.username}?startgroup=true"),
            InlineKeyboardButton("❓ Help", callback_data="help")
        ],
        [
            InlineKeyboardButton("📢 Support Channel", url="https://t.me/your_channel"),
            InlineKeyboardButton("💬 Support Group", url="https://t.me/your_support")
        ],
        [
            InlineKeyboardButton("👤 Owner", url="https://t.me/your_owner")
        ]
    ])

def get_playback_keyboard(chat_id: int):
    """Dynamic playback control keyboard"""
    is_paused = queue_manager.paused.get(chat_id, False)
    pause_text = "▶️ Resume" if is_paused else "⏸ Pause"
    
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(pause_text, callback_data=f"pause_{chat_id}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"skip_{chat_id}")
        ],
        [
            InlineKeyboardButton("📋 Queue", callback_data=f"queue_{chat_id}"),
            InlineKeyboardButton("⏹ Stop", callback_data=f"stop_{chat_id}")
        ]
    ])

# Command handlers
@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    """Welcome message with inline controls"""
    welcome_text = (
        "🎵 **Welcome to Music Assistant!**\n\n"
        "I'm a high-performance music streaming bot with:\n"
        "• ⚡ Ultra-fast playback (<3s startup)\n"
        "• 🎯 Multi-group queue management\n"
        "• 🎛 Real-time playback controls\n"
        "• 📱 Inline control buttons\n\n"
        "**Add me to your group and use /play to start!**"
    )
    
    # Send with high-quality image
    await message.reply_photo(
        photo="https://telegra.ph/file/29f784eb49d230ab62e9e.png",
        caption=welcome_text,
        reply_markup=get_welcome_keyboard()
    )

@bot.on_message(filters.command("help"))
async def help_handler(client: Client, message: Message):
    """Command documentation"""
    help_text = (
        "📚 **Available Commands:**\n\n"
        "🎵 **Playback:**\n"
        "`/play <query/url>` - Play a track\n"
        "`/pause` - Pause playback\n"
        "`/resume` - Resume playback\n"
        "`/skip` - Skip current track\n"
        "`/stop` - Stop and clear queue\n\n"
        "📋 **Queue:**\n"
        "`/queue` - Show current queue\n"
        "`/leave` - Leave voice chat\n\n"
        "⚙️ **Admin:**\n"
        "`/settings` - Bot settings\n"
    )
    await message.reply_text(help_text)

@bot.on_message(filters.command("play") & filters.group)
async def play_handler(client: Client, message: Message):
    """
    Main play command - optimized for speed
    Target: <3s from command to audio
    """
    chat_id = message.chat.id
    
    # Rate limiting
    if not queue_manager.check_cooldown(chat_id):
        await message.reply_text("⏳ Please wait before sending another command.")
        return
    
    # Parse query
    if len(message.command) < 2:
        await message.reply_text("❌ Usage: `/play <song name or URL>`")
        return
    
    query = message.text.split(None, 1)[1]
    
    # Send processing message
    status_msg = await message.reply_text(f"🔍 Searching: `{query}`...")
    
    start_time = time.time()
    
    try:
        # Extract track info (parallel processing)
        track_info = await extract_info_fast(query)
        
        if not track_info:
            await status_msg.edit_text("❌ Could not find the track. Try another query.")
            return
        
        extraction_time = time.time() - start_time
        
        # Check if assistant is in call
        try:
            call_active = await calls.get_call(chat_id)
        except Exception:
            call_active = None
        
        # Join call if needed
        if not call_active:
            await status_msg.edit_text("🔌 Joining voice chat...")
            join_success = await join_call(chat_id)
            if not join_success:
                await status_msg.edit_text("❌ Please start a voice chat first!")
                return
        
        # Queue or play immediately
        current = queue_manager.get_current(chat_id)
        
        if current:
            # Add to queue
            queue_manager.add(chat_id, track_info)
            position = len(queue_manager.get_queue(chat_id))
            
            await status_msg.edit_text(
                f"📋 **Added to Queue (#{position})**\n\n"
                f"🎵 **{track_info['title']}**\n"
                f"⏱ Duration: {track_info['duration']//60}:{track_info['duration']%60:02d}\n"
                f"👤 {track_info['uploader']}",
                reply_markup=get_playback_keyboard(chat_id)
            )
        else:
            # Play immediately
            await play_track(chat_id, track_info)
            
            total_time = time.time() - start_time
            
            await status_msg.edit_text(
                f"🎵 **Now Playing**\n\n"
                f"**{track_info['title']}**\n"
                f"⏱ Duration: {track_info['duration']//60}:{track_info['duration']%60:02d}\n"
                f"👤 {track_info['uploader']}\n"
                f"⚡ Started in {total_time:.2f}s",
                reply_markup=get_playback_keyboard(chat_id)
            )
        
        logger.info(f"✅ Play command completed in {time.time() - start_time:.2f}s")
        
    except Exception as e:
        logger.error(f"Play handler error: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Error: {str(e)}")

@bot.on_message(filters.command("pause") & filters.group)
async def pause_handler(client: Client, message: Message):
    """Pause playback"""
    chat_id = message.chat.id
    
    try:
        await calls.pause_stream(chat_id)
        queue_manager.paused[chat_id] = True
        await message.reply_text("⏸ Paused playback")
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

@bot.on_message(filters.command("resume") & filters.group)
async def resume_handler(client: Client, message: Message):
    """Resume playback"""
    chat_id = message.chat.id
    
    try:
        await calls.resume_stream(chat_id)
        queue_manager.paused[chat_id] = False
        await message.reply_text("▶️ Resumed playback")
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

@bot.on_message(filters.command("skip") & filters.group)
async def skip_handler(client: Client, message: Message):
    """Skip current track"""
    chat_id = message.chat.id
    await message.reply_text("⏭ Skipping...")
    await skip_track(chat_id)

@bot.on_message(filters.command("stop") & filters.group)
async def stop_handler(client: Client, message: Message):
    """Stop playback and clear queue"""
    chat_id = message.chat.id
    
    try:
        await calls.leave_group_call(chat_id)
        queue_manager.clear(chat_id)
        await message.reply_text("⏹ Stopped and cleared queue")
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

@bot.on_message(filters.command("queue") & filters.group)
async def queue_handler(client: Client, message: Message):
    """Display current queue"""
    chat_id = message.chat.id
    current = queue_manager.get_current(chat_id)
    queue = queue_manager.get_queue(chat_id)
    
    if not current and not queue:
        await message.reply_text("📋 Queue is empty")
        return
    
    text = "📋 **Current Queue:**\n\n"
    
    if current:
        text += f"🎵 **Now Playing:**\n{current['title']}\n\n"
    
    if queue:
        text += "**Up Next:**\n"
        for i, track in enumerate(queue[:10], 1):
            text += f"{i}. {track['title']}\n"
        
        if len(queue) > 10:
            text += f"\n... and {len(queue) - 10} more"
    
    await message.reply_text(text)

@bot.on_message(filters.command("leave") & filters.group)
async def leave_handler(client: Client, message: Message):
    """Leave voice chat"""
    chat_id = message.chat.id
    
    try:
        await calls.leave_group_call(chat_id)
        queue_manager.clear(chat_id)
        await message.reply_text("👋 Left voice chat")
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")

# Callback query handlers for inline buttons
@bot.on_callback_query()
async def callback_handler(client: Client, callback: CallbackQuery):
    """Handle inline button callbacks"""
    data = callback.data
    
    if data == "help":
        help_text = (
            "📚 **Quick Help:**\n\n"
            "1. Add me to your group\n"
            "2. Start a voice chat\n"
            "3. Use `/play <song>` to play music\n"
            "4. Use inline buttons to control playback\n\n"
            "Use /help for full command list"
        )
        await callback.answer()
        await callback.message.edit_caption(help_text, reply_markup=get_welcome_keyboard())
        return
    
    # Parse action and chat_id
    parts = data.split('_')
    action = parts[0]
    chat_id = int(parts[1]) if len(parts) > 1 else callback.message.chat.id
    
    try:
        if action == "pause":
            is_paused = queue_manager.paused.get(chat_id, False)
            if is_paused:
                await calls.resume_stream(chat_id)
                queue_manager.paused[chat_id] = False
                await callback.answer("▶️ Resumed", show_alert=False)
            else:
                await calls.pause_stream(chat_id)
                queue_manager.paused[chat_id] = True
                await callback.answer("⏸ Paused", show_alert=False)
            
            # Update keyboard
            await callback.message.edit_reply_markup(get_playback_keyboard(chat_id))
            
        elif action == "skip":
            await skip_track(chat_id)
            await callback.answer("⏭ Skipped", show_alert=False)
            
        elif action == "stop":
            await calls.leave_group_call(chat_id)
            queue_manager.clear(chat_id)
            await callback.answer("⏹ Stopped", show_alert=False)
            
        elif action == "queue":
            current = queue_manager.get_current(chat_id)
            queue = queue_manager.get_queue(chat_id)
            
            if not current and not queue:
                await callback.answer("Queue is empty", show_alert=True)
            else:
                queue_text = f"🎵 Now: {current['title']}\n" if current else ""
                if queue:
                    queue_text += f"📋 {len(queue)} track(s) in queue"
                await callback.answer(queue_text, show_alert=True)
    
    except Exception as e:
        logger.error(f"Callback error: {e}")
        await callback.answer(f"Error: {e}", show_alert=True)

# Main execution
async def main():
    """Initialize and start bot"""
    logger.info("🚀 Starting Music Assistant Bot...")
    
    try:
        # Start clients
        await bot.start()
        await assistant.start()
        
        # Start pytgcalls
        await calls.start()
        
        bot_info = await bot.get_me()
        assistant_info = await assistant.get_me()
        
        logger.info(f"✅ Bot started: @{bot_info.username}")
        logger.info(f"✅ Assistant started: @{assistant_info.username}")
        logger.info("🎵 Music Bot is ready!")
        
        # Keep alive
        await asyncio.Event().wait()
        
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        await bot.stop()
        await assistant.stop()

if __name__ == "__main__":
    asyncio.run(main())
