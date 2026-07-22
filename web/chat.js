// chat.js — on-device chat session store for anon-router.
//
// PRIVACY: conversations are stored ONLY in this browser (IndexedDB), never sent
// to the server. Each session is one record { id, title, model, createdAt,
// updatedAt, messages: [{role, text, err, ts}] }. Content lives here so a
// refresh restores your conversations; clearing local data removes it entirely.

const DB_NAME = 'anon-router-chat';
const STORE = 'sessions';
const DB_VERSION = 1;

let _dbPromise = null;
function db() {
  if (_dbPromise) return _dbPromise;
  _dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const d = req.result;
      if (!d.objectStoreNames.contains(STORE)) {
        d.createObjectStore(STORE, { keyPath: 'id' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return _dbPromise;
}
function store(mode) { return db().then((d) => d.transaction(STORE, mode).objectStore(STORE)); }
function reqP(makeReq) {
  return new Promise((resolve, reject) => {
    const r = makeReq(); r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error);
  });
}

let _uidn = 0;
/** A collision-resistant id without Math.random (varies by call + time). */
function newId() {
  _uidn += 1;
  const rnd = (typeof crypto !== 'undefined' && crypto.randomUUID)
    ? crypto.randomUUID() : `${Date.now()}-${_uidn}`;
  return 's_' + rnd;
}

export function newSession(model) {
  const now = Date.now();
  return { id: newId(), title: 'New chat', model: model || '', createdAt: now, updatedAt: now, messages: [] };
}

export async function putSession(s) {
  try { const os = await store('readwrite'); await reqP(() => os.put(s)); } catch (e) { /* ignore */ }
  return s;
}

export async function getSession(id) {
  try { const os = await store('readonly'); return await reqP(() => os.get(id)); } catch (e) { return null; }
}

export async function deleteSession(id) {
  try { const os = await store('readwrite'); await reqP(() => os.delete(id)); } catch (e) { /* ignore */ }
}

/** Session summaries (no messages), newest-updated first. */
export async function listSessions() {
  let all = [];
  try { const os = await store('readonly'); all = await reqP(() => os.getAll()) || []; } catch (e) { all = []; }
  return all
    .map((s) => ({ id: s.id, title: s.title, model: s.model, updatedAt: s.updatedAt, count: (s.messages || []).length }))
    .sort((a, b) => b.updatedAt - a.updatedAt);
}

export async function clearAll() {
  try { const os = await store('readwrite'); await reqP(() => os.clear()); } catch (e) { /* ignore */ }
}

// ---- pure helpers (browser-free, testable) ----

/** Derive a short title from the first user message. */
export function titleFrom(messages) {
  const first = (messages || []).find((m) => m.role === 'user' && m.text);
  if (!first) return 'New chat';
  const t = first.text.trim().replace(/\s+/g, ' ');
  return t.length > 40 ? t.slice(0, 40) + '…' : t;
}

/** Append a message and bump updatedAt/title, returning the same session. */
export function appendMessage(session, role, text, err) {
  session.messages.push({ role, text, err: !!err, ts: Date.now() });
  session.updatedAt = Date.now();
  if (session.title === 'New chat') session.title = titleFrom(session.messages);
  return session;
}
