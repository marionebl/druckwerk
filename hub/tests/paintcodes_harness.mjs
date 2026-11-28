// Runs hub/ui/views/paintcodes.js: prints the refined size and the engine paint of a small hand-made mesh.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const src = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '../ui/views/paintcodes.js'), 'utf8');
const { subdivide, refinePasses, toObjectPaint } = await import('data:text/javascript;base64,' + Buffer.from(src).toString('base64'));
const tri = Float32Array.from([0, 0, 0, 8, 0, 0, 0, 8, 0]);
const sub = subdivide(tri, 2);
const area = (p, t) => { const o = t * 9, u = [p[o + 3] - p[o], p[o + 4] - p[o + 1]], v = [p[o + 6] - p[o], p[o + 7] - p[o + 1]]; return Math.abs(u[0] * v[1] - u[1] * v[0]) / 2; };
let total = 0; for (let t = 0; t < sub.length / 9; t++) total += area(sub, t);
const paint = toObjectPaint({ supports: Uint8Array.from([0, 1, 2, 0, 1]), seam: Uint8Array.from([0, 0, 0, 0, 0]) });
console.log(JSON.stringify({ tris: sub.length / 9, area: total, passes: refinePasses(tri, 1.5), capped: refinePasses(tri, 0.001, 100),
  paint: { triangles: [...paint.supports.triangles], codes: paint.supports.codes, ends: [...paint.supports.ends], kinds: Object.keys(paint) }, none: toObjectPaint({ seam: new Uint8Array(3) }) }));
