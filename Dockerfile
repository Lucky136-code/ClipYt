FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies: FFmpeg, subtitle fonts, curl, ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-liberation \
    fonts-dejavu-core \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face requires a non-root user with UID 1000
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY --chown=user:user . .

USER user

# Ensure writable temp directories exist
RUN mkdir -p /tmp/clipyt /app/data/temp

EXPOSE 7860

# Launch FastAPI directly on port 7860
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
