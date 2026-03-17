#!/usr/bin/env bash
set -euo pipefail

# Write the cron schedule for music-ingest into /etc/cron.d.
# CRON_SCHEDULE defaults to 03:00 daily. Override via compose env.
CRON_SCHEDULE="${CRON_SCHEDULE:-0 3 * * *}"
echo "${CRON_SCHEDULE} root /usr/local/bin/music-ingest >> /var/log/music-ingest.log 2>&1" \
    > /etc/cron.d/pipeline-cron
chmod 0644 /etc/cron.d/pipeline-cron

exec "$@"
