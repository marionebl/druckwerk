// New print: put one or more STL files on the plate, pick printer + filament, set per-object settings, slice the chosen
// variants side by side, choose, queue.
import { api } from '../api.js';
import { h, clear, icon, btn, pill, field, swatch, empty, attempt, toast, fail, durSec, money, hex } from '../util.js';
import { gcodePreview } from './preview.js';
import { warm, sliceVariant, loadPresets, parseStl } from './browserslice.js';
import { plateView } from './plateview.js';
import { IDENTITY, autoOrient, resetOrient, arrange, size, bedOf, uniqueNames, uploadName, writeStl, placed } from './plate.js';
import { promote } from './promote.js';
import { objectCard } from './objectsettings.js';
import { paintModel } from './paint.js';
import { paintedCount } from './paintcodes.js';
import { sliceProgress } from './progress.js';
import { nozzleRow, chosenNozzle, same as sameNozzle } from './nozzlerow.js';

const QUALITIES = [['good', 'Good', 'Fastest, visible layers'], ['better', 'Better', 'Balanced'], ['best', 'Best', 'Finest detail, slowest']];
const BEDS = ['Textured PEI Plate', 'Cool Plate', 'Engineering Plate', 'High Temp Plate', 'Supertack Plate'];
const TRI = { on: true, off: false, auto: 'auto' };
const BED_TYPES = [['auto', 'Auto'], ['cool_plate', 'Cool Plate'], ['eng_plate', 'Engineering Plate'], ['hot_plate', 'High Temp Plate'], ['textured_plate', 'Textured PEI Plate'], ['supertack_plate', 'Supertack Plate'], ['3d_effect_plate', '3D Effect Plate']];
const ACCEPT = /\.stl$/i;
const VARIANTS_KEY = 'dw-variants';  // the chosen variants, remembered in this browser
const ALL = QUALITIES.map((q) => q[0]);
const wanted = () => {
  try { const v = JSON.parse(localStorage.getItem(VARIANTS_KEY)), ok = ALL.filter((q) => v?.includes(q)); if (ok.length) return new Set(ok); } catch { /* no storage: all three */ }
  return new Set(ALL);
};
const sliceLabel = (want, busy) => {
  const n = QUALITIES.filter(([q]) => want.has(q)).map((q) => q[1]);
  if (busy) return `Slicing ${['', 'one', 'two', 'three'][n.length]} variant${n.length > 1 ? 's' : ''}…`;
  return n.length === 3 ? 'Slice good, better and best' : 'Slice ' + (n.length === 1 ? n[0] : `${n[0]} and ${n[1]}`);
};
const dim = (v) => (Math.round(v * 10) / 10).toString();

export async function mount(ctx, root) {
  if (promote()) return;  // the top window is moving to this page, where threads work
  let libErr = '';
  const [printers, spools] = await Promise.all([api.printers(), api.spools().catch((e) => { libErr = e.message; return []; })]);
  // tray: "unit:slot" of a loaded AMS tray; spool: id of a library spool. At most one of them is set.
  // objects: the plate (see plate.js); sel: ids of the selected ones; variants: the slices of the last run; want: the variants to slice.
  const S = { objects: [], sel: new Set(), loading: [], machine: null, machineModel: null, autoOrient: true, want: wanted(), target: null, tray: null, spool: null, bed: BEDS[0], variants: null, pick: null,
    options: { timelapse: false, bed_leveling: true, use_ams: true, flow_cali: true, vibration_cali: true, layer_inspect: false, nozzle_offset_cali: false, bed_type: 'auto', ext_change_assist: false },
    name: '', busy: false, engine: null, filName: null, filKey: null };
  let seq = 0;
  const box = h('div', { class: 'stack' });
  const view = plateView({ onPick: (id, add) => select(id, add) });
  root.append(h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'New print'), h('div', { class: 'sub' }, 'Upload, compare three slices, then queue'))), box);

  const models = [...new Set(printers.map((p) => p.model))];
  const refPrinter = () => {
    if (!S.target) return null;
    if (S.target.startsWith('any:')) { const m = S.target.slice(4); return printers.find((p) => p.model === m && p.online) || printers.find((p) => p.model === m); }
    return printers.find((p) => p.serial === S.target);
  };
  const trays = () => (refPrinter()?.ams || []).flatMap((u) => u.trays.filter((t) => t.type).map((t) => ({ ...t, unit: u.unit })));
  const trayKey = (t) => `${t.unit}:${t.slot}`;
  const spoolOf = (t) => spools.find((s) => s.id === t.spool_id);
  const spoolById = (id) => spools.find((s) => s.id === id);
  // Spools of the printer's cloud account that sit in no printer; those in the AMS are listed as trays.
  const library = () => { const acc = refPrinter()?.account; return spools.filter((s) => !s.printer && (!acc || s.account === acc)); };
  // What the slicer is told: the AMS tray, or the spool's type, brand and name for the matching preset.
  const filChoice = () => {
    if (S.tray) return { tray: S.tray };
    const s = S.spool && spoolById(S.spool);
    return s ? { type: s.type, brand: s.brand, name: s.name, colour: s.colour, spool: s.id } : null;
  };
  // The first loaded tray is the default; the file carries no filament to match it against.
  function defaultFil() {
    const t = !S.tray && !S.spool && S.target && trays()[0];
    if (t) S.tray = trayKey(t);
  }
  const pickFil = (tray, spool) => () => { S.tray = tray; S.spool = spool; S.variants = null; draw(); };

  function startEngine() {
    const e = S.engine = { state: 'loading' };
    warm().then((x) => Object.assign(e, x, { state: 'ready' }), (x) => Object.assign(e, { state: 'error', error: x.message }))
      .then(() => { if (box.isConnected) draw(); else e.worker?.terminate(); });
  }
  function dropEngine() { S.engine?.worker?.terminate(); S.engine = null; }
  // Leaving the page: the engine worker holds the engine's memory, the views hold GL contexts.
  let shown = null;  // the G-code preview of the chosen variant: {id, node}
  ctx.onLeave(() => { dropEngine(); view.dispose(); shown?.node.dispose(); });

  const invalidate = () => { S.variants = null; S.pick = null; };
  const picked = () => S.objects.filter((o) => S.sel.has(o.id));
  const fitting = () => S.objects.every((o) => o.fit !== false);

  // Add models to the plate: parse, auto-orient (if on), arrange the whole plate, select what came in.
  async function addFiles(files, { orient = S.autoOrient } = {}) {
    const list = [...files].filter((f) => ACCEPT.test(f.name));
    if (list.length < files.length) toast('Upload STL files.', 'error');
    if (!list.length) return;
    S.loading.push(...list.map((f) => f.name)); draw();
    const added = [];
    for (const f of list) {
      try {
        const o = { id: 'o' + ++seq, name: f.name, file: f, local: parseStl(await f.arrayBuffer()), rot: IDENTITY, oriented: false, at: [0, 0], config: {}, paint: null, fit: true };
        if (orient) autoOrient(o);
        added.push(o);
      } catch (e) { toast(`${f.name}: ${e.message}`, 'error'); }
      S.loading.splice(S.loading.indexOf(f.name), 1);
    }
    S.objects.push(...added); arrange(S.objects, S.machine);
    S.sel = new Set(added.map((o) => o.id)); invalidate(); draw();
  }
  function select(id, add) {
    if (id == null) { if (!add) S.sel = new Set(); }
    else if (add) S.sel.has(id) ? S.sel.delete(id) : S.sel.add(id);
    else S.sel = new Set([id]);
    draw();
  }
  const remove = (o) => { S.objects = S.objects.filter((x) => x !== o); S.sel.delete(o.id); invalidate(); draw(); };
  const reorient = (fn, list) => { list.forEach(fn); arrange(S.objects, S.machine); invalidate(); draw(); };
  // The bed comes from the printer's machine preset. A new printer model arranges the plate again.
  async function loadMachine() {
    const ref = refPrinter();
    if (!ref || S.machineModel === ref.model) return;
    try {
      const m = (await loadPresets(ref.model)).machine;
      if (refPrinter()?.model !== ref.model) return;
      S.machine = m; S.machineModel = ref.model; arrange(S.objects, S.machine); invalidate(); draw();
    } catch { /* the engine note and the slice button report a missing engine */ }
  }

  function stepper(n) {
    return h('div', { class: 'steps' }, ['Plate', 'Printer and filament', 'Object settings', 'Compare', 'Queue'].map((l, i) =>
      h('div', { class: 'step' + (i + 1 === n ? ' on' : i + 1 < n ? ' done' : '') }, h('i', null, i + 1 < n ? '✓' : i + 1), l)));
  }

  function plateCard() {
    const input = h('input', { type: 'file', accept: '.stl', multiple: true, hidden: true, onchange: (e) => { addFiles(e.target.files); e.target.value = ''; } });
    const drop = h('div', { class: 'drop', tabindex: '0', role: 'button', onclick: () => input.click(),
      onkeydown: (e) => (e.key === 'Enter' || e.key === ' ') && input.click(),
      ondragover: (e) => { e.preventDefault(); drop.classList.add('over'); }, ondragleave: () => drop.classList.remove('over'),
      ondrop: (e) => { e.preventDefault(); drop.classList.remove('over'); addFiles(e.dataTransfer.files); } },
      icon('upload', 28), h('b', null, S.objects.length ? 'Drop more models or tap to add' : 'Drop models here or tap to choose'), h('small', null, 'STL files, one or several on one plate'), input);
    const none = !S.objects.length, bed = bedOf(S.machine), sel = picked(), all = S.objects.length && sel.length === S.objects.length;
    const tools = h('div', { class: 'row plate-tools' },
      btn('Auto-orient', () => reorient(autoOrient, sel.length ? sel : S.objects), 'sm', { id: 'plate-orient', disabled: none }),
      btn('Original orientation', () => reorient(resetOrient, sel), 'sm', { id: 'plate-original', disabled: !sel.length }),
      btn('Arrange', () => reorient(() => {}, []), 'sm', { id: 'plate-arrange', disabled: none }),
      btn(all ? 'Select none' : 'Select all', () => { S.sel = all ? new Set() : new Set(S.objects.map((o) => o.id)); draw(); }, 'sm', { id: 'plate-all', disabled: none }));
    const auto = h('label', { class: 'check-inline' }, h('input', { type: 'checkbox', id: 'plate-auto', checked: S.autoOrient, onchange: (e) => (S.autoOrient = e.target.checked) }), 'Auto-orient new models');
    return h('section', { class: 'card stack', id: 'plate-card' }, h('h2', { style: 'margin:0' }, '1 · Plate'), drop, view.node,
      h('div', { class: 'plate-note' }, h('small', { class: 'muted', id: 'plate-bed' }, `Bed ${dim(bed.x1 - bed.x0)} × ${dim(bed.y1 - bed.y0)} mm${S.machine ? '' : ', until you pick a printer'}`),
        h('small', { class: 'muted' }, 'Click a model to select it, Shift-click for several. Drag to turn the view.')),
      tools, auto,
      h('div', { class: 'plate-list', id: 'plate-list', role: 'listbox', 'aria-multiselectable': 'true', 'aria-label': 'Models on the plate' }, listRows()));
  }
  const listRows = () => [...S.objects.map((o) => objectRow(o, bedOf(S.machine))), ...S.loading.map((n) => h('div', { class: 'pobj loading' }, h('span', { class: 'spin' }), h('div', { class: 'pobj-main' }, h('b', null, n), h('small', null, 'Reading the model…'))))];
  const refreshList = () => document.getElementById('plate-list')?.replaceChildren(...listRows());  // after a settings edit: the "changed" counts

  function objectRow(o, bed) {
    const [w, d, hh] = size(o), n = Object.keys(o.config).length, on = S.sel.has(o.id), c = (o.paint ? ' · painted' : '');
    const pick = (e) => select(o.id, e.shiftKey || e.ctrlKey || e.metaKey);
    return h('div', { class: 'pobj', role: 'option', tabindex: '0', 'aria-selected': String(on), 'data-id': o.id, 'data-name': o.name, 'data-selected': String(on),
      'data-changed': String(n), 'data-config': JSON.stringify(o.config), 'data-oriented': String(o.oriented), 'data-fit': String(o.fit !== false), 'data-size': [w, d, hh].map(dim).join('x'),
      'data-painted': String(!!o.paint), onclick: pick, onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(e); } } },
      h('div', { class: 'pobj-main' }, h('b', null, o.name), h('small', null, `${dim(w)} × ${dim(d)} × ${dim(hh)} mm${n ? ` · ${n} changed` : ''}${c}`)),
      o.fit === false ? pill(w > bed.x1 - bed.x0 || d > bed.y1 - bed.y0 || hh > bed.h ? 'Does not fit the bed' : 'No room left', 'warn') : null,
      h('button', { type: 'button', class: 'icon-btn', 'aria-label': `Remove ${o.name}`, title: 'Remove', onclick: (e) => { e.stopPropagation(); remove(o); } }, icon('x', 18)));
  }

  function printerCard() {
    const opts = [...models.map((m) => ['any:' + m, `Any compatible ${m}`, (n => n + (n === 1 ? ' printer' : ' printers'))(printers.filter((p) => p.model === m).length)]),
      ...printers.map((p) => [p.serial, p.name, p.model + (p.online === false ? ' · offline' : '')])];
    defaultFil();
    return h('section', { class: 'card stack' }, h('h2', { style: 'margin:0' }, '2 · Printer and filament'),
      h('div', null, h('div', { class: 'tag', style: 'margin-bottom:6px' }, 'Target'),
        h('div', { class: 'chips' }, opts.map(([v, l, sub]) =>
          h('button', { class: 'chip', 'aria-pressed': String(S.target === v), onclick: () => { S.target = v; S.tray = null; S.spool = null; S.variants = null; draw(); loadMachine(); } },
            l, h('small', null, sub))))),
      S.target && filamentPicker(),
      S.target && nozzleRow(S, refPrinter(), draw),
      S.target && engineNote(),
      S.target && field('Build plate', h('select', { onchange: (e) => { S.bed = e.target.value; S.variants = null; draw(); } }, BEDS.map((b) => h('option', { selected: b === S.bed }, b)))));
  }

  function sliceCard() {
    const ready = S.objects.length && S.target && (S.tray || S.spool) && !S.loading.length;
    const chips = h('div', { class: 'chips', id: 'variant-chips', role: 'group', 'aria-label': 'Variants to slice' }, QUALITIES.map(([q, name, sub]) =>
      h('button', { type: 'button', class: 'chip', 'data-q': q, 'aria-pressed': String(S.want.has(q)), disabled: S.busy || undefined, onclick: () => {
        if (S.want.has(q) && S.want.size === 1) return;  // one stays on
        S.want.has(q) ? S.want.delete(q) : S.want.add(q);
        try { localStorage.setItem(VARIANTS_KEY, JSON.stringify([...S.want])); } catch { /* not remembered */ }
        draw();
      } }, name, h('small', null, sub))));
    const n = S.objects.length;
    return h('section', { class: 'card stack', id: 'slice-card' }, h('div', null, h('div', { class: 'tag', style: 'margin-bottom:6px' }, 'Variants to slice'), chips),
      btn(S.busy ? [h('span', { class: 'spin' }), ' ' + sliceLabel(S.want, true)] : sliceLabel(S.want), sliceAll, 'primary', { disabled: !ready || !fitting() || S.busy }),
      h('small', { class: 'slice-note ' + (fitting() ? 'muted' : 'warn-text'), id: 'slice-note' }, !fitting() ? 'A model does not fit the bed. Remove it or choose a bigger printer.'
        : n ? `${n} model${n > 1 ? 's' : ''} on the plate, sliced as one print.` : 'Put a model on the plate first.'));
  }

  // The filament preset name that the engine slices with, for the settings rules. The app resolves it from the tray or spool.
  function ensureFilament() {
    const ref = refPrinter(), fil = filChoice();
    if (!ref || !fil) return null;
    const nz = chosenNozzle(S, ref), key = ref.serial + JSON.stringify(fil) + JSON.stringify(nz);
    if (S.filKey !== key) {
      S.filKey = key; S.filName = null;
      api.engineFilament(ref.serial, { ...fil, ...(nz && { nozzle: nz.diameter, nozzle_type: nz.type }) }).then((r) => { if (S.filKey === key) { S.filName = r.filament; if (box.isConnected) draw(); } }, () => {});
    }
    return S.filName;
  }

  function paintSummary() {
    const one = picked().length === 1 ? picked()[0] : null;
    const c = paintedCount(one?.paint?.states), any = c.supports || c.seam;
    const txt = [c.supports && `${c.supports.toLocaleString()} support triangles`, c.seam && `${c.seam.toLocaleString()} seam triangles`].filter(Boolean).join(', ');
    return h('div', { class: 'ogroup', id: 'paint-summary' }, h('div', { class: 'row between', style: 'gap:8px;flex-wrap:wrap;align-items:center' },
      h('span', null, h('b', null, 'Paint supports and seam'), h('br'),
        h('small', { class: 'muted' }, !one ? 'Select a single model to paint it.' : any ? 'Painted: ' + txt : 'Mark where supports or the seam must (or must not) go.')),
      h('span', { class: 'row', style: 'gap:8px' },
        btn(any ? 'Edit paint…' : 'Paint…', async () => {
          try {
            const r = await paintModel(one.local, one.paint, one.rot);
            if (!r) return;
            one.paint = Object.keys(paintedCount(r.states)).length ? r : null;
            invalidate(); draw();
          } catch (e) { toast(e.message, 'error'); }
        }, 'sm', { id: 'paint-open', disabled: !one }),
        any ? btn('Remove paint', () => { one.paint = null; invalidate(); draw(); }, 'sm') : null)));
  }

  function objectSection() {
    const name = ensureFilament(), ref = refPrinter();
    if (!name || !ref) return h('section', { class: 'card' }, h('h2', null, '3 · Object settings'), h('p', { class: 'muted', style: 'margin:0' }, 'Pick a printer and a filament first.'));
    return objectCard(S, { engine: () => S.engine, model: ref.model, nozzle: chosenNozzle(S, ref)?.diameter, quality: 'better', bed: S.bed, filament: name },
      { onChange: () => { refreshList(); if (S.variants) { invalidate(); draw(); } }, paintSummary });
  }

  function filamentPicker() {
    const ref = refPrinter(), tr = trays(), lib = library(), sp = S.spool && spoolById(S.spool);
    const hasAms = !!ref?.ams?.length;
    const msg = !tr.length && h('p', { class: 'muted', id: 'fil-note', style: 'margin:0' }, hasAms ? 'The AMS is empty.' : 'This printer reports no AMS.',
      lib.length ? ' Choose a spool from your filament library.' : libErr ? ` Your filament library could not be read: ${libErr}` : ' There are no spools in your filament library.');
    return h('div', { class: 'stack', style: 'gap:10px' }, h('div', { class: 'tag' }, 'Filament'),
      msg,
      tr.length > 0 && h('div', null, h('small', { class: 'muted' }, 'In the AMS'),
        h('div', { class: 'chips', id: 'fil-ams' }, tr.map((t) => h('button', { class: 'chip', 'aria-pressed': String(S.tray === trayKey(t)), onclick: pickFil(trayKey(t), null) },
          swatch(t.colour, 22), `AMS ${t.unit + 1}·${t.slot + 1} ${t.type}`, h('small', null, t.name))))),
      lib.length > 0 && h('div', null, h('small', { class: 'muted' }, 'From your library'),
        h('div', { class: 'chips', id: 'fil-library' }, lib.map((x) => h('button', { class: 'chip', 'data-spool': x.id, 'aria-pressed': String(S.spool === x.id), onclick: pickFil(null, x.id) },
          swatch(x.colour, 22), `${x.brand} ${x.type}`.trim(), h('small', null, x.name + (x.grams_left != null ? ` · ${Math.round(x.grams_left)} g` : '')))))),
      sp && h('div', { class: 'row', id: 'load-note', style: 'gap:8px;flex-wrap:wrap;align-items:center' },
        h('small', { class: 'muted' }, hasAms ? `This spool is not in the AMS of ${ref.name}. It has to be loaded before the print starts.` : 'This spool is not loaded. Put it on the printer before the print starts.'),
        // Asked for once the job is queued, so the request belongs to it.
        hasAms && h('label', { class: 'row', style: 'gap:6px;font-size:13px;cursor:pointer' },
          h('input', { type: 'checkbox', id: 'load-ask', checked: !!S.askLoad, onchange: (e) => (S.askLoad = e.target.checked) }), 'Ask the owner to load it when queued')));
  }

  function engineNote() {
    const e = S.engine;
    if (!e) return '';
    const note = h('small', { class: 'muted', id: 'engine-info', 'data-state': e.state });
    if (e.state === 'ready') Object.assign(note.dataset, { variant: e.variant, loadMs: Math.round(e.loadMs), isolated: String(crossOriginIsolated) });
    note.append(e.state === 'loading' ? 'Starting the slicing engine…'
      : e.state === 'error' ? 'The slicing engine did not start: ' + e.error
      : `Engine ready (${e.variant === 'mt' ? 'multi-thread' : 'single-thread'}), started in ${(e.loadMs / 1000).toFixed(1)} s.`);
    return note;
  }

  // The chosen variants one after another in the warm engine: each multi-thread slice already uses every core.
  async function sliceAll() {
    const ref = refPrinter(), nz = chosenNozzle(S, ref), objs = [...S.objects], qs = ALL.filter((q) => S.want.has(q));
    S.busy = true; S.pick = null;
    S.variants = qs.map((q) => ({ q, state: 'queued' }));
    draw();
    try {
      await loadMachine();  // the plate is arranged on this printer's bed
      if (!fitting()) throw new Error('A model does not fit the bed.');
      // The server keeps one model per job: the whole plate as one STL, as placed.
      const stl = new File([writeStl(objs.map(placed))], uploadName(objs), { type: 'model/stl' }), names = uniqueNames(objs);
      for (const v of S.variants) v.id = (await api.upload(stl)).id;
      if (!S.engine) startEngine();
      for (const v of S.variants) {
        v.state = 'slicing'; v.elapsed = 0;
        const t0 = performance.now(), ticker = setInterval(() => { v.elapsed = Math.round((performance.now() - t0) / 1000); draw(); }, 1000);
        const opts = { objects: objs, names, jobId: v.id, model: ref.model, serial: ref.serial, fil: filChoice(), nozzle: nz?.diameter, nozzleType: nz?.type, bed: S.bed, quality: v.q,
          onProgress: (p) => (v.progress = { percent: p.percent, message: p.message || p.stage || '' }) };  // drawn by the 1 s ticker
        try {
          let r;
          for (let attempt = 0; ; attempt++) {  // a fresh engine after an error: its memory may be spent
            if (S.engine?.state === 'loading') await new Promise((ok) => { const i = setInterval(() => S.engine?.state !== 'loading' && (clearInterval(i), ok()), 100); });
            if (S.engine?.state !== 'ready') throw new Error(S.engine?.error || 'The slicing engine is not running');
            try { r = await sliceVariant(S.engine, opts); break; } catch (e) { if (attempt || e.code === -101) throw e; dropEngine(); startEngine(); }
          }
          Object.assign(v, r.job, { state: 'done', slice_ms: Math.round(r.sliceMs), engine_variant: S.engine.variant });
        } catch (e) { Object.assign(v, { state: 'failed', error: e.message }); } finally { clearInterval(ticker); draw(); }
      }
    } catch (e) { fail(e); S.variants = null; }
    S.busy = false;
    dropEngine(); startEngine();  // a fresh worker for the next slice, started now so it is ready
    const done = (q) => S.variants?.find((v) => v.q === q && v.state === 'done');
    const ok = done(S.prefQ || 'better') || done('better') || S.variants?.find((v) => v.state === 'done');
    if (ok && !S.pick) S.pick = ok.q;
    draw();
  }

  const costOf = (v) => {
    if (v.info?.cost != null) return [v.info.cost, false];
    const sp = S.spool ? spoolById(S.spool) : spoolOf(trays().find((t) => trayKey(t) === S.tray) || {});
    return sp?.price_per_kg ? [(v.info.grams * sp.price_per_kg) / 1000, true] : [null, false];
  };

  function compareCard() {
    const done = S.variants.filter((v) => v.state === 'done');
    const fastest = done.length > 1 && done.reduce((a, b) => (a.info.seconds <= b.info.seconds ? a : b)).q;
    const cards = S.variants.map((v) => {
      const [, name, sub] = QUALITIES.find((x) => x[0] === v.q);
      if (v.state === 'failed') return h('div', { class: 'variant' }, h('b', null, name), h('small', { style: 'color:var(--err)' }, v.error || 'Slicing failed'));
      if (v.state !== 'done') return h('div', { class: 'variant' }, h('b', null, name), v.state === 'slicing' ? sliceProgress(v) : h('small', null, h('span', { class: 'spin' }), ' ', 'Waiting for the slicer'));
      const [cost, est] = costOf(v);
      return h('button', { class: 'variant', 'aria-pressed': String(S.pick === v.q), onclick: () => { S.pick = v.q; draw(); } },
        h('div', { class: 'row between' }, h('b', null, name), fastest === v.q ? pill('fastest', 'ok') : null),
        h('div', { class: 'big' }, durSec(v.info.seconds)),
        h('dl', null, h('dt', null, 'Filament'), h('dd', null, v.info.grams.toFixed(1) + ' g'),
          h('dt', null, est ? 'Cost (filament)' : 'Cost'), h('dd', null, cost == null ? '–' : (est ? '≈ ' : '') + money(cost)),
          h('dt', null, 'Layers'), h('dd', null, (v.settings?.process || '').match(/^[\d.]+mm/)?.[0] || '–')),
        h('small', null, sub),
        v.slice_ms != null ? h('small', { class: 'slice-time', 'data-slice-ms': v.slice_ms, 'data-engine': v.engine_variant },
          `Sliced here in ${(v.slice_ms / 1000).toFixed(1)} s (${v.engine_variant})`) : null);
    });
    return h('section', { class: 'card stack' }, h('h2', { style: 'margin:0' }, S.variants.length > 1 ? '4 · Compare' : '4 · Result'),
      h('p', { class: 'muted', style: 'margin:0' }, 'Pick the trade-off between quality and speed. Prices use your spool and the printer power estimate.'),
      h('div', { class: 'variants' }, cards));
  }

  function queueCard() {
    const v = S.variants.find((x) => x.q === S.pick);
    // One preview per chosen variant, kept across redraws (each new one would fetch the G-code again).
    if (shown?.id !== v.id) {
      const node = gcodePreview();
      api.gcode(v.id).then((t) => node.load(t)).catch((e) => node.fail(e.message));
      shown = { id: v.id, node };
    }
    const prev = shown.node;
    const opt = (key, label, hint) => h('label', { class: 'check' },
      h('input', { type: 'checkbox', checked: S.options[key], onchange: (e) => (S.options[key] = e.target.checked) }),
      h('span', null, h('b', null, label), h('br'), h('small', null, hint)));
    const warn = (v.info.warnings || []).filter(Boolean);
    const tri = (key, label, hint) => field(label, h('select', { 'data-opt': key, onchange: (e) => (S.options[key] = TRI[e.target.value]) },
      Object.keys(TRI).map((k) => h('option', { value: k, selected: TRI[k] === S.options[key] }, k[0].toUpperCase() + k.slice(1)))), hint);
    const advanced = h('details', { class: 'adv', open: S.advOpen, ontoggle: (e) => (S.advOpen = e.target.open) },
      h('summary', null, 'Advanced print options'),
      h('div', { class: 'form two' },
        tri('bed_leveling', 'Bed leveling', 'Auto: the printer checks if it is needed.'),
        tri('flow_cali', 'Flow dynamics calibration', 'Auto: skipped if the filament was calibrated lately.'),
        tri('nozzle_offset_cali', 'Nozzle offset calibration', 'For printers with two nozzles.'),
        field('Build plate in the printer', h('select', { 'data-opt': 'bed_type', onchange: (e) => (S.options.bed_type = e.target.value) },
          BED_TYPES.map(([val, l]) => h('option', { value: val, selected: val === S.options.bed_type }, l))), 'Auto: the file decides.'),
        opt('vibration_cali', 'Vibration calibration', 'Measure the resonance before printing'),
        opt('layer_inspect', 'First layer inspection', 'The camera checks the first layer'),
        opt('ext_change_assist', 'External spool change assist', 'Help when you change the external spool')),
      h('small', null, 'Not every printer supports every option. The printer ignores what it cannot do.'));
    return h('section', { class: 'card stack' }, h('h2', { style: 'margin:0' }, '5 · Preview and queue'),
      warn.map((w) => h('div', { class: 'hms warn' }, icon('alert', 20), h('div', { class: 'txt' }, w))),
      h('div', { class: 'preview' }, v.info.has_png ? h('img', { src: api.plateUrl(v.id) + '?v=' + v.id, alt: 'Plate preview', onerror: (e) => e.target.remove() }) : null, prev),
      field('Name of the print', h('input', { type: 'text', maxlength: 80, placeholder: S.objects.length ? uploadName(S.objects).replace(/\.[^.]+$/, '') : '', value: S.name,
        oninput: (e) => (S.name = e.target.value) }), 'Shown in the queue and on the phone. Optional.'),
      h('div', { class: 'form two' },
        opt('timelapse', 'Timelapse', 'Record a video of the print'),
        opt('use_ams', 'Use AMS', 'Feed filament from the AMS')),
      advanced,
      btn([icon('plus', 18), `Add ${QUALITIES.find((x) => x[0] === S.pick)[1]} to queue`], async () => {
        const any = S.target.startsWith('any:');
        const r = await attempt(() => api.queueJob(v.id, { target: any ? 'any' : S.target, ...(any ? { model: S.target.slice(4) } : {}), options: S.options, ...(S.name.trim() ? { name: S.name.trim() } : {}) }));
        if (!r) return;
        await askChanges(r);
        toast(r.state === 'pending_approval' ? 'Added. An admin needs to approve it first.' : 'Added to the queue', 'ok');
        ctx.go('#/queue');
      }, 'primary'));
  }

  // Change requests of the chosen nozzle and spool, made for the job that was just queued (it is blocked until they close).
  async function askChanges(job) {
    const ref = refPrinter(), nz = chosenNozzle(S, ref), sp = S.spool && spoolById(S.spool);
    if (S.askNozzle && nz && ref.nozzle && !sameNozzle(nz, ref.nozzle)) {
      await attempt(() => api.askNozzle(ref.serial, { diameter: nz.diameter, type: nz.type, note: (S.askNozzleNote || '').trim(), job_id: job.id }), 'Nozzle change requested');
    }
    if (S.askLoad && sp && ref.ams?.length && !(sp.printer === ref.serial && sp.in_printer)) {
      await attempt(() => api.askLoad(sp.id, ref.serial, '', job.id), 'Load requested');
    }
  }

  function draw() {
    const step = S.pick ? 5 : S.variants ? 4 : S.objects.length && S.target ? 3 : S.objects.length ? 2 : 1;
    if (!printers.length) return clear(box, empty('You have no printer access yet. Ask an admin for a grant.'));
    clear(box, stepper(step), plateCard(), S.objects.length > 0 && printerCard(), S.objects.length > 0 && S.target && objectSection(), S.objects.length > 0 && sliceCard(), S.variants && compareCard(), S.pick && queueCard());
    view.update({ objects: S.objects, sel: S.sel, machine: S.machine });
  }
  // Reslice (from a history or queue detail) hands over what the old print had; it is used once.
  const rs = ctx.reslice;
  ctx.reslice = null;
  if (rs) {
    S.name = rs.name || '';
    Object.assign(S.options, rs.options || {});
    if (BEDS.includes(rs.bed)) S.bed = rs.bed;
    S.prefQ = rs.quality;
    if (ALL.includes(rs.quality)) S.want.add(rs.quality);
    const any = rs.target === 'any' ? 'any:' + rs.printer_model : null;
    S.target = any && models.includes(rs.printer_model) ? any : printers.some((p) => p.serial === rs.target) ? rs.target : null;
    // The old filament: a library spool, or an AMS tray if it is still loaded; otherwise the default tray stays.
    const t = rs.tray || {};
    if (S.target && t.ams == null && t.spool_id != null && spoolById(String(t.spool_id))) S.spool = String(t.spool_id);
    else if (S.target && t.ams != null && trays().some((x) => trayKey(x) === `${t.ams}:${t.id}`)) S.tray = `${t.ams}:${t.id}`;
    // The stored model is the whole plate as one STL: it comes back as one object, in the pose it was printed in.
    if (!rs.slice_job) toast('The model is no longer stored. Choose it again.', 'info');
    else api.jobModel(rs.slice_job).then((blob) => addFiles([new File([blob], rs.model || 'model.stl')], { orient: false }).then(loadMachine),
      () => toast('The model is no longer stored. Choose it again.', 'error'));
  }
  startEngine();
  draw();
  loadMachine();
}
