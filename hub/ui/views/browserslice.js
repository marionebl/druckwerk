// Slicing in the browser: OrcaSlicer compiled to WebAssembly (Muon3D Slicer Engine) in a Web Worker, speaking the
// engine's protocol v2. The server hands out the runtime at engine/ and the flattened presets; see hub/slicer/engine.
import { api } from '../api.js';
import { toObjectPaint } from './paintcodes.js';

const cache = { presets: {} };

function connect(worker) {
  let next = 0; const pending = new Map();
  worker.onmessage = ({ data: m }) => {
    const p = pending.get(m.id);
    if (!p) return;
    if (m.kind === 'progress') p.progress?.(m.progress);
    else if (m.kind === 'result') { pending.delete(m.id); p.resolve(m.result); }
    else if (m.kind === 'error') { pending.delete(m.id); p.reject(new Error(m.error.message + (m.error.detail ? ` (${m.error.detail})` : ''))); }
  };
  worker.onerror = (e) => { for (const p of pending.values()) p.reject(new Error('The engine worker failed: ' + (e.message || 'could not start'))); pending.clear(); };
  return (op, params, progress, transfer = []) => new Promise((resolve, reject) => {
    const id = ++next; pending.set(id, { resolve, reject, progress });
    worker.postMessage({ v: 2, id, op, params }, transfer);
  });
}

// Starts the engine (download, compile, threads) so that it is ready before Slice is pressed. The multi-thread variant
// needs a cross-origin isolated page; ?engine=st forces the single-thread one for comparing the two.
export function warm() {
  const t0 = performance.now();
  return (async () => {
    const tag = (await (await fetch('engine/version.json')).json()).tag;
    const base = new URL(`engine/${tag}/`, location.href);
    const manifest = await (await fetch(new URL('manifest.json', base))).json();
    const worker = new Worker(new URL(manifest.host.file, base), { type: 'module', name: 'slicer-engine' });
    try {
      const request = connect(worker);
      const hello = await request('hello', { protocol: { major: 2 }, client: { name: 'druckwerk', version: '1' } });
      const forceSt = new URLSearchParams(location.search).get('engine') === 'st';
      const variant = crossOriginIsolated && !forceSt && hello.variants.includes('mt') ? 'mt' : 'st';
      await request('load', { variant });
      return { worker, request, variant, loadMs: performance.now() - t0, licence: new URL('LICENSE', base).href };
    } catch (e) { worker.terminate(); throw e; }
  })();
}

export function parseStl(buf) {
  const dv = new DataView(buf);
  if (buf.byteLength >= 84 && buf.byteLength === 84 + dv.getUint32(80, true) * 50) {  // binary: 84 bytes + 50 per triangle
    const n = dv.getUint32(80, true), out = new Float32Array(n * 9);
    for (let t = 0; t < n; t++) for (let i = 0; i < 9; i++) out[t * 9 + i] = dv.getFloat32(84 + t * 50 + 12 + i * 4, true);
    return out;
  }
  const v = [...new TextDecoder().decode(buf).matchAll(/^\s*vertex\s+(\S+)\s+(\S+)\s+(\S+)/gm)].flatMap((m) => [+m[1], +m[2], +m[3]]);
  if (!v.length || v.length % 9) throw new Error('Not an STL file: no triangles found.');
  return new Float32Array(v);
}

// The engine wants bed coordinates: centred on the printable area, resting on z = 0.
export function placeOnBed(pos, machine) {
  const area = [machine.printable_area].flat().join(',').split(',').map((p) => p.split('x').map(Number));
  const mid = (a) => (Math.min(...area.map((p) => p[a])) + Math.max(...area.map((p) => p[a]))) / 2;
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < pos.length; i += 3) for (let a = 0; a < 3; a++) { lo[a] = Math.min(lo[a], pos[i + a]); hi[a] = Math.max(hi[a], pos[i + a]); }
  const d = [mid(0) - (lo[0] + hi[0]) / 2, mid(1) - (lo[1] + hi[1]) / 2, -lo[2]];
  for (let i = 0; i < pos.length; i += 3) for (let a = 0; a < 3; a++) pos[i + a] += d[a];
  return pos;
}

// The presets of a printer model and nozzle (0.4 mm is the model's own file), loaded once. Per request they are cut to what the engine needs.
export async function loadPresets(model, nozzle = '0.4') {
  const file = nozzle === '0.4' ? model : `${model}-${nozzle}`;
  return (cache.presets[file] ||= await (await fetch(`engine/presets/${file}.json`)).json());
}

// The flattened configs of a variant. Orca refuses PETG on its default Cool Plate, so the plate must be set.
export async function variantConfigs(model, quality, bed, filamentName, nozzle = '0.4') {
  const presets = await loadPresets(model, nozzle);
  const filament = presets.filaments.find((f) => f.name === filamentName);
  if (!filament) throw new Error('No filament preset ' + filamentName);
  return { machine: { ...presets.machine, curr_bed_type: bed }, process: { ...presets.process[quality], curr_bed_type: bed }, filament: filament.config };
}

// Settings service of the engine (settings.view, settings.edit). Configs travel once per engine and then by hash.
export async function settingsCall(eng, ctx, op, params) {
  const { model, quality, bed, filament, nozzle = '0.4' } = ctx;
  const c = await variantConfigs(model, quality, bed, filament, nozzle);
  eng.sent ||= new Set();
  const ref = (kind, hash, config) => (eng.sent.has(hash) ? { hash } : { hash, config });
  const refs = { machine: ref('m', `m:${model}-${nozzle}:${bed}`, c.machine), process: ref('p', `p:${model}-${nozzle}:${quality}:${bed}`, c.process), filament: ref('f', `f:${model}-${nozzle}:${filament}`, c.filament) };
  const body = { presets: refs, env: { mode: 'expert', vendor: 'BBL', filamentCount: 1 }, ...params };
  try {
    const r = await eng.request(op, body);
    Object.values(refs).forEach((x) => eng.sent.add(x.hash));
    return r;
  } catch (e) {
    if (!/cache/i.test(e.message) || !Object.values(refs).some((x) => !x.config)) throw e;
    eng.sent.clear();  // the host dropped a config: send them all again
    return settingsCall(eng, ctx, op, params);
  }
}

// What a painted object needs to have any effect: supports must be on for support paint (Orca warns and ignores it otherwise).
export function effectiveObjectConfig(config, process, paint) {
  const out = { ...(config || {}) };
  if (paint?.supports && (out.enable_support ?? process.enable_support) !== '1') out.enable_support = '1';
  return out;
}

// One variant: slice in the (warm) engine, then hand the G-code to the app, which packs and checks it like a Pi slice.
// opts: file, jobId, model, serial, fil, bed, quality, onProgress, objectConfig (ConfigPatch), paint ({positions, states} from paint.js).
// Returns the job and the slice time.
export async function sliceVariant(eng, opts) {
  const { file, jobId, model, serial, bed, quality } = opts, nozzle = opts.nozzle || '0.4';
  const fil = { ...opts.fil, nozzle, ...(opts.nozzleType && { nozzle_type: opts.nozzleType }) };  // fil: {tray} or a library spool, see api.engineFilament
  const [{ filament: name }, buf] = await Promise.all([api.engineFilament(serial, fil), file.arrayBuffer()]);
  const c = await variantConfigs(model, quality, bed, name, nozzle);
  const { machine, process } = c;
  const painted = opts.paint ? toObjectPaint(opts.paint.states) : null;
  // Painted triangles are numbered in the refined mesh that the painter made, so that mesh goes to the engine.
  const positions = placeOnBed(painted ? Float32Array.from(opts.paint.positions) : parseStl(buf), machine);
  const config = effectiveObjectConfig(opts.objectConfig, process, painted);
  const object = { name: file.name, mesh: { positions }, ...(Object.keys(config).length ? { config } : {}), ...(painted ? { paint: painted } : {}) };
  const t0 = performance.now();
  const r = await eng.request('slice', {
    configs: { machine, process, filaments: [c.filament] },
    objects: [object], output: { toolpaths: false },
  }, (p) => opts.onProgress?.(p), [positions.buffer]);
  const sliceMs = performance.now() - t0, st = r.stats;
  const qs = new URLSearchParams({ serial, quality, ...fil, bed, filament: name, seconds: st.printTimeSeconds ?? 0, grams: st.filamentG ?? 0,
    metres: (st.filamentMm ?? 0) / 1000, engine: eng.variant, slice_s: (sliceMs / 1000).toFixed(1) });
  const job = await api.uploadGcode(jobId, qs, r.gcode);
  return { job, sliceMs, warnings: r.warnings || [] };
}
