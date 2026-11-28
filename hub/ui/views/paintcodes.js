// Pure helpers for painting on an STL: refine the mesh so a brush can paint finely, and turn the painted triangles into the
// engine's ObjectPaint (protocol v2.2). Every triangle is one leaf (whole-triangle states), so a code is one hex digit:
// "4" is state 1, "8" is state 2.

export const KINDS = [
  { id: 'supports', state: 1, key: 'enforce', label: 'Support enforcer', colour: [0.2, 0.75, 0.3] },
  { id: 'supports', state: 2, key: 'block', label: 'Support blocker', colour: [0.9, 0.25, 0.25] },
  { id: 'seam', state: 1, key: 'enforce', label: 'Seam enforcer', colour: [0.25, 0.5, 0.95] },
  { id: 'seam', state: 2, key: 'block', label: 'Seam blocker', colour: [0.95, 0.6, 0.15] },
];

const LEAF = { 1: '4', 2: '8' };

// Splits every triangle of a soup (9 floats each) into four, `passes` times. All triangles split alike, so the mesh stays
// watertight (no T-junctions) and the shape does not change. The midpoint of an edge is the same float from both sides.
export function subdivide(pos, passes) {
  let cur = pos;
  for (let p = 0; p < passes; p++) {
    const n = cur.length / 9, out = new Float32Array(n * 36);
    for (let t = 0; t < n; t++) {
      const o = t * 9, v = (i) => [cur[o + i * 3], cur[o + i * 3 + 1], cur[o + i * 3 + 2]];
      const A = v(0), B = v(1), C = v(2), mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2];
      const ab = mid(A, B), bc = mid(B, C), ca = mid(C, A);
      out.set([...A, ...ab, ...ca, ...ab, ...B, ...bc, ...bc, ...C, ...ca, ...ab, ...bc, ...ca], t * 36);  // same winding as the parent
    }
    cur = out;
  }
  return cur;
}

// Passes needed so the longest edges of 90 % of the triangles are about `target` mm, without passing `maxTris`.
export function refinePasses(pos, target = 1.5, maxTris = 150000) {
  const n = pos.length / 9, longest = new Float32Array(n);
  for (let t = 0; t < n; t++) {
    const d = (i, j) => Math.hypot(pos[t * 9 + i * 3] - pos[t * 9 + j * 3], pos[t * 9 + i * 3 + 1] - pos[t * 9 + j * 3 + 1], pos[t * 9 + i * 3 + 2] - pos[t * 9 + j * 3 + 2]);
    longest[t] = Math.max(d(0, 1), d(1, 2), d(2, 0));
  }
  const l90 = [...longest].sort((a, b) => a - b)[Math.floor(n * 0.9)] || target;
  let passes = Math.max(0, Math.ceil(Math.log2(l90 / target)));
  while (passes > 0 && n * 4 ** passes > maxTris) passes--;
  return passes;
}

// states: {supports?, seam?, fuzzy?} each a Uint8Array with a state per triangle. Returns the engine's `paint`, or null.
export function toObjectPaint(states) {
  const paint = {};
  for (const [kind, st] of Object.entries(states || {})) {
    const tri = [], ends = [];
    let codes = '';
    for (let i = 0; i < st.length; i++) if (LEAF[st[i]]) { tri.push(i); codes += LEAF[st[i]]; ends.push(codes.length); }
    if (tri.length) paint[kind] = { triangles: Uint32Array.from(tri), codes, ends: Uint32Array.from(ends) };
  }
  return Object.keys(paint).length ? paint : null;
}

// Fuzzy skin paint is left out: the engine (v0.6.0-m1.9) gives the same G-code with fuzzy_skin "none" (painted only) as without paint.
export const paintedCount = (states) => Object.fromEntries(Object.entries(states || {}).map(([k, s]) => [k, s.reduce((n, v) => n + (v ? 1 : 0), 0)]).filter(([, n]) => n));
