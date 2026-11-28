// History: the one list of prints. The Bambu cloud's tasks are the truth, enriched with the Druckwerk job where one matches;
// a job the cloud never saw is listed too. Grouped by day; admins can filter by user and printer. #/history/<id> is one print.
import { api } from '../api.js';
import { h, clear, icon, btn, pill, empty, field, clock, dateTime, durSec, money, toDate, fail, loading, thumb, attempt, staleNote } from '../util.js';
import * as D from './detail.js';
import { gcodePreview } from './preview.js';

const STATUS = { finished: ['Finished', 'ok'], failed: ['Failed', 'err'], cancelled: ['Cancelled', ''], stopped: ['Stopped early', 'err'], running: ['Printing', 'ok'] };
const dayKey = (d) => d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();

function dayLabel(d) {
  const now = new Date(), y = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (dayKey(d) === dayKey(now)) return 'Today';
  if (dayKey(d) === dayKey(y)) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' });
}

// "Started by": the Druckwerk job's owner, else the printer's owner, else the old text.
const startedBy = (t) => t.user_name || (t.user_id ? 'Unknown user' : t.printer_owner ? 'Printer owner: ' + t.printer_owner : 'Not started here');
const titleOf = (t) => t.job?.name || t.title || 'Untitled print';
const SOURCE = { job: ['Druckwerk only', 'warn'], cloud: ['Not started from Druckwerk', ''] };

const DAY_TICKS = [0, 6, 12, 18, 24];

// The bar of one print on its start day's 24 h axis: left and width in percent, or null when the times are missing.
// The day's own midnights bound it (a DST day is not 24 h); a print past midnight is cut there and marked.
function span(t, now) {
  const s = toDate(t.started_at);
  if (!s) return null;
  let e = toDate(t.ended_at);
  if (!e) e = t.status === 'running' ? now : t.seconds ? new Date(s.getTime() + t.seconds * 1000) : null;
  if (!e || e < s) return null;
  const d0 = new Date(s.getFullYear(), s.getMonth(), s.getDate()), d1 = new Date(s.getFullYear(), s.getMonth(), s.getDate() + 1);
  const len = d1 - d0, cut = e > d1;
  const left = (s - d0) / len * 100, width = ((cut ? d1 : e) - s) / len * 100;
  return { left, width, cut };
}

// One print as a timeline row. The excerpt on a printer page drops the printer name and the day bar, and gives the date.
export function entry(t, { showUser = false, excerpt = false } = {}) {
  const [label, cls] = STATUS[t.status] || ['Status ' + t.status, ''];
  const meta = [!excerpt && h('span', null, t.printer_name), h('span', null, excerpt ? dateTime(t.when) : clock(t.when))].filter(Boolean);
  if (t.seconds) meta.push(h('span', null, durSec(t.seconds)));
  if (t.grams) meta.push(h('span', null, Math.round(t.grams) + ' g'));
  if (t.cost != null) meta.push(h('span', null, money(t.cost)));
  if (showUser) meta.push(h('span', { class: 'who-tag' }, icon('user', 14), startedBy(t)));
  const sp = !excerpt && span(t, new Date());
  return h('li', { class: 'tl-item ' + (cls || 'plain') },
    h('a', { class: 'tl-card', href: '#/history/' + encodeURIComponent(t.id) },
      thumb(t.job?.plate_png || t.cover_url),
      h('div', { class: 'main' }, h('div', { class: 'row between' }, h('b', { class: 'ttl' }, titleOf(t)), t.source === 'job' ? pill(...SOURCE.job) : pill(label, cls)),
        h('div', { class: 'meta' }, meta))),
    sp && h('div', { class: 'tl-span', title: label }, h('i', {
      class: 'tl-bar st-' + t.status + (sp.cut ? ' cut' : ''),
      style: `left:min(${sp.left}%, calc(100% - var(--bar-min)));width:${sp.width}%`,
    })));
}

export async function mount(ctx, root, parts = []) {
  if (parts[0]) return detailView(ctx, root, decodeURIComponent(parts[0]));
  const admin = ctx.isAdmin(), showUser = ctx.managesAny();
  // #/history?printer=<serial> opens the list filtered (the printer page links here); the admin selects start from it.
  const filters = { user: ctx.query.get('user') || '', printer: ctx.query.get('printer') || '' };
  const timeline = h('div', { class: 'timeline' });
  const note = h('div');
  const bar = h('div', { class: 'filters' });
  root.append(h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'History'),
    h('div', { class: 'sub' }, admin ? 'All prints: the printer cloud, with Druckwerk jobs added' : 'Your prints: the printer cloud, with Druckwerk jobs added'))), bar, note, h('div', { class: 'stale-host' }, staleNote(), timeline));
  timeline.append(loading());

  if (admin) {
    let users = [], printers = [];
    try { [users, printers] = await Promise.all([api.haUsers(), api.printers()]); } catch (e) { fail(e); }
    const pick = (key, label, opts) => {
      const sel = h('select', { onchange: () => { filters[key] = sel.value; load(); } },
        opts.map(([v, n]) => h('option', { value: v, selected: v === filters[key] }, n)));
      return field(label, sel);
    };
    bar.append(
      pick('user', 'User', [['', 'All users'], ...users.map((u) => [u.id, u.name || u.id]), ['unknown', 'Unknown (not started here)']]),
      pick('printer', 'Printer', [['', 'All printers'], ...printers.map((p) => [p.serial, p.name || p.serial])]));
  } else if (filters.printer) {  // no selects for users: say what the list shows and how to see all of it
    bar.append(h('div', { class: 'row', style: 'gap:8px' }, pill('One printer'), h('a', { href: '#/history' }, 'Show all your prints')));
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

  const axis = () => h('div', { class: 'tl-axis', 'aria-hidden': 'true' }, DAY_TICKS.map((x) => h('span', { style: `left:${x / 24 * 100}%` }, String(x).padStart(2, '0'))));

  function draw(items) {
    if (!items.length) return clear(timeline, empty(admin ? 'No prints match these filters.' : filters.printer ? 'You have no prints on this printer yet.' : 'You have no prints yet.'));
    const days = [];
    for (const t of items) {
      const d = toDate(t.when) || new Date(0);
      let g = days[days.length - 1];
      if (!g || g.key !== dayKey(d)) days.push((g = { key: dayKey(d), label: t.when ? dayLabel(d) : 'Unknown date', items: [] }));
      g.items.push(t);
    }
    clear(timeline, days.map((g) => h('section', { class: 'tl-day' }, h('h2', null, g.label), g.items.some((t) => t.started_at) && axis(), h('ol', null, g.items.map((t) => entry(t, { showUser }))))));
  }

  ctx.every(60000, load);
}

// The 3D preview of the print's own file. Nothing loads until the settings say a file exists; a placeholder stands in until then.
function previewCard(id) {
  const note = h('p', { class: 'muted', style: 'margin:0' }, 'Loading the 3D preview…');
  const box = h('section', { class: 'card stack', style: 'gap:10px' }, h('h2', { style: 'margin:0' }, '3D preview'), note);
  return {
    box,
    none: () => { note.textContent = 'No file is stored for this print, so there is no 3D preview.'; },
    show: () => {
      const prev = gcodePreview();
      api.historyGcode(id).then((t) => { note.remove(); box.append(prev); return prev.load(t); })
        .catch(() => { note.textContent = 'The 3D preview could not be loaded.'; });
    },
  };
}

async function detailView(ctx, root, id) {
  const box = h('div');
  root.append(D.back('#/history', 'History'), box);
  box.append(loading());
  let t, spools = [];
  try {
    const [hist, sp] = await Promise.all([api.history('limit=500'), api.spools().catch(() => [])]);
    t = hist.items.find((i) => String(i.id) === id);
    spools = sp;
  } catch (e) { return clear(box, empty('Could not load this print: ' + e.message)); }
  if (!t) return clear(box, empty('This print is not in the history (any more).'));
  const job = t.job, [label, cls] = STATUS[t.status] || ['Status ' + t.status, ''];
  const source = await D.sliceSource(job);
  const can = job && (ctx.me.is_admin || (job.target === 'any' ? ctx.managesAny() : ctx.canManage(job.printer || job.target)));
  const spoolName = (sid) => { const s = spools.find((x) => String(x.id) === String(sid)); return s ? `${s.brand} ${s.type} ${s.name}`.replace(/\s+/g, ' ').trim() : 'Spool ' + sid; };
  const used = (job?.spools_used || []).map((u) => h('a', { class: 'spoolprint', href: '#/filament/' + encodeURIComponent(u.spool_id) },
    h('span', null, spoolName(u.spool_id)), h('span', { class: 'muted' }, Math.round(u.grams * 10) / 10 + ' g')));
  const secs = t.seconds || job?.print_seconds;
  const settings = h('div', null, h('section', { class: 'card' }, loading()));
  const pv = previewCard(t.id);
  const same = h('span', { style: 'display:contents' }), get = h('span', { style: 'display:contents' });
  clear(box, D.page({
    title: titleOf(t), sub: job?.name && t.title && job.name !== t.title ? 'Cloud title: ' + t.title : null,
    pills: [pill(label, cls), SOURCE[t.source] && pill(...SOURCE[t.source])],
    image: D.plateImage(job?.plate_png, t.cover_url),
    facts: D.facts([
      ['Printer', t.printer_name], ['Started by', startedBy(t)], ['Started', t.started_at ? dateTime(t.started_at) : null],
      ['Finished', t.ended_at ? dateTime(t.ended_at) : null], ['Print time', secs ? durSec(secs) : null],
      ['Filament', t.grams ? Math.round(t.grams) + ' g' : null], ['Cost', t.cost != null ? money(t.cost) : null],
      ['File', job?.source || job?.file],
      ['Error', (t.error || job?.error) && h('span', { style: 'color:var(--err)' }, t.error || job.error)]]),
    blocks: [...D.settingsCards(job), settings, D.card('Spools used', used), pv.box],
    actions: [
      same, get,
      source && btn('Reslice', () => D.reslice(ctx, job, source)),
      can && ['failed', 'cancelled'].includes(job?.state) && btn('Retry', async () => { if (await attempt(() => api.jobAction(job.id, 'retry'), 'Requeued')) ctx.go('#/queue/' + job.id); }),
    ],
  }));
  // The settings come from the file, so they load after the page; the one primary action depends on whether the file is still there.
  api.historySettings(t.id).then((data) => {
    clear(settings, D.slicingCard(data, job));
    data.preview ? pv.show() : pv.none();
    const label = [icon('plus', 18), 'New print job with the same settings'];
    if (data.download) get.append(h('a', { class: 'btn', href: api.historyFileUrl(t.id), download: data.download.name },
      icon('download', 18), data.download.kind === 'model' ? 'Download model' : 'Download sliced 3MF'));
    if (data.can_reprint) same.append(btn(label, () => D.sameJob(ctx, t.id), 'primary'));
    else if (job) same.append(btn(label, () => D.reslice(ctx, job, source), 'primary'));
  }).catch((e) => { pv.none(); clear(settings, D.card('Slicing settings', h('p', { class: 'muted', style: 'margin:0' }, 'Could not load the settings: ' + e.message))); });
}
