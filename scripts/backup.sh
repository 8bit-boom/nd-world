#!/usr/bin/env bash
# Downloads a full nd-world backup (database + uploads + maps) via the GM-only
# /admin/backup.zip endpoint and writes it to a timestamped file, pruning old
# backups beyond a retention count. Meant for cron.
#
# Why hit the HTTP endpoint instead of copying /data directly: nd-world runs as
# a single container and /data may be a Docker named volume with no host path
# to copy from, or the app may be running elsewhere entirely. The endpoint
# itself uses SQLite's VACUUM INTO to take a consistent snapshot, so this is
# safe to run while the app is live.
#
# Usage (from the repo's root folder, with .env already set up):
#   bash scripts/backup.sh
#
# Cron example (nightly at 3am, keep 14 days):
#   0 3 * * * cd /path/to/nd-world && BACKUP_DIR=/mnt/backups/nd-world KEEP=14 bash scripts/backup.sh >> /var/log/nd-world-backup.log 2>&1

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

NDWORLD_URL="${NDWORLD_URL:-http://localhost:${APP_PORT:-8080}}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP="${KEEP:-14}"

if [ -z "${GM_EMAIL:-}" ] || [ -z "${GM_PASSWORD:-}" ]; then
  echo "Error: GM_EMAIL and GM_PASSWORD must be set (in .env or the environment)." >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
stamp="$(date +%Y%m%d-%H%M%S)"
out_file="$BACKUP_DIR/nd-world-backup-$stamp.zip"
cookie_jar="$(mktemp)"
trap 'rm -f "$cookie_jar"' EXIT

echo "Logging in to $NDWORLD_URL as $GM_EMAIL..."
login_status="$(curl -sS -o /dev/null -w '%{http_code}' \
  -c "$cookie_jar" \
  --data-urlencode "email=$GM_EMAIL" \
  --data-urlencode "password=$GM_PASSWORD" \
  --data-urlencode "next=/" \
  "$NDWORLD_URL/login")"
if [ "$login_status" != "303" ]; then
  echo "Error: login failed (HTTP $login_status). Check GM_EMAIL/GM_PASSWORD and NDWORLD_URL." >&2
  exit 1
fi

echo "Downloading backup to $out_file..."
backup_status="$(curl -sS -o "$out_file" -w '%{http_code}' -b "$cookie_jar" "$NDWORLD_URL/admin/backup.zip")"
if [ "$backup_status" != "200" ]; then
  echo "Error: backup download failed (HTTP $backup_status)." >&2
  rm -f "$out_file"
  exit 1
fi

size="$(du -h "$out_file" | cut -f1)"
echo "Backup complete: $out_file ($size)"

if [ "$KEEP" -gt 0 ]; then
  # shellcheck disable=SC2012
  stale="$(ls -1t "$BACKUP_DIR"/nd-world-backup-*.zip 2>/dev/null | tail -n +$((KEEP + 1)))"
  if [ -n "$stale" ]; then
    echo "Pruning $(echo "$stale" | wc -l) backup(s) older than the last $KEEP..."
    echo "$stale" | xargs rm -f
  fi
fi
