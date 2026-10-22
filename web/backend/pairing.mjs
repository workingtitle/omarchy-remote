// Device pairing. On the machine, the owner
// starts a short-lived pairing offer (QR code with a one-time token, plus a six-digit code for typing).
// A device claims it with its name and a public device key; the owner approves it on the machine's
// own screen, which proves physical presence once. Approved devices are stored with their key so a
// later transport without Tailscale can authenticate them by signature. /signal, /vnc, /terminal and
// takeover accept only approved devices (PAIRING_REQUIRED=0 lifts it except for /terminal, tests only; config.mjs).
import {randomBytes, randomInt, createHash, createPublicKey, verify as verifySignature} from 'node:crypto';
import {readFile, writeFile, rename, mkdir} from 'node:fs/promises';
import path from 'node:path';

export const OFFER_MS = 5 * 60 * 1000;
export const MAX_ATTEMPTS = 5;          // wrong codes before the offer is withdrawn
export const MAX_PENDING = 4;
export const CHALLENGE_MS = 60 * 1000;
const MAX_CHALLENGES = 64;
const NAME = /^[\p{L}\p{N} ._'()-]{1,48}$/u;

// P-256 public key as JWK; only the public members are kept.
export function publicKey(jwk) {
  if (!jwk || jwk.kty !== 'EC' || jwk.crv !== 'P-256' || jwk.d !== undefined) return null;
  const b64url = /^[A-Za-z0-9_-]{43}$/;
  if (!b64url.test(jwk.x || '') || !b64url.test(jwk.y || '')) return null;
  return {kty: 'EC', crv: 'P-256', x: jwk.x, y: jwk.y};
}
export const fingerprint = key => createHash('sha256').update(`${key.crv}:${key.x}:${key.y}`).digest('hex').slice(0, 16);

export function createPairing({file, now = () => Date.now(), log = console} = {}) {
  let offer = null;                 // {token, code, expires, attempts}
  const pending = new Map();        // id -> {id, name, key, login, created, status}
  let devices = null;               // [{id, name, key, login, approved}]
  const challenges = new Map();     // nonce -> {owner, expires}; single use

  async function load() {
    if (devices) return devices;
    try {
      const data = JSON.parse(await readFile(file, 'utf8'));
      devices = Array.isArray(data.devices) ? data.devices.filter(d => d && publicKey(d.key) && typeof d.name === 'string') : [];
    } catch {devices = [];}
    return devices;
  }
  async function save() {
    await mkdir(path.dirname(file), {recursive: true, mode: 0o700});
    const temporary = `${file}.tmp`;
    await writeFile(temporary, JSON.stringify({devices}, null, 1), {mode: 0o600});
    await rename(temporary, file);
  }
  const live = () => offer && now() < offer.expires ? offer : (offer = null);

  return {
    // Local owner only (see server.mjs). Replaces any previous offer.
    start() {
      offer = {token: randomBytes(16).toString('hex'), code: String(randomInt(0, 1e6)).padStart(6, '0'), expires: now() + OFFER_MS, attempts: 0};
      return {token: offer.token, code: offer.code, expires: offer.expires};
    },
    cancel() {offer = null;},
    // From a device. `secret` is the QR token or the six-digit code.
    claim({secret, name, key, login}) {
      const current = live();
      if (!current) return {error: 'No pairing is open on the machine. Start pairing there first.'};
      const jwk = publicKey(key);
      if (typeof secret !== 'string' || !jwk || typeof name !== 'string' || !NAME.test(name.trim())) return {error: 'Invalid pairing request.'};
      if (secret !== current.token && secret !== current.code) {
        if (++current.attempts >= MAX_ATTEMPTS) offer = null;
        return {error: 'Wrong or expired pairing code.'};
      }
      if (pending.size >= MAX_PENDING) return {error: 'Too many devices are waiting for approval.'};
      offer = null;                                   // one device per offer
      const id = randomBytes(8).toString('hex');
      pending.set(id, {id, name: name.trim(), key: jwk, login: login || null, created: now(), status: 'pending'});
      log.log(`Pairing request from device ${fingerprint(jwk)}`);
      return {id, status: 'pending', fingerprint: fingerprint(jwk)};
    },
    status(id) {return pending.get(id)?.status || null;},
    // Local owner only.
    pendingRequests() {
      for (const [id, request] of pending) if (now() - request.created > OFFER_MS) pending.delete(id);
      return [...pending.values()].filter(r => r.status === 'pending').map(({id, name, key, login}) => ({id, name, login, fingerprint: fingerprint(key)}));
    },
    async decide(id, approve) {
      const request = pending.get(id);
      if (!request || request.status !== 'pending') return false;
      request.status = approve ? 'approved' : 'denied';
      if (approve) {
        const list = await load();
        const fp = fingerprint(request.key);
        const kept = list.filter(d => fingerprint(d.key) !== fp);
        kept.push({id: fp, name: request.name, key: request.key, login: request.login, approved: new Date(now()).toISOString()});
        devices = kept;
        await save();
      }
      log.log(`Pairing ${approve ? 'approved' : 'denied'} for device ${fingerprint(request.key)}`);
      return true;
    },
    async devices() {return (await load()).map(({id, name, login, approved}) => ({id, name, login, approved}));},
    async remove(id) {
      const list = await load();
      devices = list.filter(d => d.id !== id);
      if (devices.length === list.length) return false;
      await save();
      return true;
    },
    // Proof of a paired device: sign a fresh nonce (ECDSA P-256, SHA-256, raw r||s as WebCrypto
    // produces). The nonce is bound to the caller (browser session or tailnet login) and used once.
    challenge(owner) {
      for (const [nonce, c] of challenges) if (now() > c.expires) challenges.delete(nonce);
      if (challenges.size >= MAX_CHALLENGES) challenges.delete(challenges.keys().next().value);
      const nonce = randomBytes(32).toString('hex');
      challenges.set(nonce, {owner, expires: now() + CHALLENGE_MS});
      return nonce;
    },
    async verify({device, nonce, signature, owner}) {
      const c = typeof nonce === 'string' ? challenges.get(nonce) : null;
      if (!c) return null;
      challenges.delete(nonce);
      if (c.owner !== owner || now() > c.expires || typeof device !== 'string' || typeof signature !== 'string') return null;
      const record = (await load()).find(d => d.id === device);
      if (!record) return null;
      try {
        const key = createPublicKey({key: record.key, format: 'jwk'});
        const ok = verifySignature('sha256', Buffer.from(nonce), {key, dsaEncoding: 'ieee-p1363'}, Buffer.from(signature, 'base64url'));
        return ok ? {id: record.id, name: record.name} : null;
      } catch {return null;}
    },
    get open() {return !!live();},
  };
}
