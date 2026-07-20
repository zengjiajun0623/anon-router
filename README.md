# anon-router

Pay crypto for AI inference. Deposit testnet ETH (or redeem a voucher), get
credits, and call any model. The provider only ever sees an anonymous router,
and your spends are cryptographically decoupled from the deposit that funded
them. You use it through a small local proxy (`anon-router serve`) that speaks
the OpenAI and Anthropic APIs; there is no hosted API key, because a persistent
key would be a linkable identifier, so that lane was removed.

## Try the demo (local)

```bash
./run_e2e.sh            # anvil + contracts + router + watcher, asserts 7/7 stages
# then open the site:
#   http://127.0.0.1:8402   (KEEP=1 ./run_e2e.sh leaves it running)
```

The site (`web/index.html`): create a wallet (no signup), **deposit ETH** from
any wallet (it switches you to Sepolia), watch credits land in ~2s, and **chat**
in the browser. To use it from code or an agent (Cursor, Claude Code, the OpenAI
SDK), run the local proxy (`anon-router serve`) and point the tool at
`http://127.0.0.1:8788`; see `CLI.md` or the `/quickstart` page.

## What's built

| Lane | What it gives you | Trust |
|---|---|---|
| **Simple** (`sk-anon-*` bearer key + `CreditVault`) | deposit → key → any OpenAI client | custodial (operator holds float) |
| **Ecash** (blind-signed tokens) | unlinkable prepaid credits | trusted mint |
| **Channel** (confetti zk payment channels, on-chain escrow) | deposits escrowed on-chain, unlinkable per-request payments | trust-minimized: operator can't steal/freeze |
| **Free** (`local/*`) | self-hosted models, no payment | — |

**Verification:** the on-chain contract has three independent code reviews
(Fable, Codex, Kimi) plus a machine-checked Lean proof of its safety core
(conservation, no-theft, terminality) — see [VERIFICATION.md](VERIFICATION.md).
The settlement core is also written in the [Verity](https://veritylang.com) Lean
EDSL that compiles to EVM. Testnet-only; nothing is deployed to a public chain.

---

Payer-anonymous, OpenAI-compatible inference proxy. Prepay for credits, spend them as blind-signed bearer tokens: the router can verify every payment but cannot link any request to the deposit that funded it, or link two requests to each other.

v1 rail is Cashu-style blind signatures (BDHKE) with a trusted mint. The channel lane replaces the trusted mint with confetti zk payment channels (see `../zk-payments-confetti/PROTOCOL.md`) so the router also cannot steal or freeze deposits.

## How it works

1. Deposit (dev: free faucet; prod: USDC) and receive blind-signed tokens in power-of-two credit denominations. 1 credit = $0.0001.
2. Each `/v1/chat/completions` request attaches tokens in the `X-Cash` header as prepayment.
3. The router verifies + burns the tokens, proxies to the upstream (OpenRouter), reads the exact USD cost from usage accounting, and holds the overpayment under a one-time receipt (`X-Change-Receipt` response header).
4. The wallet redeems the receipt for fresh blind-signed change tokens.

Blinding means the mint signs tokens without seeing them, so issued tokens and spent tokens are cryptographically unlinkable. Amount is bound by using one mint key per denomination.

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # set OPENROUTER_API_KEY
.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8402

# in another shell
.venv/bin/python cli.py topup 50000          # $5 of dev credits
.venv/bin/python cli.py chat "hello there"   # pays per request
.venv/bin/python cli.py balance
```

Any OpenAI-compatible client works against `http://127.0.0.1:8402/v1` if it can attach the `X-Cash` header per request (the CLI/wallet does this automatically).

## Selling credits (vouchers)

The MVP resale loop: the operator funds an OpenRouter account wholesale, sells voucher codes through any channel (USDC, WeChat, resellers), and buyers redeem codes for anonymous credits. The mint sees which voucher a redemption came from but cannot link the resulting tokens to any later request (blind signatures).

```bash
# operator, on the router box:
.venv/bin/python admin.py issue 50000          # prints a code worth $5
.venv/bin/python admin.py list                 # ledger of issued/redeemed

# buyer, anywhere:
python cli.py redeem ar-XXXXXXXXXXXXXXXXXXXX   # code → anonymous credits
python cli.py chat "hello" --model openai/gpt-4o-mini
```

Set `DEV_FAUCET=0` in `.env` for any deployment where credits are sold.

## Trust-minimized channel lane (confetti, M4a)

An opt-in payment lane where the buyer's deposit is governed by the confetti
zk payment channel instead of trusting the mint. See [confetti/README.md](confetti/README.md).

```bash
python cli.py channel open 5000                       # deposit 5000 credits
python cli.py chat "hello" --model openai/gpt-4o-mini --channel
python cli.py channel status
```

Each request builds a payment proof, the router verifies and countersigns it,
and a stale/rolled-back close is provably challengeable. M4a runs off-chain
against an in-memory referee with the reference (non-ZK) prover; M4b adds the
on-chain contract and the real STARK prover.

## Free local lane

Models prefixed `local/` route to a free upstream (RTX 3080 PC running Ollama, reached via ssh tunnel) and require no payment. For user testing without spending credits:

```bash
ssh -f -N -L 11435:127.0.0.1:11434 pc3080   # once per boot
.venv/bin/python cli.py chat "hello" --model local/qwen3:8b
```

Override the upstream with `LOCAL_UPSTREAM` in `.env`.

## Honest limitations (v1)

- The mint is trusted with float: it could refuse redemption. v2 (confetti channels) removes this.
- Payment unlinkability is not payer anonymity: use fresh connections/proxy for transport privacy. Requests within one HTTP session are linkable by the connection itself.
- Prepay is a fixed amount per request, not a per-model max-cost estimate.
- Streaming change redemption requires polling the receipt after the stream ends.
- Dev faucet stands in for the USDC deposit watcher.

## Milestones

- **v0** — blind-signature mint, OpenRouter proxy, exact-cost metering. Done.
- **Voucher resale** — operator sells codes, buyers redeem for anonymous credits. Done.
- **Simple lane** — `CreditVault` deposit → bearer key → any OpenAI client; site + watcher. Done.
- **M4a** — confetti off-chain channel protocol + router lane. Done.
- **M4b** — on-chain escrow (`ConfettiChannels`), deposits leave custody, 3-reviewer + Lean-proof gate. Done (local Anvil).
- **Verity** — settlement core written in Lean, compiled to EVM. In progress.
- **M4b-real** — SP1 Groth16 verifier replacing the mock. In progress (Docker/colima).

## Roadmap

- USDC deposit lane (same as `CreditVault`, ERC-20 `transferFrom`)
- Known-answer sampling audits of upstreams, published scores
- new-api/one-api payment module (中转站 integration)
- Durable persistence of the router's off-chain state (dedup/inbox/XMSS)
- Public deploy to Ethereum Sepolia (operator holds the key; counsel gate first)
