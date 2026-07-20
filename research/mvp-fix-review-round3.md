# Review round 3: verify the round-2 fixes closed every finding

Round 2 (Kimi + Codex) returned FAIL with 13 findings. Every one has been
addressed. Re-verify against the CURRENT code (repo:
/Users/clawbox/cleavelabs/anon-router; full diff: /tmp/mvp_fix_v2.diff) that each
is actually closed, and that the fixes introduced no new money-loss.

## What changed since round 2 (map to the round-2 findings)

1. **Double-issue under concurrent recovery (Critical).** `_replay_change` now
   does read -> sign -> `UPDATE ... WHERE state='final'` -> `rowcount` check ALL
   under `db_write_lock`; on a lost race it returns the WINNER's cached sigs, so
   change is issued at most once. Test: e2e_money_safety `#5` fires two concurrent
   recovers with different blanks and asserts identical signatures.

2. **Live stream vs stale sweep double-settle (Critical).** Added
   `MAX_STREAM_TOTAL_SEC=600` hard ceiling on stream wall-clock, kept below
   `RECEIPT_STALE_SEC=900`, so a live stream can never age into the sweep.

3. **Recover 404 TOCTOU (High).** `_spend_and_open_receipt` inserts the spent
   nullifiers AND the receipt in ONE transaction, so receipt-exists IFF
   nullifiers-spent. A 404 therefore means the tokens are genuinely unspent and
   safe to restore. (Cross-process shared-wallet racing is out of scope for the
   single-user MVP; noted.)

4. **409 recovery overwriting pending (High).** `_recover_pending()` now RAISES on
   409, so `chat()`/`open_stream()` abort before starting a new spend that would
   overwrite the unresolved pending record.

5. **Wallet discarding full refunds on upstream error (High).** `wallet.chat`
   absorbs `X-Cash-Change` whenever the header is present, regardless of HTTP
   status, then surfaces the error. Tokens are restored ONLY on a pre-spend
   rejection (no change header, not 409).

6. **Streaming settlement after terminal marker + cancellation (High).** Upstream
   `[DONE]` is suppressed; on clean completion we emit the `x-cash-change` event
   THEN our own `[DONE]`. Finalization is in a `finally` that runs a DETACHED,
   idempotent `_settle()` so client cancel/disconnect still bills.

7. **SSE peeler accepts an upstream-forged payment event (High).** The router now
   strips ALL upstream `event:` lines and emits only its own `x-cash-change`; a
   forged change would also fail signature verification at spend time anyway.

8. **Voucher consumed without signatures (High).** `redeem_voucher` signs FIRST
   (pure, may 400 on a bad point) and only then CAS-marks redeemed; only the CAS
   winner returns sigs. Test: e2e_unlinkability `F4b` (malformed redeem 400, then
   the voucher is still redeemable).

9. **Purging claims reopened double-debit (High).** The purge task is REMOVED;
   claim idempotency rows and receipts are retained (they carry no spend-linkage
   the mint doesn't already hold in `spent`).

10. **X-Cash-Recover didn't suppress inference (Medium).** Recovery is now handled
    at the TOP of `chat()`, before free/channel/paid routing. Test:
    e2e_unlinkability `F1b` (recover header + model=local/* + unspent -> 404).

11. **receipt_id collisions (Medium).** `_receipt_id` hashes canonical JSON of the
    sorted secrets (no delimiter injection).

12. **Change input/signing unbounded (Medium).** `_parse_change_blanks` requires
    EXACTLY `len(DENOMS)` (21) blanks, each a 33-byte compressed-point hex, header
    <= 8 KB; prepaid capped below `2^len(DENOMS)`. Test: e2e_unlinkability `F3b`.

13. **Migrated NULL-ts records (Low).** Moot — purge removed. Also the daily-cap
    reservation is now persisted per-receipt (`res_day`/`res_usd`) and RELEASED by
    the stale-recovery sweep, closing the money-safety cap-reservation FAIL.

Browser: bearer-key polling no longer runs on a forever timer — `watchFunding()`
runs only while a deposit is crediting/claiming and STOPS once drained.

## Deliverable
Re-verify findings 1-13 are closed. Report any that are NOT, plus any NEW
money-loss/linkage the round-2 fixes introduced. Give a PASS/FAIL verdict.
Tests green locally: e2e_money_safety (11/11), e2e_privacy (7/7),
e2e_unlinkability (14/14), test_confetti, streaming roundtrip.
