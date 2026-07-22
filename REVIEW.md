# anon-router — Product & Code Review

_Grounded review of the codebase as of branch `fix/watcher-no-account-race` (HEAD `fd6b8c4` + the watcher fix). Every claim cites a file, function, route, table, or line. Nothing is inferred from page behavior._

---

# Executive Summary

anon-router is a **payer-anonymous, OpenAI/Anthropic-compatible inference proxy**. Its engineering center of gravity — the ecash money path and the confetti zk-channel protocol — is genuinely strong: cost-bounding before spend, atomic spend+receipt, in-band Cashu-NUT-08 change, crash recovery, and a machine-checked settlement core. That part is production-grade in intent and well tested (`tests/e2e_money_safety.py`, `tests/e2e_unlinkability.py`, `tests/test_confetti.py`).

Everything **around** that core is still a demo. The gaps the user listed are real and confirmed in code:

- **The browser never persists the account/recovery key.** Only three localStorage keys exist (`app.js:17-19`), and the `account` object is not one of them. Ecash *tokens* survive a refresh; the account, chat history, and deposit context do not.
- **There is no API-key management, usage, activity, logs, or chat-session subsystem — anywhere.** The DB schema has seven tables (`server.py:195-250`), none of which store per-key metadata, request records, usage, or messages. This is partly a *deliberate privacy decision* and partly *unbuilt product*.
- **Wallets and backups are plaintext bearer money** (`ecash.js`, `app.js:467-483`, `wallet.py:109-114`). The only protection is file mode `0600` on the CLI side; the browser has none.
- **`store.py` (the Postgres/HA layer) is dead code** — never imported, and its schema *reintroduces* the plaintext `api_key` column `server.py` explicitly migrated away from.

**The central tension this review resolves:** a privacy product *cannot* implement OpenRouter's server-side per-key usage/activity/logs without destroying its own unlinkability guarantee. The recommended architecture therefore puts usage/logs/chat/key-management **client-side** (browser IndexedDB / CLI files), keeps the server minimal, and treats "OpenRouter parity" as a *local dashboard over the user's own wallet*, not a server account system.

**Verdict:** the money engine is ready; the product shell is not. Priorities in order: (P0) don't lose the user's wallet/identity on refresh and make backup/restore trustworthy; (P1) client-side usage/activity/chat history; (P2) local key/limits management and voucher/deposit UX; (P3) the non-custodial channel lane and shielded funding.

---

# Current Architecture

## Runtime components
| Component | File | Role |
|---|---|---|
| Router (hosted) | `server.py` (1618 ln) | FastAPI: ecash mint, `/account/*`, `/mint/*`, `/v1/chat/completions`, `/v1/models`, `/privacy`, `/healthz`, serves `web/` |
| Deposit watcher | `watcher.py` | Polls `CreditVault` `Deposited`/`DepositedToken` events → `POST /account/credit`; reorg-safe |
| Local ecash proxy | `serve_ecash.py` | stdlib HTTP proxy on `:8788`, OpenAI **and** Anthropic compatible; pays each request with ecash |
| Local channel proxy | `serve.py` | FastAPI proxy backed by one confetti channel (advanced/`--channel` lane) |
| Anthropic translation | `anthropic_proxy.py` | Pure `/v1/messages` ↔ chat-completions translation lib |
| CLI | `cli.py` → `wallet.py` | `account/deposit/claim/chat/redeem/channel/serve/setup` |
| Operator tools | `admin.py` (vouchers), `provider_settle.py` (custodial payout) | Direct DB / on-chain |
| Browser app | `web/index.html`, `web/app.js`, `web/ecash.js` | 3-step demo: create wallet → deposit → chat |
| Contracts | `contracts/src/CreditVault.sol`, `ConfettiChannels.sol` | Deposit front door + (roadmap) escrow |
| Dead/foundation | `store.py` | Async Postgres/SQLite store — **not imported anywhere** |

## Payment lanes (from `server.py` chat routing)
1. **Free** (`local/*`) — no payment, streams (`server.py:1271-1292`).
2. **Ecash** (`X-Cash`) — the live paid lane; blind-signed tokens, cost-bounded, in-band change (`server.py:1366-1617`).
3. **Channel** (`X-Channel-Payment`/`_channel_payment`) — confetti zk, gated by `CHANNEL_LANE_ENABLED` (default off, `server.py:1300-1364`).
4. **Bearer-key inference — deliberately removed** (`server.py:1294-1299`): paying inference with the account key would re-link every request to the funding identity. This is the single most important design decision in the repo and it shapes everything below.

## Data model as built (SQLite, `server.py:195-250`)
`spent(secret)` · `receipts(id,prepaid,cost,state,change_sigs,ts,res_day,res_usd,change_key)` · `vouchers(code,credits,state,sigs,redeem_key)` · `accounts(key_hash,balance)` · `seen_deposits(txhash)` · `claims(idem_key,response,ts)` · `spend_ledger(day,usd)`.

Note what is **absent by design**: no `api_key` column (migrated out at `server.py:227-232`, asserted by `tests/e2e_unlinkability.py:247`), no user, no per-request row, no usage, no model, no timestamp-of-use, no chat. `spend_ledger` is a **global daily USD aggregate** for the cap — not per-key, not per-model.

---

# Existing Features (what actually works)

**Money core (well-built, tested):**
- Anonymous account mint: `POST /account/new` → `sk-anon-…`, stores `key_hash` only (`server.py:927-966`).
- On-chain deposit → credit: `CreditVault.deposit(bytes32)` → watcher → `POST /account/credit`, idempotent per `txhash:logIndex` (`server.py:988-1016`, `watcher.py`), with reorg hardening (CONFIRMATIONS, deep-reorg halt) and the just-added `no_such_account` retry fix (`watcher.py`).
- Claim balance → unlinkable ecash: `POST /mint/claim`, mandatory `Idempotency-Key`, atomic debit (`server.py:1048-1110`).
- Ecash spend with cost-bounding + in-band change: `POST /v1/chat/completions` (`server.py:1366-1617`), verified by `tests/e2e_money_safety.py`.
- Crash recovery: stale-receipt sweep → full refund; `X-Cash-Recover` replay (`server.py:684-732`, `1232-1248`).
- Vouchers: `admin.py issue/list` + `POST /mint/redeem` (body-only, oracle-free, idempotent) (`server.py:1156-1205`).
- USDC deposits: `depositUSDC` + `DepositedToken` watcher path (`CreditVault.sol`, `watcher.py`).
- Privacy plumbing: strict upstream field allowlist (`server.py:124-141`, tested `tests/e2e_privacy.py`), no-store/CSP headers (`server.py:325-339`), no IP logging.
- Confetti channel protocol (off-chain), machine-checked core (`confetti/`, `tests/test_confetti.py` — 18 tests).

**Local dev experience:**
- Browser 3-step demo (`web/index.html`) with ecash wallet in localStorage, backup/import to file, live model dropdown from catalog (`app.js:565-584`), Tor-onion footer when published.
- CLI + local proxies for OpenAI and Anthropic SDKs; `anon-router serve` (`cli.py`, `serve_ecash.py`, `anthropic_proxy.py`), tested by `tests/e2e_proxy.py`.

---

# Missing or Incomplete Features

## (2) Partially complete
- **Wallet persistence:** ecash tokens persist (`app.js:17-21`), but the **account is never saved** (only `WALLET_KEY`, `PENDING_CLAIM_KEY`, `PENDING_CHANGE_KEY` exist). Half-built.
- **Backup/restore:** export/import works (`app.js:467-515`) but is **plaintext, unencrypted, no passphrase**, and there is **no test** for it.
- **Channel lane:** full protocol + contract exist, but `CHANNEL_LANE_ENABLED` defaults off, prover needs a Rust binary, and off-chain channel state is in-memory (`server.py:155-159`; `MAINNET.md:73`). Roadmap, not usable.
- **Tor onion:** code exists (`start.sh:14-22`) but off by default; `PRIVACY.md:12`/`README.md:67` still claim "Onion live" — **stale overclaim** (commit `fa2598d` parked it).
- **Postgres/HA store:** `store.py` written but **unwired** and schema-inconsistent with the live server.

## (3) UI exists but backend does not support it
- **"Recovery key" label** (`index.html:106`, `app.js:64`) implies server-side recovery. There is **none** — the api_key only re-claims an *unclaimed account balance*; already-claimed ecash is only in localStorage/the backup file (`app.js:455-456`). Losing the browser with only the "recovery key" saved loses the money. Misleading mental model.
- **Model dropdown** offers models (`app.js:568-572`) with no per-model availability/pricing/context surfaced; unavailable-model handling is a raw upstream error.
- **Balance "Claiming your deposit…" states** (`app.js:33,50-54`) assume an account is present; after refresh there is no account, so the affordance is dead until re-mint/import.

## (4) Backend exists but UI does not expose it
- **Voucher redemption** (`POST /mint/redeem`) — no browser UI; only CLI/`ecash.js:257`.
- **Free local lane** (`local/*`) — routable but not offered in the dropdown.
- **USDC deposits** — supported server/contract-side, no browser path (ETH only in `app.js:224-243`).
- **`/config`, `/quickstart`, `/privacy`** — `/config` (`server.py:1019-1045`) is unused by `app.js` (it hardcodes selectors/params instead); `/privacy` is only used for the onion footer.
- **Channel lane** — entire `/channel/*` surface unexposed in the browser.

## (5) State lost on page refresh
| State | Survives refresh? | Where it lives |
|---|---|---|
| Ecash tokens (money) | ✅ yes | `localStorage['anon-router-ecash-v1']` (`app.js:17-21`) |
| Pending claim/change (crash recovery) | ✅ yes | `localStorage` (`app.js:18-19`) |
| **Account / recovery key** | ❌ **no** | JS var `account` only (`app.js:6`), never written to storage |
| **Chat history** | ❌ **no** | DOM only (`add()`, `app.js:247-255`) |
| Deposit in-flight / status | ❌ no | JS vars; watcher loop restarts blind |
| Selected model, UI step | ❌ no | DOM only |

## (6) Data currently persisted in the DB
The seven tables above (`server.py:195-250`) — i.e. spent-token nullifiers, receipts, vouchers, `key_hash`+balance accounts, seen deposits, claim idempotency, daily spend aggregate. **Money-integrity data only.**

## (7) Data with no persistence at all
Chat messages/sessions; any request/usage/cost log; per-key or per-model stats; deposit history (only `seen_deposits(txhash)` exists, no amount/time/model); activity/analytics; the browser account object; latency/status/TTFT; provider attribution (explicitly "not tracked yet", `provider_settle.py:10-12`).

## (8) End-to-end data flows (as built)

**API key / account:** `POST /account/new` → `sk-anon-…` returned once, server stores `keccak(key)`→`key_hash`+balance (`server.py:927-966`). Browser holds it in a JS var; CLI persists it plaintext in `~/.anon-router/wallet.json` (`wallet.py:109-114`). No list/rotate/revoke/expiry anywhere.

**Wallet (ecash):** blinded client-side (`ecash.js:150-176`) → mint blind-signs (`server.py:445-452`) → unblinded to `{amount,secret,C}` → localStorage (browser) or wallet.json (CLI). The token secret *is* bearer money; stored plaintext.

**Deposit → credits:** user tx to `CreditVault` referencing `key_hash` → watcher event scan (≥CONFIRMATIONS deep) → `POST /account/credit` (idempotent) → `accounts.balance +=`. No history row is written beyond `seen_deposits(txhash)`.

**Chat spend:** select tokens (`ecash.js:202-217`) → `_bound_cost` worst-case check (`server.py:854-879`) → atomic `_spend_and_open_receipt` (`server.py:467-487`) → upstream → `_settle_receipt` bills exact cost, issues in-band change once (`server.py:594-667`). **Nothing about the request is logged.** `X-Cost-Credits`/`X-Cash-Change` are returned, never stored.

**Usage:** the only server-side "usage" is `spend_ledger(day, usd)` — a single global number per UTC day for the cap (`server.py:735-771`). There is no per-user, per-key, or per-model usage anywhere.

## (9) Bugs, TODOs, placeholders, temporary code
**Confirmed correctness/robustness issues (agent-verified, cited):**
- `serve_ecash.py:189-212, 279-302` — the wallet `lock` is **held across the entire upstream SSE stream**; a slow provider serializes and stalls *all* concurrent proxy requests.
- `serve.py:131-132` — the channel proxy silently **drops `tools`/`tool_choice`/`response_format`** (only passes 4 fields); the exact bug `serve_ecash.py:148-155` fixed but was never ported. Tool-calling agents lose tools on the `--channel` lane.
- `onchain.py:31-42` — fixed `gas=800000`, **no receipt-status check** (a reverted close/challenge looks "successful"), and **non-`pending` nonce** (two quick txs collide). `wallet.py:163`/`provider_settle.py:79` do this correctly.
- `serve.py:94`, `server.py:297,308` — deprecated `@app.on_event("startup")` (removed in newer Starlette) — latent breakage.
- `serve.py:110-113` — `healthz` reads prover state without the lock (torn read).
- `serve.py:116-120`, `serve_ecash.py` — `/v1/models` served **unauthenticated** even when a daemon key is set.
- Duplicated constant `VOUCHER_FACE_VALUES` in `admin.py:37` **and** `wallet.py:67` — drift silently breaks voucher redemption.
- `provider_settle.py:56-57,86` — compounding floor-division dust; success check `after>before` conflates external balance changes.
- `cli.py:202-205` — deposit poll compares exact `bal>=target`; a rounding mismatch always times out to "still crediting" even on success.
- `app.js:177` — **hardcoded Alchemy RPC key committed to client JS** (added during this session's debugging) — must be reverted before any public deploy.
- Watcher `no_such_account` race — **fixed** this session (`fix/watcher-no-account-race`).

**Docs vs reality (from docs agent):** "Tor onion live" overclaim (`PRIVACY.md:12` vs `start.sh:15`); `mint_master.hex` path mismatch (`Dockerfile:19` `/data/mint_master.hex` vs `.env.example:32` `/data/anon-router/…`) — a misconfig **strands all ecash**; Groth16 verifier status disagrees between commit `b85e78a` and `VERIFICATION.md:53`/`MAINNET.md:474`; confetti test count 14 (docs) vs 18 (actual); product rename to "Tornado Router" incomplete.

**Placeholders/hardcodes:** default model `openai/gpt-4o-mini` in 4 files; `.onion` address hardcoded (`cli.py:161`); empty proof args (`onchain.py:53-61`); `topup` "dev faucet" still shipped in the CLI (`cli.py:107`).

## (10) Security / privacy / accounting / key-management risks
- **Plaintext bearer money at rest:** ecash secrets + account key in localStorage (no protection) and `wallet.json` (`0600` only); channel state is a **plaintext pickle** loaded with `pickle.load` — untrusted-deserialization hazard (`wallet.py:387`).
- **No wallet encryption option** — a shared/backed-up/synced machine leaks all funds.
- **`store.py` reintroduces plaintext `api_key`** — if wired in as-is it undoes the `key_hash`-only privacy migration.
- **Unlinkability is statistical, and the app doesn't help maximize it** — no batching guidance, no automatic claim/spend spacing; `PRIVACY.md`/`/privacy` are honest about this but the UX doesn't act on it.
- **`CREDIT_SECRET` = full mint authority** (mint arbitrary credits, `MAINNET.md:85`); single shared secret, no rotation.
- **`DAILY_USD_CAP<=0` silently disables the breaker** (`server.py:739`).
- **Custodial float** — operator holds all balances; no per-key limit means one compromised key can drain its whole balance at the daily-cap ceiling.
- **Accounting has no independent ledger** — balances are a single mutable integer per `key_hash`; there is no append-only credit ledger to reconcile deposits vs claims vs spends. A bug in any debit path is silent and unauditable.

---

# Prioritized Feature Backlog

> **Guiding principle:** for a privacy product, usage/logs/keys/history live **on the user's device**, not the server. Where OpenRouter would add a server table, we add browser IndexedDB / CLI state. The server stays minimal by design.

## P0 — must fix before the product is reliably usable
- **A. Persist the account/wallet in the browser** and auto-restore on refresh (`app.js` — write `account` alongside tokens; hydrate on load). _Today the identity is lost on refresh._
- **A. Encrypt the local wallet at rest** with Web Crypto (passphrase-derived key, AES-GCM) — both localStorage blob and the export file; keep an explicit "no passphrase" opt-out with a loud warning. _Today it's plaintext bearer money._
- **A. Trustworthy backup/restore:** versioned encrypted backup file, import validation, corruption handling, and fix the **"Recovery key" mislabel** (rename to "Account key"; make the **backup file** the recovery artifact in copy and flow).
- **B. Append-only credit ledger + deposit history** (server: one new table; see Phase 3) so balances are auditable and the UI can show pending/confirmed deposits with tx links. _Today balance is one mutable int; no history._
- **Bug sweep:** `serve_ecash.py` lock-across-stream, `serve.py` tool-dropping, `onchain.py` gas/nonce/status, deprecated startup hooks, revert the hardcoded Alchemy key, reconcile `mint_master.hex` path.

## P1 — needed for a credible MVP
- **A.** Import/switch/clear wallets; multi-browser restore via the encrypted backup; storage-version migration.
- **D+E. Client-side Usage & Activity + Logs** (browser IndexedDB / CLI SQLite): per-request rows the *client already sees* (model, in/out tokens from `usage`, cost from `X-Cost-Credits`, latency, status, timestamp) — aggregated into Activity (by model/day/status, totals, success rate) and a Logs table. **Prompt content stored locally only, opt-in, with a one-click purge and retention setting.** This gives OpenRouter-parity dashboards *without* server-side correlation.
- **F. Chat session management** (local-first): sessions list, autosave messages, restore on refresh, rename/delete/search, regenerate/stop/copy, markdown/code rendering, per-message cost. Default **local-only**; optional passphrase-encrypted server sync as a later toggle.
- **C. Local "API key" management** reframed: since there is no persistent server key, manage **named local wallets/proxies** — create, label, set a *local* spend limit / expiry the proxy enforces, and revoke by wiping local state. Show created/last-used from local records.
- **B.** Voucher redemption UI; deposit confirmations/history in the browser; balance refresh; failed-deposit recovery guidance.
- **H.** Product shell: Dashboard / Wallet / Credits / Activity / Logs / Chat / Settings pages; empty/loading/error states; toasts; confirm dialogs; mobile; onboarding; copyable curl/JS/Python.

## P2 — completeness & operability
- **C.** Local per-key limits (daily/monthly), model allow/deny enforced by the proxy; key rotation flow.
- **D/E.** CSV export, timezone handling, retention cleanup, provider/status-code breakdowns, TTFT/latency percentiles (all client-side).
- **G.** Model catalog with pricing/context/availability, default model, provider routing prefs, graceful unavailable-model handling.
- **I.** Server hardening: wire `store.py` (fixing its schema), Postgres migration, indexes, pagination, `CREDIT_SECRET` rotation, structured ops metrics on `/healthz`, reconciliation job over the new ledger, backup runbooks, integration tests for persistence/backup/usage.
- **Docs truth-up:** fix Tor "live" claim, verifier status, test counts, finish the rename.

## P3 — advanced
- **Channel lane to production:** durable channel state, real SP1/Groth16 verifier on-chain, non-custodial escrow, per-token metered pricing.
- **Shielded-pool funding** (hide deposit origin) — the funding-layer upgrade in `PRIVACY.md:41-46`.
- Guardrails (content/safety routing), workspace-level settings, agent-oriented per-request budgets.

---

# Target Data Model & State Placement

## The split that preserves the privacy thesis
| Layer | Holds | Why here |
|---|---|---|
| **React state** | current view, in-flight request, unsaved input | ephemeral |
| **localStorage** | encrypted-wallet blob pointer, active-wallet id, UI prefs, pending-claim/change | small, synchronous, already used |
| **IndexedDB (client)** | **usage rows, request logs, chat sessions/messages, deposit history, per-wallet metadata** | this is the OpenRouter-parity data — kept on-device so the server can't correlate it |
| **Server DB (minimal)** | money integrity only: spent nullifiers, receipts, `key_hash`+balance, vouchers, seen_deposits, claims, **+ new credit_ledger, deposits** | must be server-side for double-spend/custody; must stay identity-poor |
| **User device only (never server)** | ecash token secrets, wallet passphrase, prompt content, chat history | leaking these to the server breaks unlinkability or custody |

## Server tables (add two; keep the rest minimal)
- **`credit_ledger`** — append-only. `id PK, key_hash, delta, reason ENUM(deposit|claim|refund|adjust), ref (txhash/receipt_id/idem_key), ts`. Index `(key_hash, ts)`. Makes `accounts.balance` a derivable projection and gives reconciliation something to check. _No amounts-per-model, no request link — stays identity-poor._
- **`deposits`** — `event_id PK (txhash:logIndex), key_hash, token, amount, credits, block_number, block_hash, status ENUM(seen|credited|orphaned), ts`. Supersedes the bare `seen_deposits(txhash)`; powers deposit history + reorg reconciliation. (The watcher already tracks most of this in `.watcher_credited`; promote it to the DB.)
- **Keep:** `spent`, `receipts`, `vouchers`, `accounts(key_hash,balance)`, `claims`, `spend_ledger`. **Do NOT add** server-side `api_keys`, `model_requests`, `usage_daily`, `chat_*` — those go client-side.
- **Fix `store.py`** before wiring: drop the plaintext `api_key` column (match `server.py:229`), add the two tables above, add indexes.

## Client (IndexedDB) object stores
- **`wallets`** — `id PK, label, encrypted_blob (AES-GCM: {account, tokens}), created_at, last_used`. The encrypted blob is the money; passphrase never leaves the device.
- **`model_requests`** — `id PK, wallet_id, ts, model, provider, input_tokens, output_tokens, cost_credits, latency_ms, ttft_ms, status, streamed, error_code`. Populated from each response's `usage`/`X-Cost-Credits`. Index `(wallet_id, ts)`, `(wallet_id, model)`.
- **`usage_daily`** — `wallet_id+day PK, requests, in_tok, out_tok, cost_credits` — rollup of `model_requests` for fast Activity charts.
- **`chat_sessions`** — `id PK, wallet_id, title, model, created_at, updated_at`. **`chat_messages`** — `id PK, session_id, role, content, ts, cost_credits`. Prompt content local-only, purgeable.
- **`deposit_history`** — mirror of server `deposits` for the active wallet (fetched, then cached), for offline history.
- **Encryption:** everything except non-sensitive UI prefs is inside the passphrase-derived AES-GCM envelope; `model_requests`/`chat_*` are sensitive (they reveal usage) and must be encrypted too.

## How the server recognizes "the same anonymous wallet" without linking usage to funding
- Funding rendezvous uses `key_hash` (deposit → claim). After claim, the client **drains fully to ecash and stops using the key** (already the design; `app.js:114-153` avoids idle bearer polling). Spends carry **no** identifier — only ecash. So the server can tie *a deposit* to *a claim* (same `key_hash`), but **never a spend to either**. The new `credit_ledger`/`deposits` tables stay strictly on the funding side of that boundary; usage/logs/chat live on the client. This is exactly why those must not become server tables.

---

# Phased Implementation Plan

_Adjusted to the actual code, not a generic template. Each phase is independently shippable._

### Phase 0 — Audit & test baseline (foundation)
- **Goal:** stop the bleeding; make the demo honest and the money path CI-gated.
- **Tasks:** fix the confirmed bugs (`serve_ecash.py` lock-across-stream, `serve.py` tool passthrough, `onchain.py` gas/nonce/status, deprecated startup hooks, revert `app.js:177` key, reconcile `mint_master.hex` path, dedupe `VOUCHER_FACE_VALUES`); truth-up docs (Tor, verifier, counts, rename). Add a browser/persistence test harness (currently zero coverage — docs agent confirmed).
- **Files:** `serve_ecash.py`, `serve.py`, `onchain.py`, `server.py`, `web/app.js`, `admin.py`/`wallet.py`, `PRIVACY.md`/`README.md`/`VERIFICATION.md`.
- **DB/API:** none. **Accept:** existing e2e suite green in CI; docs match code; no plaintext RPC key in `web/`.
- **Risk:** low. **Depends on:** nothing.

### Phase 1 — Wallet security & persistence (P0)
- **Goal:** never lose identity on refresh; money encrypted at rest; trustworthy backup.
- **Tasks:** persist+hydrate `account` in the browser; Web Crypto AES-GCM wallet encryption (passphrase, with explicit opt-out); versioned encrypted backup/import; corruption + version-migration handling; rename "Recovery key"→"Account key" and make the backup file the recovery artifact; wallet switch/clear.
- **Files:** new `web/wallet-store.js` (IndexedDB + crypto), `web/app.js`, `web/index.html`; mirror at-rest encryption in `wallet.py` (opt-in) and **replace the `pickle` channel store** with JSON.
- **DB/API:** none server-side. **Frontend:** major. **Tests:** refresh-survival, wrong-passphrase, corrupt-blob, import round-trip, backup version migration.
- **Accept:** refresh restores account+balance+chat; export→clear→import restores exactly; wrong passphrase fails safely; no plaintext secret in storage when a passphrase is set.
- **Risk:** medium (crypto correctness — don't strand funds; keep a one-release plaintext-read fallback + migration). **Depends on:** Phase 0.

### Phase 2 — Credit ledger & deposit history (P0/P1)
- **Goal:** auditable accounting; deposit pending/confirmed/history UI.
- **Tasks:** add `credit_ledger` + `deposits` tables; write ledger entries on credit/claim/refund; promote watcher `.watcher_credited` state into `deposits`; expose an **authenticated** (bearer, funding-side only) `GET /account/deposits` for history; browser deposit-status + tx-explorer links + confirmations.
- **Files:** `server.py` (schema + credit/claim/settle paths), `watcher.py`, `web/app.js`.
- **DB:** +2 tables, indexes. **API:** +1 read route. **Tests:** ledger sums == balance; reorg → `orphaned` status; idempotent credit still single-row.
- **Accept:** every balance change has a ledger row; deposit history renders with statuses; reconciliation job flags mismatches.
- **Risk:** medium (touches the money path — additive only, behind the existing write lock). **Depends on:** Phase 0.

### Phase 3 — Client-side Usage / Activity / Logs (P1)
- **Goal:** OpenRouter-parity analytics, on-device.
- **Tasks:** IndexedDB `model_requests`/`usage_daily`; record every response's `usage`+`X-Cost-Credits`+latency; Activity page (totals, by model/day/status, success rate, tokens, avg $/Mtok) and Logs table (paginated, filterable); CSV export; retention setting + purge; prompt-content opt-in only.
- **Files:** new `web/usage-store.js`, `web/activity.js`, `web/logs.js`, page shells; the CLI proxies (`serve_ecash.py`) optionally write a local SQLite usage db behind an explicit flag.
- **DB/API:** none server-side (**deliberately**). **Tests:** rollup correctness, retention purge, export.
- **Accept:** dashboards match the money path's `X-Cost-Credits`; clearing local data removes all of it; nothing usage-related is sent to the server.
- **Risk:** low. **Depends on:** Phase 1 (wallet id + encryption).

### Phase 4 — Chat sessions (P1)
- **Goal:** durable, local-first chat.
- **Tasks:** IndexedDB sessions/messages; autosave; restore on refresh; rename/delete/search/sort; regenerate/stop/copy; markdown+code; per-message cost; clear-all + retention. Optional (later) passphrase-encrypted server sync as a toggle.
- **Files:** new `web/chat-store.js`, `web/chat.js`, refactor `app.js` send loop.
- **Accept:** refresh restores conversations; delete/clear works; local-only by default.
- **Risk:** low. **Depends on:** Phase 1.

### Phase 5 — Local key/limits & product shell (P1/P2)
- **Goal:** manage named local wallets/proxies with limits; coherent app.
- **Tasks:** named wallets, local spend limit/expiry enforced by the proxy + browser, model allow/deny, rotation/revoke-by-wipe, last-used; full page shell (Dashboard/Wallet/Credits/Activity/Logs/Chat/Settings), empty/loading/error states, toasts, confirms, mobile, onboarding, copyable snippets, voucher UI, USDC deposit path.
- **Files:** `serve_ecash.py` (limit enforcement), new page modules, `web/index.html` → multi-view.
- **Accept:** a labeled wallet with a daily limit blocks over-spend locally; navigation + states complete.
- **Risk:** medium (UX surface). **Depends on:** Phases 1–4.

### Phase 6 — Server productionization (P2)
- **Goal:** HA + operability without weakening privacy.
- **Tasks:** wire `store.py` (schema fixed), Postgres, indexes, pagination, `CREDIT_SECRET` rotation, reconciliation job over `credit_ledger`, structured metrics, backup/runbook, integration tests for persistence/backup/usage/deposit-history.
- **Files:** `store.py`, `server.py`, `watcher.py`, deploy config.
- **Accept:** multi-worker concurrency test green; reconciliation clean; docs/runbook match.
- **Risk:** medium. **Depends on:** Phase 2.

### Phase 7 — Non-custodial & funding privacy (P3)
- **Goal:** remove custody and hide funding origin.
- **Tasks:** durable channel state, real on-chain verifier (retire MockVerifier), non-custodial escrow, metered channel pricing; shielded-pool funding.
- **Files:** `confetti/`, `contracts/`, `server.py` channel path.
- **Accept:** channel lane usable end-to-end on testnet with a real verifier; funding origin hidden in an anonymity set.
- **Risk:** high (crypto + audit gated). **Depends on:** Phase 6.

---

## Appendix — the one thing to internalize
OpenRouter is a KYC'd, centralized product; its per-key usage/activity/logs live server-side because it *is* the identity. anon-router's entire value proposition is the opposite. Copying OpenRouter's **server-side** data model would delete the product's reason to exist. The right move — reflected in every phase above — is **OpenRouter-parity dashboards computed on the user's own device**, over a server that knows as little as mathematically possible.
