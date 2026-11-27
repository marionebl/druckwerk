// Access grants (HA admins).
import { api } from '../api.js';
import { h, clear, icon, btn, field, pill, empty, attempt, dateTime, loading } from '../util.js';

export async function mount(ctx, root) {
  const tableBox = h('div', null, loading()), formBox = h('div');
  let users = [], printers = [], entities = [], grants = [];
  root.append(h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'Access'), h('div', { class: 'sub' }, 'Who may use which printer, and when'))),
    h('div', { class: 'stack' }, tableBox, formBox));
  const pname = (s) => (s === '*' ? 'All printers' : printers.find((p) => p.serial === s)?.name || s);

  async function load() {
    try {
      [grants, users, printers, entities] = await Promise.all([api.grants(), api.haUsers(), api.printers(),
        api.haEntities(['schedule', 'input_boolean', 'binary_sensor', 'person'])]);
    } catch (e) { clear(tableBox, empty(e.message)); return; }
    draw();
  }

  function draw() {
    const ent = (id) => entities.find((e) => e.entity_id === id);
    clear(tableBox, h('section', { class: 'card' }, h('h2', null, 'Grants'),
      grants.length ? h('table', { class: 'stack-sm' },
        h('thead', null, h('tr', null, ['User', 'Printer', 'Role', 'Until', 'Condition', 'Approval', 'Status', ''].map((t) => h('th', null, t)))),
        h('tbody', null, grants.map((g) => h('tr', null,
          h('td', { 'data-l': 'User' }, h('b', null, g.user_name)),
          h('td', { 'data-l': 'Printer' }, pname(g.printer)),
          h('td', { 'data-l': 'Role' }, g.role),
          h('td', { 'data-l': 'Until' }, g.until ? dateTime(g.until) : 'No end'),
          h('td', { 'data-l': 'Condition' }, g.condition_entity ? h('span', null, ent(g.condition_entity)?.name || g.condition_entity, h('br'), h('small', null, g.condition_entity)) : '–'),
          h('td', { 'data-l': 'Approval' }, g.needs_approval ? pill('needed', 'warn') : '–'),
          h('td', { 'data-l': 'Status' }, pill(g.active ? 'Active' : 'Inactive', g.active ? 'ok' : '')),
          h('td', null, h('button', { class: 'icon-btn', 'aria-label': 'Remove grant for ' + g.user_name, onclick: async () => {
            if (confirm(`Remove access for ${g.user_name} on ${pname(g.printer)}?`) && await attempt(() => api.deleteGrant(g.id), 'Grant removed')) load();
          } }, icon('trash')))))))
        : empty('No grants yet. HA admins always have access.')));
    clear(formBox, form());
  }

  function form() {
    const user = h('select', null, users.map((u) => h('option', { value: u.id }, u.name + (u.is_admin ? ' (admin)' : ''))));
    const printer = h('select', null, h('option', { value: '*' }, 'All printers'), printers.map((p) => h('option', { value: p.serial }, p.name)));
    const role = h('select', null, h('option', { value: 'user' }, 'User (own jobs)'));
    const until = h('input', { type: 'datetime-local' });
    const cond = h('input', { type: 'text', list: 'cond-ents', placeholder: 'Search schedule, helper, sensor or person', autocomplete: 'off' });
    const dl = h('datalist', { id: 'cond-ents' }, entities.map((e) => h('option', { value: e.entity_id }, `${e.name} (${e.state})`)));
    const appr = h('input', { type: 'checkbox' });
    cond.style.cssText = 'flex:1;min-width:0';
    const mkSwitch = btn([icon('plus', 18), 'Create switch'], async () => {
      const made = await attempt(() => api.createSwitch(user.value, printer.value), 'Switch created');
      if (!made) return;
      // HA lists the new entity a moment later, so the form adds it itself; the add-grant check needs it in `entities`.
      if (!entities.some((e) => e.entity_id === made.entity_id)) {
        entities.push(made);
        dl.append(h('option', { value: made.entity_id }, `${made.name} (${made.state})`));
      }
      cond.value = made.entity_id;
    });
    return h('section', { class: 'card' }, h('h2', null, 'Add grant'),
      h('div', { class: 'form' },
        h('div', { class: 'form two' }, field('Home Assistant user', user), field('Printer', printer), field('Role', role),
          field('Until', until, 'Leave empty for no end date.')),
        field('Only while this entity is on', h('div', { class: 'row', style: 'flex-wrap:nowrap' }, cond, mkSwitch), 'Optional. A schedule, helper, binary sensor or person decides when the grant applies. Create switch makes a new helper switch for the chosen user and printer.'), dl,
        h('label', { class: 'check' }, appr, h('span', null, h('b', null, 'Jobs need approval'), h('br'), h('small', null, 'An admin approves each job before it can print.'))),
        h('div', null, btn([icon('plus', 18), 'Add grant'], async () => {
          if (cond.value && !entities.some((e) => e.entity_id === cond.value)) return attempt(() => { throw new Error('Pick the condition from the list, or clear it.'); });
          const body = { user_id: user.value, printer: printer.value, role: role.value, needs_approval: appr.checked };
          if (until.value) body.until = new Date(until.value).toISOString();
          if (cond.value) body.condition_entity = cond.value;
          if (await attempt(() => api.addGrant(body), 'Grant added')) load();
        }, 'primary'))));
  }
  load();
}
