// What a spool is called in words: material, finish (Basic, Matte, ...) and colour name. No DOM, so node can import it.

// Words of a filament name that name the finish or line. The first one found in the name wins.
const FINISHES = ['Basic', 'Matte', 'Translucent', 'Silk', 'Metal', 'Sparkle', 'Marble', 'Glow', 'Galaxy', 'Gradient', 'Tough', 'Aero', 'Lite', 'Wood', 'Support', 'Impact', 'CF', 'GF', 'HF'];
const NO_FINISH = 'Basic';  // a name with no finish word is the plain line
const MATERIAL_WORD = /^(PLA|PETG|PET|ABS|ASA|TPU|PA\d*|PC|PVA|HIPS|PP|PPS)([-+/].*)?$/i;
const WAITING = ['queued', 'pending_approval'];
const NOISE = new Set(['plus', '+', 'pro', 'filament', 'bambu']);

// Exact hex to name, for colours whose name is known from the data of this repo. Anything else goes to the nearest basic colour.
const KNOWN = { '2E2E2E': 'Charcoal' };

const BASIC = [['Black', '000000'], ['White', 'FFFFFF'], ['Grey', '808080'], ['Silver', 'C0C0C0'], ['Red', 'E02020'], ['Orange', 'FF8000'], ['Yellow', 'FFE600'],
  ['Green', '30C070'], ['Blue', '2060E0'], ['Purple', '8040C0'], ['Pink', 'FF80B0'], ['Brown', '804020'], ['Beige', 'E8D8B8']];

const rgb = (x) => [0, 2, 4].map((i) => parseInt(x.slice(i, i + 2), 16));

export function colourName(colour, finish = '') {
  const c = String(colour || '').replace(/^#/, '').toUpperCase();
  if (!/^[0-9A-F]{6}([0-9A-F]{2})?$/.test(c)) return /translucent/i.test(finish) ? 'Transparent' : 'Unknown';
  if (c.length === 8 && c.slice(6) === '00') return 'Transparent';
  if (KNOWN[c.slice(0, 6)]) return KNOWN[c.slice(0, 6)];
  const [r, g, b] = rgb(c);
  let best = '', low = Infinity;
  for (const [name, h] of BASIC) {
    const [r2, g2, b2] = rgb(h), mean = (r + r2) / 2;  // "redmean" distance, closer to the eye than plain RGB
    const d = (2 + mean / 256) * (r - r2) ** 2 + 4 * (g - g2) ** 2 + (2 + (255 - mean) / 256) * (b - b2) ** 2;
    if (d < low) { low = d; best = name; }
  }
  return best;
}

// {material, finish, colour} of a library spool ({brand, type, name, colour}).
export function describe(s) {
  const words = String(s.name || '').split(/\s+/).filter(Boolean);
  const material = String(s.type || '').trim().toUpperCase() || (words.find((w) => MATERIAL_WORD.test(w)) || 'Other').toUpperCase();
  const finish = FINISHES.find((f) => words.some((w) => w.toLowerCase() === f.toLowerCase())) || NO_FINISH;
  const brand = new Set(String(s.brand || '').toLowerCase().split(/\s+/).filter(Boolean));
  const left = words.filter((w) => {
    const l = w.toLowerCase();
    return !(FINISHES.some((f) => f.toLowerCase() === l) || MATERIAL_WORD.test(w) || /^\d+[a-d]$/i.test(w) || NOISE.has(l) || brand.has(l) || l === material.toLowerCase());
  });
  // The name has the colour in words when something is left after the material, finish and brand.
  return { material, finish, colour: left.length ? left.join(' ') : colourName(s.colour, finish) };
}

// The filament a queued job will use: {colour (hex), name (colour in words), label (material and finish), loaded}, or null when
// the job says nothing. Sources in order: the library spool, the AMS tray of the slice, the slice's tray, the slice's or job's filament list.
// loaded is false only when a library spool is known not to be in the target printer's AMS; null when unknown.
export function jobFilament(j, printers = []) {
  if (!j || j.kind === 'dry') return null;
  const ams = (printers.find((p) => p.serial === j.target)?.ams || []);
  const trays = ams.flatMap((u) => (u.trays || []).map((t) => ({ ...t, unit: u.unit })));
  let src = null, loaded = null;
  if (j.slice_spool) {
    src = j.slice_spool;
    if (ams.length && WAITING.includes(j.state)) loaded = trays.some((t) => t.spool_id != null && String(t.spool_id) === String(src.spool_id));
  } else if (Array.isArray(j.slice_tray)) {
    const t = trays.find((x) => x.unit === j.slice_tray[0] && x.slot === j.slice_tray[1]);
    src = t && t.colour ? { type: t.type, colour: t.colour } : null;  // the tray name carries the brand, so only type and colour
  }
  src = src || (j.slice?.tray?.colour ? j.slice.tray : null) || j.slice?.filaments?.[0] || j.filaments?.[0];
  if (!src || !(src.colour || src.type || src.name)) return null;
  const d = describe({ brand: src.brand, type: src.type, name: src.name, colour: src.colour });
  return { colour: src.colour || '', name: d.colour, label: src.name ? `${d.material} ${d.finish}` : String(src.type || ''), loaded };
}
