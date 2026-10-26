# DT Mini Browser Server – container image (linux/amd64 + linux/arm64)
FROM python:3.12-slim-bookworm

# Set by CI to the newest Playwright release (= newest bundled Chromium).
# Empty = pip picks the latest at build time.
ARG PLAYWRIGHT_VERSION=""

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    MINI_HOST=0.0.0.0 \
    MINI_PORT=8080 \
    MINI_NO_SANDBOX=1

# Python deps + Chromium incl. its system libraries
RUN pip install aiohttp pillow "playwright${PLAYWRIGHT_VERSION:+==$PLAYWRIGHT_VERSION}" \
 && playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright \
 && rm -rf /var/lib/apt/lists/*

# Non-root user; /data holds states/ downloads/ debug/ (the server writes relative to its cwd)
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin mini \
 && mkdir -p /app /data \
 && chown mini:mini /data

COPY server2.py /app/server.py

USER mini
WORKDIR /data
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/ping'%os.environ.get('MINI_PORT','8080'),timeout=4)" || exit 1

CMD ["python", "/app/server.py"]
