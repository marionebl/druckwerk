// The Bambu Cloud filament library: list, edit; the AMS location is read-only and the price is kept by the hub.
// #/filament/<spool id> shows one spool and the prints done with it.
// Sort and filter live in the hash (#/filament?sort=remaining&material=PLA&finish=Matte) and in localStorage per user.
import { api } from '../api.js';
import { h, clear, icon, btn, field, swatch, empty, modal, attempt, money, pill, hex, loading, dateTime } from '../util.js';
import * as D from './detail.js';
import { describe } from './spoolname.js';

export async function mount(ctx, root, parts = []) {
  if (parts[0]) return spoolView(ctx, root, decodeURIComponent(parts[0]));
  const list = h('div', { class: 'grid' }, loading());
  let printers = [], spools = [], requests = [];
  const canEdit = (s) => ctx.me.is_admin || (s?.printer && ctx.canManage(s.printer));
  const canCancel = (r) => ctx.me.is_admin || r.user_id === ctx.me.user_id || ctx.canManage(r.printer);
  const pname = (serial) => printers.find((p) => p.serial === serial)?.name || serial;
  const controls = h('div', { class: 'card filters', id: 'filters', hidden: true });
  root.append(h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'Filament'), h('div', { class: 'sub' }, 'Spools from your Bambu Cloud filament library, with remaining grams and your prices')),
    ctx.managesAny() && btn([icon('plus', 18), 'Add spool'], () => edit(null), 'primary')), controls, list);

  const where = (s) => (s.printer ? `${pname(s.printer)}${s.unit != null && s.slot != null ? ` · AMS ${s.unit + 1}·${s.slot + 1}` : ''}` : '');
  const known = (n) => n != null;

  async function load() {
    try {
      // A failing request list must not hide the spools.
      [spools, printers, requests] = await Promise.all([api.spools(), api.printers(), api.loadRequests().catch(() => [])]);
    } catch (e) { clear(list, empty(e.message)); return; }
    infos.clear();
    if (spools.length) { buildControls(); refresh(); save(); } else draw();
  }

  function card(s) {
    const mine = requests.filter((r) => r.spool_id === s.id), targets = askable(s);
    const frac = known(s.grams_left) && s.grams_total ? s.grams_left / s.grams_total : null, low = frac != null && frac < 0.15;
    return h('section', { class: 'card spool' },
      swatch(s.colour, 44),
      h('div', null, h('div', { class: 'row between' }, h('b', null, h('a', { href: '#/filament/' + s.id, style: 'color:inherit;text-decoration:none' }, `${s.brand} ${s.type}`.trim() || s.name)), s.printer ? pill(where(s), 'ok') : pill('not loaded')),
        h('small', null, s.name),
        h('div', { class: 'spoolcolour' }, `${info(s).colour} · ${info(s).finish}`),
        mine.map((r) => h('div', null, pill(`Requested for ${pname(r.printer)} by ${r.user_name}`, 'warn')))),
      h('div', { class: 'bar' + (low ? ' low' : '') }, h('i', { style: `width:${Math.round((frac ?? 0) * 100)}%` })),
      h('div', { class: 'row between', style: 'grid-column:1/-1' },
        h('span', null, known(s.grams_left) ? [h('b', null, Math.round(s.grams_left) + ' g'), known(s.grams_total) && h('small', null, ` of ${s.grams_total} g`)] : h('small', null, 'unknown')),
        h('small', null, s.price_per_kg != null ? money(s.price_per_kg) + ' / kg' : 'no price')),
      (canEdit(s) || targets.length > 0 || mine.some(canCancel)) && h('div', { class: 'acts' },
        targets.length > 0 && btn([icon('plus', 16), 'Ask to load'], () => ask(s, targets), 'sm'),
        mine.filter(canCancel).map((r) => btn('Cancel request', async () => { if (await attempt(() => api.cancelLoad(r.id), 'Request cancelled')) load(); }, 'sm')),
        canEdit(s) && btn([icon('edit', 16), 'Edit'], () => edit(s), 'sm'),
        canEdit(s) && btn([icon('trash', 16), 'Delete'], async () => { if (confirm(`Delete ${s.brand} ${s.type} (${s.name})?`) && await attempt(() => api.deleteSpool(s.id), 'Deleted')) load(); }, 'sm danger')));
  }
  // Printers with an AMS that the user sees, where the spool is not loaded and not yet requested.
  const askable = (s) => printers.filter((p) => p.ams?.length && p.serial !== s.printer && !requests.some((r) => r.spool_id === s.id && r.printer === p.serial));

  function ask(s, targets) {
    return modal('Ask to load', (close) => {
      const sel = h('select', null, targets.map((p) => h('option', { value: p.serial }, p.name)));
      const note = h('input', { type: 'text', maxlength: 200, placeholder: 'Optional' });
      return h('div', { class: 'body' },
        h('p', null, `Ask the printer owner to put ${`${s.brand} ${s.type}`.trim()} (${s.name}) into the AMS.`),
        field('Printer', sel), field('Note', note),
        btn('Send', async () => { if (await attempt(() => api.askLoad(s.id, sel.value, note.value.trim()), 'Request sent')) { close(true); load(); } }, 'primary'));
    });
  }
  // Sort and filter. Default "Location": what is loaded now comes first, by printer and AMS unit, then the other spools.
  const SORTS = { location: 'Location', remaining: 'Remaining (g)', percent: 'Remaining (%)', name: 'Name', material: 'Material', vendor: 'Vendor' };
  const FILTERS = { material: 'Material', finish: 'Finish' };
  const store = `dw1:${ctx.me.user_id}:filament-view`;
  const infos = new Map();
  const info = (s) => infos.get(s.id) || infos.set(s.id, describe(s)).get(s.id);
  const frac = (s) => (known(s.grams_left) && s.grams_total ? s.grams_left / s.grams_total : null);
  const KEY = { remaining: (s) => s.grams_left, percent: frac, name: (s) => s.name, material: (s) => info(s).material, vendor: (s) => s.brand };
  const state = readState();
  function readState() {
    const q = new URLSearchParams(location.hash.split('?')[1] || '');
    let src = q;
    if (!['sort', 'dir', ...Object.keys(FILTERS)].some((k) => q.has(k))) {
      try { src = new URLSearchParams(localStorage.getItem(store) || ''); } catch { src = new URLSearchParams(); }
    }
    const sort = SORTS[src.get('sort')] ? src.get('sort') : 'location';
    return { sort, desc: src.get('dir') === 'desc', material: new Set((src.get('material') || '').split(',').filter(Boolean)), finish: new Set((src.get('finish') || '').split(',').filter(Boolean)) };
  }
  function save() {
    const q = new URLSearchParams();
    if (state.sort !== 'location') q.set('sort', state.sort);
    if (state.desc && state.sort !== 'location') q.set('dir', 'desc');
    for (const k of Object.keys(FILTERS)) if (state[k].size) q.set(k, [...state[k]].join(','));
    const text = q.toString().replace(/%2C/g, ',');
    try { localStorage.setItem(store, text); } catch { /* the hash still carries it */ }
    ctx.replaceHash('#/filament' + (text ? '?' + text : ''));
  }
  const passes = (s, except) => Object.keys(FILTERS).every((k) => k === except || !state[k].size || state[k].has(info(s)[k]));
  function ordered(items) {
    if (state.sort === 'location') return items;
    const key = KEY[state.sort], dir = state.desc ? -1 : 1;
    const num = state.sort === 'remaining' || state.sort === 'percent';
    // An unknown remaining stays last in both directions.
    return [...items].sort((a, b) => {
      const x = key(a), y = key(b);
      if (num && (x == null || y == null)) return (x == null) - (y == null);
      return dir * (num ? x - y : String(x).localeCompare(String(y))) || a.name.localeCompare(b.name);
    });
  }
  let chips = [], dirBtn, sortSel;
  function buildControls() {
    const options = (k) => [...new Set(spools.map((s) => info(s)[k]))].sort((a, b) => a.localeCompare(b));
    for (const k of Object.keys(FILTERS)) for (const v of [...state[k]]) if (!options(k).includes(v)) state[k].delete(v);
    sortSel = h('select', { onchange: () => { state.sort = sortSel.value; save(); refresh(); } }, Object.entries(SORTS).map(([v, t]) => h('option', { value: v }, t)));
    dirBtn = h('button', { type: 'button', class: 'chip', 'aria-label': 'Sort direction', onclick: () => { state.desc = !state.desc; save(); refresh(); } });
    chips = [];
    const group = (k) => h('div', { class: 'fgroup', role: 'group', 'aria-label': FILTERS[k] }, h('b', null, FILTERS[k]),
      options(k).map((v) => {
        const c = h('button', { type: 'button', class: 'chip', 'aria-pressed': 'false', 'data-filter': k, 'data-value': v, onclick: () => {
          state[k].has(v) ? state[k].delete(v) : state[k].add(v); save(); refresh(); } });
        chips.push([c, k, v]);
        return c;
      }));
    clear(controls,
      h('div', { class: 'fgroup' }, h('label', { class: 'row' }, h('b', null, 'Sort by'), sortSel), dirBtn),
      group('material'), group('finish'),
      h('div', { class: 'fgroup' }, h('button', { type: 'button', class: 'chip', 'data-clear': '1', onclick: clearFilters }, 'Clear filters')));
    controls.hidden = false;
  }
  function clearFilters() { for (const k of Object.keys(FILTERS)) state[k].clear(); save(); refresh(); }
  function refresh() {
    sortSel.value = state.sort;
    const text = !['remaining', 'percent'].includes(state.sort);
    dirBtn.disabled = state.sort === 'location';
    dirBtn.textContent = text ? (state.desc ? 'Z to A' : 'A to Z') : (state.desc ? 'High to low' : 'Low to high');
    for (const [c, k, v] of chips) {
      const n = spools.filter((s) => info(s)[k] === v && passes(s, k)).length;
      c.setAttribute('aria-pressed', String(state[k].has(v)));
      clear(c, v, h('small', null, String(n)));
    }
    controls.querySelector('[data-clear]').hidden = !Object.keys(FILTERS).some((k) => state[k].size);
    draw();
  }
  function draw() {
    if (!spools.length) { controls.hidden = true; return clear(list, empty('No spools in the Bambu Cloud filament library yet.')); }
    const shown = spools.filter((s) => passes(s));
    if (!shown.length) return clear(list, empty('No spool matches these filters. Use Clear filters to see all spools.'));
    if (state.sort !== 'location') return clear(list, h('div', { class: 'grid', style: 'grid-column:1/-1' }, ordered(shown).map(card)));
    const loaded = shown.filter((s) => s.printer).sort((a, b) => pname(a.printer).localeCompare(pname(b.printer)) || (a.unit ?? -1) - (b.unit ?? -1) || (a.slot ?? -1) - (b.slot ?? -1));
    const groups = new Map();
    for (const s of loaded) { const k = `${pname(s.printer)}${s.unit != null ? ` · AMS ${s.unit + 1}` : ''}`; (groups.get(k) || groups.set(k, []).get(k)).push(s); }
    const other = shown.filter((s) => !s.printer);
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

async function spoolView(ctx, root, id) {
  const box = h('div');
  root.append(D.back('#/filament', 'Filament'), box);
  box.append(loading());
  let spool, prints, printers = [];
  try {
    const [all, ps, pr] = await Promise.all([api.spools(), api.printers().catch(() => []), api.spoolPrints(id)]);
    spool = all.find((s) => String(s.id) === id); printers = ps; prints = pr.items;
  } catch (e) { return clear(box, empty('Could not load this spool: ' + e.message)); }
  if (!spool) return clear(box, empty('This spool is not in the Bambu Cloud filament library (any more).'));
  const pname = (serial) => printers.find((p) => p.serial === serial)?.name || serial;
  const info = describe(spool);
  const frac = spool.grams_left != null && spool.grams_total ? spool.grams_left / spool.grams_total : null;
  const total = prints.reduce((n, p) => n + (p.spool_grams || 0), 0);
  const rows = prints.map((p) => h('a', { class: 'spoolprint', href: '#/history/' + encodeURIComponent(p.id) },
    h('span', null, h('b', null, p.job?.name || p.title || 'Untitled print'), h('br'), h('small', { class: 'muted' }, [p.printer_name, dateTime(p.when)].filter(Boolean).join(' · '))),
    h('span', { class: 'muted' }, Math.round(p.spool_grams * 10) / 10 + ' g')));
  clear(box, D.page({
    title: `${spool.brand} ${spool.type}`.trim() || spool.name, sub: spool.name,
    pills: [spool.printer ? pill(`${pname(spool.printer)}${spool.unit != null && spool.slot != null ? ` · AMS ${spool.unit + 1}·${spool.slot + 1}` : ''}`, 'ok') : pill('not loaded')],
    image: h('div', { class: 'detail-img', style: 'display:grid;place-items:center' }, swatch(spool.colour, 120)),
    facts: D.facts([['Colour', info.colour], ['Material', info.material], ['Finish', info.finish], ['Remaining', spool.grams_left != null ? Math.round(spool.grams_left) + ' g' + (frac != null ? ` (${Math.round(frac * 100)}%)` : '') : 'unknown'],
      ['Spool size', spool.grams_total != null ? spool.grams_total + ' g' : null], ['Price', spool.price_per_kg != null ? money(spool.price_per_kg) + ' / kg' : 'no price'],
      ['Prints', prints.length ? `${prints.length} · ${Math.round(total)} g` : null]]),
    blocks: [h('section', { class: 'card stack', style: 'gap:6px', id: 'spool-prints' }, h('h2', { style: 'margin:0' }, 'Prints with this spool'),
      rows.length ? rows : h('p', { class: 'muted', style: 'margin:0' }, 'No finished print used this spool yet.'))],
  }));
}
