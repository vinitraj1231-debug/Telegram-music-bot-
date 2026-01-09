"""
Health check HTTP server for Render.com
Simple and lightweight implementation
"""

import asyncio
import logging
import os
import time
from aiohttp import web

logger = logging.getLogger(__name__)

start_time = time.time()

async def root_handler(request):
    """Root endpoint"""
    uptime = time.time() - start_time
    return web.Response(
        text=f"Telegram Music Bot - Running ✅\nUptime: {uptime:.0f}s",
        content_type='text/plain'
    )

async def health_handler(request):
    """Health check endpoint"""
    return web.json_response({
        'status': 'healthy',
        'uptime': f"{time.time() - start_time:.2f}s",
        'timestamp': time.time()
    })

async def start_health_server():
    """Start health check server"""
    app = web.Application()
    app.router.add_get('/', root_handler)
    app.router.add_get('/health', health_handler)
    
    port = int(os.getenv('PORT', 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    logger.info(f"🏥 Health check server started on port {port}")
    return runner

if __name__ == "__main__":
    # Standalone health check for Docker
    import sys
    try:
        import requests
        response = requests.get('http://localhost:8080/health', timeout=5)
        if response.status_code == 200:
            print("✅ Health check passed")
            sys.exit(0)
        else:
            print(f"❌ Health check failed: {response.status_code}")
            sys.exit(1)
    except Exception as e:
        print(f"❌ Health check error: {e}")
        sys.exit(1)
