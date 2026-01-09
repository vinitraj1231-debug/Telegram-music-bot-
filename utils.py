"""
Utility functions for Music Bot
Performance monitoring, formatting, and helpers
"""

import time
import asyncio
import logging
from typing import Optional, Dict, Any
from functools import wraps
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class PerformanceMonitor:
    """Track bot performance metrics"""
    
    def __init__(self):
        self.metrics: Dict[str, list] = {
            'extraction_times': [],
            'playback_start_times': [],
            'command_response_times': []
        }
        self.start_time = datetime.now()
    
    def record_extraction(self, duration: float):
        """Record extraction time"""
        self.metrics['extraction_times'].append(duration)
        if len(self.metrics['extraction_times']) > 100:
            self.metrics['extraction_times'].pop(0)
    
    def record_playback_start(self, duration: float):
        """Record playback start time"""
        self.metrics['playback_start_times'].append(duration)
        if len(self.metrics['playback_start_times']) > 100:
            self.metrics['playback_start_times'].pop(0)
    
    def record_command_response(self, duration: float):
        """Record command response time"""
        self.metrics['command_response_times'].append(duration)
        if len(self.metrics['command_response_times']) > 100:
            self.metrics['command_response_times'].pop(0)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get performance statistics"""
        stats = {}
        
        for metric_name, values in self.metrics.items():
            if values:
                stats[metric_name] = {
                    'avg': sum(values) / len(values),
                    'min': min(values),
                    'max': max(values),
                    'count': len(values)
                }
        
        stats['uptime'] = str(datetime.now() - self.start_time)
        return stats
    
    def log_stats(self):
        """Log current statistics"""
        stats = self.get_stats()
        logger.info("=== Performance Stats ===")
        for metric, data in stats.items():
            if isinstance(data, dict):
                logger.info(
                    f"{metric}: avg={data['avg']:.2f}s, "
                    f"min={data['min']:.2f}s, max={data['max']:.2f}s"
                )
        logger.info(f"Uptime: {stats.get('uptime', 'N/A')}")

# Global performance monitor
perf_monitor = PerformanceMonitor()

def measure_time(metric_name: str):
    """Decorator to measure function execution time"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.time()
            result = await func(*args, **kwargs)
            duration = time.time() - start
            
            if metric_name == 'extraction':
                perf_monitor.record_extraction(duration)
            elif metric_name == 'playback':
                perf_monitor.record_playback_start(duration)
            elif metric_name == 'command':
                perf_monitor.record_command_response(duration)
            
            logger.debug(f"{func.__name__} took {duration:.2f}s")
            return result
        return wrapper
    return decorator

def format_duration(seconds: int) -> str:
    """Format duration in human-readable format"""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds//60}:{seconds%60:02d}"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours}:{minutes:02d}:{secs:02d}"

def format_bytes(bytes_size: int) -> str:
    """Format bytes in human-readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.2f} TB"

class RateLimiter:
    """Simple rate limiter for commands"""
    
    def __init__(self, max_requests: int, time_window: int):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests: Dict[int, list] = {}
    
    def is_allowed(self, user_id: int) -> bool:
        """Check if user is allowed to make request"""
        now = time.time()
        
        if user_id not in self.requests:
            self.requests[user_id] = []
        
        # Remove old requests
        self.requests[user_id] = [
            req_time for req_time in self.requests[user_id]
            if now - req_time < self.time_window
        ]
        
        # Check limit
        if len(self.requests[user_id]) >= self.max_requests:
            return False
        
        self.requests[user_id].append(now)
        return True
    
    def get_wait_time(self, user_id: int) -> Optional[int]:
        """Get wait time before next request"""
        if user_id not in self.requests or not self.requests[user_id]:
            return 0
        
        oldest_request = self.requests[user_id][0]
        wait_time = self.time_window - (time.time() - oldest_request)
        return max(0, int(wait_time))

class Cache:
    """Simple in-memory cache with TTL"""
    
    def __init__(self, ttl: int = 300):
        self.ttl = ttl
        self.cache: Dict[str, tuple] = {}
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache"""
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return value
            else:
                del self.cache[key]
        return None
    
    def set(self, key: str, value: Any):
        """Set value in cache"""
        self.cache[key] = (value, time.time())
    
    def clear_expired(self):
        """Clear expired entries"""
        now = time.time()
        expired = [
            key for key, (_, timestamp) in self.cache.items()
            if now - timestamp >= self.ttl
        ]
        for key in expired:
            del self.cache[key]
    
    def clear(self):
        """Clear all cache"""
        self.cache.clear()

# Global instances
rate_limiter = RateLimiter(max_requests=20, time_window=60)
track_cache = Cache(ttl=600)  # 10 minutes

async def retry_on_error(func, max_retries: int = 3, delay: int = 1):
    """Retry function on error with exponential backoff"""
    for attempt in range(max_retries):
        try:
            return await func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            
            wait_time = delay * (2 ** attempt)
            logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)

def sanitize_filename(filename: str) -> str:
    """Sanitize filename for safe storage"""
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    return filename[:255]  # Limit length

def parse_duration(duration_str: str) -> int:
    """Parse duration string to seconds"""
    try:
        parts = duration_str.split(':')
        if len(parts) == 2:  # MM:SS
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:  # HH:MM:SS
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        else:
            return int(duration_str)
    except:
        return 0

def get_readable_time():
    """Get current time in readable format"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

async def check_admin(client, chat_id: int, user_id: int) -> bool:
    """Check if user is admin in chat"""
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in ["creator", "administrator"]
    except:
        return False

def create_progress_bar(current: int, total: int, length: int = 10) -> str:
    """Create ASCII progress bar"""
    if total == 0:
        return "▱" * length
    
    filled = int((current / total) * length)
    bar = "▰" * filled + "▱" * (length - filled)
    percentage = (current / total) * 100
    return f"{bar} {percentage:.1f}%"

# Startup performance check
async def warmup_check():
    """Perform warmup operations for faster startup"""
    logger.info("🔥 Running warmup checks...")
    
    # Test yt-dlp
    try:
        import yt_dlp
        logger.info("✅ yt-dlp ready")
    except Exception as e:
        logger.error(f"❌ yt-dlp error: {e}")
    
    # Test ffmpeg
    try:
        import subprocess
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, timeout=5)
        if result.returncode == 0:
            logger.info("✅ FFmpeg ready")
        else:
            logger.warning("⚠️ FFmpeg not found")
    except Exception as e:
        logger.error(f"❌ FFmpeg error: {e}")
    
    logger.info("🚀 Warmup complete")
