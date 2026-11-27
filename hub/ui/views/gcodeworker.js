// Parses off the main thread; the buffers are transferred, not copied.
import { parse } from './gcodeparse.js';

self.onmessage = (ev) => {
  try {
    const r = parse(ev.data);
    const own = [];
    for (const g of r.groups) own.push(g.positions.buffer, g.layerStart.buffer);
    self.postMessage({ ok: true, result: r }, own);
  } catch (err) {
    self.postMessage({ ok: false, error: String(err && err.message || err) });
  }
};
