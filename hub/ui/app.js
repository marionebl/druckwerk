// Shell: identity, hash router, tabs, polling.
import { api, RESTARTING, setUser, newRoute, stale, uploading } from './api.js';
import { h, clear, icon, fail, empty, loading } from './util.js';
import { settle } from './views/promote.js';

const VIEWS = {
  printers: () => import('./views/printers.js'),
  queue: () => import('./views/queue.js'),
  history: () => import('./views/history.js'),
  new: () => import('./views/newprint.js'),
  filament: () => import('./views/filament.js'),
  access: () => import('./views/access.js'),
  setup: () => import('./views/setup.js'),
};

const ctx = { me: null, timers: [], leave: [] };
ctx.isAdmin = () => !!ctx.me?.is_admin;
ctx.role = (serial) => (ctx.me.is_admin ? 'admin' : ctx.me.printers.find((p) => p.serial === serial)?.role || null);
ctx.canManage = (serial) => ctx.role(serial) === 'admin';
ctx.managesAny = () => ctx.me.is_admin || ctx.me.printers.some((p) => p.role === 'admin');
// Run fn now and every ms until the route changes. A hidden tab does not poll (it runs once when the tab shows again),
// and a tick is skipped while the last call is still pending.
ctx.every = (ms, fn) => {
  let busy = false;
  const run = async () => {
    if (document.hidden || busy) return;
    busy = true;
    try { await fn(); } finally { busy = false; }
  };
  const id = setInterval(run, ms);
  ctx.timers.push(id);
  const onShow = () => {
    if (!ctx.timers.includes(id)) document.removeEventListener('visibilitychange', onShow);  // the route changed
    else run();
  };
  document.addEventListener('visibilitychange', onShow);
  run();
};
ctx.go = (hash) => { location.hash = hash; };
// Run fn when the route changes: release what the view holds outside its own DOM (workers, GL contexts).
ctx.onLeave = (fn) => { ctx.leave.push(fn); };
// Reslice hands the next New print screen what to start from ({slice_job, model, name, options, target, quality, tray, bed});
// New print reads it once on mount and clears it.
ctx.reslice = null;

// HA shows the app in an iframe of its app panel (frontend ha-panel-app.ts) at /app/<slug><path>. The panel sends that
// path in home-assistant/properties and moves its own URL on home-assistant/navigate, so /app/<slug>/history/123 is a deep link.
let haRoute = null;
const inPanel = window.parent !== window;
const toHa = () => {
  if (!haRoute) return;
  const path = location.hash.replace(/^#/, '') || '/';
  if (path === (haRoute.path || '/')) return;
  window.parent.postMessage({ type: 'home-assistant/navigate', path: haRoute.prefix + path, options: { replace: !haRoute.path } }, '*');
};
if (inPanel) {
  document.documentElement.classList.add('in-panel');
  addEventListener('message', (e) => {
    if (e.source !== window.parent || e.data?.type !== 'home-assistant/properties' || !e.data.route) return;
    const first = !haRoute;
    haRoute = e.data.route;
    if (haRoute.path && '#' + haRoute.path !== location.hash) location.hash = haRoute.path;  // HA's URL wins: first load, back button
    else if (first) toHa();
  });
  window.parent.postMessage({ type: 'home-assistant/subscribe-properties' }, '*');
}

const app = document.getElementById('app');
let main, tabs, menuPop;

// skeleton: the frame before api/me has answered (no name, the admin tabs come with the answer).
function shell(skeleton) {
  const nav = [['printers', 'Printers', 'printer'], ['queue', 'Queue', 'queue'], ['history', 'History', 'clock'], ['new', 'New print', 'plus'], ['filament', 'Filament', 'spool']];
  if (!skeleton && ctx.isAdmin()) nav.push(['access', 'Access', 'key']);
  tabs = h('nav', { class: 'tabs', 'aria-label': 'Sections' },
    nav.map(([id, label, ic]) => h('a', { href: '#/' + id, 'data-id': id }, icon(ic), h('span', null, label))));
  menuPop = h('div', { class: 'pop', hidden: true },
    !skeleton && ctx.isAdmin() && h('a', { href: '#/setup', onclick: () => (menuPop.hidden = true) }, 'Setup wizard'),
    h('button', { onclick: () => location.reload() }, 'Reload'));
  const menu = h('div', { class: 'menu' },
    h('button', { class: 'icon-btn', 'aria-label': 'Settings', 'aria-haspopup': 'true', onclick: () => (menuPop.hidden = !menuPop.hidden) }, icon('gear')), menuPop);
  document.addEventListener('click', (e) => { if (!menu.contains(e.target)) menuPop.hidden = true; });
  main = h('main', { id: 'view' });
  clear(app,
    h('header', { class: 'topbar' }, h('div', { class: 'in' },
      h('div', { class: 'brand' }, h('i'), 'Druckwerk'), tabs,
      h('div', { class: 'who' }, h('span', { class: 'nm' }, skeleton ? '' : ctx.me.name, !skeleton && ctx.isAdmin() ? ' · admin' : ''), !skeleton && ctx.isAdmin() && menu))),
    main);
  if (skeleton) clear(main, loading());
}

// Home Assistant restarts: say so once, probe with backoff, then reload (an update may have changed the view files; the loaded api.js is old).
let banner = null;
addEventListener('druckwerk:restarting', (e) => {
  if (banner) return;
  banner = h('div', { class: 'restart', role: 'status', 'aria-live': 'polite' }, h('span', { class: 'spin' }), ' ', e.detail || RESTARTING);
  document.body.prepend(banner);
  let wait = 2000;
  const probe = async () => {
    try { await api.me(); } catch (e) {
      if (e.retry || e.status === 0) { wait = Math.min(wait * 1.6, 20000); setTimeout(probe, wait); return; }
    }
    if (uploading()) { setTimeout(probe, 1000); return; }
    location.reload();
  };
  setTimeout(probe, wait);
});

let seq = 0;
// keep: redraw in place after a fresh answer replaced a cached one (no spinner, same scroll position).
async function route(keep) {
  newRoute();
  ctx.timers.splice(0).forEach(clearInterval);
  ctx.leave.splice(0).forEach((fn) => { try { fn(); } catch { /* a view's cleanup must not stop the next route */ } });
  const [path, search] = location.hash.replace(/^#\/?/, '').split('?');
  ctx.query = new URLSearchParams(search || '');  // #/history?printer=<serial>
  const parts = path.split('/').filter(Boolean);
  let id = parts[0] || 'printers';
  if (!Object.hasOwn(VIEWS, id) || (id === 'access' || id === 'setup') && !ctx.isAdmin()) id = 'printers';
  const mine = ++seq;
  tabs.querySelectorAll('a').forEach((a) => (a.getAttribute('data-id') === id ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current')));
  const y = scrollY;
  if (!keep) { clear(main, loading()); scrollTo(0, 0); } else main.style.minHeight = main.offsetHeight + 'px';  // no collapse while the view is rebuilt
  toHa();
  try {
    const mod = await VIEWS[id]();
    if (mine !== seq) return;
    clear(main);
    await mod.mount(ctx, main, parts.slice(1));
    // Decisions need fresh data: the filament view shows cached data as is.
    main.classList.toggle('stale', stale() && id !== 'filament');
    if (keep) scrollTo(0, y);
    main.style.minHeight = '';
  } catch (e) {
    if (mine !== seq) return;
    main.style.minHeight = '';
    clear(main, e.retry ? h('p', { class: 'muted' }, h('span', { class: 'spin' }), ' ', e.message) : empty('Could not load this screen: ' + e.message));
    fail(e);
  }
}

let started = false;
function draw() {
  shell();
  if (!location.hash && ctx.isAdmin() && !ctx.me.setup_done) location.hash = '#/setup';
  if (!started) {
    started = true;
    addEventListener('hashchange', () => route());
    // A cached list was shown and the network answered differently: draw the view again, unless the person is in a dialog.
    // New print keeps the plate in memory: a redraw would remove the models. Then the page only stops being stale.
    addEventListener('druckwerk:fresh', () => {
      if (document.querySelector('dialog[open]') || location.hash.startsWith('#/new')) { if (!stale()) main.classList.remove('stale'); return; }
      route(true);
    });
    // Cached lists keep buttons from acting; the pointer is blocked in CSS, a key press still clicks a focused button.
    app.addEventListener('click', (e) => {
      if (main.classList.contains('stale') && main.contains(e.target) && e.target.closest('button, .btn:not(a)')) { e.preventDefault(); e.stopImmediatePropagation(); }
    }, true);
    addEventListener('druckwerk:settled', () => { if (!stale()) main.classList.remove('stale'); });
  }
  route();
}
async function start() {
  // The cached lists are per user, so they draw only after api/me has said who this is; the frame stands meanwhile.
  if (!ctx.me) shell(true);
  let me;
  try { me = await api.me(); } catch (e) {
    if (ctx.me) return;
    clear(app, h('main', null, e.retry ? h('p', { class: 'muted' }, h('span', { class: 'spin' }), ' ', e.message) : empty('Cannot reach the hub: ' + e.message)));
    return;
  }
  if (ctx.me && JSON.stringify(me) === JSON.stringify(ctx.me)) return;
  ctx.me = me; setUser(me.user_id); draw();
}
// Thin bar at the top of the viewport while some request has been pending for 3 s (fixed: no layout space).
const slowBar = h('div', { id: 'slowbar', hidden: true });
document.body.append(slowBar);
addEventListener('druckwerk:slow', (e) => { slowBar.hidden = e.detail <= 0; });
settle();
start();
