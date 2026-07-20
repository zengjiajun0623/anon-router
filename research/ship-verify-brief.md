# Verify the design-vs-code fix diff before shipping to prod

A prior Codex+Kimi review found gaps between the anon-router MVP DESIGN and the
CODE. Those gaps have now been fixed. Your job: verify the FIXES actually close the
gaps AND that they introduce NO new bug — especially no money-loss or unlinkability
regression in the new streaming code. This diff ships to a live testnet immediately
after you pass it, so be adversarial.

Working dir: /Users/clawbox/cleavelabs/anon-router
The full diff is committed to your reading as: research/ (this file) + read the
changed files directly. Changed: cli.py, serve_ecash.py, server.py, web/app.js,
web/index.html; NEW: web/quickstart.html, tests/e2e_proxy.py.

## The gaps that were supposed to be fixed
1. BLOCKER: site Step-1 advertised a hosted "API key + Base URL, use with any OpenAI
   tool" that 402s (bearer lane removed). Fix: Step 1 reframed to a "Wallet key";
   header + a new /quickstart page point developers at the local proxy instead.
2. MAJOR: OpenAI proxy lane dropped tools/tool_choice/response_format (5-field
   whitelist). Fix: serve_ecash now forwards every field except model/messages/
   stream; the server's UPSTREAM_ALLOWED_FIELDS does the security filtering.
3. MAJOR: OpenAI streaming was faked (non-stream upstream, single synthesized chunk,
   tool_calls dropped). Fix: new `_openai_stream` opens a real upstream stream,
   buffers UNDER the wallet lock, peels the in-band x-cash-change event, settles via
   finish_stream, then replays genuine upstream chunks (incl. tool_call deltas + [DONE]).
4. MAJOR: deposit told users to partial-claim (`claim <amount>`). Fix: cli.py deposit
   + setup now say bare `anon-router claim` (full drain).
5. MAJOR: privacy copy overclaimed categorical unlinkability. Fix: qualified to
   "cryptographic payment unlinkability; IP/timing correlate without Tor" in
   index.html, the proxy banner, and server.py /privacy; stale bearer-key line fixed.
6. MINOR: mid-session refill guidance + swallowed startup-claim errors. Fix: banner/402
   say "fund, then restart the proxy"; startup claim failure is surfaced.

Deliberately NOT changed: the Anthropic (/v1/messages) lane stays buffered (that is the
fix for a Claude Code concurrency deadlock). Only the copy was corrected there.

## What to check — be adversarial
A. **Money-safety of the new `_openai_stream`** (serve_ecash.py). Highest priority.
   - Can it double-spend, lose change, or settle twice? Compare to the proven Anthropic
     buffered path in `_messages`. Is finish_stream always called exactly once? Is the
     x-cash-change event peeled correctly so it is NOT replayed to the client (which
     would leak blinded change) AND is consumed by finish_stream?
   - On upstream error / partial stream / exception mid-buffer: is `pending` handled so
     the next request recovers (no lost change, no free inference)?
   - Does holding the lock only across upstream I/O (not client I/O) hold? Any path that
     writes to the client while holding `lock` (the deadlock we are avoiding)?
B. **Field passthrough** doesn't leak identity or bypass a cost bound. serve_ecash now
   forwards arbitrary client fields to wallet.chat -> server. Confirm the server still
   allowlist-filters (UPSTREAM_ALLOWED_FIELDS) so nothing dangerous reaches upstream, and
   that cost-bounding still happens before spend regardless of the new fields (e.g. a
   client sending huge `n`, `max_tokens`, or `tools` can't underpay). Local/free lane
   still works (no ecash attached for local/*).
C. **The fixes match the design** the site now tells users: does the Step-1 wallet framing
   + /quickstart accurately describe a flow that actually works end to end (deposit ->
   claim -> serve -> OpenAI/Anthropic SDK)? Any remaining place the site/docs promise
   something the code doesn't do?
D. **No frontend breakage**: web/app.js had the Base URL element removed; confirm no
   dangling element reference throws at runtime.
E. **Privacy claims** are now honest and consistent across index.html, /quickstart,
   /privacy, and the proxy banner.

## Deliverable
PASS or FAIL. If FAIL: the exact file:line, the concrete failure (inputs -> wrong
outcome), and the minimal fix. Rank by severity (blocker/major/minor). If PASS, say so
explicitly and note anything minor worth a follow-up. This gates a live testnet deploy.
