// The screen-cast portal's backend (xdg-desktop-portal-hyprland) leaks a shared-memory buffer per capture session and in the end
// spins at 100% CPU, after which no session can start (seen with 180 leaked descriptors and 150 CPU minutes). While no
// session is running the service looks at it now and then and restarts the two portal services when it is leaking or spinning.
// The sender covers the other case, a portal that went silent in the middle of a start (native/tools/media-probe/portal_recovery.py).
import {execFile} from 'node:child_process';
import {readdir, readFile} from 'node:fs/promises';
import {promisify} from 'node:util';

const run = promisify(execFile);
export const BACKEND = 'xdg-desktop-portal-hyprland';
export const SERVICES = [BACKEND, 'xdg-desktop-portal'];
export const FD_LIMIT = 80;        // a fresh backend holds about 22; the leak is one or two per session
export const CPU_LIMIT = 60;       // percent of one core over the sample window
export const SAMPLE_MS = 3000;
export const CHECK_MS = 5 * 60 * 1000;
const TICKS = 100;                 // USER_HZ on Linux

// Why the backend should be restarted, or null.
export function verdict({fds, cpu}) {
  if (cpu > CPU_LIMIT) return `spinning at ${Math.round(cpu)}% CPU`;
  if (fds > FD_LIMIT) return `${fds} open descriptors`;
  return null;
}

// utime + stime (clock ticks) from the contents of /proc/<pid>/stat; the command name may contain spaces and parentheses.
export function cpuTicks(stat) {
  const fields = stat.slice(stat.lastIndexOf(')') + 2).split(' ');   // starts at field 3 (state)
  return Number(fields[11]) + Number(fields[12]);                       // fields 14 and 15
}

const defaults = {
  pid: async () => {
    const {stdout} = await run('systemctl', ['--user', 'show', '-p', 'MainPID', '--value', BACKEND], {timeout: 5000});
    return Number(stdout.trim()) || 0;
  },
  fds: async pid => (await readdir(`/proc/${pid}/fd`)).length,
  stat: async pid => readFile(`/proc/${pid}/stat`, 'utf8'),
  sleep: ms => new Promise(resolve => setTimeout(resolve, ms)),
  restart: () => run('systemctl', ['--user', 'restart', ...SERVICES], {timeout: 20000}),
};

// One look at the backend. Returns {restarted, reason} (reason null: all well, or nothing to look at).
export async function check({isBusy, log = console, io = {}} = {}) {
  const {pid, fds, stat, sleep, restart} = {...defaults, ...io};
  try {
    if (isBusy()) return {restarted: false, reason: null};
    const id = await pid();
    if (!id) return {restarted: false, reason: null};
    const before = cpuTicks(await stat(id));
    await sleep(SAMPLE_MS);
    const cpu = (cpuTicks(await stat(id)) - before) / TICKS / (SAMPLE_MS / 1000) * 100;
    const reason = verdict({fds: await fds(id), cpu});
    if (!reason) return {restarted: false, reason: null};
    // A session may have started during the sample: it must not lose its capture.
    if (isBusy()) return {restarted: false, reason};
    log.log(`PORTAL_HEALTH restarting the portal services: ${reason}`);
    await restart();
    return {restarted: true, reason};
  } catch (error) {
    if (error.code === 'ENOENT') return {restarted: false, reason: null};   // no systemd or no such process: nothing to look at
    log.log(`PORTAL_HEALTH check failed: ${String(error.message || error).slice(0, 120)}`);
    return {restarted: false, reason: null};
  }
}

// Looks soon after the service starts, then every few minutes; only while no session runs.
export function watch({isBusy, log = console, every = CHECK_MS, io = {}} = {}) {
  const look = () => check({isBusy, log, io});
  const first = setTimeout(look, 30000); first.unref?.();
  const timer = setInterval(look, every); timer.unref?.();
  return () => {clearTimeout(first); clearInterval(timer);};
}
