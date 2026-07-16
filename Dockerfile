# fakes3 — local Amazon S3 replica (FastAPI + uvicorn)
#
# Build:  docker build -t fakes3 .
# Run:    docker run -p 9000:9000 -v ./storage:/data fakes3
# (or simply: docker compose up)

FROM python:3.13-slim

# Sensible Python-in-Docker defaults: no .pyc clutter, unbuffered logs,
# no pip version-check noise or cache in the image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fakes3/ ./fakes3/

# Run as an unprivileged user; objects are stored under /data (a volume).
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data
USER appuser

ENV FAKE_S3_STORAGE=/data \
    FAKE_S3_PORT=9000

VOLUME ["/data"]
EXPOSE 9000

# GET /health is the app's reserved liveness probe (see fakes3.py docstring).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('FAKE_S3_PORT','9000')}/health\")" || exit 1

CMD ["python", "-m", "fakes3"]
