// app.js: anon-router demo frontend. UI and wiring only; the BDHKE wallet
// crypto lives in /ecash.js and is unchanged here.

import * as ecash from '/ecash.js';

let account = null;
let keys = null;         // /mint/keys payload (denomination pubkeys etc.)
let claiming = false;
let lastAcctBal = 0;
let polling = false;
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
  if (!polling) { polling = true; pollBalance(); }
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

async function pollBalance() {
  if (account) {
    try {
      const r = await fetch('/account/status', { headers: { Authorization: 'Bearer ' + account.api_key } });
      const d = await r.json();
      lastAcctBal = d.balance;
      if ((lastAcctBal > 0 || localStorage.getItem(PENDING_CLAIM_KEY)) && !claiming) {
        await claimToEcash(lastAcctBal);
      }
    } catch (e) {}
  }
  renderBalance();
  setTimeout(pollBalance, 2500);
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

async function redeemPendingChange() {
  const receipt = localStorage.getItem(PENDING_CHANGE_KEY);
  if (!receipt) return;
  const k = await mintKeys();
  const settle = await ecash.redeemChange('', receipt, k.pubkeys);
  if (settle.tokens.length) saveWallet(loadWallet().concat(settle.tokens));
  localStorage.removeItem(PENDING_CHANGE_KEY);
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
  let requestStarted = false;
  inFlight = true;
  updateSendState();
  try {
    if (!free) {
      await redeemPendingChange();
      const k = await mintKeys();
      let sel;
      try {
        sel = ecash.selectTokens(loadWallet(), Math.max(2000, k.min_prepay));
      } catch (e) {
        showErr(pending, 'Not enough credits for this request. Add credits to keep chatting.');
        return;
      }
      spent = sel.spend;
      saveWallet(sel.keep);
      renderBalance();
      headers['X-Cash'] = ecash.encodeCash(sel.spend);
    }
    requestStarted = true;
    const r = await fetch('/v1/chat/completions', {
      method: 'POST', headers,
      body: JSON.stringify({ model, messages: [{ role: 'user', content: text }], stream: true }),
    });
    const receipt = r.headers.get('X-Change-Receipt');
    if (receipt) localStorage.setItem(PENDING_CHANGE_KEY, receipt);
    const ctype = r.headers.get('Content-Type') || '';
    if (!r.ok || !ctype.includes('text/event-stream')) {
      let d = null;
      try { d = await r.json(); } catch (e) {}
      if (!r.ok) {
        // No change receipt means the mint never recorded these tokens as
        // spent (e.g. the daily cap tripped first), so they are still valid.
        if (spent && !receipt) { saveWallet(loadWallet().concat(spent)); spent = null; }
        showErr(pending, friendly(r.status, (d && (d.detail || d.error)) || r.statusText, free));
      } else {
        pending.textContent = (d && d.choices && d.choices[0] && d.choices[0].message
          && d.choices[0].message.content) || '(no content)';
      }
    } else {
      const err = await streamInto(pending, r.body);
      if (err) showErr(pending, friendly(0, err, free));
    }
    if (receipt) await redeemPendingChange();
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
