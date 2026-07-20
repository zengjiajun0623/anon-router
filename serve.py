"""anon-router prover daemon — a thin OpenAI-compatible endpoint backed by a
confetti channel with pipelined proving.

Run this on a box you trust (your VPS, desktop, or the 3080 PC); point any
OpenAI-compatible app at it with `base_url=http://<box>:<port>/v1`. The heavy
part of the confetti protocol — the ~28 GB / ~45 s STARK proof per payment —
lives HERE, not on the user's device, and is proven ahead of time so requests
feel instant as long as the box keeps a payment ready.

Privacy note: this daemon holds the channel secret and sees the payment witness,
so it must run somewhere the *user* trusts — their own box, or (later) a TEE
whose attestation they verify. Running it on the same operator as the router
would let that operator link payments; that is exactly what a self-hosted or
attested prover avoids.

  ANON_ROUTER_URL   upstream anon-router (default http://127.0.0.1:8402)
  ANON_DAEMON_KEY   if set, require `Authorization: Bearer <key>` from clients
  ANON_DEFAULT_MODEL fallback model when a request omits one
"""
from __future__ import annotations

import os
import threading

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from wallet import Wallet

DAEMON_KEY = os.environ.get("ANON_DAEMON_KEY", "")
DEFAULT_MODEL = os.environ.get("ANON_DEFAULT_MODEL", "openai/gpt-4o-mini")

app = FastAPI(title="anon-router prover daemon")
_w = Wallet()

# One channel = one sequential payment stream, so payments are serialized. The
# lock is held only briefly (spend + hand-off); the ~45 s proof runs in the
# background thread OUTSIDE the lock, so a waiting request blocks only for the
# time left on the in-flight proof — the pipelining behaviour, server-side.
_pay_lock = threading.Lock()
_prover = {"thread": None, "store": {}}   # background prove-ahead of the next payment


def _start_bg_prove() -> None:
    store: dict = {}

    def run():
        try:
            store["prepared"] = _w.channel_prove_next()
        except Exception as e:            # balance exhausted, prover missing, …
            store["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    _prover["thread"], _prover["store"] = t, store


def _next_prepared() -> dict:
    """The proven-ahead payment for the current tip: join the background prover
    if it is still running, else cold-prove now. Runs under _pay_lock."""
    cached = _w.prepared_ready()
    if cached is not None:
        return cached
    t = _prover["thread"]
    if t is not None:
        t.join()
        _prover["thread"] = None
        store = _prover["store"]
        if store.get("error"):
            raise store["error"]
        if store.get("prepared") is not None:
            return store["prepared"]
    return _w.channel_prove_next()        # cold path (first request / after error)


def _pay_and_reprove(messages: list[dict], model: str, **kwargs) -> dict:
    with _pay_lock:
        prepared = _next_prepared()
        reply, _settle = _w.channel_pay_prepared(prepared, messages, model=model, **kwargs)
        _start_bg_prove()                 # prove the next payment while the user reads
        return reply


def _require_auth(request: Request) -> None:
    if not DAEMON_KEY:
        return
    header = request.headers.get("authorization", "")
    if header != f"Bearer {DAEMON_KEY}":
        raise HTTPException(401, "missing or invalid daemon key")


@app.on_event("startup")
def _warm() -> None:
    # Prove the first payment ahead of the first request, if a channel exists.
    try:
        _w.channel_status()
    except Exception:
        return                            # no channel open yet; first request cold-proves
    _start_bg_prove()


@app.get("/healthz")
def healthz() -> dict:
    try:
        s = _w.channel_status()
    except Exception as e:
        return {"ok": False, "channel": None, "error": str(e)}
    ready = _w.prepared_ready() is not None or (
        _prover["thread"] is not None and not _prover["thread"].is_alive()
        and _prover["store"].get("prepared") is not None)
    return {"ok": True, "channel": s, "payment_ready": ready}


@app.get("/v1/models")
async def models():
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{_w.url}/v1/models")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/v1/chat/completions")
async def chat(request: Request):
    _require_auth(request)
    body = await request.json()
    messages = body.get("messages")
    if not messages:
        raise HTTPException(400, "messages required")
    model = body.get("model") or DEFAULT_MODEL
    passthrough = {k: v for k, v in body.items()
                   if k in ("temperature", "top_p", "max_tokens", "stop")}
    try:
        reply = await run_in_threadpool(_pay_and_reprove, messages, model, **passthrough)
    except RuntimeError as e:
        raise HTTPException(402, str(e))  # channel exhausted / not open
    return JSONResponse(reply)
