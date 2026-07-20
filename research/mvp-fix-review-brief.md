# Review round 2: verify the unlinkability fix set

You previously reviewed anon-router's design and found it did NOT deliver
payment-layer unlinkability. This round reviews the IMPLEMENTED fixes. Verify
each fix actually holds in the code (repo: /Users/clawbox/cleavelabs/anon-router;
diff: /tmp/mvp_fix.diff), and hunt for NEW bugs the changes introduced —
especially money-loss (double-issue of change, refund/spend races) and any
linkage the fixes failed to close or newly opened.

## What was changed (claims to verify against the code)

F1. Bearer-inference lane REMOVED (server.py chat()): an account key
    (`Bearer sk-anon-...`) can no longer pay for inference; the account is a
    funding rendezvous only (deposit/voucher -> /mint/claim -> ecash).

F2. Balance-less funding: client claims the FULL account balance to ecash at
    funding (wallet.claim_all, cli `claim` with no amount). The proxy's
    just-in-time auto-refill (claim right before a spend = the L1 pipeline) is
    REMOVED; serve_ecash claims once at startup, never per-request.

F3. In-band blinded change (Cashu NUT-08 style), replacing the separate
    /mint/change redeem endpoints (both DELETED):
    - client sends fixed-count (21) blinded blank outputs in `X-Cash-Change`
      with the spend; the mint signs the decompose(change) prefix and returns
      the signatures IN-BAND: response header `X-Cash-Change` (non-stream) or a
      trailing `event: x-cash-change` SSE event (stream).
    - deterministic `receipt_id = sha256(sorted token secrets)`; a lost response
      is recovered by re-presenting the same tokens with `X-Cash-Recover`
      (returns cached change; 404 if never spent so the client keeps the tokens;
      409 if in flight). Crash-recovered ('final', cost=0) receipts sign the
      blanks for a full refund on recovery.
    - server helpers: _receipt_id, _parse_change_blanks, _sign_change,
      _finalize_redeemed, _replay_change (server.py). Client: _make_change_blanks,
      _absorb_change, _recover_pending, finish_stream (wallet.py). pending spend
      persisted to wallet.json for recovery.

F4. Fixed voucher face values ($1/$5/$10/$20) enforced in admin.py; the client
    (wallet.redeem_voucher) tries the fixed set. Voucher redeemed via POST
    /mint/redeem with the code in the BODY (not the URL).

F5. Per-request connections: wallet httpx client uses
    max_keepalive_connections=0 + Connection: close so the router can't link a
    wallet's requests by TCP/TLS session. (Tor stays opt-in via --tor.)

F6. No status oracles: GET /mint/voucher/{code} and GET /mint/change/{id}
    REMOVED. Unknown vs already-redeemed vouchers return a uniform 400.
    Accounts store ONLY key_hash (raw api_key column dropped + migrated).
    Receipts + claims are purged after PURGE_TTL_SEC (_purge_expired); spent
    nullifiers are kept forever (double-spend safety).

## Money-safety invariants that MUST still hold (regression check)
- No double-charge and no double-ISSUE of change (the new risk: in-band change +
  recovery could sign change twice for one spend). Verify _finalize_redeemed +
  _replay_change caching prevents a second issuance.
- Cost is still bounded before spend; prepay must cover worst case; daily cap
  reserved; crash leaves a recoverable full refund.
- The X-Cash-Recover path must NEVER spend unspent tokens or run inference.

## Tests (all green locally)
tests/e2e_money_safety.py (9/9), tests/e2e_privacy.py (7/7),
tests/e2e_unlinkability.py (9/9), tests/test_confetti.py.

## Deliverable
1. For EACH of F1-F6: does the code deliver it? (yes / partial / no + why).
2. NEW bugs introduced (money-loss, double-issue, races, DoS), severity + the
   concrete failing scenario.
3. Any linkage the fix set STILL leaves that belongs in this MVP (vs roadmap).
4. PASS / FAIL verdict on the fix set. If FAIL, the minimal changes to pass.
