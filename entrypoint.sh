#!/usr/bin/env bash
set -euo pipefail

# Write two cron entries: fast local scan (frequent) and network sync (daily).
# Both schedules can be overridden via compose/k8s env vars.
SCAN_CRON_SCHEDULE="${SCAN_CRON_SCHEDULE:-*/5 * * * *}"
SYNC_CRON_SCHEDULE="${SYNC_CRON_SCHEDULE:-0 3 * * *}"

printf '%s root /usr/local/bin/music-scan >> /var/log/music-scan.log 2>&1\n' \
    "$SCAN_CRON_SCHEDULE" > /etc/cron.d/pipeline-cron
printf '%s root /usr/local/bin/music-ingest >> /var/log/music-ingest.log 2>&1\n' \
    "$SYNC_CRON_SCHEDULE" >> /etc/cron.d/pipeline-cron
chmod 0644 /etc/cron.d/pipeline-cron

exec "$@"
