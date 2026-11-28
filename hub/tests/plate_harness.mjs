// Runs hub/ui/views/plate.js on hand-made meshes and the fixtures; prints one JSON object with the numbers the tests check.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '../ui/views/plate.js'), 'utf8');
const P = await import('data:text/javascript;base64,' + Buffer.from(src).toString('base64'));
const stl = (f) => P.parseStl(readFileSync(join(here, 'fixtures', f)).buffer.slice(0));

// --- meshes (triangle soups, outward normals)
const tri = (out, a, b, c) => out.push(...a, ...b, ...c);
const quad = (out, a, b, c, d) => { tri(out, a, b, c); tri(out, a, c, d); };
function box(w, d, h, o = [0, 0, 0]) {
  const [x0, y0, z0] = o, x1 = x0 + w, y1 = y0 + d, z1 = z0 + h, out = [];
  quad(out, [x0, y0, z0], [x0, y1, z0], [x1, y1, z0], [x1, y0, z0]); quad(out, [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]);
  quad(out, [x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1]); quad(out, [x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [x1, y0, z1]);
  quad(out, [x1, y1, z0], [x0, y1, z0], [x0, y1, z1], [x1, y1, z1]); quad(out, [x0, y1, z0], [x0, y0, z0], [x0, y0, z1], [x0, y1, z1]);
  return out;
}
function cyl(r, h, z0 = 0, seg = 48, r2 = r) {  // r2 < r: a cone frustum
  const out = [], pt = (i, z, rr) => [rr * Math.cos((i / seg) * 2 * Math.PI), rr * Math.sin((i / seg) * 2 * Math.PI), z];
  for (let i = 0; i < seg; i++) {
    const a = pt(i, z0, r), b = pt(i + 1, z0, r), c = pt(i + 1, z0 + h, r2), d = pt(i, z0 + h, r2);
    tri(out, [0, 0, z0], b, a);
    if (r2 > 0) { tri(out, [0, 0, z0 + h], d, c); quad(out, a, b, c, d); } else tri(out, a, b, [0, 0, z0 + h]);
  }
  return out;
}
function pyramid(s, h) {
  const out = [], a = [-s / 2, -s / 2, 0], b = [s / 2, -s / 2, 0], c = [s / 2, s / 2, 0], d = [-s / 2, s / 2, 0], t = [0, 0, h];
  quad(out, a, d, c, b); tri(out, a, b, t); tri(out, b, c, t); tri(out, c, d, t); tri(out, d, a, t);
  return out;
}
const F = (a) => Float32Array.from(a);
const turn = (pos, deg, axis) => {  // rotate a soup about x or y by deg
  const c = Math.cos((deg * Math.PI) / 180), s = Math.sin((deg * Math.PI) / 180), out = new Float32Array(pos.length);
  for (let i = 0; i < pos.length; i += 3) {
    const [x, y, z] = [pos[i], pos[i + 1], pos[i + 2]];
    [out[i], out[i + 1], out[i + 2]] = axis === 'x' ? [x, c * y - s * z, s * y + c * z] : [c * x + s * z, y, -s * x + c * z];
  }
  return out;
};
const obj = (local, name = 'a.stl') => ({ id: name, name, local, rot: P.IDENTITY, oriented: false, at: [128, 128], config: {}, paint: null });

// After orient: the height of the model and the contact area (how far the model is from standing on its best face).
function pose(local) {
  const o = obj(local), t0 = performance.now(); P.autoOrient(o); const ms = performance.now() - t0, s = P.size(o), b = P.placed(o);
  let z0 = Infinity; for (let i = 2; i < b.length; i += 3) z0 = Math.min(z0, b[i]);
  return { size: s.map((v) => +v.toFixed(2)), rot: o.rot.map((v) => +v.toFixed(3)), ms: Math.round(ms), minZ: +z0.toFixed(4), identity: o.rot.every((v, i) => Math.abs(v - P.IDENTITY[i]) < 1e-6) };
}

const out = {};
const cube = F(box(20, 20, 20));
out.cube30x = pose(turn(cube, 30, 'x'));
out.cube40y = pose(turn(cube, 40, 'y'));
out.cube30xy = pose(turn(turn(cube, 30, 'x'), 40, 'y'));
out.cubeFlat = pose(cube);
out.plateFlat = pose(F(box(100, 100, 3)));
out.plateOnEdge = pose(turn(F(box(100, 3, 100)), 90, 'x'));
out.plateTilted = pose(turn(F(box(100, 100, 3)), 25, 'x'));
out.pyramid = pose(F(pyramid(30, 30)));
out.pyramidApexDown = pose(turn(F(pyramid(30, 30)), 180, 'x'));
out.cone = pose(F(cyl(15, 30, 0, 48, 0)));
out.coneApexDown = pose(turn(F(cyl(15, 30, 0, 48, 0)), 180, 'x'));
const mushroom = [...cyl(20, 5, 25), ...cyl(5, 25, 0)];  // cap on a stem
// Width of the model near the bed: a cap down is 40 wide there, a stem down is 10 wide.
const baseWidth = (local) => { const o = obj(local); P.autoOrient(o); const b = P.placed(o); let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < b.length; i += 3) if (b[i + 2] < 4.9) { lo = Math.min(lo, b[i]); hi = Math.max(hi, b[i]); } return +(hi - lo).toFixed(1); };
out.mushroom = { ...pose(F(mushroom)), base: baseWidth(F(mushroom)) };  // given stem down, cap up
out.mushroomCapDown = { ...pose(turn(F(mushroom), 180, 'x')), base: baseWidth(turn(F(mushroom), 180, 'x')) };
const T = [...box(60, 10, 8, [0, 0, 22]), ...box(10, 10, 22, [25, 0, 0])];
out.tshape = pose(F(T));
out.fixture = pose(stl('test_plate.stl'));
out.cube20 = pose(stl('cube20.stl'));

// speed on a big mesh: a sphere-ish cylinder stack with ~150k triangles, tilted
{
  const big = []; const seg = 600;
  for (let k = 0; k < 125; k++) big.push(...cyl(10, 0.2, k * 0.2, seg, 10));
  out.bigTris = big.length / 9; out.big = pose(turn(F(big), 33, 'x'));
}

// arrange
const mach = { printable_area: ['0x0', '256x0', '256x256', '0x256'] };
const overlap = (a, b, gap) => { const [sa, sb] = [P.size(a), P.size(b)];
  return Math.abs(a.at[0] - b.at[0]) < (sa[0] + sb[0]) / 2 + gap - 1e-6 && Math.abs(a.at[1] - b.at[1]) < (sa[1] + sb[1]) / 2 + gap - 1e-6; };
const inside = (o, m) => { const bed = P.bedOf(m), s = P.size(o); return o.at[0] - s[0] / 2 >= bed.x0 - 1e-6 && o.at[0] + s[0] / 2 <= bed.x1 + 1e-6 && o.at[1] - s[1] / 2 >= bed.y0 - 1e-6 && o.at[1] + s[1] / 2 <= bed.y1 + 1e-6; };
{
  const objs = [obj(F(box(60, 40, 10)), 'a'), obj(F(box(100, 90, 20)), 'b'), obj(F(box(20, 20, 20)), 'c'), obj(F(box(80, 80, 5)), 'd'), obj(F(box(50, 30, 5)), 'e')];
  P.arrange(objs, mach);
  let ov = 0; for (let i = 0; i < objs.length; i++) for (let j = i + 1; j < objs.length; j++) if (overlap(objs[i], objs[j], P.GAP)) ov++;
  out.arrange = { overlaps: ov, allInside: objs.every((o) => inside(o, mach)), allFit: objs.every((o) => o.fit) };
  const one = [obj(F(box(20, 20, 20)), 'x')]; P.arrange(one, mach); out.single = one[0].at;
  const big = [obj(F(box(300, 20, 5)), 'long'), obj(F(box(20, 20, 20)), 'small')]; P.arrange(big, mach);
  out.tooBig = { long: big[0].fit, small: big[1].fit, smallInside: inside(big[1], mach) };
  const full = [obj(F(box(200, 200, 5)), 'f1'), obj(F(box(200, 200, 5)), 'f2')]; P.arrange(full, mach);
  out.full = [full[0].fit, full[1].fit];
  const tall = [obj(F(box(20, 20, 300)), 'tall')]; P.arrange(tall, { ...mach, printable_height: '250' }); out.tallFit = tall[0].fit;
  const keep = { ...mach, bed_exclude_area: ['0x0', '60x0', '60x40', '0x40'] };
  const kx = [obj(F(box(30, 30, 5)), 'k1'), obj(F(box(30, 30, 5)), 'k2'), obj(F(box(30, 30, 5)), 'k3')];
  P.arrange(kx, keep);
  out.keepOut = kx.every((o) => { const s = P.size(o); return !(o.at[0] - s[0] / 2 < 60 && o.at[1] - s[1] / 2 < 40) && inside(o, keep); });
  const circ = { printable_area: ['100x0', '70x70', '0x100', '-70x70'].map((s) => s) };
  out.bedBox = (({ x0, y0, x1, y1 }) => [x0, y0, x1, y1])(P.bedOf(mach));
}

// placement rests on z = 0 and the footprint is centred on `at`
{
  const o = obj(turn(F(box(20, 10, 30, [5, -7, 11])), 20, 'x')); P.autoOrient(o); o.at = [50, 70];
  const b = P.placed(o), lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < b.length; i += 3) for (let a = 0; a < 3; a++) { lo[a] = Math.min(lo[a], b[i + a]); hi[a] = Math.max(hi[a], b[i + a]); }
  out.placed = { minZ: lo[2], centre: [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2], size: [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]] };
}

// writeStl round trip through parseStl, two objects in one file
{
  const a = F(box(20, 20, 20)), c = F(cyl(5, 9, 0, 12)), buf = P.writeStl([a, c]), back = P.parseStl(buf);
  out.roundTrip = { tris: back.length / 9, want: (a.length + c.length) / 9, bytes: buf.byteLength, same: back.length === a.length + c.length && [...a, ...c].every((v, i) => Math.abs(v - back[i]) < 1e-5) };
}
out.names = P.uniqueNames([{ name: 'a.stl' }, { name: 'b.stl' }, { name: 'a.stl' }, { name: 'a.stl' }]);
out.upload = [P.uploadName([{ name: 'a.stl' }]), P.uploadName([{ name: 'a.STL' }, { name: 'b.stl' }, { name: 'c.stl' }])];
console.log(JSON.stringify(out));
