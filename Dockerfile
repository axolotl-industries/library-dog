FROM python:3.11-slim

# gosu drops privileges in the entrypoint without su's TTY/process-group quirks.
# tini reaps zombies cleanly when Playwright crashes a Chromium child.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gosu \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Build-time user. Runtime PUID/PGID env vars override these IDs in the entrypoint
# so files written to mounted volumes pick up the host's expected ownership.
RUN groupadd -g 1000 librarydog && \
    useradd -u 1000 -g librarydog -m -s /bin/bash librarydog

WORKDIR /app

# Python's stdout/stderr is block-buffered when attached to a pipe (which is
# what `docker logs` is). Without this, uvicorn's startup banner and any of
# our log lines sit in the buffer until 4–8KB accumulates, making it look
# like the container is hanging for a minute or two on cold starts.
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=UTF-8

ENV PLAYWRIGHT_BROWSERS_PATH=/app/.cache/ms-playwright
RUN mkdir -p /app/.cache/ms-playwright /app/downloads /app/torrents && \
    chown -R librarydog:librarydog /app

COPY --chown=librarydog:librarydog requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browsers + system deps. install-deps must run as root for apt-get.
# install (download) runs as root too; we re-chown the cache afterward so the
# librarydog user can update browser caches at runtime.
RUN playwright install chromium && \
    playwright install-deps chromium && \
    chown -R librarydog:librarydog /app/.cache

COPY --chown=librarydog:librarydog . .
RUN chmod +x /app/entrypoint.sh

EXPOSE 80

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["python", "app.py"]
