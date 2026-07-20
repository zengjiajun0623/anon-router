# Review round 9: confirm the round-8 fixes (single-instance target)

Round 8 (single-process FAIL) had 3 residuals: two NaN/malformed-usage money-loss
paths and one legacy-row strictness item. Verify against CURRENT code (repo
/Users/clawbox/cleavelabs/anon-router; diff /tmp/mvp_fix_v8.diff). Target: SINGLE
uvicorn worker.

## Round-8 findings and fixes

R8-1 **`_num` accepted NaN/Infinity -> `math.ceil(NaN)` raises / inf blows the cap,
   post-spend.** `_num` now returns None for non-finite floats (`math.isfinite`
   check) as well as non-numbers and bools. `_usd_to_credits(nan|inf)` -> 1
   (floor), `_billed_usd` -> reserved; no billing path raises. Unit-verified:
   `_num('bad'|nan|inf|True) -> None`, `0.5 -> 0.5`.

R8-2 **Streaming: malformed `usage` (e.g. `"usage":"bad"`) threw inside the parse
   `try`, SKIPPING the content/`produced` scan -> full paid output billed at the
   1-credit floor (near-free inference).** Cost extraction and the content scan
   are now INDEPENDENT: JSON parse is its own try; `usage` is used only if it's a
   dict; the `choices`/`produced` scan runs regardless, guarded by isinstance. A
   malformed `usage` can no longer hide that paid output was delivered.

R8-3 **Migration-nullable `change_key` on pre-deploy `redeemed` rows (strictness,
   NOT money-loss).** New receipts bind `change_key` under the CAS and 409 on a
   blanks mismatch. Legacy rows (settled before this deploy) have NULL
   `change_key`; a mismatched-blanks recovery of one returns the cached sigs. This
   is NOT money-loss: the cached sigs are blind signatures cryptographically bound
   to the ORIGINAL blinds (C = S - r_orig*K); a caller with different `r` unblinds
   to invalid tokens that the mint rejects at spend and the wallet's self-heal
   drops. No change is created, none lost, no double-issue. Denying legacy
   recovery outright would instead strand legit in-flight-at-deploy receipts, so
   we keep best-effort return-cached for legacy rows and strict 409 for all new
   ones. Confirm this reasoning (crypto-bound => harmless) rather than requiring a
   behavior change.

## Deliverable
Per R8 finding: closed / not-closed with code evidence (and for R8-3, confirm or
refute the crypto-bound harmlessness argument). Any NEW single-process money-loss.
A clear PASS/FAIL for the SINGLE-instance testnet-alpha target; treat multi-worker
HA as the known Postgres gate. If genuinely money-safe for single-instance, say
PASS. Green locally: e2e_money_safety 13/13, e2e_privacy 7/7, e2e_unlinkability
17/17, test_confetti, streaming; `_num` NaN/inf unit-checked.
