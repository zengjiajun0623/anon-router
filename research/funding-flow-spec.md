# Funding flow spec: voucher onboarding + self-serve USDC top-up

**Status:** approved 2026-07-20 (counsel cleared mainnet custody). Voucher
onboarding ships first; mainnet USDC top-up is wired only after the money-path
review loop returns a clean PASS. Reuses the deposit code already built.

## Core principle: the wallet is the continuity, not an account

After the balance-less-funding change, the server holds **no account balance**.
The unit that persists and accumulates credits is the **client-side ecash
wallet** (`~/.anon-router/wallet.json` for CLI, `localStorage` for web). Every
funding rail mints fresh ecash into that same wallet:

```
voucher code ─┐
              ├─→ blind-signed ecash ─→ SAME wallet (credits just add up)
USDC deposit ─┘
```

Consequences that make the UX seamless:
- There is nothing to "top up into" and no account to match — a top-up just adds
  ecash to the existing wallet balance.
- The API key / base_url the user points their tool at **never changes** across
  top-ups.
- Continuity is **per-surface** (the wallet lives on the device). Web has
  export/import (bearer money: the file IS the funds) for cross-device.

## Rail A — Voucher (onboarding, ships first)

1. User obtains a code (reseller / we hand one out). Fixed face values
   ($1/$5/$10/$20).
2. Paste into the site or `anon-router redeem <code>`.
3. `POST /mint/redeem` (code in body) → blind-signed ecash into the wallet.
4. Chat. No wallet, no gas, no chain, instant.

Custody exposure is bounded (voucher float). Privacy: as private as how the code
was bought — copy must say exactly that, not oversell.

## Rail B — Self-serve USDC top-up (retention, post-PASS)

The returning user clicks **"Top up"** on the site:

1. Mint a FRESH disposable rendezvous account key (`POST /account/new`) for THIS
   top-up. (Not reused across top-ups — a fresh key stops multiple deposits from
   linking to one on-chain identity, and there's no persistent balance to carry.)
2. Connect wallet → send `deposit(keyHash)` USDC to the vault.
3. Watcher credits the rendezvous key; the site polls (`watchFunding(awaitCredit)`)
   through mining.
4. On credit, `claim_all` drains the whole balance into the EXISTING ecash wallet;
   discard the rendezvous key.
5. Credits appear in the same balance; user keeps chatting. No re-pointing, no new
   API key.

Net: user sees one continuous balance; under the hood, disposable rendezvous keys
+ one persistent wallet.

### "Top up" UI states (web)
- **idle** — "Top up" button; shows current ecash balance.
- **connect** — request wallet; show the fixed USD⇄credits preview (demo rate,
  not a live feed — say so).
- **submitted** — "Waiting for your deposit to confirm…" spinner; `watchFunding`
  polls through mining (does NOT quit on the pre-mine zero balance).
- **credited** — auto-claim to ecash; balance ticks up; "Added N credits."
- **error/timeout** — after ~6–7 min, "Not seeing it yet — it'll appear when the
  tx confirms; refresh to check." (Funds are safe in the account until claimed.)

## The one seam to communicate
Cross-device / web→CLI is export→import, not automatic (the wallet is on the
device). For the intended funnel (voucher onboard → top up on the same site in
the same browser) this never bites. Add a one-line "Back up your wallet" nudge.

## Gates
- **Legal:** cleared (counsel sign-off 2026-07-20).
- **Engineering:** mainnet USDC top-up wired to production ONLY after the money
  path passes the Codex+Kimi review loop. Voucher onboarding may precede it.
- Testnet remains Sepolia until mainnet cutover; keep the honest privacy copy
  (payment-private, not content-private; funding pseudonymous).
