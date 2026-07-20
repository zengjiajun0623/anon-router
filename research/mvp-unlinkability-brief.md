# Review brief: does this design achieve payment-layer unlinkability?

## The claim under review (the product's promise)
"Cash for AI": anonymous pay-per-use access to hosted models. Specifically:
C1. The inference provider (OpenRouter and the model vendors behind it) sees only
    the router, never who paid or any session/user identifier.
C2. The router itself cannot link an ecash spend to the deposit/voucher that
    funded it (cryptographic, not policy).
C3. Requests are not linkable to each other, beyond artifacts the user reuses.
C4. No account, card, email, or identity exists anywhere in the flow.

## Architecture (as designed for the real-money MVP)
- Hosted custodial router (FastAPI, Railway) with OpenRouter upstream. Forwards a
  strict ALLOWLIST of inference fields only (model/messages/sampling params);
  drops user/metadata/store/session_id/trace; client headers never forwarded;
  upstream request uses only the router's own API key.
- Chaumian mint (Cashu-style BDHKE blind signatures, secp256k1):
  - Fund: value arrives either (a) on-chain deposit to CreditVault keyed by
    hash(bearer key) -> watcher credits an account balance, or (b) voucher code
    redeemed into the account balance. Account = bearer key "sk-anon-...", minted
    client-side, no signup.
  - Claim: account balance -> blind-signed ecash tokens. Client blinds token
    secrets; mint signs blinded messages (cannot see the tokens it signs).
    Denominations are fixed powers of 2.
  - Spend: client attaches tokens (X-Cash header) to a chat request. Router
    verifies signature + spends nullifier (double-spend DB). Worst-case cost
    bounded up front; change returned via a one-time receipt the client redeems
    into fresh blind tokens.
- Transport: HTTPS and a Tor v3 onion (stable address); no access logs, no
  cookies, no server header; CLI has --tor (SOCKS).
- Client surfaces: web chat (in-browser JS ecash wallet), OpenAI-compatible API,
  local proxy that also speaks the Anthropic Messages API (Claude Code), CLI.
- MVP hardening planned: automatic bearer-key rotation per conversation;
  claim-timing decorrelation (batched/delayed claims).
- Custody: operator holds pooled funds, pays OpenRouter conventionally.

## User flows (MVP)
A. Web: open site (or onion) -> "New key" (client-side) -> fund via voucher code
   or wallet deposit -> chat in browser. Balance decrements per request.
B. Claude Code / agents: pip install anon-router; fund once; `anon-router serve`
   local proxy; ANTHROPIC_BASE_URL=http://127.0.0.1:8788 -> claude. Proxy pays
   per request with ecash from the local wallet, auto-refills from the account.
C. No-card users: buy a voucher code from a reseller (out-of-band payment,
   e.g. local rails via API-relay resellers), redeem, chat.

## Accepted/known limitations (do NOT just restate these; attack what's left)
- Content is not private: the model reads prompts (local/* lane is the answer).
- Custodial balance: operator can steal/freeze (non-custodial channels = roadmap).
- Funding is pseudonymous, not anonymous: an on-chain deposit traces to the
  payer's address/exchange; vouchers trace to the purchase channel.
- The mint sees amount + timing metadata of deposits/claims/spends (blind sigs
  make linkage cryptographically impossible, not correlation-proof).

## What we want from you
1. Enumerate CONCRETE linkage attacks that break C1-C4 as designed, with the
   attacker position stated (router-insider, provider, chain observer, network
   observer, voucher seller, or coalitions). Severity + likelihood each.
2. Attack the USER FLOWS specifically: where does identity leak in practice
   (funding step, claim step, spend step, change redemption, key rotation gaps,
   browser/JS fingerprinting, proxy defaults, voucher purchase)?
3. Judge the planned mitigations (key rotation per conversation, claim-timing
   decorrelation, fixed denominations): sufficient? What's missing that is CHEAP
   and belongs in the MVP vs what is genuinely roadmap?
4. Verdict: does this design, as specified, deliver the promise "your usage
   cannot be tied to you unless your prompts tie it themselves"? If not, state
   the minimum set of changes that would make the claim honest.
