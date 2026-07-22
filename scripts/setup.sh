#!/usr/bin/env bash
# One-command setup for self-hosting nd-world (TrueNAS, any Linux box, or a Mac
# with Docker).
#
# What this does, step by step:
#   1. Checks that Docker is installed and working.
#   2. Creates a .env file with a random session secret (unless you already
#      have one — it will never overwrite an existing .env), and asks for
#      your GM login (the account you'll use to run your worlds — there's no
#      public signup, players only ever join via a GM-issued invite link).
#   3. Builds and starts nd-world + SwarmUI + Ollama with `docker compose`.
#   4. Prints the web address to open once it's ready.
#
# Usage (from the repo's root folder):
#   bash scripts/setup.sh

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '  %s\n' "$1"; }
fail() { printf '\033[31mError: %s\033[0m\n' "$1" >&2; exit 1; }

bold "N&D World — self-host setup"
echo

# --- 1. Check Docker ---------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  fail "Docker isn't installed or isn't on your PATH. On TrueNAS, make sure the Apps service is enabled first."
fi
if ! docker compose version >/dev/null 2>&1; then
  fail "'docker compose' isn't available. Docker needs the Compose plugin (recent Docker installs include it automatically)."
fi
info "Docker is available."

# --- 2. Create .env if needed -------------------------------------------
if [ -f .env ]; then
  info ".env already exists — leaving it as-is."
else
  info "Creating .env..."
  cp .env.example .env

  SECRET_KEY_VALUE="$(openssl rand -hex 32)"
  sedi() { sed -i.bak "$1" .env && rm -f .env.bak; }
  sedi "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET_KEY_VALUE}|"

  echo
  if [ -t 0 ]; then
    info "Set up your GM login (this is the account you'll use to manage worlds —"
    info "everyone else joins later as a player via an invite link you generate)."
    read -r -p "  GM email: " GM_EMAIL_VALUE
    read -r -s -p "  GM password (min 8 characters): " GM_PASSWORD_VALUE
    echo
    sedi "s|^GM_EMAIL=.*|GM_EMAIL=${GM_EMAIL_VALUE}|"
    sedi "s|^GM_PASSWORD=.*|GM_PASSWORD=${GM_PASSWORD_VALUE}|"

    echo
    info "AI chat (Ollama) and AI image generation (SwarmUI) are optional —"
    info "both are large downloads and Ollama in particular wants a decent GPU/CPU."
    read -r -p "  Enable AI chat (Ollama)? [y/N] " ENABLE_OLLAMA
    read -r -p "  Enable AI image generation (SwarmUI)? [y/N] " ENABLE_SWARMUI
    PROFILES=""
    [[ "$ENABLE_OLLAMA" =~ ^[Yy] ]] && PROFILES="ollama"
    if [[ "$ENABLE_SWARMUI" =~ ^[Yy] ]]; then
      PROFILES="${PROFILES:+$PROFILES,}swarmui"
    fi
    sedi "s|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=${PROFILES}|"
  else
    info "No terminal input available — leaving GM_EMAIL/GM_PASSWORD blank."
    info "Edit .env and set them before first start, or after: fill them in,"
    info "then run 'docker compose up -d' again to bootstrap the GM account."
  fi
  info "Done."
fi

# --- 3. Build and start --------------------------------------------------
echo
bold "Building and starting the stack (this can take a few minutes the first time)..."
docker compose up -d --build

# --- 4. Wait for it to come up -------------------------------------------
echo
printf "Waiting for nd-world to become healthy"
APP_PORT="$(grep -E '^APP_PORT=' .env 2>/dev/null | cut -d= -f2)"
APP_PORT="${APP_PORT:-8080}"
READY=0
for _ in $(seq 1 60); do
  if curl -fs "http://localhost:${APP_PORT}/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  printf '.'
  sleep 2
done
echo

if [ "$READY" != "1" ]; then
  echo
  bold "It's taking longer than expected to come up."
  info "Check what's happening with: docker compose logs world"
  exit 1
fi

LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
LOCAL_IP="${LOCAL_IP:-<your-server-ip>}"

echo
bold "✅ It's running!"
info "On this network, open: http://${LOCAL_IP}:${APP_PORT}"
info "(or http://localhost:${APP_PORT} from this machine itself)"
if grep -qE '^GM_EMAIL=.+' .env && grep -qE '^GM_PASSWORD=.+' .env; then
  info "Log in with the GM email/password you just set."
else
  info "GM_EMAIL/GM_PASSWORD aren't set yet — edit .env, then run 'docker compose up -d' again."
fi
CURRENT_PROFILES="$(grep -E '^COMPOSE_PROFILES=' .env 2>/dev/null | cut -d= -f2)"
if [ -n "$CURRENT_PROFILES" ]; then
  info "AI features enabled: ${CURRENT_PROFILES}"
else
  info "AI chat/image generation are off. Enable later by setting COMPOSE_PROFILES"
  info "in .env (e.g. COMPOSE_PROFILES=ollama,swarmui), then 'docker compose up -d'."
fi
echo
info "To invite players: log in as the GM, open a world's Edit page, and create"
info "an Invite Link under 'Invite Links'. Share the /join/... link it gives you."
echo
info "To make it reachable from the internet, see docs/DEPLOYMENT.md"
info "(Cloudflare Tunnel is the recommended, easiest option)."
