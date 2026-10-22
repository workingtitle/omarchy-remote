// Read-only view of the active Omarchy theme: the colors of
// ~/.local/state/omarchy/current/theme/colors.toml and the current Omarchy
// font. The frontend maps them to CSS tokens so the remote UI always looks
// like the desktop. Nothing here writes or dispatches.
import path from 'node:path';
import os from 'node:os';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {readFile, stat} from 'node:fs/promises';

const exec = promisify(execFile);
const STATE = path.join(os.homedir(), '.local/state/omarchy/current');
const COLORS = path.join(STATE, 'theme/colors.toml');
const NAME = path.join(STATE, 'theme.name');
const FONT_ROOTS = ['/usr/share/fonts', path.join(os.homedir(), '.local/share/fonts')];
const FONT_TYPES = {'.ttf': 'font/ttf', '.otf': 'font/otf', '.woff2': 'font/woff2', '.woff': 'font/woff'};
const KEYS = [
  'accent', 'selection', 'muted', 'background', 'dark_background', 'darker_background', 'lighter_background',
  'foreground', 'dark_foreground', 'light_foreground', 'bright_foreground',
  'red', 'yellow', 'orange', 'green', 'cyan', 'blue', 'magenta',
];
const CACHE_MS = 5000;

// Omarchy's default theme, used when the state files are missing.
export const TOKYO_NIGHT = {
  accent: '#7aa2f7', selection: '#292e42', muted: '#414868', background: '#1a1b26', dark_background: '#13141c',
  darker_background: '#0e0e14', lighter_background: '#24283b', foreground: '#a9b1d6', dark_foreground: '#565f89',
  light_foreground: '#b4bee6', bright_foreground: '#c0caf5', red: '#f7768e', yellow: '#e0af68', orange: '#eb927b',
  green: '#9ece6a', cyan: '#449dab', blue: '#7aa2f7', magenta: '#ad8ee6',
};

// Only `key = "#rrggbb"` pairs of known keys and the mode survive; everything
// else in the file is ignored. Pure.
export function parseColors(text) {
  const colors = {};
  let mode = null;
  for (const line of text.split('\n')) {
    const match = /^\s*([a-z_]+)\s*=\s*"([^"]*)"\s*(?:#.*)?$/.exec(line);
    if (!match) continue;
    const [, key, value] = match;
    if (key === 'mode' && ['dark', 'light'].includes(value)) mode = value;
    else if (KEYS.includes(key) && /^#[0-9a-f]{6}$/i.test(value)) colors[key] = value.toLowerCase();
  }
  return {mode, colors};
}

async function fontFile(family, style) {
  const {stdout} = await exec('fc-match', ['-f', '%{file}', `${family}:style=${style}`], {timeout: 2000});
  const file = path.resolve(stdout.trim());
  if (!FONT_TYPES[path.extname(file).toLowerCase()]) return null;
  if (!FONT_ROOTS.some(root => file.startsWith(root + path.sep))) return null;
  return (await stat(file)).isFile() ? file : null;
}

let cached = null;
export async function theme() {
  if (cached && Date.now() - cached.at < CACHE_MS) return cached.value;
  const [text, name, changed] = await Promise.all([
    readFile(COLORS, 'utf8').catch(() => ''),
    readFile(NAME, 'utf8').then(s => s.trim()).catch(() => ''),
    stat(COLORS).then(s => s.mtimeMs).catch(() => 0),
  ]);
  const parsed = parseColors(text);
  const known = Object.keys(parsed.colors).length >= 6;
  const family = await exec('omarchy-font-current', [], {timeout: 2000}).then(r => r.stdout.trim()).catch(() => '');
  const fonts = {};
  if (family) {
    for (const [weight, style] of [['regular', 'Regular'], ['bold', 'Bold']]) {
      fonts[weight] = await fontFile(family, style).catch(() => null);
    }
  }
  const value = {
    name: known ? name || 'custom' : 'tokyo-night',
    mode: known ? parsed.mode || 'dark' : 'dark',
    colors: {...TOKYO_NIGHT, ...(known ? parsed.colors : {})},
    font: family,
    fonts,
    version: `${name}:${changed}:${family}`,
  };
  cached = {at: Date.now(), value};
  return value;
}

// Public shape for /api/theme: font paths stay on the server.
export async function publicTheme() {
  const t = await theme();
  return {name: t.name, mode: t.mode, colors: t.colors, font: t.font, fonts: {regular: !!t.fonts.regular, bold: !!t.fonts.bold}, version: t.version};
}

export async function themeFont(weight) {
  const file = (await theme()).fonts[weight];
  return file ? {file, type: FONT_TYPES[path.extname(file).toLowerCase()]} : null;
}

// Terminal colors of the active theme, as Omarchy writes them for its terminals: ghostty.conf (flat
// `key = #rrggbb` and `palette = N=#rrggbb`) or, failing that, alacritty.toml. Sent to every new terminal so
// the remote shell looks like the local one. Pure parsers.
const HEX = /^#?(?:0x)?([0-9a-f]{6})$/i;
const hex = value => {const m = HEX.exec(String(value || '').trim().replace(/^["']|["']$/g, '')); return m ? `#${m[1].toLowerCase()}` : null;};
const ANSI = ['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white'];

function complete(t) {
  if (!t.background || !t.foreground || t.palette.length !== 16 || t.palette.some(c => !c)) return null;
  return {background: t.background, foreground: t.foreground, cursor: t.cursor || t.foreground,
    selectionBackground: t.selectionBackground || null, selectionForeground: t.selectionForeground || null, palette: t.palette};
}

export function parseGhostty(text) {
  const t = {palette: Array(16).fill(null)};
  const keys = {background: 'background', foreground: 'foreground', 'cursor-color': 'cursor',
    'selection-background': 'selectionBackground', 'selection-foreground': 'selectionForeground'};
  for (const line of text.split('\n')) {
    const match = /^\s*([a-z-]+)\s*=\s*(.+?)\s*$/.exec(line);
    if (!match) continue;
    const [, key, value] = match;
    if (key === 'palette') {
      const entry = /^(\d{1,2})\s*=\s*(\S+)$/.exec(value);
      if (entry && Number(entry[1]) < 16) t.palette[Number(entry[1])] = hex(entry[2]);
    } else if (keys[key]) t[keys[key]] = hex(value);
  }
  return complete(t);
}

export function parseAlacritty(text) {
  const t = {palette: Array(16).fill(null)};
  let section = '';
  for (const line of text.split('\n')) {
    const header = /^\s*\[([a-z_.]+)\]\s*$/.exec(line);
    if (header) {section = header[1]; continue;}
    const match = /^\s*([a-z_]+)\s*=\s*(\S+)\s*(?:#.*)?$/.exec(line);
    if (!match) continue;
    const [, key, raw] = match, value = hex(raw);
    if (section === 'colors.primary' && (key === 'background' || key === 'foreground')) t[key] = value;
    else if (section === 'colors.cursor' && key === 'cursor') t.cursor = value;
    else if (section === 'colors.selection' && key === 'background') t.selectionBackground = value;
    else if (section === 'colors.selection' && key === 'text') t.selectionForeground = value;
    else if ((section === 'colors.normal' || section === 'colors.bright') && ANSI.includes(key)) {
      t.palette[ANSI.indexOf(key) + (section === 'colors.bright' ? 8 : 0)] = value;
    }
  }
  return complete(t);
}

export async function terminalTheme() {
  // Omarchy 4 keeps the current theme under ~/.local/state, Omarchy 3 under ~/.config.
  for (const dir of [path.join(STATE, 'theme'), path.join(os.homedir(), '.config/omarchy/current/theme')]) {
    const ghostty = await readFile(path.join(dir, 'ghostty.conf'), 'utf8').then(parseGhostty).catch(() => null);
    if (ghostty) return ghostty;
    const alacritty = await readFile(path.join(dir, 'alacritty.toml'), 'utf8').then(parseAlacritty).catch(() => null);
    if (alacritty) return alacritty;
  }
  return null;
}
