// ecash.js — BDHKE blind-signature (Cashu-style) client for anon-router.
// Mirrors ec.py + mint.py byte-for-byte: same curve math, same hash_to_curve
// domain/counter scheme, same compress/decompress rules. Pure BigInt
// secp256k1, no dependencies, no external loads. Works as a browser module
// (served same-origin at /ecash.js) and under Node >= 20 (global fetch +
// webcrypto) for the test harness.
//
// Flow (see mint.py):
//   wallet: secret s -> Y = hash_to_curve(s); pick r; B_ = Y + r*G  -> send B_
//   mint:   C_ = k_d * B_                                           -> C_
//   wallet: C = C_ - r*K_d   token = (d, s, C), spent via X-Cash header.

// ---- secp256k1 (mirror of ec.py) ----

const P = (1n << 256n) - (1n << 32n) - 977n;
export const N = BigInt('0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141');
export const G = [
  BigInt('0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798'),
  BigInt('0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8'),
];

const mod = (a, m) => ((a % m) + m) % m;

function modpow(base, exp, m) {
  let r = 1n;
  base = mod(base, m);
  while (exp > 0n) {
    if (exp & 1n) r = (r * base) % m;
    base = (base * base) % m;
    exp >>= 1n;
  }
  return r;
}

const inv = (a) => modpow(mod(a, P), P - 2n, P); // Fermat; P is prime

// Points are [x, y] BigInt pairs; null is the point at infinity.
export function ptAdd(p1, p2) {
  if (p1 === null) return p2;
  if (p2 === null) return p1;
  const [x1, y1] = p1, [x2, y2] = p2;
  let m;
  if (x1 === x2) {
    if (mod(y1 + y2, P) === 0n) return null;
    m = mod(3n * x1 * x1 * inv(2n * y1), P);
  } else {
    m = mod((y2 - y1) * inv(x2 - x1), P);
  }
  const x3 = mod(m * m - x1 - x2, P);
  return [x3, mod(m * (x1 - x3) - y1, P)];
}

export function ptMul(k, pt) {
  k = mod(k, N);
  let result = null;
  let addend = pt;
  while (k > 0n) {
    if (k & 1n) result = ptAdd(result, addend);
    addend = ptAdd(addend, addend);
    k >>= 1n;
  }
  return result;
}

export function ptNeg(pt) {
  return pt === null ? null : [pt[0], mod(P - pt[1], P)];
}

// ---- byte helpers ----

export function hexToBytes(hex) {
  hex = hex.replace(/^0x/, '');
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(2 * i, 2 * i + 2), 16);
  return out;
}

export function bytesToHex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}

const bytesToBig = (bytes) => BigInt('0x' + (bytesToHex(bytes) || '0'));

function bigTo32(n) {
  return hexToBytes(n.toString(16).padStart(64, '0'));
}

function concatBytes(...arrs) {
  const out = new Uint8Array(arrs.reduce((s, a) => s + a.length, 0));
  let off = 0;
  for (const a of arrs) { out.set(a, off); off += a.length; }
  return out;
}

function randBytes(n) {
  const a = new Uint8Array(n);
  crypto.getRandomValues(a);
  return a;
}

async function sha256(bytes) {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
}

// ---- compress / decompress (mirror of ec.py) ----

export function compress(pt) {
  if (pt === null) throw new Error('cannot compress point at infinity');
  const [x, y] = pt;
  const prefix = (y % 2n === 0n) ? '02' : '03';
  return prefix + x.toString(16).padStart(64, '0');
}

export function decompress(data) {
  if (data.length !== 33 || (data[0] !== 2 && data[0] !== 3)) throw new Error('bad compressed point');
  const x = bytesToBig(data.slice(1));
  if (x >= P) throw new Error('x out of range');
  const ySq = mod(modpow(x, 3n, P) + 7n, P);
  let y = modpow(ySq, (P + 1n) / 4n, P);
  if ((y * y) % P !== ySq) throw new Error('x not on curve');
  if ((y % 2n === 0n) !== (data[0] === 2)) y = P - y;
  return [x, y];
}

// ---- BDHKE (mirror of mint.py) ----

const HTC_DOMAIN = new TextEncoder().encode('anon-router-htc-v1');
export const DENOMS = Array.from({ length: 21 }, (_, i) => 1 << i); // 1 .. 1,048,576

export async function hashToCurve(msgBytes) {
  for (let counter = 0; counter < 65536; counter++) {
    const ctr = new Uint8Array(4);
    new DataView(ctr.buffer).setUint32(0, counter, true); // little-endian, as in mint.py
    const digest = await sha256(concatBytes(HTC_DOMAIN, msgBytes, ctr));
    try {
      return decompress(concatBytes(new Uint8Array([2]), digest));
    } catch (e) { /* not a valid x; bump counter */ }
  }
  throw new Error('hash_to_curve: no valid point found');
}

export function decompose(amount) {
  const out = [];
  for (const d of [...DENOMS].reverse()) {
    while (amount >= d) { out.push(d); amount -= d; }
  }
  return out;
}

export async function blind(secret) {
  const y = await hashToCurve(new TextEncoder().encode(secret));
  let r = bytesToBig(randBytes(32)) % N;
  if (r === 0n) r = 1n;
  const blinded = ptAdd(y, ptMul(r, G));
  return { B_: compress(blinded), r };
}

export function unblind(signedHex, r, mintPubHex) {
  const signed = decompress(hexToBytes(signedHex));
  const rTimesK = ptMul(r, decompress(hexToBytes(mintPubHex)));
  return compress(ptAdd(signed, ptNeg(rTimesK)));
}

// ---- wallet ops against the router ----

/** Blind fresh secrets covering `amount`. The returned data is JSON-safe so a
 *  caller can persist it before posting and reuse it verbatim on retry. */
export async function prepareMint(amount) {
  const blinds = [];
  for (const denom of decompose(amount)) {
    const secret = bytesToHex(randBytes(32)); // = secrets.token_hex(32)
    const { B_, r } = await blind(secret);
    blinds.push({ amount: denom, secret, r: '0x' + r.toString(16), B_ });
  }
  return blinds;
}

/** Post previously prepared blinded outputs and unblind their signatures. */
export async function submitMint(base, endpoint, blinds, headers, pubkeys) {
  const resp = await fetch(base + endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(headers || {}) },
    body: JSON.stringify({ outputs: blinds.map((b) => ({ amount: b.amount, B_: b.B_ })) }),
  });
  if (!resp.ok) throw new Error(endpoint + ' -> ' + resp.status + ': ' + await resp.text());
  const sigs = (await resp.json()).signatures;
  return blinds.map((b, i) => ({
    amount: b.amount,
    secret: b.secret,
    C: unblind(sigs[i].C_, BigInt(b.r), pubkeys[String(b.amount)]),
  }));
}

/** Blind fresh secrets covering `amount`, post to `endpoint`, unblind the
 *  returned signatures. Returns tokens: [{amount, secret, C}]. */
export async function mintTokens(base, endpoint, amount, headers, pubkeys) {
  return submitMint(base, endpoint, await prepareMint(amount), headers, pubkeys);
}

/** Pick tokens covering >= amount, largest first (change comes back from the
 *  router). Returns {spend, keep}; throws if the wallet can't cover it. */
export function selectTokens(tokens, amount) {
  const sorted = [...tokens].sort((a, b) => b.amount - a.amount);
  const spend = [];
  let total = 0;
  for (const t of sorted) {
    if (total >= amount) break;
    spend.push(t);
    total += t.amount;
  }
  if (total < amount) {
    const have = tokens.reduce((s, t) => s + t.amount, 0);
    throw new Error(`insufficient ecash: have ${have}, need ${amount}`);
  }
  const keep = sorted.slice(spend.length);
  return { spend, keep };
}

/** Encode tokens for the X-Cash header: base64(JSON [{amount, secret, C}]). */
export function encodeCash(tokens) {
  return btoa(JSON.stringify(tokens.map((t) => ({ amount: t.amount, secret: t.secret, C: t.C }))));
}

/** Fixed-count blinded blank outputs to send WITH a spend (in-band change,
 *  Cashu NUT-08 style). One per denomination so the header never encodes the
 *  change amount. JSON-safe so a caller can persist them for crash recovery. */
export async function prepareChangeBlanks() {
  const blanks = [];
  for (const denom of DENOMS) {
    const secret = bytesToHex(randBytes(32));
    const { B_, r } = await blind(secret);
    blanks.push({ amount: denom, secret, r: '0x' + r.toString(16), B_ });
  }
  return blanks;
}

/** Header value for X-Cash-Change: base64(JSON [{B_}]). */
export function encodeChange(blanks) {
  return btoa(JSON.stringify(blanks.map((b) => ({ B_: b.B_ }))));
}

/** Unblind the in-band change signatures (from the X-Cash-Change response header
 *  or the trailing SSE event) into spendable tokens. `sigs` is a prefix of
 *  `blanks`; each sig carries its assigned amount. Returns {cost, change, tokens}. */
export function absorbChange(payload, blanks, pubkeys) {
  const sigs = (payload && payload.signatures) || [];
  const tokens = sigs.map((sig, i) => ({
    amount: sig.amount,
    secret: blanks[i].secret,
    C: unblind(sig.C_, BigInt(blanks[i].r), pubkeys[String(sig.amount)]),
  }));
  return { cost: (payload && payload.cost) || 0, change: (payload && payload.change) || 0, tokens };
}

/** Redeem a voucher into ecash. The code goes in the BODY (never the URL); the
 *  server rejects a wrong face value, so we send the standard full decomposition. */
export async function redeemVoucher(base, code, credits, pubkeys) {
  const blinds = await prepareMint(credits);
  const resp = await fetch(base + '/mint/redeem', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, outputs: blinds.map((b) => ({ amount: b.amount, B_: b.B_ })) }),
  });
  if (!resp.ok) throw new Error('redeem -> ' + resp.status + ': ' + await resp.text());
  const sigs = (await resp.json()).signatures;
  return blinds.map((b, i) => ({
    amount: b.amount, secret: b.secret,
    C: unblind(sigs[i].C_, BigInt(b.r), pubkeys[String(b.amount)]),
  }));
}
