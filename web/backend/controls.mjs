// Shortcuts menu of the native apps: the machine's open windows (list, focus, close), its installed apps
// (list, launch) and a command line to run. Only for a paired device's proof (server.mjs /api/device/*);
// the same device can already open a terminal, so a command adds no new power. Nothing here logs window
// titles or commands.
import {execFile, spawn} from 'node:child_process';
import {promisify} from 'node:util';
import {readdir, readFile, stat} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const exec = promisify(execFile);
const hyprctl = args => exec('hyprctl', args, {timeout: 3000, maxBuffer: 2 ** 20});

export const ADDRESS = /^0x[0-9a-f]{1,16}$/;
export const APP_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;
export const MAX_COMMAND = 500;

const clean = (value, limit) => String(value ?? '').replace(/[\u0000-\u001f\u007f‪-‮⁦-⁩]/g, ' ').trim().slice(0, limit);

// Windows -------------------------------------------------------------------------
export function windowList(clients, active) {
  const focused = typeof active?.address === 'string' ? active.address : null;
  return (Array.isArray(clients) ? clients : [])
    // Windows in a scratchpad that is not shown count as hidden for Hyprland but belong in the list.
    .filter(c => c && c.mapped !== false && (c.hidden !== true || String(c.workspace?.name ?? '').startsWith('special:')) && ADDRESS.test(String(c.address)))
    .map(c => ({
      address: c.address,
      app: clean(c.class || c.initialClass, 80),
      title: clean(c.title, 120),
      workspace: Number.isInteger(c.workspace?.id) ? c.workspace.id : null,
      scratchpad: String(c.workspace?.name ?? '').startsWith('special:'),
      focused: c.address === focused,
    }))
    .slice(0, 100);
}

export async function listWindows() {
  const [clients, active] = await Promise.all([
    hyprctl(['clients', '-j']).then(r => JSON.parse(r.stdout)),
    hyprctl(['activewindow', '-j']).then(r => JSON.parse(r.stdout)).catch(() => ({})),
  ]);
  return windowList(clients, active);
}

async function dispatch(lua) {
  const {stdout, stderr} = await hyprctl(['dispatch', lua]);
  const text = `${stdout}${stderr}`;
  if (/not found|error/i.test(text)) throw new Error('Window not found');
}

export async function focusWindow(address) {
  if (!ADDRESS.test(String(address))) throw new Error('Invalid window');
  await dispatch(`hl.dsp.focus({ window = "address:${address}" })`);
}

export async function closeWindow(address) {
  if (!ADDRESS.test(String(address))) throw new Error('Invalid window');
  await dispatch(`hl.dsp.window.close({ window = "address:${address}" })`);
}

// Where a window goes: workspace 1 to 10 (the view follows it, like Omarchy's Super+Shift+number) or the
// scratchpad (it vanishes there, like Super+Alt+S; Super+S shows it).
// `follow` false (a window dragged onto another workspace in the app) leaves the view where it is.
export function moveTarget(workspace, follow = true) {
  if (workspace === 'scratchpad') return {workspace: 'special:scratchpad', follow: false};
  const number = Number(workspace);
  if (Number.isInteger(number) && number >= 1 && number <= 10) return {workspace: String(number), follow: follow !== false};
  throw new Error('Invalid workspace');
}

// `address` 'active' moves the focused window (a dock shortcut in the app, which does not know it).
export async function moveWindow(address, workspace, follow = true) {
  const target = moveTarget(workspace, follow);
  if (address === 'active') address = await hyprctl(['activewindow', '-j']).then(r => JSON.parse(r.stdout)?.address).catch(() => null);
  if (!ADDRESS.test(String(address))) throw new Error(address === null || address === undefined ? 'No focused window' : 'Invalid window');
  await dispatch(`hl.dsp.window.move({ workspace = "${target.workspace}", follow = ${target.follow}, window = "address:${address}" })`);
}

// Go to a workspace 1 to 10, or show / hide the scratchpad. Through the compositor, not a key chord: the key
// chord Super+S did nothing for the scratchpad (the same dispatcher run by hyprctl works).
export function showTarget(workspace) {
  if (workspace === 'scratchpad') return 'hl.dsp.workspace.toggle_special("scratchpad")';
  const number = Number(workspace);
  if (Number.isInteger(number) && number >= 1 && number <= 10) return `hl.dsp.focus({ workspace = "${number}" })`;
  throw new Error('Invalid workspace');
}

export async function showWorkspace(workspace) {
  await dispatch(showTarget(workspace));
}

// Clipboard shortcuts of the focused window, as Omarchy's own bindings send them (default/hypr/bindings/clipboard.lua):
// terminals (tag "terminal") take Shift+Insert to paste and Ctrl+Insert to copy, everything else Ctrl+V / Ctrl+C (and
// Ctrl+X to cut; a terminal cannot cut, so it copies). Sent
// by the compositor with explicit modifiers, so it also works when the key chord Super+V from a virtual keyboard does not.
export function clipboardShortcut(tags, kind) {
  const terminal = (Array.isArray(tags) ? tags : []).some(tag => String(tag).replace(/\*$/, '') === 'terminal');
  if (kind === 'paste') return terminal ? {mods: 'SHIFT', key: 'Insert'} : {mods: 'CTRL', key: 'V'};
  if (kind === 'copy') return terminal ? {mods: 'CTRL', key: 'Insert'} : {mods: 'CTRL', key: 'C'};
  if (kind === 'cut') return terminal ? {mods: 'CTRL', key: 'Insert'} : {mods: 'CTRL', key: 'X'};
  throw new Error('Invalid clipboard action');
}

export async function clipboardKey(kind) {
  const active = await hyprctl(['activewindow', '-j']).then(r => JSON.parse(r.stdout)).catch(() => ({}));
  const {mods, key} = clipboardShortcut(active?.tags, kind);
  await dispatch(`hl.dsp.send_key_state({ mods = "${mods}", key = "${key}", state = "down" })`);
  await new Promise(resolve => setTimeout(resolve, 60));
  await dispatch(`hl.dsp.send_key_state({ mods = "${mods}", key = "${key}", state = "up" })`);
}

// Apps ----------------------------------------------------------------------------
// The visible launchers of the machine (desktop entries): id (file name without .desktop) and name.
export function parseDesktopEntry(text) {
  const fields = {};
  let inside = false;
  for (const line of String(text).split('\n')) {
    if (line.startsWith('[')) {inside = line.trim() === '[Desktop Entry]'; continue;}
    if (!inside) continue;
    const match = /^([A-Za-z0-9-]+)=(.*)$/.exec(line);
    if (match && !(match[1] in fields)) fields[match[1]] = match[2].trim();
  }
  if (fields.Type !== 'Application' || fields.NoDisplay === 'true' || fields.Hidden === 'true' || fields.Terminal === 'true') return null;
  const name = clean(fields.Name, 80);
  return name ? {name, icon: clean(fields.Icon, 200), wmClass: clean(fields.StartupWMClass, 80)} : null;
}

export async function listApps(directories = [path.join(os.homedir(), '.local/share/applications'), '/usr/share/applications']) {
  const apps = new Map();
  for (const directory of directories) {
    const files = await readdir(directory).catch(() => []);
    for (const file of files) {
      if (!file.endsWith('.desktop')) continue;
      const id = file.slice(0, -'.desktop'.length);
      if (!APP_ID.test(id) || apps.has(id)) continue;
      const entry = parseDesktopEntry(await readFile(path.join(directory, file), 'utf8').catch(() => ''));
      if (entry) apps.set(id, {id, ...entry});
    }
  }
  return [...apps.values()].sort((a, b) => a.name.localeCompare(b.name)).slice(0, 400);
}

// Icons ---------------------------------------------------------------------------
// App icons as small PNGs (the icon theme Omarchy uses first, then hicolor and the pixmaps): for an app id (a
// desktop entry), a window class (StartupWMClass or the class itself as an icon name) or an icon name.
const ICON_NAME = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,119}$/;
const ICON_SIZES = ['128x128', '96x96', '64x64', '256x256', '48x48', '512x512', '192x192', '32x32'];
const MAX_ICON_BYTES = 65536;
let themeName = null;

async function currentTheme() {
  if (themeName) return themeName;
  themeName = await exec('gsettings', ['get', 'org.gnome.desktop.interface', 'icon-theme'], {timeout: 2000})
    .then(r => r.stdout.trim().replace(/^'|'$/g, '')).catch(() => '');
  if (!/^[A-Za-z0-9._-]{1,60}$/.test(themeName)) themeName = 'hicolor';
  return themeName;
}

export function iconCandidates(name, theme, home = os.homedir()) {
  const roots = [path.join(home, '.local/share/icons'), '/usr/share/icons'];
  const themes = [...new Set([theme, 'hicolor', 'Adwaita'])];
  const out = [];
  if (path.isAbsolute(name)) return [name];
  for (const root of roots) for (const t of themes) {
    for (const size of ICON_SIZES) for (const kind of ['apps', 'applications']) out.push(`${root}/${t}/${size}/${kind}/${name}.png`);
    out.push(`${root}/${t}/scalable/apps/${name}.svg`);
  }
  out.push(`/usr/share/pixmaps/${name}.png`, `/usr/share/pixmaps/${name}.svg`);
  return out;
}

async function firstExisting(candidates) {
  for (const file of candidates) {
    if (await stat(file).then(info => info.isFile(), () => false)) return file;
  }
  return null;
}

async function toPng(file) {
  if (file.endsWith('.svg')) return (await execBuffer('rsvg-convert', ['-w', '96', '-h', '96', file])).stdout;
  const info = await readFile(file);
  if (info.length <= MAX_ICON_BYTES) return info;
  return (await execBuffer('magick', [file, '-resize', '96x96', 'png:-'])).stdout;
}
const execBuffer = (command, args) => exec(command, args, {timeout: 5000, maxBuffer: 2 ** 20, encoding: 'buffer'});

/** Icon name for an app id or window class (via the desktop entries), else the name itself. */
export function iconNameFor(name, apps) {
  const lower = String(name).toLowerCase();
  const entry = apps.find(app => app.id === name) || apps.find(app => app.wmClass && app.wmClass.toLowerCase() === lower)
    || apps.find(app => app.id.toLowerCase() === lower);
  return entry?.icon || name;
}

/** {name: base64 PNG} for the names that resolve; at most 24 per call. */
export async function iconPngs(names, apps) {
  const result = {};
  const theme = await currentTheme();
  for (const name of (Array.isArray(names) ? names : []).slice(0, 24)) {
    if (typeof name !== 'string' || !ICON_NAME.test(name)) continue;
    try {
      const icon = iconNameFor(name, apps);
      const file = await firstExisting(iconCandidates(icon, theme));
      if (!file) continue;
      const png = await toPng(file);
      if (png.length > 0 && png.length <= MAX_ICON_BYTES * 2) result[name] = png.toString('base64');
    } catch {/* no icon for this one */}
  }
  return result;
}

function detached(command, args) {
  const child = spawn(command, args, {detached: true, stdio: 'ignore', env: process.env});
  child.on('error', () => {});
  child.unref();
}

export async function launchApp(id, apps = listApps) {
  if (!APP_ID.test(String(id))) throw new Error('Invalid app');
  if (!(await apps()).some(app => app.id === id)) throw new Error('Unknown app');
  detached('gtk-launch', [id]);
}

// Commands ------------------------------------------------------------------------
export function validCommand(text) {
  return typeof text === 'string' && text.trim().length > 0 && text.length <= MAX_COMMAND && !/[\u0000]/.test(text);
}

export function runCommand(text) {
  if (!validCommand(text)) throw new Error('Invalid command');
  detached('/bin/sh', ['-c', text]);
}
