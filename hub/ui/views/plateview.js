// The plate of New print in 3D (three.js): the bed outline and every object, the selected ones in the accent colour.
// Click selects, Shift/Ctrl/Cmd-click toggles, a click on the empty bed clears. Drag orbits, wheel and pinch zoom.
import { h } from '../util.js';
import { toBed, bedOf } from './plate.js';

let three = null;
const loadThree = () => (three ||= import('../vendor/three.module.min.js'));
const cssColour = (name, T, fallback) => { try { return new T.Color(getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback); } catch { return new T.Color(fallback); } };

// onPick(id | null, additive): the page changes the selection and calls update() again.
export function plateView({ onPick }) {
  const canvas = h('canvas', { class: 'plate-canvas', tabindex: '0', role: 'img', 'aria-label': 'Plate with the models. Select them in the list below too.' });
  const hint = h('div', { class: 'plate-hint' }, 'Loading the 3D view…');
  const node = h('div', { class: 'plate3d', id: 'plate3d' }, canvas, hint);
  let T, renderer, scene, camera, world, bedGroup, ro, raf = 0, gone = false, last = null;
  const view = { az: 0.35, el: 0.95, r: 400, tx: 128, ty: 0, tz: -128 };
  const meshes = new Map();  // id -> THREE.Mesh
  let dirty = true;

  const ready = loadThree().then((t) => {
    if (gone) return;
    T = t;
    renderer = new T.WebGLRenderer({ canvas, antialias: true });
    renderer.setClearColor(0x15181c, 1);
    scene = new T.Scene(); camera = new T.PerspectiveCamera(40, 1, 1, 4000);
    scene.add(new T.AmbientLight(0xffffff, 1.0)); const sun = new T.DirectionalLight(0xffffff, 1.7); sun.position.set(0.4, 1, 0.7); scene.add(sun);
    world = new T.Group(); world.rotation.x = -Math.PI / 2; scene.add(world);  // bed Z is up, three.js Y is up
    bedGroup = new T.Group(); world.add(bedGroup);
    ro = new ResizeObserver(resize); ro.observe(canvas);
    hint.hidden = true;
    node.dataset.ready = '1';
    const frame = () => { raf = requestAnimationFrame(frame); if (!node.isConnected) return dispose(); if (dirty && canvas.width) { dirty = false; renderer.render(scene, camera); } };
    resize(); frame();
    if (last) update(last);
  });

  function resize() {
    const w = canvas.clientWidth, hh = canvas.clientHeight;
    if (!w || !hh) return;
    renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2)); renderer.setSize(w, hh, false);
    camera.aspect = w / hh; camera.updateProjectionMatrix(); place();
  }
  function place() {
    if (!camera) return;
    const { az, el, r } = view, ce = Math.cos(el);
    camera.position.set(view.tx + r * ce * Math.sin(az), view.ty + r * Math.sin(el), view.tz + r * ce * Math.cos(az));
    camera.lookAt(view.tx, view.ty, view.tz); dirty = true;
  }
  // Fit the camera to the bed once per bed size.
  function fit(bed) {
    const w = bed.x1 - bed.x0, d = bed.y1 - bed.y0, key = `${bed.x0},${bed.y0},${w},${d}`;
    if (node.dataset.bed === key) return;
    node.dataset.bed = key;
    Object.assign(view, { tx: (bed.x0 + bed.x1) / 2, ty: 0, tz: -(bed.y0 + bed.y1) / 2, r: Math.max(w, d) * 1.6 });
    place();
  }

  // state: {objects, sel (Set of ids), machine}
  function update(state) {
    last = state;
    if (!T) return;
    const bed = bedOf(state.machine), accent = cssColour('--accent', T, '#008c3a'), err = cssColour('--err', T, '#c0332b');
    const bedKey = JSON.stringify([bed.poly, bed.exclusion]);
    if (bedGroup.userData.key !== bedKey) {
      bedGroup.userData.key = bedKey;
      for (const c of [...bedGroup.children]) { c.geometry?.dispose(); bedGroup.remove(c); }
      const shape = new T.Shape(bed.poly.map(([x, y]) => new T.Vector2(x, y)));
      const plane = new T.Mesh(new T.ShapeGeometry(shape), new T.MeshBasicMaterial({ color: 0x2b2f36, side: T.DoubleSide }));
      plane.position.z = -0.05; bedGroup.add(plane);
      const grid = [], x0 = Math.ceil(bed.x0 / 10) * 10, y0 = Math.ceil(bed.y0 / 10) * 10;
      for (let x = x0; x <= bed.x1; x += 10) grid.push(x, bed.y0, 0, x, bed.y1, 0);
      for (let y = y0; y <= bed.y1; y += 10) grid.push(bed.x0, y, 0, bed.x1, y, 0);
      const gg = new T.BufferGeometry(); gg.setAttribute('position', new T.Float32BufferAttribute(grid, 3));
      bedGroup.add(new T.LineSegments(gg, new T.LineBasicMaterial({ color: 0x3a3f48 })));
      const og = new T.BufferGeometry(); og.setAttribute('position', new T.Float32BufferAttribute(bed.poly.flatMap(([x, y]) => [x, y, 0.05]), 3));
      bedGroup.add(new T.LineLoop(og, new T.LineBasicMaterial({ color: 0x8b95a3 })));
      if (bed.exclude) {
        const k = bed.exclude, kg = new T.PlaneGeometry(k.x1 - k.x0, k.y1 - k.y0), km = new T.Mesh(kg, new T.MeshBasicMaterial({ color: 0xc0332b, transparent: true, opacity: 0.35 }));
        km.position.set((k.x0 + k.x1) / 2, (k.y0 + k.y1) / 2, 0.02); bedGroup.add(km);
      }
    }
    fit(bed);
    const ids = new Set(state.objects.map((o) => o.id));
    for (const [id, m] of meshes) if (!ids.has(id)) { m.geometry.dispose(); m.material.dispose(); world.remove(m); meshes.delete(id); }
    for (const o of state.objects) {
      let m = meshes.get(o.id);
      if (!m) { m = new T.Mesh(new T.BufferGeometry(), new T.MeshLambertMaterial({ side: T.DoubleSide })); m.userData.id = o.id; world.add(m); meshes.set(o.id, m); }
      if (m.userData.rot !== o.rot) {  // geometry at the origin; `at` moves the mesh, so Arrange does not rebuild it
        const pos = toBed(o.local, { ...o, at: [0, 0] });
        m.geometry.dispose(); m.geometry = new T.BufferGeometry(); m.geometry.setAttribute('position', new T.BufferAttribute(pos, 3)); m.geometry.computeVertexNormals();
        m.geometry.computeBoundingSphere(); m.userData.rot = o.rot;
      }
      m.position.set(o.at[0], o.at[1], 0);
      const on = state.sel.has(o.id);
      m.material.color.copy(on ? accent : o.fit === false ? err : new T.Color(0xb8bec8));
      m.material.emissive.copy(on ? accent : new T.Color(0)).multiplyScalar(on ? 0.12 : 0);
    }
    node.dataset.objects = String(state.objects.length); node.dataset.selected = [...state.sel].join(',');
    dirty = true;
  }

  // --- pointer: a short press picks, a drag orbits, two fingers and the wheel zoom
  const ptr = new Map(); let moved = 0, pinch = 0;
  const ray = () => (T ? new T.Raycaster() : null);
  function pickAt(e) {
    const r = canvas.getBoundingClientRect(), rc = ray();
    if (!rc) return;
    rc.setFromCamera(new T.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), camera);
    world.updateMatrixWorld(true);
    const hit = rc.intersectObjects([...meshes.values()], false)[0];
    onPick(hit ? hit.object.userData.id : null, e.shiftKey || e.ctrlKey || e.metaKey);
  }
  canvas.addEventListener('pointerdown', (e) => {
    canvas.setPointerCapture(e.pointerId); ptr.set(e.pointerId, [e.clientX, e.clientY]);
    if (ptr.size === 1) moved = 0;
    if (ptr.size === 2) { const [a, b] = [...ptr.values()]; pinch = Math.hypot(a[0] - b[0], a[1] - b[1]); moved = 99; }
  });
  canvas.addEventListener('pointermove', (e) => {
    const p = ptr.get(e.pointerId); if (!p) return;
    const dx = e.clientX - p[0], dy = e.clientY - p[1]; ptr.set(e.pointerId, [e.clientX, e.clientY]);
    if (ptr.size === 2) {
      const [a, b] = [...ptr.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      if (pinch && d) view.r = Math.min(Math.max((view.r * pinch) / d, 60), 1500);
      pinch = d; place();
    } else { moved += Math.abs(dx) + Math.abs(dy); if (moved > 5) { view.az -= dx * 0.008; view.el = Math.min(1.5, Math.max(0.05, view.el + dy * 0.008)); place(); } }
  });
  const up = (e) => { const was = ptr.size === 1 && ptr.has(e.pointerId); ptr.delete(e.pointerId); if (was && moved <= 5 && e.type === 'pointerup') pickAt(e); };
  canvas.addEventListener('pointerup', up); canvas.addEventListener('pointercancel', up);
  canvas.addEventListener('wheel', (e) => { e.preventDefault(); view.r = Math.min(Math.max(view.r * Math.exp(e.deltaY * 0.0012), 60), 1500); place(); }, { passive: false });

  function dispose() {
    gone = true; cancelAnimationFrame(raf); ro?.disconnect();
    for (const m of meshes.values()) { m.geometry.dispose(); m.material.dispose(); }
    renderer?.dispose(); renderer?.forceContextLoss();
  }
  return { node, update, dispose, ready };
}
