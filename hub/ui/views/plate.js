// The plate of New print as pure geometry (no DOM): auto-orient, arrange, placement in bed coordinates, binary STL.
// An object keeps its raw STL ("local"); its pose is rot (3x3, row-major) and `at` (footprint centre on the bed).

export const IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1];
export const GAP = 6;  // mm between footprints
const BED256 = '0x0,256x0,256x256,0x256';
const pts = (v) => [v].flat().join(',').split(',').map((p) => p.split('x').map(Number)).filter((p) => p.length === 2 && p.every(Number.isFinite));

// STL file (binary or ASCII) to a triangle soup, 9 floats per triangle.
export function parseStl(buf) {
  const dv = new DataView(buf);
  // binary: 84 bytes + 50 per triangle; some exporters pad the end. The count of an ASCII file reads as a huge number.
  if (buf.byteLength >= 84 && dv.getUint32(80, true) > 0 && buf.byteLength >= 84 + dv.getUint32(80, true) * 50) {
    const n = dv.getUint32(80, true), out = new Float32Array(n * 9);
    for (let t = 0; t < n; t++) for (let i = 0; i < 9; i++) out[t * 9 + i] = dv.getFloat32(84 + t * 50 + 12 + i * 4, true);
    return out;
  }
  const v = [...new TextDecoder().decode(buf).matchAll(/^\s*vertex\s+(\S+)\s+(\S+)\s+(\S+)/gm)].flatMap((m) => [+m[1], +m[2], +m[3]]);
  if (!v.length || v.length % 9) throw new Error('Not an STL file: no triangles found.');
  return new Float32Array(v);
}

// The bed of a machine preset: outline, its bounding box, the print height and a keep-out box (or null). No machine: 256 x 256.
export function bedOf(machine) {
  const poly = pts(machine?.printable_area || BED256), xs = poly.map((p) => p[0]), ys = poly.map((p) => p[1]);
  const ex = pts(machine?.bed_exclude_area || ''), exs = ex.map((p) => p[0]), eys = ex.map((p) => p[1]);
  const keep = ex.length >= 3 && Math.max(...exs) > Math.min(...exs) && Math.max(...eys) > Math.min(...eys)
    ? { x0: Math.min(...exs), y0: Math.min(...eys), x1: Math.max(...exs), y1: Math.max(...eys) } : null;
  const h = parseFloat([machine?.printable_height].flat()[0]);
  return { poly, x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys), h: h > 0 ? h : Infinity, exclude: keep, exclusion: ex };
}

export const rotate = (R, x, y, z) => [R[0] * x + R[1] * y + R[2] * z, R[3] * x + R[4] * y + R[5] * z, R[6] * x + R[7] * y + R[8] * z];

// The rotation that turns the unit vector d to -Z (Rodrigues; a half turn about X when d is +Z).
function down(d) {
  const [x, y, z] = d, c = -z;  // d . (0,0,-1)
  if (c > 1 - 1e-9) return IDENTITY;
  if (c < -1 + 1e-9) return [1, 0, 0, 0, -1, 0, 0, 0, -1];
  const v = [y * -1 - z * 0, z * 0 - x * -1, 0], k = 1 / (1 + c);  // v = d x (0,0,-1)
  const K = [0, -v[2], v[1], v[2], 0, -v[0], -v[1], v[0], 0], K2 = [];
  for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) K2.push(K[i * 3] * K[j] + K[i * 3 + 1] * K[3 + j] + K[i * 3 + 2] * K[6 + j]);
  return IDENTITY.map((e, i) => e + K[i] + K2[i] * k);
}

// Orca's Orient.cpp in short: try the big face directions as "down", score overhang, height and contact, keep the best.
const K_HEIGHT = 0.25, K_BOTTOM = 0.5, OVERHANG = -0.707, FLAT = -0.99, LAYER0 = 0.15, UNSTABLE = 0.01;
export function orient(pos) {
  const n = pos.length / 9, nor = new Float32Array(n * 3), area = new Float32Array(n), cells = new Map();
  let total = 0;
  for (let t = 0; t < n; t++) {
    const o = t * 9, ux = pos[o + 3] - pos[o], uy = pos[o + 4] - pos[o + 1], uz = pos[o + 5] - pos[o + 2];
    const vx = pos[o + 6] - pos[o], vy = pos[o + 7] - pos[o + 1], vz = pos[o + 8] - pos[o + 2];
    const cx = uy * vz - uz * vy, cy = uz * vx - ux * vz, cz = ux * vy - uy * vx, l = Math.hypot(cx, cy, cz);
    if (!l) continue;
    const a = l / 2, x = cx / l, y = cy / l, z = cz / l;
    nor[t * 3] = x; nor[t * 3 + 1] = y; nor[t * 3 + 2] = z; area[t] = a; total += a;
    const key = Math.round(x * 10) * 441 + Math.round(y * 10) * 21 + Math.round(z * 10), c = cells.get(key) || { v: [0, 0, 0], a: 0 };
    c.v[0] += x * a; c.v[1] += y * a; c.v[2] += z * a; c.a += a; cells.set(key, c);
  }
  if (!total) return IDENTITY;
  const dirs = [[0, 0, -1], [0, 0, 1], [1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0]];  // the current down first: it wins ties
  for (const c of [...cells.values()].sort((a, b) => b.a - a.a).slice(0, 30)) { const l = Math.hypot(...c.v); if (l) dirs.push(c.v.map((e) => e / l)); }
  const zs = new Float32Array(n * 3), sz = Math.sqrt(total);
  let best = null;
  for (const [dx, dy, dz] of dirs) {
    let lo = Infinity, hi = -Infinity;
    for (let i = 0; i < n * 3; i++) { const o = i * 3, z = -(pos[o] * dx + pos[o + 1] * dy + pos[o + 2] * dz); zs[i] = z; if (z < lo) lo = z; if (z > hi) hi = z; }
    let bottom = 0, over = 0;
    for (let t = 0; t < n; t++) {
      const nz = -(nor[t * 3] * dx + nor[t * 3 + 1] * dy + nor[t * 3 + 2] * dz);
      if (nz >= OVERHANG) continue;
      if (nz < FLAT && Math.max(zs[t * 3], zs[t * 3 + 1], zs[t * 3 + 2]) < lo + LAYER0) bottom += area[t]; else over += area[t];
    }
    const cost = over / total + (K_HEIGHT * (hi - lo)) / sz - (K_BOTTOM * bottom) / total + (bottom / total < UNSTABLE ? 1 : 0);
    const tilt = -dz;  // how little this turns the model from the current pose: ties keep the smaller turn
    if (!best || cost < best.cost - 1e-4 || (Math.abs(cost - best.cost) <= 1e-4 && tilt > best.tilt)) best = { cost, tilt, d: [dx, dy, dz] };
  }
  return down(best.d);
}

// Bounding box of the rotated model (cached per rotation).
function box(o) {
  if (o._box?.rot === o.rot) return o._box;
  const p = o.local, R = o.rot, lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < p.length; i += 3) {
    const v = rotate(R, p[i], p[i + 1], p[i + 2]);
    for (let a = 0; a < 3; a++) { if (v[a] < lo[a]) lo[a] = v[a]; if (v[a] > hi[a]) hi[a] = v[a]; }
  }
  return (o._box = { rot: R, lo, hi });
}
export const size = (o) => { const { lo, hi } = box(o); return [hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]]; };

// Local positions (the model's own, or the painter's refined copy) to bed coordinates: rotate, rest on z = 0, footprint centred on `at`.
export function toBed(pos, o) {
  const { lo, hi } = box(o), R = o.rot, t = [o.at[0] - (lo[0] + hi[0]) / 2, o.at[1] - (lo[1] + hi[1]) / 2, -lo[2]], out = new Float32Array(pos.length);
  for (let i = 0; i < pos.length; i += 3) { const v = rotate(R, pos[i], pos[i + 1], pos[i + 2]); out[i] = v[0] + t[0]; out[i + 1] = v[1] + t[1]; out[i + 2] = v[2] + t[2]; }
  return out;
}
export const placed = (o) => toBed(o.local, o);

// Auto-orient one object (sets rot and oriented).
export function autoOrient(o) { o.rot = orient(o.local); o.oriented = true; }
export function resetOrient(o) { o.rot = IDENTITY; o.oriented = false; }

// Does the object fit the bed on its own (footprint and height)?
export function fits(o, bed) { const s = size(o); return s[0] <= bed.x1 - bed.x0 + 1e-6 && s[1] <= bed.y1 - bed.y0 + 1e-6 && s[2] <= bed.h + 1e-6; }

// Shelf-free greedy packing, biggest footprint first, bottom-left, then the group is centred. Sets `at` and `fit` on every object.
export function arrange(objs, machine) {
  const bed = bedOf(machine), rects = [], keep = bed.exclude ? [bed.exclude] : [];
  const hit = (x, y, w, d) => rects.some((r) => x < r.x1 + GAP && x + w > r.x0 - GAP && y < r.y1 + GAP && y + d > r.y0 - GAP)
    || keep.some((r) => x < r.x1 && x + w > r.x0 && y < r.y1 && y + d > r.y0);
  let spill = bed.x1 + GAP;
  const items = objs.map((o) => ({ o, s: size(o) })).sort((a, b) => b.s[0] * b.s[1] - a.s[0] * a.s[1]);
  const done = [];
  for (const { o, s } of items) {
    const [w, d] = s;
    o.fit = fits(o, bed);
    let spot = null;
    if (o.fit) {
      const xs = [bed.x0, ...rects.map((r) => r.x1 + GAP), ...keep.map((r) => r.x1)].sort((a, b) => a - b);
      const ys = [bed.y0, ...rects.map((r) => r.y1 + GAP), ...keep.map((r) => r.y1)].sort((a, b) => a - b);
      search: for (const y of ys) for (const x of xs) if (x + w <= bed.x1 + 1e-6 && y + d <= bed.y1 + 1e-6 && !hit(x, y, w, d)) { spot = [x, y]; break search; }
    }
    if (spot) { rects.push({ x0: spot[0], y0: spot[1], x1: spot[0] + w, y1: spot[1] + d }); done.push({ o, r: rects[rects.length - 1] }); }
    else {  // too big, or the plate is full: park it beside the bed, flagged
      o.fit = false; o.at = [spill + w / 2, bed.y0 + d / 2]; spill += w + GAP;
    }
  }
  if (done.length) {  // centre the group on the bed if that stays clear of the keep-out box
    const x0 = Math.min(...done.map((e) => e.r.x0)), x1 = Math.max(...done.map((e) => e.r.x1)), y0 = Math.min(...done.map((e) => e.r.y0)), y1 = Math.max(...done.map((e) => e.r.y1));
    const dx = (bed.x0 + bed.x1) / 2 - (x0 + x1) / 2, dy = (bed.y0 + bed.y1) / 2 - (y0 + y1) / 2;
    const clear = !keep.some((k) => done.some(({ r }) => r.x0 + dx < k.x1 && r.x1 + dx > k.x0 && r.y0 + dy < k.y1 && r.y1 + dy > k.y0));
    for (const { o, r } of done) o.at = [(r.x0 + r.x1) / 2 + (clear ? dx : 0), (r.y0 + r.y1) / 2 + (clear ? dy : 0)];
  }
}

// Names for the engine and the upload: a repeated file name gets " (2)", " (3)".
export function uniqueNames(objs) {
  const seen = new Map();
  return objs.map((o) => { const k = (seen.get(o.name) || 0) + 1; seen.set(o.name, k); return k > 1 ? o.name.replace(/(\.[^.]*)?$/, ` (${k})$1`) : o.name; });
}
export function uploadName(objs) {
  const base = objs[0].name.replace(/\.stl$/i, '');
  return (objs.length > 1 ? `${base} + ${objs.length - 1} more` : base) + '.stl';
}

// Binary STL of several triangle soups (9 floats per triangle). The normals are computed.
export function writeStl(list) {
  const n = list.reduce((s, p) => s + p.length / 9, 0), buf = new ArrayBuffer(84 + n * 50), dv = new DataView(buf);
  dv.setUint32(80, n, true);
  let at = 84;
  for (const p of list) for (let t = 0; t < p.length; t += 9) {
    const ux = p[t + 3] - p[t], uy = p[t + 4] - p[t + 1], uz = p[t + 5] - p[t + 2], vx = p[t + 6] - p[t], vy = p[t + 7] - p[t + 1], vz = p[t + 8] - p[t + 2];
    const cx = uy * vz - uz * vy, cy = uz * vx - ux * vz, cz = ux * vy - uy * vx, l = Math.hypot(cx, cy, cz) || 1;
    [cx / l, cy / l, cz / l, ...p.subarray(t, t + 9)].forEach((e, i) => dv.setFloat32(at + i * 4, e, true));
    at += 50;
  }
  return buf;
}
