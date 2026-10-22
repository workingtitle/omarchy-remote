import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {readFile} from 'node:fs/promises';
const exec=promisify(execFile),cache=new Map();
export async function textKeysyms(layout){
  if(cache.has(layout))return cache.get(layout);
  const symbols=new Map([...((await readFile('/usr/include/xkbcommon/xkbcommon-keysyms.h','utf8')).matchAll(/^#define XKB_KEY_(\w+)\s+(0x[\da-f]+)/gmi))].map(m=>[m[1],Number(m[2])]));
  const {stdout}=await exec('xkbcli',['compile-keymap','--layout',layout],{timeout:3000,maxBuffer:2**20});
  const found=new Set(),level3=new Set();
  for(const block of stdout.matchAll(/key\s+<[^>]+>\s*\{([\s\S]*?)\};/g)){
    for(const name of block[1].match(/\w+/g)||[])if(symbols.has(name))found.add(symbols.get(name));
    // Conventional alphabetic four-level keys: explicitly select level three
    // to avoid WayVNC choosing duplicate dedicated keys such as KEY_EURO.
    const levels=/\[\s*([a-z]),\s*([A-Z]),\s*(\w+)/.exec(block[1]);
    if(levels&&levels[1].toUpperCase()===levels[2]&&symbols.has(levels[3]))level3.add(symbols.get(levels[3]));
  }
  const result={symbols:[...found],level3:[...level3]};cache.set(layout,result);return result;
}
