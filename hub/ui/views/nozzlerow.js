// New print: the nozzle row, one chip per nozzle of the printer's library. Its height is the same in every state.
import { h, pill } from '../util.js';

export const nozzleName = (n) => `${n.diameter} mm${n.type ? ' ' + n.type.replace(/_/g, ' ') : ''}`;
export const same = (a, b) => !!a && !!b && a.diameter === b.diameter && a.type === b.type;

// The picked nozzle (of this printer), else the installed one, else the first of the library.
export function chosenNozzle(S, ref) {
  const lib = ref?.nozzles || [];
  return lib.find((n) => S.nozzle?.serial === ref.serial && same(n, S.nozzle.pick)) || lib.find((n) => same(n, ref?.nozzle)) || lib[0] || null;
}

export function nozzleRow(S, ref, draw) {
  const lib = ref?.nozzles, pick = chosenNozzle(S, ref), now = ref?.nozzle || null;
  // A change is asked for once the job is queued, so the request belongs to it. The controls look the same checked or not.
  const note = h('input', { type: 'text', id: 'nozzle-note', maxlength: 200, placeholder: 'Note (optional)', value: S.askNozzleNote || '',
    class: 'nnote', oninput: (e) => (S.askNozzleNote = e.target.value) });
  let status = null;
  if (pick && same(pick, now)) status = pill('installed', 'ok');
  else if (pick && !now) status = h('small', { class: 'muted' }, 'The installed nozzle is unknown. Check it before the print starts.');
  else if (pick) status = [h('small', { class: 'muted' }, `The printer has ${now.diameter} mm.`), note,
    h('label', { class: 'row', style: 'gap:6px;font-size:13px;cursor:pointer' }, h('input', { type: 'checkbox', id: 'nozzle-ask', checked: !!S.askNozzle, onchange: (e) => (S.askNozzle = e.target.checked) }),
      h('span', null, 'Ask the owner to change it when queued'))];
  return h('div', { class: 'nozrow', id: 'nozzle-row' }, h('div', { class: 'tag' }, 'Nozzle'),
    h('div', { class: 'chips' },
      !lib ? h('button', { class: 'chip', disabled: true }, '…')
        : !lib.length ? h('small', { class: 'muted' }, 'No nozzle known. Add your nozzles in Setup.')
        : lib.map((n) => h('button', { class: 'chip', 'data-nozzle': n.diameter, 'aria-pressed': String(same(n, pick)),
          onclick: () => { S.nozzle = { serial: ref.serial, pick: n }; S.variants = null; S.pick = null; draw(); } },
          nozzleName(n), same(n, now) && h('small', null, 'installed')))),
    h('div', { class: 'nstat' }, status));
}
