# syntax=docker/dockerfile:1
#
# Spool engine — headless JSON API (routes/api_v1.py) + MCP server. No UI:
# the studio (apps/studio) is a separate Next.js app that talks to this over HTTP.
FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 spool
WORKDIR /app

COPY requirements.txt .
# requirements.txt pins yt-dlp to its master tarball (YouTube extractors break on
# stable) and includes the diarization stack (core install — ~1.3 GB once, cached
# in this layer).
RUN pip install --no-cache-dir -r requirements.txt

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
