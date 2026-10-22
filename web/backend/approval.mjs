// Approving sudo (and later polkit) with Face ID / Touch ID instead of typing the password. A PAM helper on the
// machine (scripts/omarchy-remote-sudo-approve, run by pam_exec before the password prompt) asks this service; a
// paired device that is connected shows the request and answers it after Face ID / Touch ID. An answer other than
// "approved" (denied, no device connected, timeout, busy) makes the helper fail, and sudo asks for the password as it
// always did. Nothing here can approve by itself: only a paired device's signed proof (server.mjs) calls decide().
import {randomBytes} from 'node:crypto';

export const TTL_MS = 20000;       // how long sudo waits for an answer
export const READY_MS = 8000;      // a device counts as connected this long after it last asked
export const MAX_PENDING = 4;
export const WINDOW_MS = 60000, LIMIT = 6;   // at most this many requests per window: a local process cannot flood the phone

const clean = (value, limit) => String(value ?? '').replace(/[\u0000-\u001f\u007f‎‏‪-‮⁦-⁩]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, limit);

export function createApprovals({now = Date.now, ttl = TTL_MS, ready = READY_MS, log = console} = {}) {
  const pending = new Map();   // id -> {id, service, user, command, expires, resolve, timer}
  let lastSeen = -Infinity;
  let times = [];

  const finish = (entry, decision) => {
    if (!pending.delete(entry.id)) return false;
    clearTimeout(entry.timer);
    entry.resolve(decision);
    return true;
  };

  return {
    // A device asked for the list (or the lock status): it is connected and can answer.
    seen() {lastSeen = now();},
    ready() {return now() - lastSeen < ready;},
    count() {return pending.size;},
    // What a device shows: no secrets, only what is needed to recognise the request.
    list() {
      return [...pending.values()].map(({id, service, user, command, expires}) => ({id, service, user, command, expires}));
    },
    // From the PAM helper (local). Resolves with approved | denied | timeout | busy | unavailable.
    request({service, user, command}) {
      if (!this.ready()) return Promise.resolve('unavailable');
      const t = now();
      times = times.filter(x => t - x < WINDOW_MS);
      if (times.length >= LIMIT || pending.size >= MAX_PENDING) return Promise.resolve('busy');
      times.push(t);
      return new Promise(resolve => {
        const entry = {id: randomBytes(8).toString('hex'), service: clean(service, 24) || 'sudo', user: clean(user, 32),
          command: clean(command, 300), expires: t + ttl, resolve, timer: null};
        entry.timer = setTimeout(() => finish(entry, 'timeout'), ttl);
        pending.set(entry.id, entry);
      });
    },
    // From a paired device (proof checked by the caller). False when the request is gone (answered, expired).
    decide(id, allow, device = '') {
      const entry = pending.get(String(id));
      if (!entry) return false;
      log.log?.(`Approval ${entry.service} ${allow ? 'approved' : 'denied'} by device ${device}`);
      return finish(entry, allow ? 'approved' : 'denied');
    },
  };
}
