// Printer grid and printer detail.
import { api } from '../api.js';
import { h, clear, icon, ring, btn, empty, dur, clock, attempt, pill, swatch, modal, field } from '../util.js';
import { statusPill, isPrinting, hmsBanners, amsBlock, temps, timeLeft, finishAt } from './common.js';

export async function mount(ctx, root, [serial]) {
  if (!ctx.me.setup_done && !ctx.isAdmin()) {
    root.append(empty('The hub has not been set up yet. Ask a Home Assistant admin to open the setup wizard.'));
    return;
  }
  return serial ? detail(ctx, root, decodeURIComponent(serial)) : grid(ctx, root);
}

// The live stream is MPEG-TS for H.264 printers, which <img> cannot show. A still works for every printer: the next one
// is requested only after the last one loaded or failed, and the loop ends when the image leaves the page.
// A still takes the bridge 10 to 60 s without a live tunnel, so the pause only adds to that: 2 s in the detail view, 10 s on cards.
const stills = new Map();
function still(p, big) {
  const key = p.serial + (big ? ':big' : '');
  let s = stills.get(key);
  if (!s) {
    s = { img: h('img', { alt: `Camera of ${p.name}` }), running: false, url: p.camera_url, pause: big ? 2000 : 10000 };
    stills.set(key, s);
  }
  s.url = p.camera_url;
  if (!s.running) {
    s.running = true;
    const next = () => {
      if (!s.img.isConnected) { s.running = false; return; }
      if (document.hidden) { setTimeout(next, 1000); return; }
      const probe = new Image();
      const again = () => setTimeout(next, s.pause);
      probe.onload = () => { s.img.src = probe.src; again(); };
      probe.onerror = again;
      probe.src = s.url + (s.url.includes('?') ? '&' : '?') + 'mode=snapshot&t=' + Date.now();
    };
    setTimeout(next, 0);
  }
  return s.img;
}

function camera(p, big) {
  const box = h('div', { class: 'cam' + (big ? ' big' : '') });
  if (p.online && p.camera_url) box.append(still(p, big));
  else box.append(h('div', { class: 'off' }, icon('camera', 28), p.online ? 'No camera image' : 'Printer offline'));
  box.append(h('span', { class: 'badge' }, statusPill(p)));
  if (p.task_name && isPrinting(p)) box.append(h('div', { class: 'name' }, p.task_name));
  return box;
}

function card(p) {
  const printing = isPrinting(p);
  return h('a', { class: 'card pcard', href: '#/printers/' + encodeURIComponent(p.serial), 'aria-label': p.name },
    camera(p),
    h('div', { class: 'info' },
      h('div', { class: 'row between' }, h('div', null, h('h2', { style: 'margin:0' }, p.name), h('small', null, p.model)),
        p.queue_length ? pill(`${p.queue_length} queued`) : null),
      p.hms?.length > 0 && h('div', { class: 'hms', role: 'alert' }, icon('alert', 18), h('div', { class: 'txt' }, h('span', null, h('code', null, p.hms[0].code), ' ', p.hms[0].text))),
      printing && h('div', { class: 'pbar' },
        h('div', { class: 'top' }, h('b', null, Math.round(p.progress) + '%'), h('small', null, [timeLeft(p), finishAt(p)].filter(Boolean).join(' · '))),
        h('div', { class: 'bar' }, h('i', { style: `width:${p.progress}%` })),
        h('small', null, `Layer ${p.layer}/${p.total_layers}`)),
      p.online && h('div', { class: 'row between' },
        h('small', null, `Nozzle ${Math.round(p.nozzle_temp)}° · Bed ${Math.round(p.bed_temp)}°`)),
      (p.ams?.length || p.external) && amsBlock(p, true)));
}

async function grid(ctx, root) {
  const list = h('div', { class: 'grid' });
  root.append(h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'Printers'), h('div', { class: 'sub' }, 'Live status of your printers')),
    btn([icon('plus', 18), 'New print'], () => ctx.go('#/new'), 'primary')), list);
  let last = '';
  ctx.every(5000, async () => {
    try {
      const ps = await api.printers();
      const sig = JSON.stringify(ps);
      if (sig === last) return;
      last = sig;
      clear(list, ps.length ? ps.map(card) : ctx.isAdmin()
        ? h('div', { class: 'empty' }, 'No printers yet. They appear here automatically when a logged-in cloud account has a printer. ', h('a', { href: '#/setup' }, 'Open setup'), '.')
        : empty('No printers yet. An admin can grant you access, or add printers in the setup wizard.'));
    } catch (e) { if (!last) clear(list, empty(e.message)); }
  });
}

async function detail(ctx, root, serial) {
  const body = h('div');
  const camHost = h('div');
  root.append(h('a', { class: 'btn sm', href: '#/printers', style: 'margin-bottom:12px' }, icon('back', 16), 'Printers'), body);
  let camUrl = '', last = '';
  ctx.every(5000, async () => {
    let ps, queue;
    try { [ps, queue] = await Promise.all([api.printers(), api.queue()]); } catch (e) { if (!last) clear(body, empty(e.message)); return; }
    const p = ps.find((x) => x.serial === serial);
    if (!p) { clear(body, empty('This printer was not found, or you have no access to it.')); return; }
    const sig = JSON.stringify([p, queue.jobs]);
    if (sig === last) return;
    last = sig;
    if (p.camera_url !== camUrl || !p.online) { camUrl = p.camera_url; clear(camHost, camera(p, true)); }
    const mine = queue.jobs.filter((j) => j.printer === serial || j.target === serial || (j.target === 'any' && j.model === p.model));
    draw(ctx, body, camHost, p, mine, () => { last = ''; });
  });
}

function jobCard(p) {
  const j = p.current_job;
  if (!isPrinting(p)) {
    return h('section', { class: 'card' }, h('h2', null, 'Current job'),
      p.online ? h('p', { class: 'muted' }, p.status === 'finish' ? 'The last print is finished. Clear the plate before starting the next one.' : 'Nothing is printing.') : h('p', { class: 'muted' }, 'The printer is offline.'));
  }
  return h('section', { class: 'card' }, h('h2', null, 'Current job'),
    h('div', { class: 'jobbox' },
      ring(p.progress, null, `Layer ${p.layer}/${p.total_layers}`),
      h('div', { class: 'meta' },
        h('div', { class: 't' }, p.task_name || 'Print'),
        h('div', { class: 'big' }, timeLeft(p)),
        h('small', null, p.end_time ? 'Finishes at ' + clock(p.end_time) : ''),
        j && h('small', null, `Started by ${j.owner_name}`),
        h('small', null, p.stage && p.stage !== 'printing' ? p.stage : ''))));
}

// Drying presets of the vendor filament profiles (first entry, the AMS 2 Pro column), capped by the unit's maximum.
const DRY_PRESETS = { PLA: [45, 12], PETG: [65, 12], ABS: [65, 12], ASA: [65, 12], PA: [65, 12], PC: [65, 12], TPU: [65, 12] };

export function confirmDry(p, unit, d, label) {
  return modal('Start drying?', (close) => h('div', { class: 'body' },
    h('p', { style: 'margin:0' }, `${p.name}, ${label}: ${d.filament || 'any filament'} at ${d.temp}°C for ${d.hours} h.`),
    h('p', { class: 'muted', style: 'margin:0' }, 'The AMS heats up. Printing on this printer waits until the drying ends or you stop it.'),
    h('div', { class: 'row', style: 'gap:8px' }, btn('Start drying', () => close(true), 'primary'), btn('Cancel', () => close(false)))));
}

const dryDraft = {}; // form values survive the 5 s redraw
function dryCard(ctx, p, u, refresh) {
  const label = `${u.name} (unit ${u.ams_id < 128 ? u.ams_id + 1 : 'HT'})`;
  const draft = (dryDraft[p.serial + u.ams_id] ||= { filament: 'PLA', temp: Math.min(45, u.temp_max), hours: 12 });
  const active = u.active;
  const f = h('select', { onchange: (e) => { draft.filament = e.target.value; const pr = DRY_PRESETS[draft.filament]; if (pr) { draft.temp = Math.min(pr[0], u.temp_max); draft.hours = pr[1]; temp.value = draft.temp; hours.value = draft.hours; } } },
    [...Object.keys(DRY_PRESETS), ''].map((t) => h('option', { value: t, selected: t === draft.filament }, t || 'Other')));
  const temp = h('input', { type: 'number', min: u.temp_min, max: u.temp_max, step: 1, value: draft.temp, oninput: (e) => (draft.temp = +e.target.value) });
  const hours = h('input', { type: 'number', min: 1, max: 72, step: 1, value: draft.hours, oninput: (e) => (draft.hours = +e.target.value) });
  const body = () => ({ ams_id: u.ams_id, temp: +temp.value, hours: +hours.value, filament: f.value });
  const status = active
    ? `${u.status === 'drying' ? 'Drying' : 'Active'}: ${u.filament || 'filament'} at ${u.temp ?? '–'}°C, ${dur(u.left_min)} left`
    : u.status === 'cooling' ? 'Cooling down' : 'Not drying';
  return h('section', { class: 'card dry' },
    h('div', { class: 'unit-head' }, h('h2', { style: 'margin:0' }, 'Drying: ' + label), pill(active ? 'drying' : u.status, active ? 'ok' : '')),
    h('p', { class: 'dry-state' }, status),
    ctx.canManage(p.serial) && h('div', { class: 'stack', style: 'gap:10px' },
      !active && h('div', { class: 'form two' }, field('Filament', f), field(`Temperature (°C, ${u.temp_min} to ${u.temp_max})`, temp), field('Time (hours, 1 to 72)', hours)),
      h('div', { class: 'row', style: 'gap:8px;flex-wrap:wrap' },
        !active && btn('Start drying', async () => {
          const d = body();
          if (!(await confirmDry(p, u, d, label))) return;
          if (await attempt(() => api.dryNow(p.serial, d), 'Drying started')) refresh();
        }, 'primary', { 'data-act': 'dry-start' }),
        !active && btn('Add to queue', async () => {
          if (await attempt(() => api.queueDry({ target: p.serial, ...body() }), 'Added to the queue')) refresh();
        }),
        active && btn('Stop drying', async () => {
          if (await attempt(() => api.stopDrying(p.serial, u.ams_id), 'Drying stopped')) refresh();
        }, 'danger', { 'data-act': 'dry-stop' }))));
}

const plateOk = new Set(); // survives the 5 s redraw
function startNext(ctx, p, mine, refresh) {
  const next = mine.find((j) => j.state === 'queued');
  if (!next) return null; // nothing to start, so the plate check would only be noise
  if (next.kind === 'dry') {
    const d = next.dry;
    return h('section', { class: 'card' }, h('h2', null, 'Start next job'),
      h('p', { class: 'muted', style: 'margin-top:0' }, `Next in line: ${next.name} (${next.owner_name})`),
      btn([icon('check', 18), 'Start drying'], async () => {
        if (!(await confirmDry(p, null, d, `AMS ${d.ams_id < 128 ? d.ams_id + 1 : 'HT'}`))) return;
        if (await attempt(() => api.startNext(p.serial), 'Drying started')) refresh();
      }, 'primary', { disabled: isPrinting(p) }));
  }
  const sure = h('input', { type: 'checkbox', id: 'plate-clear', checked: plateOk.has(p.serial) });
  const go = btn([icon('check', 18), `Start “${next.name || next.source}”`], async () => {
    if (!sure.checked) return;
    if (await attempt(() => api.startNext(p.serial), 'Starting the next job')) { sure.checked = false; plateOk.delete(p.serial); refresh(); }
  }, 'primary', { disabled: !sure.checked || isPrinting(p) });
  sure.addEventListener('change', () => {
    sure.checked ? plateOk.add(p.serial) : plateOk.delete(p.serial);
    go.disabled = !sure.checked || isPrinting(p);
  });
  return h('section', { class: 'card' }, h('h2', null, 'Start next print'),
    h('p', { class: 'muted', style: 'margin-top:0' }, `Next in line: ${next.name || next.source} (${next.owner_name})`),
    h('div', { class: 'form' },
      h('label', { class: 'check' }, sure, h('span', null, h('b', null, 'Plate is clear'), h('br'), h('small', null, 'I removed the last print and the right filament is loaded.'))),
      go,
      isPrinting(p) && h('small', null, 'The printer is busy.')));
}

function draw(ctx, body, camHost, p, mine, refresh) {
  const queueCard = h('section', { class: 'card' }, h('div', { class: 'row between' }, h('h2', { style: 'margin:0' }, 'Queue'), h('a', { href: '#/queue' }, 'Open queue')),
    mine.length ? h('div', { class: 'stack', style: 'margin-top:12px;gap:8px' }, mine.map((j, i) =>
      h('div', { class: 'row between' }, h('span', null, h('b', null, `${i + 1}. `), j.name || j.source, h('small', null, ' · ' + j.owner_name)),
        pill(j.state === 'pending_approval' ? 'needs approval' : j.state, j.state === 'pending_approval' ? 'warn' : ['printing', 'drying'].includes(j.state) ? 'ok' : ''))))
      : h('p', { class: 'muted' }, 'Nothing waiting.'));
  clear(body,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, p.name), h('div', { class: 'sub' }, `${p.model} · ${p.serial.slice(0, 4)}…${p.serial.slice(-3)}`)), statusPill(p)),
    h('div', { class: 'stack', style: 'margin-bottom:16px' }, hmsBanners(p.hms)),
    h('div', { class: 'cols' },
      h('div', { class: 'stack' }, camHost, jobCard(p), ctx.canManage(p.serial) && startNext(ctx, p, mine, refresh)),
      h('div', { class: 'stack' },
        h('section', { class: 'card' }, h('h2', null, 'Temperatures'), p.online ? temps(p) : h('p', { class: 'muted' }, 'No data while offline.')),
        (p.ams?.length || p.external) && h('section', { class: 'card' }, h('h2', null, 'Filament'), amsBlock(p)),
        (p.drying || []).map((u) => dryCard(ctx, p, u, refresh)),
        queueCard)));
}
