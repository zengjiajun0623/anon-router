// usage.js — on-device usage/activity store for anon-router.
//
// PRIVACY: this data NEVER leaves the browser. A privacy router must not log
// per-request usage server-side (that would re-link spends), so OpenRouter-style
// Activity/Logs are computed here, on the user's own device, from what the
// client already sees in each response (model, tokens, cost, latency, status).
// Prompt/response CONTENT is not stored here — only metadata.
//
// Storage: IndexedDB (larger + async, right for a growing log). The aggregation
// helpers below are PURE (operate on plain arrays) so they run and test without
// a browser.

const DB_NAME = 'anon-router-usage';
const STORE = 'requests';
const DB_VERSION = 1;

let _dbPromise = null;
function db() {
  if (_dbPromise) return _dbPromise;
  _dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const d = req.result;
      if (!d.objectStoreNames.contains(STORE)) {
        const os = d.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
        os.createIndex('ts', 'ts', { unique: false });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return _dbPromise;
}

function tx(mode) {
  return db().then((d) => d.transaction(STORE, mode).objectStore(STORE));
}

/** Record one request's metadata. Best-effort: never throws into the caller. */
export async function recordRequest(rec) {
  try {
    const store = await tx('readwrite');
    await new Promise((resolve, reject) => {
      const r = store.add({
        ts: rec.ts || Date.now(),
        model: rec.model || '',
        provider: rec.provider || '',
        inputTokens: rec.inputTokens | 0,
        outputTokens: rec.outputTokens | 0,
        costCredits: rec.costCredits | 0,
        latencyMs: rec.latencyMs | 0,
        status: rec.status | 0,
        streamed: !!rec.streamed,
        free: !!rec.free,
        error: rec.error || '',
      });
      r.onsuccess = resolve; r.onerror = () => reject(r.error);
    });
  } catch (e) { /* usage logging must never break a request */ }
}

/** All rows, oldest first. */
export async function allRequests() {
  try {
    const store = await tx('readonly');
    return await new Promise((resolve, reject) => {
      const r = store.getAll();
      r.onsuccess = () => resolve(r.result || []);
      r.onerror = () => reject(r.error);
    });
  } catch (e) { return []; }
}

export async function clearUsage() {
  try {
    const store = await tx('readwrite');
    await new Promise((resolve, reject) => {
      const r = store.clear(); r.onsuccess = resolve; r.onerror = () => reject(r.error);
    });
  } catch (e) { /* ignore */ }
}

/** Delete rows older than `cutoffTs` (retention). Returns count removed. */
export async function pruneOlderThan(cutoffTs) {
  const rows = await allRequests();
  const old = rows.filter((r) => r.ts < cutoffTs);
  if (!old.length) return 0;
  try {
    const store = await tx('readwrite');
    await Promise.all(old.map((r) => new Promise((resolve) => {
      const req = store.delete(r.id); req.onsuccess = resolve; req.onerror = resolve;
    })));
  } catch (e) { /* ignore */ }
  return old.length;
}

// ---- pure aggregation (browser-free, unit-testable) ----

const isOk = (r) => r.status >= 200 && r.status < 400;

export function totals(rows) {
  const t = { requests: rows.length, inputTokens: 0, outputTokens: 0,
              costCredits: 0, ok: 0, latencySum: 0, latencyN: 0 };
  for (const r of rows) {
    t.inputTokens += r.inputTokens | 0;
    t.outputTokens += r.outputTokens | 0;
    t.costCredits += r.costCredits | 0;
    if (isOk(r)) t.ok += 1;
    if (r.latencyMs) { t.latencySum += r.latencyMs; t.latencyN += 1; }
  }
  t.successRate = rows.length ? t.ok / rows.length : 0;
  t.avgLatencyMs = t.latencyN ? Math.round(t.latencySum / t.latencyN) : 0;
  return t;
}

export function byModel(rows) {
  const m = new Map();
  for (const r of rows) {
    const k = r.model || '(unknown)';
    const g = m.get(k) || { model: k, requests: 0, inputTokens: 0, outputTokens: 0, costCredits: 0, ok: 0 };
    g.requests += 1; g.inputTokens += r.inputTokens | 0; g.outputTokens += r.outputTokens | 0;
    g.costCredits += r.costCredits | 0; if (isOk(r)) g.ok += 1;
    m.set(k, g);
  }
  return [...m.values()].sort((a, b) => b.costCredits - a.costCredits);
}

/** Group by local calendar day (YYYY-MM-DD). Pass a Date factory for testing. */
export function byDay(rows, dateOf = (ts) => new Date(ts)) {
  const m = new Map();
  for (const r of rows) {
    const d = dateOf(r.ts);
    const day = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const g = m.get(day) || { day, requests: 0, costCredits: 0, inputTokens: 0, outputTokens: 0 };
    g.requests += 1; g.costCredits += r.costCredits | 0;
    g.inputTokens += r.inputTokens | 0; g.outputTokens += r.outputTokens | 0;
    m.set(day, g);
  }
  return [...m.values()].sort((a, b) => (a.day < b.day ? 1 : -1));
}

export function toCSV(rows) {
  const cols = ['ts', 'iso', 'model', 'provider', 'inputTokens', 'outputTokens',
                'costCredits', 'latencyMs', 'status', 'streamed', 'free', 'error'];
  const esc = (v) => {
    const s = String(v == null ? '' : v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [cols.join(',')];
  for (const r of rows) {
    const iso = new Date(r.ts).toISOString();
    lines.push([r.ts, iso, r.model, r.provider, r.inputTokens, r.outputTokens,
                r.costCredits, r.latencyMs, r.status, r.streamed, r.free, r.error]
      .map(esc).join(','));
  }
  return lines.join('\n');
}
