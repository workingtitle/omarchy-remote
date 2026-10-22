// Static frontend files. Every load revalidates (ETag, 304) so an update is picked up at once while
// an unchanged ~400 KB bundle is not downloaded again on each reconnect; text files go out
// compressed (Brotli or gzip), compressed once per file version and kept in memory.
import {readFile, stat} from 'node:fs/promises';
import path from 'node:path';
import {promisify} from 'node:util';
import zlib from 'node:zlib';

export const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.webmanifest': 'application/manifest+json', '.svg': 'image/svg+xml', '.png': 'image/png',
};
const COMPRESSIBLE = new Set(['.html', '.js', '.mjs', '.css', '.json', '.webmanifest', '.svg']);
const MIN_COMPRESS_BYTES = 1024;
const MAX_CACHED_FILES = 64;   // the frontend has about 40 files
const brotli = promisify(zlib.brotliCompress);
const gzip = promisify(zlib.gzip);
const ENCODERS = {
  // Quality 9: ~0.2 s once for the largest bundle, within 10 % of quality 11 (which takes ~0.8 s).
  br: data => brotli(data, {params: {[zlib.constants.BROTLI_PARAM_QUALITY]: 9, [zlib.constants.BROTLI_PARAM_SIZE_HINT]: data.length}}),
  gzip: data => gzip(data, {level: 9}),
};
const NOT_FOUND = new Set(['ENOENT', 'ENOTDIR', 'ENAMETOOLONG', 'ERR_INVALID_ARG_VALUE', 'ERR_INVALID_ARG_TYPE']);

// The preferred encoding the client accepts (q=0 refuses one), or null for identity.
export function chooseEncoding(header = '') {
  const accepted = new Set();
  for (const part of String(header).split(',')) {
    const [name, ...params] = part.trim().toLowerCase().split(';');
    const q = params.map(p => /^\s*q=([\d.]+)\s*$/.exec(p)?.[1]).find(Boolean);
    if (name && (q === undefined || Number(q) > 0)) accepted.add(name);
  }
  return accepted.has('br') ? 'br' : accepted.has('gzip') ? 'gzip' : null;
}

// Weak: the same tag covers the identity, Brotli and gzip bodies of one file version.
export const entityTag = info => `W/"${info.size.toString(16)}-${Math.floor(info.mtimeMs).toString(16)}"`;

export function matches(header, tag) {
  if (!header) return false;
  const bare = tag.replace(/^W\//, '');
  return header.split(',').some(t => t.trim() === '*' || t.trim().replace(/^W\//, '') === bare);
}

export function createStaticFiles(root) {
  root = path.resolve(root);
  const cache = new Map();   // file -> {tag, data: Promise<Buffer>, encoded: {br?, gzip?}}

  function entry(file, tag) {
    let found = cache.get(file);
    if (found?.tag === tag) return found;
    cache.delete(file);
    if (cache.size >= MAX_CACHED_FILES) cache.delete(cache.keys().next().value);
    found = {tag, data: readFile(file), encoded: {}};
    found.data.catch(() => cache.delete(file));
    cache.set(file, found);
    return found;
  }

  function body(found, file, encoding) {
    if (!encoding) return found.data;
    found.encoded[encoding] ??= found.data.then(ENCODERS[encoding]);
    found.encoded[encoding].catch(() => cache.delete(file));
    return found.encoded[encoding];
  }

  // Serves `name` (a URL path, still percent-encoded) below root; answers 400/403/404 itself.
  return async function serve(req, res, name) {
    const fail = (code, error) => {
      res.writeHead(code, {'Content-Type': 'application/json'});
      res.end(JSON.stringify({error}));
    };
    let decoded;
    try {decoded = decodeURIComponent(name);} catch {return fail(400, 'Bad request');}
    const file = path.resolve(root, `.${decoded}`);
    if (!file.startsWith(root + path.sep)) return fail(403, 'Forbidden');
    let info;
    try {info = await stat(file);} catch (error) {
      if (NOT_FOUND.has(error.code)) return fail(404, 'Not found');
      throw error;
    }
    if (!info.isFile()) return fail(404, 'Not found');

    const extension = path.extname(file);
    const tag = entityTag(info);
    const compressible = COMPRESSIBLE.has(extension) && info.size >= MIN_COMPRESS_BYTES;
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('ETag', tag);
    if (compressible) res.setHeader('Vary', 'Accept-Encoding');
    if (matches(req.headers['if-none-match'], tag)) {
      res.writeHead(304);
      return res.end();
    }
    const encoding = compressible ? chooseEncoding(req.headers['accept-encoding']) : null;
    const data = await body(entry(file, tag), file, encoding);
    res.setHeader('Content-Type', MIME[extension] || 'application/octet-stream');
    if (encoding) res.setHeader('Content-Encoding', encoding);
    res.setHeader('Content-Length', data.length);
    res.end(req.method === 'HEAD' ? undefined : data);
  };
}
