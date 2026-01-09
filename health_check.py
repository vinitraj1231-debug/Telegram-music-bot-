"""
Health check HTTP server for Render.com
Provides status endpoint and metrics
"""

import asyncio
import logging
import time
from aiohttp import web

logger = logging.getLogger(__name__)

class HealthCheckServer:
    """Simple HTTP server for health checks"""
    
    def __init__(self, port: int = 8080):
        self.port = port
        self.app = web.Application()
        self.setup_routes()
        self.start_time = time.time()
    
    def setup_routes(self):
        """Setup HTTP routes"""
        self.app.router.add_get('/health', self.health_handler)
        self.app.router.add_get('/metrics', self.metrics_handler)
        self.app.router.add_get('/', self.root_handler)
    
    async def root_handler(self, request):
        """Root endpoint"""
        return web.Response(text="Telegram Music Bot - Running ✅")
    
    async def health_handler(self, request):
        """Health check endpoint for Render"""
        try:
            # Check bot is running
            uptime = time.time() - self.start_time
            
            health_data = {
                'status': 'healthy',
                'uptime': f"{uptime:.2f}s",
                'timestamp': time.time()
            }
            
            return web.json_response(health_data)
            
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return web.json_response(
                {'status': 'unhealthy', 'error': str(e)},
                status=503
            )
    
    async def metrics_handler(self, request):
        """Metrics endpoint"""
        try:
            from utils import perf_monitor
            
            stats = perf_monitor.get_stats()
            return web.json_response(stats)
            
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
    
    async def start(self):
        """Start health check server"""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', self.port)
        await site.start()
        logger.info(f"🏥 Health check server started on port {self.port}")

# Standalone health check for Docker
if __name__ == "__main__":
    import sys
    import httpx
    
    try:
        response = httpx.get('http://localhost:8080/health', timeout=5)
        if response.status_code == 200:
            print("✅ Health check passed")
            sys.exit(0)
        else:
            print(f"❌ Health check failed: {response.status_code}")
            sys.exit(1)
    except Exception as e:
        print(f"❌ Health check error: {e}")
        sys.exit(1)
