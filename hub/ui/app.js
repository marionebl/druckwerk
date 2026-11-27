// Shell: identity, hash router, tabs, polling.
import { api, RESTARTING, setUser, newRoute } from './api.js';
import { h, clear, icon, fail, empty, loading } from './util.js';

const VIEWS = {
  printers: () => import('./views/printers.js'),
  queue: () => import('./views/queue.js'),
  history: () => import('./views/history.js'),
  new: () => import('./views/newprint.js'),
  filament: () => import('./views/filament.js'),
  access: () => import('./views/access.js'),
  setup: () => import('./views/setup.js'),
};

const ctx = { me: null, timers: [] };
ctx.isAdmin = () => !!ctx.me?.is_admin;
ctx.role = (serial) => (ctx.me.is_admin ? 'admin' : ctx.me.printers.find((p) => p.serial === serial)?.role || null);
ctx.canManage = (serial) => ctx.role(serial) === 'admin';
ctx.managesAny = () => ctx.me.is_admin || ctx.me.printers.some((p) => p.role === 'admin');
// Run fn now and every ms until the route changes.
ctx.every = (ms, fn) => { fn(); ctx.timers.push(setInterval(fn, ms)); };
ctx.go = (hash) => { location.hash = hash; };
// Reslice hands the next New print screen what to start from ({slice_job, model, name, options, target, quality, tray, bed});
// New print reads it once on mount and clears it.
ctx.reslice = null;

// The app runs in an iframe of the HA panel, whose own URL never shows our hash. The frame is same-origin with HA,
// so mirror the route into the parent's hash and read it back on load: <HA panel URL>#/history/123 is a deep link.
const outer = () => { try { return window.parent !== window && window.parent.location.origin === location.origin ? window.parent : null; } catch { return null; } };
const readOuterHash = () => { const w = outer(); return w && /^#\//.test(w.location.hash) ? w.location.hash : ''; };
const mirrorHash = () => { try { const w = outer(); if (w && w.location.hash !== location.hash) w.history.replaceState(w.history.state, '', location.hash || '#/'); } catch { /* the panel keeps its own URL */ } };

const app = document.getElementById('app');
let main, tabs, menuPop;

function shell() {
  const nav = [['printers', 'Printers', 'printer'], ['queue', 'Queue', 'queue'], ['history', 'History', 'clock'], ['new', 'New print', 'plus'], ['filament', 'Filament', 'spool']];
  if (ctx.isAdmin()) nav.push(['access', 'Access', 'key']);
  tabs = h('nav', { class: 'tabs', 'aria-label': 'Sections' },
    nav.map(([id, label, ic]) => h('a', { href: '#/' + id, 'data-id': id }, icon(ic), h('span', null, label))));
  menuPop = h('div', { class: 'pop', hidden: true },
    ctx.isAdmin() && h('a', { href: '#/setup', onclick: () => (menuPop.hidden = true) }, 'Setup wizard'),
    h('button', { onclick: () => location.reload() }, 'Reload'));
  const menu = h('div', { class: 'menu' },
    h('button', { class: 'icon-btn', 'aria-label': 'Settings', 'aria-haspopup': 'true', onclick: () => (menuPop.hidden = !menuPop.hidden) }, icon('gear')), menuPop);
  document.addEventListener('click', (e) => { if (!menu.contains(e.target)) menuPop.hidden = true; });
  main = h('main', { id: 'view' });
  clear(app,
    h('header', { class: 'topbar' }, h('div', { class: 'in' },
      h('div', { class: 'brand' }, h('i'), 'Druckwerk'), tabs,
      h('div', { class: 'who' }, h('span', { class: 'nm' }, ctx.me.name, ctx.isAdmin() ? ' · admin' : ''), ctx.isAdmin() && menu))),
    main);
}

// Home Assistant restarts: say so once, probe with backoff, then draw the view again.
let banner = null;
addEventListener('druckwerk:restarting', () => {
  if (banner) return;
  banner = h('div', { class: 'restart', role: 'status', 'aria-live': 'polite' }, h('span', { class: 'spin' }), ' ', RESTARTING);
  document.body.prepend(banner);
  let wait = 2000;
  const probe = async () => {
    try { await api.me(); } catch (e) {
      if (e.retry || e.status === 0) { wait = Math.min(wait * 1.6, 20000); setTimeout(probe, wait); return; }
    }
    banner.remove(); banner = null;
    if (ctx.me) route(); else start();
  };
  setTimeout(probe, wait);
});

let seq = 0;
// keep: redraw in place after a fresh answer replaced a cached one (no spinner, same scroll position).
async function route(keep) {
  newRoute();
  ctx.timers.splice(0).forEach(clearInterval);
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  let id = parts[0] || 'printers';
  if (!VIEWS[id] || (id === 'access' || id === 'setup') && !ctx.isAdmin()) id = 'printers';
  const mine = ++seq;
  tabs.querySelectorAll('a').forEach((a) => (a.getAttribute('data-id') === id ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current')));
  if (!keep) { clear(main, loading()); scrollTo(0, 0); }
  mirrorHash();
  try {
    const mod = await VIEWS[id]();
    if (mine !== seq) return;
    clear(main);
    await mod.mount(ctx, main, parts.slice(1));
  } catch (e) {
    if (mine !== seq) return;
    clear(main, e.retry ? h('p', { class: 'muted' }, h('span', { class: 'spin' }), ' ', RESTARTING) : empty('Could not load this screen: ' + e.message));
    fail(e);
  }
}

let started = false;
async function start() {
  try { ctx.me = await api.me(); setUser(ctx.me.user_id); } catch (e) {
    clear(app, h('main', null, e.retry ? h('p', { class: 'muted' }, h('span', { class: 'spin' }), ' ', RESTARTING) : empty('Cannot reach the hub: ' + e.message)));
    return;
  }
  shell();
  if (!location.hash) { const o = readOuterHash(); if (o) location.hash = o; }
  if (!location.hash && ctx.isAdmin() && !ctx.me.setup_done) location.hash = '#/setup';
  if (!started) {
    started = true;
    addEventListener('hashchange', () => route());
    // A cached list was shown and the network answered differently: draw the view again, unless the person is in a dialog.
    addEventListener('druckwerk:fresh', () => { if (!document.querySelector('dialog[open]')) route(true); });
  }
  route();
}
start();
