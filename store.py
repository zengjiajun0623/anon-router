"""Async data store for the router's state.

Production: PostgreSQL (asyncpg pool) via DATABASE_URL — real multi-worker
concurrency, no single-writer lock, point-in-time recovery. Dev/tests: aiosqlite
(a file) when DATABASE_URL is unset. One interface either way, so server.py has
no backend-specific code.

Safety under concurrency comes from SQL, not an app lock: the `spent(secret)`
primary key rejects a double-spend from any worker, balance debits use atomic
`UPDATE ... WHERE balance >= ?` conditionals, and multi-statement invariants run
inside `async with store.tx()` transactions. Postgres serializes these across
workers; the concurrency test proves it.

Portability rules honored by callers:
  * `?` placeholders everywhere (translated to $1..$n for asyncpg).
  * No SQL `date('now')` etc. — compute values in Python, pass as params.
  * Catch `store.UniqueViolation` (not a backend-specific error) for conflicts.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith("postgres")

# Portable schema — types chosen to mean the same in SQLite and Postgres.
SCHEMA = [
    "CREATE TABLE IF NOT EXISTS spent(secret TEXT PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, prepaid BIGINT, "
    "cost BIGINT, state TEXT, change_sigs TEXT, ts BIGINT)",
    "CREATE TABLE IF NOT EXISTS vouchers(code TEXT PRIMARY KEY, credits BIGINT, state TEXT)",
    "CREATE TABLE IF NOT EXISTS accounts(api_key TEXT PRIMARY KEY, key_hash TEXT UNIQUE, "
    "balance BIGINT)",
    "CREATE TABLE IF NOT EXISTS seen_deposits(txhash TEXT PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS claims(idem_key TEXT PRIMARY KEY, response TEXT)",
    "CREATE TABLE IF NOT EXISTS spend_ledger(day TEXT PRIMARY KEY, usd DOUBLE PRECISION)",
]


class UniqueViolation(Exception):
    """Raised on a primary-key / unique-constraint conflict, backend-agnostic."""


def _to_pg(sql: str) -> str:
    """`?` positional placeholders -> `$1..$n` for asyncpg."""
    n = 0

    def repl(_m):
        nonlocal n
        n += 1
        return f"${n}"

    return re.sub(r"\?", repl, sql)


# ---------- Postgres backend ----------

class _PgTx:
    def __init__(self, con):
        self._con = con

    async def execute(self, sql: str, *params):
        try:
            await self._con.execute(_to_pg(sql), *params)
        except Exception as e:  # asyncpg.exceptions.UniqueViolationError et al.
            _raise_unique(e)

    async def fetchone(self, sql: str, *params):
        row = await self._con.fetchrow(_to_pg(sql), *params)
        return tuple(row.values()) if row is not None else None

    async def fetchval(self, sql: str, *params):
        return await self._con.fetchval(_to_pg(sql), *params)


class _PgStore:
    def __init__(self, url: str):
        self._url = url
        self._pool = None

    async def connect(self):
        import asyncpg
        self._pool = await asyncpg.create_pool(self._url, min_size=1, max_size=10)
        async with self._pool.acquire() as con:
            for ddl in SCHEMA:
                await con.execute(ddl)
            await _migrate_pg(con)

    async def execute(self, sql: str, *params):
        async with self._pool.acquire() as con:
            try:
                await con.execute(_to_pg(sql), *params)
            except Exception as e:
                _raise_unique(e)

    async def fetchone(self, sql: str, *params):
        async with self._pool.acquire() as con:
            row = await con.fetchrow(_to_pg(sql), *params)
            return tuple(row.values()) if row is not None else None

    async def fetchval(self, sql: str, *params):
        async with self._pool.acquire() as con:
            return await con.fetchval(_to_pg(sql), *params)

    def tx(self):
        store = self

        class _Ctx:
            async def __aenter__(self):
                self._con = await store._pool.acquire()
                self._t = self._con.transaction()
                await self._t.start()
                return _PgTx(self._con)

            async def __aexit__(self, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        await self._t.commit()
                    else:
                        await self._t.rollback()
                finally:
                    await store._pool.release(self._con)
                return False

        return _Ctx()

    async def close(self):
        if self._pool is not None:
            await self._pool.close()


def _raise_unique(e: Exception):
    name = type(e).__name__
    if "UniqueViolation" in name or "IntegrityError" in name:
        raise UniqueViolation(str(e)) from e
    raise


async def _migrate_pg(con):
    for col, typ in (("change_sigs", "TEXT"), ("ts", "BIGINT")):
        try:
            await con.execute(f"ALTER TABLE receipts ADD COLUMN {col} {typ}")
        except Exception:
            pass  # column exists


# ---------- SQLite backend (dev/tests) ----------

class _SqliteTx:
    def __init__(self, db):
        self._db = db

    async def execute(self, sql: str, *params):
        try:
            await self._db.execute(sql, params)
        except Exception as e:
            _raise_unique(e)

    async def fetchone(self, sql: str, *params):
        cur = await self._db.execute(sql, params)
        return await cur.fetchone()

    async def fetchval(self, sql: str, *params):
        cur = await self._db.execute(sql, params)
        row = await cur.fetchone()
        return row[0] if row else None


class _SqliteStore:
    def __init__(self, path: str):
        self._path = path
        self._db = None
        import asyncio
        self._lock = asyncio.Lock()  # one aiosqlite connection -> serialize

    async def connect(self):
        import aiosqlite
        self._db = await aiosqlite.connect(self._path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        for ddl in SCHEMA:
            await self._db.execute(ddl)
        for col, typ in (("change_sigs", "TEXT"), ("ts", "BIGINT")):
            try:
                await self._db.execute(f"ALTER TABLE receipts ADD COLUMN {col} {typ}")
            except Exception:
                pass
        await self._db.commit()

    async def execute(self, sql: str, *params):
        async with self._lock:
            try:
                await self._db.execute(sql, params)
                await self._db.commit()
            except Exception as e:
                await self._db.rollback()
                _raise_unique(e)

    async def fetchone(self, sql: str, *params):
        async with self._lock:
            cur = await self._db.execute(sql, params)
            return await cur.fetchone()

    async def fetchval(self, sql: str, *params):
        async with self._lock:
            cur = await self._db.execute(sql, params)
            row = await cur.fetchone()
            return row[0] if row else None

    def tx(self):
        store = self

        class _Ctx:
            async def __aenter__(self):
                await store._lock.acquire()
                return _SqliteTx(store._db)

            async def __aexit__(self, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        await store._db.commit()
                    else:
                        await store._db.rollback()
                finally:
                    store._lock.release()
                return False

        return _Ctx()

    async def close(self):
        if self._db is not None:
            await self._db.close()


def make_store():
    if IS_PG:
        return _PgStore(DATABASE_URL)
    path = os.environ.get("STATE_DB_PATH",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.db"))
    return _SqliteStore(path)
