// The printer cloud filament library: list, edit; the AMS location is read-only and the price is kept by the hub.
// #/filament/<spool id> shows one spool and the prints done with it.
import { api } from '../api.js';
import { h, clear, icon, btn, field, swatch, empty, modal, attempt, money, pill, hex, loading, dateTime } from '../util.js';
import * as D from './detail.js';
import { nozzleName } from './nozzlerow.js';

export async function mount(ctx, root, parts = []) {
  if (parts[0]) return spoolView(ctx, root, decodeURIComponent(parts[0]));
  const list = h('div', { class: 'grid' }, loading());
  let printers = [], spools = [], requests = [], nozzleReqs = [];
  const canEdit = (s) => ctx.me.is_admin || (s?.printer && ctx.canManage(s.printer));
  const canCancel = (r) => ctx.me.is_admin || r.user_id === ctx.me.user_id || ctx.canManage(r.printer);
  const pname = (serial) => printers.find((p) => p.serial === serial)?.name || serial;
  root.append(h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'Filament'), h('div', { class: 'sub' }, 'Spools from your printer cloud filament library, with remaining grams and your prices')),
    ctx.managesAny() && btn([icon('plus', 18), 'Add spool'], () => edit(null), 'primary')), list);

  const where = (s) => (s.printer ? `${pname(s.printer)}${s.unit != null && s.slot != null ? ` · AMS ${s.unit + 1}·${s.slot + 1}` : ''}` : '');
  const known = (n) => n != null;

  async function load() {
    try {
      // A failing request list must not hide the spools.
      [spools, printers, requests, nozzleReqs] = await Promise.all([api.spools(), api.printers(), api.loadRequests().catch(() => []), api.nozzleRequests().catch(() => [])]);
    } catch (e) { clear(list, empty(e.message)); return; }
    draw();
  }

  function card(s) {
    const mine = requests.filter((r) => r.spool_id === s.id);
    const frac = known(s.grams_left) && s.grams_total ? s.grams_left / s.grams_total : null, low = frac != null && frac < 0.15;
    return h('section', { class: 'card spool' },
      swatch(s.colour, 44),
      h('div', { class: 'spool-main' }, h('div', { class: 'row between' }, h('b', { class: 'ell' }, h('a', { href: '#/filament/' + s.id, style: 'color:inherit;text-decoration:none' }, `${s.brand} ${s.type}`.trim() || s.name)), s.printer ? pill(where(s), 'ok') : pill('not loaded')),
        h('small', { class: 'ell', title: s.name }, s.name),
        mine.map((r) => h('div', null, pill(`Requested for ${pname(r.printer)} by ${r.user_name}`, 'warn'), forJob(r)))),
      h('div', { class: 'bar' + (low ? ' low' : '') }, h('i', { style: `width:${Math.round((frac ?? 0) * 100)}%` })),
      h('div', { class: 'row between', style: 'grid-column:1/-1' },
        h('span', null, known(s.grams_left) ? [h('b', null, Math.round(s.grams_left) + ' g'), known(s.grams_total) && h('small', null, ` of ${s.grams_total} g`)] : h('small', null, 'unknown')),
        h('small', null, s.price_per_kg != null ? money(s.price_per_kg) + ' / kg' : 'no price')),
      (canEdit(s) || mine.some(canCancel)) && h('div', { class: 'acts' },
        mine.filter(canCancel).map((r) => btn('Cancel request', async () => { if (await attempt(() => api.cancelLoad(r.id), 'Request cancelled')) load(); }, 'sm')),
        canEdit(s) && btn([icon('edit', 16), 'Edit'], () => edit(s), 'sm'),
        canEdit(s) && btn([icon('trash', 16), 'Delete'], async () => { if (confirm(`Delete ${s.brand} ${s.type} (${s.name})?`) && await attempt(() => api.deleteSpool(s.id), 'Deleted')) load(); }, 'sm danger')));
  }
  // Requests are made from a queue entry, so each one names its job (older requests have none).
  const forJob = (r) => r.job_id && h('small', null, ' for ', h('a', { href: '#/queue/' + r.job_id }, r.job_name || 'the job'));
  // What is loaded now comes first, by printer and AMS unit; then the other known spools.
  // Open requests to change a printer's nozzle, shown where the owner sees the load requests.
  const nozzleSection = () => nozzleReqs.length > 0 && h('section', { class: 'group', id: 'nozzle-requests', style: 'grid-column:1/-1' }, h('h3', null, 'Nozzle change requests'),
    h('div', { class: 'stack', style: 'gap:8px' }, nozzleReqs.map((r) => h('div', { class: 'card row between', 'data-nozzle-request': r.id },
      h('span', null, `${r.user_name} asks for a ${nozzleName(r)} nozzle in ${pname(r.printer)}`, r.note && `: ${r.note}`, forJob(r)),
      canCancel(r) && btn('Cancel request', async () => { if (await attempt(() => api.cancelNozzle(r.id), 'Request cancelled')) load(); }, 'sm')))));
  function draw() {
    if (!spools.length) return clear(list, nozzleSection(), empty('No spools in the printer cloud filament library yet.'));
    const loaded = spools.filter((s) => s.printer).sort((a, b) => pname(a.printer).localeCompare(pname(b.printer)) || (a.unit ?? -1) - (b.unit ?? -1) || (a.slot ?? -1) - (b.slot ?? -1));
    const groups = new Map();
    for (const s of loaded) { const k = `${pname(s.printer)}${s.unit != null ? ` · AMS ${s.unit + 1}` : ''}`; (groups.get(k) || groups.set(k, []).get(k)).push(s); }
    const other = spools.filter((s) => !s.printer);
    clear(list, nozzleSection(), h('div', { class: 'stack', style: 'grid-column:1/-1' },
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
        field('Price per kg (EUR)', f.price, 'Kept by Druckwerk; the printer cloud has no price.'),
        !s && accounts.length > 1 && field('Cloud account', f.account),
        s?.printer && h('p', { class: 'muted' }, `Loaded in ${where(s)}. The printer cloud keeps the AMS location.`),
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
  if (!spool) return clear(box, empty('This spool is not in the printer cloud filament library (any more).'));
  const pname = (serial) => printers.find((p) => p.serial === serial)?.name || serial;
  const frac = spool.grams_left != null && spool.grams_total ? spool.grams_left / spool.grams_total : null;
  const total = prints.reduce((n, p) => n + (p.spool_grams || 0), 0);
  const rows = prints.map((p) => h('a', { class: 'spoolprint', href: '#/history/' + encodeURIComponent(p.id) },
    h('span', null, h('b', null, p.job?.name || p.title || 'Untitled print'), h('br'), h('small', { class: 'muted' }, [p.printer_name, dateTime(p.when)].filter(Boolean).join(' · '))),
    h('span', { class: 'muted' }, Math.round(p.spool_grams * 10) / 10 + ' g')));
  clear(box, D.page({
    title: `${spool.brand} ${spool.type}`.trim() || spool.name, sub: spool.name,
    pills: [spool.printer ? pill(`${pname(spool.printer)}${spool.unit != null && spool.slot != null ? ` · AMS ${spool.unit + 1}·${spool.slot + 1}` : ''}`, 'ok') : pill('not loaded')],
    image: h('div', { class: 'detail-img', style: 'display:grid;place-items:center' }, swatch(spool.colour, 120)),
    facts: D.facts([['Remaining', spool.grams_left != null ? Math.round(spool.grams_left) + ' g' + (frac != null ? ` (${Math.round(frac * 100)}%)` : '') : 'unknown'],
      ['Spool size', spool.grams_total != null ? spool.grams_total + ' g' : null], ['Price', spool.price_per_kg != null ? money(spool.price_per_kg) + ' / kg' : 'no price'],
      ['Prints', prints.length ? `${prints.length} · ${Math.round(total)} g` : null]]),
    blocks: [h('section', { class: 'card stack', style: 'gap:6px', id: 'spool-prints' }, h('h2', { style: 'margin:0' }, 'Prints with this spool'),
      rows.length ? rows : h('p', { class: 'muted', style: 'margin:0' }, 'No finished print used this spool yet.'))],
  }));
}
