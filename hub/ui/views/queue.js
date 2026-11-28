// Queue: jobs grouped by printer, approvals separate, drag or buttons to reorder. #/queue/<job id> is one job.
// Finished jobs live in History (one list for cloud tasks and jobs).
import { api } from '../api.js';
import { h, clear, icon, btn, pill, empty, modal, field, attempt, dateTime, money, loading, thumb, durSec, toast, staleNote } from '../util.js';
import * as D from './detail.js';
import { stepsView, watchStart } from './progress.js';

const WAITING = ['queued', 'pending_approval'];
const STATE = { pending_approval: ['Needs approval', 'warn'], queued: ['Queued', ''], starting: ['Starting', 'ok'], printing: ['Printing', 'ok'], drying: ['Drying', 'ok'], done: ['Done', 'ok'], failed: ['Failed', 'err'], cancelled: ['Cancelled', ''] };

export async function mount(ctx, root, parts = []) {
  if (parts[0]) return detailView(ctx, root, decodeURIComponent(parts[0]));
  const state = { dragging: null };
  const list = h('div', { class: 'stack' }, loading());
  let data = null, names = {}, models = {}, starts = {}, installed = {}, last = '';

  root.append(h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'Queue'),
    h('div', { class: 'sub' }, ctx.isAdmin() ? 'All jobs' : 'Jobs on your printers', ' · finished prints are under ', h('a', { href: '#/history' }, 'History'))),
    h('div', { class: 'row' }, btn([icon('plus', 18), 'New print'], () => ctx.go('#/new'), 'primary'))), h('div', { class: 'stale-host' }, staleNote(), list));

  const isAdminFor = (j) => ctx.me.is_admin || (j.target === 'any' ? ctx.managesAny() : ctx.canManage(j.target));
  const isOwn = (j) => j.owner_user_id === ctx.me.user_id;
  const label = (target) => (target === 'any' ? 'Any compatible printer' : names[target] || target);

  // Why a waiting job cannot start: its nozzle is not the one in the printer. An unknown installed nozzle never blocks.
  function nozzleWhy(j) {
    if (!j.nozzle || j.kind === 'dry') return '';
    const serials = j.target === 'any' ? Object.keys(models).filter((s) => models[s] === j.model) : [j.target];
    const have = serials.map((s) => installed[s]);
    if (!have.length || have.some((d) => !d || Number(d) === Number(j.nozzle))) return '';
    return `This job needs a ${j.nozzle} mm nozzle; the printer has ${have[0]} mm.`;
  }

  async function act(j, action, body, ok) {
    if (await attempt(() => api.jobAction(j.id, action, body), ok)) refresh();
  }

  // Reorder: position is 0-based among the waiting jobs of the same printer (scope), or of all printers for "any" jobs;
  // a user's pool holds only their own jobs.
  const pool = (j) => data.jobs.filter((x) => WAITING.includes(x.state) && (j.target === 'any' || x.target === j.target) && (isAdminFor(j) || isOwn(x)));
  function moveTo(j, neighbour) {
    const at = pool(j).findIndex((x) => x.id === neighbour.id);
    if (at < 0 || neighbour.id === j.id) return;
    return act(j, 'reorder', j.target === 'any' ? { position: at } : { position: at, scope: j.target });
  }
  function step(j, group, dir) {
    const scope = group.filter((x) => isAdminFor(j) || isOwn(x));
    const n = scope[scope.findIndex((x) => x.id === j.id) + dir];
    if (n) moveTo(j, n);
  }

  const unitName = (d) => (d.ams_id < 128 ? 'AMS ' + (d.ams_id + 1) : 'AMS HT');
  function startDryDialog(j) {
    const d = j.dry;
    return modal('Start drying?', (close) => h('div', { class: 'body' },
      h('p', { style: 'margin:0' }, `${label(j.target)}, ${unitName(d)}: ${d.filament || 'any filament'} at ${d.temp}°C for ${d.hours} h.`),
      h('p', { class: 'muted', style: 'margin:0' }, 'The AMS heats up. Prints on this printer wait until the drying ends or you stop it.'),
      h('div', { class: 'row', style: 'gap:8px' },
        btn('Start drying', async () => { if (await attempt(() => api.startJob(j.target, j.id), 'Drying started')) { close(true); refresh(); } }, 'primary'),
        btn('Cancel', () => close(false)))));
  }
  function startDialog(j) {
    if (j.kind === 'dry') return startDryDialog(j);
    const candidates = Object.entries(names).filter(([s]) => ctx.canManage(s) && (j.target === 'any' ? models[s] === j.model : s === j.target));
    return modal('Start this job now', (close) => {
      const sure = h('input', { type: 'checkbox' });
      const sel = h('select', null, candidates.map(([s, n]) => h('option', { value: s }, n)));
      const steps = h('div');  // the steps of the start while the request runs
      const go = btn('Start print', async () => {
        go.disabled = true;
        const stop = watchStart(sel.value, steps);
        const done = await attempt(() => api.startJob(sel.value, j.id), 'Starting');
        stop();
        if (done) { close(true); refresh(); } else go.disabled = !sure.checked;
      }, 'primary', { disabled: true });
      sure.addEventListener('change', () => (go.disabled = !sure.checked));
      return h('div', { class: 'body' }, h('p', { class: 'muted', style: 'margin:0' }, j.name || j.source),
        j.target === 'any' && field('Printer', sel),
        j.target !== 'any' && h('p', { style: 'margin:0' }, 'Printer: ', h('b', null, label(j.target)), h('span', { hidden: true }, sel)),
        h('label', { class: 'check' }, sure, h('span', null, h('b', null, 'Plate is clear'), h('br'), h('small', null, 'The last print is removed and the right filament is loaded.'))), go, steps);
    });
  }
  function moveDialog(j) {
    return modal('Move job', (close) => {
      const opts = [['any:' + j.model, 'Any compatible ' + (j.model || 'printer')], ...Object.entries(names).map(([s, n]) => [s, n])];
      const sel = h('select', null, opts.map(([v, n]) => h('option', { value: v, selected: v === j.target }, n)));
      return h('div', { class: 'body' }, field('Target printer', sel),
        btn('Move', async () => {
          const v = sel.value, any = v.startsWith('any:');
          if (await attempt(() => api.jobAction(j.id, 'move', any ? { target: 'any', model: v.slice(4) } : { target: v, model: models[v] }), 'Job moved')) { close(true); refresh(); }
        }, 'primary'));
    });
  }

  function row(j, group, idx) {
    const [stl, cls] = STATE[j.state] || [j.state, ''];
    const waiting = j.state === 'queued', pending = j.state === 'pending_approval';
    const admin = isAdminFor(j), own = isOwn(j);
    const canOrder = (waiting || pending) && (admin || own);
    const acts = [];
    if (canOrder && waiting) acts.push(
      h('button', { class: 'icon-btn', 'aria-label': 'Move up', disabled: idx === 0, onclick: () => step(j, group, -1) }, icon('up')),
      h('button', { class: 'icon-btn', 'aria-label': 'Move down', disabled: idx === group.length - 1, onclick: () => step(j, group, 1) }, icon('down')));
    if (pending && admin) acts.push(btn('Approve', () => act(j, 'approve', undefined, 'Approved'), 'sm primary'), btn('Reject', () => act(j, 'reject', undefined, 'Rejected'), 'sm danger'));
    const why = waiting ? nozzleWhy(j) : '';
    if (waiting && admin) acts.push(btn('Start', () => startDialog(j), 'sm', { disabled: !!why, title: why || null }), btn('Skip', () => act(j, 'skip', undefined, 'Skipped'), 'sm'),
      j.kind !== 'dry' && btn('Move…', () => moveDialog(j), 'sm'));
    if (j.state === 'drying' && admin) acts.push(btn('Stop drying', () => act(j, 'stop', undefined, 'Drying stopped'), 'sm danger'));
    if (['queued', 'pending_approval', 'starting', 'printing'].includes(j.state) && (admin || own)) acts.push(btn('Cancel', () => act(j, 'cancel', undefined, 'Cancelled'), 'sm danger'));
    const el = h('div', { class: 'job' + (pending ? ' pending' : ''), draggable: canOrder && waiting ? 'true' : null, 'data-id': j.id },
      canOrder && waiting ? h('div', { class: 'grip', title: 'Drag to reorder', 'aria-hidden': 'true' }, icon('grip')) : h('div', { class: 'pos' }, waiting ? idx + 1 : ''),
      j.kind === 'dry' ? h('div', { class: 'thumb' }, '♨') : thumb(j.plate_png),
      h('div', { class: 'main' },
        h('div', { class: 'row between' }, h('span', { class: 'title' }, h('a', { href: '#/queue/' + j.id }, j.name || j.source || j.file)),
          h('span', { class: 'row', style: 'gap:6px' }, why && h('span', { title: why, 'data-nozzle-pill': j.nozzle }, pill(`needs ${j.nozzle} nozzle`, 'warn')), pill(stl, cls))),
        h('div', { class: 'sub' },
          h('span', { class: 'row', style: 'gap:6px' }, h('span', { class: 'avatar' }, (j.owner_name || '?')[0].toUpperCase()), j.owner_name, own && ' (you)'),
          j.grams != null && h('span', null, j.grams.toFixed(0) + ' g'),
          j.cost != null && h('span', null, money(j.cost)),
          j.kind === 'dry' && h('span', null, `${unitName(j.dry)} · ${j.dry.temp}°C · ${j.dry.hours} h`),
          j.options?.timelapse && h('span', null, 'timelapse'),
          j.name && j.file && j.name !== j.file.replace(/\.gcode\.3mf$/, '') && h('span', null, j.source || j.file),
          h('span', null, dateTime(j.finished_at || j.created_at))),
        starts[j.id] && stepsView(starts[j.id]),
        j.error && h('small', { style: 'color:var(--err)' }, j.error)),
      acts.length ? h('div', { class: 'acts' }, acts) : null);
    el.addEventListener('click', (e) => { if (!e.target.closest('button, a, input, select')) ctx.go('#/queue/' + j.id); });
    if (canOrder && waiting) {
      el.addEventListener('dragstart', (e) => { state.dragging = j; el.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', j.id); });
      el.addEventListener('dragend', () => { state.dragging = null; el.classList.remove('dragging'); list.querySelectorAll('.over').forEach((x) => x.classList.remove('over')); });
    }
    el.addEventListener('dragover', (e) => { if (state.dragging && state.dragging.id !== j.id && state.dragging.target === j.target && waiting) { e.preventDefault(); el.classList.add('over'); } });
    el.addEventListener('dragleave', () => el.classList.remove('over'));
    el.addEventListener('drop', (e) => { e.preventDefault(); el.classList.remove('over'); const d = state.dragging; state.dragging = null; if (d) moveTo(d, j); });
    return el;
  }

  function draw() {
    if (!data) return;
    const pending = data.jobs.filter((j) => j.state === 'pending_approval');
    const groups = new Map();
    for (const j of data.jobs) {
      if (j.state === 'pending_approval') continue;
      (groups.get(j.target) || groups.set(j.target, []).get(j.target)).push(j);
    }
    const out = [];
    if (pending.length) out.push(h('section', { class: 'group' },
      h('div', { class: 'section-note' }, `${pending.length} job${pending.length > 1 ? 's' : ''} waiting for approval`),
      pending.map((j, i) => row(j, pending, i))));
    const order = [...groups.keys()].sort((a, b) => (a === 'any') - (b === 'any'));
    for (const t of order) {
      const g = groups.get(t), waitingOnly = g.filter((j) => j.state === 'queued');
      out.push(h('section', { class: 'group' }, h('h3', null, label(t), pill(String(g.length))),
        g.map((j) => row(j, waitingOnly, waitingOnly.indexOf(j)))));
    }
    clear(list, out.length ? out : empty('The queue is empty. Slice something under New print.'));
  }

  async function refresh() {
    try {
      const [q, ps] = await Promise.all([api.queue(), api.printers()]);
      names = Object.fromEntries(ps.map((p) => [p.serial, p.name]));
      models = Object.fromEntries(ps.map((p) => [p.serial, p.model]));
      installed = Object.fromEntries(ps.map((p) => [p.serial, p.nozzle?.diameter]));
      starts = Object.fromEntries(ps.filter((p) => p.start && p.start_job).map((p) => [p.start_job, p.start]));
      const sig = JSON.stringify([q.jobs, names, starts, installed]);
      if (sig === last || state.dragging || document.querySelector('dialog[open]')) return;
      last = sig; data = q; draw();
    } catch (e) { if (!data) clear(list, empty('Could not load the queue: ' + e.message)); }
  }
  ctx.every(8000, refresh);
}

// One job: what it is, how it was sliced, and (while it waits) the things that can still change.
async function detailView(ctx, root, id) {
  const box = h('div');
  root.append(D.back('#/queue', 'Queue'), box);
  box.append(loading());
  let job = null, start = null, names = {}, models = {}, sig = '', dirty = false, source = null, sourceFor = null;
  const label = (target) => (target === 'any' ? 'Any compatible printer' : names[target] || target);
  const isAdminFor = (j) => ctx.me.is_admin || (j.target === 'any' ? ctx.managesAny() : ctx.canManage(j.printer || j.target));

  function draw() {
    const j = job, [stl, cls] = STATE[j.state] || [j.state, ''];
    const waiting = WAITING.includes(j.state), admin = isAdminFor(j), own = j.owner_user_id === ctx.me.user_id;
    const editable = waiting && j.kind !== 'dry' && (admin || own);
    const name = h('input', { type: 'text', maxlength: 80, value: j.name, oninput: () => (dirty = true) });
    const target = h('select', { 'data-field': 'target', onchange: () => (dirty = true) },
      [['any:' + j.model, 'Any compatible ' + (j.model || 'printer')], ...Object.entries(names)].map(([v, n]) =>
        h('option', { value: v, selected: v === j.target || (j.target === 'any' && v === 'any:' + j.model) }, n)));
    const opts = editable ? D.optionsEditor(j.options || {}, () => (dirty = true)) : null;
    const save = btn('Save changes', async () => {
      const body = {}, nm = name.value.trim(), changed = opts.get();
      if (nm && nm !== j.name) body.name = nm;
      if (Object.keys(changed).length) body.options = changed;
      const v = target.value, any = v.startsWith('any:');
      const moved = admin && v !== (j.target === 'any' ? 'any:' + j.model : j.target);
      if (!Object.keys(body).length && !moved) return;
      if (moved && !await attempt(() => api.jobAction(j.id, 'move', any ? { target: 'any', model: v.slice(4) } : { target: v, model: models[v] }))) return;
      if (Object.keys(body).length && !await attempt(() => api.jobAction(j.id, 'edit', body))) return;
      dirty = false; toast('Saved', 'ok'); await load(true);
    }, 'primary');
    const act = (action, ok, to) => async () => { if (await attempt(() => api.jobAction(j.id, action), ok)) { dirty = false; await load(true); if (to) ctx.go(to); } };
    const secs = j.print_seconds || j.slice?.seconds, [optCard, sliceCard] = D.settingsCards(j);
    clear(box, D.page({
      title: j.name || j.source || j.file, sub: j.name && j.file && j.name !== j.file.replace(/\.gcode\.3mf$/, '') ? j.source || j.file : null,
      pills: [pill(stl, cls)],
      image: D.plateImage(j.plate_png),
      facts: D.facts([
        ['Printer', label(j.printer || j.target)], ['Owner', j.owner_name && j.owner_name + (own ? ' (you)' : '')], ['Added', dateTime(j.created_at)],
        ['Started', j.started_at ? dateTime(j.started_at) : null], ['Finished', j.finished_at ? dateTime(j.finished_at) : null],
        ['Print time', secs ? durSec(secs) : null], ['Filament', j.grams != null ? Math.round(j.grams) + ' g' : null],
        ['Cost', j.cost != null ? money(j.cost) : null], ['File', j.source || j.file],
        ['Error', j.error && h('span', { style: 'color:var(--err)' }, j.error)]]),
      blocks: [
        editable ? D.card('Change this job', h('div', { class: 'form' }, field('Name', name), admin && field('Target printer', target)), opts.node, h('div', null, save)) : optCard,
        start && D.card('Starting the print', stepsView(start)),
        sliceCard, D.previewCard(source && j.slice_job)],
      actions: [
        source && btn('Reslice', () => D.reslice(ctx, j, source), 'primary'),
        j.state === 'pending_approval' && admin && btn('Approve', act('approve', 'Approved'), 'primary'),
        j.state === 'pending_approval' && admin && btn('Reject', act('reject', 'Rejected'), 'danger'),
        ['queued', 'pending_approval', 'starting', 'printing'].includes(j.state) && (admin || own) && btn('Cancel job', act('cancel', 'Cancelled'), 'danger'),
        ['failed', 'cancelled'].includes(j.state) && admin && j.kind !== 'dry' && btn('Retry', act('retry', 'Requeued')),
      ],
    }));
  }

  async function load(force) {
    try {
      const [q, ps] = await Promise.all([api.queue(), api.printers()]);
      names = Object.fromEntries(ps.map((p) => [p.serial, p.name]));
      models = Object.fromEntries(ps.map((p) => [p.serial, p.model]));
      const found = [...q.jobs, ...q.history].find((j) => j.id === id);
      if (!found) return clear(box, empty('This job is not in the queue (any more). Finished prints are under History.'));
      start = ps.find((p) => p.start && p.start_job === id)?.start || null;
      const s = JSON.stringify([found, names, start]);
      if (!force && (s === sig || dirty || document.querySelector('dialog[open]'))) return;
      sig = s; job = found;
      if (sourceFor !== job.slice_job) { sourceFor = job.slice_job; source = await D.sliceSource(job); }
      draw();
    } catch (e) { if (!job) clear(box, empty('Could not load this job: ' + e.message)); }
  }
  ctx.every(8000, load);
}
