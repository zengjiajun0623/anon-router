# Review round 7: confirm the round-6 fixes

Round 6 (Codex FAIL) findings and their fixes. Deployment target is a
SINGLE-instance testnet alpha (`start.sh` runs one uvicorn worker); multi-worker
HA is the separately-tracked Postgres migration gate. Verify against CURRENT code
(repo /Users/clawbox/cleavelabs/anon-router; diff /tmp/mvp_fix_v6.diff).

## Round-6 findings and fixes

R6-1 **No state CAS in `_settle_receipt` (multi-process double-issue + cap
   corruption).** RESTORED guarded CAS on every transition: pending->redeemed
   `WHERE state='pending'`, final->redeemed `WHERE state='final'`; issuance +
   ledger delta run ONLY on rowcount==1; a lost CAS re-reads and returns the
   winner's canonical sigs. So change is issued once and the cap adjusted once
   even across worker processes (SQLite serializes the conditional UPDATE), not
   only under the in-process lock. NOTE: today's deploy is single-worker, so this
   was latent; CAS makes it correct under HA too.

R6-2 **Streaming total deadline incomplete (non-200 body read outside the
   deadline).** The trickling non-200 `r.aread()` is now wrapped in
   `asyncio.wait_for(remaining)`. The header wait is bounded by the httpx read
   timeout (STREAM_READ_TIMEOUT_S) + connect. Per-line reads already use
   wait_for. So no streaming phase can outlive RECEIPT_STALE_SEC.

R6-3 **Object-shaped malformed 200 (`{"usage":"bad"}`) -> bare 500 after spend.**
   The non-stream path now type-checks `usage` (dict-or-None) inside the guarded
   block; any malformed body routes to `_refund()` (full in-band refund), never a
   500. The streaming chunk parse already catches AttributeError.

R6-4 **Different-blinds recovery returns sigs invalid for the caller (High).**
   With CAS, change is issued exactly once, bound to the winner's blinds. A
   legitimate client persists and re-sends the SAME blinds, so it always matches.
   A different-blinds caller (shared wallet across devices — out of the
   single-user MVP model) receives the canonical sigs which its `r` values can't
   unblind; those tokens are dropped by the wallet's self-healing at next spend
   (the mint rejects invalid signatures). No double-issue, no corruption; the
   legitimate flow is unaffected. Documented as inherent single-issue behavior.

R6-5 **Slow requests truncated by the deadline (Low, explicit tradeoff).**
   MAX_STREAM_TOTAL_SEC=600 (10 min). Requests over that are force-settled +
   refunded. Accepted tradeoff to keep the sweep-race invariant; tune the env if
   longer generations are needed.

## Deliverable
Per R6 finding: closed / not-closed (code evidence). Any NEW money-loss. A clear
PASS/FAIL for the SINGLE-instance testnet-alpha target (treat multi-worker HA as
the known Postgres gate, not a blocker here — but do flag any SINGLE-process
money-loss). Green locally: e2e_money_safety 12/12, e2e_privacy 7/7,
e2e_unlinkability 17/17, test_confetti, streaming roundtrip.
