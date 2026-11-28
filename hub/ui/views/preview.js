// 3D G-code preview: orbit/pan/zoom, layer range, feature toggles, exploded objects. three.js is vendored (no CDN: the pages may get COEP).
import { h } from '../util.js';

const FEATURE = { 'Outer wall': '#ff8a3d', 'Inner wall': '#ffd166', 'Sparse infill': '#c86bfa', 'Internal solid infill': '#9b5de5',
  'Top surface': '#ef476f', 'Bottom surface': '#4cc9f0', 'Bridge': '#06d6a0', 'Support': '#8ecae6', 'Support interface': '#219ebc',
  'Floating vertical shell': '#a98467', 'Internal Bridge': '#38b000', 'Custom': '#9aa5b1', 'Brim': '#adb5bd', 'Skirt': '#adb5bd', 'Gap infill': '#f4a261', 'Overhang wall': '#2a9d8f', 'Prime tower': '#6c757d' };
const TRAVEL = 'Travel';
const color = (f) => (f === TRAVEL ? '#6b7683' : FEATURE[f] || '#dddddd');
const BG = 0x15181c;
const HOME = { az: -0.6, el: 0.62 };

let three = null;
const loadThree = () => (three ||= import('../vendor/three.module.min.js'));

// Parse in a worker; fall back to the main thread where module workers are missing.
async function parseText(text) {
  try {
    const w = new Worker(new URL('./gcodeworker.js', import.meta.url), { type: 'module' });
    return await new Promise((res, rej) => {
      w.onmessage = (ev) => { w.terminate(); ev.data.ok ? res(ev.data.result) : rej(new Error(ev.data.error)); };
      w.onerror = (ev) => { w.terminate(); rej(new Error(ev.message || 'worker failed')); };
      w.postMessage(text);
    });
  } catch (e) {
    return (await import('./gcodeparse.js')).parse(text);
  }
}

// One cached parse: the New print page redraws and mounts a fresh preview for the same file.
let cache = { text: null, result: null };
let live = null; // only one WebGL context at a time, browsers cap them

export function gcodePreview() {
  live?.();
  const canvas = h('canvas', { class: 'view3d', role: 'img', 'aria-label': '3D G-code preview', tabindex: 0 });
  const viewport = h('div', { class: 'viewport' }, canvas);
  const resetBtn = h('button', { type: 'button', class: 'chip', onclick: () => reset() }, 'Reset view');
  const travelBtn = h('button', { type: 'button', class: 'chip', 'aria-pressed': 'false', onclick: () => { S.travel = !S.travel; sync(); } }, 'Travel moves');
  const modelBtn = h('button', { type: 'button', class: 'chip', 'aria-pressed': 'true', hidden: true, onclick: () => { S.model = !S.model; sync(); } }, 'Model');
  const oneBtn = h('button', { type: 'button', class: 'chip', 'aria-pressed': 'false', onclick: () => { S.one = !S.one; if (S.one) S.from = S.to; sync(); } }, 'One layer');
  const label = h('small', { class: 'muted', 'aria-live': 'polite' }, 'Loading G-code…');
  const from = h('input', { type: 'range', min: 0, max: 0, value: 0, 'aria-label': 'From layer', oninput: () => { S.from = +from.value; if (S.one || S.from > S.to) S.to = S.from; sync(); } });
  const to = h('input', { type: 'range', min: 0, max: 0, value: 0, 'aria-label': 'To layer', oninput: () => { S.to = +to.value; if (S.one || S.to < S.from) S.from = S.to; sync(); } });
  const fill = h('div', { class: 'fill' });
  const range = h('div', { class: 'range2' }, h('div', { class: 'track' }), fill, from, to);
  const explode = h('input', { type: 'range', min: 0, max: 100, value: 0, disabled: true, 'aria-label': 'Explode', oninput: () => { S.explode = +explode.value / 100; sync(); } });
  const explodeNote = h('small', { class: 'muted' }, '');
  const legend = h('div', { class: 'legend' });
  const node = h('div', { class: 'stack preview3d', style: 'gap:8px' }, viewport,
    h('div', { class: 'row tools' }, resetBtn, travelBtn, modelBtn, oneBtn), label, range,
    h('label', { class: 'explode' }, h('small', null, 'Explode'), explode, explodeNote), legend);

  const S = { from: 0, to: 0, one: false, travel: false, model: true, explode: 0, off: new Set() };
  let T, renderer, scene, camera, world, meshes = [], solids = [], solidData = null, clip = [], R = null, plateC = [0, 0], objC = new Map(), dirty = false, gone = false;
  const view = { az: HOME.az, el: HOME.el, r: 300, tx: 0, ty: 0, tz: 0 };
  let home = { ...view }, ro = null, fit = null, fitted = false;

  const dispose = () => {
    gone = true; live = null; ro?.disconnect();
    for (const m of [...meshes, ...solids]) m.geometry.dispose();
    renderer?.dispose(); renderer?.forceContextLoss();
  };
  live = dispose;

  function draw() {
    if (!renderer || dirty) return;
    dirty = true;
    requestAnimationFrame(() => { dirty = false; if (!gone) renderer.render(scene, camera); });
  }
  function place() {
    const { az, el, r } = view, ce = Math.cos(el);
    camera.position.set(view.tx + r * ce * Math.sin(az), view.ty + r * Math.sin(el), view.tz + r * ce * Math.cos(az));
    camera.lookAt(view.tx, view.ty, view.tz);
    camera.near = Math.max(0.5, r / 100); camera.far = r * 20;
    camera.updateProjectionMatrix();
    draw();
  }
  // Home view: fit the plate and the print, using the narrower of the two field-of-view angles.
  function homeView() {
    const vf = camera.fov * Math.PI / 180, hf = 2 * Math.atan(Math.tan(vf / 2) * camera.aspect);
    Object.assign(view, { az: HOME.az, el: HOME.el, r: (fit.rad * 1.08) / Math.sin(Math.min(vf, hf) / 2), tx: fit.cx, ty: fit.cz * 0.5, tz: fit.cy });
    home = { ...view }; place();
  }
  function reset() { Object.assign(view, home); place(); }
  function resize() {
    const w = viewport.clientWidth, hh = viewport.clientHeight;
    if (!renderer || !w || !hh) return;
    renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
    renderer.setSize(w, hh, false);
    camera.aspect = w / hh;
    if (fit && !fitted) { fitted = true; homeView(); } else camera.updateProjectionMatrix();
    draw();
  }

  // Pointer input: one pointer orbits, shift or right button or two fingers pan, wheel and pinch zoom.
  function controls() {
    const ptr = new Map(); let mode = null, pinch = 0;
    const pan = (dx, dy) => {
      const k = (2 * view.r * Math.tan(camera.fov * Math.PI / 360)) / canvas.clientHeight;
      const { az, el } = view;
      const rx = Math.cos(az), rz = -Math.sin(az), ux = -Math.sin(el) * Math.sin(az), uy = Math.cos(el), uz = -Math.sin(el) * Math.cos(az);
      view.tx += (-dx * rx + dy * ux) * k; view.ty += dy * uy * k; view.tz += (-dx * rz + dy * uz) * k;
    };
    const zoom = (f) => { view.r = Math.min(Math.max(view.r * f, home.r / 20), home.r * 6); };
    canvas.addEventListener('pointerdown', (e) => {
      canvas.setPointerCapture(e.pointerId); ptr.set(e.pointerId, [e.clientX, e.clientY]);
      mode = ptr.size > 1 ? 'two' : (e.button === 2 || e.button === 1 || e.shiftKey) ? 'pan' : 'orbit';
      if (ptr.size === 2) { const [a, b] = [...ptr.values()]; pinch = Math.hypot(a[0] - b[0], a[1] - b[1]); }
    });
    canvas.addEventListener('pointermove', (e) => {
      const p = ptr.get(e.pointerId); if (!p) return;
      const dx = e.clientX - p[0], dy = e.clientY - p[1];
      if (mode === 'two' && ptr.size === 2) {
        const [a, b] = [...ptr.values()], before = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
        ptr.set(e.pointerId, [e.clientX, e.clientY]);
        const [c, d] = [...ptr.values()], d1 = Math.hypot(c[0] - d[0], c[1] - d[1]);
        pan((c[0] + d[0]) / 2 - before[0], (c[1] + d[1]) / 2 - before[1]);
        if (pinch && d1) zoom(pinch / d1);
        pinch = d1;
      } else {
        ptr.set(e.pointerId, [e.clientX, e.clientY]);
        if (mode === 'pan') pan(dx, dy);
        else if (mode === 'orbit') { view.az -= dx * 0.008; view.el = Math.min(1.55, Math.max(-0.1, view.el + dy * 0.008)); }
      }
      place();
    });
    const up = (e) => { ptr.delete(e.pointerId); mode = ptr.size === 1 ? 'orbit' : null; };
    canvas.addEventListener('pointerup', up); canvas.addEventListener('pointercancel', up);
    canvas.addEventListener('wheel', (e) => { e.preventDefault(); zoom(Math.exp(e.deltaY * (e.ctrlKey ? 0.01 : 0.0012))); place(); }, { passive: false });
    canvas.addEventListener('contextmenu', (e) => e.preventDefault());
    canvas.addEventListener('dblclick', reset);
    canvas.addEventListener('keydown', (e) => {
      const k = { ArrowLeft: [0.1, 0], ArrowRight: [-0.1, 0], ArrowUp: [0, -0.1], ArrowDown: [0, 0.1] }[e.key];
      if (k) { view.az += k[0]; view.el = Math.min(1.55, Math.max(-0.1, view.el + k[1])); }
      else if (e.key === '+' || e.key === '=') zoom(0.85); else if (e.key === '-') zoom(1.18); else if (e.key === '0') reset(); else return;
      e.preventDefault(); place();
    });
  }

  // Each object moves along its direction from the plate centre; 100 % puts it 1.6 times as far out.
  function scale() { return Math.min(1, Math.max(0, S.explode)) * 0.6; }
  function sync() {
    if (!R) return;
    const nL = R.layers.length, a = R.layerIndex[S.from], b = R.layerIndex[S.to];
    from.value = S.from; to.value = S.to;
    fill.style.left = (nL > 1 ? (S.from / (nL - 1)) * 100 : 0) + '%'; fill.style.right = (nL > 1 ? (1 - S.to / (nL - 1)) * 100 : 0) + '%';
    const z = (i) => R.layers[i].toFixed(2);
    label.textContent = S.from === S.to ? `Layer ${S.from + 1} of ${nL} · Z ${z(S.from)} mm`
      : `Layers ${S.from + 1} to ${S.to + 1} of ${nL} · Z ${z(S.from)} to ${z(S.to)} mm`;
    oneBtn.setAttribute('aria-pressed', String(S.one)); travelBtn.setAttribute('aria-pressed', String(S.travel)); modelBtn.setAttribute('aria-pressed', String(S.model));
    // The model follows the layer range: clip above the top layer and below the layer under the first one.
    if (clip.length) { clip[0].set(new T.Vector3(0, -1, 0), R.layers[S.to] + 0.001); clip[1].set(new T.Vector3(0, 1, 0), S.from ? -R.layers[S.from - 1] : 1e6); }
    for (const m of solids) {
      const c = m.userData.c; m.visible = S.model;
      m.position.set(c ? (c[0] - plateC[0]) * scale() : 0, c ? (c[1] - plateC[1]) * scale() : 0, 0);
    }
    explodeNote.textContent = R.objects.length > 1 ? Math.round(S.explode * 100) + ' %' : 'One object on this plate';
    const k = scale();
    for (const m of meshes) {
      const g = m.userData.g, ls = g.layerStart, s = ls[a], e = ls[b + 1];
      m.geometry.setDrawRange(s * 2, (e - s) * 2);
      m.visible = e > s && !S.off.has(g.feature) && (g.feature !== TRAVEL || S.travel);
      const c = objC.get(g.object);
      m.position.set(c ? (c[0] - plateC[0]) * k : 0, c ? (c[1] - plateC[1]) * k : 0, 0);
    }
    for (const b2 of legend.children) b2.setAttribute('aria-pressed', String(!S.off.has(b2.dataset.f)));
    draw();
  }

  // The placed STL of each object (bed coordinates, like the G-code), as a translucent shaded mesh.
  function addModel() {
    const mat = new T.MeshLambertMaterial({ color: 0x9fb4c8, transparent: true, opacity: 0.5, side: T.DoubleSide, depthWrite: false, clippingPlanes: clip });
    for (const pos of solidData) {
      const geo = new T.BufferGeometry();
      geo.setAttribute('position', new T.BufferAttribute(pos, 3)); geo.computeVertexNormals();
      geo.computeBoundingBox(); const bx = geo.boundingBox, c = [(bx.min.x + bx.max.x) / 2, (bx.min.y + bx.max.y) / 2];
      let best = null, d2 = Infinity; // follow the nearest G-code object when exploding
      for (const oc of objC.values()) { const d = (oc[0] - c[0]) ** 2 + (oc[1] - c[1]) ** 2; if (d < d2) { d2 = d; best = oc; } }
      const m = new T.Mesh(geo, mat); m.frustumCulled = false; m.renderOrder = 3; m.userData.c = best;
      world.add(m); solids.push(m);
    }
    scene.add(new T.AmbientLight(0xffffff, 1.6)); const sun = new T.DirectionalLight(0xffffff, 2.2); sun.position.set(1, 2, 1.5); scene.add(sun);
    modelBtn.hidden = false;
  }

  async function build(res, t0) {
    R = res; T = await loadThree();
    if (gone) return;
    try { renderer = new T.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true }); }
    catch { label.textContent = 'The 3D preview needs WebGL, which this browser does not offer.'; return; }
    renderer.setClearColor(BG, 1); renderer.localClippingEnabled = true; clip.push(new T.Plane(), new T.Plane());
    scene = new T.Scene(); camera = new T.PerspectiveCamera(40, 1, 1, 1000);
    world = new T.Group(); world.rotation.x = -Math.PI / 2; scene.add(world); // G-code Z is up, three.js Y is up

    const bb = R.bbox, zmax = Math.max(bb[5], 1);
    const plate = R.plate || [[bb[0] - 10, bb[1] - 10], [bb[2] + 10, bb[1] - 10], [bb[2] + 10, bb[3] + 10], [bb[0] - 10, bb[3] + 10]];
    const px = plate.map((p) => p[0]), py = plate.map((p) => p[1]);
    plateC = [(Math.min(...px) + Math.max(...px)) / 2, (Math.min(...py) + Math.max(...py)) / 2];
    const shape = new T.Shape(plate.map((p) => new T.Vector2(p[0], p[1])));
    const pm = new T.Mesh(new T.ShapeGeometry(shape), new T.MeshBasicMaterial({ color: 0x232830, depthWrite: false }));
    pm.position.z = -0.05; world.add(pm);
    const outline = new T.LineLoop(new T.BufferGeometry().setFromPoints(plate.map((p) => new T.Vector3(p[0], p[1], -0.04))), new T.LineBasicMaterial({ color: 0x5d6978 }));
    world.add(outline);
    const grid = [];
    for (let gx = Math.ceil(Math.min(...px) / 50) * 50; gx < Math.max(...px); gx += 50) grid.push(new T.Vector3(gx, Math.min(...py), -0.04), new T.Vector3(gx, Math.max(...py), -0.04));
    for (let gy = Math.ceil(Math.min(...py) / 50) * 50; gy < Math.max(...py); gy += 50) grid.push(new T.Vector3(Math.min(...px), gy, -0.04), new T.Vector3(Math.max(...px), gy, -0.04));
    world.add(new T.LineSegments(new T.BufferGeometry().setFromPoints(grid), new T.LineBasicMaterial({ color: 0x2f3640 })));

    // Darker low layers, brighter top: a depth cue that needs no extra vertex data.
    const mats = new Map();
    const mat = (f) => {
      if (!mats.has(f)) {
        const n = parseInt(color(f).slice(1), 16); // raw sRGB: the shader writes straight to the canvas
        mats.set(f, new T.ShaderMaterial({
          uniforms: { uC: { value: new T.Vector3(((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255) }, uZ: { value: zmax }, uA: { value: f === TRAVEL ? 0.45 : 1 } },
          vertexShader: 'varying float vH; uniform float uZ; void main(){ vH = position.z / uZ; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
          fragmentShader: 'uniform vec3 uC; uniform float uA; varying float vH; void main(){ gl_FragColor = vec4(uC * (0.62 + 0.38 * vH), uA); }',
          transparent: f === TRAVEL, depthWrite: f !== TRAVEL,
        }));
      }
      return mats.get(f);
    };
    for (const g of R.groups) {
      const geo = new T.BufferGeometry();
      geo.setAttribute('position', new T.BufferAttribute(g.positions, 3));
      const m = new T.LineSegments(geo, mat(g.feature));
      m.frustumCulled = false; m.userData.g = g; m.renderOrder = g.feature === TRAVEL ? 2 : 1;
      world.add(m); meshes.push(m);
    }
    for (const o of R.objects) objC.set(o.id, [(o.box[0] + o.box[2]) / 2, (o.box[1] + o.box[3]) / 2]);

    fit = { cx: plateC[0], cy: -plateC[1], cz: zmax / 2, rad: Math.hypot(Math.max(...px) - Math.min(...px), Math.max(...py) - Math.min(...py), zmax) / 2 };
    resize();

    const nL = R.layers.length;
    from.max = to.max = nL - 1;
    S.from = 0; S.to = nL - 1;
    const used = [...new Set(R.groups.map((g) => g.feature))].filter((f) => f !== TRAVEL);
    const order = Object.keys(FEATURE);
    used.sort((p, q) => (order.indexOf(p) + 1 || 99) - (order.indexOf(q) + 1 || 99));
    legend.replaceChildren(...used.map((f) => h('button', { type: 'button', class: 'key', 'data-f': f, 'aria-pressed': 'true',
      onclick: () => { S.off.has(f) ? S.off.delete(f) : S.off.add(f); sync(); } }, h('i', { style: `background:${color(f)}` }), f)));
    explode.disabled = R.objects.length < 2;
    if (solidData) addModel();
    controls();
    ro = new ResizeObserver(() => { if (!node.isConnected) dispose(); else resize(); });
    ro.observe(viewport);
    place(); sync();
    renderer.render(scene, camera); renderer.getContext().finish();
    node.dataset.segments = R.segments; node.dataset.objects = R.objects.length; node.dataset.ready = '1';
    node.dataset.buildMs = Math.round(performance.now() - t0 - Number(node.dataset.parseMs));
  }

  node.load = async (text) => {
    try {
      const t0 = performance.now();
      let res = cache.text === text ? cache.result : null;
      if (!res) { res = await parseText(text); cache = { text, result: res }; }
      node.dataset.parseMs = Math.round(performance.now() - t0);
      if (gone) return;
      if (!res.layers.length) { label.textContent = 'The G-code has no layers to show.'; return; }
      await build(res, t0);
    } catch (e) { label.textContent = 'The 3D preview failed: ' + e.message; }
  };
  node.setModel = (list) => { solidData = list; if (R && !solids.length && renderer) { addModel(); sync(); } };
  node.fail = (msg) => { label.textContent = msg; };
  node.dispose = dispose;
  return node;
}
