# Production-ready Dockerfile with guaranteed dependency installation
FROM python:3.11-slim as builder

# Prevent Python from writing pyc files and buffering
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    libffi-dev \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create and activate virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip and install wheel
RUN pip install --upgrade pip setuptools wheel

# Copy and install requirements
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && \
    pip list && \
    echo "✅ All dependencies installed successfully"

# ==========================================
# Final Stage - Production Image
# ==========================================
FROM python:3.11-slim

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080

# Install runtime dependencies (FFmpeg, curl for health checks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Verify installations
RUN python --version && \
    pip list && \
    python -c "import pyrogram; print('✅ Pyrogram:', pyrogram.__version__)" && \
    python -c "import pytgcalls; print('✅ PyTgCalls installed')" && \
    python -c "import yt_dlp; print('✅ yt-dlp installed')"

# Create app directory
WORKDIR /app

# Copy application files
COPY main.py .
COPY health_check.py .
COPY start.sh .

# Make scripts executable
RUN chmod +x start.sh

# Create non-root user for security
RUN useradd -m -u 1000 botuser && \
    chown -R botuser:botuser /app && \
    mkdir -p /tmp/pytgcalls /home/botuser/.cache && \
    chown -R botuser:botuser /tmp/pytgcalls /home/botuser/.cache

# Switch to non-root user
USER botuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

# Expose port
EXPOSE 8080

# Start bot
CMD ["bash", "start.sh"]
