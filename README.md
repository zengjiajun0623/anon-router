# anon-router

[![PyPI](https://img.shields.io/pypi/v/anon-router.svg)](https://pypi.org/project/anon-router/)

**Pay crypto for AI inference, privately.** Deposit testnet ETH (or redeem a
voucher), get credits, and call any model (GPT, Claude, and more). The provider
only ever sees an anonymous router, and your spends are cryptographically
decoupled from the deposit that funded them. No account, no card, no KYC.

> Product name: **Tornado Router**. The package, repo, and CLI are still
> `anon-router` during the rename.

Live testnet alpha: **https://anon-router-production.up.railway.app** (Sepolia,
custodial MVP).

## Quickstart

```bash
pip install anon-router

anon-router redeem <voucher>          # anonymous credit, no crypto needed
#   or, on-chain: anon-router deposit 0.05 --key wallet.json  &&  anon-router claim

anon-router serve                     # a private OpenAI + Anthropic endpoint on :8788
```

Then point any OpenAI- or Anthropic-compatible tool at `http://127.0.0.1:8788`:

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"openai/gpt-4o-mini","messages":[{"role":"user","content":"say hi"}]}'
```

Full walkthrough: the [`/quickstart`](https://anon-router-production.up.railway.app/quickstart)
page or [`CLI.md`](CLI.md).

**There is no hosted API key.** A persistent key would be an identifier the router
sees on every call, so it could not be unlinkable. The developer interface is the
local proxy, which attaches blind-signed ecash client-side. (The `api_key` on the
proxy is any string.)

## How it works

1. **Fund once.** Deposit Sepolia ETH to a `CreditVault` (a watcher credits your
   account) or redeem a voucher. Then `claim` drains your whole balance into
   blind-signed ecash tokens, in one deliberate step.
2. **Spend per request.** Each `/v1/chat/completions` call attaches ecash in the
   `X-Cash` header. The router verifies and burns the tokens, forwards to the
   upstream (OpenRouter), bills the exact cost, and returns change **in-band** as
   fresh blind-signed tokens on the same response (Cashu NUT-08 style).
3. **Unlinkable.** The mint blind-signs tokens it never sees unblinded (BDHKE), so
   issued tokens and spent tokens cannot be linked, and a spend cannot be tied to
   the funding account. One mint key per denomination binds the amount.

## What's private, honestly

- **No signup, no card, no KYC.** A deposit creates a pseudonymous account (a key
  hash); a voucher creates none.
- **No direct link from a spend to your deposit** (blind signatures). In practice,
  unlinkability is only as strong as the number of others claiming and spending in
  the same window, which is small on an early alpha.
- **The provider only ever sees the router**, never you.
- **Honest limits:** the model reads your prompt (use a `local/*` model to keep it
  off third parties); the balance is **custodial** (keep it small); the router
  still sees amounts and timing; the app does not log IPs but the hosting edge can,
  so connect over the live Tor `.onion` and space out claims and spends.

Machine-readable posture:
[`/privacy`](https://anon-router-production.up.railway.app/privacy) ·
[`PRIVACY.md`](PRIVACY.md).

## Lanes

| Lane | What it is | Trust | Status |
|---|---|---|---|
| **Ecash** (blind-signed tokens) | unlinkable prepaid credits, the live payment rail | custodial (operator holds float) | **live** |
| **Channel** (confetti zk payment channels, on-chain escrow) | deposits escrowed on-chain, operator cannot steal or freeze | trust-minimized | roadmap (off by default) |
| **Free** (`local/*`) | self-hosted models, no payment | n/a | dev |

The old "bearer key into any OpenAI client" lane was intentionally removed: a
persistent hosted key is linkable, which defeats the point.

## Verification

The confetti channel contract has three independent code reviews (Fable, Codex,
Kimi) plus a machine-checked **Lean** proof of its safety core (conservation,
no-theft, terminality); see [VERIFICATION.md](VERIFICATION.md). The settlement
core is also written in the [Verity](https://veritylang.com) Lean EDSL that
compiles to EVM. The live money path (cost-bounding, at-most-once spend and
change, crash recovery, data-minimization) is covered by `tests/e2e_*.py` and was
reviewed by Codex + Kimi + Fable.

## Honest limitations

- **Custodial:** the router holds prepaid float and could refuse redemption. The
  confetti channel lane removes this (roadmap).
- **Single instance, SQLite:** no HA. Multi-instance + Postgres is the mainnet
  gate (one instance is downtime; many on SQLite is double-spend).
- **Testnet only:** Sepolia ETH, no real funds. Mainnet needs an audit + counsel.
- **Unlinkability is anonymity-set-dependent** and weakens without Tor.

## Repo layout

- `server.py`: the router (spend/redeem/change, cost-bounding, deposit-watcher
  supervision, `/privacy`), `watcher.py`: the on-chain deposit watcher.
- `wallet.py` / `cli.py`: the client wallet + the `anon-router` CLI.
- `serve_ecash.py`: the local proxy (OpenAI + Anthropic); `anthropic_proxy.py`
  does the Anthropic to OpenAI translation.
- `mint.py` / `ec.py`: the BDHKE mint.
- `web/`: the site, the in-browser ecash wallet, and `/quickstart`.
- `confetti/`, `contracts/`, `lean/`, `verity/`: the non-custodial channel lane
  and its proofs.

## Run your own router (local dev)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # set OPENROUTER_API_KEY (and DEV_FAUCET=0 to sell credits)
./run_e2e.sh                  # anvil + contracts + router + watcher, asserts the E2E stages
```

Issue voucher codes with `python admin.py issue 50000`.

## Status / roadmap

- Live Sepolia testnet alpha: ecash lane, custodial, faucet off, channel off,
  daily spend cap. **Done.**
- Non-custodial confetti channel lane on-chain, with a real verifier (not the
  mock). **In progress.**
- HA + Postgres, independent security audit, counsel: the mainnet gate. **Pending.**
