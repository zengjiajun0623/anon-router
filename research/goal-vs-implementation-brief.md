# Final review: is there a GAP between our GOAL and the IMPLEMENTATION?

anon-router is a live Sepolia testnet alpha. This is a final, honest gap review.
Not "is the code clean" — the real question: **does what we built actually deliver
what we set out to do?** Find every place the implementation falls short of the goal,
ranked by how much it undermines the goal.

## THE GOAL (the target to measure against)

**Thesis:** a crypto protocol makes the PAYMENT LAYER of AI inference private. For
people who can't run their own GPUs, or who want to use hosted models (Claude/OpenAI)
without their usage being tied to their identity, they can pay for inference privately.

**The MVP, concretely (product owner's own words):**
1. A user deposits ETH on Sepolia testnet and gets a WORKING API with credit.
2. The user can USE that API for inference.
3. The user's spends CANNOT be linked to their deposit.

**How we claim to deliver it:**
- Blind-signed ecash (Cashu-style BDHKE): the user funds once, gets ecash tokens, and
  pays per request with them. The router blind-signs tokens it never sees unblinded, so
  a spend can't be tied to the funding account or to other spends.
- The developer-facing API is a LOCAL PROXY (`anon-router serve`) that attaches ecash
  client-side and is OpenAI- + Anthropic-compatible. There is deliberately NO hosted
  bearer-inference key (it would be a linkable identifier; it returns 402).
- No account, no card, no KYC. The model provider sees only the router, never the payer.

**Honest declared scope (NOT gaps unless we overclaim them):** content privacy is out
(the model reads the prompt; mitigated by `local/*` models); the MVP is CUSTODIAL (the
router holds prepaid balances; non-custodial = the confetti channel lane, currently OFF);
IP/timing need Tor; this is testnet, not mainnet.

## THE IMPLEMENTATION (what to review)
- Router: `server.py` (spend/redeem/change, cost-bounding, daily cap, no-bearer-lane,
  /privacy, /account/new, /config exposing chain_id), `watcher.py` (Sepolia deposit → credit).
- Mint / crypto: `mint.py` (BDHKE), `ec.py`.
- Client: `wallet.py` (claim_all, ecash spend, in-band change, open_stream/finish_stream),
  `cli.py` (account/deposit/claim/redeem/serve, clean-error wrapper), `serve_ecash.py`
  (the local proxy: OpenAI + Anthropic lanes, field passthrough, real streaming), `anthropic_proxy.py`.
- Site: `web/index.html`, `web/app.js` (browser deposit now switches wallet to Sepolia +
  precise wei), `web/ecash.js` (in-browser BDHKE wallet), `web/quickstart.html`.
- Tests: `tests/e2e_unlinkability.py`, `e2e_money_safety.py`, `e2e_privacy.py`, `e2e_proxy.py`.
- Docs: `AGENTS.md`, served `/quickstart`, `/privacy`.
- Live: https://anon-router-production.up.railway.app (custodial, faucet OFF, channel OFF,
  DAILY_USD_CAP=$10, single instance).

## WHAT TO CHECK — find gaps between goal and implementation
1. **Unlinkability (goal 3) — the core claim.** Is spend↔deposit unlinkability actually
   achieved, or are there residual linkage vectors the design still leaks (claim→spend
   timing, amounts, connection/metadata, oracle endpoints, the account key touching the
   inference path, change-token correlation, receipt/claim records)? Is every claim in
   `/privacy` and the site/quickstart TRUE given the code? Any place we OVERCLAIM or
   UNDERCLAIM privacy vs. what BDHKE + the plumbing actually give.
2. **"Deposit → working API with credit" (goals 1-2).** Does a real first-time user get
   from deposit (or voucher) to a working inference API with no dead-end? Any broken step,
   wrong instruction, or claim the code can't back (install, funding, claim, serve, call)?
3. **Is the "working API" actually usable** the way real tools need it (OpenAI SDK, Cursor,
   Claude Code): tools, streaming, models, errors? Any capability gap vs. "swap the API"?
4. **Money-safety as it bears on "a WORKING API":** can the user lose credit, get charged
   for nothing, double-spend, or get free inference — anything that breaks the product
   promise? (Deep dive already passed; look for anything new or missed.)
5. **Goal-vs-scope honesty:** are the declared non-goals (content privacy, custody, Tor,
   testnet) disclosed clearly where a user would look, or is there a gap between what a
   user reasonably infers and what's true?
6. **Conceptual / product gap (esp. Fable):** does this actually solve the stated problem
   for the target user, or is there a gap between the thesis ("private inference for people
   who can't run their own boxes") and what shipped — e.g. friction, custody trust, the
   proxy model, the funding UX — that means the goal isn't really met in practice?

## DELIVERABLE
A ranked list of GAPS (goal ↔ implementation), each: the goal it undermines, the concrete
shortfall (file:line or user-visible behavior), severity (breaks-the-goal / weakens-it /
cosmetic), and the smallest fix. Then a one-line verdict: **does the implementation meet
the stated goal for a testnet alpha, yes or no**, and the single most important gap to close.
If you believe there is NO material gap, say so explicitly and justify it.
