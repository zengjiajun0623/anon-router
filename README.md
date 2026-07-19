# anon-router

Payer-anonymous, OpenAI-compatible inference proxy. Prepay for credits, spend them as blind-signed bearer tokens: the router can verify every payment but cannot link any request to the deposit that funded it, or link two requests to each other.

v1 rail is Cashu-style blind signatures (BDHKE) with a trusted mint. v2 replaces the trusted mint with confetti zk payment channels (see `../zk-payments-confetti/PROTOCOL.md`) so the router also cannot steal or freeze deposits.

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

## Roadmap

- USDC deposit watcher replacing the faucet
- Known-answer sampling audits of upstreams, published scores
- new-api/one-api payment module (中转站 integration)
- Confetti channel rail (pending Phase 0 proving benchmark in `research/`)
