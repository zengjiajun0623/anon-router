#!/usr/bin/env bash
# End-to-end verification loop: brings up anvil + contracts + router + watcher,
# runs e2e_verify.py, reports, and (unless KEEP=1) tears everything down.
set -uo pipefail
export PATH="$HOME/.foundry/bin:$PATH"
cd "$(dirname "$0")"
ROOT="$(pwd)"
RPC=http://127.0.0.1:8545
DEPLOYER=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
CREDIT_SECRET=devsecret123
export CREDITS_PER_ETH=10000000

echo "== cleanup =="
pkill -f "uvicorn server:app" 2>/dev/null; pkill -f "watcher.py" 2>/dev/null
# anvil restarts as a fresh chain below, so stale watcher state (cursor/ledger/
# halt) would be reconciled against a different chain and falsely trip the
# deep-reorg halt. Clear it for a clean local run.
rm -f "$ROOT"/.watcher_cursor "$ROOT"/.watcher_credited "$ROOT"/.watcher_halt "$ROOT"/.watcher_heartbeat
if ! cast block-number --rpc-url $RPC >/dev/null 2>&1; then
  echo "== start anvil =="; anvil --silent --block-time 1 > "$ROOT/contracts/anvil.log" 2>&1 &
  sleep 3
fi

echo "== deploy contracts =="
cd "$ROOT/contracts"
dep() { forge create "$1" --rpc-url $RPC --private-key $DEPLOYER --broadcast ${2:+--constructor-args $2} 2>&1 | grep "Deployed to" | awk '{print $3}'; }
MOCK=$(dep src/MockVerifier.sol:MockVerifier)
CONFETTI=$(forge create src/ConfettiChannels.sol:ConfettiChannels --rpc-url $RPC --private-key $DEPLOYER --broadcast --constructor-args $MOCK 60 300 120 86400 2>&1 | grep "Deployed to" | awk '{print $3}')
VAULT=$(dep src/CreditVault.sol:CreditVault 0x0000000000000000000000000000000000000000)
echo "  MockVerifier=$MOCK"; echo "  ConfettiChannels=$CONFETTI"; echo "  CreditVault=$VAULT"
cd "$ROOT"

# fund alice/bob (public anvil dev accounts)
cast rpc anvil_setBalance 0x70997970C51812dc3A010C7d01b50e0d17dc79C8 0x21e19e0c9bab2400000 --rpc-url $RPC >/dev/null
cast rpc anvil_setBalance 0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC 0x21e19e0c9bab2400000 --rpc-url $RPC >/dev/null

echo "== start router =="
VAULT_ADDRESS="$VAULT" CONFETTI_ADDRESS="$CONFETTI" CHAIN_RPC="$RPC" \
  CREDIT_SECRET="$CREDIT_SECRET" PUBLIC_BASE_URL="http://127.0.0.1:8402" \
  .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8402 --no-access-log --no-server-header > "$ROOT/server.log" 2>&1 &
# wait for readiness (XMSS keygen takes a few seconds)
for i in $(seq 1 30); do curl -s http://127.0.0.1:8402/mint/keys >/dev/null 2>&1 && break; sleep 1; done

echo "== start watcher =="
RPC=$RPC VAULT="$VAULT" ROUTER=http://127.0.0.1:8402 CREDIT_SECRET="$CREDIT_SECRET" \
  .venv/bin/python watcher.py > "$ROOT/watcher.log" 2>&1 &
sleep 2

echo "== run e2e verification =="
VAULT="$VAULT" CONFETTI="$CONFETTI" ROUTER=http://127.0.0.1:8402 RPC=$RPC \
  .venv/bin/python e2e_verify.py
RC=$?

if [ "${KEEP:-0}" != "1" ]; then
  echo "== teardown =="
  pkill -f "uvicorn server:app" 2>/dev/null; pkill -f "watcher.py" 2>/dev/null
fi
echo "exit $RC"; exit $RC
