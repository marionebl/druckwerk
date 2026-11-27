// PROTOTYPE (measurement only, nothing ships): slice on the server with the Muon3D Slicer Engine (OrcaSlicer compiled
// to WebAssembly) in Node.js, with the contract of slice.sh:
//   node node_slice.mjs <model.stl> <out.gcode | out.gcode.3mf> <filament preset name>
// env: ENGINE_DIR (folder with manifest.json, default ./engine/<tag> next to this file), PRESETS (folder with <model>.json,
//      default ./presets), MODEL (H2S | P1S, default H2S), QUALITY (good|better|best, default better), BED (default
//      Textured PEI Plate), VARIANT (st | mt | mt64, default st), PRINTER_SERIAL unused.
// It calls the engine like hub/ui/views/browserslice.js: worker host, protocol v2, ops hello, load, slice.
// A .gcode.3mf output is packed by hub/slicer/pack.py, as the hub does for a browser slice.
import fs from 'node:fs';
import path from 'node:path';
import { Worker } from 'node:worker_threads';
import { execFileSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const [modelFile, outFile, filamentName] = process.argv.slice(2);
if (!modelFile || !outFile || !filamentName) { console.error('usage: node_slice.mjs <model.stl> <out.gcode|.gcode.3mf> <filament>'); process.exit(2); }
const MODEL = process.env.MODEL || 'H2S', QUALITY = process.env.QUALITY || 'better', BED = process.env.BED || 'Textured PEI Plate';
const VARIANT = process.env.VARIANT || 'st';
const ENGINE_DIR = path.resolve(process.env.ENGINE_DIR || path.join(HERE, 'engine-runtime'));
const PRESETS = path.resolve(process.env.PRESETS || path.join(HERE, 'presets'));

function parseStl(b) {
  const buf = b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength), dv = new DataView(buf);
  if (buf.byteLength >= 84 && buf.byteLength === 84 + dv.getUint32(80, true) * 50) {
    const n = dv.getUint32(80, true), out = new Float32Array(n * 9);
    for (let t = 0; t < n; t++) for (let i = 0; i < 9; i++) out[t * 9 + i] = dv.getFloat32(84 + t * 50 + 12 + i * 4, true);
    return out;
  }
  const v = [...new TextDecoder().decode(buf).matchAll(/^\s*vertex\s+(\S+)\s+(\S+)\s+(\S+)/gm)].flatMap((m) => [+m[1], +m[2], +m[3]]);
  if (!v.length || v.length % 9) throw new Error('Not an STL file: no triangles found.');
  return new Float32Array(v);
}
function placeOnBed(pos, machine) {  // same as browserslice.js
  const area = [machine.printable_area].flat().join(',').split(',').map((p) => p.split('x').map(Number));
  const mid = (a) => (Math.min(...area.map((p) => p[a])) + Math.max(...area.map((p) => p[a]))) / 2;
  const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < pos.length; i += 3) for (let a = 0; a < 3; a++) { lo[a] = Math.min(lo[a], pos[i + a]); hi[a] = Math.max(hi[a], pos[i + a]); }
  const d = [mid(0) - (lo[0] + hi[0]) / 2, mid(1) - (lo[1] + hi[1]) / 2, -lo[2]];
  for (let i = 0; i < pos.length; i += 3) for (let a = 0; a < 3; a++) pos[i + a] += d[a];
  return pos;
}

const manifest = JSON.parse(fs.readFileSync(path.join(ENGINE_DIR, 'manifest.json'), 'utf8'));
const presets = JSON.parse(fs.readFileSync(path.join(PRESETS, `${MODEL}.json`), 'utf8'));
const filament = presets.filaments.find((f) => f.name === filamentName);
if (!filament) { console.error('FAIL: no filament preset ' + filamentName); process.exit(2); }
const machine = { ...presets.machine, curr_bed_type: BED }, process_ = { ...presets.process[QUALITY], curr_bed_type: BED };
const positions = placeOnBed(parseStl(fs.readFileSync(modelFile)), machine);

const t0 = performance.now();
const worker = new Worker(pathToFileURL(path.join(ENGINE_DIR, manifest.host.file)), { workerData: { engineBase: pathToFileURL(ENGINE_DIR + '/').href } });
let next = 0; const pending = new Map();
worker.on('message', (m) => {
  const p = pending.get(m.id); if (!p) return;
  if (m.kind === 'progress') { if (process.env.PROGRESS) process.stderr.write(`\rprogress ${JSON.stringify(m.progress).slice(0, 80)}`); }
  else if (m.kind === 'result') { pending.delete(m.id); p.resolve(m.result); }
  else if (m.kind === 'error') { pending.delete(m.id); p.reject(new Error(m.error.message + (m.error.detail ? ` (${m.error.detail})` : ''))); }
});
worker.on('error', (e) => { for (const p of pending.values()) p.reject(e); pending.clear(); });
const request = (op, params, transfer = []) => new Promise((resolve, reject) => {
  const id = ++next; pending.set(id, { resolve, reject }); worker.postMessage({ v: 2, id, op, params }, transfer);
});
try {
  const hello = await request('hello', { protocol: { major: 2 }, client: { name: 'druckwerk-node', version: '0' } });
  console.error('hello: variants=' + JSON.stringify(hello.variants) + ' env=' + JSON.stringify(hello.environment ?? hello.env ?? null));
  if (!hello.variants.includes(VARIANT)) throw new Error(`variant ${VARIANT} not offered: ${hello.variants}`);
  await request('load', { variant: VARIANT });
  const tLoad = performance.now();
  const r = await request('slice', {
    configs: { machine, process: process_, filaments: [filament.config] },
    objects: [{ name: path.basename(modelFile), mesh: { positions } }], output: { toolpaths: false },
  }, [positions.buffer]);
  const tEnd = performance.now(), st = r.stats ?? {};
  const gcode = typeof r.gcode === 'string' ? r.gcode : Buffer.from(r.gcode);
  const gpath = outFile.endsWith('.3mf') ? outFile.replace(/\.3mf$/, '') : outFile;
  fs.mkdirSync(path.dirname(path.resolve(gpath)), { recursive: true });
  fs.writeFileSync(gpath, gcode);
  if (outFile.endsWith('.3mf')) {
    const py = `import sys, json; sys.path.insert(0, sys.argv[1]); import pack
a = json.load(open(sys.argv[2])); P = json.load(open(sys.argv[3]))
f = next(x for x in P["filaments"] if x["name"] == a["filament"])
pack.pack(a["out"], open(a["gcode"], "rb").read(), {"machine": P["machine"], "process": P["process"][a["quality"]]}, f, a["bed"], a["model_id"], "#808080", a["seconds"], a["grams"], a["metres"], a["name"])`;
    const args = path.join(path.dirname(path.resolve(gpath)), '.pack-args.json');
    fs.writeFileSync(args, JSON.stringify({ out: outFile, gcode: gpath, filament: filamentName, quality: QUALITY, bed: BED, model_id: MODEL === 'H2S' ? 'O1S' : 'C12',
      seconds: Math.round(st.printTimeSeconds ?? 0), grams: st.filamentG ?? 0, metres: (st.filamentMm ?? 0) / 1000, name: path.basename(modelFile) }));
    execFileSync('python3', ['-I', '-c', py, path.join(HERE, '..'), args, path.join(PRESETS, `${MODEL}.json`)]);
    fs.unlinkSync(args);
  }
  console.log(JSON.stringify({ model: MODEL, quality: QUALITY, variant: VARIANT, filament: filamentName, load_s: +((tLoad - t0) / 1000).toFixed(2),
    slice_s: +((tEnd - tLoad) / 1000).toFixed(2), stats: st, gcode_bytes: gcode.length, out: outFile }));
} catch (e) {
  console.error('FAIL: ' + (e.stack || e)); process.exitCode = 3;
} finally { await worker.terminate(); }
