// Printer grid and printer detail.
import { api } from '../api.js';
import { h, clear, icon, ring, btn, empty, dur, clock, attempt, pill, modal, field, toast } from '../util.js';
import { nozzleName } from './nozzlerow.js';
import { statusPill, isPrinting, hmsBanners, amsBlock, temps, timeLeft, finishAt } from './common.js';
import { stepsView, watchStart } from './progress.js';
import { gcodePreview } from './preview.js';
import { entry } from './history.js';

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
// The <img> stays hidden until a frame has loaded, so a printer whose snapshot fails or takes a minute shows the
// placeholder, never a broken image. After a first frame, a failed one keeps the last good frame.
const noImage = (text) => h('div', { class: 'off' }, icon('camera', 28), text);
// The last frame per printer (640 px wide JPEG, in localStorage) shows at once after a reload, labelled with its age.
const frameKey = (p) => 'dw1:frame:' + p.serial;
const readFrame = (p) => { try { const f = JSON.parse(localStorage.getItem(frameKey(p))); return f && f.d && f.t ? f : null; } catch { return null; } };
const ago = (t) => { const sec = Math.max(0, Math.round((Date.now() - t) / 1000)); return sec < 60 ? sec + ' s ago' : sec < 3600 ? Math.floor(sec / 60) + ' min ago' : Math.floor(sec / 3600) + ' h ago'; };
function saveFrame(p, img) {
  try {
    const w = Math.min(640, img.naturalWidth), c = document.createElement('canvas');
    c.width = w; c.height = Math.round(img.naturalHeight * w / img.naturalWidth);
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    localStorage.setItem(frameKey(p), JSON.stringify({ t: Date.now(), d: c.toDataURL('image/jpeg', 0.7) }));
  } catch { /* full, blocked or tainted: no cached frame */ }
}
function still(p, big) {
  const key = p.serial + (big ? ':big' : '');
  let s = stills.get(key);
  if (!s) {
    const img = h('img', { alt: `Camera of ${p.name}` }), text = h('span', null, 'Loading camera image…');
    const holder = h('div', { class: 'still' }, h('div', { class: 'off' }, icon('camera', 28), text), img);
    s = { holder, img, text, running: false, url: p.camera_url, pause: big ? 2000 : 10000, saved: 0 };
    stills.set(key, s);
    const old = readFrame(p);
    if (old) {
      // Overlay label: no layout space. It ticks while the cached frame is the only one.
      const label = h('span', { class: 'ago' }, ago(old.t));
      const tick = setInterval(() => { if (!label.isConnected) clearInterval(tick); else label.textContent = ago(old.t); }, 1000);
      s.label = label; s.tick = tick;
      img.src = old.d; holder.classList.add('ok'); holder.append(label);
    }
  }
  s.url = p.camera_url;
  if (!s.running) {
    s.running = true;
    let timer = null, probe = null;
    const next = (fresh) => {
      clearTimeout(timer);
      if (!s.holder.isConnected) { s.running = false; return; }
      if (document.hidden || s.holder.closest('.live.playing')) { timer = setTimeout(next, 1000); return; }
      if (probe) { probe.onload = probe.onerror = null; probe.src = ''; }  // a fresh request replaces the one in flight
      const pr = probe = new Image();
      const again = () => { probe = null; timer = setTimeout(next, s.pause); };
      pr.onload = () => {
        s.img.src = pr.src; s.holder.classList.add('ok');
        if (s.label) { s.label.remove(); clearInterval(s.tick); s.label = null; }
        if (Date.now() - s.saved > 20000) { s.saved = Date.now(); saveFrame(p, pr); }  // not every frame: encoding costs
        again();
      };
      pr.onerror = () => { if (!s.holder.classList.contains('ok')) s.text.textContent = 'No camera image'; again(); };
      pr.src = s.url + (s.url.includes('?') ? '&' : '?') + 'mode=snapshot' + (fresh ? '&fresh=1' : '') + '&t=' + Date.now();
    };
    s.fresh = () => next(true);
    timer = setTimeout(next, 0);
  }
  return s.holder;
}

// The detail view plays the live stream. H.264 printers send MPEG-TS, which mpegts.js feeds into Media Source
// (ManagedMediaSource on iOS 17.1+); JPEG printers send multipart, which <img> shows. The still stays underneath as
// poster and as fallback. A stream that fails, ends or freezes for STALL_MS starts again with backoff.
// A hidden page drops its stream; the bridge keeps the upstream open for a while, so coming back is quick.
const STALL_MS = 10000;
let mpegtsLib = null;
// Its own console logging is off: a failed stream is an ERROR event here, and a retry follows.
const loadMpegts = () => (mpegtsLib ||= import('../vendor/mpegts.js').then(() => {
  window.mpegts.LoggingControl.applyConfig({ enableAll: false });
  return window.mpegts;
}));

function live(p) {
  const wrap = h('div', { class: 'live' }, still(p, true));
  const url = new URL(p.camera_url, location.href).href;
  let player = null, starting = false, retry = 0, timer = null;
  const playing = () => { wrap.classList.add('playing'); retry = 0; };
  const drop = () => { if (player) { player.destroy(); player = null; } wrap.classList.remove('playing'); };
  const again = () => {
    drop();
    clearTimeout(timer);
    timer = setTimeout(run, Math.min(30000, 1000 * 2 ** retry++));
  };
  const start = async () => {
    if (player || starting || !wrap.isConnected || document.hidden) return;
    if (p.camera_live === 'mjpeg') {
      const img = h('img', { class: 'video', alt: `Live camera of ${p.name}` });
      img.onload = playing;
      img.onerror = again;
      img.src = url + (url.includes('?') ? '&' : '?') + 't=' + Date.now();
      wrap.append(img);
      player = { destroy: () => { img.removeAttribute('src'); img.remove(); }, frozen: () => false };
      return;
    }
    starting = true;
    const mpegts = await loadMpegts().catch(() => null);
    starting = false;
    if (!mpegts?.isSupported() || player || !wrap.isConnected) return;  // no Media Source: the still loop stays
    const video = h('video', { class: 'video', playsinline: '', autoplay: '', muted: '', 'aria-label': `Live camera of ${p.name}` });
    video.muted = true;
    wrap.append(video);
    const pl = mpegts.createPlayer({ type: 'mpegts', isLive: true, hasAudio: false, url },
      { enableStashBuffer: false, liveBufferLatencyChasing: true, liveBufferLatencyMaxLatency: 2, liveBufferLatencyMinRemain: 0.3 });
    pl.attachMediaElement(video);
    pl.on(mpegts.Events.ERROR, again);
    pl.on(mpegts.Events.LOADING_COMPLETE, again);
    video.addEventListener('playing', playing);
    let seen = -1, since = Date.now();
    player = {
      destroy: () => { pl.destroy(); video.remove(); },
      frozen: () => {
        if (video.currentTime !== seen) { seen = video.currentTime; since = Date.now(); }
        return Date.now() - since > STALL_MS;
      },
    };
    pl.load();
    Promise.resolve(pl.play()).catch(() => {});
  };
  const watch = () => {
    if (!wrap.isConnected) { clearTimeout(timer); drop(); return; }
    if (document.hidden) drop();
    else if (player?.frozen()) again();
    else if (!player && !timer) start();
    setTimeout(watch, 1000);
  };
  const run = () => { timer = null; start(); };
  timer = setTimeout(run, 0);
  setTimeout(watch, 1000);
  return wrap;
}

function camera(p, big) {
  const box = h('div', { class: 'cam' + (big ? ' big' : '') });
  if (p.online && p.camera_url) box.append(big ? live(p) : still(p, big));
  else box.append(noImage(p.online ? 'No camera image' : p.online === null ? 'Connecting to the printer…' : 'Printer offline'));
  box.append(h('span', { class: 'badge' }, statusPill(p)));
  if (p.online && p.light != null) box.append(lightButton(p));
  if (p.task_name && isPrinting(p)) box.append(h('div', { class: 'name' }, p.task_name));
  return box;
}

// Units whose slots are all empty (and an empty external spool) are left out; "AMS empty" says why nothing shows.
function filament(p, compact) {
  const loaded = { ...p, ams: (p.ams || []).filter((u) => u.trays.some((t) => t.type)), external: p.external?.type ? p.external : null };
  if (loaded.ams.length || loaded.external) return amsBlock(loaded, compact);
  return p.ams?.length ? h('small', { class: 'muted ams-empty' }, 'AMS empty') : null;
}

// The switch answers at once, like the vendor app: the wanted state shows on click and the command goes out behind it.
// The printer reports the new state seconds later, so the wanted state stays until the report agrees or 30 s pass.
// Commands to one printer go out in click order. A refused command puts the reported state back.
const LIGHT_HOLD_MS = 30000;
const lights = new Map();  // serial -> { on, until, busy, sent: Promise }
const lightOn = (p) => {
  const l = lights.get(p.serial);
  if (l && Date.now() < l.until && l.on !== p.light) return l.on;
  if (l && !l.busy) lights.delete(p.serial);
  return p.light;
};
const showLight = (serial, on) => document.querySelectorAll(`.light-btn[data-serial="${CSS.escape(serial)}"]`).forEach((b) => {
  b.setAttribute('aria-checked', String(!!on)); b.dataset.light = on ? 'on' : 'off';
});
function switchLight(p, b) {
  const prev = lights.get(p.serial);
  const on = b.getAttribute('aria-checked') !== 'true';  // what the person sees flips, also on a button drawn earlier
  const l = { on, until: Date.now() + LIGHT_HOLD_MS, busy: true };
  l.sent = (prev?.sent || Promise.resolve()).then(() => api.setLight(p.serial, on)).then(
    () => {
      l.busy = false; l.until = Date.now() + LIGHT_HOLD_MS;
      // the bridge reuses a still for 30 s; ask for one taken after the switch, so the picture shows the light
      for (const k of [p.serial, p.serial + ':big']) stills.get(k)?.fresh?.();
    },
    (err) => {
      l.busy = false;
      toast(err.message, 'error');
      if (lights.get(p.serial) !== l) return;  // a later click already decides what shows
      lights.delete(p.serial);
      showLight(p.serial, p.light);
    });
  lights.set(p.serial, l);
  showLight(p.serial, on);
}
function lightButton(p) {
  return h('button', { class: 'light-btn', type: 'button', role: 'switch', 'aria-label': 'Chamber light', 'aria-checked': String(!!lightOn(p)),
    title: 'Chamber light', disabled: p.role !== 'admin', 'data-light': lightOn(p) ? 'on' : 'off', 'data-serial': p.serial,
    onclick: (e) => { e.preventDefault(); e.stopPropagation(); switchLight(p, e.currentTarget); } },  // the card around it is a link
    icon('bulb', 18));
}

function card(p) {
  const printing = isPrinting(p);
  return h('a', { class: 'card pcard', href: '#/printers/' + encodeURIComponent(p.serial), 'aria-label': p.name },
    camera(p),
    h('div', { class: 'info' },
      h('div', { class: 'row between' }, h('div', null, h('h2', { style: 'margin:0' }, p.name), h('small', null, p.model)),
        p.queue_length ? pill(`${p.queue_length} queued`) : null),
      p.hms?.length > 0 && h('div', { class: 'hms', role: 'alert' }, icon('alert', 18), h('div', { class: 'txt' }, h('span', null, h('code', null, p.hms[0].code), ' ', p.hms[0].text))),
      p.start && stepsView(p.start),
      printing && h('div', { class: 'pbar' },
        h('div', { class: 'top' }, h('b', null, Math.round(p.progress) + '%'), h('small', null, [timeLeft(p), finishAt(p)].filter(Boolean).join(' · '))),
        h('div', { class: 'bar' }, h('i', { style: `width:${p.progress}%` })),
        h('small', null, `Layer ${p.layer}/${p.total_layers}`)),
      p.online && h('div', { class: 'row between' },
        h('small', null, `Nozzle ${Math.round(p.nozzle_temp)}° · Bed ${Math.round(p.bed_temp)}°`, p.nozzle && h('span', { id: 'nozzle-installed', title: nozzleName(p.nozzle) }, ` · ${p.nozzle.diameter} mm`)),
        p.stale && h('small', { class: 'muted' }, `status from ${Math.max(1, Math.round(p.status_age / 60))} min ago`)),
      filament(p, true)));
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

const RECENT = 5;  // prints in the history excerpt

async function detail(ctx, root, serial) {
  const body = h('div');
  const camHost = h('div');
  // The 3D view and the history excerpt live outside the 5 s redraw: the canvas keeps its view, and they load once a minute.
  const model = currentModel(), recent = h('div');
  let p = null, hist = null;
  root.append(h('a', { class: 'btn sm', href: '#/printers', style: 'margin-bottom:12px' }, icon('back', 16), 'Printers'), body);
  // The history is cached for a minute on the hub, so a faster poll gives nothing new.
  ctx.every(60000, async () => {
    try { hist = await api.history(new URLSearchParams({ printer: serial, limit: RECENT + 1 }).toString()); } catch (e) { hist = { error: e.message }; }
    clear(recent, historyCard(ctx, serial, hist));
    if (p) model.sync(p, hist);
  });
  let camUrl = '', camOnline, last = '';
  ctx.every(5000, async () => {
    let ps, queue;
    try { [ps, queue] = await Promise.all([api.printers(), api.queue()]); } catch (e) { if (!last) clear(body, empty(e.message)); return; }
    p = ps.find((x) => x.serial === serial);
    if (!p) { clear(body, empty('This printer was not found, or you have no access to it.')); return; }
    model.sync(p, hist);
    const sig = JSON.stringify([p, queue.jobs]);
    if (sig === last) return;
    last = sig;
    if (p.camera_url !== camUrl || p.online !== camOnline || !p.online) { camUrl = p.camera_url; camOnline = p.online; clear(camHost, camera(p, true)); }
    const mine = queue.jobs.filter((j) => j.printer === serial || j.target === serial || (j.target === 'any' && j.model === p.model));
    draw(ctx, body, { camHost, model: model.box, recent }, p, mine, () => { last = ''; });
    if (p.light != null) showLight(p.serial, lightOn(p));  // the camera's switch is drawn once, with the camera
  });
}

// The running print in 3D, from the G-code of its history item (the same file as the History detail's preview).
// The item appears when the cloud lists the print, up to a minute after the start. The preview loads once per print.
function currentModel() {
  const box = h('div');
  let shown = null;
  const note = (text) => h('p', { class: 'muted', style: 'margin:0' }, text);
  const card = (...kids) => h('section', { class: 'card stack', style: 'gap:10px' }, h('h2', { style: 'margin:0' }, 'Current print in 3D'), ...kids);
  return {
    box,
    sync(p, hist) {
      const own = p.role === 'admin';  // printer admins see every print of the printer in the history, others only their own
      const run = isPrinting(p) && hist?.items?.find((t) => t.status === 'running');
      const key = !isPrinting(p) ? 'idle' : run ? 'id:' + run.id : hist ? 'wait' : 'loading';
      if (key === shown) return;
      shown = key;
      box.hidden = key === 'idle';  // an empty item would still take a gap in the column
      if (key === 'idle') return clear(box);
      if (!run) return clear(box, card(note(hist?.error ? 'Could not load the history: ' + hist.error
        : hist ? 'The 3D view shows when the history lists this print, up to a minute after the start.' + (own ? '' : ' You see only your own prints.')
          : 'Loading the 3D view…')));
      const msg = note('Loading the 3D view…'), view = card(msg);
      clear(box, view);
      api.historyGcode(run.id).then((text) => {
        if (shown !== key) return;
        const prev = gcodePreview();
        prev.dataset.task = run.id;
        msg.replaceWith(h('a', { href: '#/history/' + encodeURIComponent(run.id) }, run.job?.name || run.title || 'This print'), prev);
        return prev.load(text);
      }).catch(() => { if (shown === key) msg.textContent = 'No file is stored for this print, so there is no 3D view.'; });
    },
  };
}

function historyCard(ctx, serial, hist) {
  const all = h('a', { href: '#/history?printer=' + encodeURIComponent(serial) }, 'All prints on this printer');
  const items = (hist.items || []).slice(0, RECENT);
  return h('section', { class: 'card printer-history' }, h('div', { class: 'row between' }, h('h2', { style: 'margin:0' }, 'History'), all),
    hist.error ? h('p', { class: 'muted' }, 'Could not load the history: ' + hist.error)
      : items.length ? h('ol', { class: 'tl-list' }, items.map((t) => entry(t, { showUser: ctx.managesAny(), excerpt: true })))
        : h('p', { class: 'muted' }, ctx.isAdmin() ? 'No prints on this printer yet.' : 'You have no prints on this printer yet.'));
}

function jobCard(p) {
  const j = p.current_job;
  if (p.start) return h('section', { class: 'card' }, h('h2', null, 'Starting the print'), stepsView(p.start));
  if (!isPrinting(p)) {
    return h('section', { class: 'card' }, h('h2', null, 'Current job'),
      p.online ? h('p', { class: 'muted' }, p.status === 'finish' ? 'The last print is finished. Clear the plate before starting the next one.' : 'Nothing is printing.') : h('p', { class: 'muted' }, p.online === null ? 'Connecting to the printer…' : 'The printer is offline.'));
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
  const steps = h('div');  // the steps of the start while the request runs
  const go = btn([icon('check', 18), `Start “${next.name || next.source}”`], async () => {
    if (!sure.checked) return;
    const stop = watchStart(p.serial, steps);
    const done = await attempt(() => api.startNext(p.serial), 'Starting the next job');
    stop();
    if (done) { sure.checked = false; plateOk.delete(p.serial); refresh(); }
  }, 'primary', { disabled: !sure.checked || isPrinting(p) });
  sure.addEventListener('change', () => {
    sure.checked ? plateOk.add(p.serial) : plateOk.delete(p.serial);
    go.disabled = !sure.checked || isPrinting(p);
  });
  return h('section', { class: 'card' }, h('h2', null, 'Start next print'),
    h('p', { class: 'muted', style: 'margin-top:0' }, `Next in line: ${next.name || next.source} (${next.owner_name})`),
    h('div', { class: 'form' },
      h('label', { class: 'check' }, sure, h('span', null, h('b', null, 'Plate is clear'), h('br'), h('small', null, 'I removed the last print and the right filament is loaded.'))),
      go, steps,
      isPrinting(p) && h('small', null, 'The printer is busy.')));
}

function draw(ctx, body, { camHost, model, recent }, p, mine, refresh) {
  const queueCard = h('section', { class: 'card' }, h('div', { class: 'row between' }, h('h2', { style: 'margin:0' }, 'Queue'), h('a', { href: '#/queue' }, 'Open queue')),
    mine.length ? h('div', { class: 'stack', style: 'margin-top:12px;gap:8px' }, mine.map((j, i) =>
      h('div', { class: 'row between' }, h('span', null, h('b', null, `${i + 1}. `), j.name || j.source, h('small', null, ' · ' + j.owner_name)),
        pill(j.state === 'pending_approval' ? 'needs approval' : j.state, j.state === 'pending_approval' ? 'warn' : ['printing', 'drying'].includes(j.state) ? 'ok' : ''))))
      : h('p', { class: 'muted' }, 'Nothing waiting.'));
  clear(body,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, p.name), h('div', { class: 'sub' }, `${p.model} · ${p.serial.slice(0, 4)}…${p.serial.slice(-3)}`)), statusPill(p)),
    h('div', { class: 'stack', style: 'margin-bottom:16px' }, hmsBanners(p.hms)),
    h('div', { class: 'cols' },
      h('div', { class: 'stack' }, camHost, jobCard(p), model, ctx.canManage(p.serial) && startNext(ctx, p, mine, refresh)),
      h('div', { class: 'stack' },
        h('section', { class: 'card' }, h('h2', null, 'Temperatures'), p.online ? temps(p) : h('p', { class: 'muted' }, p.online === null ? 'Connecting to the printer…' : 'No data while offline.'),
          p.nozzle && h('small', { class: 'muted', id: 'nozzle-installed', style: 'display:block;margin-top:8px' }, 'Installed nozzle: ' + nozzleName(p.nozzle)),
          p.online && p.light != null && h('div', { class: 'row between', style: 'margin-top:12px' }, h('span', null, 'Chamber light'), lightButton(p))),
        (p.ams?.length || p.external) && h('section', { class: 'card' }, h('h2', null, 'Filament'), filament(p)),
        (p.drying || []).map((u) => dryCard(ctx, p, u, refresh)),
        queueCard, recent)));
}
