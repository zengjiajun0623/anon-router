#!/usr/bin/env python3
"""anon-router CLI.

  python cli.py topup 50000
  python cli.py balance
  python cli.py chat "hello" --model openai/gpt-4o-mini
  python cli.py models --search qwen
"""
import argparse
import json
import os
import sys
import threading
import time

from wallet import Wallet


def channel_repl(w: Wallet, model: str) -> int:
    """Interactive confetti-channel chat with pipelined proving: after each
    reply, the next payment proves in the background while you read/type, so
    only the first message waits on the ~45s prover."""
    credit_usd = w.keys()["credit_usd"]
    print("confetti channel · pipelined proving. Only the first message waits on "
          "the prover; after that the next payment proves while you read.\n"
          "Ctrl-D or /exit to quit.\n", file=sys.stderr)

    prepared = w.prepared_ready()
    if prepared is None:
        print("proving first payment (SP1 STARK, ~45s)…", file=sys.stderr, flush=True)
        t0 = time.time()
        try:
            prepared = w.channel_prove_next()
        except RuntimeError as e:
            print(f"cannot start channel: {e}", file=sys.stderr)
            return 1
        print(f"ready in {time.time() - t0:.0f}s. Ask anything.\n", file=sys.stderr)
    else:
        print("first payment already proven (pipelined from last session). "
              "Ask anything.\n", file=sys.stderr)

    history: list[dict] = []
    bg: dict = {}
    prove_thread: threading.Thread | None = None

    while True:
        try:
            msg = input("you› ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            break
        if not msg:
            continue
        if msg in ("/exit", "/quit"):
            break

        # If the next payment is still proving, wait for it (partial hiding:
        # felt latency = max(0, prove_time - think_time)).
        if prove_thread is not None:
            if prove_thread.is_alive():
                print("(finishing payment proof…)", file=sys.stderr, flush=True)
            wait0 = time.time()
            prove_thread.join()
            waited = time.time() - wait0
            if bg.get("error"):
                print(f"[channel closed: {bg['error']}]", file=sys.stderr)
                break
            prepared = bg["prepared"]
            if waited > 0.5:
                print(f"(waited {waited:.0f}s for proof)", file=sys.stderr)

        history.append({"role": "user", "content": msg})
        try:
            reply, settle = w.channel_pay_prepared(prepared, history, model=model)
        except Exception as e:
            print(f"[payment failed: {e}]", file=sys.stderr)
            history.pop()
            break
        content = reply["choices"][0]["message"]["content"]
        history.append({"role": "assistant", "content": content})
        print(content + "\n", flush=True)
        print(f"[cost {settle['cost']} (${settle['cost'] * credit_usd:.6f}) · "
              f"remaining {settle['remaining']}]\n", file=sys.stderr)

        # Prove the NEXT payment in the background while the user reads/types.
        prepared, bg = None, {}
        def _prove(store=bg):
            try:
                store["prepared"] = w.channel_prove_next()
            except Exception as e:  # balance exhausted, prover gone, etc.
                store["error"] = e
        prove_thread = threading.Thread(target=_prove, daemon=True)
        prove_thread.start()

    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="anon-router")
    p.add_argument("--url", default=None, help="router URL (default env/localhost)")
    p.add_argument("--tor", action="store_true",
                   help="route over Tor SOCKS 127.0.0.1:9050 to the .onion (needs tor running)")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("topup", help="dev faucet topup")
    t.add_argument("credits", type=int)

    r = sub.add_parser("redeem", help="redeem a purchased voucher code")
    r.add_argument("code")

    sub.add_parser("account", help="create/show your anonymous account key (for on-chain deposits)")

    d = sub.add_parser("deposit", help="deposit ETH on-chain to fund your account, then wait for credit")
    d.add_argument("eth", type=float)
    d.add_argument("--key", default=None,
                   help="funding EOA key FILE (JSON with private_key); default "
                        ".sepolia-deployer.json or $ANON_DEPOSIT_KEY. Never pass a raw key.")
    d.add_argument("--rpc", default=None,
                   help="EVM RPC URL (default $ANON_RPC or Sepolia public)")

    cl = sub.add_parser("claim", help="convert account balance -> unlinkable ecash tokens")
    cl.add_argument("amount", type=int)

    sub.add_parser("balance")

    c = sub.add_parser("chat")
    c.add_argument("message", nargs="?", default=None,
                   help="one-shot message; omit with --channel for an "
                        "interactive pipelined session")
    c.add_argument("--model", default="openai/gpt-4o-mini")
    c.add_argument("--prepay", type=int, default=2000)
    c.add_argument("--channel", action="store_true",
                   help="pay via confetti channel instead of ecash tokens")

    ch = sub.add_parser("channel", help="confetti trust-minimized payment channel")
    chs = ch.add_subparsers(dest="channel_cmd", required=True)
    cho = chs.add_parser("open")
    cho.add_argument("credits", type=int)
    chs.add_parser("status")

    sv = sub.add_parser("serve", help="run a thin OpenAI-compatible prover daemon "
                        "(point any app at http://host:port/v1)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8788)

    m = sub.add_parser("models")
    m.add_argument("--search", default=None)

    args = p.parse_args()
    ONION = "http://buudenzevnvddmb7crzki7daedc7p4hnsvint66xtala2y2bs3afobad.onion"
    url = args.url or (ONION if args.tor else None)
    w = Wallet(mint_url=url, tor=args.tor) if url else Wallet(tor=args.tor)

    if args.cmd == "topup":
        bal = w.topup(args.credits)
        usd = bal * w.keys()["credit_usd"]
        print(f"balance: {bal} credits (${usd:.4f})")
    elif args.cmd == "redeem":
        bal = w.redeem_voucher(args.code)
        usd = bal * w.keys()["credit_usd"]
        print(f"voucher redeemed. balance: {bal} credits (${usd:.4f})")
    elif args.cmd == "account":
        acct = w.account or w.new_account()
        print(f"account key: {acct['api_key']}")
        print(f"key hash:    {acct['key_hash']}")
    elif args.cmd == "deposit":
        # Funding key from a FILE or $ANON_DEPOSIT_KEY only — never a raw hex on
        # the command line (it would leak via shell history / process listing).
        keyfile = args.key or ".sepolia-deployer.json"
        if os.path.isfile(keyfile):
            key_hex = json.load(open(keyfile))["private_key"]
        elif os.environ.get("ANON_DEPOSIT_KEY"):
            key_hex = os.environ["ANON_DEPOSIT_KEY"]
        else:
            p.error(f"funding key not found: keyfile {keyfile!r} missing and "
                    "$ANON_DEPOSIT_KEY unset (pass --key <file>, don't paste a raw key)")
        rpc = args.rpc or os.environ.get("ANON_RPC", "https://ethereum-sepolia-rpc.publicnode.com")
        if not w.account:
            w.new_account()
            print("minted a fresh anonymous account", file=sys.stderr)
        before = w.account_status()["balance"]   # credit THIS deposit against the delta
        print(f"depositing {args.eth} ETH on-chain…", file=sys.stderr)
        res = w.deposit_onchain(args.eth, key_hex, rpc)
        target = before + res["expected_credits"]
        print(f"deposit tx {res['tx']} — waiting for watcher credit…", file=sys.stderr)
        bal = before
        for _ in range(60):
            bal = w.account_status()["balance"]
            if bal >= target:
                break
            time.sleep(5)
        ok = "✓" if bal >= target else "…(still crediting)"
        print(f"{ok} account balance: {bal} credits (+{bal - before} this deposit, "
              f"${bal * w.keys()['credit_usd']:.4f}). Now: cli.py claim {res['expected_credits']}",
              file=sys.stderr)
    elif args.cmd == "claim":
        if not w.account:
            p.error("no account; run: cli.py deposit <eth> first")
        bal = w.claim_from_account(w.account["api_key"], args.amount)
        print(f"claimed {args.amount} credits to ecash. spendable wallet balance: "
              f"{bal} credits (${bal * w.keys()['credit_usd']:.4f})")
    elif args.cmd == "balance":
        bal = w.balance()
        print(f"balance: {bal} credits (${bal * w.keys()['credit_usd']:.4f})")
    elif args.cmd == "chat":
        if args.channel and args.message is None:
            return channel_repl(w, model=args.model)
        if args.message is None:
            p.error("chat needs a message (or use --channel for interactive mode)")
        msgs = [{"role": "user", "content": args.message}]
        credit_usd = w.keys()["credit_usd"]
        if args.channel:
            reply, settle = w.channel_chat(msgs, model=args.model)
            print(reply["choices"][0]["message"]["content"])
            print(
                f"\n[channel · cost: {settle['cost']} credits "
                f"(${settle['cost'] * credit_usd:.6f}) · "
                f"remaining: {settle['remaining']}]",
                file=sys.stderr,
            )
        else:
            reply, settle = w.chat(msgs, model=args.model, prepay=args.prepay)
            print(reply["choices"][0]["message"]["content"])
            cost = settle["cost"]
            print(
                f"\n[cost: {cost} credits (${(cost or 0) * credit_usd:.6f}) · "
                f"change returned: {settle['change']} · balance: {w.balance()}]",
                file=sys.stderr,
            )
    elif args.cmd == "channel":
        if args.channel_cmd == "open":
            info = w.channel_open(args.credits)
            print(f"channel open: deposit {info['deposit']} credits, "
                  f"price {info['price']} credits/request")
        elif args.channel_cmd == "status":
            s = w.channel_status()
            print(f"deposit {s['deposit']} · spent {s['spent']} · "
                  f"remaining {s['remaining']} · payments {s['payments']}")
    elif args.cmd == "serve":
        import uvicorn
        os.environ.setdefault("ANON_ROUTER_URL", w.url)
        print(f"prover daemon → router {w.url}\n"
              f"point any OpenAI app at http://{args.host}:{args.port}/v1", file=sys.stderr)
        uvicorn.run("serve:app", host=args.host, port=args.port, log_level="warning")
    elif args.cmd == "models":
        data = w.http.get(f"{w.url}/v1/models").json()["data"]
        for entry in data:
            if args.search and args.search.lower() not in entry["id"].lower():
                continue
            print(entry["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
