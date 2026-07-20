// app.js: anon-router demo frontend. UI and wiring only; the BDHKE wallet
// crypto lives in /ecash.js and is unchanged here.

import * as ecash from '/ecash.js';

let account = null;
let keys = null;         // /mint/keys payload (denomination pubkeys etc.)
let claiming = false;
let lastAcctBal = 0;
let fundingWatch = false;
let awaitCreditMode = false;
let inFlight = false;    // one chat request at a time

const $ = (id) => document.getElementById(id);

// ---- ecash wallet: blind-signed tokens held in this browser ----
const WALLET_KEY = 'anon-router-ecash-v1';
const PENDING_CLAIM_KEY = 'anon-router-pending-claim-v1';
const PENDING_CHANGE_KEY = 'anon-router-pending-change-v1';
const loadWallet = () => { try { return JSON.parse(localStorage.getItem(WALLET_KEY)) || []; } catch (e) { return []; } };
const saveWallet = (t) => localStorage.setItem(WALLET_KEY, JSON.stringify(t));
const ecashBalance = () => loadWallet().reduce((s, t) => s + t.amount, 0);

async function mintKeys() {
  if (!keys) keys = await (await fetch('/mint/keys')).json();
  return keys;
}

function renderBalance() {
  const eb = ecashBalance();
  const cu = keys ? keys.credit_usd : 0.0001;
  $('balance').textContent = (eb * cu).toFixed(2);
  $('bal-side').textContent = lastAcctBal > 0 ? 'Claiming your deposit…' : '';
  updateSendState();
}

// ---- chat send/empty/loading states ----

function updateSendState() {
  const send = $('send');
  const hint = $('chat-hint');
  const free = $('model').value.startsWith('local/');
  if (inFlight) {
    send.disabled = true;
    send.textContent = 'Sending…';
    return;
  }
  send.textContent = 'Send';
  if (!free && ecashBalance() <= 0) {
    send.disabled = true;
    hint.textContent = (lastAcctBal > 0 || claiming)
      ? 'Deposit received. Converting it to private credits…'
      : 'Deposit above to chat with paid models.';
    hint.classList.remove('hidden');
  } else {
    send.disabled = false;
    hint.classList.add('hidden');
  }
}

function unlockAfterKey() {
  $('key-start').classList.add('hidden');
  $('key-info').classList.remove('hidden');
  $('apikey').textContent = account.api_key;
  $('baseurl').textContent = account.base_url || '';
  $('deposit-locked').classList.add('hidden');
  $('deposit-body').classList.remove('hidden');
  renderDepositPreview();
  // Drain any balance left from a previous session once, then stop (no idle poll).
  watchFunding();
}

async function mint() {
  const btn = $('mint');
  btn.disabled = true;
  try {
    const r = await fetch('/account/new', { method: 'POST' });
    if (!r.ok) throw new Error('could not mint a key (' + r.status + '), try again');
    account = await r.json();
  } catch (e) {
    btn.disabled = false;
    walletNote(e.message);
    return;
  }
  unlockAfterKey();
}

async function claimToEcash(balance) {
  claiming = true;
  try {
    const k = await mintKeys();
    let pending = null;
    try { pending = JSON.parse(localStorage.getItem(PENDING_CLAIM_KEY)); } catch (e) {}
    if (pending && pending.keyHash !== account.key_hash) {
      throw new Error('a pending ecash claim belongs to a different session key');
    }
    if (!pending) {
      pending = {
        idempotencyKey: crypto.randomUUID(),
        keyHash: account.key_hash,
        blinds: await ecash.prepareMint(balance),
      };
      localStorage.setItem(PENDING_CLAIM_KEY, JSON.stringify(pending));
    }
    const minted = await ecash.submitMint('', '/mint/claim', pending.blinds, {
      Authorization: 'Bearer ' + account.api_key,
      'Idempotency-Key': pending.idempotencyKey,
    }, k.pubkeys);
    saveWallet(loadWallet().concat(minted));
    localStorage.removeItem(PENDING_CLAIM_KEY);
    lastAcctBal = 0;
  } finally { claiming = false; }
}

// Balance-less funding + privacy: we do NOT poll the account with the bearer key
// on a forever timer — those account-key requests, interleaved with ecash spends
// over the same IP, would relink spending to the funding account. We only watch
// the account WHILE funding is in flight (a deposit is crediting or a claim is
// pending), draining it fully to ecash, then STOP until the next explicit fund.
// awaitCredit=true is used right after a deposit: keep polling through the
// initial zero balance (the tx isn't mined yet) until credit arrives and is
// drained, then stop. awaitCredit=false (page load) just drains any leftover
// once and stops immediately if there's nothing — no idle bearer polling.
async function watchFunding(awaitCredit = false) {
  // `awaitCreditMode` is a SHARED flag (not a local), so a deposit that fires
  // while a page-load watcher is already running flips the running loop into
  // "wait through mining" mode instead of being suppressed by the early return.
  if (awaitCredit) awaitCreditMode = true;
  if (fundingWatch || !account) return;
  fundingWatch = true;
  try {
    let creditedOnce = false;
    for (let i = 0; i < 160; i++) {  // ~6-7 min max, then give up until next fund
      let bal = 0;
      try {
        const r = await fetch('/account/status', { headers: { Authorization: 'Bearer ' + account.api_key } });
        bal = (await r.json()).balance;
      } catch (e) {}
      lastAcctBal = bal;
      if ((bal > 0 || localStorage.getItem(PENDING_CLAIM_KEY)) && !claiming) {
        await claimToEcash(bal);         // drain fully to ecash
        creditedOnce = true;
      }
      renderBalance();
      const idle = lastAcctBal === 0 && !localStorage.getItem(PENDING_CLAIM_KEY);
      // Stop once idle — but while awaiting a deposit, not until credit actually
      // arrived and was drained (so we don't quit before the tx mines).
      if (idle && (!awaitCreditMode || creditedOnce)) { awaitCreditMode = false; break; }
      await new Promise((res) => setTimeout(res, 2500));
    }
  } finally {
    fundingWatch = false;
  }
}

// ---- deposit ----

// Fixed demo rate on this testnet: credits_per_eth is a router constant, not
// a live ETH price feed. Real pricing (or USDC at 1:1) is a mainnet concern.
function renderDepositPreview() {
  const el = $('deposit-usd');
  if (!account || !account.credits_per_eth) { el.textContent = ''; return; }
  const raw = $('amount').value;
  const eth = parseFloat(raw);
  const cu = (keys && keys.credit_usd) || account.credit_usd || 0.0001;
  if (!isFinite(eth) || eth <= 0) { el.textContent = ''; return; }
  const usd = eth * account.credits_per_eth * cu;
  el.textContent = raw + ' ETH ≈ $' + usd.toFixed(2) + ' credit';
}

async function deposit() {
  if (!account) return;
  if (!window.ethereum) { alert('No browser wallet found. Install one like MetaMask to deposit.'); return; }
  const eth = $('amount').value;
  const wei = BigInt(Math.round(parseFloat(eth) * 1e18));
  const accts = await window.ethereum.request({ method: 'eth_requestAccounts' });
  const data = account.deposit_selector + account.key_hash.replace(/^0x/, '');
  await window.ethereum.request({
    method: 'eth_sendTransaction',
    params: [{ from: accts[0], to: account.vault_address, value: '0x' + wei.toString(16), data }],
  });
  watchFunding(true);  // wait through mining until this deposit credits + drains
}

// ---- chat ----

function add(role, text) {
  const el = document.createElement('div');
  el.className = 'msg ' + (role === 'user' ? 'u' : 'a');
  el.textContent = text;
  const log = $('log');
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function showErr(el, text) {
  el.classList.add('err');
  el.textContent = text;
}

/** Map raw failures to plain, honest messages. */
function friendly(status, raw, free) {
  const msg = typeof raw === 'string' ? raw : JSON.stringify(raw || '');
  if (/insufficient ecash/i.test(msg) || (status === 402 && /insufficient/i.test(msg))) {
    return 'Not enough credits for this request. Add credits to keep chatting.';
  }
  if (/daily budget/i.test(msg)) {
    return 'The demo reached its daily spending cap. Try again tomorrow.';
  }
  if (status === 429 || /rate limit/i.test(msg)) {
    return 'Rate limited. Wait a moment and retry.';
  }
  if (free && (status >= 500 || /connect|network|fetch|load failed|no content/i.test(msg))) {
    return 'No reply from the free local model. It may be offline; try a paid model.';
  }
  return 'Request failed: ' + (msg || status || 'unknown error');
}

// Settle an interrupted spend (page closed before the in-band change arrived):
// re-present the same tokens with X-Cash-Recover. 404 => never spent, restore
// the tokens; 200 => spent, absorb the change; 409 => in flight, leave pending.
async function redeemPendingChange() {
  const raw = localStorage.getItem(PENDING_CHANGE_KEY);
  if (!raw) return;
  let p;
  try { p = JSON.parse(raw); } catch (e) { localStorage.removeItem(PENDING_CHANGE_KEY); return; }
  if (!p || !p.spend || !p.blanks) { localStorage.removeItem(PENDING_CHANGE_KEY); return; }
  const k = await mintKeys();
  const r = await fetch('/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Cash': ecash.encodeCash(p.spend),
      'X-Cash-Change': ecash.encodeChange(p.blanks),
      'X-Cash-Recover': '1',
    },
    body: JSON.stringify({ model: 'recover', messages: [{ role: 'user', content: '.' }] }),
  });
  if (r.status === 404) {
    saveWallet(loadWallet().concat(p.spend));       // never spent — tokens live
    localStorage.removeItem(PENDING_CHANGE_KEY);
  } else if (r.ok) {
    const settle = ecash.absorbChange(await r.json(), p.blanks, k.pubkeys);
    if (settle.tokens.length) saveWallet(loadWallet().concat(settle.tokens));
    localStorage.removeItem(PENDING_CHANGE_KEY);
  } else {
    // Any other status (409 still in flight, or a 5xx) leaves the spend
    // unresolved. THROW so the caller aborts instead of starting a new spend that
    // would overwrite this pending record and strand its change.
    throw new Error('A previous request is still settling — try again in a moment.');
  }
}

/** Read an SSE response body into `el`, appending content deltas as they
 *  arrive. Returns null on success, or an error string when the stream
 *  carried an error or produced no content at all. */
async function streamInto(el, body) {
  const log = $('log');
  const reader = body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  let text = '';
  let stray = '';   // non-SSE lines (an error body passed through verbatim)
  let errMsg = null;
  const feed = (line) => {
    line = line.replace(/\r$/, '');
    if (!line || line.startsWith(':')) return;   // keep-alive comments
    if (!line.startsWith('data: ')) { stray += line; return; }
    const payload = line.slice(6);
    if (payload === '[DONE]') return;
    let chunk;
    try { chunk = JSON.parse(payload); } catch (e) { return; }
    if (chunk.error) {
      errMsg = chunk.error.message || JSON.stringify(chunk.error);
      return;
    }
    const choice = (chunk.choices || [])[0] || {};
    const delta = (choice.delta && choice.delta.content) || choice.text || '';
    if (delta) {
      text += delta;
      el.textContent = text;
      log.scrollTop = log.scrollHeight;
    }
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      feed(buf.slice(0, nl));
      buf = buf.slice(nl + 1);
    }
  }
  buf += dec.decode();
  if (buf) feed(buf);
  if (text) return null;   // partial output beats an error banner
  if (errMsg) return errMsg;
  if (stray) {
    try {
      const j = JSON.parse(stray);
      return j.detail || (j.error && (j.error.message || JSON.stringify(j.error))) || stray;
    } catch (e) { return stray; }
  }
  return 'the model returned no content';
}

async function send() {
  if (inFlight) return;
  const input = $('prompt');
  const text = input.value.trim();
  if (!text) return;
  const model = $('model').value;
  const free = model.startsWith('local/');   // local lane is free, no payment
  if (!free && ecashBalance() <= 0) { updateSendState(); return; }
  input.value = '';
  add('user', text);
  const pending = add('assistant', '…');
  const headers = { 'Content-Type': 'application/json' };
  let spent = null;          // ecash tokens attached to this request
  let blanks = null;         // blinded change blanks for this request
  let requestStarted = false;
  const k0 = free ? null : await mintKeys();
  inFlight = true;
  updateSendState();
  try {
    if (!free) {
      await redeemPendingChange();
      let sel;
      try {
        sel = ecash.selectTokens(loadWallet(), Math.max(2000, k0.min_prepay));
      } catch (e) {
        showErr(pending, 'Not enough credits for this request. Add credits to keep chatting.');
        return;
      }
      spent = sel.spend;
      blanks = await ecash.prepareChangeBlanks();
      saveWallet(sel.keep);
      // Persist the in-flight spend so a page close recovers the change.
      localStorage.setItem(PENDING_CHANGE_KEY, JSON.stringify({ spend: spent, blanks }));
      renderBalance();
      headers['X-Cash'] = ecash.encodeCash(sel.spend);
      headers['X-Cash-Change'] = ecash.encodeChange(blanks);
    }
    requestStarted = true;
    // Paid lane is non-streaming so the in-band change rides the response header;
    // the free lane streams (no payment, no change).
    const r = await fetch('/v1/chat/completions', {
      method: 'POST', headers,
      body: JSON.stringify({ model, messages: [{ role: 'user', content: text }], stream: free }),
    });
    if (free) {
      const ctype = r.headers.get('Content-Type') || '';
      if (!r.ok || !ctype.includes('text/event-stream')) {
        let d = null; try { d = await r.json(); } catch (e) {}
        showErr(pending, friendly(r.status, (d && (d.detail || d.error)) || r.statusText, free));
      } else {
        const err = await streamInto(pending, r.body);
        if (err) showErr(pending, friendly(0, err, free));
      }
    } else {
      const hdr = r.headers.get('X-Cash-Change');
      if (hdr) {  // tokens were spent — absorb the change either way (success or upstream error)
        const settle = ecash.absorbChange(JSON.parse(atob(hdr)), blanks, k0.pubkeys);
        if (settle.tokens.length) saveWallet(loadWallet().concat(settle.tokens));
        localStorage.removeItem(PENDING_CHANGE_KEY);
        spent = null;
      } else if (r.status === 400 || r.status === 402) {
        // PRE-spend rejection only (validation / cost bound / cap) — tokens were
        // NOT burned, so restore them. Any other error (5xx, etc.) keeps the
        // pending record so redeemPendingChange() can recover the change; do NOT
        // restore tokens that may already be spent.
        saveWallet(loadWallet().concat(spent));
        localStorage.removeItem(PENDING_CHANGE_KEY);
        spent = null;
      }
      let d = null; try { d = await r.json(); } catch (e) {}
      if (!r.ok) {
        showErr(pending, friendly(r.status, (d && (d.detail || d.error)) || r.statusText, free));
      } else {
        pending.textContent = (d && d.choices && d.choices[0] && d.choices[0].message
          && d.choices[0].message.content) || '(no content)';
      }
    }
  } catch (e) {
    if (spent && !requestStarted) { saveWallet(loadWallet().concat(spent)); spent = null; }
    showErr(pending, friendly(0, e.message, free));
  } finally {
    inFlight = false;
    renderBalance();
  }
}

// ---- wallet backup: the tokens live only in this browser, so the backup
// file IS the money. No server-side recovery exists by design. ----

let walletNoteTimer = null;
function walletNote(msg) {
  const el = $('key-info').classList.contains('hidden')
    ? $('wallet-note-start') : $('wallet-note');
  el.textContent = msg;
  clearTimeout(walletNoteTimer);
  walletNoteTimer = setTimeout(() => { el.textContent = ''; }, 6000);
}

function exportWallet() {
  const data = {
    format: 'anon-router-wallet-v1',
    exported_at: new Date().toISOString(),
    account,
    tokens: loadWallet(),
  };
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'anon-router-wallet-' + new Date().toISOString().slice(0, 10) + '.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  walletNote('Backup downloaded. Keep the file private; it is the money.');
}

function importWalletFile(file) {
  const fr = new FileReader();
  fr.onload = () => {
    try {
      const data = JSON.parse(fr.result);
      if (data.format !== 'anon-router-wallet-v1' || !data.account
          || typeof data.account.api_key !== 'string' || !Array.isArray(data.tokens)) {
        throw new Error('bad format');
      }
      for (const t of data.tokens) {
        if (!(Number.isInteger(t.amount) && t.amount > 0
            && typeof t.secret === 'string' && typeof t.C === 'string')) {
          throw new Error('bad token');
        }
      }
      // Merge, deduped by secret: importing must never drop tokens already here.
      const bySecret = new Map(loadWallet().map((t) => [t.secret, t]));
      for (const t of data.tokens) {
        bySecret.set(t.secret, { amount: t.amount, secret: t.secret, C: t.C });
      }
      saveWallet([...bySecret.values()]);
      account = data.account;
      unlockAfterKey();
      renderBalance();
      walletNote('Wallet imported.');
    } catch (e) {
      walletNote('That file is not an anon-router wallet backup.');
    }
  };
  fr.readAsText(file);
}

// ---- copy affordances ----

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e2) {}
    ta.remove();
    return ok;
  }
}

function wireCopy(btnId, srcId) {
  const btn = $(btnId);
  btn.onclick = async () => {
    const ok = await copyText($(srcId).textContent);
    btn.textContent = ok ? 'Copied' : 'Copy failed';
    btn.classList.toggle('done', ok);
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('done'); }, 1400);
  };
}

$('mint').onclick = mint;
$('deposit').onclick = deposit;
$('send').onclick = send;
$('prompt').addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
$('model').addEventListener('change', updateSendState);
$('amount').addEventListener('input', renderDepositPreview);
$('export').onclick = exportWallet;
const importInput = $('import-file');
$('import-start').onclick = () => importInput.click();
$('import-again').onclick = () => importInput.click();
importInput.addEventListener('change', () => {
  if (importInput.files && importInput.files[0]) importWalletFile(importInput.files[0]);
  importInput.value = '';   // allow re-importing the same file
});
wireCopy('copy-key', 'apikey');
wireCopy('copy-base', 'baseurl');
updateSendState();
mintKeys().then(redeemPendingChange).then(renderBalance).catch(() => renderBalance());

// Show the Tor .onion address in the footer when the router publishes one.
fetch('/privacy').then((r) => r.json()).then((p) => {
  const onion = p && p.transport && p.transport.onion;
  if (onion) {
    $('onion-addr').textContent = onion;
    $('onion-line').classList.remove('hidden');
  }
}).catch(() => {});
