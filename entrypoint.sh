#!/bin/bash
# Adjust the runtime UID/GID of the librarydog user so files written to the
# mounted /app/downloads volume end up owned by the host user, then drop
# privileges and exec the app. LinuxServer.io-style PUID/PGID convention.
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

if [ "$(id -u librarydog)" != "$PUID" ] || [ "$(id -g librarydog)" != "$PGID" ]; then
    groupmod -o -g "$PGID" librarydog
    usermod  -o -u "$PUID" librarydog
fi

# Re-own paths the runtime needs to write through. Skipping /app/downloads
# and /app/torrents recursively on purpose: pre-existing libraries / seeds
# may be huge and host-managed, and new files inherit the right ownership
# from the running process.
chown librarydog:librarydog /app/downloads 2>/dev/null || true
chown librarydog:librarydog /app/torrents  2>/dev/null || true
chown -R librarydog:librarydog /app/.cache 2>/dev/null || true

exec gosu librarydog:librarydog "$@"
