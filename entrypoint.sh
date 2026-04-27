#!/bin/bash
# Adjust the runtime UID/GID of the librarydog user so files written to the
# mounted /app/downloads volume end up owned by the host user, then drop
# privileges and exec the app. LinuxServer.io-style PUID/PGID convention.
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

NEEDS_REOWN=0
if [ "$(id -u librarydog)" != "$PUID" ] || [ "$(id -g librarydog)" != "$PGID" ]; then
    groupmod -o -g "$PGID" librarydog
    usermod  -o -u "$PUID" librarydog
    NEEDS_REOWN=1
fi

# Cheap, non-recursive: just the volume mount points. New files inherit the
# right ownership from the running process; pre-existing host-managed libraries
# / seeds keep theirs.
chown librarydog:librarydog /app/downloads 2>/dev/null || true
chown librarydog:librarydog /app/torrents  2>/dev/null || true

# /app/.cache holds Playwright's Chromium install — roughly 10k small files.
# Recursive chown over that on slow storage (NAS, Unraid array) was costing
# 30–90s of silent startup. Only rewrite ownership when the runtime UID
# actually differs from what the image was built with; otherwise it's a slow
# no-op walk on every container start.
if [ "$NEEDS_REOWN" = "1" ]; then
    echo "[library-dog] adjusting /app/.cache ownership for PUID=$PUID (one-time, can take a moment)..."
    chown -R librarydog:librarydog /app/.cache 2>/dev/null || true
fi

exec gosu librarydog:librarydog "$@"
