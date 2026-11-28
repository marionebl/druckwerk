// Slicing in the browser: OrcaSlicer compiled to WebAssembly (Muon3D Slicer Engine) in a Web Worker, speaking the
// engine's protocol v2. The server hands out the runtime at engine/ and the flattened presets; see hub/slicer/engine.
import { api } from '../api.js';
import { toObjectPaint } from './paintcodes.js';
import { toBed } from './plate.js';
export { parseStl } from './plate.js';

const cache = { presets: {} };

function connect(worker) {
  let next = 0; const pending = new Map();
  worker.onmessage = ({ data: m }) => {
    const p = pending.get(m.id);
    if (!p) return;
    if (m.kind === 'progress') p.progress?.(m.progress);
    else if (m.kind === 'result') { pending.delete(m.id); p.resolve(m.result); }
    else if (m.kind === 'error') { pending.delete(m.id); p.reject(Object.assign(new Error(m.error.message + (m.error.detail ? ` (${m.error.detail})` : '')), { code: m.error.code, conflict: m.error.conflict })); }
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

// The presets of a printer model, loaded once. Per request they are cut to what the engine needs.
export async function loadPresets(model) {
  return (cache.presets[model] ||= await (await fetch(`engine/presets/${model}.json`)).json());
}

// The flattened configs of a variant. Orca refuses PETG on its default Cool Plate, so the plate must be set.
export async function variantConfigs(model, quality, bed, filamentName) {
  const presets = await loadPresets(model);
  const filament = presets.filaments.find((f) => f.name === filamentName);
  if (!filament) throw new Error('No filament preset ' + filamentName);
  return { machine: { ...presets.machine, curr_bed_type: bed }, process: { ...presets.process[quality], curr_bed_type: bed }, filament: filament.config };
}

// Settings service of the engine (settings.view, settings.edit). Configs travel once per engine and then by hash.
export async function settingsCall(eng, ctx, op, params) {
  const { model, quality, bed, filament } = ctx;
  const c = await variantConfigs(model, quality, bed, filament);
  eng.sent ||= new Set();
  const ref = (kind, hash, config) => (eng.sent.has(hash) ? { hash } : { hash, config });
  const refs = { machine: ref('m', `m:${model}:${bed}`, c.machine), process: ref('p', `p:${model}:${quality}:${bed}`, c.process), filament: ref('f', `f:${model}:${filament}`, c.filament) };
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

// What a failed slice says in plain words. Code -101: two objects' paths cross on one layer.
export function sliceError(e) {
  const c = e.conflict;
  return e.code === -101 && c ? `"${c.objects[0]}" and "${c.objects[1]}" touch each other at ${c.z} mm height. Press Arrange to move them apart.` : e.message;
}

// One variant: slice the whole plate in the (warm) engine, then hand the G-code to the app, which packs and checks it like a Pi slice.
// opts: objects (plate objects, see plate.js), names (unique, same order), jobId, model, serial, fil, bed, quality, onProgress.
// A plate object has config (ConfigPatch) and paint ({positions, states} in its local frame, from paint.js). Returns the job and the slice time.
export async function sliceVariant(eng, opts) {
  const { objects, names, jobId, model, serial, fil, bed, quality } = opts;  // fil: {tray} or a library spool, see api.engineFilament
  const { filament: name } = await api.engineFilament(serial, fil);
  const c = await variantConfigs(model, quality, bed, name);
  const { machine, process } = c;
  // Painted triangles are numbered in the refined mesh that the painter made, so that mesh goes to the engine.
  const plate = objects.map((o, i) => {
    const painted = o.paint ? toObjectPaint(o.paint.states) : null, config = effectiveObjectConfig(o.config, process, painted);
    return { name: names[i], mesh: { positions: toBed(painted ? o.paint.positions : o.local, o) }, ...(Object.keys(config).length ? { config } : {}), ...(painted ? { paint: painted } : {}) };
  });
  const t0 = performance.now();
  const r = await eng.request('slice', {
    configs: { machine, process, filaments: [c.filament] },
    objects: plate, output: { toolpaths: false },
  }, (p) => opts.onProgress?.(p), plate.map((o) => o.mesh.positions.buffer)).catch((e) => { throw Object.assign(new Error(sliceError(e)), { code: e.code }); });
  const sliceMs = performance.now() - t0, st = r.stats;
  const qs = new URLSearchParams({ serial, quality, ...fil, bed, filament: name, seconds: st.printTimeSeconds ?? 0, grams: st.filamentG ?? 0,
    metres: (st.filamentMm ?? 0) / 1000, engine: eng.variant, slice_s: (sliceMs / 1000).toFixed(1) });
  const job = await api.uploadGcode(jobId, qs, r.gcode);
  return { job, sliceMs, warnings: r.warnings || [] };
}
