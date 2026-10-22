// Remote terminal: one WebSocket per terminal, bridged to a login shell on a pseudo-terminal
// (pty-bridge.py). Client → service: binary frames are keyboard bytes, text frames are JSON
// {"type":"resize","cols":C,"rows":R}. Service → client: binary frames of terminal output, and first one text
// frame {"type":"theme",background,foreground,cursor,selectionBackground,selectionForeground,palette[16]} when
// the machine's theme colors are known, and {"type":"secret","on":bool} when a password prompt (input without
// echo, see pty-bridge.py) starts or ends, so a client can offer a stored password. A shell is full
// access to the account, so every terminal requires a paired device's signed proof (server.mjs), even when
// a test setup lifts it for /signal with PAIRING_REQUIRED=0. Content is never logged.
import {spawn, execFile} from 'node:child_process';
import {readFile, readdir, readlink, stat} from 'node:fs/promises';
import {promisify} from 'node:util';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

export const MAX_TERMINALS = 4;
export const MAX_INPUT_BYTES = 65536;
const MAX_BUFFERED = 1024 * 1024;   // pause the shell's output while the client is this far behind
const bridge = path.join(path.dirname(fileURLToPath(import.meta.url)), 'pty-bridge.py');
const open = new Set();
export const count = () => open.size;

export function frame(kind, payload) {
  const header = Buffer.alloc(5);
  header[0] = kind; header.writeUInt32BE(payload.length, 1);
  return Buffer.concat([header, payload]);
}
export function resizeFrame(cols, rows) {
  if (!Number.isInteger(cols) || !Number.isInteger(rows) || cols < 1 || rows < 1 || cols > 1000 || rows > 1000) return null;
  const payload = Buffer.alloc(4); payload.writeUInt16BE(cols, 0); payload.writeUInt16BE(rows, 2);
  return frame(1, payload);
}

// "Open the terminal where you are": when the focused window is a terminal, the newest shell (or other
// process) below it tells its working directory; otherwise the shell starts in the home directory.
// Pure part: pick the newest descendant of `root` from a process table [{pid, ppid, start}].
export function newestDescendant(root, processes) {
  const children = new Map();
  for (const p of processes) {if (!children.has(p.ppid)) children.set(p.ppid, []); children.get(p.ppid).push(p);}
  let current = root;
  for (let depth = 0; depth < 8; depth++) {
    const next = (children.get(current) || []).sort((a, b) => b.start - a.start)[0];
    if (!next) break;
    current = next.pid;
  }
  return current;
}

export async function focusedDirectory({hyprctl = args => promisify(execFile)('hyprctl', args, {timeout: 2000}), proc = '/proc'} = {}) {
  try {
    const window = JSON.parse((await hyprctl(['activewindow', '-j'])).stdout);
    const pid = Number(window?.pid);
    if (!Number.isInteger(pid) || pid <= 1) return null;
    const processes = [];
    for (const name of await readdir(proc)) {
      if (!/^\d+$/.test(name)) continue;
      try {
        // /proc/<pid>/stat: "pid (comm) state ppid ... starttime(22nd field)"; comm may contain spaces.
        const text = await readFile(`${proc}/${name}/stat`, 'utf8');
        const fields = text.slice(text.lastIndexOf(')') + 2).split(' ');
        processes.push({pid: Number(name), ppid: Number(fields[1]), start: Number(fields[19])});
      } catch {}
    }
    const directory = await readlink(`${proc}/${newestDescendant(pid, processes)}/cwd`);
    return (await stat(directory)).isDirectory() ? directory : null;
  } catch {return null;}
}

// A terminal can outlive its WebSocket when the client asks (`keep`, with its connection id): an app that goes to the background
// loses its network, and the shell, its work and its output are still there when it returns. The shell then runs on without a
// client for up to DETACH_MS, its output kept (the newest MAX_BACKLOG bytes), and the same paired device reattaches with the same id.
// Without `keep` (the Mac app, the browser) a closed WebSocket ends the shell as before. A device can end it at once with
// {"type":"close"}.
export const DETACH_MS = 30 * 60 * 1000;
const MAX_BACKLOG = 256 * 1024;
const resumable = new Map();   // id -> session, for sessions started with keep
// Every paired device may pick up a kept terminal (all of them have the account's full access anyway): it moves to the device that
// attaches, and a device that still holds it is told it was taken over.
export const canResume = id => !!id && resumable.has(id);
export const list = () => [...resumable.values()].filter(s => !s.closed)
  .map(s => ({id: s.id, attached: !!(s.ws && s.ws.readyState <= 1), age: Math.round((Date.now() - s.created) / 1000)}));
// At the limit a paired device's new terminal replaces its own oldest terminal that no client is attached to (an app that was
// closed or restarted leaves those behind for DETACH_MS); terminals with a client, and other devices', stay.
export function evictDetached(device) {
  for (const session of open) {
    if (!session.ws && !session.closed && session.device === device) {session.end('Replaced by a newer terminal'); return true;}
  }
  return false;
}

export function start(ws, {cols = 80, rows = 24, cwd = null, theme = null, id = null, device = null, keep = false, detachMs = DETACH_MS, spawnBridge = spawn, log = console} = {}) {
  if (keep && id && resumable.has(id)) {
    const existing = resumable.get(id);
    const takeover = existing.device !== device;
    existing.device = device;
    existing.attach(ws, {cols, rows, theme, resumed: true, takeover});
    log.log('Terminal resumed');
    return existing;
  }
  const child = spawnBridge('python3', [bridge, String(cols), String(rows), ...(cwd ? [cwd] : [])], {stdio: ['pipe', 'pipe', 'ignore', 'pipe']});
  const session = {child, ws: null, id, device, backlog: [], backlogBytes: 0, timer: null, secret: false, closed: false, created: Date.now()};
  open.add(session);
  if (keep && id) resumable.set(id, session);
  const live = () => session.ws && session.ws.readyState === 1;
  const end = reason => {
    if (session.closed) return; session.closed = true; open.delete(session);
    if (resumable.get(id) === session) resumable.delete(id);
    clearTimeout(session.timer);
    try {child.stdin.end();} catch {}
    setTimeout(() => {if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');}, 3000).unref();
    if (session.ws && session.ws.readyState <= 1) session.ws.close(1000, reason);
    log.log(`Terminal closed: ${reason}`);
  };
  session.end = end;
  const detach = () => {
    session.ws = null;
    session.timer = setTimeout(() => end('Detach expired'), detachMs); session.timer.unref?.();
    log.log('Terminal detached');
  };
  child.stdout.on('data', chunk => {
    if (live()) {
      session.ws.send(chunk, {binary: true});
      if ((session.ws.bufferedAmount || 0) > MAX_BUFFERED) {child.stdout.pause(); setTimeout(function resume() {
        if (session.closed) return;
        if (live() && (session.ws.bufferedAmount || 0) > MAX_BUFFERED / 4) return setTimeout(resume, 50);
        child.stdout.resume();
      }, 50);}
    } else if (keep && !session.closed) {
      session.backlog.push(Buffer.from(chunk)); session.backlogBytes += chunk.length;
      while (session.backlogBytes > MAX_BACKLOG && session.backlog.length > 1) session.backlogBytes -= session.backlog.shift().length;
    }
  });
  let control = '';
  child.stdio?.[3]?.on('data', chunk => {
    control = (control + chunk.toString('latin1')).slice(-64);
    let newline;
    while ((newline = control.indexOf('\n')) >= 0) {
      const line = control.slice(0, newline); control = control.slice(newline + 1);
      if (line === 'S1' || line === 'S0') {
        session.secret = line === 'S1';
        if (live()) session.ws.send(JSON.stringify({type: 'secret', on: session.secret}));
      }
    }
  });
  child.on('exit', () => end('Shell exited'));
  child.on('error', () => end('Terminal unavailable'));
  session.attach = (socket, {cols: c, rows: r, theme: t, resumed = false, takeover = false} = {}) => {
    if (session.closed) {socket.close(1000, 'Shell exited'); return;}
    clearTimeout(session.timer); session.timer = null;
    if (session.ws && session.ws !== socket && session.ws.readyState <= 1) {const old = session.ws; session.ws = null; old.close(1000, takeover ? 'Taken over by another device' : 'Resumed elsewhere');}
    session.ws = socket;
    if (t && socket.readyState === 1) socket.send(JSON.stringify({type: 'theme', ...t}));
    if (keep && socket.readyState === 1) socket.send(JSON.stringify({type: 'attached', resumed}));
    if (resumed) {
      const resize = resizeFrame(c, r); if (resize) child.stdin.write(resize);
      for (const part of session.backlog.splice(0)) socket.send(part, {binary: true});
      session.backlogBytes = 0;
      if (session.secret) socket.send(JSON.stringify({type: 'secret', on: true}));
    }
    socket.on('message', (data, binary) => {
      if (session.closed || session.ws !== socket) return;
      if (binary) {
        if (data.length > MAX_INPUT_BYTES) return end('Input too large');
        child.stdin.write(frame(0, data));
        return;
      }
      let message;
      try {message = JSON.parse(data.toString('utf8'));} catch {return end('Invalid message');}
      if (message?.type === 'close') return end('Closed by the device');
      const resize = message?.type === 'resize' ? resizeFrame(message.cols, message.rows) : null;
      if (!resize) return end('Invalid message');
      child.stdin.write(resize);
    });
    const gone = reason => {if (session.ws !== socket || session.closed) return; if (keep && id) detach(); else end(reason);};
    socket.on('close', () => gone('Transport closed'));
    socket.on('error', () => gone('Transport error'));
  };
  session.attach(ws, {cols, rows, theme});
  log.log('Terminal opened');
  return session;
}
