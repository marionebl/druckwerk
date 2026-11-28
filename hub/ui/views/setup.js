// Setup wizard: cloud accounts, printers, done.
import { api } from '../api.js';
import { h, clear, icon, btn, field, pill, empty, attempt, toast } from '../util.js';

const PLATES = ['Textured PEI Plate', 'Cool Plate', 'Engineering Plate', 'High Temp Plate', 'Supertack Plate'];
const NOZZLES = ['0.2', '0.4', '0.6', '0.8'];

export async function mount(ctx, root) {
  let step = 1, data;
  const box = h('div', { class: 'stack wizard' });
  root.append(box);

  async function load() { data = await api.setup(); draw(); }

  const stepper = () => h('div', { class: 'steps' }, ['Cloud account', 'Printers', 'Done'].map((l, i) =>
    h('button', { class: 'step' + (i + 1 === step ? ' on' : i + 1 < step ? ' done' : ''), style: 'background:none;border:0;cursor:pointer;padding:0;font:inherit',
      onclick: () => { step = i + 1; draw(); } }, h('i', null, i + 1 < step ? '✓' : i + 1), l)));

  // Step 1
  function accounts() {
    const list = data.accounts.map((a) => h('div', { class: 'row between', style: 'padding:10px 0;border-bottom:1px solid var(--line)' },
      h('div', null, h('b', null, a.email), h('br'), h('small', null, a.daemon_running ? 'Connection running' : 'Connection stopped')),
      h('div', { class: 'row' }, pill(a.logged_in ? 'Logged in' : 'Logged out', a.logged_in ? 'ok' : 'warn'),
        btn('Remove', async () => { if (confirm(`Remove ${a.email}?`) && await attempt(() => api.removeAccount(a.id), 'Account removed')) load(); }, 'sm danger'))));
    return h('section', { class: 'card' }, h('h2', null, 'Logged-in cloud accounts'),
      list.length ? list : h('p', { class: 'muted' }, 'No account yet. Log in with the cloud account that owns your printers.'));
  }
  function loginForm() {
    const email = h('input', { type: 'email', autocomplete: 'username', required: true });
    const pass = h('input', { type: 'password', autocomplete: 'current-password', required: true });
    const code = h('input', { type: 'text', inputmode: 'numeric', autocomplete: 'one-time-code' });
    const codeBox = h('div', { hidden: true, class: 'form' });
    const msg = h('small', null);
    const finish = (r) => {
      if (r.state === 'done') { toast('Logged in', 'ok'); email.value = pass.value = code.value = ''; codeBox.hidden = true; load(); return; }
      codeBox.hidden = false;
      msg.textContent = r.state === 'need_tfa' ? 'Enter the code from your authenticator app (two-factor).' : 'A verification code was sent to your email.';
    };
    clear(codeBox, field('Code', code), msg, btn('Verify', async () => { const r = await attempt(() => api.loginCode(email.value, code.value.trim())); if (r) finish(r); }, 'primary'));
    return h('section', { class: 'card' }, h('h2', null, 'Add an account'),
      h('form', { class: 'form', onsubmit: async (e) => { e.preventDefault(); const r = await attempt(() => api.loginStart(email.value, pass.value)); if (r) finish(r); } },
        h('div', { class: 'form two' }, field('Email', email), field('Password', pass)),
        h('div', null, h('button', { class: 'btn primary', type: 'submit' }, 'Log in')), codeBox));
  }

  // Step 2
  function printerForm(p) {
    const saved = p.configured, c = p.config || {};
    const f = {
      name: h('input', { type: 'text', value: c.name || p.device_name || '' }),
      prefix: h('input', { type: 'text', value: c.ha_prefix || p.suggested_prefix || '', placeholder: 'e.g. workshop_h2s' }),
      plate: h('select', null, PLATES.map((x) => h('option', { selected: x === (c.plate || PLATES[0]) }, x))),
      nozzle: h('select', null, NOZZLES.map((x) => h('option', { selected: x === String(c.nozzle || '0.4') }, x))),
      pause: h('input', { type: 'text', value: (c.pause_entities || []).join(', '), placeholder: 'switch.printer_power, input_boolean.pause_printing' }),
      price: h('input', { type: 'number', step: '0.01', min: 0, value: c.electricity_kwh_price || '' }),
      owner: h('select', { 'data-field': 'owner' }, h('option', { value: '' }, 'No owner')),
    };
    // The owner administers this printer; the list is the HA users.
    api.haUsers().then((us) => f.owner.append(...us.map((u) => h('option', { value: u.id, selected: u.id === c.owner }, u.name || u.id)))).catch(() => {});
    return h('section', { class: 'card' }, h('div', { class: 'row between' }, h('div', null, h('h2', { style: 'margin:0' }, p.device_name), h('small', null, `${p.model} · ${p.serial.slice(0, 4)}…${p.serial.slice(-3)}`)),
      pill(saved ? 'Configured' : 'New', saved ? 'ok' : 'warn')),
      h('div', { class: 'form', style: 'margin-top:14px' },
        h('div', { class: 'form two' }, field('Name', f.name),
          field('Prefix (optional)', f.prefix, 'Only a name for the printer in the hub. If you also use the printer integration, use its entity prefix.')),
        h('div', { class: 'form two' }, field('Build plate', f.plate), field('Nozzle (mm)', f.nozzle)),
        field('Owner', f.owner, 'Administers this printer: manages its queue and spools, gets load requests, and is named for prints nobody started here.'),
        field('Pause entities', f.pause, 'Entity ids, comma separated. The hub turns these on to pause printing, for example a smart plug.'),
        field('Electricity price (EUR per kWh)', f.price, 'Used for the cost estimate. Optional.'),
        h('div', { class: 'row' }, btn('Save printer', async () => {
          const body = { name: f.name.value.trim(), ha_prefix: f.prefix.value.trim(), plate: f.plate.value, nozzle: f.nozzle.value,
            pause_entities: f.pause.value.split(',').map((x) => x.trim()).filter(Boolean), owner: f.owner.value };
          if (f.price.value !== '') body.electricity_kwh_price = +f.price.value;
          if (await attempt(() => api.savePrinter(p.serial, body), 'Printer saved')) load();
        }, 'primary'), saved && btn('Remove', async () => { if (confirm(`Remove ${p.device_name} from the hub?`) && await attempt(() => api.removePrinter(p.serial), 'Printer removed')) load(); }, 'danger'))));
  }

  async function doneStep() {
    const sec = h('section', { class: 'card stack' }, h('h2', { style: 'margin:0' }, 'All set'),
      h('p', { class: 'muted', style: 'margin:0' }, `${data.printers.filter((p) => p.configured).length} printer(s) configured. Give friends access under Access.`));
    try {
      const i = await api.integration(); const shown = h('span', { class: 'secret' }, '•'.repeat(16));
      let open = false;
      sec.append(h('div', { class: 'stack', style: 'gap:8px' }, h('b', null, 'Home Assistant integration'),
        h('p', { class: 'muted', style: 'margin:0' }, i.connected
          ? 'The Home Assistant integration is connected.'
          : 'Install the Druckwerk integration from the Marketplace (HACS) and restart Home Assistant. It then sets itself up from this app.'),
        h('details', null, h('summary', null, 'Manual setup'),
          h('div', { class: 'stack', style: 'gap:8px;margin-top:8px' },
            h('p', { class: 'muted', style: 'margin:0' }, 'Only if the integration does not find this app. Add the “Druckwerk” integration in Settings → Devices & services and enter these two values.'),
            field('Hub URL', h('span', { class: 'secret' }, i.url)),
            field('Secret', h('div', { class: 'row' }, shown, btn(open ? 'Hide' : 'Show', (e) => {
              open = !open; shown.textContent = open ? i.secret : '•'.repeat(16); e.target.textContent = open ? 'Hide' : 'Show';
            }, 'sm'), btn('Copy', () => navigator.clipboard?.writeText(i.secret).then(() => toast('Copied', 'ok'), () => toast('Copy failed', 'error')), 'sm')))))));
    } catch (e) { sec.append(h('small', null, 'Integration secret unavailable: ' + e.message)); }
    const pi = data.plates_inbox;
    if (pi?.enabled) sec.append(h('small', { class: 'muted' }, `Plates inbox ${pi.repo}: ${pi.last_poll ? 'last poll ' + new Date(pi.last_poll * 1000).toLocaleTimeString() : 'not polled yet'}${pi.last_error ? ', last error: ' + pi.last_error : ', no error'}`));
    sec.append(h('div', null, btn('Go to printers', () => ctx.go('#/printers'), 'primary')));
    return sec;
  }

  async function draw() {
    if (!data) return;
    clear(box, h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'Setup'), h('div', { class: 'sub' }, 'Connect your cloud account and printers'))), stepper());
    if (step === 1) box.append(accounts(), loginForm(), h('div', { class: 'row between' }, h('span'), btn('Next: printers', () => { step = 2; draw(); }, 'primary', { disabled: !data.accounts.length })));
    if (step === 2) {
      box.append(...(data.printers.length ? data.printers.map(printerForm) : [empty('No printers found yet. Log in to an account first.')]),
        h('div', { class: 'row between' }, btn('Back', () => { step = 1; draw(); }), btn('Next', () => { step = 3; draw(); }, 'primary')));
    }
    if (step === 3) box.append(await doneStep());
  }
  await load();
}
