// The steps of a print start (upload, printer download, preparation, printing) and the slicing percent.
import { api } from '../api.js';
import { h, clear, icon } from '../util.js';

const LABEL = { upload: 'Upload to the Bambu cloud', download: 'Printer downloads the plate', prepare: 'Printer prepares', print: 'Printing' };
const STAGE = {
  auto_bed_leveling: 'Leveling the bed', heatbed_preheating: 'Heating the bed', sweeping_xy_mech_mode: 'Checking the axes',
  changing_filament: 'Loading filament', heating_hotend: 'Heating the nozzle', calibrating_extrusion: 'Calibrating extrusion',
  scanning_bed_surface: 'Scanning the bed', identifying_build_plate_type: 'Identifying the plate', calibrating_micro_lidar: 'Calibrating the lidar',
  homing_toolhead: 'Homing the toolhead', cleaning_nozzle_tip: 'Cleaning the nozzle', checking_extruder_temperature: 'Checking the nozzle temperature',
  calibrating_extrusion_flow: 'Calibrating flow', filament_unloading: 'Unloading filament', filament_loading: 'Loading filament',
  calibrating_motor_noise: 'Calibrating the motors',
};
const detailText = (s) => (s.id === 'prepare' ? STAGE[s.detail] || (s.detail || '').replaceAll('_', ' ') : s.detail);

export function stepsView(steps) {
  return h('ol', { class: 'start-steps', 'aria-label': 'Print start' }, steps.map((s) => {
    const note = [s.percent != null ? Math.round(s.percent) + '%' : null, detailText(s)].filter(Boolean).join(' · ');
    return h('li', { class: 'sstep', 'data-step': s.id, 'data-state': s.state, 'aria-current': s.state === 'active' ? 'step' : null },
      h('span', { class: 'mark' }, s.state === 'done' ? icon('check', 14) : s.state === 'active' ? h('span', { class: 'spin' }) : null),
      h('div', { class: 'what' }, h('span', null, LABEL[s.id]), note && h('small', null, note),
        s.state === 'active' && s.percent != null && h('div', { class: 'bar' }, h('i', { style: `width:${Math.min(100, s.percent)}%` }))));
  }));
}

// Shows the steps of a start in `box` until the returned stop() runs. The start request itself only answers when the upload is over.
export function watchStart(serial, box) {
  const tick = async () => {
    const p = (await api.printers().catch(() => [])).find((x) => x.serial === serial);
    if (!stopped) clear(box, p?.start ? stepsView(p.start) : '');
  };
  let stopped = false;
  const timer = setInterval(tick, 2000);
  tick();
  return () => { stopped = true; clearInterval(timer); };
}

// A slice in progress: the percent and the slicer's own text (browser engine and Pi CLI give both).
export function sliceProgress(v) {
  const p = v.progress, pc = p?.percent;
  return h('div', { class: 'slice-progress', 'data-percent': pc ?? '' },
    h('small', null, h('span', { class: 'spin' }), ' ', 'Slicing…', pc != null ? ` ${Math.round(pc)}%` : '', p?.message ? ` · ${p.message}` : '', ` · ${v.elapsed || 0} s`),
    pc != null && h('div', { class: 'bar' }, h('i', { style: `width:${Math.min(100, pc)}%` })));
}
