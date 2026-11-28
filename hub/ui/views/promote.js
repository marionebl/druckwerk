// Slicing wants threads, and threads want a cross-origin isolated page. Chromium isolates a frame (Document-Isolation-
// Policy); WebKit does not. On a phone or tablet inside a same-origin frame (the Home Assistant sidebar) the app
// therefore moves the top window to its own URL, where COOP+COEP isolate it. The flag stops a second try.
const FLAG = 'dw-promoted';
const store = (fn) => { try { return fn(sessionStorage); } catch { return null; } };

export function isTouchMobile() {
  const touch = matchMedia('(pointer: coarse)').matches || navigator.maxTouchPoints > 0;
  const ua = navigator.userAgent;
  const ios = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);  // iPadOS says Mac
  return touch && (ios || /Android/.test(ua));
}

function topIsSameOrigin() {
  try { return window.top.location.origin === location.origin; } catch { return false; }  // a cross-origin top throws
}

// A top-level isolated page clears the flag, so a later frame may be promoted again.
export function settle() {
  if (window.top === window && crossOriginIsolated) store((s) => s.removeItem(FLAG));
}

export function promote() {
  if (window.top === window || crossOriginIsolated || !isTouchMobile() || !topIsSameOrigin()) return false;
  if (store((s) => s.getItem(FLAG))) return false;
  if (store((s) => (s.setItem(FLAG, '1'), 1)) == null) return false;  // no storage, no guard: stay put
  window.top.location.replace(location.href);
  return true;
}
