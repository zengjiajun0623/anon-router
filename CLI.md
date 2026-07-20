# anon-router CLI — pay crypto for AI inference, privately, from your terminal

No account, no card, no email. Deposit crypto, get an anonymous key, call any
model. Your spends can't be linked to your deposit, and the model provider only
ever sees the router — never you.

> Testnet alpha: deposits use **Sepolia test ETH** (valueless). Don't put real
> funds in. See [PRIVACY.md](PRIVACY.md) for exactly what is and isn't private.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install .   # from this repo (Python 3.10+)
```

That puts an `anon-router` command on your PATH. It talks to the hosted router by
default; to use your own set `export ANON_ROUTER_URL=https://your-router` (or
`--url`).

## Use it

```bash
# 1. Mint an anonymous key (no signup). This is a wallet — back up ~/.anon-router.
anon-router account

# 2a. Fund it by depositing test ETH on-chain. Put YOUR Sepolia key in a file
#     {"private_key": "0x..."}  (or export ANON_DEPOSIT_KEY), then:
anon-router deposit 0.001 --key myfunding.json     # waits for the credit (~30s)
#  ...or 2b. redeem a voucher code someone gave you:
anon-router redeem ar-XXXXXXXXXXXXXXXXXXXX

# 3. Convert your balance into unlinkable ecash (this breaks the deposit link).
anon-router claim 5000

# 4. Chat — paid per request with ecash, unlinkable to your deposit.
anon-router chat "hello there" --model openai/gpt-4o-mini
anon-router balance
anon-router models --search gpt
```

(Running from the repo without installing? `.venv/bin/python cli.py <same args>`.)

## Use it from your agent / tool ("swap the API")

Already have something that speaks the OpenAI API (Cursor, aider, the OpenAI SDK,
your own agent)? Two ways to point it here:

**Private (recommended) — run the local proxy, point your tool at it:**
```bash
anon-router claim 5000            # make sure your wallet has ecash
anon-router serve                 # local proxy on http://127.0.0.1:8788/v1
# then in your tool:  base_url = http://127.0.0.1:8788/v1   (api_key = anything)
```
Every request the tool makes is paid with blind-signed ecash — the router can't
link your tool's requests to each other or to your deposit. No code changes in
the tool. (Set `ANON_DAEMON_KEY` to require a bearer key on the proxy.)

> Note: there is no hosted-API-key lane. A persistent bearer key would be a
> linkable identifier, so inference is paid only with client-attached ecash
> (via the local proxy above). Pointing an OpenAI client straight at the router
> with an `sk-anon-…` key returns 402 by design.

### Claude Code
The proxy also speaks the Anthropic Messages API (streaming + tool use), so
Claude Code runs against it with two env vars:
```bash
anon-router serve                 # local proxy, leave it running
export ANTHROPIC_BASE_URL=http://127.0.0.1:8788
export ANTHROPIC_API_KEY=anon-router     # any non-empty value
claude                            # every request now pays private ecash
```
The model names Claude Code sends are mapped to a valid router model
automatically; full agentic tool use works.

## Or just chat in the terminal
`anon-router chat "…"` — private (ecash), no other tool needed.

## Privacy hygiene
Fund from a fresh wallet, deposit a common round amount, spend over time, and
rotate keys (`cli.py account`) so sessions don't link. Reach the router over its
Tor `.onion` (`--tor`) to hide your IP. The model still reads your prompt — for
content privacy use a `local/*` model.
