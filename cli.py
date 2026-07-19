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

    sub.add_parser("balance")

    c = sub.add_parser("chat")
    c.add_argument("message")
    c.add_argument("--model", default="openai/gpt-4o-mini")
    c.add_argument("--prepay", type=int, default=2000)

    m = sub.add_parser("models")
    m.add_argument("--search", default=None)

    args = p.parse_args()
    w = Wallet(args.url) if args.url else Wallet()

    if args.cmd == "topup":
        bal = w.topup(args.credits)
        usd = bal * w.keys()["credit_usd"]
        print(f"balance: {bal} credits (${usd:.4f})")
    elif args.cmd == "balance":
        bal = w.balance()
        print(f"balance: {bal} credits (${bal * w.keys()['credit_usd']:.4f})")
    elif args.cmd == "chat":
        reply, settle = w.chat(
            [{"role": "user", "content": args.message}],
            model=args.model,
            prepay=args.prepay,
        )
        print(reply["choices"][0]["message"]["content"])
        credit_usd = w.keys()["credit_usd"]
        cost = settle["cost"]
        print(
            f"\n[cost: {cost} credits (${(cost or 0) * credit_usd:.6f}) · "
            f"change returned: {settle['change']} · balance: {w.balance()}]",
            file=sys.stderr,
        )
    elif args.cmd == "models":
        data = w.http.get(f"{w.url}/v1/models").json()["data"]
        for entry in data:
            if args.search and args.search.lower() not in entry["id"].lower():
                continue
            print(entry["id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
