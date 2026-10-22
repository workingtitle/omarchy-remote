// WebRTC signaling bridge: one WebSocket per session carries exactly the line-delimited JSON the
// native sender speaks on stdio (offer, answer, ice, heartbeat, stop, error). The service spawns
// one sender per connection, as sshd does for the Mac client, and never parses SDP. Media flows
// peer to peer. One controlling client at a time, shared with the WayVNC path (see server.mjs).
import {spawn} from 'node:child_process';
import {awake} from './awake.mjs';
import os from 'node:os';
import path from 'node:path';

export const MAX_MESSAGE_BYTES = 131072;   // same bound as the sender and the Mac client
const CLIENT_TYPES = new Set(['answer', 'ice', 'heartbeat', 'stop']);
const MAX_PENDING_BYTES = 1024 * 1024;     // sender stdout backlog while the socket is slow
// Only fixed-format markers reach the journal; SDP, text and clipboard never do.
const LOGGABLE = /^(INPUT_LEASE|LOCAL_RECLAIM|BAR_NUDGE|ENCODER|QUALITY|BITRATE|STAY_AWAKE|AUDIO_SOURCE|VIRTUAL_DISPLAY|POINTER_REGION|SENDER_ERROR|VIDEO_STALE_FRAME|RATE_LIMIT|READY|CAPTURE_START_TIMEOUT|PORTAL_STUCK|PORTAL_HEALTH|SESSION_END|VIDEO_PAUSE|VIDEO_STATS|DISPLAY_MODE|VIDEO_RESIZE|AUDIO_RATE|Portal [A-Za-z]+ response)\b/;

export const senderCommand = () => process.env.WEBRTC_SENDER
  ? process.env.WEBRTC_SENDER.split(' ').filter(Boolean)
  : ['python3', path.join(os.homedir(), '.local/bin/webrtc_sender.py'), '--desktop'];

// Safety net for the remote display mode (native/tools/media-probe/virtual_display.py): however the sender
// ended, even killed, bring the machine's own screens back. A no-op when no state was saved.
// A weak connection (cellular, relayed, or one that just dropped): the client asks for a lighter stream with bw=1..3, and
// the sender is started with a bitrate and a frame rate that fit (the client also asks for fewer pixels). Anything else
// is no limit.
const BANDWIDTH = {1: {kbps: 5000, fps: 30}, 2: {kbps: 2500, fps: 24}, 3: {kbps: 1200, fps: 15}};
export function bandwidthArgs(level) {
  const limit = BANDWIDTH[Number(level)];
  return limit ? ['--bitrate-kbps', String(limit.kbps), '--max-fps', String(limit.fps)] : [];
}

export const restoreCommand = () => ['python3', path.join(os.homedir(), '.local/bin/virtual_display.py'), '--restore'];
export function restoreDisplays({command = restoreCommand(), spawnRestore = spawn, log = console} = {}) {
  return new Promise(resolve => {
    let child;
    try {child = spawnRestore(command[0], command.slice(1), {stdio: ['ignore', 'ignore', 'pipe']});} catch {return resolve(false);}
    let errors = '';
    child.stderr?.on('data', chunk => {errors = (errors + chunk.toString('utf8')).slice(-400);});
    const timer = setTimeout(() => child.kill('SIGKILL'), 10000);
    child.on('error', () => {clearTimeout(timer); resolve(false);});
    child.on('exit', code => {
      clearTimeout(timer);
      const line = errors.split('\n').find(l => l.startsWith('VIRTUAL_DISPLAY'));
      if (line) log.log(line.slice(0, 200));
      resolve(code === 0);
    });
  });
}

// The screens come back a few seconds after a remote-display session ends, so a reconnect or a hand-over
// to another device reuses the headless output instead of removing and re-adding outputs.
export const RESTORE_GRACE_MS = 60000;   // a phone that comes back after switching apps (20-40 s) finds the display up: no outputs removed and created each time (Quickshell crashed at the 20 s edge)
let pendingRestore = null;
export function scheduleRestore({ms = RESTORE_GRACE_MS, restore = restoreDisplays, log = console} = {}) {
  clearTimeout(pendingRestore);
  pendingRestore = setTimeout(async () => {
    pendingRestore = null;
    // A session that took over (it started before this one's sender exited) now owns its own display.
    if (active?.display) return;
    await restore({log});
    if (!active && await awake.release()) log.log('STAY_AWAKE released');
  }, ms);
  pendingRestore.unref?.();
}
// Before a new session: keep the display for a remote one, restore now for a mirroring one.
export async function settleDisplays(remote, {restore = restoreDisplays, log = console} = {}) {
  if (!pendingRestore) return;
  clearTimeout(pendingRestore); pendingRestore = null;
  if (!remote) await restore({log});
}

export function holdAwake(log = console) {
  awake.hold().then(held => {if (held) log.log('STAY_AWAKE held');}).catch(() => {});
}

let active = null;
export const activeConnection = () => active;

export function owns(connection, session, id) {
  return !!connection && connection.session === session && connection.id === id;
}

export function reserve(session, id, device = null) {
  if (active) return null;
  active = {session, id, device};
  return active;
}

export function unreserve(connection) {
  if (active === connection) active = null;
}

// Validate one client message; returns the JSON line for the sender or null.
export function clientLine(data) {
  const text = Buffer.isBuffer(data) ? data.toString('utf8') : String(data);
  if (Buffer.byteLength(text) > MAX_MESSAGE_BYTES) return null;
  let message;
  try {message = JSON.parse(text);} catch {return null;}
  if (!message || typeof message !== 'object' || Array.isArray(message) || !CLIENT_TYPES.has(message.type)) return null;
  return JSON.stringify(message) + '\n';
}

// Bridge an accepted WebSocket to a new sender process. `spawnSender` is injectable for tests.
export const AWAKE_CHECK_MS = 60000;
export const TAKEN_BACK = 'Taken back at the machine';   // close reason; the clients do not reconnect after it
export function start(connection, ws, {command = senderCommand(), spawnSender = spawn, restore = scheduleRestore, log = console} = {}) {
  // Keep the machine awake for the whole own-display session (holdAwake re-checks Omarchy's switch).
  if (connection.display === 'remote') {
    connection.awakeTimer = setInterval(() => holdAwake(log), AWAKE_CHECK_MS);
    connection.awakeTimer.unref?.();
  }
  connection.ws = ws;
  const child = connection.child = spawnSender(command[0], command.slice(1), {stdio: ['pipe', 'pipe', 'pipe']});
  let buffer = '', errors = '';
  let reclaimed = false;
  const finish = reason => stop(connection, reason, log);

  child.stdout.on('data', chunk => {
    buffer += chunk.toString('utf8');
    let end;
    while ((end = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      if (!line) continue;
      if (Buffer.byteLength(line) > MAX_MESSAGE_BYTES) return finish('Oversized sender message');
      if (ws.readyState !== 1) continue;
      if ((ws.bufferedAmount || 0) > MAX_PENDING_BYTES) return finish('Client too slow');
      ws.send(line);
      // The machine's own keyboard or trackpad was used while its screens were off (sender: local_reclaim.py): the session ends.
      if (line.length < 100 && line.includes('local-reclaim')) {
        let message = null; try {message = JSON.parse(line);} catch {}
        if (message?.type === 'local-reclaim') {reclaimed = true; return finish(TAKEN_BACK);}
      }
    }
    if (Buffer.byteLength(buffer) > MAX_MESSAGE_BYTES) finish('Oversized sender message');
  });
  child.stderr.on('data', chunk => {
    errors = (errors + chunk.toString('utf8')).slice(-65536);
    let end;
    while ((end = errors.indexOf('\n')) >= 0) {
      const line = errors.slice(0, end); errors = errors.slice(end + 1);
      if (LOGGABLE.test(line)) log.log(line.slice(0, 200));
    }
  });
  child.on('error', () => finish('Sender failed to start'));
  // Taken back at the machine: its screens come back at once (otherwise a minute later, when a phone may still return).
  child.on('exit', () => {restore(reclaimed ? {log, ms: 0} : {log}); finish('Sender exited');});

  ws.on('message', (data, binary) => {
    if (connection.stopping) return;
    const line = binary ? null : clientLine(data);
    if (!line) return finish('Invalid signaling message');
    if (child.stdin.writableLength > MAX_PENDING_BYTES) return finish('Sender not reading');
    child.stdin.write(line);
  });
  ws.on('close', () => finish('Transport closed'));
  ws.on('error', () => finish('Transport error'));
  log.log('WebRTC session started');
}

// Ordered teardown: ask the sender to stop (it releases held input and restores the display
// mode), then close stdin, then signal. Every wait is bounded.
// A second call while the teardown runs waits for it (the same promise): a client that restarts (another display mode) while its old
// session is still ending must not get 409 "in use" because the reservation is not released yet.
export function stop(connection, reason, log = console) {
  if (!connection) return Promise.resolve();
  if (connection.stopPromise) return connection.stopPromise;
  connection.stopping = true;
  connection.stopPromise = teardown(connection, reason, log);
  return connection.stopPromise;
}
async function teardown(connection, reason, log) {
  clearInterval(connection.awakeTimer);
  const child = connection.child;
  if (child && child.exitCode === null && child.signalCode === null) {
    try {child.stdin.write('{"type":"stop"}\n'); child.stdin.end();} catch {}
    const exited = new Promise(resolve => child.once('exit', resolve));
    const wait = ms => Promise.race([exited, new Promise(resolve => setTimeout(resolve, ms))]);
    await wait(3000);
    if (child.exitCode === null && child.signalCode === null) {child.kill('SIGTERM'); await wait(2000);}
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
  }
  if (connection.ws && connection.ws.readyState <= 1) connection.ws.close(1000, reason.slice(0, 120));
  unreserve(connection);
  log.log(`WebRTC session ended: ${reason}`);
}
