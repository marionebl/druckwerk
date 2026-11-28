// New print: upload, pick printer + filament, slice good/better/best side by side, choose, queue.
import { api } from '../api.js';
import { h, clear, icon, btn, pill, field, swatch, empty, attempt, toast, fail, durSec, money, hex } from '../util.js';
import { gcodePreview } from './preview.js';
import { warm, sliceVariant } from './browserslice.js';
import { promote } from './promote.js';
import { sliceProgress } from './progress.js';

const QUALITIES = [['good', 'Good', 'Fastest, visible layers'], ['better', 'Better', 'Balanced'], ['best', 'Best', 'Finest detail, slowest']];
const BEDS = ['Textured PEI Plate', 'Cool Plate', 'Engineering Plate', 'High Temp Plate', 'Supertack Plate'];
const TRI = { on: true, off: false, auto: 'auto' };
const BED_TYPES = [['auto', 'Auto'], ['cool_plate', 'Cool Plate'], ['eng_plate', 'Engineering Plate'], ['hot_plate', 'High Temp Plate'], ['textured_plate', 'Textured PEI Plate'], ['supertack_plate', 'Supertack Plate'], ['3d_effect_plate', '3D Effect Plate']];
const ACCEPT = /\.stl$/i;

export async function mount(ctx, root) {
  if (promote()) return;  // the top window is moving to this page, where threads work
  let libErr = '';
  const [printers, spools, requests] = await Promise.all([api.printers(), api.spools().catch((e) => { libErr = e.message; return []; }), api.loadRequests().catch(() => [])]);
  // tray: "unit:slot" of a loaded AMS tray; spool: id of a library spool. At most one of them is set.
  const S = { file: null, target: null, tray: null, spool: null, bed: BEDS[0], variants: null, pick: null,
    options: { timelapse: false, bed_leveling: true, use_ams: true, flow_cali: true, vibration_cali: true, layer_inspect: false, nozzle_offset_cali: false, bed_type: 'auto', ext_change_assist: false },
    name: '', busy: false, engine: null };
  const box = h('div', { class: 'stack' });
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

  function pickFile(f) {
    if (!f) return;
    if (!ACCEPT.test(f.name)) return toast('Upload an STL file.', 'error');
    S.file = f; S.variants = null; S.pick = null; draw();
  }

  function stepper(n) {
    return h('div', { class: 'steps' }, ['Model', 'Printer and filament', 'Compare', 'Queue'].map((l, i) =>
      h('div', { class: 'step' + (i + 1 === n ? ' on' : i + 1 < n ? ' done' : '') }, h('i', null, i + 1 < n ? '✓' : i + 1), l)));
  }

  function fileCard() {
    const input = h('input', { type: 'file', accept: '.stl', hidden: true, onchange: (e) => pickFile(e.target.files[0]) });
    const drop = h('div', { class: 'drop', tabindex: '0', role: 'button', onclick: () => input.click(),
      onkeydown: (e) => (e.key === 'Enter' || e.key === ' ') && input.click(),
      ondragover: (e) => { e.preventDefault(); drop.classList.add('over'); }, ondragleave: () => drop.classList.remove('over'),
      ondrop: (e) => { e.preventDefault(); drop.classList.remove('over'); pickFile(e.dataTransfer.files[0]); } },
      icon('upload', 28),
      S.file ? h('b', null, S.file.name) : h('b', null, 'Drop a model here or tap to choose'),
      h('small', null, S.file ? `${(S.file.size / 1048576).toFixed(1)} MB · tap to replace` : 'STL'), input);
    return h('section', { class: 'card' }, h('h2', null, '1 · Model'), drop);
  }

  function printerCard() {
    const opts = [...models.map((m) => ['any:' + m, `Any compatible ${m}`, (n => n + (n === 1 ? ' printer' : ' printers'))(printers.filter((p) => p.model === m).length)]),
      ...printers.map((p) => [p.serial, p.name, p.model + (p.online ? '' : ' · offline')])];
    defaultFil();
    return h('section', { class: 'card stack' }, h('h2', { style: 'margin:0' }, '2 · Printer and filament'),
      h('div', null, h('div', { class: 'tag', style: 'margin-bottom:6px' }, 'Target'),
        h('div', { class: 'chips' }, opts.map(([v, l, sub]) =>
          h('button', { class: 'chip', 'aria-pressed': String(S.target === v), onclick: () => { S.target = v; S.tray = null; S.spool = null; S.variants = null; draw(); } },
            l, h('small', null, sub))))),
      S.target && filamentPicker(),
      S.target && engineNote(),
      S.target && field('Build plate', h('select', { onchange: (e) => (S.bed = e.target.value) }, BEDS.map((b) => h('option', { selected: b === S.bed }, b)))),
      btn(S.busy ? [h('span', { class: 'spin' }), ' Slicing three variants…'] : 'Slice good, better and best', sliceAll, 'primary',
        { disabled: !(S.file && S.target && (S.tray || S.spool)) || S.busy }));
  }

  function filamentPicker() {
    const ref = refPrinter(), tr = trays(), lib = library(), sp = S.spool && spoolById(S.spool);
    const hasAms = !!ref?.ams?.length;
    const asked = sp && requests.some((r) => r.spool_id === sp.id && r.printer === ref.serial);
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
        hasAms && (asked ? pill('load requested', 'warn')
          : btn('Ask to load', async () => { const r = await attempt(() => api.askLoad(sp.id, ref.serial, ''), 'Request sent'); if (r) { requests.push(r); draw(); } }, 'sm'))));
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

  // The three variants one after another in the warm engine: each multi-thread slice already uses every core.
  async function sliceAll() {
    const ref = refPrinter();
    S.busy = true; S.pick = null;
    S.variants = QUALITIES.map(([q]) => ({ q, state: 'queued' }));
    draw();
    try {
      for (const v of S.variants) v.id = (await api.upload(S.file)).id;
      if (!S.engine) startEngine();
      for (const v of S.variants) {
        v.state = 'slicing'; v.elapsed = 0;
        const t0 = performance.now(), ticker = setInterval(() => { v.elapsed = Math.round((performance.now() - t0) / 1000); draw(); }, 1000);
        const opts = { file: S.file, jobId: v.id, model: ref.model, serial: ref.serial, fil: filChoice(), bed: S.bed, quality: v.q,
          onProgress: (p) => (v.progress = { percent: p.percent, message: p.message || p.stage || '' }) };  // drawn by the 1 s ticker
        try {
          let r;
          for (let attempt = 0; ; attempt++) {  // a fresh engine after an error: its memory may be spent
            if (S.engine?.state === 'loading') await new Promise((ok) => { const i = setInterval(() => S.engine?.state !== 'loading' && (clearInterval(i), ok()), 100); });
            if (S.engine?.state !== 'ready') throw new Error(S.engine?.error || 'The slicing engine is not running');
            try { r = await sliceVariant(S.engine, opts); break; } catch (e) { if (attempt) throw e; dropEngine(); startEngine(); }
          }
          Object.assign(v, r.job, { state: 'done', slice_ms: Math.round(r.sliceMs), engine_variant: S.engine.variant });
        } catch (e) { Object.assign(v, { state: 'failed', error: e.message }); } finally { clearInterval(ticker); draw(); }
      }
    } catch (e) { fail(e); S.variants = null; }
    S.busy = false;
    dropEngine(); startEngine();  // a fresh worker for the next slice, started now so it is ready
    const ok = S.variants?.find((v) => v.q === (S.prefQ || 'better') && v.state === 'done') || S.variants?.find((v) => v.q === 'better' && v.state === 'done');
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
    return h('section', { class: 'card stack' }, h('h2', { style: 'margin:0' }, '3 · Compare'),
      h('p', { class: 'muted', style: 'margin:0' }, 'Pick the trade-off between quality and speed. Prices use your spool and the printer power estimate.'),
      h('div', { class: 'variants' }, cards));
  }

  function queueCard() {
    const v = S.variants.find((x) => x.q === S.pick);
    const prev = gcodePreview();
    api.gcode(v.id).then((t) => prev.load(t)).catch((e) => prev.fail(e.message));
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
    return h('section', { class: 'card stack' }, h('h2', { style: 'margin:0' }, '4 · Preview and queue'),
      warn.map((w) => h('div', { class: 'hms warn' }, icon('alert', 20), h('div', { class: 'txt' }, w))),
      h('div', { class: 'preview' }, v.info.has_png ? h('img', { src: api.plateUrl(v.id) + '?v=' + v.id, alt: 'Plate preview', onerror: (e) => e.target.remove() }) : null, prev),
      field('Name of the print', h('input', { type: 'text', maxlength: 80, placeholder: S.file ? S.file.name.replace(/\.[^.]+$/, '') : '', value: S.name,
        oninput: (e) => (S.name = e.target.value) }), 'Shown in the queue and on the phone. Optional.'),
      h('div', { class: 'form two' },
        opt('timelapse', 'Timelapse', 'Record a video of the print'),
        opt('use_ams', 'Use AMS', 'Feed filament from the AMS')),
      advanced,
      btn([icon('plus', 18), `Add ${QUALITIES.find((x) => x[0] === S.pick)[1]} to queue`], async () => {
        const any = S.target.startsWith('any:');
        const r = await attempt(() => api.queueJob(v.id, { target: any ? 'any' : S.target, ...(any ? { model: S.target.slice(4) } : {}), options: S.options, ...(S.name.trim() ? { name: S.name.trim() } : {}) }));
        if (!r) return;
        toast(r.state === 'pending_approval' ? 'Added. An admin needs to approve it first.' : 'Added to the queue', 'ok');
        ctx.go('#/queue');
      }, 'primary'));
  }

  function draw() {
    const step = S.pick ? 4 : S.variants ? 3 : S.file ? 2 : 1;
    if (!printers.length) return clear(box, empty('You have no printer access yet. Ask an admin for a grant.'));
    clear(box, stepper(step), fileCard(), S.file && printerCard(), S.variants && compareCard(), S.pick && queueCard());
  }
  // Reslice (from a history or queue detail) hands over what the old print had; it is used once.
  const rs = ctx.reslice;
  ctx.reslice = null;
  if (rs) {
    S.name = rs.name || '';
    Object.assign(S.options, rs.options || {});
    if (BEDS.includes(rs.bed)) S.bed = rs.bed;
    S.prefQ = rs.quality;
    const any = rs.target === 'any' ? 'any:' + rs.printer_model : null;
    S.target = any && models.includes(rs.printer_model) ? any : printers.some((p) => p.serial === rs.target) ? rs.target : null;
    // The old filament: a library spool, or an AMS tray if it is still loaded; otherwise the default tray stays.
    const t = rs.tray || {};
    if (S.target && t.ams == null && t.spool_id != null && spoolById(String(t.spool_id))) S.spool = String(t.spool_id);
    else if (S.target && t.ams != null && trays().some((x) => trayKey(x) === `${t.ams}:${t.id}`)) S.tray = `${t.ams}:${t.id}`;
    api.jobModel(rs.slice_job).then((blob) => { S.file = new File([blob], rs.model || 'model.stl'); S.variants = null; S.pick = null; draw(); },
      () => toast('The model is no longer stored. Choose it again.', 'error'));
  }
  startEngine();
  draw();
}
