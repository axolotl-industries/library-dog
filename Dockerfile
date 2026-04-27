FROM python:3.11-slim

# Whether to install Playwright + Chromium for Anna's Archive / Libgen
# scraping. False by default → small image (~150MB). True for the
# '-grey' build variant in CI → large image (~700MB).
ARG INSTALL_PLAYWRIGHT=false

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

COPY --chown=librarydog:librarydog requirements.txt requirements-grey.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Conditional grey-sources install: pip + chromium binary + system deps.
# Skipped entirely in the standard image, which keeps it ~150MB and removes
# Playwright import overhead from cold start.
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
        pip install --no-cache-dir -r requirements-grey.txt && \
        playwright install chromium && \
        playwright install-deps chromium && \
        chown -R librarydog:librarydog /app/.cache ; \
    fi

COPY --chown=librarydog:librarydog . .
RUN chmod +x /app/entrypoint.sh

EXPOSE 80

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["python", "app.py"]
