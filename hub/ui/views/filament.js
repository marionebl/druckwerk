// The Bambu Cloud filament library: list, edit; the AMS location is read-only and the price is kept by the hub.
import { api } from '../api.js';
import { h, clear, icon, btn, field, swatch, empty, modal, attempt, money, pill, hex } from '../util.js';

export async function mount(ctx, root) {
  const list = h('div', { class: 'grid' });
  let printers = [], spools = [];
  const canEdit = (s) => ctx.me.is_admin || (s?.printer && ctx.canManage(s.printer));
  const pname = (serial) => printers.find((p) => p.serial === serial)?.name || serial;
  root.append(h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'Filament'), h('div', { class: 'sub' }, 'Spools from your Bambu Cloud filament library, with remaining grams and your prices')),
    ctx.managesAny() && btn([icon('plus', 18), 'Add spool'], () => edit(null), 'primary')), list);

  const where = (s) => (s.printer ? `${pname(s.printer)}${s.unit != null && s.slot != null ? ` · AMS ${s.unit + 1}·${s.slot + 1}` : ''}` : '');
  const known = (n) => n != null;

  async function load() {
    try { [spools, printers] = await Promise.all([api.spools(), api.printers()]); } catch (e) { clear(list, empty(e.message)); return; }
    draw();
  }

  function card(s) {
    const frac = known(s.grams_left) && s.grams_total ? s.grams_left / s.grams_total : null, low = frac != null && frac < 0.15;
    return h('section', { class: 'card spool' },
      swatch(s.colour, 44),
      h('div', null, h('div', { class: 'row between' }, h('b', null, `${s.brand} ${s.type}`.trim()), s.printer ? pill(where(s), 'ok') : pill('not loaded')),
        h('small', null, s.name)),
      h('div', { class: 'bar' + (low ? ' low' : '') }, h('i', { style: `width:${Math.round((frac ?? 0) * 100)}%` })),
      h('div', { class: 'row between', style: 'grid-column:1/-1' },
        h('span', null, known(s.grams_left) ? [h('b', null, Math.round(s.grams_left) + ' g'), known(s.grams_total) && h('small', null, ` of ${s.grams_total} g`)] : h('small', null, 'unknown')),
        h('small', null, s.price_per_kg != null ? money(s.price_per_kg) + ' / kg' : 'no price')),
      canEdit(s) && h('div', { class: 'acts' }, btn([icon('edit', 16), 'Edit'], () => edit(s), 'sm'),
        btn([icon('trash', 16), 'Delete'], async () => { if (confirm(`Delete ${s.brand} ${s.type} (${s.name})?`) && await attempt(() => api.deleteSpool(s.id), 'Deleted')) load(); }, 'sm danger')));
  }
  // What is loaded now comes first, by printer and AMS unit; then the other known spools.
  function draw() {
    if (!spools.length) return clear(list, empty('No spools in the Bambu Cloud filament library yet.'));
    const loaded = spools.filter((s) => s.printer).sort((a, b) => pname(a.printer).localeCompare(pname(b.printer)) || (a.unit ?? -1) - (b.unit ?? -1) || (a.slot ?? -1) - (b.slot ?? -1));
    const groups = new Map();
    for (const s of loaded) { const k = `${pname(s.printer)}${s.unit != null ? ` · AMS ${s.unit + 1}` : ''}`; (groups.get(k) || groups.set(k, []).get(k)).push(s); }
    const other = spools.filter((s) => !s.printer);
    clear(list, h('div', { class: 'stack', style: 'grid-column:1/-1' },
      [...groups].map(([k, items]) => h('section', { class: 'group' }, h('h3', null, k), h('div', { class: 'grid' }, items.map(card)))),
      other.length > 0 && h('section', { class: 'group' }, h('h3', null, 'Not loaded'), h('div', { class: 'grid' }, other.map(card)))));
  }

  function edit(s) {
    return modal(s ? 'Edit spool' : 'Add spool', (close) => {
      const v = s || { brand: '', type: 'PLA', colour: '1D1D1D', name: '', grams_left: 1000, grams_total: 1000, price_per_kg: 20 };
      const accounts = [...new Set(spools.map((x) => x.account))];
      const f = {
        brand: h('input', { type: 'text', value: v.brand }), type: h('input', { type: 'text', value: v.type }), name: h('input', { type: 'text', value: v.name }),
        colour: h('input', { type: 'color', value: hex(v.colour), style: 'width:100%;height:42px;padding:2px' }),
        left: h('input', { type: 'number', min: 0, value: v.grams_left ?? '' }), total: h('input', { type: 'number', min: 1, value: v.grams_total ?? '' }),
        price: h('input', { type: 'number', min: 0, step: '0.01', value: v.price_per_kg ?? '' }),
        account: h('select', null, accounts.map((a) => h('option', { value: a }, a))),
      };
      return h('div', { class: 'body' },
        h('div', { class: 'form two' }, field('Brand', f.brand), field('Material', f.type)),
        h('div', { class: 'form two' }, field('Name', f.name), field('Colour', f.colour)),
        h('div', { class: 'form two' }, field('Grams left', f.left), field('Spool size (g)', f.total)),
        field('Price per kg (EUR)', f.price, 'Kept by Druckwerk; the Bambu Cloud has no price.'),
        !s && accounts.length > 1 && field('Cloud account', f.account),
        s?.printer && h('p', { class: 'muted' }, `Loaded in ${where(s)}. Bambu Cloud keeps the AMS location.`),
        btn('Save', async () => {
          const num = (el) => (el.value === '' ? undefined : +el.value);
          const all = { brand: f.brand.value.trim(), type: f.type.value.trim(), name: f.name.value.trim(), colour: f.colour.value.slice(1).toUpperCase(),
            grams_left: num(f.left), grams_total: num(f.total) };
          // An edit sends only what changed, so untouched fields are not rewritten in the cloud.
          const body = s ? Object.fromEntries(Object.entries(all).filter(([k, x]) => x !== undefined && x !== s[k])) : Object.fromEntries(Object.entries(all).filter(([, x]) => x !== undefined));
          body.price_per_kg = f.price.value === '' ? null : +f.price.value;
          if (!s && accounts.length > 1) body.account = f.account.value;
          if (await attempt(() => (s ? api.saveSpool(s.id, body) : api.addSpool(body)), 'Saved')) { close(true); load(); }
        }, 'primary'));
    });
  }
  load();
}
