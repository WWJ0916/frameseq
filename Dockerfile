# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    VIDEO_PLATFORM_ROOT=/var/lib/frameseq \
    TMPDIR=/var/lib/frameseq/tmp \
    XDG_CACHE_HOME=/var/lib/frameseq/cache \
    HF_HOME=/var/lib/frameseq/cache/huggingface \
    TORCH_HOME=/var/lib/frameseq/cache/torch \
    PIP_CACHE_DIR=/var/lib/frameseq/cache/pip

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-arphic-ukai ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-platform.txt ./
RUN pip install --no-cache-dir -r requirements-platform.txt

# Explicit source allowlist: never COPY the developer's workspace or credentials.
COPY video_platform/ ./video_platform/
COPY scripts/ ./scripts/
COPY web/ ./web/
COPY preview/ ./preview/
COPY README.md FRIEND_README.md LICENSE NOTICE.md ./
RUN groupadd --gid 10001 frameseq \
    && useradd --uid 10001 --gid frameseq --no-create-home frameseq \
    && mkdir -p /var/lib/frameseq \
    && chown frameseq:frameseq /var/lib/frameseq
USER 10001:10001
EXPOSE 18080
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=4 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18080/healthz', timeout=3).read()"]
CMD ["python", "-m", "scripts.container_review", "serve", "--host", "0.0.0.0", "--port", "18080"]
