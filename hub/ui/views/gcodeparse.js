// G-code to merged line buffers: one buffer per (feature, object), segments in layer order so a layer range is one draw range.
// Pure functions, no DOM: runs in a Web Worker (gcodeworker.js) and in node for timing.
const ARC_SAG = 0.02; // mm the chords of an arc may stray from it (about a pixel at the closest zoom)

class Group {
  constructor(feature, object) { this.feature = feature; this.object = object; this.buf = new Float32Array(3 << 14); this.n = 0; this.layerStart = []; this.last = -1; }
  mark(layer) { while (this.last < layer) { this.layerStart[++this.last] = this.n / 6; } }
  add(layer, x0, y0, z0, x1, y1, z1) {
    this.mark(layer);
    if (this.n + 6 > this.buf.length) { const b = new Float32Array(this.buf.length * 2); b.set(this.buf); this.buf = b; }
    const b = this.buf, n = this.n;
    b[n] = x0; b[n + 1] = y0; b[n + 2] = z0; b[n + 3] = x1; b[n + 4] = y1; b[n + 5] = z1;
    this.n = n + 6;
  }
}

// Reads a G-code number at text[i..]; returns the end index and leaves the value in num.
let num = 0;
function readNum(t, i, n) {
  let neg = false, c = t.charCodeAt(i);
  if (c === 45) { neg = true; c = t.charCodeAt(++i); } else if (c === 43) c = t.charCodeAt(++i);
  let v = 0;
  while (c >= 48 && c <= 57) { v = v * 10 + (c - 48); c = t.charCodeAt(++i); }
  if (c === 46) {
    let f = 0.1; c = t.charCodeAt(++i);
    while (c >= 48 && c <= 57) { v += (c - 48) * f; f /= 10; c = t.charCodeAt(++i); }
  }
  num = neg ? -v : v;
  return i;
}

export function parse(text) {
  const n = text.length, groups = new Map(), layerZ = [], objIds = new Map(), objBox = [];
  let layer = -1, x = 0, y = 0, z = 0, e = 0, feat = '', relE = true, obj = -1, i = 0;
  const bbox = [1e9, 1e9, -1e9, -1e9, 1e9, -1e9];
  let segments = 0;
  // Orca writes both the label marker and a named one for the same object; use the label marker alone when it exists.
  const named = !text.includes('; start printing object, unique label id: ');

  const group = (f, o) => {
    const k = f + '\u0000' + o;
    let g = groups.get(k);
    if (!g) groups.set(k, (g = new Group(f, o)));
    return g;
  };
  const objectOf = (key) => {
    let o = objIds.get(key);
    if (o === undefined) { o = objIds.size; objIds.set(key, o); objBox[o] = [1e9, 1e9, -1e9, -1e9]; }
    return o;
  };
  const seg = (f, x0, y0, z0, x1, y1, z1, extrusion) => {
    group(f, obj).add(layer, x0, y0, z0, x1, y1, z1);
    if (!extrusion) return;
    segments++;
    if (x0 < bbox[0]) bbox[0] = x0; if (x1 < bbox[0]) bbox[0] = x1;
    if (y0 < bbox[1]) bbox[1] = y0; if (y1 < bbox[1]) bbox[1] = y1;
    if (x0 > bbox[2]) bbox[2] = x0; if (x1 > bbox[2]) bbox[2] = x1;
    if (y0 > bbox[3]) bbox[3] = y0; if (y1 > bbox[3]) bbox[3] = y1;
    if (z1 < bbox[4]) bbox[4] = z1; if (z1 > bbox[5]) bbox[5] = z1;
    if (obj >= 0) {
      const b = objBox[obj];
      b[0] = Math.min(b[0], x0, x1); b[1] = Math.min(b[1], y0, y1); b[2] = Math.max(b[2], x0, x1); b[3] = Math.max(b[3], y0, y1);
    }
  };

  while (i < n) {
    let end = text.indexOf('\n', i);
    if (end < 0) end = n;
    const c = text.charCodeAt(i);
    if (c === 59) { // ';'
      if (text.startsWith('; FEATURE: ', i)) feat = text.slice(i + 11, end).trim();
      else if (text.startsWith(';TYPE:', i)) feat = text.slice(i + 6, end).trim();
      else if (text.startsWith('; CHANGE_LAYER', i) || text.startsWith(';LAYER_CHANGE', i) || text.startsWith(';LAYER:', i)) { layerZ[++layer] = z; }
      else if (text.startsWith('; Z_HEIGHT: ', i) && layer >= 0) layerZ[layer] = parseFloat(text.slice(i + 12, end));
      else if (text.startsWith('; start printing object, unique label id: ', i)) obj = objectOf(text.slice(i + 42, end).trim());
      else if (named && text.startsWith('; printing object ', i) && text.indexOf(' id:', i) > 0 && text.indexOf(' id:', i) < end) obj = objectOf(text.slice(text.indexOf(' id:', i) + 4, end).split(' ')[0]);
      else if (text.startsWith('; stop printing object', i)) obj = -1;
    } else if (c === 71) { // 'G'
      let j = readNum(text, i + 1, end);
      const g = num;
      if (g <= 3 || g === 92) {
        let X = NaN, Y = NaN, Z = NaN, E = NaN, I = NaN, J = NaN;
        while (j < end) {
          const l = text.charCodeAt(j);
          if (l === 59) break;
          if (l === 32 || l === 9 || l === 13) { j++; continue; }
          const k = readNum(text, j + 1, end);
          if (k === j + 1) { j = k + 1; continue; }
          j = k;
          if (l === 88) X = num; else if (l === 89) Y = num; else if (l === 90) Z = num;
          else if (l === 69) E = num; else if (l === 73) I = num; else if (l === 74) J = num;
        }
        if (g === 92) { if (E === E) e = E; }
        else {
          const nx = X === X ? X : x, ny = Y === Y ? Y : y, pz = z;
          if (Z === Z) z = Z;
          let ext = false;
          if (E === E) { ext = relE ? E > 0 : E > e; if (!relE) e = E; }
          if (layer >= 0) {
            const arc = (g === 2 || g === 3) && I === I, moved = arc || nx !== x || ny !== y; // a full-circle arc ends where it began
            if (ext && moved) {
              const f = feat || 'Other';
              if (arc) {
                const cx = x + (I || 0), cy = y + (J || 0), r = Math.hypot(x - cx, y - cy);
                let a0 = Math.atan2(y - cy, x - cx), a1 = Math.atan2(ny - cy, nx - cx);
                if (g === 3 && a1 <= a0) a1 += 2 * Math.PI;
                if (g === 2 && a1 >= a0) a1 -= 2 * Math.PI;
                const da = r > ARC_SAG ? 2 * Math.acos(1 - ARC_SAG / r) : Math.PI / 2;
                const steps = Math.max(2, Math.ceil(Math.abs(a1 - a0) / da));
                let px = x, py = y;
                for (let s = 1; s <= steps; s++) {
                  const a = a0 + (a1 - a0) * s / steps, qx = cx + r * Math.cos(a), qy = cy + r * Math.sin(a);
                  seg(f, px, py, z, qx, qy, z, true); px = qx; py = qy;
                }
              } else seg(f, x, y, z, nx, ny, z, true);
            } else if (!ext && moved) seg('Travel', x, y, pz, nx, ny, z, false);
          }
          x = nx; y = ny;
        }
      }
    } else if (c === 77 && end - i >= 3) { // 'M'
      if (text.startsWith('M82', i)) relE = false; else if (text.startsWith('M83', i)) relE = true;
    } else if (c === 69 && text.startsWith('EXCLUDE_OBJECT_START NAME=', i)) obj = objectOf(text.slice(i + 26, end).trim());
    else if (c === 69 && text.startsWith('EXCLUDE_OBJECT_END', i)) obj = -1;
    i = end + 1;
  }

  // Keep layers that have an extrusion; the rest (travel-only) would be empty steps for the slider.
  const nl = layer + 1, has = new Uint8Array(nl);
  for (const g of groups.values()) {
    g.mark(nl);
    if (g.feature === 'Travel') continue;
    for (let l = 0; l < nl; l++) if (g.layerStart[l + 1] > g.layerStart[l]) has[l] = 1;
  }
  const layers = [], layerIndex = [];
  for (let l = 0; l < nl; l++) if (has[l]) { layers.push(layerZ[l]); layerIndex.push(l); }

  const list = [...groups.values()].map((g) => ({ feature: g.feature, object: g.object, positions: g.buf.slice(0, g.n), layerStart: Uint32Array.from(g.layerStart) }));
  const objects = [...objIds.values()].filter((o) => objBox[o][2] > -1e8).map((o) => ({ id: o, box: objBox[o] }));
  return { layers, layerIndex, groups: list, objects, bbox, segments, plate: plateOf(text) };
}

// Bambu Studio and Orca put printable_area in the header, older ones in the config at the end; Prusa writes bed_shape.
function plateOf(text) {
  const re = /^; (?:printable_area|bed_shape) = (.+)$/m;
  const m = re.exec(text.slice(0, 200000)) || re.exec(text.slice(-80000));
  if (!m) return null;
  const pts = m[1].trim().split(',').map((p) => p.split('x').map(Number));
  return pts.length >= 3 && pts.every((p) => p.length === 2 && p.every(Number.isFinite)) ? pts : null;
}
