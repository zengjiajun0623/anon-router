// app.js: anon-router demo frontend. UI and wiring only; the BDHKE wallet
// crypto lives in /ecash.js and is unchanged here.

import * as ecash from '/ecash.js';
import { encryptJSON, decryptJSON, deriveVaultKey, sealWithKey, openWithKey } from '/crypto.js';
import * as usage from '/usage.js';
import * as chat from '/chat.js';

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
const ACCOUNT_KEY = 'anon-router-account-v1';
const CHAT_KEY = 'anon-router-chat-v1';
// Encrypted-at-rest vault (opt-in). When present, the account + tokens live ONLY
// inside this AES-GCM envelope on disk; the plaintext WALLET_KEY/ACCOUNT_KEY are
// removed. The decrypted wallet is held in `mem` in memory after unlock.
const VAULT_KEY = 'anon-router-vault-v1';

// Wallet storage has two modes:
//   * plaintext (default, skippable path): loadWallet/saveWallet read/write
//     localStorage synchronously — unchanged, crash-safe behavior.
//   * vault (a passphrase is set): `mem` is the in-memory source of truth; every
//     write re-encrypts asynchronously with a key derived ONCE at unlock. The
//     synchronous crash-recovery records (PENDING_*) are only dropped AFTER an
//     awaited flushWallet(), so the async write never opens a money-loss window.
let vaultMode = false;       // true when the wallet is stored as an encrypted vault
let vaultKey = null;         // CryptoKey held in memory while unlocked (never persisted)
let vaultSalt = null;        // b64 salt bound to this vault
const mem = { account: null, tokens: [] };
let persistTimer = null;

const loadWallet = () => {
  if (vaultMode) return mem.tokens;
  try { return JSON.parse(localStorage.getItem(WALLET_KEY)) || []; } catch (e) { return []; }
};
const saveWallet = (t) => {
  if (vaultMode) { mem.tokens = t; schedulePersist(); }
  else localStorage.setItem(WALLET_KEY, JSON.stringify(t));
};
const ecashBalance = () => loadWallet().reduce((s, t) => s + t.amount, 0);

const loadAccount = () => {
  if (vaultMode) return mem.account;
  try { return JSON.parse(localStorage.getItem(ACCOUNT_KEY)) || null; } catch (e) { return null; }
};
const saveAccount = (a) => {
  if (vaultMode) { mem.account = a; schedulePersist(); }
  else { try { localStorage.setItem(ACCOUNT_KEY, JSON.stringify(a)); } catch (e) {} }
};

// Coalesce rapid writes; a real durability point uses awaited flushWallet().
function schedulePersist() {
  if (persistTimer) return;
  persistTimer = setTimeout(() => { persistTimer = null; flushWallet().catch(() => {}); }, 120);
}

// Durably write the encrypted vault NOW. Awaited at money-critical points before
// dropping a plaintext recovery record. No-op in plaintext mode (already durable).
async function flushWallet() {
  if (persistTimer) { clearTimeout(persistTimer); persistTimer = null; }
  if (!vaultMode || !vaultKey) return;
  const env = await sealWithKey(
    { account: mem.account, tokens: mem.tokens }, vaultKey, vaultSalt);
  localStorage.setItem(VAULT_KEY, JSON.stringify(env));
}

// Turn on at-rest encryption: derive a key, migrate the current plaintext wallet
// into the vault, and remove the plaintext copies.
async function enableVault(passphrase) {
  const cur = { account: loadAccount(), tokens: loadWallet() };
  const dk = await deriveVaultKey(passphrase, null);
  vaultKey = dk.key; vaultSalt = dk.salt; vaultMode = true;
  mem.account = cur.account; mem.tokens = cur.tokens;
  await flushWallet();
  for (const k of [WALLET_KEY, ACCOUNT_KEY]) { try { localStorage.removeItem(k); } catch (e) {} }
}

// ---- chat sessions (multiple conversations, stored on-device via chat.js) ----
let activeSession = null;

function renderMessages(session) {
  const log = $('log');
  log.innerHTML = '';
  for (const m of (session && session.messages) || []) {
    const el = add(m.role, m.text);
    if (m.err) el.classList.add('err');
  }
}

async function renderSessionBar() {
  const sel = $('session-select');
  if (!sel) return;
  const sessions = await chat.listSessions();
  sel.innerHTML = '';
  for (const s of sessions) {
    const o = document.createElement('option');
    o.value = s.id;
    o.textContent = (s.title || 'New chat') + (s.count ? ` (${s.count})` : '');
    sel.appendChild(o);
  }
  if (activeSession) sel.value = activeSession.id;
}

async function loadSession(id) {
  const s = await chat.getSession(id);
  if (!s) return;
  activeSession = s;
  renderMessages(s);
  if (s.model) {
    const m = $('model');
    if ([...m.options].some((o) => o.value === s.model)) m.value = s.model;
  }
  await renderSessionBar();
  updateSendState();
}

async function newChat() {
  activeSession = chat.newSession($('model').value);
  await chat.putSession(activeSession);
  $('log').innerHTML = '';
  await renderSessionBar();
  updateSendState();
}

async function renameActiveSession() {
  if (!activeSession) return;
  const t = prompt('Rename this chat:', activeSession.title || '');
  if (t === null) return;
  activeSession.title = t.trim() || activeSession.title;
  await chat.putSession(activeSession);
  await renderSessionBar();
}

async function deleteActiveSession() {
  if (!activeSession) return;
  if (!confirm('Delete this conversation?')) return;
  await chat.deleteSession(activeSession.id);
  activeSession = null;
  const list = await chat.listSessions();
  if (list.length) await loadSession(list[0].id); else await newChat();
}

// Open the most recent conversation, migrating a legacy single transcript first.
async function initSessions() {
  const list = await chat.listSessions();
  if (!list.length) {
    let legacy = null;
    try { legacy = JSON.parse(localStorage.getItem(CHAT_KEY)); } catch (e) {}
    if (Array.isArray(legacy) && legacy.length) {
      const s = chat.newSession($('model').value);
      s.messages = legacy.map((m) => ({ role: m.role, text: m.text, err: !!m.err, ts: Date.now() }));
      s.title = chat.titleFrom(s.messages);
      await chat.putSession(s);
      try { localStorage.removeItem(CHAT_KEY); } catch (e) {}
    }
  }
  const fresh = await chat.listSessions();
  if (fresh.length) await loadSession(fresh[0].id); else await newChat();
}

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
    saveAccount(account);   // survive a refresh (see ACCOUNT_KEY)
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
    await flushWallet();   // durable before dropping the plaintext claim record
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

// The testnet the vault lives on. The router tells us which chain (account.chain_id);
// default Sepolia. A deposit sent on any other network (e.g. the wallet's default
// mainnet) never reaches the vault and never credits, so we switch the wallet first.
const SEPOLIA_ADD_PARAMS = {
  chainId: '0xaa36a7',  // 11155111
  chainName: 'Sepolia',
  nativeCurrency: { name: 'Sepolia Ether', symbol: 'ETH', decimals: 18 },
  rpcUrls: ['https://ethereum-sepolia-rpc.publicnode.com', 'https://rpc.sepolia.org'],
  blockExplorerUrls: ['https://sepolia.etherscan.io'],
};

function wantChainHex() {
  const id = (account && account.chain_id) || 11155111;
  return '0x' + Number(id).toString(16);
}

// Convert an ETH decimal string to a wei BigInt WITHOUT floating point (parseFloat
// * 1e18 overflows JS's 2^53 safe-integer range for amounts >= ~0.009 ETH, sending
// a wrong value). Returns null on invalid input.
function ethToWei(s) {
  s = String(s == null ? '' : s).trim();
  if (!/^\d*\.?\d*$/.test(s) || s === '' || s === '.') return null;
  const [whole, frac = ''] = s.split('.');
  const fracPadded = (frac + '0'.repeat(18)).slice(0, 18);
  const wei = BigInt(whole || '0') * (10n ** 18n) + BigInt(fracPadded || '0');
  return wei > 0n ? wei : null;
}

// Make the wallet point at the vault's chain (Sepolia), adding it if the wallet
// doesn't know it yet. Runs on the deposit click (a user gesture), so the wallet
// is allowed to prompt.
async function ensureChain() {
  const want = wantChainHex();
  let current;
  try { current = await window.ethereum.request({ method: 'eth_chainId' }); } catch (e) { /* older wallets */ }
  if (current && String(current).toLowerCase() === want.toLowerCase()) return;
  try {
    await window.ethereum.request({
      method: 'wallet_switchEthereumChain', params: [{ chainId: want }],
    });
  } catch (err) {
    // 4902 = the wallet doesn't have this chain yet; add it, which also switches.
    const code = err && (err.code != null ? err.code
      : (err.data && err.data.originalError && err.data.originalError.code));
    if (code === 4902 && want.toLowerCase() === SEPOLIA_ADD_PARAMS.chainId) {
      await window.ethereum.request({
        method: 'wallet_addEthereumChain', params: [SEPOLIA_ADD_PARAMS],
      });
    } else {
      throw err;
    }
  }
}

async function deposit() {
  if (!account) return;
  if (!window.ethereum) { alert('No browser wallet found. Install one like MetaMask to deposit.'); return; }
  const wei = ethToWei($('amount').value);
  if (wei == null) { alert('Enter a valid ETH amount above 0.'); return; }
  try {
    const accts = await window.ethereum.request({ method: 'eth_requestAccounts' });
    await ensureChain();  // switch the wallet to Sepolia BEFORE sending the deposit
    const data = account.deposit_selector + account.key_hash.replace(/^0x/, '');
    await window.ethereum.request({
      method: 'eth_sendTransaction',
      params: [{ from: accts[0], to: account.vault_address, value: '0x' + wei.toString(16), data }],
    });
    watchFunding(true);  // wait through mining until this deposit credits + drains
  } catch (err) {
    // user rejected the connect / network switch / tx, or the switch failed
    const msg = (err && err.message) ? err.message : 'cancelled';
    alert('Deposit not sent: ' + msg);
  }
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
    await flushWallet();
    localStorage.removeItem(PENDING_CHANGE_KEY);
  } else if (r.ok) {
    const settle = ecash.absorbChange(await r.json(), p.blanks, k.pubkeys);
    if (settle.tokens.length) saveWallet(loadWallet().concat(settle.tokens));
    await flushWallet();
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
  // Per-request usage metadata, recorded on-device only (see usage.js).
  const startedAt = Date.now();
  const rec = { model, free, streamed: free, status: 0, costCredits: 0,
                inputTokens: 0, outputTokens: 0, error: '' };
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
      // Make the spend-removal durable BEFORE the request and before writing the
      // (plaintext, synchronous) recovery record, so a crash can never leave the
      // spent tokens back in the vault alongside a pending record (double-count).
      await flushWallet();
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
    rec.status = r.status;
    const cc = r.headers.get('X-Cost-Credits');
    if (cc != null) rec.costCredits = parseInt(cc, 10) || 0;
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
        await flushWallet();   // change durable before dropping the recovery record
        localStorage.removeItem(PENDING_CHANGE_KEY);
        spent = null;
      } else if (r.status === 400 || r.status === 402) {
        // PRE-spend rejection only (validation / cost bound / cap) — tokens were
        // NOT burned, so restore them. Any other error (5xx, etc.) keeps the
        // pending record so redeemPendingChange() can recover the change; do NOT
        // restore tokens that may already be spent.
        saveWallet(loadWallet().concat(spent));
        await flushWallet();   // restoration durable before dropping the record
        localStorage.removeItem(PENDING_CHANGE_KEY);
        spent = null;
      }
      let d = null; try { d = await r.json(); } catch (e) {}
      const u = d && d.usage;
      if (u) { rec.inputTokens = u.prompt_tokens | 0; rec.outputTokens = u.completion_tokens | 0; }
      if (!r.ok) {
        rec.error = (d && (d.detail || (d.error && (d.error.message || d.error)))) || String(r.status);
        showErr(pending, friendly(r.status, (d && (d.detail || d.error)) || r.statusText, free));
      } else {
        pending.textContent = (d && d.choices && d.choices[0] && d.choices[0].message
          && d.choices[0].message.content) || '(no content)';
      }
    }
  } catch (e) {
    if (spent && !requestStarted) { saveWallet(loadWallet().concat(spent)); spent = null; }
    rec.error = e.message || 'error';
    showErr(pending, friendly(0, e.message, free));
  } finally {
    inFlight = false;
    await flushWallet();   // ensure any wallet change this turn is durable
    renderBalance();
    // Save the turn into the active conversation (on-device only).
    if (activeSession) {
      chat.appendMessage(activeSession, 'user', text);
      chat.appendMessage(activeSession, 'assistant', pending.textContent,
        pending.classList.contains('err'));
      await chat.putSession(activeSession);
      renderSessionBar();
    }
    if (requestStarted) {
      rec.latencyMs = Date.now() - startedAt;
      usage.recordRequest(rec);   // on-device only; never sent to the server
      if (activeView === 'activity') renderActivity();
    }
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

function downloadBackup(data, note) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'anon-router-wallet-' + new Date().toISOString().slice(0, 10) + '.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  walletNote(note);
}

// Back up the wallet. Default: prompt for a passphrase and encrypt the payload
// (the file is bearer money, so an encrypted backup is far safer to store/sync).
// Skippable — an empty passphrase falls back to a plaintext backup after an
// explicit warning, honoring "encryption is optional".
async function exportWallet() {
  const payload = { account, tokens: loadWallet() };
  const pass = prompt(
    'Set a passphrase to ENCRYPT this backup (recommended — the file is your '
    + 'money).\n\nLeave blank to export UNENCRYPTED.');
  if (pass === null) return;  // user cancelled the whole export
  if (pass) {
    const confirmPass = prompt('Re-enter the passphrase to confirm.');
    if (confirmPass === null) return;
    if (confirmPass !== pass) { walletNote('Passphrases did not match — not exported.'); return; }
    let vault;
    try {
      vault = await encryptJSON(payload, pass);
    } catch (e) {
      walletNote('Could not encrypt the backup: ' + e.message);
      return;
    }
    downloadBackup(
      { format: 'anon-router-wallet-enc-v1', exported_at: new Date().toISOString(), vault },
      'Encrypted backup downloaded. Keep the passphrase safe — it cannot be recovered.');
    return;
  }
  if (!confirm('Export WITHOUT encryption? Anyone who gets this file can spend '
               + 'your credits.')) return;
  downloadBackup(
    { format: 'anon-router-wallet-v1', exported_at: new Date().toISOString(), account, tokens: loadWallet() },
    'Unencrypted backup downloaded. Keep the file private; it is the money.');
}

// Validate a decrypted {account, tokens} payload and merge it into this wallet.
// Merge (deduped by secret) so importing never drops tokens already here.
function applyImportedPayload(payload) {
  if (!payload || !payload.account || typeof payload.account.api_key !== 'string'
      || !Array.isArray(payload.tokens)) {
    throw new Error('bad format');
  }
  for (const t of payload.tokens) {
    if (!(Number.isInteger(t.amount) && t.amount > 0
        && typeof t.secret === 'string' && typeof t.C === 'string')) {
      throw new Error('bad token');
    }
  }
  const bySecret = new Map(loadWallet().map((t) => [t.secret, t]));
  for (const t of payload.tokens) {
    bySecret.set(t.secret, { amount: t.amount, secret: t.secret, C: t.C });
  }
  saveWallet([...bySecret.values()]);
  account = payload.account;
  saveAccount(account);
  unlockAfterKey();
  renderBalance();
  walletNote('Wallet imported.');
}

function importWalletFile(file) {
  const fr = new FileReader();
  fr.onload = async () => {
    let data;
    try { data = JSON.parse(fr.result); } catch (e) {
      walletNote('That file is not an anon-router wallet backup.');
      return;
    }
    try {
      if (data.format === 'anon-router-wallet-enc-v1') {
        const pass = prompt('This backup is encrypted. Enter its passphrase.');
        if (pass === null) return;   // cancelled
        let payload;
        try {
          payload = await decryptJSON(data.vault, pass);
        } catch (e) {
          walletNote(e.message || 'Could not decrypt the backup.');
          return;
        }
        applyImportedPayload(payload);
      } else if (data.format === 'anon-router-wallet-v1') {
        applyImportedPayload({ account: data.account, tokens: data.tokens });
      } else {
        throw new Error('bad format');
      }
    } catch (e) {
      walletNote('That file is not a valid anon-router wallet backup.');
    }
  };
  fr.readAsText(file);
}

// Clear this browser's wallet and start fresh. The tokens live ONLY here, so
// this can destroy credits — gate it behind an explicit back-up-first confirm.
function newWallet() {
  const bal = ecashBalance();
  const warn = bal > 0
    ? `This browser holds ${bal} credits ($${(bal * ((keys && keys.credit_usd) || 0.0001)).toFixed(2)}). `
      + 'Starting a new wallet CLEARS them from this browser. Back up first if you '
      + 'want to keep them. Continue?'
    : 'Start a new wallet? This clears the current wallet from this browser.';
  if (!confirm(warn)) return;
  for (const k of [ACCOUNT_KEY, WALLET_KEY, CHAT_KEY, PENDING_CLAIM_KEY, PENDING_CHANGE_KEY, VAULT_KEY]) {
    try { localStorage.removeItem(k); } catch (e) {}
  }
  vaultMode = false; vaultKey = null; vaultSalt = null;
  mem.account = null; mem.tokens = [];
  account = null;
  lastAcctBal = 0;
  $('log').innerHTML = '';
  $('key-info').classList.add('hidden');
  $('key-start').classList.remove('hidden');
  $('mint').disabled = false;
  $('deposit-body').classList.add('hidden');
  $('deposit-locked').classList.remove('hidden');
  reflectEncState();
  renderBalance();
  newChat();   // fresh conversation to match the fresh wallet (old chats remain)
}

// ---- at-rest encryption UI ----

// Reflect vault state on the "Encrypt this wallet" control.
function reflectEncState() {
  const btn = $('encrypt');
  const state = $('enc-state');
  if (!btn || !state) return;
  if (vaultMode) {
    btn.classList.add('hidden');
    state.textContent = '🔒 Encrypted at rest';
  } else {
    btn.classList.remove('hidden');
    state.textContent = '';
  }
}

// Turn on encryption for the current wallet (prompt + confirm passphrase).
async function encryptWallet() {
  if (vaultMode) return;
  if (!account && ecashBalance() === 0) {
    walletNote('Create or import a wallet first.');
    return;
  }
  const pass = prompt('Set a passphrase to encrypt this wallet on this device.\n\n'
    + 'There is NO recovery if you forget it — back up your wallet first.');
  if (pass === null) return;
  if (!pass) { walletNote('Encryption cancelled (empty passphrase).'); return; }
  const confirmPass = prompt('Re-enter the passphrase to confirm.');
  if (confirmPass === null) return;
  if (confirmPass !== pass) { walletNote('Passphrases did not match — not encrypted.'); return; }
  try {
    await enableVault(pass);
  } catch (e) {
    walletNote('Could not encrypt: ' + (e.message || e));
    return;
  }
  reflectEncState();
  walletNote('Wallet encrypted on this device. It will ask for the passphrase on reload.');
}

// If an encrypted vault exists, prompt to unlock it into memory. Returns true
// once unlocked (or if there is no vault); false if the user left it locked.
async function maybeUnlockVault() {
  let env = null;
  try { env = JSON.parse(localStorage.getItem(VAULT_KEY)); } catch (e) { env = null; }
  if (!env) return true;   // plaintext mode, nothing to unlock
  for (;;) {
    const pass = prompt('This wallet is encrypted. Enter your passphrase to unlock.');
    if (pass === null) return false;   // user cancelled -> stay locked
    try {
      const dk = await deriveVaultKey(pass, env.salt);
      const data = await openWithKey(env, dk.key);
      vaultKey = dk.key; vaultSalt = env.salt; vaultMode = true;
      mem.account = (data && data.account) || null;
      mem.tokens = (data && Array.isArray(data.tokens)) ? data.tokens : [];
      return true;
    } catch (e) {
      alert('Wrong passphrase. Try again, or discard the encrypted wallet.');
    }
  }
}

function showLocked(locked) {
  $('locked').classList.toggle('hidden', !locked);
  $('main').classList.toggle('hidden', locked);
}

// ---- views / navigation + Activity ----
let activeView = 'chat';
const VIEWS = ['chat', 'activity', 'settings'];
const escHtml = (s) => String(s == null ? '' : s).replace(
  /[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function showView(name) {
  activeView = name;
  for (const v of VIEWS) {
    $('view-' + v).classList.toggle('hidden', v !== name);
    $('tab-' + v).classList.toggle('active', v === name);
  }
  if (name === 'activity') renderActivity();
}

async function renderActivity() {
  const rows = await usage.allRequests();
  const cu = (keys && keys.credit_usd) || 0.0001;
  const t = usage.totals(rows);
  $('act-empty').classList.toggle('hidden', rows.length > 0);
  $('act-stats').innerHTML = [
    ['Requests', t.requests],
    ['Spent', '$' + (t.costCredits * cu).toFixed(4)],
    ['Input tokens', t.inputTokens.toLocaleString()],
    ['Output tokens', t.outputTokens.toLocaleString()],
    ['Success', rows.length ? (t.successRate * 100).toFixed(0) + '%' : '—'],
    ['Avg latency', t.avgLatencyMs ? t.avgLatencyMs + ' ms' : '—'],
  ].map(([k, v]) => `<div class="s"><div class="v">${escHtml(v)}</div><div class="k">${k}</div></div>`).join('');

  const bm = usage.byModel(rows);
  $('act-by-model').innerHTML = bm.length
    ? '<tr><th>Model</th><th class="num">Reqs</th><th class="num">In</th><th class="num">Out</th><th class="num">Cost</th></tr>'
      + bm.map((g) => `<tr><td>${escHtml(g.model)}</td><td class="num">${g.requests}</td>`
        + `<td class="num">${g.inputTokens}</td><td class="num">${g.outputTokens}</td>`
        + `<td class="num">$${(g.costCredits * cu).toFixed(4)}</td></tr>`).join('')
    : '';
  const bd = usage.byDay(rows);
  $('act-by-day').innerHTML = bd.length
    ? '<tr><th>Day</th><th class="num">Reqs</th><th class="num">Cost</th></tr>'
      + bd.map((g) => `<tr><td>${g.day}</td><td class="num">${g.requests}</td>`
        + `<td class="num">$${(g.costCredits * cu).toFixed(4)}</td></tr>`).join('')
    : '';
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
$('new-wallet').onclick = newWallet;
$('encrypt').onclick = encryptWallet;
const importInput = $('import-file');
$('import-start').onclick = () => importInput.click();
$('import-again').onclick = () => importInput.click();
importInput.addEventListener('change', () => {
  if (importInput.files && importInput.files[0]) importWalletFile(importInput.files[0]);
  importInput.value = '';   // allow re-importing the same file
});
wireCopy('copy-key', 'apikey');

// Restore a prior session so a refresh keeps the whole wallet, not just the
// balance. If an encrypted vault exists, unlock it FIRST (into memory) so the
// mode-aware loaders see the decrypted wallet.
async function restoreSession() {
  const savedAccount = loadAccount();
  if (savedAccount && typeof savedAccount.api_key === 'string') {
    account = savedAccount;
    unlockAfterKey();
  }
  await initSessions();
  reflectEncState();
  updateSendState();
  try { await mintKeys(); await redeemPendingChange(); } catch (e) {}
  renderBalance();
}

async function boot() {
  const unlocked = await maybeUnlockVault();
  if (!unlocked) { showLocked(true); return; }   // stay locked until unlocked/discarded
  showLocked(false);
  await restoreSession();
}

// tabs
for (const v of VIEWS) $('tab-' + v).onclick = () => showView(v);

// chat sessions
$('new-chat').onclick = newChat;
$('rename-chat').onclick = renameActiveSession;
$('delete-chat').onclick = deleteActiveSession;
$('session-select').onchange = (e) => loadSession(e.target.value);

// activity actions
$('act-export').onclick = async () => {
  const rows = await usage.allRequests();
  const blob = new Blob([usage.toCSV(rows)], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'anon-router-activity-' + new Date().toISOString().slice(0, 10) + '.csv';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
};
const clearActivity = async () => {
  if (!confirm('Clear all activity on this device?')) return;
  await usage.clearUsage(); renderActivity();
};
$('act-clear').onclick = clearActivity;
$('set-clear-activity').onclick = clearActivity;
$('set-clear-chat').onclick = async () => {
  if (!confirm('Delete ALL conversations on this device?')) return;
  try { localStorage.removeItem(CHAT_KEY); } catch (e) {}
  await chat.clearAll();
  activeSession = null;
  await newChat();
};
$('set-clear-all').onclick = () => {
  if (!confirm('Erase EVERYTHING on this device (wallet, activity, chat)? Back up your '
    + 'wallet first — its credits are gone otherwise.')) return;
  usage.clearUsage();
  chat.clearAll();
  for (const k of [ACCOUNT_KEY, WALLET_KEY, CHAT_KEY, PENDING_CLAIM_KEY,
                   PENDING_CHANGE_KEY, VAULT_KEY, RETENTION_KEY]) {
    try { localStorage.removeItem(k); } catch (e) {}
  }
  location.reload();
};
// retention
const RETENTION_KEY = 'anon-router-retention-days';
$('set-retention').value = (() => { try { return localStorage.getItem(RETENTION_KEY) || '0'; } catch (e) { return '0'; } })();
$('set-retention').onchange = async () => {
  const d = parseInt($('set-retention').value, 10) || 0;
  try { localStorage.setItem(RETENTION_KEY, String(d)); } catch (e) {}
  if (d > 0) await usage.pruneOlderThan(Date.now() - d * 86400000);
};

$('unlock').onclick = async () => {
  if (await maybeUnlockVault()) { showLocked(false); await restoreSession(); }
};
$('discard-vault').onclick = () => {
  if (!confirm('Discard the encrypted wallet on this device? If you have no '
    + 'backup + passphrase, its credits are gone for good.')) return;
  for (const k of [VAULT_KEY, ACCOUNT_KEY, WALLET_KEY, CHAT_KEY, PENDING_CLAIM_KEY, PENDING_CHANGE_KEY]) {
    try { localStorage.removeItem(k); } catch (e) {}
  }
  location.reload();
};

boot();

// Apply the activity retention window on load (prune anything older).
(() => {
  let d = 0;
  try { d = parseInt(localStorage.getItem(RETENTION_KEY) || '0', 10) || 0; } catch (e) {}
  if (d > 0) usage.pruneOlderThan(Date.now() - d * 86400000);
})();

// Populate the model dropdown from the LIVE catalog so it never offers a retired
// model. Keep a small curated shortlist (in preference order); fall back to the
// static gpt-4o-mini option if the catalog can't be fetched.
const PREFERRED_MODELS = [
  'openai/gpt-4o-mini', 'openai/gpt-4o',
  'anthropic/claude-sonnet-4.5', 'anthropic/claude-haiku-4.5',
  'google/gemini-2.0-flash-001', 'meta-llama/llama-3.3-70b-instruct',
];
fetch('/v1/models').then((r) => r.json()).then((d) => {
  const live = new Set((d.data || []).map((m) => m.id));
  const pick = PREFERRED_MODELS.filter((m) => live.has(m));
  if (!pick.length) return;                       // keep the static default
  const sel = $('model');
  sel.innerHTML = '';
  for (const id of pick) {
    const o = document.createElement('option');
    o.value = id; o.textContent = id;
    sel.appendChild(o);
  }
}).catch(() => {});

// Show the Tor .onion address in the footer when the router publishes one.
fetch('/privacy').then((r) => r.json()).then((p) => {
  const onion = p && p.transport && p.transport.onion;
  if (onion) {
    $('onion-addr').textContent = onion;
    $('onion-line').classList.remove('hidden');
  }
}).catch(() => {});
