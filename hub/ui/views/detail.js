// One layout for a print and its Druckwerk job: used by the history and the queue detail views.
// Every block is optional; a block with nothing to show is left out.
import { api } from '../api.js';
import { h, clear, icon, btn, pill, field, swatch, attempt, dateTime, durSec, money, thumb } from '../util.js';
import { gcodePreview } from './preview.js';

export const OPTION_LABELS = {
  timelapse: 'Timelapse', use_ams: 'Use AMS', bed_leveling: 'Bed leveling', flow_cali: 'Flow dynamics calibration',
  vibration_cali: 'Vibration calibration', layer_inspect: 'First layer inspection', nozzle_offset_cali: 'Nozzle offset calibration',
  bed_type: 'Build plate in the printer', ext_change_assist: 'External spool change assist',
};
const TRI = ['bed_leveling', 'flow_cali', 'nozzle_offset_cali'];
const BED_TYPES = [['auto', 'Auto'], ['cool_plate', 'Cool Plate'], ['eng_plate', 'Engineering Plate'], ['hot_plate', 'High Temp Plate'], ['textured_plate', 'Textured PEI Plate'], ['supertack_plate', 'Supertack Plate'], ['3d_effect_plate', '3D Effect Plate']];
const QUALITY = { good: 'Good', better: 'Better', best: 'Best' };
const shown = (k, v) => (k === 'bed_type' ? BED_TYPES.find(([x]) => x === v)?.[1] || v : v === true ? 'On' : v === false ? 'Off' : v === 'auto' ? 'Auto' : String(v));

// A definition list that skips rows without a value.
export function facts(rows) {
  const rs = rows.filter(([, v]) => v != null && v !== false && v !== '' && !(Array.isArray(v) && !v.length));
  return rs.length ? h('dl', { class: 'kv' }, rs.map(([k, v]) => [h('dt', null, k), h('dd', null, v)])) : null;
}

export function card(title, ...body) {
  const kids = body.flat(Infinity).filter(Boolean);
  return kids.length ? h('section', { class: 'card stack', style: 'gap:10px' }, h('h2', { style: 'margin:0' }, title), kids) : null;
}

const tray = (t) => t && (t.ams != null && t.id != null ? `AMS ${+t.ams + 1}·${+t.id + 1}` : '') + (t.type ? ` ${t.type}` : '');

// Print options and slice of a job, as the person chose them.
export function settingsCards(job) {
  if (!job) return [];
  const sl = job.slice || {};
  const opts = job.options && Object.keys(job.options).length ? facts(Object.entries(job.options).filter(([k]) => OPTION_LABELS[k]).map(([k, v]) => [OPTION_LABELS[k], shown(k, v)])) : null;
  const fil = (sl.filaments || []).map((f) => h('span', { class: 'row', style: 'gap:6px;justify-content:flex-end' }, swatch(f.colour, 14), `${f.type} · ${(+f.grams || 0).toFixed(1)} g`));
  const slice = facts([
    ['Quality', QUALITY[sl.quality] || sl.quality], ['Process', sl.process], ['Filament preset', sl.filament], ['Build plate', sl.bed],
    ['Tray', tray(sl.tray)], ['Print time', sl.seconds ? durSec(sl.seconds) : null], ['Filament', fil], ['Model', sl.model]]);
  return [card('Print options', opts), card('Slice', slice)];
}

export function plateImage(...srcs) {
  const src = srcs.find(Boolean);
  return thumb(src, 'detail-img', { loading: 'eager' });
}

// The 3D preview needs the sliced file, which the slicer keeps only for its newest jobs; without it nothing is added.
export function previewCard(sliceJob) {
  if (!sliceJob) return null;
  const box = h('section', { class: 'card stack', style: 'gap:10px', hidden: true }, h('h2', { style: 'margin:0' }, '3D preview'));
  const prev = gcodePreview();
  api.gcode(sliceJob).then((t) => { box.hidden = false; box.append(prev); return prev.load(t); }).catch(() => box.remove());
  return box;
}

// The slice job as the slicer still keeps it (model and result); null when it is gone.
export async function sliceSource(job) {
  if (!job?.slice_job) return null;
  try { const s = await api.job(job.slice_job); return s.state === 'done' ? s : null; } catch { return null; }
}

// Reslice: New print opens with the same model and settings. The model is the slicer job that is still stored.
export function reslice(ctx, job, source) {
  const sl = job.slice || {};
  ctx.reslice = { slice_job: job.slice_job, model: sl.model || source?.filename, name: job.name, options: job.options, target: job.target,
    printer_model: job.model, quality: sl.quality, tray: sl.tray, bed: sl.bed };
  ctx.go('#/new');
}

// Print again: the same slice goes into the queue once more, with the same name, options and target.
export async function printAgain(ctx, job) {
  const any = job.target === 'any';
  const body = { target: any ? 'any' : job.printer || job.target, ...(any ? { model: job.model } : {}), options: job.options, name: job.name };
  const r = await attempt(() => api.queueJob(job.slice_job, body), 'Added to the queue');
  if (r) ctx.go('#/queue/' + r.id);
}

// Editable print options; get() returns the options that differ from `options`.
export function optionsEditor(options, onInput) {
  const keys = Object.keys(OPTION_LABELS).filter((k) => k in options), now = { ...options };
  const set = (k, v) => { now[k] = v; onInput?.(); };
  const rows = keys.map((k) => {
    if (TRI.includes(k) || k === 'bed_type') {
      const opts = k === 'bed_type' ? BED_TYPES : [['true', 'On'], ['false', 'Off'], ['auto', 'Auto']];
      const cur = String(now[k]);
      return field(OPTION_LABELS[k], h('select', { 'data-opt': k, onchange: (e) => set(k, k === 'bed_type' || e.target.value === 'auto' ? e.target.value : e.target.value === 'true') },
        opts.map(([v, l]) => h('option', { value: v, selected: v === cur }, l))));
    }
    return h('label', { class: 'check' }, h('input', { type: 'checkbox', 'data-opt': k, checked: now[k], onchange: (e) => set(k, e.target.checked) }), h('span', null, h('b', null, OPTION_LABELS[k])));
  });
  return { node: h('div', { class: 'form two' }, rows), get: () => Object.fromEntries(Object.entries(now).filter(([k, v]) => v !== options[k])) };
}

export const back = (href, label) => h('a', { class: 'btn sm', href, style: 'margin-bottom:12px;align-self:start' }, icon('back', 16), label);

// The page: heading with pills, then the cards, then the actions.
export function page({ title, sub, pills, facts: f, image, blocks, actions }) {
  return h('div', { class: 'stack detail' },
    h('div', { class: 'page-head', style: 'margin:0' }, h('div', null, h('h1', { style: 'overflow-wrap:anywhere' }, title), sub && h('div', { class: 'sub' }, sub)),
      h('div', { class: 'row' }, pills)),
    h('div', { class: 'detail-top' }, image, card('Details', f)),
    blocks,
    actions && actions.filter(Boolean).length ? h('div', { class: 'row', style: 'gap:8px' }, actions) : null);
}

export { btn, pill, dateTime, durSec, money, clear };
