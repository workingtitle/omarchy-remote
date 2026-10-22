// Pairing page: claim the machine's pairing offer with this device's name and public key, then
// wait for approval on the machine. The private key is created non-extractable and kept in this
// browser's IndexedDB, so a later transport can prove it is the same device.
import {deviceKey, saveDeviceId} from './device-key.mjs';
import {installTheme} from './theme.mjs';
const $ = id => document.getElementById(id);
const token = /^#([a-f0-9]{32})$/.exec(location.hash)?.[1] || null;
history.replaceState(null, '', location.pathname);   // keep the one-time token out of history
if (token) $('code-row').hidden = true;
installTheme();
// On an iPhone or iPad (iPadOS reports itself as a Mac with touch) a scanned QR code opens this page in
// Safari; offer to hand the one-time token to the app instead (omarchyremote://, clients/ios PairingLink).
const appleMobile = /iPhone|iPad/.test(navigator.userAgent) || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
// On a Mac the Omarchy Remote app handles omarchyremote:// too; this page would pair only the browser.
const mac = /Macintosh/.test(navigator.userAgent) && !appleMobile;
if (mac && !token) $('mac-hint').hidden = false;
// Without the browser client (the testers' checkout) this page only hands over to the apps: it pairs no browser.
const browser = await fetch('index.html', {method: 'HEAD'}).then(r => r.ok, () => false);
if (token && (appleMobile || mac)) {
  $('app-link').href = `omarchyremote://pair?${new URLSearchParams({machine: location.origin, token})}`;
  $('app-row').hidden = false;
  $('form').hidden = true;
  $('browser-toggle').addEventListener('click', () => {$('app-row').hidden = true; $('form').hidden = false;});
}
if (!browser) {
  $('form').hidden = true;
  $('mac-hint').hidden = true;
  $('app-row').querySelector('p').textContent = 'Pairs the Omarchy Remote app on this device.';
  if ($('app-row').hidden) $('intro').textContent = 'Scan the QR code on the machine with the camera of your iPhone or iPad, or enter the six-digit code in the Mac app (Add a Machine).';
}
$('name').value = /iPad/.test(navigator.userAgent) ? 'iPad' : /iPhone/.test(navigator.userAgent) ? 'iPhone' : /Mac/.test(navigator.userAgent) ? 'Mac' : 'Browser';
const say = text => {$('message').textContent = text;};

async function post(endpoint, body, csrf) {
  const response = await fetch(`/api/pair/${endpoint}`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Server returned ${response.status}`);
  return data;
}

$('form').addEventListener('submit', async event => {
  event.preventDefault();
  const secret = token || $('code').value.trim();
  if (!/^([a-f0-9]{32}|[0-9]{6})$/.test(secret)) return say('Enter the six-digit code shown on the machine.');
  $('submit').disabled = true;
  try {
    const session = await (await fetch('/api/session')).json();
    const key = await crypto.subtle.exportKey('jwk', (await deviceKey()).publicKey);
    const claim = await post('claim', {secret, name: $('name').value.trim(), key: {kty: key.kty, crv: key.crv, x: key.x, y: key.y}}, session.csrf);
    say(`Waiting for approval on the machine. Device check: ${claim.fingerprint.slice(0, 4)} ${claim.fingerprint.slice(4, 8)}`);
    const until = Date.now() + 5 * 60 * 1000;
    while (Date.now() < until) {
      await new Promise(resolve => setTimeout(resolve, 1500));
      const {status} = await post('status', {id: claim.id}, session.csrf);
      if (status === 'approved') {await saveDeviceId(claim.fingerprint); say('Paired.'); $('message').insertAdjacentHTML('beforeend', ' <a href="/">Open the remote desktop</a>'); return;}
      if (status === 'denied') {say('The request was declined on the machine.'); return;}
    }
    say('No decision within five minutes. Start pairing again on the machine.');
  } catch (error) {
    say(error.message);
  } finally {
    $('submit').disabled = false;
  }
});
if (token && browser) say('Pairing code received from the QR code.');
