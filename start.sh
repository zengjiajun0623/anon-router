#!/bin/sh
# Launch the deposit watcher (background) + the router (foreground).
# The watcher credits accounts from on-chain CreditVault deposits; the router
# serves the OpenAI-compatible + ecash API. They share the mounted /data volume.
set -e
mkdir -p /data

PORT="${PORT:-8402}"

# Tor v3 onion service (non-fatal: a tor failure never stops the router).
# The hidden-service key lives on the /data volume so the .onion address is
# stable across redeploys. Over the onion the router sees only a Tor circuit,
# never a client IP, and there is no exit node in the path.
if [ "${TOR_ONION:-1}" != "0" ] && command -v tor >/dev/null 2>&1; then
  mkdir -p /data/tor_hs && chmod 700 /data/tor_hs
  printf 'SocksPort 0\nDataDirectory /tmp/tor\nHiddenServiceDir /data/tor_hs\nHiddenServiceVersion 3\nHiddenServicePort 80 127.0.0.1:%s\n' "$PORT" > /tmp/torrc
  echo "start: launching tor onion service"
  tor -f /tmp/torrc > /data/tor.log 2>&1 &
  ( sleep 15; [ -f /data/tor_hs/hostname ] && echo "start: ONION ADDRESS = $(cat /data/tor_hs/hostname)" ) &
fi

if [ -n "$VAULT_ADDRESS" ] && [ -n "$CHAIN_RPC" ] && [ -n "$CREDIT_SECRET" ]; then
  echo "start: launching deposit watcher (vault=$VAULT_ADDRESS)"
  RPC="$CHAIN_RPC" VAULT="$VAULT_ADDRESS" ROUTER="http://127.0.0.1:$PORT" \
    CREDIT_SECRET="$CREDIT_SECRET" CREDITS_PER_ETH="${CREDITS_PER_ETH:-10000000}" \
    CONFIRMATIONS="${CONFIRMATIONS:-3}" WATCHER_CURSOR="${WATCHER_CURSOR:-/data/.watcher_cursor}" \
    python watcher.py &
else
  echo "start: no vault/rpc/credit-secret configured — watcher disabled (ecash claim + free lane only)"
fi

exec uvicorn server:app --host 0.0.0.0 --port "$PORT" --no-access-log --no-server-header
