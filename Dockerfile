FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# Install system dependencies: FFmpeg, subtitle fonts, curl, certs
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-liberation \
    fonts-dejavu-core \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure writable temp directories exist
RUN mkdir -p /tmp/clipyt /app/data/temp

# Cloud Run defaults to port 8080
EXPOSE 8080

# Launch FastAPI with uvicorn bound to dynamic $PORT
CMD exec uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
