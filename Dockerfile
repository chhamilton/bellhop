# syntax=docker/dockerfile:1

# -----------------------------------------------------------------------------
# Stage 1: Compile strictly typed TypeScript frontend -> static/*.js
# -----------------------------------------------------------------------------
FROM node:22-alpine AS frontend-builder

WORKDIR /build
COPY package.json package-lock.json tsconfig.json ./
COPY src/bellhop/web/frontend ./src/bellhop/web/frontend
RUN umask 022 \
  && npm ci --no-audit --no-fund \
  && npx tsc --noEmit \
  && npx tsc

# -----------------------------------------------------------------------------
# Stage 2: Minimal Alpine Linux Python runtime for `bellhop`
# -----------------------------------------------------------------------------
FROM python:3.13-alpine AS runtime

ARG INSTALL_FFMPEG=1

LABEL org.opencontainers.image.title="Bellhop" \
      org.opencontainers.image.description="Self-hosted live TV curator, EPG server, and browser TV guide" \
      org.opencontainers.image.licenses="Apache-2.0"

# Install timezone database (for TZ), privilege drop helper (PUID/PGID),
# CA certificates, and optional ffmpeg/ffprobe (`--build-arg INSTALL_FFMPEG=0` skips it).
RUN apk add --no-cache \
      ca-certificates \
      su-exec \
      tzdata \
  && if [ "${INSTALL_FFMPEG}" = "1" ]; then apk add --no-cache ffmpeg; fi

WORKDIR /app

COPY pyproject.toml requirements.lock README.md LICENSE NOTICE ./
COPY src ./src
COPY data ./data
COPY docker ./docker
COPY --from=frontend-builder /build/src/bellhop/web/static/*.js ./src/bellhop/web/static/

RUN umask 022 \
  && pip install --no-cache-dir -r requirements.lock \
  && pip install --no-cache-dir --no-deps . \
  && mkdir -p /usr/local/libexec /config /data/state /data/cache /data/output/xtream \
  && mv /usr/local/bin/bellhop /usr/local/libexec/bellhop \
  && cp /app/docker/bellhop-shim.sh /usr/local/bin/bellhop \
  && chmod -R go-w /app \
  && chmod 0755 /app/docker/entrypoint.sh /app/docker/bellhop-shim.sh /usr/local/bin/bellhop

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
