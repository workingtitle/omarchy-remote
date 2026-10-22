// Read-only desktop discovery through hyprctl. Nothing here dispatches.
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {readFile} from 'node:fs/promises';
import {textKeysyms} from './text-keysyms.mjs';

const exec = promisify(execFile);
const hyprctl = args => exec('hyprctl', args, {timeout: 3000, maxBuffer: 2 ** 20});
const OMARCHY_TILING = '/usr/share/omarchy/default/hypr/bindings/tiling.lua';

// Hyprland 0.56 reports empty keys for Lua code bindings. Only enrich the
// numeric workspace entries after verifying the installed default source
// contains the exact loop that produces them.
async function enrichWorkspaceBindings(bindings) {
  const tiling = await readFile(OMARCHY_TILING, 'utf8').catch(() => '');
  const verified = tiling.includes('local key = "code:" .. tostring(workspace + 9)')
    && tiling.includes('o.bind("SUPER + " .. key, "Switch to workspace " .. workspace');
  if (!verified) return;
  for (const binding of bindings) {
    const n = /^Switch to workspace ([1-9])$/.exec(binding.description || '')?.[1];
    if (!binding.key && binding.modmask === 64 && n) {
      binding.key = n;
      binding.source = 'Verified installed Omarchy numeric workspace loop';
    }
  }
}

export async function desktop() {
  const [monitors, binds, layout] = await Promise.all([
    hyprctl(['monitors', '-j']),
    hyprctl(['binds', '-j']),
    hyprctl(['getoption', 'input:kb_layout', '-j']),
  ]);
  const bindings = JSON.parse(binds.stdout)
    .filter(b => !b.submap && !b.mouse && !b.release && !b.catch_all)
    .map(({key, modmask, description, keycode}) => ({key, modmask, description, keycode}));
  await enrichWorkspaceBindings(bindings);
  const keyboardLayout = JSON.parse(layout.stdout).str;
  const text = await textKeysyms(keyboardLayout).catch(() => ({symbols: [], level3: []}));
  return {
    monitors: JSON.parse(monitors.stdout),
    bindings,
    keyboardLayout,
    textKeysyms: text.symbols,
    textLevel3: text.level3,
  };
}

// Workspaces for the clients' status strip (iOS, beside the notch): numbered workspaces with their window
// counts and the active one. Names and window titles are not sent.
export function workspaceState(list, active) {
  const workspaces = (Array.isArray(list) ? list : [])
    .filter(w => Number.isInteger(w?.id) && w.id >= 1 && w.id <= 10)
    .map(w => ({id: w.id, windows: Math.min(99, Math.max(0, Number(w.windows) | 0))}))
    .sort((a, b) => a.id - b.id);
  return {workspaces, active: Number.isInteger(active?.id) ? active.id : null};
}

export async function workspaces() {
  const [list, active] = await Promise.all([hyprctl(['workspaces', '-j']), hyprctl(['activeworkspace', '-j'])]);
  return workspaceState(JSON.parse(list.stdout), JSON.parse(active.stdout));
}
