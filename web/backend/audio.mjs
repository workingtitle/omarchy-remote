// Host audio for the active connection: ffmpeg captures the default sink's
// PipeWire monitor, encodes Opus in Ogg, and this module splits the Ogg
// stream into Opus packets for the /audio WebSocket. No audio is logged or
// stored. Optionally the local speakers are muted while streaming: the
// monitor taps the sink before its mute, so the stream is unaffected
// (measured). Only a mute this module set is ever undone.
import path from 'node:path';
import {spawn, execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {readFile, writeFile, rm} from 'node:fs/promises';
import {WebSocket} from 'ws';
import {runtimeDir} from './config.mjs';

const exec = promisify(execFile);
const SAMPLE_RATE = 48000;
const FRAME_MS = 20;

// Incremental Ogg page parser yielding complete logical packets.
export class OggPackets {
  constructor() {this.buffer = Buffer.alloc(0); this.partial = [];}
  feed(data) {
    this.buffer = Buffer.concat([this.buffer, data]);
    const packets = [];
    for (;;) {
      const b = this.buffer;
      if (b.length < 27 || b.toString('latin1', 0, 4) !== 'OggS') {
        const next = b.indexOf('OggS', 1, 'latin1');
        if (b.length < 27 && next === -1) return packets;
        if (next === -1) {this.buffer = Buffer.alloc(0); return packets;}
        if (b.toString('latin1', 0, 4) !== 'OggS') {this.buffer = b.subarray(next); continue;}
        return packets;
      }
      const segments = b[26];
      const headerLength = 27 + segments;
      if (b.length < headerLength) return packets;
      let bodyLength = 0;
      for (let i = 0; i < segments; i++) bodyLength += b[27 + i];
      if (b.length < headerLength + bodyLength) return packets;
      let offset = headerLength;
      for (let i = 0; i < segments; i++) {
        const size = b[27 + i];
        this.partial.push(b.subarray(offset, offset + size));
        offset += size;
        if (size < 255) {packets.push(Buffer.concat(this.partial)); this.partial = [];}
      }
      this.buffer = b.subarray(offset);
    }
  }
}

async function defaultSink() {
  const sink = (await exec('pactl', ['get-default-sink'], {timeout: 2000})).stdout.trim();
  if (!sink) throw new Error('No default audio sink');
  return sink;
}

// Local speaker mute ------------------------------------------------------------
// The marker file names the sink this service muted, so a crash or restart can
// undo it on the next start (restoreStaleMute).
const MUTE_MARKER = path.join(runtimeDir, 'omarchy-remote-muted-sink');
const setMute = (sink, on) => exec('pactl', ['set-sink-mute', sink, on ? '1' : '0'], {timeout: 2000});
let unmute = null;

async function muteSpeakers(sink) {
  const {stdout} = await exec('pactl', ['get-sink-mute', sink], {timeout: 2000});
  if (/yes/i.test(stdout)) return; // already muted by the user: leave it alone
  await writeFile(MUTE_MARKER, sink, {mode: 0o600});
  await setMute(sink, true);
  let done = false;
  unmute = async () => {
    if (done) return;
    done = true;
    unmute = null;
    await setMute(sink, false).catch(error => console.error(`Could not unmute ${sink}: ${error.message}`));
    await rm(MUTE_MARKER, {force: true});
  };
  console.log(`Muted local speakers (${sink}) while streaming`);
}

// Undo this service's mute now (stream end, service stop).
export async function restoreSpeakers() {
  await unmute?.();
}

// On startup: a marker left by a crashed run means our mute is still active.
export async function restoreStaleMute() {
  const sink = (await readFile(MUTE_MARKER, 'utf8').catch(() => '')).trim();
  if (!sink) return;
  await setMute(sink, false).catch(() => {});
  await rm(MUTE_MARKER, {force: true});
  console.log(`Restored speakers (${sink}) muted by an earlier run`);
}

// Start capture for a WebSocket; resolves to a stop function.
export async function startAudio(ws, {bitrate = '96k', muteLocal = false} = {}) {
  const sink = await defaultSink();
  const source = `${sink}.monitor`;
  const args = [
    '-hide_banner', '-loglevel', 'error', '-nostdin',
    '-f', 'pulse', '-fragment_size', '960', '-i', source,
    '-ac', '2', '-ar', String(SAMPLE_RATE),
    '-c:a', 'libopus', '-b:a', bitrate, '-application', 'lowdelay', '-frame_duration', String(FRAME_MS),
    '-flush_packets', '1', '-f', 'ogg', '-page_duration', '20000', 'pipe:1',
  ];
  const child = spawn('ffmpeg', args, {stdio: ['ignore', 'pipe', 'pipe']});
  child.stderr.on('data', data => process.stderr.write(`ffmpeg: ${data}`));
  const parser = new OggPackets();
  let headers = 0;
  let timestamp = 0; // microseconds
  child.stdout.on('data', data => {
    for (const packet of parser.feed(data)) {
      if (headers < 2) {headers++; continue;} // OpusHead, OpusTags
      if (ws.readyState !== WebSocket.OPEN) continue;
      const frame = Buffer.alloc(8 + packet.length);
      frame.writeDoubleBE(timestamp, 0);
      packet.copy(frame, 8);
      ws.send(frame, {binary: true});
      timestamp += FRAME_MS * 1000;
    }
  });
  const stop = () => {
    if (child.exitCode === null) child.kill('SIGTERM');
    restoreSpeakers();
  };
  child.on('exit', () => {if (ws.readyState === WebSocket.OPEN) ws.close(1011, 'Audio capture ended');});
  ws.on('close', stop);
  ws.on('error', stop);
  child.on('exit', () => restoreSpeakers());
  if (muteLocal) await muteSpeakers(sink).catch(error => console.error(`Could not mute speakers: ${error.message}`));
  if (ws.readyState !== WebSocket.OPEN) stop(); // closed while muting
  console.log(`Audio started from ${source}`);
  return stop;
}
