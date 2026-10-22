// Loopback HTTP server: Tailscale identity and Host validation, session cookies
// with CSRF and Origin checks, static frontend, clipboard endpoints and the
// authorized WebSocket upgrades that hand a connection to wayvnc.mjs (RFB) or webrtc.mjs
// (signaling for the native sender). One controlling client at a time across both.
import http from 'node:http';
import path from 'node:path';
import {readFile} from 'node:fs/promises';
import {randomBytes} from 'node:crypto';
import {WebSocketServer} from 'ws';
import {port, publicHost, localHosts, allowedHosts, allowedLogin, frontendDir, browserClient, leaseMs, sessionTtlMs, maxSessions, maxInputBytes, pairingRequired, maxClipboardBytes, maxClipboardImageBytes} from './config.mjs';
import {desktop, workspaces} from './desktop.mjs';
import {readRequestText, copyText, pasteText, copyImage, decodePng} from './clipboard.mjs';
import * as wayvnc from './wayvnc.mjs';
import * as webrtc from './webrtc.mjs';
import * as portalHealth from './portal-health.mjs';
import * as terminal from './terminal.mjs';
import {createPairing} from './pairing.mjs';
import os from 'node:os';
import {startAudio, restoreSpeakers, restoreStaleMute} from './audio.mjs';
import {publicTheme, themeFont, terminalTheme} from './theme.mjs';
import * as lock from './lock.mjs';
import * as controls from './controls.mjs';
import {createApprovals} from './approval.mjs';
import {createStaticFiles} from './static.mjs';
import {createShortcuts, MAX_BYTES as maxShortcutBytes} from './shortcuts.mjs';

const CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";

const sessions = new Map();
const unlockAttempts = lock.attemptLimiter();
const approvals = createApprovals();
const pairing = createPairing({file: path.join(os.homedir(), '.local/state/omarchy-remote/devices.json')});
const shortcuts = createShortcuts({file: path.join(os.homedir(), '.config/omarchy-remote/shortcuts.json')});
const serveStatic = createStaticFiles(frontendDir);
const token = () => randomBytes(32).toString('hex');
const json = (res, code, body) => {
  res.writeHead(code, {'Content-Type': 'application/json'});
  res.end(JSON.stringify(body));
};

// Authorization -----------------------------------------------------------------
const isLocal = req => localHosts.has(req.headers.host);

function trusted(req) {
  if (!allowedHosts.has(req.headers.host)) return false;
  if (!isLocal(req) && !req.headers['tailscale-user-login']) return false;
  return !allowedLogin || isLocal(req) || req.headers['tailscale-user-login'] === allowedLogin;
}

function sameOrigin(req) {
  const expected = `${isLocal(req) ? 'http' : 'https'}://${req.headers.host}`;
  return req.headers.origin === expected;
}

function session(req) {
  const id = /(?:^|;\s*)omarchy_session=([a-f0-9]{64})(?:;|$)/.exec(req.headers.cookie || '')?.[1];
  const s = sessions.get(id);
  if (s && Date.now() - s.seen < sessionTtlMs) {
    s.seen = Date.now();
    return s;
  }
}

function authenticated(req) {
  const s = session(req);
  return s && req.headers['x-csrf-token'] === s.csrf && sameOrigin(req) ? s : null;
}

// The desktop has one controller, whichever transport it uses.
const controller = () => wayvnc.activeConnection() || webrtc.activeConnection();
async function stopController(reason) {
  await wayvnc.stop(wayvnc.activeConnection(), reason);
  await webrtc.stop(webrtc.activeConnection(), reason);
}

// Native clients (Mac, iOS) send no Origin and hold no cookie: a browser always sends Origin on a
// WebSocket upgrade, so its absence marks a non-browser caller, authorized by tailnet identity
// (trusted()) alone. One synthetic session per login keeps ownership checks uniform.
const nativeSessions = new Map();
function nativeSession(req) {
  if (req.headers.origin !== undefined) return null;
  const login = isLocal(req) ? 'local' : req.headers['tailscale-user-login'];
  if (!login) return null;
  if (!nativeSessions.has(login)) nativeSessions.set(login, {id: `native:${login}`, native: true});
  return nativeSessions.get(login);
}

// The session's own desktop connection, WayVNC or WebRTC, named by the request's connection id.
function ownsActive(s, req) {
  const id = req.headers['x-connection-id'];
  const vnc = wayvnc.activeConnection(), rtc = webrtc.activeConnection();
  if (wayvnc.owns(vnc, s, id)) return vnc;
  return webrtc.owns(rtc, s, id) ? rtc : null;
}

// The machine's owner: a loopback request without Origin (a local CLI or bar widget), never a
// tailnet caller and never a web page (browsers always send Origin on POST and on fetch).
const localOwner = req => isLocal(req) && req.headers.origin === undefined && !req.headers['tailscale-user-login'];

async function readJson(req, limit = 8192) {
  let size = 0; const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new Error('Request too large');
    chunks.push(chunk);
  }
  const body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('Invalid request');
  return body;
}

// Who a device challenge is bound to: the browser session, or the tailnet login for native clients.
const proofOwner = (req, native) => native ? `native:${isLocal(req) ? 'local' : req.headers['tailscale-user-login']}` : `session:${session(req)?.id}`;

const deviceActions = {
  '/api/device/windows': async () => ({windows: await controls.listWindows()}),
  '/api/device/focus': async body => {await controls.focusWindow(body.address); return {ok: true};},
  '/api/device/close': async body => {await controls.closeWindow(body.address); return {ok: true};},
  '/api/device/workspace': async body => {await controls.showWorkspace(body.workspace); return {ok: true};},
  '/api/device/move': async body => {await controls.moveWindow(body.address, body.workspace, body.follow); return {ok: true};},
  // Approving sudo with Face ID / Touch ID (approval.mjs): the pending requests, and the answer to one. The device
  // signs the proof; the biometric check happens in the app before it answers.
  '/api/device/approvals': async () => {approvals.seen(); return {approvals: approvals.list()};},
  '/api/device/approve': async (body, device) => ({ok: approvals.decide(body.id, body.allow === true, device?.id)}),
  // Clipboard between a device and the machine (an explicit action in the app, not a background sync): the text to put on the
  // machine's clipboard, paste it into the focused window, or copy the focused window's selection and return it.
  '/api/device/clipboard-set': async body => {
    if (typeof body.text !== 'string' || body.text.includes('\0') || Buffer.byteLength(body.text) > maxClipboardBytes) throw new Error('Invalid clipboard text');
    await copyText(body.text);
    if (body.paste === true) await controls.clipboardKey('paste');
    return {ok: true};
  },
  // A picture (PNG, base64) for the machine's clipboard: the terminal's "paste image" puts it there and then sends Ctrl+V.
  '/api/device/clipboard-image': async body => {
    await copyImage(decodePng(body.png));
    return {ok: true};
  },
  '/api/device/clipboard-copy': async () => {
    await controls.clipboardKey('copy');
    await new Promise(resolve => setTimeout(resolve, 300));
    return {text: await pasteText().catch(() => '')};
  },
  // Copy, Paste and Cut of the shortcuts menu: the focused window's own keys (a terminal's differ), sent by the compositor.
  '/api/device/clipboard-key': async body => {await controls.clipboardKey(String(body.kind)); return {ok: true};},
  '/api/device/apps': async () => ({apps: await controls.listApps()}),
  '/api/device/icons': async body => ({icons: await controls.iconPngs(body.names, await controls.listApps())}),
  '/api/device/launch': async body => {await controls.launchApp(body.id); return {ok: true};},
  '/api/device/run': async body => {controls.runCommand(body.command); return {ok: true};},
  // The user's own shortcuts, the same on every paired device (shortcuts.mjs): read, and save over a known revision.
  '/api/device/shortcuts': async () => shortcuts.read(),
  '/api/device/shortcuts-save': async body => shortcuts.save(body.revision, body.data),
};

// Pairing: owner endpoints under /api/local/, device endpoints under /api/pair/ and /api/device/.
async function handlePairing(req, res, pathname) {
  if (pathname.startsWith('/api/local/')) {
    if (!localOwner(req)) return json(res, 403, {error: 'Only available on the machine itself'});
    if (pathname === '/api/local/status' && req.method === 'GET')
      return json(res, 200, {busy: !!controller(), transport: webrtc.activeConnection() ? 'webrtc' : wayvnc.activeConnection() ? 'vnc' : null, pairingOpen: pairing.open, pending: pairing.pendingRequests().length});
    // The PAM helper's questions (scripts/omarchy-remote-sudo-approve): is a device connected, and may this sudo run?
    if (pathname === '/api/local/approval/ready' && req.method === 'GET') return json(res, 200, {ready: approvals.ready()});
    if (req.method !== 'POST') return json(res, 405, {error: 'Method not allowed'});
    if (pathname === '/api/local/approval/request') {
      const body = await readJson(req).catch(() => ({}));
      return json(res, 200, {decision: await approvals.request({service: body.service, user: body.user, command: body.command})});
    }
    if (pathname === '/api/local/pair/start') return json(res, 200, {...pairing.start(), url: `https://${publicHost}/pair.html`, browser: browserClient});
    if (pathname === '/api/local/pair/cancel') {pairing.cancel(); return json(res, 200, {ok: true});}
    if (pathname === '/api/local/pair/pending') return json(res, 200, {pending: pairing.pendingRequests()});
    if (pathname === '/api/local/pair/decide') {
      const body = await readJson(req);
      return json(res, 200, {ok: await pairing.decide(String(body.id || ''), body.approve === true)});
    }
    if (pathname === '/api/local/devices') return json(res, 200, {devices: await pairing.devices()});
    if (pathname === '/api/local/devices/remove') {const body = await readJson(req); return json(res, 200, {ok: await pairing.remove(String(body.id || ''))});}
    return json(res, 404, {error: 'Unknown endpoint'});
  }
  // Devices: a browser with session + CSRF + Origin, or a native client (no Origin) by tailnet identity.
  const native = req.headers.origin === undefined && (isLocal(req) || req.headers['tailscale-user-login']);
  if (!native && !authenticated(req)) return json(res, 403, {error: 'Session and same-origin token required'});
  const login = req.headers['tailscale-user-login'] || null;
  if (pathname === '/api/pair/claim' && req.method === 'POST') {
    const body = await readJson(req);
    const result = pairing.claim({secret: body.secret, name: body.name, key: body.key, login});
    return json(res, result.error ? 400 : 200, result);
  }
  if (pathname === '/api/pair/challenge' && req.method === 'POST')
    return json(res, 200, {nonce: pairing.challenge(proofOwner(req, native))});
  // Screen lock: its state for any device; unlocking only for a native client with a paired device's proof
  // (the password comes from the device's Face ID-protected Keychain; a browser has no equivalent).
  // Also the status strip's data: workspaces and the machine's clock (HH:MM, its own time zone).
  if (pathname === '/api/device/lock' && req.method === 'POST') {
    const now = new Date();
    const isLocked = await lock.locked();
    approvals.seen();   // a connected device polls this: sudo approvals can reach it
    return json(res, 200, {locked: isLocked, ready: isLocked ? await lock.lockReady() : null, approvals: approvals.count(), screensaver: await lock.screensaver(),
      ...await workspaces().catch(() => ({})),
      time: `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`});
  }
  // The machine's kept terminals, for a paired device to pick one up (the terminal itself is then attached by its id).
  if (pathname === '/api/device/terminals' && req.method === 'POST') {
    if (!native) return json(res, 403, {error: 'Terminals are available in the native apps.'});
    const body = await readJson(req).catch(() => null);
    if (!body) return json(res, 400, {error: 'Invalid request.'});
    const device = await pairing.verify({device: String(body.device || ''), nonce: String(body.nonce || ''), signature: String(body.sig || ''), owner: proofOwner(req, true)});
    if (!device) return json(res, 401, {error: 'Pair this device with the machine first.'});
    return json(res, 200, {terminals: terminal.list(), max: terminal.MAX_TERMINALS});
  }
  // Waking from the screensaver needs no proof: it only ends the animation (a lock behind it stays).
  if (pathname === '/api/device/wake' && req.method === 'POST') return json(res, 200, {woke: await lock.dismissScreensaver()});
  if (pathname === '/api/device/unlock' && req.method === 'POST') {
    if (!native) return json(res, 403, {error: 'Unlocking is available in the native apps.'});
    // Parse errors can quote the body, which holds the password: answer without logging them.
    const body = await readJson(req).catch(() => null);
    if (!body) return json(res, 400, {error: 'Invalid request.'});
    const device = await pairing.verify({device: String(body.device || ''), nonce: String(body.nonce || ''), signature: String(body.sig || ''), owner: proofOwner(req, true)});
    if (!device) return json(res, 401, {error: 'Pair this device with the machine first.'});
    if (!lock.validPassword(body.password)) return json(res, 400, {error: 'Invalid password.'});
    if (!unlockAttempts()) return json(res, 429, {error: 'Too many attempts. Wait a minute.'});
    const result = await lock.unlock(body.password);
    console.log(`Unlock by paired device ${device.id}: ${result.body.unlocked === true ? 'unlocked' : result.body.unlocked === false ? 'still locked' : result.status}`);
    return json(res, result.status, result.body);
  }
  // Shortcuts menu of the native apps: windows, apps, a command line. A paired device's proof, like unlocking.
  if (deviceActions[pathname] && req.method === 'POST') {
    if (!native) return json(res, 403, {error: 'Available in the native apps.'});
    const body = await readJson(req, pathname === '/api/device/clipboard-set' ? maxClipboardBytes + 4096 : pathname === '/api/device/clipboard-image' ? Math.ceil(maxClipboardImageBytes * 4 / 3) + 4096 : pathname === '/api/device/shortcuts-save' ? maxShortcutBytes + 4096 : undefined).catch(() => null);   // answered without the parser's message: a command may be in it
    if (!body) return json(res, 400, {error: 'Invalid request.'});
    const device = await pairing.verify({device: String(body.device || ''), nonce: String(body.nonce || ''), signature: String(body.sig || ''), owner: proofOwner(req, true)});
    if (!device) return json(res, 401, {error: 'Pair this device with the machine first.'});
    try {
      const result = await deviceActions[pathname](body, device);
      if (pathname === '/api/device/run' || pathname === '/api/device/launch') console.log(`Device ${device.id}: ${pathname.slice('/api/device/'.length)}`);
      return json(res, 200, result);
    } catch (error) {
      return json(res, 400, {error: String(error.message || 'Failed').slice(0, 120)});
    }
  }
  if (pathname === '/api/pair/status' && req.method === 'POST') {
    const body = await readJson(req);
    const status = pairing.status(String(body.id || ''));
    return status ? json(res, 200, {status}) : json(res, 404, {error: 'Unknown pairing request'});
  }
  return json(res, 404, {error: 'Unknown endpoint'});
}

// HTTP ------------------------------------------------------------------------
async function handleSession(req, res) {
  let s = session(req);
  if (!s) {
    if (sessions.size >= maxSessions) return json(res, 429, {error: 'Too many sessions'});
    const id = token();
    s = {id, csrf: token(), seen: Date.now()};
    sessions.set(id, s);
    res.setHeader('Set-Cookie', `omarchy_session=${id}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400${isLocal(req) ? '' : '; Secure'}`);
  }
  return json(res, 200, {csrf: s.csrf, ...await desktop(), busy: !!controller(), leaseMs, pairingRequired});
}

async function handleApi(req, res, pathname) {
  const s = authenticated(req);
  if (!s || req.method !== 'POST') return json(res, 403, {error: 'Session and same-origin token required'});
  const active = ownsActive(s, req);
  switch (pathname) {
    case '/api/heartbeat':
      if (active && active === wayvnc.activeConnection()) wayvnc.renew(active);   // WebRTC has its own session heartbeat
      return json(res, 200, {active: !!active, busy: !!controller()});
    case '/api/release':
      if (active) await (active === wayvnc.activeConnection() ? wayvnc : webrtc).stop(active, 'Client released session');
      return json(res, 200, {ok: true});
    case '/api/takeover': {
      // Explicit user action from a paired browser: end the other client's session with the usual ordered key
      // release, then this one connects. Without the proof any tailnet member could end a paired session.
      const body = await readJson(req).catch(() => ({}));
      const device = await pairing.verify({device: String(body.device || ''), nonce: String(body.nonce || ''), signature: String(body.sig || ''), owner: proofOwner(req, false)});
      if (pairingRequired && !device) return json(res, 401, {error: 'Pair this device with the machine first.'});
      if (controller() && !active) await stopController('Taken over by another device');
      return json(res, 200, {ok: true});
    }
    case '/api/clipboard/write':
    case '/api/clipboard/read':
      if (!active || active.stopping) return json(res, 409, {error: 'Connect before accessing the remote clipboard'});
      if (pathname.endsWith('/write')) {
        await copyText(await readRequestText(req));
        return json(res, 200, {ok: true});
      }
      return json(res, 200, {text: await pasteText()});
    default:
      return json(res, 404, {error: 'Unknown endpoint'});
  }
}

// Theme colors and the current Omarchy font file; read-only and cacheable.
async function handleTheme(req, res, pathname) {
  if (!['GET', 'HEAD'].includes(req.method)) return json(res, 405, {error: 'Method not allowed'});
  if (pathname === '/api/theme') return json(res, 200, await publicTheme());
  const weight = /^\/theme\/font-(regular|bold)$/.exec(pathname)?.[1];
  const font = weight && await themeFont(weight);
  if (!font) return json(res, 404, {error: 'Not found'});
  res.setHeader('Cache-Control', 'max-age=86400');
  res.setHeader('Content-Type', font.type);
  res.end(req.method === 'HEAD' ? undefined : await readFile(font.file));
}

async function handleStatic(req, res, pathname) {
  if (!['GET', 'HEAD'].includes(req.method)) return json(res, 405, {error: 'Method not allowed'});
  // One browser client (index.html, WebRTC or WayVNC inside). The earlier pages stay as addresses: /vnc.html
  // serves it too, /webrtc.html (the earlier separate WebRTC page) redirects with its query.
  if (pathname === '/webrtc.html') {
    res.writeHead(302, {Location: `/${new URL(req.url, 'http://localhost').search}`});
    return res.end();
  }
  if (pathname === '/' || pathname === '/vnc.html') return serveStatic(req, res, browserClient ? '/index.html' : '/apps.html');
  return serveStatic(req, res, pathname);
}

const server = http.createServer(async (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('Content-Security-Policy', CSP);
  if (!trusted(req)) return json(res, 403, {error: 'Use the configured Tailscale HTTPS address.'});
  try {
    const {pathname} = new URL(req.url, 'http://localhost');
    if (pathname === '/api/session' && req.method === 'GET') return await handleSession(req, res);
    if (pathname === '/api/theme' || pathname.startsWith('/theme/')) return await handleTheme(req, res, pathname);
    if (pathname.startsWith('/api/local/') || pathname.startsWith('/api/pair/') || pathname.startsWith('/api/device/')) return await handlePairing(req, res, pathname);
    if (pathname.startsWith('/api/')) return await handleApi(req, res, pathname);
    return await handleStatic(req, res, pathname);
  } catch (error) {
    console.error(error.message);
    json(res, 503, {error: 'Desktop unavailable; check the user service journal.'});
  }
});
server.requestTimeout = 10000;
server.headersTimeout = 10000;

// WebSocket upgrade --------------------------------------------------------------
const wss = new WebSocketServer({noServer: true, maxPayload: maxInputBytes, perMessageDeflate: false});

server.on('upgrade', async (req, socket, head) => {
  const deny = code => {socket.end(`HTTP/1.1 ${code}\r\nConnection: close\r\n\r\n`);};
  let connection = null;
  try {
    const url = new URL(req.url, 'http://localhost');
    if (!trusted(req) || !['/vnc', '/audio', '/signal', '/terminal'].includes(url.pathname)) return deny('403 Forbidden');
    const native = ['/signal', '/terminal'].includes(url.pathname) ? nativeSession(req) : null;
    const s = native || session(req);
    if (!s || (!native && (!sameOrigin(req) || url.searchParams.get('token') !== s.csrf))) return deny('403 Forbidden');
    const id = url.searchParams.get('connection');
    if (!/^[a-f0-9-]{36}$/.test(id || '')) return deny('400 Bad Request');
    if (url.pathname === '/terminal') {
      // A shell is full account access: always require a paired device's signed proof.
      const device = url.searchParams.get('nonce') ? await pairing.verify({
        device: url.searchParams.get('device'), nonce: url.searchParams.get('nonce'),
        signature: url.searchParams.get('sig'), owner: proofOwner(req, !!native)}) : null;
      if (!device) return deny('401 Unauthorized');
      const keep = url.searchParams.get('keep') === '1';
      if (terminal.count() >= terminal.MAX_TERMINALS && !(keep && terminal.canResume(id)) && !terminal.evictDetached(device.id)) return deny('409 Conflict');
      const cols = Number(url.searchParams.get('cols')) || 80, rows = Number(url.searchParams.get('rows')) || 24;
      console.log(`Terminal for paired device ${device.id}`);
      const cwd = url.searchParams.get('here') === '0' ? null : await terminal.focusedDirectory();
      const theme = await terminalTheme();
      return wss.handleUpgrade(req, socket, head, ws => terminal.start(ws, {cols: Math.min(1000, Math.max(1, cols)), rows: Math.min(1000, Math.max(1, rows)), cwd, theme, id, device: device.id, keep}));
    }
    if (url.pathname === '/signal') {
      // Paired-device proof (device, nonce, sig); always required, unless PAIRING_REQUIRED=0 (tests only).
      const device = url.searchParams.get('nonce') ? await pairing.verify({
        device: url.searchParams.get('device'), nonce: url.searchParams.get('nonce'),
        signature: url.searchParams.get('sig'), owner: proofOwner(req, !!native)}) : null;
      if (pairingRequired && !device) return deny('401 Unauthorized');
      if (device) console.log(`Signal from paired device ${device.id}`);
      // Same tailnet identity and an explicit request: end the other controller first.
      if (controller() && url.searchParams.get('takeover') === '1') await stopController('Taken over by another device');
      // The same paired device connecting again (app restart, network change) replaces its own stale session
      // at once instead of waiting for the 15 s heartbeat expiry.
      const own = webrtc.activeConnection();
      if (device && own?.device === device.id) await webrtc.stop(own, 'Replaced by the same device');
      if (wayvnc.activeConnection()) return deny('409 Conflict');
      const connection = webrtc.reserve(s, id, device?.id || null);
      if (!connection) return deny('409 Conflict');
      if (socket.destroyed) return webrtc.unreserve(connection);
      // Picture quality the client asks for (web/backend/webrtc.mjs senderCommand; the sender decides what the
      // machine can do). Only the two fixed values reach the command line.
      // Display mode: remote = a headless output sized for the client with the physical screens off for the
      // session, extend = that output as an extra screen beside them (side=left for the left; virtual_display.py); anything
      // else mirrors the physical screen.
      const requested = ['remote', 'extend'].includes(url.searchParams.get('display')) ? url.searchParams.get('display') : null;
      // A locked machine keeps its screens: switching outputs under the lock screen made Quickshell's lock client die
      // (Hyprland's "lockscreen app died" screen). The session mirrors; unlock, then reconnect for the
      // remote display.
      const display = requested && await lock.locked().catch(() => null) !== true ? requested : null;
      const extra = [...(url.searchParams.get('quality') === 'sharp' ? ['--quality', 'sharp'] : []),
                     ...webrtc.bandwidthArgs(url.searchParams.get('bw')),
                     ...(display ? ['--display', display] : []),
                     ...(display === 'extend' && url.searchParams.get('side') === 'left' ? ['--side', 'left'] : [])];
      const command = extra.length ? [...webrtc.senderCommand(), ...extra] : undefined;
      connection.display = display;
      connection.pendingDisplay = requested && !display ? requested : null;
      await webrtc.settleDisplays(Boolean(display));
      if (socket.destroyed) {webrtc.unreserve(connection); webrtc.scheduleRestore(); return;}
      // Someone works on the machine remotely (its own screens are off): keep it awake (backend/awake.mjs) for the session. A
      // mirroring or extending session is used at the machine as well, which counts as activity there.
      if (display === 'remote') webrtc.holdAwake();
      // A remote session starts with the desktop visible: end a running screensaver (a lock behind it stays).
      lock.dismissScreensaver().catch(() => {});
      if (!display) lock.wakeScreens().catch(() => {});
      // ws drops an upgrade whose socket is already half closed (the page went away mid-handshake) without calling
      // back; without this the reservation stayed and the machine looked busy until the service restarted.
      socket.once('close', () => {if (!connection.ws) {webrtc.unreserve(connection); webrtc.scheduleRestore();}});
      return wss.handleUpgrade(req, socket, head, ws => webrtc.start(connection, ws, {command}));
    }
    if (url.pathname === '/audio') {
      // Audio rides on the active desktop connection and stops with it.
      const active = wayvnc.activeConnection();
      if (!wayvnc.owns(active, s, id) || active.stopping) return deny('409 Conflict');
      active.audio?.close(1000, 'Replaced');
      return wss.handleUpgrade(req, socket, head, async ws => {
        active.audio = ws;
        ws.on('message', () => ws.close(1003, 'Audio is one-way'));
        try {await startAudio(ws, {muteLocal: url.searchParams.get('mute') === '1'});} catch (error) {console.error(error.message); ws.close(1011, 'Audio unavailable');}
      });
    }
    // The WayVNC client needs the same paired-device proof as /signal (unless PAIRING_REQUIRED=0, tests only).
    const device = url.searchParams.get('nonce') ? await pairing.verify({
      device: url.searchParams.get('device'), nonce: url.searchParams.get('nonce'),
      signature: url.searchParams.get('sig'), owner: proofOwner(req, false)}) : null;
    if (pairingRequired && !device) return deny('401 Unauthorized');
    if (device) console.log(`WayVNC from paired device ${device.id}`);
    if (webrtc.activeConnection()) return deny('409 Conflict');
    connection = wayvnc.reserve(s, id, socket);
    if (!connection) return deny('409 Conflict');
    socket.once('close', () => {if (!connection.ws) wayvnc.stop(connection, 'Upgrade socket closed');});

    const info = await desktop();
    const output = url.searchParams.get('output') || info.monitors[0]?.name;
    if (!info.monitors.some(m => m.name === output)) {
      wayvnc.unreserve(connection);
      return deny('400 Bad Request');
    }
    if (socket.destroyed) {
      wayvnc.unreserve(connection);
      return;
    }
    wss.handleUpgrade(req, socket, head, ws => wayvnc.start(connection, ws, {output, layout: info.keyboardLayout || 'us'}));
  } catch (error) {
    if (connection && !connection.ws) wayvnc.unreserve(connection);
    console.error(error.message);
    deny('503 Service Unavailable');
  }
});

// Housekeeping ------------------------------------------------------------------
// The screen-cast portal's backend leaks and finally spins: restart it while nobody is connected (portal-health.mjs).
portalHealth.watch({isBusy: () => !!controller()});
// A session that asked for the remote display but started while the machine was locked mirrors (see above). Once the
// machine has stayed unlocked for a few seconds the session ends; the app reconnects by itself and now gets the display.
let unlockedSince = 0;
let unlockWatch = false;
setInterval(async () => {
  const active = webrtc.activeConnection();
  if (!active?.pendingDisplay || active.stopping) {unlockedSince = 0; return;}
  if (unlockWatch) return;
  unlockWatch = true;
  try {
    if (await lock.locked().catch(() => null) !== false) unlockedSince = 0;
    else if (!unlockedSince) unlockedSince = Date.now();
    else if (Date.now() - unlockedSince > 3000 && webrtc.activeConnection() === active) {
      unlockedSince = 0; active.pendingDisplay = null;
      await webrtc.stop(active, 'Unlocked: reconnecting for the remote display');
    }
  } finally {unlockWatch = false;}
}, 1000).unref();

setInterval(() => {
  const active = wayvnc.activeConnection();
  if (active && wayvnc.expired(active)) wayvnc.stop(active, 'Input lease expired');
  // WebRTC sessions need no lease here: the sender expires them after 15 s without a heartbeat.
  for (const [id, s] of sessions) if (Date.now() - s.seen > sessionTtlMs) sessions.delete(id);
}, 1000).unref();

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, async () => {
    await stopController('Service stopping');
    await restoreSpeakers();
    server.close();
    process.exit(0);
  });
}

restoreStaleMute().catch(error => console.error(error.message));
// Screens a crashed remote-display session may have left off come back after the grace period, unless a new
// remote session (the usual reconnect after a service restart) takes the display over first.
webrtc.scheduleRestore();
server.listen(port, '127.0.0.1', () => console.log(`Omarchy Remote listening on http://127.0.0.1:${port}; tailnet host ${publicHost}`));
