// Keeps an Omarchy 4 machine awake while a remote session runs. Its idle service (Quickshell IdleMonitor)
// does not count the virtual keyboard and pointer, so the screensaver started (hiding the real cursor) and the
// lock would follow while someone worked remotely. Omarchy's own "stay awake" switch
// (omarchy-shell idle disable/enable, the bar's indicator) is turned on for the session and back off after
// it, only if it was off before. A marker file makes a restarted service undo a hold it left behind.
// Machines without omarchy-shell (Omarchy 3) are left alone.
import {execFile} from 'node:child_process';
import {mkdir, readFile, rm, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const MARKER = path.join(os.homedir(), '.local/state/omarchy-remote/stay-awake-held');
const shell = () => path.join(process.env.OMARCHY_PATH || '/usr/share/omarchy', 'bin/omarchy-shell');
const run = (file, args) => new Promise(resolve => {
  execFile(file, args, {timeout: 3000, maxBuffer: 65536}, (error, stdout) => resolve(error ? null : String(stdout)));
});

export function createAwake({exec = run, marker = MARKER, command = shell} = {}) {
  let busy = Promise.resolve();
  const serial = task => (busy = busy.then(task, task));
  async function status() {
    try {return JSON.parse(await exec(command(), ['idle', 'status']) || 'null');} catch {return null;}
  }
  return {
    // Session start, and once a minute during a session: stay awake unless the user already chose to. The
    // switch is checked each time: something (seen after an unlock) turned it off under a held
    // marker, and the machine then locked itself mid-session.
    hold: () => serial(async () => {
      const state = await status();
      if (!state || state.stayAwake) return false;   // ours and still on, or the user's own choice
      if (await exec(command(), ['idle', 'disable']) === null) return false;
      await mkdir(path.dirname(marker), {recursive: true, mode: 0o700});
      await writeFile(marker, 'held\n', {mode: 0o600});
      return true;
    }),
    // After the session (and its grace period): back to normal idle, only if this service turned it on.
    release: () => serial(async () => {
      if (!await readFile(marker, 'utf8').then(() => true, () => false)) return false;
      if (await exec(command(), ['idle', 'enable']) === null) return false;
      await rm(marker, {force: true});
      return true;
    }),
  };
}

export const awake = createAwake();
