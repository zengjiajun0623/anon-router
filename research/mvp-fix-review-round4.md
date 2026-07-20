# Review round 4: confirm the round-3 fixes converged

Round 3 (Codex FAIL, Kimi partial) surfaced 8 real issues, all now fixed.
Re-verify against the CURRENT code (repo: /Users/clawbox/cleavelabs/anon-router;
diff: /tmp/mvp_fix_v3.diff) that each is closed and no new money-loss was
introduced. This is a convergence pass — the code has been through three review
rounds; focus on whether anything money-losing remains.

## Round-3 findings and their fixes (verify each)

R3-1 **Live-stream vs stale-sweep double-issue.** `_finalize_redeemed` is now the
   SINGLE issuance point: it returns `(won, cached_sigs)` — `won` True only if its
   CAS transitioned pending->redeemed. Callers reconcile the daily cap ONLY if
   `won` and return the returned `cached` sigs, never their own local set. Plus
   `MAX_STREAM_TOTAL_SEC` is now CLAMPED below `RECEIPT_STALE_SEC` so a live stream
   can never age into the sweep even with a bad env override.

R3-2 **Streaming `_settle` set `done` before commit.** `done` is now set AFTER the
   settle commits; the `finally` reschedules a detached `_settle` if not done, so
   a cancel mid-commit still bills (idempotent via the CAS). Upstream-error
   streams now full-refund (cost 0), not a 1-credit min-charge.

R3-3 **Off-curve change blank -> `_sign_change` fails after burn -> free
   inference (Kimi NEW-1, Critical).** `_parse_change_blanks` now curve-validates
   every B_ with `ec.decompress` BEFORE the spend. Test: e2e_unlinkability `F3c`.

R3-4 **Daily-cap reservation double-release / leak on stale recovery.** The sweep
   now flips each receipt AND releases its reservation in ONE transaction guarded
   by the per-receipt CAS (rowcount==1), so only the winner releases and a crash
   can't leak it.

R3-5 **Voucher lost-response destroyed value.** `vouchers` now cache `sigs` +
   `redeem_key` (a hash of the exact blinds); a retry with the SAME blinds gets
   the cached sigs (idempotent recovery), DIFFERENT blinds get a uniform 400 (no
   re-issue, no oracle). Client retries with identical blinds. Tests: F4 replay +
   F4/F6 diff-blinds 400.

R3-6 **Wallet/browser overwrote unresolved pending.** `_recover_pending` now
   RAISES on 409 AND on transport error; `open_stream` raises on 409 without
   restoring tokens; browser `redeemPendingChange` throws on 409 so `send()`
   aborts. Tokens are only restored on a genuine pre-spend rejection.

R3-7 **Recover never runs inference / never spends unspent tokens** — verify still
   holds (handled at the top of `chat()` before all routing; spend+receipt
   atomic so receipt-exists IFF spent, making the 404 restore safe).

R3-8 **Browser bearer-key polling relinked spend<->account.** Replaced the forever
   2.5s poll with `watchFunding()` that runs only while a deposit credits/drains
   and STOPS once the balance is zero.

## Deliverable
For each R3-1..R3-8: closed / not-closed (code evidence). Any NEW money-loss or
linkage introduced by the round-3 fixes. A PASS/FAIL verdict; if FAIL, the
minimal remaining changes. Tests green locally: e2e_money_safety (11/11),
e2e_privacy (7/7), e2e_unlinkability (17/17), test_confetti, streaming roundtrip.
