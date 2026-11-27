// Every network call of the UI lives here. Paths are relative (ingress adds a path prefix).
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
const get = (p) => call('GET', p);
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
  dryNow: (serial, body) => post(`api/printers/${q(serial)}/drying`, { ...body, confirmed: true }),
  stopDrying: (serial, ams_id) => post(`api/printers/${q(serial)}/drying/stop`, { ams_id }),
  queueDry: (body) => post('api/queue', { ...body, kind: 'dry' }),
  history: (query) => get('api/history' + (query ? '?' + query : '')),
  // queue
  queue: () => get('api/queue'),
  jobAction: (id, action, body) => post(`api/queue/${q(id)}/${action}`, body),
  // slicing: one uploaded job per slice (the slicer keeps one result per job)
  upload: (file) => call('POST', 'api/jobs?name=' + q(file.name), file, true),
  slice: (id, body) => post(`api/jobs/${q(id)}/slice`, body),
  job: (id) => get(`api/jobs/${q(id)}`),
  plateUrl: (id) => `api/jobs/${q(id)}/plate.png`,
  gcode: async (id) => {
    const r = await fetch(`api/jobs/${q(id)}/plate.gcode`);
    if (!r.ok) throw new ApiError(r.status, 'No G-code preview');
    return r.text();
  },
  // slicing in the browser: the filament preset for a tray, and the G-code the browser made (the app packs and stores it)
  engineFilament: (serial, tray) => get(`api/engine/filament?serial=${q(serial)}&tray=${q(tray)}`),
  uploadGcode: (id, params, gcode) => call('POST', `api/jobs/${q(id)}/gcode?` + params, gcode, true),
  queueJob: (id, body) => post(`api/jobs/${q(id)}/queue`, body),
  // filament
  spools: () => get('api/spools'),
  addSpool: (s) => post('api/spools', s),
  saveSpool: (id, s) => put('api/spools/' + q(id), s),
  deleteSpool: (id) => del('api/spools/' + q(id)),
  loadRequests: () => get('api/load-requests'),
  askLoad: (id, printer, note) => post(`api/spools/${q(id)}/load-request`, { printer, note }),
  cancelLoad: (id) => del('api/load-requests/' + q(id)),
  // access
  grants: () => get('api/grants'),
  addGrant: (g) => post('api/grants', g),
  deleteGrant: (id) => del('api/grants/' + q(id)),
  createSwitch: (user_id, printer) => post('api/ha/switches', { user_id, printer }),
  haUsers: () => get('api/ha/users'),
  haEntities: (domains) => get('api/ha/entities?domains=' + q(domains.join(','))),
};
