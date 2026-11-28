// Copy of schaltpult web/feedback.js (third host: plain page, used by Druckwerk). Keep the HA and Unity paths unchanged
// so the file can be synced back to Schaltpult. Only the SNAPDOM line differs: here snapdom is vendored, not loaded from a CDN.
// Feedback tool for Mario's smart home. One file for three hosts:
//  - Home Assistant frontend (dashboard resource): a button, then click-select any element.
//  - Schaltpult web page (injected by serve.py): a button, then drag-select a rectangle of the 3D view.
//  - Any other page without unity-canvas (Druckwerk): a button, then click-select any element, send to relative api/feedback.
// A submission is a to-do item in todo.feedback. Its description is JSON with the context. An automation in
// Home Assistant starts a Claude session for each new item.
(function () {
  if (window.__pbFeedback) return;
  window.__pbFeedback = true;

  var HA_EL = document.querySelector("home-assistant");
  var IS_HA = !!HA_EL;
  var SNAPDOM = new URL("vendor/snapdom.mjs", document.baseURI).href;
  var LIST = "todo.feedback";
  // Without unity-canvas the page gets element selection like HA; checked at use time, as Unity may add its canvas later.
  function clickSelect() { return IS_HA || !document.getElementById("unity-canvas"); }

  // Unity clears the WebGL buffer after each shown frame, and it does not draw every display frame, so a later read
  // of the canvas can return nothing. Keeping the buffer makes the read work at any time.
  if (!IS_HA) {
    var getContext = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (type, attrs) {
      if (this.id === "unity-canvas" && /^(experimental-)?webgl2?$/.test(type)) attrs = Object.assign({}, attrs, { preserveDrawingBuffer: true });
      return getContext.call(this, type, attrs);
    };
  }

  // ---------------------------------------------------------------- recent browser errors
  var errors = [];
  function note(kind, text) {
    errors.push({ t: new Date().toISOString(), kind: kind, text: String(text).slice(0, 300) });
    if (errors.length > 15) errors.shift();
  }
  ["error", "warn"].forEach(function (k) {
    var orig = console[k];
    console[k] = function () {
      try { note("console." + k, Array.prototype.map.call(arguments, function (a) { return a && a.message || String(a); }).join(" ")); } catch (e) { /* ignore */ }
      return orig.apply(console, arguments);
    };
  });
  window.addEventListener("error", function (e) { note("error", e.message + " " + (e.filename || "") + ":" + (e.lineno || "")); });
  window.addEventListener("unhandledrejection", function (e) { note("rejection", e.reason && e.reason.message || e.reason); });

  // ---------------------------------------------------------------- helpers
  function clip(s, n) { s = String(s == null ? "" : s); return s.length > n ? s.slice(0, n) + "…" : s; }
  function round(n) { return Math.round(n * 10) / 10; }
  function rectOf(r) { return { x: round(r.left), y: round(r.top), w: round(r.width), h: round(r.height) }; }

  function parentOf(n) {
    if (n.parentElement) return n.parentElement;
    var root = n.getRootNode && n.getRootNode();
    return root && root.host ? root.host : null;
  }

  // Deepest element under a point, through open shadow roots.
  function deepAt(x, y) {
    var root = document, el = null;
    for (var i = 0; i < 40; i++) {
      var next = root.elementsFromPoint(x, y).filter(function (e) { return e !== host; })[0];
      if (!next || next === el) break;
      el = next;
      if (!el.shadowRoot) break;
      root = el.shadowRoot;
    }
    return el;
  }

  function textOf(node, cap) {
    var out = [];
    (function walk(n) {
      if (out.join(" ").length > cap) return;
      if (n.nodeType === 3) { var t = n.nodeValue.replace(/\s+/g, " ").trim(); if (t) out.push(t); return; }
      if (n.nodeType !== 1 || /^(STYLE|SCRIPT)$/.test(n.tagName)) return;
      if (n.shadowRoot) Array.prototype.forEach.call(n.shadowRoot.childNodes, walk);
      Array.prototype.forEach.call(n.childNodes, walk);
    })(node);
    return clip(out.join(" "), cap);
  }

  // HTML of an element with shadow roots written inline as <#shadow-root>. Styles and long path data are dropped.
  function domOf(el, cap) {
    var out = [], size = 0;
    function add(s) { out.push(s); size += s.length; }
    (function walk(n, depth) {
      if (size > cap) return;
      var pad = new Array(Math.min(depth, 30) + 1).join(" ");
      if (n.nodeType === 3) { var t = n.nodeValue.replace(/\s+/g, " ").trim(); if (t) add(pad + clip(t, 200) + "\n"); return; }
      if (n.nodeType !== 1 || /^(STYLE|SCRIPT)$/.test(n.tagName)) return;
      var attrs = Array.prototype.map.call(n.attributes, function (a) {
        return " " + a.name + '="' + clip(a.name === "d" ? a.value.slice(0, 20) : a.value, 120).replace(/"/g, "'") + '"';
      }).join("");
      add(pad + "<" + n.tagName.toLowerCase() + attrs + ">\n");
      if (n.shadowRoot) {
        add(pad + " <#shadow-root>\n");
        Array.prototype.forEach.call(n.shadowRoot.childNodes, function (c) { walk(c, depth + 2); });
        add(pad + " </#shadow-root>\n");
      }
      Array.prototype.forEach.call(n.childNodes, function (c) { walk(c, depth + 1); });
    })(el, 0);
    return out.join("") + (size > cap ? "…(cut)\n" : "");
  }

  function yaml(v, ind) {
    ind = ind || "";
    if (v === null || v === undefined) return "null";
    if (typeof v !== "object") return typeof v === "string" && /^[\w./ -]+$/.test(v) && v !== "" && !/^(true|false|null|\d.*)$/.test(v) ? v : JSON.stringify(v);
    var keys = Array.isArray(v) ? v.map(function (_, i) { return i; }) : Object.keys(v);
    if (!keys.length) return Array.isArray(v) ? "[]" : "{}";
    return keys.map(function (k) {
      var item = v[k], scalar = item === null || typeof item !== "object" || !Object.keys(item).length;
      var head = Array.isArray(v) ? ind + "- " : ind + k + ":";
      if (Array.isArray(v)) {
        if (scalar) return head + yaml(item);
        return head + yaml(item, ind + "  ").replace(/^\s+/, "");
      }
      return scalar ? head + " " + yaml(item) : head + "\n" + yaml(item, ind + "  ");
    }).join("\n");
  }

  function downscale(canvas, max, q, fill) {
    var k = Math.min(1, max / Math.max(canvas.width, canvas.height));
    var c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(canvas.width * k));
    c.height = Math.max(1, Math.round(canvas.height * k));
    var g = c.getContext("2d");
    g.fillStyle = fill || "#fff";
    g.fillRect(0, 0, c.width, c.height);
    g.drawImage(canvas, 0, 0, c.width, c.height);
    return { url: c.toDataURL("image/jpeg", q), w: c.width, h: c.height };
  }

  // ---------------------------------------------------------------- Home Assistant context
  function hassNow() { return HA_EL && HA_EL.hass; }

  function entityInfo(hass, ids) {
    return ids.slice(0, 12).map(function (id) {
      var s = hass.states[id], attrs = {};
      Object.keys(s.attributes || {}).forEach(function (k) {
        if (k === "entity_picture" || k === "icon") return;
        var v = s.attributes[k];
        attrs[k] = typeof v === "object" && v ? clip(JSON.stringify(v), 160) : v;
      });
      return { id: id, state: s.state, last_changed: s.last_changed, attributes: attrs };
    });
  }

  function haContext(el) {
    var hass = hassNow() || {}, chain = [], cards = [], ids = {}, n;
    for (n = el; n && chain.length < 60; n = parentOf(n)) {
      chain.push(n);
      var cfg = n._config || n.config;
      if (cfg && typeof cfg === "object" && typeof cfg.type === "string" && cards.length < 4) cards.push({ tag: n.tagName.toLowerCase(), el: n, config: cfg });
      if (n.stateObj && n.stateObj.entity_id) ids[n.stateObj.entity_id] = 1;
    }
    var found = JSON.stringify(cards.map(function (c) { return c.config; }));
    (found.match(/\b[a-z_]+\.[a-z0-9_]+\b/g) || []).forEach(function (m) { if (hass.states && hass.states[m]) ids[m] = 1; });
    var parts = location.pathname.split("/").filter(Boolean);
    var card = cards[0];
    return {
      dashboard: parts[0] || "", view: parts[1] || "",
      card: card ? { tag: card.tag, yaml: clip(yaml(card.config), 4000), rect: rectOf(card.el.getBoundingClientRect()),
                     text: textOf(card.el, 400) } : null,
      parent_cards: cards.slice(1).map(function (c) { return { tag: c.tag, type: c.config.type }; }),
      entities: hass.states ? entityInfo(hass, Object.keys(ids)) : [],
      ha: hass.config ? { version: hass.config.version, time_zone: hass.config.time_zone } : null,
      user: hass.user ? { name: hass.user.name, admin: hass.user.is_admin } : null,
      theme: hass.themes ? { name: hass.selectedTheme && hass.selectedTheme.theme || hass.themes.theme, dark: hass.themes.darkMode } : null,
      language: hass.language,
      card_el: card ? card.el : null,
    };
  }

  async function shot(el) {
    try {
      var work = (async function () {
        var mod = await import(SNAPDOM);
        var r = await mod.snapdom(el, { scale: 1, dpr: 1, embedFonts: false });
        return downscale(await r.toCanvas(), 700, 0.7);
      })();
      var c = await Promise.race([work, new Promise(function (_, no) { setTimeout(function () { no(new Error("screenshot timed out")); }, 15000); })]);
      return { image: c.url, w: c.w, h: c.h };
    } catch (e) { return { error: clip(e && e.message || e, 200) }; }
  }

  // ---------------------------------------------------------------- Schaltpult context
  var buildVersion = null;
  function loadBuild() {
    return buildVersion || (buildVersion = fetch("precache.json", { cache: "no-store" })
      .then(function (r) { return r.json(); }).then(function (j) { return j.version; }).catch(function () { return "unknown"; }));
  }

  function canvasShot(rect) {
    var canvas = document.getElementById("unity-canvas");
    if (!canvas) return { error: "no canvas" };
    try {
      var b = canvas.getBoundingClientRect(), sx = canvas.width / b.width, sy = canvas.height / b.height;
      var c = document.createElement("canvas");
      c.width = Math.max(1, Math.round(rect.w * sx)); c.height = Math.max(1, Math.round(rect.h * sy));
      var g = c.getContext("2d");
      g.drawImage(canvas, Math.round((rect.x - b.left) * sx), Math.round((rect.y - b.top) * sy), c.width, c.height, 0, 0, c.width, c.height);
      var px = g.getImageData(0, 0, c.width, c.height).data, empty = true;
      for (var i = 3; i < px.length && empty; i += 4 * 97) if (px[i]) empty = false;
      var d = downscale(c, 900, 0.75, "#000");
      var out = { image: d.url, w: d.w, h: d.h };
      if (empty) out.warning = "canvas read returned no pixels";
      return out;
    } catch (e) { return { error: clip(e && e.message || e, 200) }; }
  }

  // ---------------------------------------------------------------- UI
  var host = document.createElement("div");
  host.id = "pb-feedback";
  host.style.cssText = "position:fixed;inset:0;z-index:2147483647;pointer-events:none;";
  var root = host.attachShadow({ mode: "open" });
  var side = "right";
  root.innerHTML = '<style>' +
    ':host{all:initial;--bg:#fff;--fg:#111;--mut:#555;--bd:#bbb;--bd2:#ddd;--ph:#eee;--bar:#222;--barfg:#fff;--err:#c62828;--veil:#0008}:host(.dark){--bg:#1e1e22;--fg:#eee;--mut:#aaa;--bd:#555;--bd2:#444;--ph:#111;--bar:#38383e;--barfg:#fff;--err:#ef9a9a;--veil:#000a}*{box-sizing:border-box;font:14px/1.4 -apple-system,system-ui,Roboto,sans-serif}' +
    '.fab{position:fixed;bottom:16px;' + side + ':16px;width:44px;height:44px;border-radius:50%;border:0;background:#8e24aa;color:#fff;' +
    'box-shadow:0 2px 8px #0006;pointer-events:auto;opacity:.75;display:flex;align-items:center;justify-content:center;padding:0}' +
    '.fab:hover,.fab:focus{opacity:1}.fab svg{width:24px;height:24px;fill:currentColor}' +
    '.bar{position:fixed;top:12px;left:50%;transform:translateX(-50%);width:max-content;max-width:calc(100vw - 24px);display:none;align-items:center;gap:12px;padding:8px 8px 8px 16px;' +
    'background:var(--bar);color:var(--barfg);border-radius:12px;box-shadow:0 2px 12px #0008;pointer-events:auto}' +
    '.hl{position:fixed;display:none;border:2px solid #8e24aa;background:#8e24aa22;border-radius:4px;pointer-events:none}' +
    '.veil{position:fixed;inset:0;display:none;pointer-events:auto;cursor:crosshair;touch-action:none}' +
    '.dlg{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:var(--veil);pointer-events:auto;padding:16px}' +
    '.card{width:min(520px,100%);max-height:100%;overflow:auto;background:var(--bg);color:var(--fg);border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:10px}' +
    '.card h2{margin:0;font-size:18px}.sel{color:var(--mut);font-size:13px;word-break:break-word}' +
    '.card img{max-width:100%;max-height:160px;object-fit:contain;border:1px solid var(--bd2);border-radius:6px;align-self:flex-start;background:var(--ph)}' +
    'textarea{width:100%;min-height:96px;border:1px solid var(--bd);border-radius:8px;padding:8px;color:var(--fg);background:var(--bg);resize:vertical}' +
    '.row{display:flex;gap:8px;justify-content:flex-end;align-items:center}.err{color:var(--err);margin-right:auto;font-size:13px}' +
    'button.b{border:1px solid var(--bd);background:var(--bg);color:var(--fg);border-radius:8px;padding:8px 16px}' +
    'textarea:focus,button:focus-visible{outline:2px solid #8e24aa;outline-offset:0}textarea:focus{border-color:#8e24aa}' +
    'button.p{background:#8e24aa;border-color:#8e24aa;color:#fff}button:disabled{opacity:.5}' +
    '.toast{position:fixed;bottom:72px;' + side + ':16px;display:none;background:var(--bar);color:var(--barfg);padding:8px 14px;border-radius:10px;pointer-events:none}' +
    '</style>' +
    '<button class="fab" title="Send feedback (F)" aria-label="Send feedback"><svg viewBox="0 0 24 24"><path d="M12,2A7,7 0 0,0 5,9C5,11.38 6.19,13.47 8,14.74V17A1,1 0 0,0 9,18H15A1,1 0 0,0 16,17V14.74C17.81,13.47 19,11.38 19,9A7,7 0 0,0 12,2M9,21A1,1 0 0,0 10,22H14A1,1 0 0,0 15,21V20H9V21Z"/></svg></button>' +
    '<div class="hl"></div><div class="veil"></div>' +
    '<div class="bar"><span class="hint"></span><button class="b cancel">Cancel</button></div>' +
    '<div class="dlg"><div class="card"><h2>Feedback</h2><div class="sel"></div><img alt="" hidden>' +
    '<textarea placeholder="What should change?"></textarea>' +
    '<div class="row"><span class="err"></span><button class="b cancel2">Cancel</button><button class="b p send">Send</button></div></div></div>' +
    '<div class="toast"></div>';
  var $ = function (s) { return root.querySelector(s); };
  var fab = $(".fab"), hl = $(".hl"), veil = $(".veil"), bar = $(".bar"), dlg = $(".dlg"), toast = $(".toast");
  var ta = $("textarea"), sendBtn = $(".send"), errEl = $(".err"), thumb = $(".card img"), selEl = $(".sel");
  ["keydown", "keyup", "keypress"].forEach(function (t) { host.addEventListener(t, function (e) { e.stopPropagation(); }); });

  function say(text, ms) {
    toast.textContent = text; toast.style.display = "block";
    setTimeout(function () { toast.style.display = "none"; }, ms || 3000);
  }

  var mode = "idle", pending = null;
  var BLOCK = ["pointerdown", "pointerup", "mousedown", "mouseup", "click", "dblclick", "auxclick", "contextmenu", "touchstart", "touchend", "keydown", "keyup", "keypress"];

  function ours(e) { return e.composedPath().indexOf(host) >= 0; }

  function onBlock(e) {
    if (ours(e)) return;
    if (e.type === "keydown" && e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); return stop(); }
    e.preventDefault(); e.stopImmediatePropagation();
    if (e.type === "pointerup" && e.button <= 0 && clickSelect()) {
      var el = deepAt(e.clientX, e.clientY);
      if (el) (IS_HA ? pick : pickPlain)(el).catch(fail);
    }
  }
  function onMove(e) {
    if (ours(e) || !clickSelect()) return;
    var el = deepAt(e.clientX, e.clientY);
    if (!el) return;
    var r = el.getBoundingClientRect();
    hl.style.cssText = "display:block;left:" + r.left + "px;top:" + r.top + "px;width:" + r.width + "px;height:" + r.height + "px";
  }

  // HA: follow the HA theme. Schaltpult: follow the system setting.
  var mq = window.matchMedia && matchMedia("(prefers-color-scheme: dark)");
  function theme() {
    var h = IS_HA && hassNow(), dark = h && h.themes ? !!h.themes.darkMode : !!(mq && mq.matches);
    host.classList.toggle("dark", dark);
  }
  if (mq && mq.addEventListener) mq.addEventListener("change", theme);
  theme();

  function start() {
    theme();
    if (mode !== "idle") return;
    mode = "select";
    fab.style.display = "none";
    bar.style.display = "flex";
    $(".hint").textContent = clickSelect() ? "Tap the element your feedback is about" : "Drag a rectangle around what your feedback is about";
    BLOCK.forEach(function (t) { window.addEventListener(t, onBlock, { capture: true, passive: false }); });
    window.addEventListener("pointermove", onMove, true);
    window.addEventListener("pointerdown", onMove, true);
    if (!clickSelect()) { veil.style.display = "block"; dragSelect(); }
  }

  function stop() {
    BLOCK.forEach(function (t) { window.removeEventListener(t, onBlock, { capture: true }); });
    window.removeEventListener("pointermove", onMove, true);
    window.removeEventListener("pointerdown", onMove, true);
    hl.style.display = veil.style.display = bar.style.display = dlg.style.display = "none";
    fab.style.display = "flex";
    mode = "idle"; pending = null;
  }

  function fail(e) {
    host.style.visibility = "";
    stop();
    say("Feedback tool error: " + clip(e && e.message || e, 120), 6000);
  }

  function dragSelect() {
    var x0, y0, drag = false;
    veil.onpointerdown = function (e) { drag = true; x0 = e.clientX; y0 = e.clientY; veil.setPointerCapture(e.pointerId); e.preventDefault(); };
    veil.onpointermove = function (e) {
      if (!drag) return;
      hl.style.cssText = "display:block;left:" + Math.min(x0, e.clientX) + "px;top:" + Math.min(y0, e.clientY) + "px;width:" + Math.abs(e.clientX - x0) + "px;height:" + Math.abs(e.clientY - y0) + "px";
    };
    veil.onpointerup = function (e) {
      if (!drag) return;
      drag = false;
      var rect = { x: Math.min(x0, e.clientX), y: Math.min(y0, e.clientY), w: Math.abs(e.clientX - x0), h: Math.abs(e.clientY - y0) };
      if (rect.w < 12 || rect.h < 12) { hl.style.display = "none"; return; }
      veil.style.display = "none"; hl.style.display = "none"; bar.style.display = "none";
      pickRect(rect).catch(fail);
    };
  }

  function form(label, shots, ctx) {
    mode = "form";
    BLOCK.forEach(function (t) { window.removeEventListener(t, onBlock, { capture: true }); });
    window.removeEventListener("pointermove", onMove, true);
    window.removeEventListener("pointerdown", onMove, true);
    bar.style.display = hl.style.display = "none";
    pending = { ctx: ctx, label: label };
    selEl.textContent = label;
    var first = shots.filter(function (s) { return s && s.image; })[0];
    thumb.hidden = !first; if (first) thumb.src = first.image;
    errEl.textContent = ""; ta.value = ""; sendBtn.disabled = false;
    dlg.style.display = "flex";
    setTimeout(function () { ta.focus(); }, 50);
  }

  async function pick(el) {
    mode = "busy"; bar.style.display = hl.style.display = "none";
    var c = haContext(el), card = c.card_el;
    delete c.card_el;
    var r = el.getBoundingClientRect();
    // The page without our UI: hide it while snapdom clones the element.
    host.style.visibility = "hidden";
    var shots = [await shot(el)];
    if (card && card !== el) shots.push(await shot(card));
    host.style.visibility = "";
    var ctx = {
      surface: "home-assistant",
      element: { tag: el.tagName.toLowerCase(), text: textOf(el, 200), rect: rectOf(r), dom: domOf(el, 8000),
                 path: (function () { var p = [], n; for (n = el; n && p.length < 12; n = parentOf(n)) p.push(n.tagName.toLowerCase() + (n.id ? "#" + n.id : "")); return p.reverse().join(" > "); })() },
      screenshot_element: shots[0], screenshot_card: shots[1] || null,
    };
    Object.keys(c).forEach(function (k) { ctx[k] = c[k]; });
    var label = (c.card ? c.card.tag.replace(/^hui-|-card$/g, "") + " card" : ctx.element.tag) +
      (c.entities.length ? " · " + c.entities[0].id : "") + (ctx.element.text ? " · “" + clip(ctx.element.text, 50) + "”" : "");
    form(label, shots, ctx);
  }

  // Plain page: like pick() without the HA card and entity context.
  async function pickPlain(el) {
    mode = "busy"; bar.style.display = hl.style.display = "none";
    host.style.visibility = "hidden";
    var s = await shot(el);
    host.style.visibility = "";
    var ctx = {
      surface: "plain-page", where: "druckwerk" + (location.hash ? " " + location.hash : ""),
      element: { tag: el.tagName.toLowerCase(), text: textOf(el, 200), rect: rectOf(el.getBoundingClientRect()), dom: domOf(el, 8000),
                 path: (function () { var p = [], n; for (n = el; n && p.length < 12; n = parentOf(n)) p.push(n.tagName.toLowerCase() + (n.id ? "#" + n.id : "")); return p.reverse().join(" > "); })() },
      screenshot_element: s,
    };
    form(ctx.element.tag + (ctx.element.text ? " · “" + clip(ctx.element.text, 50) + "”" : ""), [s], ctx);
  }

  // Asks the game (FeedbackProbe.cs) for camera, items and colliders in the rectangle; null when the build has no bridge.
  var probeId = 0;
  function probe(norm) {
    return new Promise(function (resolve) {
      var id = ++probeId, t0 = Date.now();
      window.__pbProbeResult = null;
      window.__pbProbeRequest = JSON.stringify({ id: id, x: norm.x, y: norm.y, w: norm.w, h: norm.h });
      (function poll() {
        var r = window.__pbProbeResult;
        if (r) { try { var j = JSON.parse(r); if (j.id === id || j.error) return resolve(j); } catch (e) { return resolve({ error: "bad answer" }); } }
        if (Date.now() - t0 > 1500) { window.__pbProbeRequest = null; return resolve(null); }
        setTimeout(poll, 40);
      })();
    });
  }

  async function pickRect(rect) {
    mode = "busy";
    host.style.visibility = "hidden";
    var s = canvasShot(rect);
    host.style.visibility = "";
    var canvas = document.getElementById("unity-canvas"), b = canvas ? canvas.getBoundingClientRect() : { left: 0, top: 0, width: innerWidth, height: innerHeight };
    var rectNorm = { x: (rect.x - b.left) / b.width, y: (rect.y - b.top) / b.height, w: rect.w / b.width, h: rect.h / b.height };
    var ctx = {
      surface: "schaltpult-web",
      rect_px: { x: round(rect.x), y: round(rect.y), w: round(rect.w), h: round(rect.h) },
      rect_norm: { x: round((rect.x - b.left) / b.width * 1000) / 1000, y: round((rect.y - b.top) / b.height * 1000) / 1000,
                   w: round(rect.w / b.width * 1000) / 1000, h: round(rect.h / b.height * 1000) / 1000 },
      canvas: { w: canvas && canvas.width, h: canvas && canvas.height },
      scene: await probe(rectNorm),
      lite: !!window.LITE, build: await loadBuild(), ingress: /hassio_ingress/.test(location.pathname),
      screenshot_region: s,
    };
    form("3D view region " + Math.round(rect.w) + "×" + Math.round(rect.h) + " px", [s], ctx);
  }

  function submit(item, description) {
    if (IS_HA) return hassNow().callService("todo", "add_item", { item: item, description: description }, { entity_id: LIST });
    return fetch("api/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ item: item, description: description }) })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); });
  }

  async function send() {
    var text = ta.value.trim();
    if (!text) { errEl.textContent = "Write a message first."; return; }
    sendBtn.disabled = true; errEl.textContent = "";
    var ctx = pending.ctx, now = new Date();
    var payload = {
      message: text, time: now.toISOString(),
      local_time: now.toString(),
      url: location.href, title: document.title,
      viewport: { w: innerWidth, h: innerHeight, dpr: devicePixelRatio, scroll: { x: Math.round(scrollX), y: Math.round(scrollY) } },
      screen: { w: screen.width, h: screen.height }, touch: navigator.maxTouchPoints > 0,
      user_agent: navigator.userAgent, browser_errors: errors.slice(),
    };
    var shotsOut = {};
    Object.keys(ctx).forEach(function (k) { if (/^screenshot_/.test(k)) shotsOut[k] = ctx[k]; else payload[k] = ctx[k]; });
    Object.keys(shotsOut).forEach(function (k) { payload[k] = shotsOut[k]; });   // screenshots last
    var where = IS_HA ? (payload.dashboard + "/" + payload.view) : ctx.where || "schaltpult";
    var item = "Feedback " + now.toISOString().replace("T", " ").slice(0, 19) + "Z · " + where + " · " + clip(text.replace(/\s+/g, " "), 60);
    try {
      await submit(item, JSON.stringify(payload));
      stop(); say("Feedback sent");
    } catch (e) {
      sendBtn.disabled = false; errEl.textContent = "Not sent: " + clip(e && e.message || JSON.stringify(e), 100);
    }
  }

  fab.onclick = start;

  // Unity WebGL listens for keys on window and cancels them, so typing in our form would do nothing. This listener is
  // registered before Unity's: keys that belong to our form never reach it, and the default action (typing) stays.
  // The stop also keeps the key from the textarea's own listeners, so the form's shortcuts are handled here.
  ["keydown", "keyup", "keypress"].forEach(function (t) {
    window.addEventListener(t, function (e) {
      if (!ours(e)) return;
      e.stopImmediatePropagation();
      if (t !== "keydown" || mode !== "form") return;
      if (e.key === "Escape") stop();
      else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
    }, true);
  });

  // F starts select mode unless a text field has focus. The capture phase and stopImmediatePropagation keep Unity and HA shortcuts from also getting the key.
  var NO_TEXT = /^(button|checkbox|radio|range|color|file|image|reset|submit)$/;
  function typing() {
    var el = document.activeElement;
    while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
    if (!el) return false;
    if (el.isContentEditable || el.tagName === "TEXTAREA" || el.tagName === "SELECT") return true;
    return el.tagName === "INPUT" && !NO_TEXT.test(el.type);
  }
  window.addEventListener("keydown", function (e) {
    if (e.key !== "f" || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || e.repeat || e.isComposing) return;
    if (mode !== "idle" || ours(e) || typing()) return;
    e.preventDefault(); e.stopImmediatePropagation();
    start();
  }, true);
  $(".cancel").onclick = stop;
  $(".cancel2").onclick = stop;
  sendBtn.onclick = send;

  document.body.appendChild(host);
  // The Schaltpult panel of HA shows the page's own button (in the iframe), so the HA one hides there.
  if (IS_HA) {
    var panel = /^\/(local_schaltpult|hassio\/ingress\/local_schaltpult)(\/|$)/;
    setInterval(function () { host.style.display = panel.test(location.pathname) ? "none" : ""; }, 300);
  }
  window.__pbFeedbackApi = { start: start, stop: stop, domOf: domOf, yaml: yaml, deepAt: deepAt };
})();
