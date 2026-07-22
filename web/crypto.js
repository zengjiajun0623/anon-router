// crypto.js — passphrase encryption for anon-router wallet backups.
//
// A wallet backup is bearer money: anyone holding the plaintext {account, tokens}
// can spend it. This module encrypts that payload under a user passphrase using
// Web Crypto only (AES-256-GCM with a PBKDF2-SHA256-derived key) so the exported
// file is useless without the passphrase. No dependencies, no external loads;
// runs the same in the browser and under Node >= 20 (global webcrypto).
//
// Envelope format (JSON-safe, self-describing so we can migrate KDF params later):
//   { v, kdf, iterations, salt(b64), iv(b64), ct(b64) }
// AES-GCM authenticates the ciphertext, so a wrong passphrase (or any tampering)
// fails decryption with an error rather than returning garbage.

const ENC = new TextEncoder();
const DEC = new TextDecoder();
const KDF_ITERATIONS = 210000;   // OWASP-2023 floor for PBKDF2-SHA256

function b64(buf) {
  return btoa(String.fromCharCode(...new Uint8Array(buf)));
}
function unb64(s) {
  return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
}

async function deriveKey(passphrase, salt, iterations) {
  const base = await crypto.subtle.importKey(
    'raw', ENC.encode(passphrase), 'PBKDF2', false, ['deriveKey']);
  return crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations, hash: 'SHA-256' },
    base, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
}

/** Encrypt a JSON-serializable object under `passphrase`. Returns the envelope. */
export async function encryptJSON(obj, passphrase) {
  if (!passphrase) throw new Error('passphrase required');
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await deriveKey(passphrase, salt, KDF_ITERATIONS);
  const ct = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv }, key, ENC.encode(JSON.stringify(obj)));
  return {
    v: 1, kdf: 'PBKDF2-SHA256', iterations: KDF_ITERATIONS,
    salt: b64(salt), iv: b64(iv), ct: b64(ct),
  };
}

// ---- key-reuse API for the live at-rest vault ----
// The backup helpers above re-derive the key each call (fine for a one-shot
// file). The live wallet re-encrypts on every change, so we derive the key ONCE
// on unlock (210k PBKDF2 is deliberately slow) and reuse the CryptoKey for all
// subsequent seals. The salt is fixed per vault (so the same passphrase re-opens
// it); a fresh IV is drawn per seal.

/** Derive (and cache-able) an AES-GCM key from a passphrase. Pass an existing
 *  salt (b64) to re-open a vault, or null to mint a new one. */
export async function deriveVaultKey(passphrase, saltB64) {
  if (!passphrase) throw new Error('passphrase required');
  const salt = saltB64 ? unb64(saltB64) : crypto.getRandomValues(new Uint8Array(16));
  const key = await deriveKey(passphrase, salt, KDF_ITERATIONS);
  return { key, salt: b64(salt), iterations: KDF_ITERATIONS };
}

/** Seal an object with an already-derived key. Fresh IV every call. */
export async function sealWithKey(obj, key, saltB64, iterations) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv }, key, ENC.encode(JSON.stringify(obj)));
  return {
    v: 1, kdf: 'PBKDF2-SHA256', iterations: iterations || KDF_ITERATIONS,
    salt: saltB64, iv: b64(iv), ct: b64(ct),
  };
}

/** Open a vault envelope with an already-derived key. Throws on wrong key. */
export async function openWithKey(env, key) {
  if (!env || env.v !== 1 || typeof env.ct !== 'string') {
    throw new Error('unsupported or corrupt vault');
  }
  let pt;
  try {
    pt = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: unb64(env.iv) }, key, unb64(env.ct));
  } catch (e) {
    throw new Error('wrong passphrase or corrupt vault');
  }
  return JSON.parse(DEC.decode(pt));
}

/** Decrypt an envelope produced by encryptJSON. Throws on a wrong passphrase,
 *  tampering, or an unsupported format (AES-GCM auth tag catches all three). */
export async function decryptJSON(env, passphrase) {
  if (!env || env.v !== 1 || typeof env.ct !== 'string') {
    throw new Error('unsupported or corrupt vault format');
  }
  const key = await deriveKey(passphrase, unb64(env.salt), env.iterations || KDF_ITERATIONS);
  let pt;
  try {
    pt = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: unb64(env.iv) }, key, unb64(env.ct));
  } catch (e) {
    throw new Error('wrong passphrase or corrupt backup');
  }
  return JSON.parse(DEC.decode(pt));
}
