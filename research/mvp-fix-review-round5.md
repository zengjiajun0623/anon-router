# Review round 5: confirm the round-4 fixes converged

Round 4 (Codex FAIL) found 4 issues in the round-3 fixes; all now addressed.
Verify against the CURRENT code (repo /Users/clawbox/cleavelabs/anon-router; diff
/tmp/mvp_fix_v4.diff) that each is closed and no new money-loss was introduced.
This is a convergence pass after 4 rounds — focus strictly on remaining
money-loss / free-inference / availability paths.

## Round-4 findings and fixes (verify each)

R4-1 **Post-spend failure stranding tokens.** (a) Server: `data = r.json()` (and
   the whole post-spend path) is now guarded — a malformed 200 body, an upstream
   non-200, or a transport error all route through a single `_refund()` that
   FULL-refunds in-band on the same response (never a bare 500). (b) Client:
   `wallet.chat` / `wallet.open_stream` now restore tokens ONLY on a pre-spend
   rejection (400/402); on 409/5xx/anything ambiguous they keep `pending` and
   recover on the next call (never restore possibly-spent tokens). Same rule in
   web/app.js `send()`. Test: e2e_money_safety `#6` (malformed 200 -> 502 with a
   full in-band refund).

R4-2 **Daily-cap overstatement on cancellation.** Finalize + cap-reconcile now
   happen in ONE transaction inside `_finalize_redeemed` (which takes
   res_day/reserved/actual and adjusts spend_ledger atomically with the receipt
   CAS). A cancel between the two writes is now impossible; only the CAS winner
   adjusts the ledger, so no double-reconcile and no leak.

R4-3 **`watchFunding` quit before a deposit mined.** `watchFunding(awaitCredit)`:
   after a deposit it keeps polling THROUGH the initial zero balance until credit
   arrives and is drained, then stops. Page-load still drains any leftover once
   and stops (no idle bearer polling).

R4-4 **Stale-threshold clamp didn't bound the misconfig.** The ordering is now
   DERIVED, not clamped: `RECEIPT_STALE_SEC = max(configured,
   MAX_STREAM_TOTAL_SEC + STREAM_READ_TIMEOUT_S + 120)`, so a live stream's max
   age (total ceiling + one idle read) is always below the stale threshold
   regardless of any env override.

## Money-safety invariants (still must hold)
- At-most-once change issuance (concurrent recovery + stream-vs-sweep).
- No free inference (every burned token is finalized; off-curve blanks rejected
  pre-spend; upstream/ malformed failures full-refund).
- Cost bounded before spend; daily cap reserved and reconciled atomically.
- Lost response recoverable without re-running inference or double-charging.
- Voucher + claim idempotency durable.

## Deliverable
For each R4-1..R4-4: closed / not-closed (code evidence). Any NEW money-loss or
linkage introduced. PASS/FAIL verdict; if FAIL, the minimal remaining changes.
Tests green locally: e2e_money_safety (12/12), e2e_privacy (7/7),
e2e_unlinkability (17/17), test_confetti, streaming roundtrip.
