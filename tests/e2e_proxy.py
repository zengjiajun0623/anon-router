"""E2E of the LOCAL PROXY (serve_ecash.run_proxy) after the design-vs-code fixes.

Verifies the two proxy fixes that no other suite covers:
  - OpenAI field passthrough: tools/tool_choice/response_format reach upstream
    (the old 5-field whitelist silently dropped them).
  - Real streaming: `stream:true` on the OpenAI lane replays genuine upstream
    chunks (content + tool_call deltas + [DONE]), not a synthesized single chunk.

Self-contained: a streaming, tool-echoing mock upstream + the real router + the
real run_proxy, all on localhost.  python tests/e2e_proxy.py
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from wallet import Wallet  # noqa: E402
from serve_ecash import run_proxy  # noqa: E402

PORT, UP, PROXY = 8416, 9416, 8417
SEEN = {"tools": None, "response_format": None, "stream": None}


class Up(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        out = json.dumps({"data": [{"id": "openai/gpt-4o-mini",
            "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        # record what actually reached the upstream (proves passthrough)
        SEEN["tools"] = body.get("tools")
        SEEN["response_format"] = body.get("response_format")
        SEEN["stream"] = bool(body.get("stream"))
        if body.get("stream"):
            return self._sse()
        out = json.dumps({
            "id": "x", "object": "chat.completion", "model": "openai/gpt-4o-mini",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "echo"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.00002}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        def frame(obj):
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
            self.wfile.flush()

        cid = "chatcmpl-stream"
        frame({"id": cid, "object": "chat.completion.chunk", "model": "openai/gpt-4o-mini",
               "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]})
        frame({"id": cid, "object": "chat.completion.chunk", "model": "openai/gpt-4o-mini",
               "choices": [{"index": 0, "delta": {"content": "lo"}}]})
        # a tool-call delta — the thing the old fake single-chunk path threw away
        frame({"id": cid, "object": "chat.completion.chunk", "model": "openai/gpt-4o-mini",
               "choices": [{"index": 0, "delta": {"tool_calls": [
                   {"index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"}}]}}]})
        frame({"id": cid, "object": "chat.completion.chunk", "model": "openai/gpt-4o-mini",
               "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        frame({"id": cid, "object": "chat.completion.chunk", "model": "openai/gpt-4o-mini",
               "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 6, "cost": 0.00002}})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> int:
    srv = HTTPServer(("127.0.0.1", UP), Up)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    home = os.path.join(ROOT, ".e2e_proxy_home")
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home)
    env = {**os.environ, "OPENROUTER_API_KEY": "rk", "UPSTREAM": f"http://127.0.0.1:{UP}/v1",
           "DEV_FAUCET": "1", "CHANNEL_LANE_ENABLED": "0",
           "STATE_DB_PATH": os.path.join(home, "state.db"),
           "MINT_MASTER_PATH": os.path.join(home, "mint.hex"), "MAX_REQUEST_USD": "0.50"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--no-access-log"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{PORT}"
    for _ in range(40):
        try:
            if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.25)

    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    try:
        # fund a wallet with spendable ecash and run the REAL proxy against it
        w = Wallet(mint_url=base, path=os.path.join(home, "w.json"))
        w.topup(50000)
        threading.Thread(target=run_proxy,
                         kwargs={"wallet": w, "host": "127.0.0.1", "port": PROXY},
                         daemon=True).start()
        purl = f"http://127.0.0.1:{PROXY}"
        for _ in range(40):
            try:
                if httpx.get(f"{purl}/healthz", timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.25)

        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "get weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        }]

        # 1) non-streaming with tools + response_format -> both reach upstream
        SEEN.update(tools=None, response_format=None, stream=None)
        r = httpx.post(f"{purl}/v1/chat/completions", json={
            "model": "openai/gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": tools, "tool_choice": "auto",
            "response_format": {"type": "json_object"}}, timeout=30)
        check("non-stream request succeeds through the proxy (200)", r.status_code == 200)
        check("tools passed through to upstream (not dropped)",
              isinstance(SEEN["tools"], list) and SEEN["tools"][0]["function"]["name"] == "get_weather")
        check("response_format passed through to upstream",
              SEEN["response_format"] == {"type": "json_object"})

        # 2) streaming -> real upstream chunks replayed, incl. tool_call delta + [DONE]
        SEEN.update(tools=None, response_format=None, stream=None)
        with httpx.stream("POST", f"{purl}/v1/chat/completions", json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "stream please"}],
                "tools": tools, "stream": True}, timeout=30) as resp:
            check("stream request returns SSE (200)",
                  resp.status_code == 200
                  and "text/event-stream" in resp.headers.get("content-type", ""))
            datas = []
            for line in resp.iter_lines():
                s = line.strip() if isinstance(line, str) else line.decode(errors="ignore").strip()
                if s.startswith("data: "):
                    datas.append(s[6:])
        check("upstream actually saw stream=true (real stream, not faked from non-stream)",
              SEEN["stream"] is True)
        check("stream carried tools too", isinstance(SEEN["tools"], list))
        check("multiple content chunks arrived (not one synthesized chunk)",
              sum(1 for d in datas if d != "[DONE]" and '"content"' in d) >= 2)
        joined = " ".join(datas)
        check("tool_call delta preserved in the stream (old path dropped it)",
              "tool_calls" in joined and "get_weather" in joined)
        check("terminal [DONE] emitted", "[DONE]" in datas)
        # every non-[DONE] data line must be valid JSON (well-formed SSE)
        wellformed = all(_is_json(d) for d in datas if d != "[DONE]")
        check("every streamed data line is valid JSON", wellformed)
        # the in-band x-cash-change event (blinded change signatures) must be
        # peeled by the proxy and settled locally, NEVER replayed to the client.
        leaked = any(("signatures" in d or "\"change\"" in d or "x-cash-change" in d)
                     for d in datas)
        check("blinded change event is NOT leaked into the client stream", not leaked)
        check("no 'event: x-cash-change' line reached the client",
              "x-cash-change" not in joined)

        # 3) balance was actually spent (proxy really paid ecash)
        check("proxy spent ecash (wallet balance dropped below the funded 50000)",
              w.balance() < 50000)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)

    print("\nPROXY E2E:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _is_json(s):
    try:
        json.loads(s)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
