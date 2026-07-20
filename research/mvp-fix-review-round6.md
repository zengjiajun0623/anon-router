# Review round 6: confirm the round-5 fixes converged

Round 5 (Codex FAIL) found that a request outliving RECEIPT_STALE_SEC could let
the sweep finalize first, after which the live finalizer returned change with
EMPTY signatures while clients cleared pending -> permanent loss; plus a few
narrower client paths. All addressed by two structural changes + client fixes.
Verify against CURRENT code (repo /Users/clawbox/cleavelabs/anon-router; diff
/tmp/mvp_fix_v5.diff).

## Round-5 findings and fixes

R5-CRITICAL **Stale-finalizer race: empty-sig change + refunded free inference.**
   TWO structural fixes:
   (1) Total wall-clock deadlines so no live request can outlive
   RECEIPT_STALE_SEC: non-stream upstream call wrapped in
   `asyncio.wait_for(..., MAX_STREAM_TOTAL_SEC)`; the streaming loop reads each
   line via `asyncio.wait_for(_it.__anext__(), min(remaining, read_timeout))`,
   bounding BOTH total age and a trickle-without-newline upstream.
   (2) Settlement consolidated into ONE function `_settle_receipt(receipt_id,
   billed_cost, blanks, ...)` that reads the receipt state under the write lock
   and ALWAYS returns valid signatures over the caller's blanks:
     pending  -> bill, sign change, reconcile cap (atomic), cache.
     final    -> sweep already refunded (cost 0); SIGN the full refund over these
                 blanks (never empty), cache. This is why a live finalizer that
                 lost to the sweep still returns valid change.
     redeemed -> return cached (idempotent).
   `_replay_change` now just 409-guards then delegates to `_settle_receipt`.

R5-R4-1 **Malformed non-200 / non-object 200.** Non-200 `.json()` is now wrapped
   in try/except; a 200 body that isn't a dict is treated as malformed -> full
   refund via the same `_refund()` -> `_settle_receipt`.

R5-R4-3 **watchFunding suppression race.** `awaitCreditMode` is now a SHARED flag:
   a deposit that fires while a page-load watcher runs flips the running loop into
   wait-through-mining mode instead of being suppressed by the early return.

R5-browser **redeemPendingChange only threw on 409.** Now throws on ANY non-404,
   non-ok status (409 or 5xx), so `send()` aborts and never overwrites an
   unresolved pending record.

## Invariants (must hold)
- At-most-once change issuance, ALWAYS with valid (non-empty) signatures, across:
  concurrent recovery, live-finalizer-vs-stale-sweep, and crash.
- No live request outlives RECEIPT_STALE_SEC (deadlines derived + enforced).
- No free inference; every burned token finalized; off-curve blanks rejected
  pre-spend; upstream/malformed failures full-refund in-band.
- Cost bounded pre-spend; daily cap reserved + reconciled atomically with finalize.
- Lost response recoverable without re-run or double-charge; voucher/claim
  idempotency durable.

## Deliverable
Per R5 finding: closed / not-closed with code evidence. Any NEW money-loss /
free-inference / availability introduced. PASS/FAIL; if FAIL, minimal remaining
changes. Green locally: e2e_money_safety (12/12), e2e_privacy (7/7),
e2e_unlinkability (17/17), test_confetti, streaming roundtrip.
