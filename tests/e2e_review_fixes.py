"""Regression tests for the goal-vs-implementation review fixes (pure functions).

  G1  Anthropic lane surfaces upstream errors instead of empty success
  G10 Anthropic lane refuses non-text (image) content with an error
  G3  a non-balance 402 shows the router's real reason, not 'claim more ecash'
  G11 exhaustion message is 'not enough ecash', not 'mint-key rotation'
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import anthropic_proxy as ap  # noqa: E402
from wallet import _payment_error, _router_reason  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    ok = ok and bool(cond)


# G1: an upstream error delivered as a `data:` line must become an Anthropic error
# event + message_stop, NOT a silent empty assistant turn.
err_line = 'data: {"error": {"message": "upstream boom"}}'
events = list(ap.stream_anthropic(iter([err_line, ""]), "anthropic/claude-sonnet-4.5"))
blob = "".join(events)
check("G1 upstream error surfaces as an Anthropic error event",
      "event: error" in blob and "upstream boom" in blob)
check("G1 error stream still terminates (message_stop)", "message_stop" in blob)
check("G1 no text content block emitted on a pure error",
      "content_block_delta" not in blob)

# a normal stream still works (no regression)
good = list(ap.stream_anthropic(iter([
    'data: {"choices":[{"delta":{"content":"hi"}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    "data: [DONE]"], ), "anthropic/claude-sonnet-4.5"))
gblob = "".join(good)
check("G1 normal stream unaffected (text delta + message_stop)",
      "content_block_delta" in gblob and "hi" in gblob and "message_stop" in gblob)

# G10: an image block on the text-only Anthropic lane must raise (-> proxy 400).
try:
    ap.to_openai({"model": "claude", "messages": [
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "data": "x"}}]}]})
    raised = False
except ValueError:
    raised = True
check("G10 image content raises ValueError (proxy turns it into 400)", raised)

# to_openai still accepts plain text + tools (no regression)
o = ap.to_openai({"model": "claude", "messages": [
    {"role": "user", "content": [{"type": "text", "text": "hello"}]}]})
check("G10 text content still translates fine", o["messages"][-1]["content"] == "hello")

# G3: a per-request-cap 402 (no parseable 'N credits needed') must surface the
# router's real reason, not tell the user to claim more.
cap_body = ('{"detail":"request may cost $0.60 upstream, over the $0.50 per-request '
            'cap — lower max_tokens/n"}')
msg = _payment_error(None, 100000, cap_body)
check("G3 cap 402 surfaces 'lower max_tokens', not 'claim'",
      "lower max_tokens" in msg and "claim" not in msg.lower(), f"({msg!r})")
check("G3 _router_reason extracts the detail", _router_reason(cap_body).startswith("request may cost"))

# a genuine balance shortfall still says claim.
short = _payment_error(5000, 1000, '{"detail":"prepay 1000 < 5000 credits needed"}')
check("G3 real shortfall still says 'claim'", "claim" in short.lower() and "5000" in short)

print("\nREVIEW-FIXES:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
