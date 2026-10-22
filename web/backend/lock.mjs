// Screen lock of the machine: whether it is locked, and typing a password a paired device supplies into
// the lock screen. Omarchy's lock (Quickshell, Omarchy 4) and hyprlock (Omarchy 3) unlock only through PAM,
// so nothing here bypasses the password: the device's owner provides it (the iOS app keeps it in the
// Keychain behind Face ID) and the service types it only while the session is verifiably locked, so it can
// never land in an ordinary window. The password is passed on stdin, never on a command line, and never logged.
import {spawn, execFile} from 'node:child_process';
import path from 'node:path';

export const MAX_PASSWORD = 256;
const runText = (file, args, env) => new Promise(resolve => {
  execFile(file, args, {timeout: 3000, env: {...process.env, ...env}}, (error, stdout) => resolve(error ? null : String(stdout)));
});
const run = (file, args) => new Promise(resolve => {
  execFile(file, args, {timeout: 3000}, error => resolve(error ? (typeof error.code === 'number' ? error.code : -1) : 0));
});

// true / false, or null when it cannot be told. Omarchy 4: omarchy-hyprland-session-locked (0 locked,
// 1 unlocked, 2 undetermined). Otherwise a running hyprlock means locked.
export async function locked({exec = run, omarchyPath = process.env.OMARCHY_PATH || '/usr/share/omarchy'} = {}) {
  const state = await exec(path.join(omarchyPath, 'bin/omarchy-hyprland-session-locked'), []);
  if (state === 0) return true;
  if (state === 1) return false;
  const hyprlock = await exec('pgrep', ['-x', 'hyprlock']);
  if (hyprlock === 0) return true;
  if (hyprlock === 1 && state !== 2) return false;
  return null;
}

// Whether the lock screen is really up and takes a password: Omarchy 4's shell reports `secure` once the lock
// surfaces are on every screen (right after the lock starts it is locked but not yet showing a prompt, and a
// password typed then is lost). true / false, or null when the shell cannot tell (hyprlock, no shell).
export async function lockReady({exec = runText, omarchyPath = process.env.OMARCHY_PATH || '/usr/share/omarchy'} = {}) {
  const text = await exec(path.join(omarchyPath, 'bin/omarchy-shell'), ['lock', 'status'],
    {OMARCHY_PATH: omarchyPath, XDG_RUNTIME_DIR: process.env.XDG_RUNTIME_DIR || `/run/user/${process.getuid?.() ?? 1000}`});
  let status;
  try {status = JSON.parse(text);} catch {return null;}
  if (typeof status?.secure !== 'boolean') return null;
  return status.locked === true && status.secure === true && status.pending !== true;
}

// Omarchy's screensaver (omarchy-screensaver in a terminal of class org.omarchy.screensaver) ends on a typed
// character, which a touch device has no natural way to send. Ending it the way its own trap does: a signal to
// the script, whose handler restores the cursor and closes the window. The terminal's command line also ends in
// "omarchy-screensaver", so the pattern requires the script path.
export async function screensaver({exec = run} = {}) {
  return await exec('pgrep', ['-f', '[o]rg.omarchy.screensaver']) === 0;
}

export async function dismissScreensaver({exec = run} = {}) {
  if (!await screensaver({exec})) return false;
  await exec('pkill', ['-TERM', '-f', '/[o]marchy-screensaver$']);
  return true;
}

// A locked or idle machine may have its screens powered off (DPMS); a mirroring session then captures no
// frames (CAPTURE_START_TIMEOUT capture-no-buffers). Hyprland 0.56 (Lua) takes the table form
// hl.dsp.dpms({ action = "on" }): the plain string form hl.dsp.dpms("on") TOGGLES there (a screen that was already on went off,
// so every second connection to a locked machine captured nothing). Older versions: the plain dispatcher.
export async function wakeScreens({exec = run} = {}) {
  if (await exec('hyprctl', ['dispatch', 'hl.dsp.dpms({ action = "on" })']) === 0) return true;
  return await exec('hyprctl', ['dispatch', 'dpms', 'on']) === 0;
}

// A password as typed: printable text only (no newline or control characters), bounded.
export function validPassword(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= MAX_PASSWORD && !/[\u0000-\u001f\u007f]/.test(value);
}

export function typePassword(password, {spawnType = spawn} = {}) {
  return new Promise(resolve => {
    const child = spawnType('wtype', ['-', '-k', 'Return'], {stdio: ['pipe', 'ignore', 'ignore']});
    const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
    child.on('error', () => {clearTimeout(timer); resolve(false);});
    child.on('exit', code => {clearTimeout(timer); resolve(code === 0);});
    child.stdin.on('error', () => {});
    child.stdin.end(password);
  });
}

// At most `limit` attempts per window, so a paired device cannot guess passwords quickly.
export function attemptLimiter({limit = 5, windowMs = 60000, now = Date.now} = {}) {
  let times = [];
  return () => {
    const t = now();
    times = times.filter(x => t - x < windowMs);
    if (times.length >= limit) return false;
    times.push(t);
    return true;
  };
}

export async function unlock(password, {isLocked = locked, isReady = lockReady, type = typePassword, sleep = ms => new Promise(r => setTimeout(r, ms))} = {}) {
  if (await isLocked() !== true) return {status: 409, body: {error: 'The machine is not locked.'}};
  // Not before the prompt is there (up to 5 s): typed earlier, the password would be lost.
  for (let i = 0; i < 20 && await isReady() === false; i++) await sleep(250);
  if (!await type(password)) return {status: 503, body: {error: 'Could not type on the machine.'}};
  // Omarchy's lock takes a few seconds after the password (PAM, its unlock animation): wait up to 8 s.
  for (let i = 0; i < 32; i++) {
    await sleep(250);
    if (await isLocked() === false) return {status: 200, body: {unlocked: true}};
  }
  return {status: 200, body: {unlocked: false}};
}
