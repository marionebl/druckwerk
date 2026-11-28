// Paint on the model: support enforcers and blockers, seam enforcers and blockers (as in the vendor slicer). A full-screen
// dialog with the model in three.js. The mesh is refined first (see paintcodes.js), so a brush can paint small areas.
// Result: {positions, states} where states.supports and states.seam hold 0 (none), 1 (enforce) or 2 (block) per triangle.
import { h, btn } from '../util.js';
import { KINDS, subdivide, refinePasses } from './paintcodes.js';

let three = null;
const loadThree = () => (three ||= import('../vendor/three.module.min.js'));
const MAX_TRIS = 400000;

const TOOLS = [...KINDS.map((k) => ({ ...k, id_: `${k.id}-${k.state}` })), { id_: 'erase', label: 'Erase', erase: true }];
const GREY = [0.72, 0.74, 0.78];

// local: the model's triangles in its own frame (the paint stays in that frame); rot: how it lies on the plate, for the view only.
// existing: {positions, states} from an earlier session, or null. Resolves with the new paint, or null on cancel.
export async function paintModel(local, existing, rot = [1, 0, 0, 0, 1, 0, 0, 0, 1]) {
  const T = await loadThree();
  let positions = existing?.positions;
  if (!positions) {
    const raw = local;
    if (raw.length / 9 > MAX_TRIS) throw new Error('This model has too many triangles to paint.');
    positions = subdivide(raw, refinePasses(raw));
  }
  const n = positions.length / 9;
  const states = { supports: new Uint8Array(existing?.states.supports || n), seam: new Uint8Array(existing?.states.seam || n) };

  return new Promise((resolve) => {
    const S = { tool: TOOLS[0], fill: false, radius: 3, angle: 30, rotate: false };
    const canvas = h('canvas', { class: 'paint-canvas', tabindex: '0', 'aria-label': 'Model to paint' });
    const dlg = h('dialog', { class: 'paint', 'data-ready': '' });
    const info = h('small', { class: 'muted', id: 'paint-info' });
    // Browsers allow about 16 GL contexts: free the buffers and let go of the context at once.
    const done = (v) => {
      ro.disconnect(); cancelAnimationFrame(raf);
      scene.traverse((o) => { o.geometry?.dispose(); o.material?.dispose(); });
      renderer.dispose(); renderer.forceContextLoss();
      dlg.close(); dlg.remove(); resolve(v);
    };

    // --- scene
    const renderer = new T.WebGLRenderer({ canvas, antialias: true });
    renderer.setClearColor(0x1b1d21, 1);
    const scene = new T.Scene(), camera = new T.PerspectiveCamera(40, 1, 0.5, 2000);
    scene.add(new T.AmbientLight(0xffffff, 1.1)); const sun = new T.DirectionalLight(0xffffff, 1.6); sun.position.set(0.5, 1, 0.8); scene.add(sun);
    // Model Z is up, three.js Y is up; rot lays the model as it sits on the plate. Brush and paint stay in the model's own frame.
    const world = new T.Group(); world.matrixAutoUpdate = false; scene.add(world);
    world.matrix.makeRotationX(-Math.PI / 2).multiply(new T.Matrix4().set(...rot.slice(0, 3), 0, ...rot.slice(3, 6), 0, ...rot.slice(6), 0, 0, 0, 0, 1));
    world.updateMatrixWorld(true);
    const geo = new T.BufferGeometry();
    geo.setAttribute('position', new T.BufferAttribute(positions, 3));
    geo.computeVertexNormals();
    const col = new Float32Array(n * 9);
    geo.setAttribute('color', new T.BufferAttribute(col, 3));
    const mesh = new T.Mesh(geo, new T.MeshLambertMaterial({ vertexColors: true, side: T.DoubleSide }));
    world.add(mesh);
    geo.computeBoundingBox(); geo.computeBoundingSphere();
    const bb = geo.boundingBox, centre = bb.getCenter(new T.Vector3()), rad = geo.boundingSphere.radius;
    const brush = new T.Mesh(new T.SphereGeometry(1, 16, 12), new T.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.35, depthWrite: false }));
    brush.visible = false; world.add(brush);

    // per-triangle centroid and normal for the brush
    const cen = new Float32Array(n * 3), nor = new Float32Array(n * 3);
    for (let t = 0; t < n; t++) {
      const o = t * 9, p = positions;
      for (let a = 0; a < 3; a++) cen[t * 3 + a] = (p[o + a] + p[o + 3 + a] + p[o + 6 + a]) / 3;
      const ux = p[o + 3] - p[o], uy = p[o + 4] - p[o + 1], uz = p[o + 5] - p[o + 2], vx = p[o + 6] - p[o], vy = p[o + 7] - p[o + 1], vz = p[o + 8] - p[o + 2];
      const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx, l = Math.hypot(nx, ny, nz) || 1;
      nor[t * 3] = nx / l; nor[t * 3 + 1] = ny / l; nor[t * 3 + 2] = nz / l;
    }
    let adj = null;  // triangle neighbours through shared edges, built on the first fill
    function adjacency() {
      const key = (o) => `${positions[o]},${positions[o + 1]},${positions[o + 2]}`, edges = new Map(); adj = Array.from({ length: n }, () => []);
      for (let t = 0; t < n; t++) for (let e = 0; e < 3; e++) {
        const a = key(t * 9 + e * 3), b = key(t * 9 + ((e + 1) % 3) * 3), k = a < b ? a + '|' + b : b + '|' + a, o = edges.get(k);
        if (o == null) edges.set(k, t); else { adj[t].push(o); adj[o].push(t); }
      }
    }

    // --- colours
    const paintColour = (t) => {
      for (const k of KINDS) if (states[k.id][t] === k.state) return k.colour;
      return GREY;
    };
    const recolour = (t) => { const c = paintColour(t); for (let v = 0; v < 3; v++) col.set(c, t * 9 + v * 3); };
    for (let t = 0; t < n; t++) recolour(t);
    let dirty = false, raf = 0;
    const frame = () => { raf = requestAnimationFrame(frame); if (dirty) { geo.attributes.color.needsUpdate = true; dirty = false; } if (moved) { place(); moved = false; } renderer.render(scene, camera); };

    function apply(tris) {
      const k = S.tool;
      for (const t of tris) {
        if (k.erase) { states.supports[t] = 0; states.seam[t] = 0; } else states[k.id][t] = k.state;  // one state per kind and triangle
        recolour(t);
      }
      if (S.tool.erase) { /* erases both kinds under the brush */ }
      dirty = true; count();
    }
    function count() {
      const c = (a) => a.reduce((s, v) => s + (v ? 1 : 0), 0);
      info.textContent = `${n.toLocaleString()} triangles · painted: support ${c(states.supports).toLocaleString()}, seam ${c(states.seam).toLocaleString()}`;
      dlg.dataset.supports = c(states.supports); dlg.dataset.seam = c(states.seam);
    }

    // --- camera: orbit around the model's centre
    const view = { az: 0.8, el: 0.5, r: rad * 3.2, tx: 0, ty: 0, tz: 0 };
    let moved = true;
    function place() {
      const c = centre.clone().applyMatrix4(world.matrix);  // the model's centre in three.js axes
      c.x += view.tx; c.y += view.ty; c.z += view.tz;
      camera.position.set(c.x + view.r * Math.cos(view.el) * Math.sin(view.az), c.y + view.r * Math.sin(view.el), c.z + view.r * Math.cos(view.el) * Math.cos(view.az));
      camera.lookAt(c);
    }
    const resize = () => { const w = canvas.clientWidth, hh = canvas.clientHeight; if (!w || !hh) return; renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2)); renderer.setSize(w, hh, false); camera.aspect = w / hh; camera.updateProjectionMatrix(); moved = true; };
    const ro = new ResizeObserver(resize);

    // --- picking and painting
    const ray = new T.Raycaster(), ndc = new T.Vector2();
    function pick(e) {
      const r = canvas.getBoundingClientRect();
      ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      ray.setFromCamera(ndc, camera);
      const hit = ray.intersectObject(mesh, false)[0];
      if (!hit) { brush.visible = false; return null; }
      const p = world.worldToLocal(hit.point.clone());
      brush.position.copy(p); brush.scale.setScalar(S.fill ? 0.001 : S.radius); brush.visible = !S.fill; moved = true;
      return { p, t: hit.faceIndex };
    }
    function stroke(hit) {
      const t0 = hit.t, r2 = S.radius * S.radius, out = [];
      if (S.fill) {
        if (!adj) adjacency();
        const lim = Math.cos((S.angle * Math.PI) / 180), seen = new Set([t0]), q = [t0];
        const dot = (a, b) => nor[a * 3] * nor[b * 3] + nor[a * 3 + 1] * nor[b * 3 + 1] + nor[a * 3 + 2] * nor[b * 3 + 2];
        while (q.length) { const t = q.pop(); out.push(t); for (const u of adj[t]) if (!seen.has(u) && dot(u, t0) >= lim) { seen.add(u); q.push(u); } }
      } else {
        for (let t = 0; t < n; t++) {
          const dx = cen[t * 3] - hit.p.x, dy = cen[t * 3 + 1] - hit.p.y, dz = cen[t * 3 + 2] - hit.p.z;
          if (dx * dx + dy * dy + dz * dz <= r2 && nor[t * 3] * nor[t0 * 3] + nor[t * 3 + 1] * nor[t0 * 3 + 1] + nor[t * 3 + 2] * nor[t0 * 3 + 2] > 0.2) out.push(t);
        }
      }
      apply(out);
    }

    const ptr = new Map(); let mode = null, pinch = 0;
    canvas.addEventListener('pointerdown', (e) => {
      canvas.setPointerCapture(e.pointerId); ptr.set(e.pointerId, [e.clientX, e.clientY]);
      const orbit = S.rotate || e.button === 2 || e.button === 1 || e.shiftKey;
      mode = ptr.size > 1 ? 'two' : orbit ? 'orbit' : 'paint';
      if (ptr.size === 2) { const [a, b] = [...ptr.values()]; pinch = Math.hypot(a[0] - b[0], a[1] - b[1]); }
      if (mode === 'paint') { const hit = pick(e); if (hit) stroke(hit); }
    });
    canvas.addEventListener('pointermove', (e) => {
      const p = ptr.get(e.pointerId);
      if (!p) { if (!S.rotate) pick(e); return; }
      const dx = e.clientX - p[0], dy = e.clientY - p[1];
      ptr.set(e.pointerId, [e.clientX, e.clientY]);
      if (mode === 'two' && ptr.size === 2) {
        const [a, b] = [...ptr.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        if (pinch && d) view.r = Math.min(Math.max((view.r * pinch) / d, rad * 0.4), rad * 12);
        pinch = d; moved = true;
      } else if (mode === 'orbit') { view.az -= dx * 0.008; view.el = Math.min(1.5, Math.max(-1.5, view.el + dy * 0.008)); moved = true; }
      else if (mode === 'paint') { const hit = pick(e); if (hit) stroke(hit); }
    });
    const up = (e) => { ptr.delete(e.pointerId); mode = ptr.size === 1 ? 'orbit' : null; };
    canvas.addEventListener('pointerup', up); canvas.addEventListener('pointercancel', up);
    canvas.addEventListener('pointerleave', () => { if (!ptr.size) { brush.visible = false; moved = true; } });
    canvas.addEventListener('wheel', (e) => { e.preventDefault(); view.r = Math.min(Math.max(view.r * Math.exp(e.deltaY * 0.0012), rad * 0.4), rad * 12); moved = true; }, { passive: false });
    canvas.addEventListener('contextmenu', (e) => e.preventDefault());

    // --- controls
    const tools = h('div', { class: 'chips', id: 'paint-tools' });
    const sw = (c) => h('i', { class: 'sw', style: `background:rgb(${c.map((x) => Math.round(x * 255))})` });
    const drawTools = () => {
      tools.replaceChildren(...TOOLS.map((t) => h('button', { type: 'button', class: 'chip', 'data-tool': t.id_, 'aria-pressed': String(S.tool === t), onclick: () => { S.tool = t; drawTools(); } },
        t.colour ? sw(t.colour) : null, t.label)));
    };
    drawTools();
    const mode2 = h('div', { class: 'chips' },
      h('button', { type: 'button', class: 'chip', id: 'paint-brush', 'aria-pressed': 'true', onclick: () => { S.fill = false; syncMode(); } }, 'Brush'),
      h('button', { type: 'button', class: 'chip', id: 'paint-fill', 'aria-pressed': 'false', onclick: () => { S.fill = true; syncMode(); } }, 'Fill by angle'),
      h('button', { type: 'button', class: 'chip', id: 'paint-rotate', 'aria-pressed': 'false', onclick: () => { S.rotate = !S.rotate; syncMode(); } }, 'Rotate'));
    function syncMode() {
      mode2.children[0].setAttribute('aria-pressed', String(!S.fill)); mode2.children[1].setAttribute('aria-pressed', String(S.fill)); mode2.children[2].setAttribute('aria-pressed', String(S.rotate));
      sizeBox.hidden = S.fill; angleBox.hidden = !S.fill; brush.visible = false; moved = true;
    }
    const slider = (id, label, min, max, step, v, on) => h('label', { class: 'slide' }, label, h('input', { id, type: 'range', min, max, step, value: v, oninput: (e) => on(+e.target.value) }));
    const sizeBox = slider('paint-size', 'Brush radius', 0.5, Math.max(8, Math.round(rad / 2)), 0.5, S.radius, (v) => (S.radius = v));
    const angleBox = slider('paint-angle', 'Max angle between faces', 5, 90, 5, S.angle, (v) => (S.angle = v));
    angleBox.hidden = true;
    const clearAll = () => { states.supports.fill(0); states.seam.fill(0); for (let t = 0; t < n; t++) recolour(t); dirty = true; count(); };

    dlg.append(h('header', null, h('h2', null, 'Paint supports and seam'), h('small', { class: 'muted' }, 'Drag on the model. Right button or Rotate: turn the view. Wheel or pinch: zoom.')),
      h('div', { class: 'paint-body' }, canvas,
        h('div', { class: 'paint-side stack' }, tools, mode2, sizeBox, angleBox, info,
          h('small', { class: 'muted' }, 'Support paint turns supports on. With Support type "manual" only the enforced areas get support.'),
          h('div', { class: 'row', style: 'gap:8px;flex-wrap:wrap' }, btn('Clear all paint', clearAll, 'sm'), btn('Cancel', () => done(null), 'sm'),
            btn('Use this paint', () => done({ positions, states }), 'primary')))));
    dlg.addEventListener('cancel', () => done(null));
    document.body.append(dlg); dlg.showModal();
    ro.observe(canvas); resize(); count(); frame();
    dlg.dataset.ready = '1';
  });
}
