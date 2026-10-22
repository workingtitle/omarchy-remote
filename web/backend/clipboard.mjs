// Narrow text-clipboard bridge around the installed wl-copy/wl-paste.
// This is not a command API: only clipboard text ever reaches these tools.
import {spawn, execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {maxClipboardBytes, maxClipboardImageBytes} from './config.mjs';

const exec = promisify(execFile);
const TIMEOUT_MS = 3000;

export async function readRequestText(req) {
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > maxClipboardBytes) throw new Error('Clipboard exceeds 1 MiB');
    chunks.push(chunk);
  }
  const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (typeof body.text !== 'string' || body.text.includes('\0')) throw new Error('Invalid clipboard text');
  return body.text;
}

export function copyText(text) {
  return new Promise((resolve, reject) => {
    const child = spawn('wl-copy', ['--type', 'text/plain;charset=utf-8'], {stdio: ['pipe', 'ignore', 'ignore']});
    const timer = setTimeout(() => {child.kill(); reject(new Error('Clipboard timeout'));}, TIMEOUT_MS);
    child.once('error', error => {clearTimeout(timer); reject(error);});
    child.once('exit', code => {clearTimeout(timer); code === 0 ? resolve() : reject(new Error('Clipboard unavailable'));});
    child.stdin.on('error', () => {});
    child.stdin.end(text);
  });
}

export async function pasteText() {
  const result = await exec('wl-paste', ['--no-newline', '--type', 'text'], {timeout: TIMEOUT_MS, maxBuffer: maxClipboardBytes});
  return result.stdout;
}

// A PNG on the machine's clipboard (programs in a terminal read it from there, e.g. a coding assistant on Ctrl+V). The data
// has to be a PNG (signature checked) of a bounded size; nothing else ever reaches wl-copy as an image.
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
export function decodePng(base64) {
  if (typeof base64 !== 'string' || base64.length > Math.ceil(maxClipboardImageBytes * 4 / 3) + 8 || !/^[A-Za-z0-9+/]+={0,2}$/.test(base64)) throw new Error('Invalid image');
  const data = Buffer.from(base64, 'base64');
  if (data.length < 24 || data.length > maxClipboardImageBytes || !data.subarray(0, 8).equals(PNG)) throw new Error('Invalid image');
  return data;
}

export function copyImage(data) {
  return new Promise((resolve, reject) => {
    const child = spawn('wl-copy', ['--type', 'image/png'], {stdio: ['pipe', 'ignore', 'ignore']});
    const timer = setTimeout(() => {child.kill(); reject(new Error('Clipboard timeout'));}, TIMEOUT_MS * 2);
    child.once('error', error => {clearTimeout(timer); reject(error);});
    child.once('exit', code => {clearTimeout(timer); code === 0 ? resolve() : reject(new Error('Clipboard unavailable'));});
    child.stdin.on('error', () => {});
    child.stdin.end(data);
  });
}
