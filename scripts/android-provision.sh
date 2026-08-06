#!/usr/bin/env bash
# Installs a NeonDragonsApp debug APK into the running "android" emulator
# container (see the "android" Compose profile in docker-compose.yml /
# truenas-compose.yml and docs/DEPLOYMENT.md) so it's ready to open from
# nd-world's /androidapp page.
#
# Usage (from the repo's root folder):
#   bash scripts/android-provision.sh path/to/neon-dragons-debug.apk
#
# The APK is copied into the container's shared /apk volume, then installed
# with `adb install -r` (replacing any previous install so re-running this
# after a new CI build just updates it in place).

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '  %s\n' "$1"; }
fail() { printf '\033[31mError: %s\033[0m\n' "$1" >&2; exit 1; }

APK_PATH="${1:-}"
if [ -z "$APK_PATH" ]; then
  fail "Usage: bash scripts/android-provision.sh path/to/neon-dragons-debug.apk"
fi
if [ ! -f "$APK_PATH" ]; then
  fail "No such file: $APK_PATH"
fi

if ! docker compose ps android >/dev/null 2>&1 || [ -z "$(docker compose ps -q android 2>/dev/null)" ]; then
  fail "The 'android' service isn't running. Start it first: docker compose --profile android up -d"
fi

bold "Installing $(basename "$APK_PATH") into the Android emulator..."
docker compose cp "$APK_PATH" android:/apk/latest.apk
info "Copied into the container. Waiting for the emulator's adb to be ready..."
docker compose exec android adb wait-for-device
docker compose exec android adb install -r /apk/latest.apk

echo
bold "Done — open /androidapp in nd-world and launch the app from the emulator's app drawer."
