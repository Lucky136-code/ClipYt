FROM python:3.11-slim

# Install FFmpeg, Node.js (for yt-dlp JS challenges), and crisp bold fonts for ASS subtitles
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    fontconfig \
    fonts-dejavu-core \
    fonts-liberation \
    ca-certificates \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user required by Hugging Face Spaces (UID 1000)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PORT=7860

WORKDIR $HOME/app

# Install Python dependencies first for better Docker layer caching
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY --chown=user . .

# Ensure writable runtime directories exist
RUN mkdir -p data/temp

EXPOSE 7860

CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-7860}"]
