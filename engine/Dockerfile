# syntax=docker/dockerfile:1
#
# Spool engine — headless JSON API (routes/api_v1.py) + MCP server. No UI:
# the studio (apps/studio) is a separate Next.js app that talks to this over HTTP.
#
# Multi-stage: compile native deps (webrtcvad's C ext, etc.) in a builder that has
# the toolchain, then ship a lean runtime carrying only the shared libs the stack
# needs at run time.

FROM python:3.12-slim AS builder
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential python3-dev && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt .
# requirements.txt pins yt-dlp to its master tarball (YouTube extractors break on
# stable) and includes the diarization stack (core install). Install into an
# isolated prefix we copy into the runtime stage.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
# Runtime system libs: ffmpeg (audio/video), libgomp1 (OpenMP — whisper.cpp / torch),
# libsndfile1 (soundfile — the diarization/audio stack).
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libgomp1 libsndfile1 && \
    rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local

RUN useradd -m -u 1000 spool
WORKDIR /app

# Persist the whisper.cpp model cache across container restarts.
VOLUME /app/models

# Engine code: flat trove modules + the api_v1 blueprint + the clip back-half package.
COPY *.py ./
COPY routes/ ./routes/
COPY clip/ ./clip/

RUN mkdir -p downloads && chown -R spool:spool /app
USER spool

EXPOSE 8899
# Localhost-only is the safe default (config.assert_safe_bind refuses a public bind
# without TROVE_TOKEN). docker-compose.yml overrides HOST=0.0.0.0 + sets a token so
# the studio on the host can reach the container.
ENV HOST=127.0.0.1
ENV PORT=8899
CMD ["python", "app.py"]
