// One controlling client at a time. Each connection owns a WayVNC child on a
// private Unix socket inside a mode-0700 runtime directory, plus a bounded
// observer of the client stream that can synthesize missing releases.
import net from 'node:net';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {existsSync} from 'node:fs';
import {WebSocket} from 'ws';
import {RfbInputState} from './rfb-input.mjs';
import {runtimeDir, wayvncBin, wayvncEnv, wayvncMaxFps, wayvncExtraArgs, leaseMs, maxInputBytes} from './config.mjs';

let active = null;

export const activeConnection = () => active;

export function owns(connection, session, connectionId) {
  return !!connection && connection.session === session && connection.id === connectionId;
}

// Reserve the desktop before asynchronous display discovery so a second
// upgrade cannot race in. Returns null when another client holds it.
export function reserve(session, id, upgradeSocket) {
  if (active) return null;
  active = {session, id, input: new RfbInputState(), upgradeSocket, deadline: Date.now() + leaseMs};
  return active;
}

export function unreserve(connection) {
  if (active === connection) active = null;
}

export function renew(connection) {
  connection.deadline = Date.now() + leaseMs;
}

export function expired(connection) {
  return Date.now() > connection.deadline;
}

async function withTimeout(promise, ms) {
  await Promise.race([promise, new Promise(resolve => setTimeout(resolve, ms))]);
}

// Ordered teardown: drain client key-ups, send synthetic releases while RFB is
// still alive, close the local socket, then stop the child. Every wait is bounded.
export async function stop(connection, reason) {
  if (!connection || connection.stopping) return;
  connection.stopping = true;
  clearInterval(connection.retry);
  if (!connection.ws) connection.upgradeSocket?.destroy();

  const tcp = connection.tcp;
  if (tcp && !tcp.destroyed) {
    const release = connection.input?.release();
    if (release?.length) {
      await withTimeout(new Promise(resolve => tcp.write(release, resolve)), 250);
      // Keep RFB alive while WayVNC dispatches the synthetic releases. Sending
      // FIN in the same read cycle can discard input in the backend's teardown.
      await new Promise(resolve => setTimeout(resolve, 150));
    }
    await withTimeout(new Promise(resolve => {tcp.once('close', resolve); tcp.end(); tcp.resume();}), 250);
    tcp.destroy();
    // Allow WayVNC's event loop to flush the Wayland releases before SIGTERM.
    await new Promise(resolve => setTimeout(resolve, 100));
  }

  connection.audio?.close(1000, reason);
  connection.ws?.close(1000, reason);
  const child = connection.child;
  if (child && child.exitCode === null) {
    child.kill('SIGTERM');
    await withTimeout(new Promise(resolve => child.once('exit', resolve)), 800);
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
  }
  if (connection.dir) await rm(connection.dir, {recursive: true, force: true});
  unreserve(connection);
  console.log(`Session ended: ${reason}`);
}

// Start WayVNC for an accepted WebSocket and bridge it to the private socket.
export async function start(connection, ws, {output, layout}) {
  connection.ws = ws;
  ws.on('error', () => stop(connection, 'Transport error'));
  ws.on('close', () => stop(connection, 'Transport closed'));
  try {
    connection.dir = await mkdtemp(path.join(runtimeDir, 'omarchy-remote-'));
    if (connection.stopping) {
      await rm(connection.dir, {recursive: true, force: true});
      return;
    }
    const sock = path.join(connection.dir, 'vnc.sock');
    const args = [
      '-C', '/dev/null',
      '-g',
      '-S', path.join(connection.dir, 'ctl.sock'),
      '-k', layout,
      '-R',
      '-f', wayvncMaxFps,
      '-L', 'info',
      ...wayvncExtraArgs,
      '-n', 'Omarchy Remote',
      '-o', output,
      `unix:${sock}`,
    ];
    const child = connection.child = spawn(wayvncBin, args, {env: wayvncEnv, stdio: ['ignore', 'ignore', 'pipe']});
    child.stderr.on('data', data => process.stderr.write(data));
    child.on('error', () => stop(connection, 'WayVNC failed'));
    child.on('exit', () => stop(connection, 'WayVNC exited'));

    connection.retry = setInterval(() => {
      if (!existsSync(sock)) return;
      clearInterval(connection.retry);
      bridge(connection, ws, sock);
    }, 30);
    console.log(`Session started on ${output}`);
  } catch (error) {
    console.error(error.message);
    await stop(connection, 'Startup failed');
  }
}

function bridge(connection, ws, sock) {
  const tcp = connection.tcp = net.createConnection(sock);
  tcp.on('error', () => stop(connection, 'VNC socket failed'));
  tcp.on('close', () => stop(connection, 'VNC socket closed'));
  // Server → client with backpressure: pause the socket until the frame is flushed.
  tcp.on('data', data => {
    if (ws.readyState !== WebSocket.OPEN) return;
    tcp.pause();
    ws.send(data, {binary: true}, () => {if (!connection.stopping) tcp.resume();});
  });
  // Client → server: observe framing first so unknown messages fail closed.
  ws.on('message', (data, binary) => {
    if (connection.stopping) return;
    if (!binary || tcp.writableLength > maxInputBytes) return stop(connection, 'Invalid or excessive input');
    try {
      connection.input.feed(data);
    } catch {
      return stop(connection, 'Invalid input framing');
    }
    tcp.write(data);
  });
}
