// Runs hub/ui/views/gcodeparse.js on stdin and prints a JSON summary (the parser is an ES module without a package.json).
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '../ui/views/gcodeparse.js'), 'utf8');
const { parse } = await import('data:text/javascript;base64,' + Buffer.from(src).toString('base64'));
const r = parse(readFileSync(0, 'utf8'));
const features = {};
const segs = [];
for (const g of r.groups) {
  features[g.feature] = (features[g.feature] || 0) + g.positions.length / 6;
  if (g.feature !== 'Travel') for (let i = 0; i < g.positions.length; i += 6) segs.push([...g.positions.slice(i, i + 6)]);
}
console.log(JSON.stringify({ objects: r.objects.length, plate: r.plate, features, segments: r.segments, layers: r.layers, segs: segs.length < 5000 ? segs : null, objectBoxes: r.objects.map((o) => o.box) }));
