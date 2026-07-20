# Review: gap between the MVP DESIGN and the FINAL CODE

Judge whether the shipped code actually delivers the MVP design below. Your job is
NOT another money-safety audit (that passed already). Your job is to find the GAP
between what the design *claims to a user* and what the code *actually does*. Every
gap = a place a real first-time user would be misled, blocked, or silently lose the
privacy property. Rank gaps by severity.

## The MVP goal (3 requirements, verbatim from the product owner)
1. A user deposits ETH on Sepolia testnet and gets a WORKING API with credit.
2. The user can USE that API for inference.
3. The user's spends CANNOT be linked to their deposit.

## The design as described to the user (this is what we must match)

**The API a developer uses = the LOCAL PROXY (`anon-router serve`).** Not a hosted
pasted key. Rationale: a hosted bearer key is an identifier the router sees on every
call, so spends made with it are linkable (breaks req 3). The bearer-inference lane
was deliberately REMOVED. The unlinkable spend must carry ecash attached client-side,
so there is a thin local relay.

Claimed flow:
```
anon-router deposit 0.05 --key wallet.json   # deposit Sepolia ETH -> account credited
   (or) anon-router redeem ar-XXXX           # voucher instead
anon-router claim                            # account balance -> ecash tokens (local)
anon-router serve                            # local OpenAI+Anthropic proxy on :8788
# then any tool: base_url=http://127.0.0.1:8788/v1, api_key=anything
```

Claimed properties:
- `serve` runs a tiny stdlib HTTP server, OpenAI (`/v1/chat/completions`) AND
  Anthropic (`/v1/messages`) compatible. No GPU, no model — just attaches ecash and
  forwards to the hosted router.
- Unlinkability comes from splitting funding and spending: `claim` drains the ENTIRE
  account balance into blind-signed ecash ONCE (deliberate event), so there is NO
  per-request claim->spend timing marker. Each request carries ecash the router
  blind-signed but never saw unblinded. Change returns in-band as fresh blinded tokens.

## What to check (design -> code gap)
For EACH claim above, find the code that implements it and confirm it matches, OR flag
the gap. Specifically:

A. **"deposit -> working API with credit" actually works for a first-time user.**
   Trace: `cli.py` deposit -> account credit -> `claim` -> `serve`. Does a fresh user
   with only Sepolia ETH reach a working localhost API? Any missing step, unclear
   prompt, or dead-end? Does `serve` auto-claim, or must the user run `claim` first
   (and does the code/help text say which)?

B. **The proxy really is OpenAI- AND Anthropic-compatible and forwards correctly.**
   `serve_ecash.py`: `/v1/chat/completions`, `/v1/messages`, `/v1/models`, streaming.
   Any client that would break (Cursor, aider, OpenAI SDK, Claude Code)?

C. **Unlinkability claim vs reality.** Does `claim` actually drain the FULL balance in
   one deliberate event (`wallet.claim_all`), decoupled from spends? Is there ANY
   residual path that re-links: per-request claim, JIT refill, an identifier attached
   to the hosted call, a status/oracle endpoint, connection reuse, the account key
   leaking into the inference path? Confirm the bearer-inference lane is truly gone
   server-side (`server.py`). If a user copies the Step-1 hosted key into an OpenAI
   client, what happens (should be 402)?

D. **The site (web/index.html + app.js) vs the design.** The site still shows a Step-1
   "API key + Base URL, use with any OpenAI tool" box. Is that key usable for hosted
   inference (it should NOT be — 402)? Is the in-browser chat unlinkable the same way
   the proxy is (does app.js/ecash.js attach blinded ecash, or does it ride the account
   key)? Flag anything the site promises that the code doesn't deliver.

E. **Honesty of the privacy claim.** "Router blind-signed but never saw unblinded" and
   "cannot tie to account/deposit/other requests" — is that true given the actual BDHKE
   in `mint.py`/`server.py`? Any metadata (amount, timing, denom pattern, IP without
   Tor) that still correlates in practice, and is it disclosed?

## Files
- server.py (router; spend/redeem/change, no-bearer-lane)  ~74KB
- wallet.py (client wallet; claim_all, ecash spend, change)  ~33KB
- serve_ecash.py (the local proxy)  ~13KB
- cli.py (deposit/claim/redeem/serve)  ~14KB
- mint.py (BDHKE mint)
- web/index.html, web/app.js, web/ecash.js (site + in-browser wallet)
- tests/e2e_unlinkability.py, tests/e2e_money_safety.py, tests/e2e_privacy.py

## Deliverable
A ranked list of GAPS between design and code (severity: blocker / major / minor /
cosmetic), each with file:line and the concrete user-visible consequence. Then a
one-line verdict: does the shipped code deliver the 3-requirement MVP as designed?
If not, the smallest fix set that closes the blocker/major gaps.
