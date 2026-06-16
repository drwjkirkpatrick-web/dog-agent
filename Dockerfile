FROM python:3.11-slim-bookworm

# Dog Agent — Docker Image
# Multi-arch support: ARM64 (Pi 4/5), ARMv7 (Pi 3/Zero 2W)

LABEL maintainer="dog-agent"
LABEL version="1.0"
LABEL description="Hermes-powered wearable AI agent for dogs"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libportaudio2 \
    libatlas-base-dev \
    libgpiod2 \
    i2c-tools \
    sqlite3 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create app user
RUN useradd -m -u 1000 dogagent && \
    mkdir -p /app/data && \
    chown -R dogagent:dogagent /app

# Set working directory
WORKDIR /app

# Copy requirements first (for layer caching)
COPY --chown=dogagent:dogagent requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY --chown=dogagent:dogagent src/ ./src/
COPY --chown=dogagent:dogagent config.example.yaml ./
COPY --chown=dogagent:dogagent setup.sh ./
COPY --chown=dogagent:dogagent hardware/ ./hardware/
COPY --chown=dogagent:dogagent hermes/ ./hermes/
COPY --chown=dogagent:dogagent cron/ ./cron/

# Create necessary directories
RUN mkdir -p data/gps_tracks data/health_logs data/behavior data/events data/alerts && \
    chown -R dogagent:dogagent data

# Make setup script executable
RUN chmod +x setup.sh cron/*.sh

# Switch to non-root user
USER dogagent

# Expose ports for all modules
# 9110 - Orchestrator (main dashboard)
# 9111 - GPS daemon
# 9112 - Sensor daemon
# 9113 - Health monitor
# 9114 - Geofence
# 9115 - Behavior
# 9116 - Voice
# 9117 - Data logger
# 9118 - Alert manager
# 9120 - Power manager
# 9121 - Status LED
# 9122 - Environmental sensors
# 9123 - Cache manager
# 9124 - Adaptive GPS
# 9125 - Offline queue
# 9126 - Activity scoring
# 9127 - Weather
# 9128 - Solar monitor
EXPOSE 9110-9128

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:9110/health || exit 1

# Default command: run orchestrator
CMD ["python", "src/main.py", "--all"]
