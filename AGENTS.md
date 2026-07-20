# For AI agents: set up private AI inference for your user

If your user asks you to let them pay for AI inference **privately** (no account,
no card, unlinkable) via anon-router, run these steps. Everything is
non-interactive and safe to script.

## 1. Install
Needs Python 3.10+. A virtualenv is the always-works path (macOS often has no bare `pip`):
```bash
python3 -m venv anon-router-env && source anon-router-env/bin/activate
pip install git+https://github.com/zengjiajun0623/anon-router.git   # PyPI package coming
```
Or one global command with pipx/uv (no venv to activate):
```bash
uv tool install git+https://github.com/zengjiajun0623/anon-router.git
# or: pipx install git+https://github.com/zengjiajun0623/anon-router.git
```
This adds an `anon-router` command. It targets the hosted router by default — no
config needed. (Override with `ANON_ROUTER_URL` only for a self-hosted router.)

## 2. Mint an anonymous key
```bash
anon-router account                  # a wallet key, stored in ~/.anon-router
```

## 3. Fund it — this step needs the USER's value-in; you can't create it
Pick whichever the user has:
- **Voucher (easiest, no crypto):** the user pastes a code. This mints spendable
  ecash DIRECTLY — no `account`, no `claim`, go straight to step 4:
  ```bash
  anon-router redeem <code>
  ```
- **Crypto deposit (Sepolia testnet for now):** the user points you at a key file
  `{"private_key":"0x…"}` (never put a raw key on the command line), then drains
  the whole credited balance into unlinkable ecash in one step:
  ```bash
  anon-router deposit 0.001 --key /path/to/keyfile.json
  anon-router claim                  # deposit path only; no amount = full balance
  ```
```bash
anon-router balance                  # confirm you have credits
```

## 4. Start the private proxy (run it in the background)
```bash
anon-router serve &                  # OpenAI-compatible endpoint at http://127.0.0.1:8788/v1
```
It claims ecash once at startup. If the wallet runs out mid-session, fund again
(redeem/deposit+claim) and RESTART the proxy — it does not refill on its own (a
per-request claim would let the router re-link your usage).

## 5. Swap the API in the user's OpenAI-compatible tool/framework
Set the base URL (and any key — it's ignored locally):
```bash
export OPENAI_BASE_URL=http://127.0.0.1:8788/v1
export OPENAI_API_KEY=anything
```
Or set `base_url = http://127.0.0.1:8788/v1` in the tool's config. Bare model
names work (`gpt-4o`, `claude-3.5-sonnet`, …). Every request now pays private
ecash — the provider only ever sees the router, never the user.

### 5b. Claude Code specifically
Claude Code speaks the Anthropic Messages API, not OpenAI — the proxy serves both
(`/v1/messages` with streaming + tool use). Point Claude Code at it with two env
vars, then use `claude` normally:
```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8788
export ANTHROPIC_API_KEY=anon-router          # any non-empty value; ignored locally
claude                                          # or: claude -p "…"
```
Anthropic model names Claude Code sends (`claude-sonnet-4-5`, `claude-opus-4-5`,
…) are resolved to a currently-valid router model automatically. Full agentic
tool use (Read/Bash/Edit) works — verified end to end.

## 6. Verify
```bash
curl -s http://127.0.0.1:8788/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"ping"}]}'
anon-router balance                  # should have dropped by a credit
```

## Rules for you (the agent)
- **Never** put the user's private key on the command line or in logs — use a
  file (`--key`) or `$ANON_DEPOSIT_KEY`.
- Funding needs the user's crypto or a voucher; if there's no balance, ask the
  user to fund (prefer a voucher for non-crypto users). Don't try to fabricate it.
- `serve` runs in the foreground — background it and leave it running.
- This is a testnet alpha (Sepolia test ETH). Say so to the user.
