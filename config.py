"""
Configuration management for Music Bot
Centralized settings with validation
"""

import os
from dataclasses import dataclass
from typing import Optional

@dataclass
class BotConfig:
    """Bot configuration with validation"""
    
    # Telegram API credentials
    api_id: int
    api_hash: str
    bot_token: str
    session_string: str
    owner_id: int
    
    # Performance settings
    max_queue_length: int = 50
    command_cooldown: int = 2  # seconds
    extraction_timeout: int = 30
    stream_buffer_size: int = 512000
    
    # Rate limiting
    max_requests_per_minute: int = 20
    flood_wait_time: int = 5
    
    # Redis configuration (optional)
    redis_url: Optional[str] = None
    redis_enabled: bool = False
    
    # Monitoring
    sentry_dsn: Optional[str] = None
    sentry_enabled: bool = False
    
    # Feature flags
    playlist_support: bool = True
    spotify_support: bool = False
    soundcloud_support: bool = True
    
    # Quality settings
    default_audio_bitrate: int = 128000
    max_audio_bitrate: int = 320000
    
    # Logging
    log_level: str = "INFO"
    log_to_file: bool = True
    
    @classmethod
    def from_env(cls) -> 'BotConfig':
        """Load configuration from environment variables"""
        
        # Required variables
        api_id = os.getenv("API_ID")
        api_hash = os.getenv("API_HASH")
        bot_token = os.getenv("BOT_TOKEN")
        session_string = os.getenv("SESSION_STRING")
        owner_id = os.getenv("OWNER_ID", "0")
        
        if not all([api_id, api_hash, bot_token, session_string]):
            raise ValueError(
                "Missing required environment variables: "
                "API_ID, API_HASH, BOT_TOKEN, SESSION_STRING"
            )
        
        # Optional variables
        redis_url = os.getenv("REDIS_URL")
        sentry_dsn = os.getenv("SENTRY_DSN")
        
        return cls(
            api_id=int(api_id),
            api_hash=api_hash,
            bot_token=bot_token,
            session_string=session_string,
            owner_id=int(owner_id),
            redis_url=redis_url,
            redis_enabled=bool(redis_url),
            sentry_dsn=sentry_dsn,
            sentry_enabled=bool(sentry_dsn),
            max_queue_length=int(os.getenv("MAX_QUEUE_LENGTH", "50")),
            command_cooldown=int(os.getenv("COMMAND_COOLDOWN", "2")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )
    
    def validate(self):
        """Validate configuration"""
        if self.api_id <= 0:
            raise ValueError("Invalid API_ID")
        
        if len(self.api_hash) < 32:
            raise ValueError("Invalid API_HASH")
        
        if not self.bot_token.startswith(("1", "2", "5", "6", "7")):
            raise ValueError("Invalid BOT_TOKEN format")
        
        if self.max_queue_length < 1 or self.max_queue_length > 100:
            raise ValueError("MAX_QUEUE_LENGTH must be between 1 and 100")

# Global config instance
config = BotConfig.from_env()
config.validate()
