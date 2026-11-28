#!/usr/bin/env python3
"""WebKit check of the phone promotion: does a framed Druckwerk page move the top window to its own URL, where COOP+COEP
isolate it (SharedArrayBuffer, so the multi-thread slicer works)? Local server, no Docker. It sends the headers of
hub/slicer/server.py isolation_headers under /iso/ and none under /plain/ (a gateway that strips them).

env: UI_DIR (default hub/ui; point it at an older tree to see the check fail), BROWSER (webkit | chromium, default webkit),
NO_DIP=1 (drop Document-Isolation-Policy, for a Chromium dry run). Exit 1 on any failure; (a) and (c) are the ones that matter."""
import http.server
import mimetypes
import os
import sys
import threading
import time

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.abspath(os.environ.get("UI_DIR") or os.path.join(HERE, "..", "ui"))
BROWSER = os.environ.get("BROWSER", "webkit")
NO_DIP = os.environ.get("NO_DIP") == "1"
hits = []

PROBE = """<!doctype html><meta charset=utf-8><title>probe</title><body>probe<script type=module>
import { settle, promote } from './views/promote.js';
settle();
const r = promote();
window.__r = { promoted: r, framed: top !== window, isolated: crossOriginIsolated, sab: typeof SharedArrayBuffer,
  flag: (() => { try { return sessionStorage.getItem('dw-promoted'); } catch { return 'no-storage'; } })() };
document.body.dataset.done = '1';
</script>"""
SAB = """<!doctype html><meta charset=utf-8><title>sab</title><body><script>
(async () => {
  const out = { isolated: crossOriginIsolated, sab: typeof SharedArrayBuffer };
  try {
    const sab = new SharedArrayBuffer(8), a = new Int32Array(sab);
    const w = new Worker(URL.createObjectURL(new Blob(['onmessage=e=>{Atomics.store(new Int32Array(e.data),0,42);postMessage(1)}'])));
    await new Promise((ok, no) => { w.onmessage = ok; w.onerror = (e) => no(new Error(e.message)); w.postMessage(sab); });
    out.shared = Atomics.load(a, 0);
  } catch (e) { out.error = String(e); }
  window.__r = out; document.body.dataset.done = '1';
})();
</script>"""
FRAMED = """<!doctype html><meta charset=utf-8><title>frame</title><body><script>
window.__r = { isolated: crossOriginIsolated, sab: typeof SharedArrayBuffer, framed: top !== window }; document.body.dataset.done = '1';
</script>"""
PARENT = """<!doctype html><meta charset=utf-8><title>parent</title><body>parent (stands for the HA frontend)
<iframe id=f src="%s" style="width:600px;height:400px"></iframe>"""


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        hits.append(path)
        if path.startswith("/parent/"):  # same origin as the page, no isolation headers of its own: the HA frontend
            return self.out(PARENT % ("/" + path[len("/parent/"):]), "text/html", iso=False)
        for prefix, iso in (("/iso/", True), ("/plain/", False)):
            if path.startswith(prefix):
                name = path[len(prefix):]
                pages = {"probe.html": PROBE, "sab.html": SAB, "framed.html": FRAMED}
                if name in pages:
                    return self.out(pages[name], "text/html", iso)
                f = os.path.abspath(os.path.join(UI, name))
                if f.startswith(UI + os.sep) and os.path.isfile(f):
                    return self.out(open(f, "rb").read(), mimetypes.guess_type(f)[0] or "text/javascript", iso)
        self.out(b"not found", "text/plain", False, 404)

    def out(self, body, ctype, iso, status=200):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if iso:  # the same four headers as hub/slicer/server.py isolation_headers
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
            if not NO_DIP:
                self.send_header("Document-Isolation-Policy", "isolate-and-require-corp")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


failures = []


def check(label, ok, detail):
    print(f"{'PASS' if ok else 'FAIL'}: {label}: {detail}", flush=True)
    if not ok:
        failures.append(label)


def result(page_or_frame):
    page_or_frame.wait_for_selector("body[data-done]", state="attached", timeout=15000)
    return page_or_frame.evaluate("window.__r")


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    with sync_playwright() as pw:
        bt = getattr(pw, BROWSER)
        br = bt.launch(executable_path=os.environ.get("BROWSER_EXE") or None)
        print(f"browser: {BROWSER} {br.version}  ui: {UI}", flush=True)
        phone = pw.devices["iPhone 15"]

        def ctx(mobile):
            return br.new_context(**phone) if mobile else br.new_context(viewport={"width": 1280, "height": 900})

        def console(p, tag):
            p.on("console", lambda m: m.type in ("error", "warning") and print(f"  [{tag} console {m.type}] {m.text}", flush=True))
            p.on("pageerror", lambda e: print(f"  [{tag} pageerror] {e}", flush=True))

        # (a) top level
        c = ctx(True)
        p = c.new_page(); console(p, "a")
        resp = p.goto(base + "/iso/sab.html")
        hd = resp.headers
        r = result(p)
        check("(a) top-level page: COOP+COEP sent", hd.get("cross-origin-opener-policy") == "same-origin" and hd.get("cross-origin-embedder-policy") == "require-corp", f"coop={hd.get('cross-origin-opener-policy')} coep={hd.get('cross-origin-embedder-policy')}")
        check("(a) top-level page: crossOriginIsolated, SharedArrayBuffer, worker shares memory", r.get("isolated") is True and r.get("sab") == "function" and r.get("shared") == 42, str(r))
        c.close()

        # (b) same-origin parent with an iframe
        c = ctx(True)
        p = c.new_page(); console(p, "b")
        p.goto(base + "/parent/iso/framed.html")
        fr = next(f for f in p.frames if f != p.main_frame)
        r = result(fr)
        print(f"INFO: (b) isolated frame under a same-origin parent on {BROWSER}: crossOriginIsolated={r['isolated']} SharedArrayBuffer={r['sab']}", flush=True)
        print("INFO: (b) " + ("the frame is NOT isolated: the promotion is needed" if not r["isolated"] else "the frame IS isolated: the promotion is not needed"), flush=True)
        if BROWSER == "webkit":
            check("(b) WebKit frame is not isolated", r["isolated"] is False, str(r))
        c.close()

        # (c) promotion: iPhone, framed, headers sent
        for tag, path, want_iso in (("iso", "/iso/probe.html", True), ("plain", "/plain/probe.html", False)):
            c = ctx(True)
            p = c.new_page(); console(p, "c-" + tag)
            before = len(hits)
            p.goto(base + "/parent" + path)
            p.wait_for_function("location.pathname === %r" % path, timeout=15000)
            r = result(p)
            n = hits[before:].count(path)
            check(f"(c) iPhone, framed, {tag} headers: top window moved to the page URL", p.url == base + path and r["framed"] is False, f"url={p.url} {r}")
            check(f"(c) {tag}: page isolated after promotion" if want_iso else "(c) plain: not isolated (no headers), no second move",
                  r["isolated"] is want_iso and r["promoted"] is False and (r["sab"] == "function") == want_iso, f"isolated={r['isolated']} sab={r['sab']} flag={r['flag']}")
            if want_iso:
                check("(c) iso: guard flag cleared once isolated", r["flag"] is None, f"flag={r['flag']}")
            else:
                check("(c) plain: guard flag stays set", r["flag"] == "1", f"flag={r['flag']}")
                # the page is framed again (the user went back into HA): the flag stops a loop
                p.evaluate("(u) => { const f = document.createElement('iframe'); f.id = 'again'; f.src = u; document.body.append(f); }", path)
                p.wait_for_selector("#again")
                fr2 = next(f for f in p.frames if f != p.main_frame)
                r2 = result(fr2)
                time.sleep(1.5)
                check("(c) plain: framed again, flag set, no loop", r2["promoted"] is False and p.url == base + path and hits.count(path) - before <= 3, f"promoted={r2['promoted']} url={p.url} loads={hits[before:].count(path)}")
            check(f"(c) {tag}: page loaded {n} time(s) in total", n == 2 if want_iso else n >= 2, f"top-level load count for {path}={n}")
            c.close()

        # desktop: no promotion
        c = ctx(False)
        p = c.new_page(); console(p, "d")
        p.goto(base + "/parent/plain/probe.html")
        fr = next(f for f in p.frames if f != p.main_frame)
        r = result(fr)
        time.sleep(1.0)
        check("(c) desktop, framed, no isolation: no move", r["promoted"] is False and "/parent/" in p.url, f"url={p.url} {r}")
        c.close()
        br.close()
    print("RESULT: " + ("FAIL " + ", ".join(failures) if failures else "all checks passed"), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
