# DT Mini Browser Server – container image (linux/amd64 + linux/arm64)
FROM python:3.12-slim-bookworm

# Set by CI to the newest Playwright release (Python driver only).
# Empty = pip picks the latest at build time.
ARG PLAYWRIGHT_VERSION=""

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    MINI_CHROME=/usr/bin/chromium \
    MINI_HOST=0.0.0.0 \
    MINI_PORT=8080 \
    MINI_NO_SANDBOX=1

# Distro Chromium (Debian security updates) + fonts.
# Playwright's own Chromium build is NOT used (it gets blocked by some sites).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium fonts-liberation fonts-dejavu-core fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/* \
 && pip install aiohttp pillow "playwright${PLAYWRIGHT_VERSION:+==$PLAYWRIGHT_VERSION}"

# Non-root user; /data holds states/ downloads/ debug/ (the server writes relative to its cwd)
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin mini \
 && mkdir -p /app /data \
 && chown mini:mini /data

COPY server.py /app/server.py

USER mini
WORKDIR /data
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/ping'%os.environ.get('MINI_PORT','8080'),timeout=4)" || exit 1

CMD ["python", "/app/server.py"]
