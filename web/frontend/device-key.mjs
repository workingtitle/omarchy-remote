// This browser's device identity for pairing: a non-extractable ECDSA P-256 key pair in IndexedDB
// plus the id (key fingerprint) the machine assigned when the device was approved.
function database() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open('omarchy-remote', 1);
    request.onupgradeneeded = () => request.result.createObjectStore('keys');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
async function get(name) {
  const db = await database();
  return new Promise(resolve => {const r = db.transaction('keys').objectStore('keys').get(name); r.onsuccess = () => resolve(r.result ?? null); r.onerror = () => resolve(null);});
}
async function put(name, value) {
  const db = await database();
  await new Promise((resolve, reject) => {const t = db.transaction('keys', 'readwrite'); t.objectStore('keys').put(value, name); t.oncomplete = resolve; t.onerror = () => reject(t.error);});
}

export async function deviceKey() {
  const stored = await get('device');
  if (stored) return stored;
  const pair = await crypto.subtle.generateKey({name: 'ECDSA', namedCurve: 'P-256'}, false, ['sign', 'verify']);
  await put('device', pair);
  return pair;
}
export const saveDeviceId = id => put('deviceId', id);

const base64url = buffer => btoa(String.fromCharCode(...new Uint8Array(buffer))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

// {device, nonce, sig} for /signal, or null when this browser was never paired.
export async function deviceProof(csrf) {
  const [pair, device] = await Promise.all([get('device'), get('deviceId')]).catch(() => [null, null]);
  if (!pair || !device) return null;
  const response = await fetch('/api/pair/challenge', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: '{}'});
  if (!response.ok) return null;
  const {nonce} = await response.json();
  const signature = await crypto.subtle.sign({name: 'ECDSA', hash: 'SHA-256'}, pair.privateKey, new TextEncoder().encode(nonce));
  return {device, nonce, sig: base64url(signature)};
}
