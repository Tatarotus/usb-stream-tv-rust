FROM python:3.11-slim

# Install ffmpeg and required utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    fonts-dejavu-core \
    quickjs \
    aria2 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir yt-dlp

WORKDIR /app

# Copy application files
COPY server.py ./
COPY torrent_downloader.py ./
COPY channels.json ./
COPY generate_slate.sh ./
COPY dashboard.html ./
COPY manifest.json ./
COPY sw.js ./
COPY app-icon.png ./
COPY app-icon-512.png ./
COPY screenshot-desktop.png ./
COPY screenshot-mobile.png ./

# Generate slate video files (720p and 1080p)
RUN chmod +x generate_slate.sh && ./generate_slate.sh /app

# Default environment variables
ENV HOST=0.0.0.0 \
    PORT=8080 \
    PYTHONUNBUFFERED=1 \
    CHANNELS_FILE=/app/channels.json \
    MOVIES_DIR=/app/filmes

EXPOSE 8080

CMD ["python3", "server.py"]
