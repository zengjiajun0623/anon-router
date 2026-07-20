// test_ecash_browser.mjs — proves the browser BDHKE port is correct against
// the LIVE router: account -> dev credit -> /mint/claim -> unblind in JS ->
// spend via X-Cash -> change -> double-spend rejected.
//
// The spend is the correctness check: the mint verifies k_d*hash_to_curve(s)
// == C server-side, so a 200 means the JS blind/unblind/hash_to_curve match
// the Python mint byte-for-byte.
//
// NOTE on model choice: local/* models are the FREE lane and the router
// short-circuits them before reading X-Cash, so a local spend would verify
// nothing. The token verification therefore runs on the paid lane
// (openai/gpt-4o-mini). See server.py chat(): free lane returns early.
//
// Run: node web/test_ecash_browser.mjs

import * as ecash from './ecash.js';

const BASE = process.env.ROUTER || 'http://127.0.0.1:8402';
const CREDIT_SECRET = process.env.CREDIT_SECRET || 'devsecret123';
const CLAIM = 5000;
const MODEL = 'openai/gpt-4o-mini';

let step = 0;
const ok = (msg) => console.log(`  ok ${++step}. ${msg}`);
function assert(cond, msg) {
  if (!cond) { console.error(`  FAIL: ${msg}`); process.exit(1); }
}

async function jfetch(path, opts) {
  const r = await fetch(BASE + path, opts);
  return { status: r.status, headers: r.headers, body: await r.json() };
}

// 1. mint keys
const keys = (await jfetch('/mint/keys')).body;
assert(keys.pubkeys && keys.pubkeys['1'], '/mint/keys returned pubkeys');
ok(`/mint/keys: ${Object.keys(keys.pubkeys).length} denominations, min_prepay ${keys.min_prepay}`);

// 2. fresh account
const acct = (await jfetch('/account/new', { method: 'POST' })).body;
assert(acct.api_key && acct.key_hash, '/account/new returned api_key + key_hash');
ok(`/account/new: key_hash ${acct.key_hash.slice(0, 18)}…`);

// 3. simulate a credited deposit (dev secret, no chain tx)
const txhash = '0xtest-ecash-browser-' + crypto.randomUUID();
const credit = await jfetch('/account/credit', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'X-Credit-Secret': CREDIT_SECRET },
  body: JSON.stringify({ key_hash: acct.key_hash, credits: CLAIM, txhash }),
});
assert(credit.body.status === 'credited', `credit status: ${JSON.stringify(credit.body)}`);
const st1 = (await jfetch('/account/status', { headers: { Authorization: 'Bearer ' + acct.api_key } })).body;
assert(st1.balance === CLAIM, `balance after credit = ${st1.balance}, want ${CLAIM}`);
ok(`/account/credit: balance ${st1.balance} credits (simulated deposit)`);

// 4. claim the balance as ecash: blind in JS, mint signs, unblind in JS
const wallet = await ecash.mintTokens(BASE, '/mint/claim', CLAIM, {
  Authorization: 'Bearer ' + acct.api_key,
  'Idempotency-Key': crypto.randomUUID(),
}, keys.pubkeys);
const minted = wallet.reduce((s, t) => s + t.amount, 0);
assert(minted === CLAIM, `minted ${minted}, want ${CLAIM}`);
const st2 = (await jfetch('/account/status', { headers: { Authorization: 'Bearer ' + acct.api_key } })).body;
assert(st2.balance === 0, `account balance after claim = ${st2.balance}, want 0`);
ok(`/mint/claim: ${wallet.length} tokens (${wallet.map((t) => t.amount).join('+')} = ${minted}), unblinded in JS, account drained to 0`);

// 5. spend via X-Cash on the paid lane — THE correctness check
const prepay = Math.max(2000, keys.min_prepay);
const { spend, keep } = ecash.selectTokens(wallet, prepay);
const xcash = ecash.encodeCash(spend);
const spent = spend.reduce((s, t) => s + t.amount, 0);
const chat = await jfetch('/v1/chat/completions', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'X-Cash': xcash },
  body: JSON.stringify({
    model: MODEL,
    messages: [{ role: 'user', content: 'Reply with exactly: ecash ok' }],
  }),
});
assert(chat.status === 200, `chat status ${chat.status}: ${JSON.stringify(chat.body).slice(0, 300)}`);
const reply = chat.body.choices?.[0]?.message?.content;
assert(reply && reply.length > 0, 'chat returned a non-empty reply');
ok(`spend accepted: mint verified ${spend.length} JS-unblinded tokens (${spent} credits) -> 200, reply "${reply.trim().slice(0, 60)}"`);

// 6. blind change comes back
const receipt = chat.headers.get('x-change-receipt');
assert(receipt, 'X-Change-Receipt header present');
const settle = await ecash.redeemChange(BASE, receipt, keys.pubkeys);
assert(settle.cost > 0, `cost ${settle.cost} > 0`);
const changeSum = settle.tokens.reduce((s, t) => s + t.amount, 0);
assert(changeSum === settle.change, `change tokens ${changeSum} == ${settle.change}`);
const final = keep.concat(settle.tokens).reduce((s, t) => s + t.amount, 0);
assert(final === CLAIM - settle.cost, `final wallet ${final} == ${CLAIM} - cost ${settle.cost}`);
ok(`change redeemed: cost ${settle.cost}, change ${settle.change}, wallet ${final} = ${CLAIM} - ${settle.cost}`);

// 7. double-spend must be rejected (proves the mint really checked + recorded)
const replay = await jfetch('/v1/chat/completions', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'X-Cash': xcash },
  body: JSON.stringify({ model: MODEL, messages: [{ role: 'user', content: 'hi' }] }),
});
assert(replay.status === 400 && /spent/.test(JSON.stringify(replay.body)),
  `replay rejected (got ${replay.status}: ${JSON.stringify(replay.body).slice(0, 120)})`);
ok('double-spend of the same tokens rejected (400 token already spent)');

console.log('\nPASS: browser BDHKE port verified against the live mint (claim -> unblind -> spend -> change -> no double-spend)');
