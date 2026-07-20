# anon-router CLI — pay crypto for AI inference, privately, from your terminal

No account, no card, no email. Deposit crypto, get an anonymous key, call any
model. Your spends can't be linked to your deposit, and the model provider only
ever sees the router — never you.

> Testnet alpha: deposits use **Sepolia test ETH** (valueless). Don't put real
> funds in. See [PRIVACY.md](PRIVACY.md) for exactly what is and isn't private.

## Install (client only)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-client.txt
```

The CLI talks to the hosted router by default. To use your own, set
`export ANON_ROUTER_URL=https://your-router` (or pass `--url`).

## Use it

```bash
PY=.venv/bin/python

# 1. Mint an anonymous key (no signup). This is a wallet — back up ~/.anon-router.
$PY cli.py account

# 2a. Fund it by depositing test ETH on-chain. Put YOUR Sepolia key in a file
#     {"private_key": "0x..."}  (or export ANON_DEPOSIT_KEY), then:
$PY cli.py deposit 0.001 --key myfunding.json     # waits for the credit (~30s)
#  ...or 2b. redeem a voucher code someone gave you:
$PY cli.py redeem ar-XXXXXXXXXXXXXXXXXXXX

# 3. Convert your balance into unlinkable ecash (this breaks the deposit link).
$PY cli.py claim 5000

# 4. Chat — paid per request with ecash, unlinkable to your deposit.
$PY cli.py chat "hello there" --model openai/gpt-4o-mini
$PY cli.py balance
$PY cli.py models --search gpt
```

## Two ways to spend

- **Private (recommended): `cli.py chat`** — pays with blind-signed ecash, so
  neither the router nor the provider can tie a request to your deposit.
- **Convenient: any OpenAI-compatible tool** — point `base_url` at
  `<router>/v1` and use your `sk-anon-…` account key as the bearer token. This
  spends the account balance directly (pseudonymous, not unlinkable) and works
  with Cursor, the OpenAI SDK, etc.

## Privacy hygiene
Fund from a fresh wallet, deposit a common round amount, spend over time, and
rotate keys (`cli.py account`) so sessions don't link. Reach the router over its
Tor `.onion` (`--tor`) to hide your IP. The model still reads your prompt — for
content privacy use a `local/*` model.
