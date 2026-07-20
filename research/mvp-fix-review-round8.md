# Review round 8: confirm the round-7 fixes (single-instance target)

Round 7 (Codex single-process FAIL) found 3 concrete money-loss edge cases; all
fixed. Verify against CURRENT code (repo /Users/clawbox/cleavelabs/anon-router;
diff /tmp/mvp_fix_v7.diff). Deployment target: SINGLE uvicorn worker (testnet
alpha). Multi-worker HA = separate Postgres gate (CAS already made the settle
cross-process sound, per your round-7 note).

## Round-7 findings and fixes

R7-1 **Streaming header/connect phase outside the total deadline (trickling
   headers -> outlive RECEIPT_STALE_SEC -> free inference).** The stream is now
   opened via `client.send(build_request(...), stream=True)` wrapped in
   `asyncio.wait_for(..., MAX_STREAM_TOTAL_SEC)`, so the header/connect phase is
   under the same total budget as the body loop and the non-200 read. On timeout
   we mark `upstream_failed` (full refund) and close via `finally: r.aclose()`.

R7-2 **Nested `usage.cost` unchecked -> `{"usage":{"cost":"bad"}}` TypeError after
   spend -> stale-refund free inference.** Added `_num()` coercion; `_usd_to_credits`
   and `_billed_usd` treat any non-number cost as missing (charge the floor /
   reserved), so no billing path can raise on a malformed cost. The non-stream
   path also type-checks `usage` (dict-or-None) before `.get`.

R7-3 **Different-blinds recovery returned unusable sigs (and the CLI could drop a
   whole token set).** The receipt now binds to the FIRST settled blanks
   (`change_key = hash(blanks)`, new column, written under the CAS). On an
   already-settled receipt, a recovery whose blanks DON'T match gets a clean 409
   ("change already issued to the original request's outputs") instead of sigs it
   can't unblind. A legitimate client persists and re-sends identical blanks, so
   it always matches and gets its cached change. This also stops the wallet from
   ever absorbing (then self-heal-dropping) unusable change tokens. Tests:
   e2e_money_safety #5a (same blanks -> identical sigs, issued once) + #5b
   (different blanks -> 409).

## Single-process invariants to confirm
- At-most-once change issuance, always valid non-empty sigs (concurrent recovery,
  live-vs-sweep, crash).
- No live request outlives RECEIPT_STALE_SEC on EITHER lane (connect+header, body,
  non-200 read all bounded).
- No billing path raises after the spend (malformed/absent/typed-wrong usage).
- Cost bound pre-spend; daily cap reserved + reconciled atomically with settle.
- Lost response recoverable (same blanks) without re-run/double-charge; voucher +
  claim idempotency durable.

## Deliverable
Per R7 finding: closed / not-closed with code evidence. Any NEW single-process
money-loss. Clear PASS/FAIL for the SINGLE-instance testnet-alpha target (note,
but don't block on, pure multi-worker-HA items). Green locally: e2e_money_safety
13/13, e2e_privacy 7/7, e2e_unlinkability 17/17, test_confetti, streaming.
