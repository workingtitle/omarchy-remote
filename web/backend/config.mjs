// Runtime configuration from the environment.
import {existsSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const frontendDir = path.join(root, 'frontend');
// The browser client is part of the development checkout only; the testers' checkout (public/export.sh) has the
// pairing page alone. Without it the start page points to the apps and pairing offers no browser.
export const browserClient = existsSync(path.join(frontendDir, 'index.html'));
export const port = Number(process.env.PORT || 8099);
// The machine's tailnet HTTPS name (Tailscale Serve). REMOTE_HOST overrides; otherwise ask Tailscale.
function tailnetName() {
  try {
    const status = JSON.parse(execFileSync('tailscale', ['status', '--json'], {timeout: 3000, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore']}));
    return String(status?.Self?.DNSName || '').replace(/\.$/, '');
  } catch {return '';}
}
export const publicHost = process.env.REMOTE_HOST || tailnetName() || 'localhost';
export const localHosts = new Set([`localhost:${port}`, `127.0.0.1:${port}`]);
export const allowedHosts = new Set([...localHosts, publicHost]);
export const allowedLogin = process.env.ALLOWED_LOGIN;
export const runtimeDir = process.env.XDG_RUNTIME_DIR || `/run/user/${process.getuid()}`;

const localWayvnc = path.join(root, '.runtime/root/usr/bin/wayvnc');
export const wayvncBin = process.env.WAYVNC_BIN || (existsSync(localWayvnc) ? localWayvnc : '/usr/bin/wayvnc');
export const wayvncEnv = {...process.env, LD_LIBRARY_PATH: path.join(root, '.runtime/root/usr/lib')};
export const wayvncMaxFps = String(Number(process.env.WAYVNC_FPS) || 60);
export const wayvncExtraArgs = (process.env.WAYVNC_ARGS || '').split(' ').filter(Boolean);

export const leaseMs = 10000;
export const sessionTtlMs = 86400000;
export const maxSessions = 100;
export const maxInputBytes = 4 * 1024 * 1024;
export const maxClipboardBytes = 1024 * 1024;
// A picture for the machine's clipboard (the terminal's "paste image"): the apps scale larger ones down.
export const maxClipboardImageBytes = 8 * 1024 * 1024;
// /signal, /vnc and /api/takeover accept only devices approved on the machine (signed challenge). Always, unless PAIRING_REQUIRED=0 (tests,
// throwaway machines).
export const pairingRequired = process.env.PAIRING_REQUIRED !== '0';
