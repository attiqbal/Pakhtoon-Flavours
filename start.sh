#!/usr/bin/env bash
# Starts Pakhtoon Flavours website on port 8000 (Codespaces or your own computer). Safe to run again any time.
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
if [ -n "$CODESPACE_NAME" ]; then
  BASE="https://${CODESPACE_NAME}-${PORT}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
else
  BASE="http://localhost:${PORT}"
fi

# .env: create it if missing; add any missing setting. Never overwrites a value you set.
touch .env && chmod 600 .env
add_default() { grep -q "^$1=" .env || printf '%s=%s\n' "$1" "$2" >> .env; }
add_default ANTHROPIC_API_KEY ""
add_default META_VERIFY_TOKEN "$(openssl rand -hex 12)"
value() { grep "^$1=" .env | head -1 | cut -d= -f2-; }

set -a; . ./.env; set +a
export PASHTOON_BASE_URL="${PASHTOON_BASE_URL:-$BASE}"

pkill -f "uvicorn pashtoon_site.asgi" 2>/dev/null; sleep 1
pip install -q -r requirements.txt
mkdir -p data
nohup python -m uvicorn pashtoon_site.asgi:app --host 0.0.0.0 --port "$PORT" --proxy-headers > data/site.log 2>&1 &
sleep 2

echo
echo "Pakhtoon Flavours is running at: $PASHTOON_BASE_URL     (log: data/site.log)"
echo "Owner dashboard:  $PASHTOON_BASE_URL/owner"
echo "Owner login:      aarzuehtishaam@gmail.com with the starter password (see README). Change it in the dashboard under Password."
if [ -z "$(value ANTHROPIC_API_KEY)" ]; then
  echo "AI assistant:     off. Add ANTHROPIC_API_KEY to the .env file and run this script again to switch it on."
else
  echo "AI assistant:     on"
fi
