# Any Video Downloader Bot - Dockerfile
# Python 3.11 + FFmpeg + FFprobe

FROM denoland/deno:bin-2.9.5 AS deno
FROM python:3.11-slim-trixie

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libmagic1 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Fail the image build if the distro FFmpeg/FFprobe tools do not terminate.
RUN ffmpeg -version >/dev/null \
    && ffprobe -version >/dev/null \
    && timeout 5 ffmpeg -hide_banner -bsfs >/dev/null \
    && timeout 5 ffprobe -hide_banner -version >/dev/null

COPY --from=deno /deno /usr/local/bin/deno

# Create non-root user
RUN useradd --create-home --shell /bin/bash --uid 1000 botuser

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PIP_RETRIES=2 \
    PIP_PROGRESS_BAR=off
RUN pip install --no-cache-dir --prefer-binary --only-binary=curl-cffi -r requirements.txt

# Copy application code
COPY --chown=botuser:botuser . .

# Create data directories
RUN mkdir -p /app/data/jobs /app/logs && chown -R botuser:botuser /app/data /app/logs

# Switch to non-root user
USER botuser

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["python", "healthcheck.py"]

# Entry point
CMD ["python", "-m", "bot.main"]
