// New print: upload, pick printer + filament, slice good/better/best side by side, choose, queue.
import { api } from '../api.js';
import { h, clear, icon, btn, pill, field, swatch, empty, attempt, toast, fail, durSec, money, hex } from '../util.js';
import { gcodePreview } from './preview.js';
import { warm, sliceVariant } from './browserslice.js';

const QUALITIES = [['good', 'Good', 'Fastest, visible layers'], ['better', 'Better', 'Balanced'], ['best', 'Best', 'Finest detail, slowest']];
const BEDS = ['Textured PEI Plate', 'Cool Plate', 'Engineering Plate', 'High Temp Plate', 'Supertack Plate'];
const TRI = { on: true, off: false, auto: 'auto' };
const BED_TYPES = [['auto', 'Auto'], ['cool_plate', 'Cool Plate'], ['eng_plate', 'Engineering Plate'], ['hot_plate', 'High Temp Plate'], ['textured_plate', 'Textured PEI Plate'], ['supertack_plate', 'Supertack Plate'], ['3d_effect_plate', '3D Effect Plate']];
const ACCEPT = /\.(stl|3mf|step|stp|obj)$/i;

export async function mount(ctx, root) {
  const [printers, spools] = await Promise.all([api.printers(), api.spools().catch(() => [])]);
  const S = { file: null, target: null, tray: null, bed: BEDS[0], variants: null, pick: null,
    options: { timelapse: false, bed_leveling: true, use_ams: true, flow_cali: true, vibration_cali: true, layer_inspect: false, nozzle_offset_cali: false, bed_type: 'auto', ext_change_assist: false },
    name: '', busy: false,
    // Threads need a cross-origin isolated page; without it the Pi is quicker, so it is the default there.
    slice_on: crossOriginIsolated ? 'device' : 'pi', engine: null };
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

  // The browser slices STL only; other models slice on the Pi.
  const where = () => (S.slice_on === 'device' && (!S.file || /\.stl$/i.test(S.file.name)) ? 'device' : 'pi');
  function startEngine() {
    const e = S.engine = { state: 'loading' };
    warm().then((x) => Object.assign(e, x, { state: 'ready' }), (x) => Object.assign(e, { state: 'error', error: x.message }))
      .then(() => { if (box.isConnected) draw(); else e.worker?.terminate(); });
  }
  function dropEngine() { S.engine?.worker?.terminate(); S.engine = null; }

  function pickFile(f) {
    if (!f) return;
    if (!ACCEPT.test(f.name)) return toast('Upload an STL, 3MF or STEP file.', 'error');
    S.file = f; S.variants = null; S.pick = null; draw();
  }

  function stepper(n) {
    return h('div', { class: 'steps' }, ['Model', 'Printer and filament', 'Compare', 'Queue'].map((l, i) =>
      h('div', { class: 'step' + (i + 1 === n ? ' on' : i + 1 < n ? ' done' : '') }, h('i', null, i + 1 < n ? '✓' : i + 1), l)));
  }

  function fileCard() {
    const input = h('input', { type: 'file', accept: '.stl,.3mf,.step,.stp,.obj', hidden: true, onchange: (e) => pickFile(e.target.files[0]) });
    const drop = h('div', { class: 'drop', tabindex: '0', role: 'button', onclick: () => input.click(),
      onkeydown: (e) => (e.key === 'Enter' || e.key === ' ') && input.click(),
      ondragover: (e) => { e.preventDefault(); drop.classList.add('over'); }, ondragleave: () => drop.classList.remove('over'),
      ondrop: (e) => { e.preventDefault(); drop.classList.remove('over'); pickFile(e.dataTransfer.files[0]); } },
      icon('upload', 28),
      S.file ? h('b', null, S.file.name) : h('b', null, 'Drop a model here or tap to choose'),
      h('small', null, S.file ? `${(S.file.size / 1048576).toFixed(1)} MB · tap to replace` : 'STL, 3MF or STEP'), input);
    return h('section', { class: 'card' }, h('h2', null, '1 · Model'), drop);
  }

  function printerCard() {
    const opts = [...models.map((m) => ['any:' + m, `Any compatible ${m}`, (n => n + (n === 1 ? ' printer' : ' printers'))(printers.filter((p) => p.model === m).length)]),
      ...printers.map((p) => [p.serial, p.name, p.model + (p.online ? '' : ' · offline')])];
    const tr = trays();
    return h('section', { class: 'card stack' }, h('h2', { style: 'margin:0' }, '2 · Printer and filament'),
      h('div', null, h('div', { class: 'tag', style: 'margin-bottom:6px' }, 'Target'),
        h('div', { class: 'chips' }, opts.map(([v, l, sub]) =>
          h('button', { class: 'chip', 'aria-pressed': String(S.target === v), onclick: () => { S.target = v; S.tray = null; S.variants = null; draw(); } },
            l, h('small', null, sub))))),
      S.target && h('div', null, h('div', { class: 'tag', style: 'margin-bottom:6px' }, 'Filament tray'),
        tr.length ? h('div', { class: 'chips' }, tr.map((t) => h('button', { class: 'chip', 'aria-pressed': String(S.tray === trayKey(t)),
          onclick: () => { S.tray = trayKey(t); S.variants = null; draw(); } },
          swatch(t.colour, 22), `AMS ${t.unit + 1}·${t.slot + 1} ${t.type}`, h('small', null, t.name))))
          : h('p', { class: 'muted', style: 'margin:0' }, 'No AMS trays reported for this printer.')),
      S.target && h('div', null, h('div', { class: 'tag', style: 'margin-bottom:6px' }, 'Slice on'), h('div', { class: 'chips', id: 'where' },
        [['device', 'This device'], ['pi', 'Pi (Bambu Studio)']].map(([v, l]) => h('button', { class: 'chip', 'aria-pressed': String(where() === v),
          disabled: v === 'device' && S.file && !/\.stl$/i.test(S.file.name),
          onclick: () => { S.slice_on = v; if (v === 'device' && !S.engine) startEngine(); draw(); } }, l))),
        h('small', { class: 'muted' }, S.file && !/\.stl$/i.test(S.file.name) ? 'Slicing on this device needs an STL; this file slices on the Pi.' : engineNote())),
      S.target && field('Build plate', h('select', { onchange: (e) => (S.bed = e.target.value) }, BEDS.map((b) => h('option', { selected: b === S.bed }, b)))),
      btn(S.busy ? [h('span', { class: 'spin' }), ' Slicing three variants…'] : 'Slice good, better and best', sliceAll, 'primary',
        { disabled: !(S.file && S.target && S.tray) || S.busy }));
  }

  function engineNote() {
    const e = S.engine;
    if (where() !== 'device' || !e) return '';
    const note = h('span', { id: 'engine-info', 'data-state': e.state });
    if (e.state === 'ready') Object.assign(note.dataset, { variant: e.variant, loadMs: Math.round(e.loadMs), isolated: String(crossOriginIsolated) });
    note.append(e.state === 'loading' ? 'Starting the slicing engine…'
      : e.state === 'error' ? 'The slicing engine did not start: ' + e.error
      : `Engine ready (${e.variant === 'mt' ? 'multi-thread' : 'single-thread'}), started in ${(e.loadMs / 1000).toFixed(1)} s.`);
    return note;
  }

  // The three variants one after another in the warm engine: each multi-thread slice already uses every core.
  async function sliceAllHere() {
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
        const opts = { file: S.file, jobId: v.id, model: ref.model, serial: ref.serial, tray: S.tray, bed: S.bed, quality: v.q };
        try {
          let r;
          for (let attempt = 0; ; attempt++) {  // a fresh engine after an error: its memory may be spent
            if (S.engine?.state === 'loading') await new Promise((ok) => { const i = setInterval(() => S.engine?.state !== 'loading' && (clearInterval(i), ok()), 100); });
            if (S.engine?.state !== 'ready') throw new Error(S.engine?.error || 'The slicing engine is not running');
            try { r = await sliceVariant(S.engine, opts); break; } catch (e) { if (attempt) throw e; dropEngine(); startEngine(); }
          }
          Object.assign(v, r.job, { state: 'done', slice_ms: Math.round(r.sliceMs), engine_variant: S.engine.variant });
        } catch (e) { Object.assign(v, { state: 'failed', error: e.message, retry_on_pi: true }); } finally { clearInterval(ticker); draw(); }
      }
    } catch (e) { fail(e); S.variants = null; }
    S.busy = false;
    dropEngine(); startEngine();  // a fresh worker for the next slice, started now so it is ready
    const ok = S.variants?.find((v) => v.q === 'better' && v.state === 'done');
    if (ok && !S.pick) S.pick = ok.q;
    draw();
  }

  async function sliceAll() {
    if (where() === 'device') return sliceAllHere();
    const ref = refPrinter();
    S.busy = true; S.pick = null;
    S.variants = QUALITIES.map(([q]) => ({ q, state: 'queued' }));
    draw();
    try {
      await Promise.all(S.variants.map(async (v) => {
        const job = await api.upload(S.file);
        v.id = job.id;
        await api.slice(job.id, { serial: ref.serial, quality: v.q, tray: S.tray, bed: S.bed });
      }));
    } catch (e) { fail(e); S.busy = false; S.variants = null; draw(); return; }
    const tick = async () => {
      if (!box.isConnected) return;
      for (const v of S.variants) if (v.state === 'queued' || v.state === 'slicing') Object.assign(v, await api.job(v.id).catch((e) => ({ state: 'failed', error: e.message })));
      S.busy = S.variants.some((v) => v.state === 'queued' || v.state === 'slicing');
      draw();
      if (S.busy) setTimeout(tick, 1500);
      else if (!S.pick) { const ok = S.variants.find((v) => v.q === 'better' && v.state === 'done'); if (ok) { S.pick = ok.q; draw(); } }
    };
    tick();
  }

  const costOf = (v) => {
    if (v.info?.cost != null) return [v.info.cost, false];
    const sp = spoolOf(trays().find((t) => trayKey(t) === S.tray) || {});
    return sp?.price_per_kg ? [(v.info.grams * sp.price_per_kg) / 1000, true] : [null, false];
  };

  function compareCard() {
    const done = S.variants.filter((v) => v.state === 'done');
    const fastest = done.length > 1 && done.reduce((a, b) => (a.info.seconds <= b.info.seconds ? a : b)).q;
    const cards = S.variants.map((v) => {
      const [, name, sub] = QUALITIES.find((x) => x[0] === v.q);
      if (v.state === 'failed') return h('div', { class: 'variant' }, h('b', null, name), h('small', { style: 'color:var(--err)' }, v.error || 'Slicing failed'),
        v.retry_on_pi ? btn('Slice on the Pi instead', () => { S.slice_on = 'pi'; sliceAll(); }) : null);
      if (v.state !== 'done') return h('div', { class: 'variant' }, h('b', null, name), h('small', null, h('span', { class: 'spin' }), ' ', v.state === 'slicing' ? `Slicing… ${v.elapsed || 0} s` : 'Waiting for the slicer'));
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
      h('div', { class: 'preview' }, v.info.has_png ? h('img', { src: api.plateUrl(v.id) + '?v=' + v.id, alt: 'Plate preview' }) : null, prev),
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
  if (where() === 'device') startEngine();
  draw();
}
