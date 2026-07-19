#!/usr/bin/env python3
"""anon-router CLI.

  python cli.py topup 50000
  python cli.py balance
  python cli.py chat "hello" --model openai/gpt-4o-mini
  python cli.py models --search qwen
"""
import argparse
import json
import sys

from wallet import Wallet


def main() -> int:
    p = argparse.ArgumentParser(prog="anon-router")
    p.add_argument("--url", default=None, help="router URL (default env/localhost)")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("topup", help="dev faucet topup")
    t.add_argument("credits", type=int)

    r = sub.add_parser("redeem", help="redeem a purchased voucher code")
    r.add_argument("code")

    sub.add_parser("balance")

    c = sub.add_parser("chat")
    c.add_argument("message")
    c.add_argument("--model", default="openai/gpt-4o-mini")
    c.add_argument("--prepay", type=int, default=2000)
    c.add_argument("--channel", action="store_true",
                   help="pay via confetti channel instead of ecash tokens")

    ch = sub.add_parser("channel", help="confetti trust-minimized payment channel")
    chs = ch.add_subparsers(dest="channel_cmd", required=True)
    cho = chs.add_parser("open")
    cho.add_argument("credits", type=int)
    chs.add_parser("status")

    m = sub.add_parser("models")
    m.add_argument("--search", default=None)

    args = p.parse_args()
    w = Wallet(args.url) if args.url else Wallet()

    if args.cmd == "topup":
        bal = w.topup(args.credits)
        usd = bal * w.keys()["credit_usd"]
        print(f"balance: {bal} credits (${usd:.4f})")
    elif args.cmd == "redeem":
        bal = w.redeem_voucher(args.code)
        usd = bal * w.keys()["credit_usd"]
        print(f"voucher redeemed. balance: {bal} credits (${usd:.4f})")
    elif args.cmd == "balance":
        bal = w.balance()
        print(f"balance: {bal} credits (${bal * w.keys()['credit_usd']:.4f})")
    elif args.cmd == "chat":
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
    elif args.cmd == "models":
        data = w.http.get(f"{w.url}/v1/models").json()["data"]
        for entry in data:
            if args.search and args.search.lower() not in entry["id"].lower():
                continue
            print(entry["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
