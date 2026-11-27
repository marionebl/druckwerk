// History: prints from the Bambu cloud on a timeline, grouped by day. Admins can filter by user and printer.
import { api } from '../api.js';
import { h, clear, icon, pill, empty, field, clock, durSec, money, toDate, fail } from '../util.js';

const STATUS = { finished: ['Finished', 'ok'], failed: ['Failed', 'err'], cancelled: ['Cancelled', ''], running: ['Printing', 'ok'] };
const dayKey = (d) => d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();

function dayLabel(d) {
  const now = new Date(), y = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (dayKey(d) === dayKey(now)) return 'Today';
  if (dayKey(d) === dayKey(y)) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' });
}

export async function mount(ctx, root) {
  const admin = ctx.isAdmin(), showUser = ctx.managesAny();
  const filters = { user: '', printer: '' };
  const timeline = h('div', { class: 'timeline' });
  const note = h('div');
  const bar = h('div', { class: 'filters' });
  root.append(h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'History'),
    h('div', { class: 'sub' }, admin ? 'All prints, from the printer cloud' : 'Your prints, from the printer cloud'))), bar, note, timeline);

  if (admin) {
    let users = [], printers = [];
    try { [users, printers] = await Promise.all([api.haUsers(), api.printers()]); } catch (e) { fail(e); }
    const pick = (key, label, opts) => {
      const sel = h('select', { onchange: () => { filters[key] = sel.value; load(); } },
        opts.map(([v, n]) => h('option', { value: v }, n)));
      return field(label, sel);
    };
    bar.append(
      pick('user', 'User', [['', 'All users'], ...users.map((u) => [u.id, u.name || u.id]), ['unknown', 'Unknown (not started here)']]),
      pick('printer', 'Printer', [['', 'All printers'], ...printers.map((p) => [p.serial, p.name || p.serial])]));
  }

  let last = '', mine = 0;
  async function load() {
    const n = ++mine;
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(filters)) if (v) q.set(k, v);
    let data;
    try { data = await api.history(q.toString()); } catch (e) {
      if (n === mine) { clear(timeline, empty('Could not load the history: ' + e.message)); last = ''; }
      return;
    }
    if (n !== mine) return;
    const sig = JSON.stringify(data);
    if (sig === last) return;
    last = sig;
    clear(note, data.unavailable.length > 0 && h('div', { class: 'hms warn', role: 'status' }, icon('alert', 22),
      h('div', { class: 'txt' }, h('span', null, 'The cloud did not answer for: ' + data.unavailable.join(', ') + '. These prints are missing.'))));
    draw(data.items);
  }

  function entry(t) {
    const [label, cls] = STATUS[t.status] || ['Status ' + t.status, ''];
    const meta = [h('span', null, t.printer_name), h('span', null, clock(t.started_at))];
    if (t.seconds) meta.push(h('span', null, durSec(t.seconds)));
    if (t.grams) meta.push(h('span', null, Math.round(t.grams) + ' g'));
    if (t.cost != null) meta.push(h('span', null, money(t.cost)));
    if (showUser) meta.push(h('span', { class: 'who-tag' }, icon('user', 14), t.user_name || (t.user_id ? 'Unknown user' : 'Not started here')));
    return h('li', { class: 'tl-item ' + (cls || 'plain') },
      h('div', { class: 'tl-card' },
        t.cover_url ? h('img', { class: 'thumb', src: t.cover_url, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer', onerror: (e) => e.target.remove() })
          : h('div', { class: 'thumb' }),
        h('div', { class: 'main' }, h('div', { class: 'row between' }, h('b', { class: 'ttl' }, t.title || 'Untitled print'), pill(label, cls)),
          h('div', { class: 'meta' }, meta))));
  }

  function draw(items) {
    if (!items.length) return clear(timeline, empty(admin ? 'No prints match these filters.' : 'You have no prints yet.'));
    const days = [];
    for (const t of items) {
      const d = toDate(t.started_at) || new Date(0);
      let g = days[days.length - 1];
      if (!g || g.key !== dayKey(d)) days.push((g = { key: dayKey(d), label: t.started_at ? dayLabel(d) : 'Unknown date', items: [] }));
      g.items.push(t);
    }
    clear(timeline, days.map((g) => h('section', { class: 'tl-day' }, h('h2', null, g.label), h('ol', null, g.items.map(entry)))));
  }

  ctx.every(60000, load);
}
