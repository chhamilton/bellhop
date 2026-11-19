# syntax=docker/dockerfile:1

# -----------------------------------------------------------------------------
# Stage 1: Compile strictly typed TypeScript frontend -> static/{prefs,app}.js
# -----------------------------------------------------------------------------
FROM node:22-alpine AS frontend-builder

WORKDIR /build
COPY package.json tsconfig.json ./
COPY src/bellhop_web/frontend ./src/bellhop_web/frontend
RUN npm install --no-audit --no-fund \
  && npx tsc --noEmit \
  && npx tsc

# -----------------------------------------------------------------------------
# Stage 2: Minimal Alpine Linux Python runtime for `bellhop`
# -----------------------------------------------------------------------------
FROM python:3.13-alpine AS runtime

LABEL org.opencontainers.image.title="Bellhop" \
      org.opencontainers.image.description="Declarative IPTV lineup pipeline, Web TV Guide & single-tuner player, and passthrough Xtream Codes API server"

# Install timezone database (for TZ), privilege drop helper (PUID/PGID),
# CA certificates, and ffmpeg/ffprobe for optional Phase (4) codec validation.
RUN apk add --no-cache \
      ca-certificates \
      ffmpeg \
      su-exec \
      tzdata

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY data ./data
COPY docker ./docker
COPY --from=frontend-builder /build/src/bellhop_web/static/prefs.js ./src/bellhop_web/static/prefs.js
COPY --from=frontend-builder /build/src/bellhop_web/static/app.js ./src/bellhop_web/static/app.js

RUN pip install --no-cache-dir . \
  && chmod +x /app/docker/entrypoint.sh \
  && mkdir -p /config /data/state /data/cache /data/output/xtream

ENV TZ=UTC \
    PUID=1000 \
    PGID=1000 \
    BELLHOP_CONFIG_DIR=/config \
    BELLHOP_STATE_DIR=/data/state \
    BELLHOP_CACHE_DIR=/data/cache \
    BELLHOP_OUTPUT_DIR=/data/output \
    BELLHOP_CONFIG=/config/config.yaml \
    BELLHOP_DEFAULT_CONFIG=/app/docker/config.docker.yaml \
    PYTHONUNBUFFERED=1

EXPOSE 8080

VOLUME ["/config", "/data/state", "/data/cache", "/data/output"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/auth/status', timeout=3)" || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["bellhop", "serve"]
