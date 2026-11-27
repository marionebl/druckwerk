// Slicing in the browser: OrcaSlicer compiled to WebAssembly (Muon3D Slicer Engine) in a Web Worker, speaking the
// engine's protocol v2. The server hands out the runtime at engine/ and the flattened presets; see hub/slicer/engine.
import { api } from '../api.js';

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

function parseStl(buf) {
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
function placeOnBed(pos, machine) {
  const area = [machine.printable_area].flat().join(',').split(',').map((p) => p.split('x').map(Number));
  const mid = (a) => (Math.min(...area.map((p) => p[a])) + Math.max(...area.map((p) => p[a]))) / 2;
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < pos.length; i += 3) for (let a = 0; a < 3; a++) { lo[a] = Math.min(lo[a], pos[i + a]); hi[a] = Math.max(hi[a], pos[i + a]); }
  const d = [mid(0) - (lo[0] + hi[0]) / 2, mid(1) - (lo[1] + hi[1]) / 2, -lo[2]];
  for (let i = 0; i < pos.length; i += 3) for (let a = 0; a < 3; a++) pos[i + a] += d[a];
  return pos;
}

// One variant: slice in the (warm) engine, then hand the G-code to the app, which packs and checks it like a Pi slice.
// opts: file, jobId, model, serial, fil, bed, quality, onProgress. Returns the job and the slice time.
export async function sliceVariant(eng, opts) {
  const { file, jobId, model, serial, fil, bed, quality } = opts;  // fil: {tray} or a library spool, see api.engineFilament
  cache.presets[model] ||= await (await fetch(`engine/presets/${model}.json`)).json();
  const presets = cache.presets[model];
  const [{ filament: name }, buf] = await Promise.all([api.engineFilament(serial, fil), file.arrayBuffer()]);
  const filament = presets.filaments.find((f) => f.name === name);
  if (!filament) throw new Error('No filament preset ' + name);
  // Orca refuses PETG on its default Cool Plate, so the plate must be set.
  const machine = { ...presets.machine, curr_bed_type: bed }, process = { ...presets.process[quality], curr_bed_type: bed };
  const positions = placeOnBed(parseStl(buf), machine);
  const t0 = performance.now();
  const r = await eng.request('slice', {
    configs: { machine, process, filaments: [filament.config] },
    objects: [{ name: file.name, mesh: { positions } }], output: { toolpaths: false },
  }, (p) => opts.onProgress?.(p), [positions.buffer]);
  const sliceMs = performance.now() - t0, st = r.stats;
  const qs = new URLSearchParams({ serial, quality, ...fil, bed, filament: name, seconds: st.printTimeSeconds ?? 0, grams: st.filamentG ?? 0,
    metres: (st.filamentMm ?? 0) / 1000, engine: eng.variant, slice_s: (sliceMs / 1000).toFixed(1) });
  const job = await api.uploadGcode(jobId, qs, r.gcode);
  return { job, sliceMs };
}
