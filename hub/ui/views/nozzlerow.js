// New print: the nozzle row, one chip per nozzle of the printer's library. Its height is the same in every state.
import { api } from '../api.js';
import { h, btn, pill, attempt } from '../util.js';

export const nozzleName = (n) => `${n.diameter} mm${n.type ? ' ' + n.type.replace(/_/g, ' ') : ''}`;
const same = (a, b) => !!a && !!b && a.diameter === b.diameter && a.type === b.type;

// The picked nozzle (of this printer), else the installed one, else the first of the library.
export function chosenNozzle(S, ref) {
  const lib = ref?.nozzles || [];
  return lib.find((n) => S.nozzle?.serial === ref.serial && same(n, S.nozzle.pick)) || lib.find((n) => same(n, ref?.nozzle)) || lib[0] || null;
}

export function nozzleRow(S, ref, draw) {
  if (!S.nozzleReqs) {  // open requests: the row keeps its height while they load
    S.nozzleReqs = [];
    api.nozzleRequests().then((r) => { S.nozzleReqs = r; draw(); }, () => {});
  }
  const lib = ref?.nozzles, pick = chosenNozzle(S, ref), now = ref?.nozzle || null;
  const asked = pick && S.nozzleReqs.find((r) => r.printer === ref.serial && r.diameter === pick.diameter && r.type === pick.type);
  const note = h('input', { type: 'text', id: 'nozzle-note', maxlength: 200, placeholder: 'Note (optional)', style: 'min-height:32px;max-width:220px' });
  let status = null;
  if (pick && same(pick, now)) status = pill('installed', 'ok');
  else if (pick && !now) status = h('small', { class: 'muted' }, 'The installed nozzle is unknown. Check it before the print starts.');
  else if (pick && asked) status = pill('change requested', 'warn');
  else if (pick) status = [h('small', { class: 'muted' }, `The printer has ${now.diameter} mm.`), note,
    btn('Ask owner to change nozzle', async () => {
      const r = await attempt(() => api.askNozzle(ref.serial, { diameter: pick.diameter, type: pick.type, note: note.value.trim() }), 'Request sent');
      if (r) { S.nozzleReqs.push(r); draw(); }
    }, 'sm', { id: 'nozzle-ask' })];
  return h('div', { class: 'nozrow', id: 'nozzle-row' }, h('div', { class: 'tag' }, 'Nozzle'),
    h('div', { class: 'chips' },
      !lib ? h('button', { class: 'chip', disabled: true }, '…')
        : !lib.length ? h('small', { class: 'muted' }, 'No nozzle known. Add your nozzles in Setup.')
        : lib.map((n) => h('button', { class: 'chip', 'data-nozzle': n.diameter, 'aria-pressed': String(same(n, pick)),
          onclick: () => { S.nozzle = { serial: ref.serial, pick: n }; S.variants = null; S.pick = null; draw(); } },
          nozzleName(n), same(n, now) && h('small', null, 'installed')))),
    h('div', { class: 'nstat' }, status));
}
