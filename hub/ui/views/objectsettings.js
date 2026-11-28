// Per-object print settings, as in Bambu Studio's object list: the common ones are always there, every other
// object setting of OrcaSlicer is added from a list. The form, the rules (greyed rows, warnings) and the values that
// an edit changes along with it all come from the engine's settings service (settings.view and settings.edit).
import { h, clear, btn, pill, toast } from '../util.js';
import { settingsCall, loadPresets } from './browserslice.js';

// Always visible, in this order. A key the engine does not know for objects is left out.
const COMMON = [
  ['Seam', ['seam_position', 'seam_gap', 'seam_slope_type', 'seam_slope_min_length', 'staggered_inner_seams']],
  ['Supports', ['enable_support', 'support_type', 'support_style', 'support_threshold_angle', 'support_on_build_plate_only', 'support_critical_regions_only', 'support_object_xy_distance', 'support_top_z_distance']],
  ['Fuzzy skin (surface randomisation)', ['fuzzy_skin', 'fuzzy_skin_thickness', 'fuzzy_skin_point_distance', 'fuzzy_skin_mode', 'fuzzy_skin_noise_type']],
  ['Walls and shells', ['wall_loops', 'top_shell_layers', 'bottom_shell_layers']],
  ['Infill', ['sparse_infill_density', 'sparse_infill_pattern']],
  ['Quality', ['layer_height', 'ironing_type']],
  ['Brim', ['brim_type', 'brim_width']],
];
const SUPPORT_KEYS = /^(support_|tree_support|enable_support$)/;

const first = (v) => (Array.isArray(v) ? v[0] : v);

// ctx: {engine(), model, quality, bed, filament} for the engine requests; S.obj is the object's settings, kept by the caller.
export function objectCard(S, ctx, { onChange, onPaint, paintSummary }) {
  const box = h('section', { class: 'card stack', id: 'object-settings' });
  let form = S.ovForm || null, view = S.ovView || null, busy = Promise.resolve(), process = null, notice = '';

  const eng = () => { const e = ctx.engine(); return e && e.state === 'ready' ? e : null; };
  const call = (op, extra) => settingsCall(eng(), ctx, op, { scope: 'object', object: { ...S.obj }, values: true, ...extra });
  const opt = (k) => form?.options[k];
  const inherited = (k) => view?.object.groups.flatMap((g) => g.rows).find((r) => r.key === k)?.inherited ?? first(process?.[k]) ?? first(opt(k)?.default) ?? '';
  const own = (k) => S.obj[k];

  // Serial queue: an edit is sent after the one before it, and the view that comes back is the one after the last edit.
  function edit(spec) {
    busy = busy.then(async () => {
      if (!eng()) return;
      try {
        const r = await call('settings.edit', { edit: spec, view: { form: form?.id, values: true } });
        for (const [k, v] of Object.entries(r.writes)) { if (v == null) delete S.obj[k]; else S.obj[k] = v; }
        notice = [...(r.notices || []), ...(r.prompts || []).map((p) => p.message)].join(' ');
        take(await call('settings.view', { form: form?.id }));
        onChange();
      } catch (e) { toast(e.message, 'error'); }
      render();
    });
    return busy;
  }
  function take(v) { if (v.form) form = v.form; view = v; S.ovForm = form; S.ovView = view; }

  async function load() {
    if (!eng()) return;
    process = (await loadPresets(ctx.model)).process[ctx.quality];
    take(await call('settings.view', { form: form?.id }));
    render();
  }

  function control(k, row) {
    const o = opt(k), cur = own(k), val = cur ?? inherited(k), cls = cur == null ? 'inherited' : 'own';
    const disabled = !!row?.disabled || undefined;
    const set = (v) => edit({ set: k, value: v });
    const common = { id: 'os-' + k, 'data-key': k, class: cls, disabled };
    if (o.control === 'bool') return h('input', { ...common, type: 'checkbox', checked: val === '1', onchange: (e) => set(e.target.checked ? '1' : '0') });
    if (o.control === 'enum') {
      const items = row?.choices?.length ? o.enum.filter((x) => row.choices.includes(x.value)) : o.enum;
      return h('select', { ...common, onchange: (e) => set(e.target.value) }, items.map((x) => h('option', { value: x.value, selected: x.value === val }, x.label)));
    }
    const pc = o.type === 'percent', num = o.control === 'number' && !pc;  // percent values are text such as "15%"
    return h('input', { ...common, type: num ? 'number' : 'text', ...(num ? { step: o.type === 'int' ? '1' : 'any', min: o.min, max: o.max } : {}), value: val, size: 8,
      onchange: (e) => { const t = e.target.value.trim(); if (t !== '') set(pc && !t.endsWith('%') ? t + '%' : t); } });
  }

  function row(k, viewRow) {
    const o = opt(k); if (!o) return null;
    const cur = own(k);
    return h('div', { class: 'orow' + (cur != null ? ' set' : ''), 'data-row': k },
      h('label', { for: 'os-' + k, title: o.tooltip || '' }, viewRow?.label || o.label),
      control(k, viewRow), o.unit ? h('small', { class: 'muted' }, o.unit) : null,
      cur != null ? h('button', { type: 'button', class: 'icon-btn sm', 'aria-label': `Use the preset value for ${o.label}`, title: `Back to the preset: ${inherited(k)}`, onclick: () => edit({ reset: [{ key: k }] }) }, '↺') : null);
  }

  function render() {
    if (!view) return clear(box, h('h2', { style: 'margin:0' }, '3 · Object settings'), h('p', { class: 'muted', style: 'margin:0' }, eng() ? 'Loading the settings…' : 'Waiting for the slicing engine…'));
    const rows = new Map(view.object.groups.flatMap((g) => g.rows).map((r) => [r.key, r]));
    const shown = new Set();
    const common = COMMON.map(([title, keys]) => {
      const rs = keys.filter((k) => opt(k) && !(SUPPORT_KEYS.test(k) && k !== 'enable_support' && (own('enable_support') ?? inherited('enable_support')) !== '1' && !rows.get(k)?.value));
      rs.forEach((k) => shown.add(k));
      const supportOff = title === 'Supports' && (own('enable_support') ?? inherited('enable_support')) !== '1';
      return h('fieldset', { class: 'ogroup' }, h('legend', null, title),
        supportOff ? h('small', { class: 'muted' }, 'Supports are off for this print. Switch them on to see more settings. Painted support turns them on by itself.') : null,
        rs.map((k) => row(k, rows.get(k))));
    });
    const extra = [...rows.keys()].filter((k) => !shown.has(k));
    const addList = view.object.add.filter((g) => g.title !== 'Frequent').map((g) => h('optgroup', { label: g.title },
      g.keys.filter((k) => opt(k) && !shown.has(k) && own(k) == null).map((k) => h('option', { value: k }, opt(k).label))));
    const picker = h('select', { id: 'os-add', 'aria-label': 'Add an object setting', onchange: (e) => { const k = e.target.value; if (k) edit({ add: k }); } },
      h('option', { value: '' }, 'Add a setting…'), addList);
    const issues = view.issues.filter((i) => i.severity !== 'info');
    const n = Object.keys(S.obj).length;
    clear(box,
      h('div', { class: 'row between' }, h('h2', { style: 'margin:0' }, '3 · Object settings'),
        n ? pill(`${n} changed`, 'ok') : h('small', { class: 'muted' }, 'Optional')),
      h('p', { class: 'muted', style: 'margin:0' }, 'These apply to the model in all three slices. A value you change replaces the preset value. ↺ returns to the preset.'),
      h('details', { class: 'adv', open: S.ovOpen, ontoggle: (e) => (S.ovOpen = e.target.open) },
        h('summary', null, n ? `Settings (${n} changed)` : 'Settings'),
        h('div', { class: 'stack', style: 'gap:12px;margin-top:10px' },
          paintSummary(),
          common,
          extra.length ? h('fieldset', { class: 'ogroup' }, h('legend', null, 'More settings'), extra.map((k) => row(k, rows.get(k)))) : null,
          h('div', { class: 'row', style: 'gap:8px;flex-wrap:wrap' }, picker,
            n ? btn('Reset all', () => { S.obj = {}; load().then(onChange); }, 'sm') : null),
          notice ? h('small', { class: 'muted', id: 'os-notice' }, notice) : null,
          issues.map((i) => h('div', { class: 'hms ' + (i.severity === 'error' ? 'err' : 'warn'), 'data-issue': i.id },
            h('div', { class: 'txt' }, i.message, i.fix && i.fixable !== false ? [' ', btn(i.fixLabel || 'Fix', () => edit({ apply: i.fix }), 'sm')] : null))))));
  }

  render();
  load().catch((e) => { clear(box, h('h2', { style: 'margin:0' }, '3 · Object settings'), h('p', { class: 'muted' }, 'The settings could not be read: ' + e.message)); });
  return box;
}
