# Mainnet launch runbook

Operator-facing checklist for taking anon-router from the testnet demo to a
mainnet deployment that accepts real funds. Follow it top to bottom. Do not
skip the gates in section 0. They are blocking, not advisory.

Audience: the human operator. An AI agent must never hold the deployer key,
run the deploy, or approve the counsel/audit gates. Agents may prepare drafts
and read-only checks; every irreversible or real-funds step is done by you.

Scope: this ships in two phases.

- **Phase 1 · Simple lane (`CreditVault`, custodial).** Deposit ETH/USDC → the
  watcher credits a bearer API key. Contracts are small and audited-simple.
  This is what "mainnet launch" means first.
- **Phase 2 · Channel lane (`ConfettiChannels`, trust-minimized).** Deposits
  leave custody and live on-chain. **Blocked on the real SP1 Groth16 verifier**
  (M4b-real, still Docker-gated / in progress). Do not deploy `ConfettiChannels`
  to mainnet with `MockVerifier`, and keep `CHANNEL_LANE_ENABLED=0` until the
  real verifier ships and passes its own audit. Section 6 covers it; it is not
  part of the first launch.

Repo paths in this doc are relative to `anon-router/`. Foundry lives at
`$HOME/.foundry/bin` (`export PATH="$HOME/.foundry/bin:$PATH"`).

---

## 0. Blocking pre-launch gates

Nothing in section 3 onward proceeds until both of these are signed off in
writing. Record who signed and when.

### 0a. COUNSEL sign-off (BLOCKING)

This service accepts real funds for **payment-unlinkable** inference. That is
exactly the fact pattern that draws money-transmission, sanctions, and AML
scrutiny. Obtain written counsel review, per served jurisdiction, covering:

- [ ] Money transmission / MSB licensing and registration
- [ ] Sanctions / OFAC screening obligations against a payer set you cannot see
- [ ] AML / KYC obligations and whether the anonymous design is defensible or
      requires controls (deposit limits, source-of-funds, blocking)
- [ ] Consumer protection, refund policy, and disclosures
- [ ] Privacy law posture (what you retain, `PRIVACY.md` accuracy)
- [ ] Tax treatment of credit sales and unredeemed float
- [ ] Terms of service and acceptable-use, including prohibited jurisdictions
- [ ] Counsel has explicitly reviewed that the payment rail is designed to be
      unlinkable and has signed off on operating it anyway, or specified the
      controls required first

Do not accept a single real deposit before counsel sign-off is on file.

### 0b. Security audit sign-off (BLOCKING)

- [ ] Independent smart-contract audit of `CreditVault` (ERC-20 `transferFrom`
      behavior incl. fee-on-transfer / non-standard tokens, `owner` / sweep
      authority, event integrity, reentrancy on `sweep`)
- [ ] Independent review of the watcher (`watcher.py`): event finality / reorg
      handling, idempotency, cursor durability, credit accounting
- [ ] Review of the router credit path (`/account/credit` auth, dedup per
      `txhash:logIndex`, balance arithmetic, daily-cap breaker)
- [ ] Key-management review (sections 1 and 4 below)
- [ ] Incident-response runbook (section 7) rehearsed at least once on testnet
- [ ] Sign-off recorded from: **legal, security, finance, operations**

Note the two known caveats the audit must accept or require fixed:

- The watcher does **not reverse a credit** if a reorg deeper than
  `CONFIRMATIONS` orphans an already-credited deposit (`watcher.py` header
  comment). Mainnet mitigation: raise `CONFIRMATIONS` (section 3) and/or add
  orphan reconciliation before large deposits are allowed.
- The router's off-chain channel state is in-memory (M4a); durable persistence
  is required before the channel lane, not before the simple lane.

---

## 1. Key and secret inventory

Three distinct secrets. They are not interchangeable, and only one of them
touches the chain.

| Secret | Who holds it | Where it lives | If lost / leaked |
|---|---|---|---|
| **Deployer / owner key** | Operator only, offline | Hardware wallet or air-gapped signer. **Never** in server env, never given to an agent, never in the repo. | Leaked → attacker can `sweep` the vault. Lost → cannot sweep deposits (funds stuck in `CreditVault` until key recovered). |
| **`CREDIT_SECRET`** | Router + watcher process | Platform env var (both processes) | Leaked → attacker can mint arbitrary credits via `/account/credit`. Rotate immediately (section 7). |
| **`mint_master.hex`** | Router process | Durable volume `/data`, `chmod 600` | Lost → all outstanding ecash tokens become unspendable. Leaked → attacker can forge ecash. Never rotate while tokens are outstanding. |

Critical property to preserve: **the router and watcher never sign
transactions.** `server.py` and `watcher.py` only read the chain (poll events,
read balances). The only signing helper, `onchain.py`, is a client used by the
CLI/demo and takes the key as a call argument. So no signing key ever needs to
sit in the server environment. Keep it that way.

### 1a. Generate `CREDIT_SECRET`

```sh
openssl rand -hex 32
```

Set it identically in the router and watcher environment. `start.sh` passes it
from the container env to the watcher automatically.

### 1b. Provision `mint_master.hex`

The router auto-generates a 32-byte master at `MINT_MASTER_PATH` on first boot
(`server.py:_master`, `chmod 600`) if the file is absent. The mint's
per-denomination signing keys are HMAC-derived from it, so it must be stable and
durable **before the first ecash token is issued**. Two options:

- **Let the router generate it**, then immediately back it up (section 5) before
  anyone claims ecash.
- **Pre-generate and place it** on the `/data` volume:

  ```sh
  install -m 600 /dev/stdin /data/anon-router/mint_master.hex <<EOF
  $(openssl rand -hex 32)
  EOF
  ```

Confirm the path matches `MINT_MASTER_PATH` in the deployment env (the
Dockerfile default is `/data/mint_master.hex`).

### 1c. Deployer key custody

- [ ] Use a hardware wallet (Ledger/Trezor) or, preferred for the owner role, a
      multisig (e.g. Safe) as `CreditVault.owner`. `owner = msg.sender` at
      deploy, so deploy **from** the address you want as owner, or transfer if
      you add ownership transfer later (the current contract has no transfer
      function, so pick the owner before deploying).
- [ ] Fund the deployer with only enough ETH for gas. It is not the float
      account.
- [ ] Record the deployer/owner address in your ops log.
- [ ] Never paste this key into `.env`, a chat, an agent prompt, or CI.

---

## 2. Contract addresses reference

| Item | Mainnet value |
|---|---|
| USDC (Ethereum mainnet, 6 decimals) | `0xA0b86991C6218b36c1d19D4a2e9Eb0cE3606eB48` |
| `depositUSDC(bytes32,uint256)` selector | `0x7c34c355` |
| `deposit(bytes32)` selector | computed by `/config` and `/account/new` |

Independently verify the USDC address (Etherscan, Circle docs) before use.
Circle rotates testnet addresses; the Sepolia staging USDC has been
`0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238` but re-check at
developers.circle.com before any staging run.

---

## 3. Phase 1 deployment: `CreditVault`

Order: deploy the contract, verify it, configure env, deploy the service, run a
canary, then open the doors.

### 3a. Deploy `CreditVault`

There is no forge script for `CreditVault` (the `script/Deploy.s.sol` script is
for `ConfettiChannels`). Deploy it directly with `forge create`. The constructor
takes the USDC address; pass the zero address for an ETH-only vault.

```sh
export PATH="$HOME/.foundry/bin:$PATH"
cd contracts

# USDC-enabled vault (recommended):
forge create src/CreditVault.sol:CreditVault \
  --rpc-url "$CHAIN_RPC" \
  --ledger \                         # or --private-key from your offline signer
  --constructor-args 0xA0b86991C6218b36c1d19D4a2e9Eb0cE3606eB48 \
  --broadcast

# ETH-only vault (USDC disabled; depositUSDC reverts):
#   --constructor-args 0x0000000000000000000000000000000000000000
```

Record the deployed address as `VAULT_ADDRESS`.

### 3b. Verify the contract

- [ ] **Etherscan source verification:**

  ```sh
  forge verify-contract "$VAULT_ADDRESS" src/CreditVault.sol:CreditVault \
    --chain mainnet \
    --constructor-args $(cast abi-encode "constructor(address)" \
      0xA0b86991C6218b36c1d19D4a2e9Eb0cE3606eB48) \
    --etherscan-api-key "$ETHERSCAN_API_KEY"
  ```

- [ ] **On-chain sanity, read-only:**

  ```sh
  cast call "$VAULT_ADDRESS" "owner()(address)"  --rpc-url "$CHAIN_RPC"   # your owner addr
  cast call "$VAULT_ADDRESS" "usdc()(address)"   --rpc-url "$CHAIN_RPC"   # matches USDC
  ```

- [ ] Confirm `owner()` is the multisig/hardware address you intended, not a hot
      key.
- [ ] Confirm the bytecode matches the audited commit.

### 3c. Mainnet environment configuration

Set these in the platform (Railway) service env. `.env.example` documents every
key. Mainnet-critical values and the reasons they differ from the demo:

```sh
# --- safety switches ---
DEV_FAUCET=0                 # NEVER 1 in production; free credits if on
CHANNEL_LANE_ENABLED=0       # stays 0 until Phase 2 (section 6)
DAILY_USD_CAP=25             # >0 arms the upstream-spend breaker; size to budget

# --- chain / deposit watcher ---
CHAIN_RPC=https://YOUR_MAINNET_RPC           # real, redundant, authenticated RPC
VAULT_ADDRESS=0xYOUR_CREDIT_VAULT            # from 3a
USDC_ADDRESS=0xA0b86991C6218b36c1d19D4a2e9Eb0cE3606eB48   # enables USDC crediting
CONFIRMATIONS=12             # raise from the demo's 3; reorg safety (see below)
CREDITS_PER_ETH=10000000     # 1 ETH -> 10,000,000 credits at CREDIT_USD=0.0001
CREDITS_PER_USDC=10000       # 1 USDC (1e6 base units) -> 10,000 credits

# --- pricing / auth ---
CREDIT_USD=0.0001            # 1 credit = $0.0001; keep consistent with rates above
MARKUP=1.0                   # upstream-cost multiplier when charging credits
CREDIT_SECRET=<openssl rand -hex 32>         # watcher<->router auth (section 1a)
OPENROUTER_API_KEY=<wholesale key>           # the float account that backs credits
UPSTREAM=https://openrouter.ai/api/v1

# --- public surface ---
PUBLIC_BASE_URL=https://your.domain          # https; returned to new accounts
ACCOUNT_RATE_PER_MIN=120                      # anonymous account-creation cap

# --- persistence (must be on the durable volume) ---
STATE_DB_PATH=/data/state.db
MINT_MASTER_PATH=/data/mint_master.hex        # match section 1b
WATCHER_CURSOR=/data/.watcher_cursor
```

Notes:

- **`CONFIRMATIONS`.** The watcher only scans blocks at least this deep and only
  advances its cursor once every credit in a range is durably handled
  (idempotent per `txhash:logIndex`). Set higher than the testnet default of 3.
  12 blocks is a reasonable floor; consider higher for large-value deposits
  because an orphaned already-credited deposit is **not** auto-reversed
  (section 0b caveat). Sizing this is a risk decision, not a default.
- **`USDC_ADDRESS` unset** leaves the watcher ETH-only. Setting it makes the
  watcher also scan `DepositedToken` filtered to that token.
- **`CREDITS_PER_USDC=10000`** with `CREDIT_USD=0.0001` means 1 USDC buys 10,000
  credits = $1.00 of credits. Keep the three (`CREDIT_USD`, `CREDITS_PER_ETH`,
  `CREDITS_PER_USDC`) mutually consistent or you sell credits at the wrong
  price.
- **Do not reuse a testnet `.watcher_cursor` or `state.db`.** Start from a clean
  volume so the watcher scans from mainnet chain head, not a stale block.

### 3d. Deploy the service

- [ ] Attach a **durable volume** mounted at `/data` (Railway volume). Verify it
      persists across redeploys.
- [ ] Deploy the Docker image (`Dockerfile` → `start.sh`). `start.sh` launches
      the watcher in the background and the router in the foreground when
      `VAULT_ADDRESS`, `CHAIN_RPC`, and `CREDIT_SECRET` are all set; otherwise it
      logs that the watcher is disabled.
- [ ] Confirm the startup log line prints the safe config:
      `anon-router SAFE config: faucet=off channel_lane=off daily_cap_usd=25`.
- [ ] Confirm the watcher log line:
      `watcher: vault=0x... from block N, ... 12 confirmations`.

### 3e. Frontend deposit call (for reference)

The site submits `depositUSDC(bytes32,uint256)` after an exact-amount `approve`.
The selector is `0x7c34c355`. This snippet is documentation for the `web/` team;
do not edit `web/` from this runbook.

```js
import { Contract, Interface, parseUnits } from "ethers";
const USDC  = "0xA0b86991C6218b36c1d19D4a2e9Eb0cE3606eB48";
const VAULT = "0xYOUR_CREDIT_VAULT";
const amount = parseUnits("10", 6);            // 10 USDC, 6 decimals
const keyHash = "0xYOUR_32_BYTE_KEY_HASH";

const usdc = new Contract(USDC, [
  "function approve(address spender,uint256 amount) returns (bool)",
], signer);
await (await usdc.approve(VAULT, amount)).wait();

const vault = new Interface(["function depositUSDC(bytes32 keyHash,uint256 amount)"]);
const data = vault.encodeFunctionData("depositUSDC", [keyHash, amount]);
await (await signer.sendTransaction({ to: VAULT, data })).wait();
```

---

## 4. Canary launch

Before publishing the URL or selling any credit:

- [ ] Create an account (`POST /account/new`), note its `key_hash`.
- [ ] Deposit a **small** real amount (e.g. 1 USDC) to `VAULT_ADDRESS`
      referencing that `key_hash`.
- [ ] Watch the watcher log credit the account after `CONFIRMATIONS` blocks.
- [ ] Confirm `GET /account/status` shows the expected balance and USD.
- [ ] Run one real `/v1/chat/completions` request; confirm the upstream is
      charged and `spend_ledger` for today increments.
- [ ] Deliberately exceed `DAILY_USD_CAP` in a test window and confirm requests
      get `402 daily budget reached`.
- [ ] Sweep the canary deposit back out with the owner key to confirm sweep
      authority works:

      ```sh
      cast send "$VAULT_ADDRESS" "sweepToken(address,address)" \
        0xA0b86991C6218b36c1d19D4a2e9Eb0cE3606eB48 "$TREASURY" \
        --ledger --rpc-url "$CHAIN_RPC"
      ```

- [ ] Restart the service and confirm the watcher resumes from `.watcher_cursor`
      (no double-credit, no skipped block) and balances survive (durable
      volume).

Only after every box is checked: publish the URL and enable credit sales.

---

## 5. Persistence and backup

Everything that must survive a redeploy lives on `/data`:

- `state.db` (+ `state.db-wal`, `state.db-shm`): accounts, balances, spent
  tokens, receipts, vouchers, `seen_deposits`, claims, `spend_ledger`.
- `mint_master.hex`: the ecash signing master (section 1b).
- `.watcher_cursor`: the deposit scan position.

Backup rules:

- [ ] **Consistent hot backup of SQLite** (never copy `state.db` alone while the
      router runs, the WAL holds uncommitted state):

      ```sh
      sqlite3 /data/state.db ".backup '/data/backups/state-$(date -u +%FT%TZ).db'"
      ```

- [ ] Back up `mint_master.hex` **once, securely, before the first ecash token
      is issued**, and store it offline (encrypted). It rarely changes; losing it
      strands all outstanding ecash.
- [ ] Cadence: automated `state.db` backup at least hourly, retained off-box
      (encrypted). Deposits and balances are money; treat backups accordingly.
- [ ] Test a restore into a staging service before you rely on it.
- [ ] Confirm backups exclude nothing load-bearing and include no secrets in
      plaintext at rest.

---

## 6. Persistence and monitoring

### 6a. Health and liveness

- `GET /healthz` returns `{status, faucet, channel_lane, daily_cap_usd}`.
  Railway healthcheck is wired to it (`railway.json`, 30s timeout, restart on
  failure, max 5 retries).
- [ ] Alert if `/healthz` is unreachable or if `faucet` is ever `true` in
      production.

### 6b. Deposit watcher liveness

The watcher is event-driven and silent when idle. Monitor that it is **making
progress**, not just alive:

- [ ] Alert if `.watcher_cursor` stops advancing while new blocks are produced
      (watcher stalled or RPC down). A stalled watcher means deposits are not
      being credited.
- [ ] Alert on repeated `credit failed ... will retry` lines (router/DB down or
      `CREDIT_SECRET` mismatch).
- [ ] Alert on `watcher error:` lines (RPC failures); ensure RPC redundancy.

### 6c. Spend ledger and the budget breaker

- `spend_ledger(day, usd)` in `state.db` tracks USD sent to the upstream per UTC
  day. `DAILY_USD_CAP` reserves against it per request and returns `402` when a
  day would exceed the cap.

  ```sh
  sqlite3 /data/state.db "SELECT day, usd FROM spend_ledger ORDER BY day DESC LIMIT 7;"
  ```

- [ ] Alert when today's `usd` crosses a warning fraction of `DAILY_USD_CAP`
      (e.g. 80%) so a legitimate spike is not silently throttled.
- [ ] Alert on any day the cap is hit (possible abuse or underpricing).

### 6d. Financial reconciliation (do this daily)

- [ ] Sum of issued account balances (`SELECT SUM(balance) FROM accounts`)
      priced at `CREDIT_USD`, plus outstanding ecash, is your **liability**.
- [ ] On-chain vault holdings (ETH + USDC `balanceOf(VAULT_ADDRESS)`) plus swept
      treasury is the **backing for deposits**.
- [ ] The OpenRouter float balance is the **backing for spend**.
- [ ] Reconcile liability ≤ backing every day. Investigate any drift before it
      compounds.

### 6e. Alert routing

- [ ] Wire the alerts above to a channel the operator actually watches (pager /
      Messages / email). Failures only, no status spam.

---

## 7. Rollback and incident plan

There is no on-chain pause in `CreditVault`; control is operational. Know these
levers cold before launch.

### Stop the bleeding

- **Halt new spend (upstream):** set `DAILY_USD_CAP` low (or rotate the
  `OPENROUTER_API_KEY` to cut off the upstream entirely) and redeploy. Note
  `DAILY_USD_CAP<=0` **disables** the breaker, so do not set it to 0 to "stop"
  spend; that opens it.
- **Halt new crediting:** stop the service, or rotate `CREDIT_SECRET` so the
  watcher can no longer post credits. Deposits still land on-chain and are
  credited later once you resume (the cursor and idempotency make replay safe).
- **Full stop:** take the router + watcher offline. In-flight deposits are not
  lost; they credit when the watcher restarts from `.watcher_cursor`.

### Recover funds

- **Sweep the vault** to treasury with the owner key:

  ```sh
  cast send "$VAULT_ADDRESS" "sweep(address,uint256)" "$TREASURY" "$WEI" \
    --ledger --rpc-url "$CHAIN_RPC"                      # ETH
  cast send "$VAULT_ADDRESS" "sweepToken(address,address)" \
    "$USDC" "$TREASURY" --ledger --rpc-url "$CHAIN_RPC"  # all USDC
  ```

### Specific incidents

- **`CREDIT_SECRET` leaked** → generate a new one (section 1a), set it on router
  + watcher, redeploy. Audit `seen_deposits` / balances for credits that do not
  match on-chain deposits; refund/adjust as needed.
- **`OPENROUTER_API_KEY` leaked** → rotate at OpenRouter, update env, redeploy.
- **Reorg deeper than `CONFIRMATIONS`** → the orphaned deposit's credit is not
  auto-reversed. Identify affected `txhash:logIndex` in `seen_deposits`,
  manually reconcile the account balance, and raise `CONFIRMATIONS`.
- **Watcher double-credit suspected** → credits are idempotent per
  `txhash:logIndex` and per `seen_deposits`; verify the row exists rather than
  re-crediting. Never manually re-run credit for a deposit already in
  `seen_deposits`.
- **`state.db` corruption** → stop the service, restore the latest consistent
  `.backup` (section 5), replay is safe because crediting is idempotent and the
  watcher resumes from the cursor. If the cursor is ahead of the restored DB,
  the watcher re-scans and idempotency absorbs the overlap.
- **`mint_master.hex` lost** → outstanding ecash is unrecoverable. Do not
  generate a new master silently; that invalidates every issued token.
  Communicate, then decide on reissue/refund. This is why the pre-first-token
  backup in section 1b is mandatory.

### Contract rollback

`CreditVault` is not upgradeable. "Rollback" means: stop the watcher, sweep
funds, deploy a fresh `CreditVault`, point `VAULT_ADDRESS` at the new one, and
migrate. Practice this path on Sepolia before you need it.

- [ ] Rehearse stop → sweep → restore → resume once on testnet and record the
      wall-clock time it takes.

---

## 8. Phase 2: `ConfettiChannels` (later, gated)

Do not start this until Phase 1 is stable **and** all of the following hold.
Deploying the channel escrow with the mock verifier on mainnet would let anyone
drain it.

Gates before any mainnet `ConfettiChannels`:

- [ ] The **real SP1 Groth16 verifier** (M4b-real) is complete, not
      `MockVerifier`. Until then, `VERIFIER` is unset and the deploy script
      *refuses* to run without `ALLOW_MOCK=1` (see `script/Deploy.s.sol`). Never
      set `ALLOW_MOCK=1` on mainnet.
- [ ] The verifier and `ConfettiChannels` pass an independent audit (the
      existing Fable/Codex/Kimi reviews + Lean proof are on the settlement core,
      see `VERIFICATION.md`; a real-verifier audit is still required).
- [ ] The router's off-chain channel state (dedup/inbox/XMSS) has **durable
      persistence**. It is in-memory today (`server.py` M4a note), and a restart
      loses the recipient's signing/challenge state, which is unacceptable when
      real deposits are escrowed.
- [ ] A funded, monitored **challenge liveness** process exists: Bob's fraud
      challenge is operator-funded and event-driven; a missed challenge window
      lets a stale close settle. Add a liveness SLO and alerting.

Deploy (only when the gates pass), from your offline signer:

```sh
export PATH="$HOME/.foundry/bin:$PATH"
cd contracts
VERIFIER=0xREAL_VERIFIER \
  forge script script/Deploy.s.sol \
  --rpc-url "$CHAIN_RPC" --ledger --broadcast
# TAU/T_ABS/T_REQ/T_ROOT default to Spec §1 (7d/90d/7d/1d); override via env.
```

Then set `CONFETTI_ADDRESS`, verify on Etherscan, and only then flip
`CHANNEL_LANE_ENABLED=1`. Re-run a canary (section 4 style) on the channel lane
before opening it.

---

## 9. Final go/no-go

- [ ] Section 0 gates signed (counsel + audit), on file.
- [ ] Deployer key in hardware/multisig custody; no signing key in server env.
- [ ] `CreditVault` deployed, Etherscan-verified, `owner`/`usdc` confirmed.
- [ ] Mainnet env set: `DEV_FAUCET=0`, `CHANNEL_LANE_ENABLED=0`,
      `DAILY_USD_CAP>0`, `CONFIRMATIONS` raised, addresses + rates consistent,
      `PUBLIC_BASE_URL` on https.
- [ ] Durable `/data` volume; `mint_master.hex` backed up before first token;
      hourly `state.db` backups tested by restore.
- [ ] `/healthz`, watcher-progress, spend-cap, and reconciliation alerts live.
- [ ] Canary passed end to end, including a real sweep and a restart.
- [ ] Incident levers rehearsed once on testnet.

When every box is checked, launch.
