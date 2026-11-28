// Every network call of the UI lives here. Paths are relative (ingress adds a path prefix).
// A proxy in front of the app (HA ingress) can refuse request bodies of about 1 MB, so files go up in parts.
const PART = 512 * 1024;

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function call(method, path, body, raw) {
  if (path.startsWith('/')) throw new Error('absolute path not allowed: ' + path);
  const init = { method, headers: {} };
  if (body !== undefined) {
    if (raw) init.body = body;
    else { init.body = JSON.stringify(body); init.headers['Content-Type'] = 'application/json'; }
  }
  let r;
  try { r = await fetch(path, init); } catch (e) { throw restarting(new ApiError(0, 'Network error: ' + e.message)); }
  const data = await r.json().catch(() => null);
  if (r.ok && method !== 'GET') forget(path);
  if (!r.ok) {
    // Home Assistant answers 502 to 504 while it restarts: the hub marks its own answer retry, the gateway sends a page.
    const gateway = [502, 503, 504].includes(r.status) && (data === null || data.retry === true);
    const err = new ApiError(r.status, (data && data.error) || 'HTTP ' + r.status);
    throw gateway ? restarting(err) : err;
  }
  return data ?? {};
}

export const RESTARTING = 'Home Assistant is restarting. This page tries again in a few seconds.';
function restarting(err) {
  err.retry = true;
  err.message = RESTARTING;
  dispatchEvent(new CustomEvent('druckwerk:restarting'));
  return err;
}
// Stale-while-revalidate for the lists people open first. The first read of a list in a page load is answered from the
// cache (per HA user, localStorage) while the network answers behind it; when the answer differs the shell redraws the view
// ('druckwerk:fresh'). Every storage call may throw (private window, blocked data): then there is no cache and nothing changes.
const SWR = /^api\/(printers|spools|queue|history|load-requests|nozzle-requests)(\?.*)?$/;
const RELATED = { history: ['queue', 'history', 'printers'], queue: ['queue', 'history', 'printers'], jobs: ['queue', 'history'], spools: ['spools', 'load-requests'], 'load-requests': ['load-requests', 'spools'], 'nozzle-requests': ['nozzle-requests'],
  printers: ['printers', 'queue', 'nozzle-requests'], setup: ['printers'], grants: ['printers'] };
let user = '';
const seen = new Set(), pending = new Set(), served = new Set(), hot = new Map();  // network answered / fetching / read stale this route / fresh answer waiting
const key = (p) => `dw1:${user}:${p}`;
const peek = (k) => { try { const v = localStorage.getItem(k); return v == null ? undefined : JSON.parse(v); } catch { return undefined; } };
const keep = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* full or blocked: no cache */ } };
export const setUser = (id) => { user = String(id || ''); };
export const newRoute = () => served.clear();
// A change makes the cached lists of that resource wrong: drop them (the next read goes to the network).
function forget(path) {
  const bases = RELATED[path.split('/')[1]] || [];
  hot.clear();
  try {
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i);
      if (k && bases.some((b) => k.startsWith(key('api/' + b)))) localStorage.removeItem(k);
    }
  } catch { /* nothing cached */ }
}
async function get(p) {
  if (!SWR.test(p) || !user) return call('GET', p);
  const k = key(p), fresh = hot.get(p);
  if (fresh && Date.now() - fresh.at < 5000) { hot.delete(p); return fresh.v; }
  const old = peek(k);
  if (old !== undefined && !seen.has(p)) {
    served.add(p);
    if (!pending.has(p)) {
      pending.add(p);
      call('GET', p).then((v) => {
        seen.add(p); keep(k, v);
        hot.set(p, { v, at: Date.now() });  // the next read (a redraw caused by another list) takes it without a second request
        if (served.has(p) && JSON.stringify(v) !== JSON.stringify(old)) dispatchEvent(new CustomEvent('druckwerk:fresh'));
      }, () => { /* offline or failing: the cached list stays */ }).finally(() => pending.delete(p));
    }
    return old;
  }
  const v = await call('GET', p);
  seen.add(p); keep(k, v);
  return v;
}
const post = (p, b) => call('POST', p, b ?? {});
const put = (p, b) => call('PUT', p, b);
const del = (p) => call('DELETE', p);
const q = encodeURIComponent;

export const api = {
  me: () => get('api/me'),
  // setup
  setup: () => get('api/setup'),
  loginStart: (email, password) => post('api/setup/login/start', { email, password }),
  loginCode: (email, code) => post('api/setup/login/code', { email, code }),
  removeAccount: (id) => del('api/setup/accounts/' + q(id)),
  savePrinter: (serial, body) => put('api/setup/printers/' + q(serial), body),
  removePrinter: (serial) => del('api/setup/printers/' + q(serial)),
  integration: () => get('api/integration'),
  // printers
  printers: () => get('api/printers'),
  // The person confirmed in the UI: the plate is clear (prints) or the drying may start (drying jobs).
  startNext: (serial) => post(`api/printers/${q(serial)}/start-next`, { plate_clear: true, confirmed: true }),
  startJob: (serial, job_id) => post(`api/printers/${q(serial)}/start`, { job_id, plate_clear: true, confirmed: true }),
  setLight: (serial, on) => post(`api/printers/${q(serial)}/light`, { on }),
  dryNow: (serial, body) => post(`api/printers/${q(serial)}/drying`, { ...body, confirmed: true }),
  stopDrying: (serial, ams_id) => post(`api/printers/${q(serial)}/drying/stop`, { ams_id }),
  queueDry: (body) => post('api/queue', { ...body, kind: 'dry' }),
  history: (query) => get('api/history' + (query ? '?' + query : '')),
  historySettings: (id) => get(`api/history/${q(id)}/settings`),
  historyFileUrl: (id) => `api/history/${q(id)}/file`,
  historyGcode: async (id) => {
    const r = await fetch(`api/history/${q(id)}/gcode`);
    if (!r.ok) throw new ApiError(r.status, 'No G-code preview');
    return r.text();
  },
  historyQueue: (id) => post(`api/history/${q(id)}/queue`, {}),
  // queue
  queue: () => get('api/queue'),
  jobAction: (id, action, body) => post(`api/queue/${q(id)}/${action}`, body),
  // slicing happens in the browser: one uploaded job per slice, the G-code goes up after it
  upload: async (file) => {
    let job = await call('POST', `api/jobs?name=${q(file.name)}&total=${file.size}`, file.slice(0, PART), true);
    for (let at = PART; at < file.size; at += PART) job = await call('POST', `api/jobs/${q(job.id)}/model?offset=${at}&total=${file.size}`, file.slice(at, at + PART), true);
    return job;
  },
  job: (id) => get(`api/jobs/${q(id)}`),
  jobModel: async (id) => {
    const r = await fetch(`api/jobs/${q(id)}/model`);
    if (!r.ok) throw new ApiError(r.status, 'The model is no longer stored');
    return r.blob();
  },
  plateUrl: (id) => `api/jobs/${q(id)}/plate.png`,
  gcode: async (id) => {
    const r = await fetch(`api/jobs/${q(id)}/plate.gcode`);
    if (!r.ok) throw new ApiError(r.status, 'No G-code preview');
    return r.text();
  },
  // slicing in the browser: the filament preset for a tray, and the G-code the browser made (the app packs and stores it)
  // fil: {tray} of an AMS tray, or {type, brand, name, colour, spool} of a library spool
  engineFilament: (serial, fil) => get('api/engine/filament?' + new URLSearchParams({ serial, ...fil })),
  uploadGcode: async (id, params, gcode) => {
    const bytes = typeof gcode === 'string' ? new TextEncoder().encode(gcode) : new Uint8Array(gcode.buffer ?? gcode);
    let r;
    for (let at = 0; at < bytes.length; at += PART) r = await call('POST', `api/jobs/${q(id)}/gcode?${params}&offset=${at}&total=${bytes.length}`, bytes.slice(at, at + PART), true);
    return r;
  },
  queueJob: (id, body) => post(`api/jobs/${q(id)}/queue`, body),
  // filament
  spools: () => get('api/spools'),
  addSpool: (s) => post('api/spools', s),
  saveSpool: (id, s) => put('api/spools/' + q(id), s),
  spoolPrints: (id) => get(`api/spools/${q(id)}/prints`),
  deleteSpool: (id) => del('api/spools/' + q(id)),
  loadRequests: () => get('api/load-requests'),
  askLoad: (id, printer, note) => post(`api/spools/${q(id)}/load-request`, { printer, note }),
  cancelLoad: (id) => del('api/load-requests/' + q(id)),
  nozzleRequests: () => get('api/nozzle-requests'),
  askNozzle: (serial, body) => post(`api/printers/${q(serial)}/nozzle-request`, body),
  cancelNozzle: (id) => del('api/nozzle-requests/' + q(id)),
  // access
  grants: () => get('api/grants'),
  addGrant: (g) => post('api/grants', g),
  deleteGrant: (id) => del('api/grants/' + q(id)),
  createSwitch: (user_id, printer) => post('api/ha/switches', { user_id, printer }),
  haUsers: () => get('api/ha/users'),
  haEntities: (domains) => get('api/ha/entities?domains=' + q(domains.join(','))),
};
