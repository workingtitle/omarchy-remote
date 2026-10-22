// The user's own shortcuts, kept on the machine so every paired device shows the same ones: their shortcuts,
// favourites, the order and hidden items of the built-in lists and the Spaces dock. One JSON document with a
// revision; a save names the revision it was based on, and a stale one gets the current document back instead of
// overwriting it. Everything is bounded; commands in it are never logged.
import {readFile, writeFile, rename, mkdir} from 'node:fs/promises';
import path from 'node:path';

export const MAX_BYTES = 64 * 1024;
export const MAX_SHORTCUTS = 100;
export const MAX_IDS = 300;
const MAX_STEPS = 8;
const MODIFIERS = new Set(['Super', 'Ctrl', 'Alt', 'Shift']);
const KINDS = new Set(['keys', 'command', 'app', 'move']);
const UUID = /^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$/;
const SYMBOL = /^[a-z0-9.]{1,48}$/;
const LISTS = ['favorites', 'hidden', 'sections', 'hiddenSections', 'dock'];
const MAX_OVERRIDES = 32;
const OVERRIDE = /^key:[A-Za-z ]{1,24}$/;

const text = (value, max) => typeof value === 'string' && value.length <= max && !value.includes('\0');
const fail = () => {throw new Error('Invalid shortcuts');};
function ids(value) {
  if (!Array.isArray(value) || value.length > MAX_IDS || !value.every(id => text(id, 240))) fail();
  return [...value];
}

function shortcut(item) {
  if (!item || typeof item !== 'object' || !UUID.test(item.id) || !text(item.title, 80) || !SYMBOL.test(item.symbol) || !KINDS.has(item.kind)) fail();
  if (!Array.isArray(item.steps) || item.steps.length < 1 || item.steps.length > MAX_STEPS) fail();
  const steps = item.steps.map(step => {
    if (!step || !Array.isArray(step.modifiers) || step.modifiers.length > 4 || !step.modifiers.every(m => MODIFIERS.has(m)) || !text(step.key, 16)) fail();
    return {modifiers: [...new Set(step.modifiers)], key: step.key};
  });
  if (!text(item.command, 2000) || !text(item.appId, 240) || !text(item.appName, 120)) fail();
  // A window move: workspace 1–10 or null for the scratchpad, and whether the view follows.
  const workspace = item.workspace ?? null;
  if (workspace !== null && !(Number.isInteger(workspace) && workspace >= 1 && workspace <= 10)) fail();
  if (item.follow !== undefined && typeof item.follow !== 'boolean') fail();
  return {id: item.id, title: item.title, symbol: item.symbol, kind: item.kind, steps, command: item.command, appId: item.appId, appName: item.appName,
    workspace, follow: item.follow === true};
}

// A clean copy of a document, or an error. Unknown fields are dropped.
export function validate(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) fail();
  if (Buffer.byteLength(JSON.stringify(data)) > MAX_BYTES) fail();
  const shortcuts = data.shortcuts ?? [];
  if (!Array.isArray(shortcuts) || shortcuts.length > MAX_SHORTCUTS) fail();
  const clean = {shortcuts: shortcuts.map(shortcut)};
  if (new Set(clean.shortcuts.map(s => s.id.toLowerCase())).size !== clean.shortcuts.length) fail();
  for (const list of LISTS) clean[list] = ids(data[list] ?? []);
  const order = data.order ?? {};
  if (!order || typeof order !== 'object' || Array.isArray(order) || Object.keys(order).length > 16) fail();
  clean.order = {};
  for (const [section, list] of Object.entries(order)) {
    if (!/^[a-z]{1,24}$/.test(section)) fail();
    clean.order[section] = ids(list);
  }
  // The user's own version of a built-in key ("key:Copy" → a shortcut).
  const overrides = data.overrides ?? {};
  if (!overrides || typeof overrides !== 'object' || Array.isArray(overrides) || Object.keys(overrides).length > MAX_OVERRIDES) fail();
  clean.overrides = {};
  for (const [id, item] of Object.entries(overrides)) {
    if (!OVERRIDE.test(id)) fail();
    clean.overrides[id] = shortcut(item);
  }
  return clean;
}

export function createShortcuts({file}) {
  let current = null;            // {revision, data}; data is null until a device saved once
  let queue = Promise.resolve();

  async function load() {
    if (current) return current;
    try {
      const saved = JSON.parse(await readFile(file, 'utf8'));
      current = {revision: Number.isSafeInteger(saved.revision) && saved.revision > 0 ? saved.revision : 0, data: validate(saved.data)};
    } catch {current = {revision: 0, data: null};}
    return current;
  }
  async function write(next) {
    await mkdir(path.dirname(file), {recursive: true, mode: 0o700});
    const temporary = `${file}.tmp`;
    await writeFile(temporary, JSON.stringify(next, null, 1), {mode: 0o600});
    await rename(temporary, file);
  }

  return {
    read: () => load().then(({revision, data}) => ({revision, data})),
    // Saves when `base` is the current revision; otherwise answers the current document (a conflict).
    save(base, data) {
      const run = queue.then(async () => {
        const clean = validate(data);
        const state = await load();
        if (base !== state.revision) return {conflict: true, revision: state.revision, data: state.data};
        const next = {revision: state.revision + 1, data: clean};
        await write(next);
        current = next;
        return {revision: next.revision};
      });
      queue = run.catch(() => {});
      return run;
    },
  };
}
