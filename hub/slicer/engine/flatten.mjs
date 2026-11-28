// usage: node flatten.mjs <profileSet.ts> <profile set dir> <printers.json> <out dir>
// Writes <out>/<model>.json per printer (0.4 mm) and <out>/<model>-<diameter>.json per other nozzle: its machine preset, the process preset of each quality and every filament
// preset that names the printer, all flattened by the engine's own flattenPreset.
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { pathToFileURL } from 'node:url';

const [protoTs, setDir, printersFile, outDir] = process.argv.slice(2);
const { flattenPreset } = await import(pathToFileURL(protoTs).href);
const printers = JSON.parse(fs.readFileSync(printersFile, 'utf8'));

const readJson = (f) => {
  let b = fs.readFileSync(f);
  if (b[0] === 0x1f && b[1] === 0x8b) b = zlib.gunzipSync(b);
  return JSON.parse(b.toString('utf8'));
};
const indexFile = fs.readdirSync(setDir).find((n) => /^index\..*\.json$/.test(n));
const index = readJson(path.join(setDir, indexFile));
const bundleOf = (id) => readJson(path.join(setDir, index.vendors.find((v) => v.id === id).bundle.path));
const vendor = bundleOf('BBL');
const library = bundleOf(index.library);
const sources = { vendor, library, defaults: index.defaults };
console.log(`profile set ${index.set}, engine ${index.engine.version}, Orca ${index.engine.orca.version}`);

const list = (v) => (Array.isArray(v) ? v : String(v ?? '').split(';').map((s) => s.trim()).filter(Boolean));
const first = (v) => list(v)[0] ?? '';
const filamentNames = new Set([...Object.keys(vendor.presets.filament), ...Object.keys(library.presets.filament)]);

const variants = Object.entries(printers).flatMap(([model, { nozzles = {}, ...cfg }]) =>
  [[model, '0.4', cfg], ...Object.entries(nozzles).map(([d, n]) => [model, d, n])]);
for (const [model, diameter, cfg] of variants) {
  const machine = flattenPreset('machine', cfg.preset, sources);
  const process = Object.fromEntries(
    Object.entries(cfg.quality).map(([quality, name]) => [quality, flattenPreset('process', name, sources)]),
  );
  const filaments = [];
  for (const name of filamentNames) {
    const owner = vendor.presets.filament[name] ?? library.presets.filament[name];
    if (owner.instantiation !== 'true') continue; // base profiles are not selectable
    let config;
    try { config = flattenPreset('filament', name, sources); } catch { continue; }
    const printersOf = list(config.compatible_printers);
    if (!printersOf.includes(cfg.preset)) continue;
    filaments.push({ name, id: first(config.filament_id), type: first(config.filament_type), n: printersOf.length, config });
  }
  if (!filaments.length) throw new Error(`no filament preset names ${cfg.preset}`);
  const file = diameter === '0.4' ? model : `${model}-${diameter}`;
  fs.writeFileSync(path.join(outDir, `${file}.json`), JSON.stringify({ model, machine, process, filaments }));
  console.log(`${file}: ${machine.name}, ${Object.keys(process).length} processes, ${filaments.length} filaments`);
}
