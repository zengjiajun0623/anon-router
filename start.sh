#!/bin/sh
# Launch the deposit watcher (background) + the router (foreground).
# The watcher credits accounts from on-chain CreditVault deposits; the router
# serves the OpenAI-compatible + ecash API. They share the mounted /data volume.
set -e
mkdir -p /data

PORT="${PORT:-8402}"

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
