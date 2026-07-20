# Privacy model

The goal: **paying for AI inference is private** — the provider can't know who
paid, and payments can't be tied to a person. This is a layered property; here
is exactly what holds today and what is an upgrade.

## The three layers

| Layer | Question | Status (MVP) |
|---|---|---|
| **Payment** | Does the money link you to your requests? | **Private.** No account, no card, no KYC. The live **ecash lane** blind-signs your balance, so spends are *cryptographically* unlinkable to your deposit at the signature layer. Caveat: the custodial router still sees deposit and redemption amounts + timing, so deposit a common round amount and spend over time to avoid statistical correlation. |
| **Transport** | Does the network path link you? | **Onion live.** The router is reachable as a Tor v3 `.onion`, so you can connect without revealing an IP and with no exit node in the path. Over clearnet the router still sees your IP; IPs are not logged (`--no-access-log`) and auth is stateless (no cookies/sessions), so nothing server-side links requests *beyond the bearer key you present* — reusing one key ties its requests to one balance, so rotate keys (free, `/account/new`) to unlink. |
| **Funding** | Does the money's origin link you? | **Pseudonymous.** The deposit is a public on-chain transaction. Fund from a fresh wallet, deposit a common round amount, and spend over time so the deposit doesn't fingerprint your usage; shielded-pool funding (hides the source in an anonymity set) is on the roadmap. |
| **Content** | Does the model see your prompt? | **Yes — inherently.** A hosted model must read your prompt to answer. The router adds no logging, but the model provider processes the text. For content privacy, use the **local/self-hosted model lane** (`local/*`). |
| **Custody** | Who holds your prepaid balance? | **The router (custodial).** This is a custody risk, not a privacy leak — keep balances small. The confetti (non-custodial) lane removes it; it is on the roadmap. |

## Why payment-layer-only is a real MVP

Every mainstream inference API requires an account and a card — your legal
identity, permanently attached to every prompt. Removing that is the single
biggest privacy gain available, and it is done. The provider only ever sees the
router, never you. For the common threat model ("I don't want a card tying my
prompts to my identity"), this is already a meaningful product.

It is **not** full anonymity: a router that logs, or an adversary watching your
network, can still correlate. We state this plainly rather than overclaim.

## What we hardened for the MVP (cheap, high-impact)

- **Stateless, sessionless auth.** Bearer key or per-request channel payment in
  headers — no cookies, no login, no server session that links requests.
- **No IP logging.** Run with `uvicorn --no-access-log`; the app never reads
  `X-Forwarded-For` or stores client IPs. Responses are `no-store`.
- **Key rotation.** Keys are free (`/account/new`); rotate per session so a
  single key doesn't tie your sessions together. The channel lane is already
  per-request unlinkable by construction.

## Upgrade path (roadmap, designed-for)

1. **Onion service + Tor guidance** — closes the transport layer. Router runs as
   a `.onion`; per-request circuits make requests unlinkable at the network too.
2. **Shielded-pool funding** — the deposit consumes a shielded note inside the
   proof (confetti spec's own extension), hiding the source among all pool users.
3. **Per-request key derivation** — client-side key rotation as the default.

None of these require changes to the payment layer, which is already the
strongest part. They plug in at transport (deployment) and funding (`open()` /
`CreditVault.deposit()`).

`GET /privacy` returns this posture machine-readably.
