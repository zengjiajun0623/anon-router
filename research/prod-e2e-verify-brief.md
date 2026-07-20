# Verify the LIVE production deployment end to end

The unlinkability/money-safety fix set (commit 2667d65) is DEPLOYED to prod and
passed a dual Codex+Kimi code review. This task VERIFIES the running deployment
end to end — not the code in the abstract, the actual live service.

## Live target
- Router: https://anon-router-production.up.railway.app  (Sepolia testnet alpha,
  custodial, faucet OFF, channel lane OFF, DAILY_USD_CAP=$10)
- Repo (for reference): /Users/clawbox/cleavelabs/anon-router
- Client: `.venv/bin/python` with the repo on sys.path, or the `anon-router` CLI.
  `from wallet import Wallet; w = Wallet(mint_url="<router>", path="<fresh temp>")`

## What to verify (end to end, against the LIVE router)
1. **Health**: GET /healthz -> ok, watcher alive, faucet=false, channel_lane=false.
2. **Voucher onboarding**: redeem a voucher (body, not URL) -> ecash lands in the
   wallet. Confirm GET /mint/voucher/<code> is NOT an oracle (removed).
3. **Real paid inference**: chat via ecash -> a REAL OpenRouter reply, with change
   returned IN-BAND (X-Cash-Change on the response, NO X-Change-Receipt header,
   NO /mint/change call). Confirm change tokens are then spendable.
4. **No re-linkage / privacy**: there is no bearer-inference lane (an account key
   can't buy inference -> 402); spend requires X-Cash-Change blanks (400 without).
5. **Money-safety spot checks** (live, non-destructive where possible):
   - double-spend of the same tokens is rejected;
   - a spend retried with the SAME tokens replays change (no double-charge);
   - X-Cash-Recover on never-spent tokens -> 404 (no spend, no inference);
   - multimodal input (image_url) rejected 400 pre-spend.
6. **Migration integrity** (if you have railway access; else skip): accounts table
   is key_hash-only with balances preserved; receipts have change_key; vouchers
   have redeem_key.

## Test vouchers (LIVE on prod — each single-use; pick ONE, note which you used)
- ar-SDmhqO28m6w6ocNT22wq   ($5 = 50000 credits)
- ar-ubgV-xkroa7YhjN9NHdK   ($5 = 50000 credits)   [reserve/second reviewer]

## Deliverable
Report what you actually observed live (status codes, the real model reply, the
in-band change payload, balance deltas). State clearly whether the deployed
service delivers the MVP end to end (voucher -> ecash -> real AI, unlinkable
in-band change, no free-inference/oracle/re-link paths). PASS/FAIL on the LIVE
deployment. If FAIL, the exact failing step + observed vs expected.
