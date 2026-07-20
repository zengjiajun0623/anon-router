#!/usr/bin/env python3
"""Operator tool: issue and list credit vouchers. Runs locally against state.db.

  python admin.py issue 50000            # one $5 voucher
  python admin.py issue 50000 --count 10 # a batch to sell
  python admin.py list
"""
import argparse
import os
import secrets
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def _credit_usd() -> float:
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path):
            if line.strip().startswith("CREDIT_USD="):
                return float(line.split("=", 1)[1].strip())
    return 0.0001


def main() -> int:
    # Use the SAME database the router reads (STATE_DB_PATH — /data/state.db in
    # prod). Defaulting to ROOT/state.db wrote vouchers to a DB the server never
    # sees, so redemptions failed with "unknown voucher".
    db = sqlite3.connect(os.environ.get("STATE_DB_PATH", os.path.join(ROOT, "state.db")))
    db.execute(
        "CREATE TABLE IF NOT EXISTS vouchers(code TEXT PRIMARY KEY, credits INT, state TEXT)"
    )
    p = argparse.ArgumentParser(prog="anon-router-admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("issue", help="create sellable voucher codes")
    i.add_argument("credits", type=int)
    i.add_argument("--count", type=int, default=1)
    sub.add_parser("list", help="show all vouchers and their state")
    args = p.parse_args()

    if args.cmd == "issue":
        usd = _credit_usd()
        for _ in range(args.count):
            code = "ar-" + secrets.token_urlsafe(15)
            db.execute("INSERT INTO vouchers VALUES (?, ?, 'issued')", (code, args.credits))
            print(f"{code}  ({args.credits} credits, ${args.credits * usd:.2f})")
        db.commit()
    elif args.cmd == "list":
        for code, credits, state in db.execute(
            "SELECT code, credits, state FROM vouchers ORDER BY rowid DESC"
        ):
            print(f"{state:9} {credits:>8}  {code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
