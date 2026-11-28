#!/usr/bin/env python3
"""Slices the test plate in headless Chromium through the real UI (New print) against the running hub image. Cases:
top-level (isolated, mt engine), cross-origin iframe without COOP/COEP in the parent (Document-Isolation-Policy, mt), a
gateway that strips the isolation headers (st engine). The app has no server-side slicer, so these are the only slices.
Prints the numbers and runs the G-code guard and the slice checks on every browser result. Exits 1 on any failure,
after all cases ran.

env: HUB (hub base URL), STL, JOBS (the hub's job dir)."""
import http.server
import json
import os
import re
import sys
import threading
import time

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "hub", "ui"))
sys.path.insert(0, os.path.join(REPO, "hub"))
sys.path.insert(0, os.path.join(REPO, "hub", "queue"))
import ingress_proxy  # noqa: E402
import filament  # noqa: E402

HUB, STL, JOBS = (os.environ[k] for k in ("HUB", "STL", "JOBS"))
SNAPDOM = "https://cdn.jsdelivr.net/npm/@zumer/snapdom@3.8.3/dist/snapdom.mjs"
UP = (HUB.split("//")[1].split(":")[0], int(HUB.rsplit(":", 1)[1]))
failures, results = [], {}


def fail(msg):
    print("FAIL: " + msg, flush=True)
    failures.append(msg)


def guard():
    """The app's G-code guard (hub/gcode_guard.py)."""
    sys.path.insert(0, os.path.join(REPO, "hub"))
    import gcode_guard
    return gcode_guard.check_gcode_3mf


def job_dirs():
    return {d for d in os.listdir(JOBS) if os.path.isfile(os.path.join(JOBS, d, "job.json"))}


def run_case(br, name, base, ident, framed=None, manual_device=False, feedback=False):
    """base: the page URL of the hub behind a gateway; framed: URL of a parent page that embeds it."""
    before = job_dirs()
    ctx = br.new_context(viewport={"width": 1280, "height": 1000})
    page = ctx.new_page()
    page.on("console", lambda m: m.type in ("error", "warning") and print(f"  [{name} console {m.type}] {m.text}", flush=True))
    page.on("pageerror", lambda e: fail(f"{name}: page error {e}"))
    if framed:
        page.goto(framed)
        page.wait_for_selector("iframe")
        frame = next(f for f in page.frames if f.url.startswith(base.split("/api/")[0]))
        root = page.frame_locator("iframe")
        frame.wait_for_selector("#app .page-head, .pcard", timeout=60000)
    else:
        page.goto(base + "?as=" + ident)
        page.wait_for_selector(".pcard", timeout=60000)
        frame, root = page.main_frame, page
    isolated = frame.evaluate("crossOriginIsolated")
    frame.evaluate("location.hash = '#/new'")
    root.locator(".drop").wait_for()
    root.locator("input[type=file]").set_input_files(STL)
    root.get_by_role("button", name="Workshop H2S").click()
    root.locator(".chip", has_text="AMS 1·2").click()  # PETG
    chips = root.locator("#where")
    has_chips = chips.count() > 0  # a UI with a "Slice on" choice; without it every slice runs on this device
    default = chips.locator("button[aria-pressed=true]").inner_text().strip() if has_chips else "This device"
    body_text = root.locator("body").inner_text()
    if manual_device and has_chips:
        chips.get_by_role("button", name="This device").click()
    on_device = default == "This device" or (manual_device and has_chips)
    if on_device:
        root.locator("#engine-info[data-state=ready], #engine-info[data-state=error]").wait_for(timeout=300000)
    engine, load_ms = "", 0
    if on_device:
        info = root.locator("#engine-info")
        if info.get_attribute("data-state") == "error":
            fail(f"{name}: the engine did not load: {info.inner_text()}")
            ctx.close()
            return
        engine, load_ms = info.get_attribute("data-variant"), int(info.get_attribute("data-load-ms"))
    print(f"CASE {name}: crossOriginIsolated={isolated} slice-on-default=\"{default}\" engine={engine or 'not started'} engine-load={load_ms / 1000:.1f}s", flush=True)
    r = {"isolated": isolated, "default": default, "engine": engine, "load_ms": load_ms, "text": body_text}
    results[name] = r
    if framed or feedback:
        ok = frame.evaluate("u => import(u).then(m => typeof m.snapdom, e => 'ERR ' + e.message)", SNAPDOM)
        print(f"  snapdom import (feedback screenshots) on the {'isolated ' if isolated else ''}page: {ok}", flush=True)
        if ok != "function":
            fail(f"{name}: the feedback button's snapdom import fails under isolation: {ok}")
    root.get_by_role("button", name="Slice good, better and best").click()
    t0 = time.time()
    root.locator(".slice-time").nth(2).or_(root.locator(".variant small[style*=err]")).first.wait_for(timeout=40 * 60000)
    failed = root.locator(".variant small[style*=err]")
    if failed.count():
        fail(f"{name}: a variant failed: {failed.first.inner_text()}")
        ctx.close()
        return
    times = [(v.get_attribute("data-engine"), int(v.get_attribute("data-slice-ms"))) for v in root.locator(".slice-time").all()]
    r["slice_ms"] = [t for _, t in times]
    print(f"  {name}: slice time per variant (good, better, best): " + ", ".join(f"{t / 1000:.1f}s ({e})" for e, t in times) +
          f"; {time.time() - t0:.0f}s in all", flush=True)
    if engine and any(e != engine for e, _ in times):
        fail(f"{name}: variants ran on {[e for e, _ in times]}, engine was {engine}")
    root.locator(".variant[aria-pressed=true]").wait_for()
    if not framed:
        root.locator('.preview3d[data-ready="1"]').wait_for(timeout=60000)  # the 3D preview parsed the browser G-code
        root.get_by_text(re.compile(r"Layers 1 to (\d+) of \1")).first.wait_for(timeout=60000)
        # Queue the picked variant against the mocks, like a Pi slice.
        root.get_by_role("button", name="Add Better to queue").click()
        root.locator(".job").first.wait_for()
        q = frame.evaluate("fetch('api/queue').then(r => r.json())")
        mine = [j for j in q["jobs"] if j.get("source") == os.path.basename(STL) and j["file"].endswith(".gcode.3mf")]
        print(f"  {name}: queued {len(mine)} job(s) from the browser slice: {[(j['state'], j['target'], j['grams'], j['print_seconds']) for j in mine]}", flush=True)
        if not mine:
            fail(f"{name}: the browser slice was not queued")
    if feedback:
        sent = []
        page.on("request", lambda rq: rq.url.endswith("api/feedback") and sent.append(rq))
        root.locator(".fab").click()
        page.mouse.click(300, 300)
        root.locator("textarea").fill("browser test")
        root.locator(".send").click()
        page.wait_for_timeout(8000)
        if not sent:
            fail(f"{name}: the feedback button did not send")
        else:
            resp = sent[0].response()
            has_image = "data:image/jpeg" in (sent[0].post_data or "")
            print(f"  {name}: feedback POST -> HTTP {resp.status if resp else '?'}, screenshot included: {has_image}", flush=True)
            if not resp or resp.status != 200 or not has_image:
                fail(f"{name}: feedback button failed on the isolated page")
    ctx.close()
    r["jobs"] = sorted(job_dirs() - before)


def parent_page(url, port):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = f'<!doctype html><iframe src="{url}" style="width:1200px;height:900px;border:0"></iframe>'.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    check_gcode = guard()
    top = ingress_proxy.start(UP)
    ptop = f"http://127.0.0.1:{top.server_address[1]}{ingress_proxy.PREFIX}"
    stripped = ingress_proxy.start(UP, strip=("cross-origin-opener-policy", "cross-origin-embedder-policy", "document-isolation-policy",
                                              "cross-origin-resource-policy"))
    pplain = f"http://127.0.0.1:{stripped.server_address[1]}{ingress_proxy.PREFIX}"
    # The parent is another site (localhost) and sends no COOP/COEP, like Home Assistant's own page.
    parent = parent_page(ptop + "?as=admin#/new", 18815)
    with sync_playwright() as p:
        br = p.chromium.launch()
        print("Chromium " + br.version, flush=True)
        for name, kw in (("top-level-mt", dict(base=ptop, ident="user", feedback=True)),
                         ("iframe-no-coop", dict(base=ptop, ident="admin", framed="http://localhost:18815/")),
                         ("not-isolated", dict(base=pplain, ident="user", manual_device=True))):
            try:
                run_case(br, name, **kw)
            except Exception as e:  # noqa: BLE001
                fail(f"{name}: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")
        br.close()
    top.shutdown(); stripped.shutdown(); parent.shutdown()
    t, f, n = results.get("top-level-mt"), results.get("iframe-no-coop"), results.get("not-isolated")
    if t and not (t["isolated"] and t["engine"] == "mt" and t["default"] == "This device"):
        fail(f"top-level: expected isolated, default 'This device', mt; got {t['isolated']}, {t['default']!r}, {t['engine']}")
    if f and not (f["isolated"] and f["engine"] == "mt" and f["default"] == "This device"):
        fail(f"iframe: expected crossOriginIsolated=true and mt (Document-Isolation-Policy); got isolated={f['isolated']} engine={f['engine']} default={f['default']!r}")
    if n:
        if n["isolated"] or n["engine"] != "st":
            fail(f"not-isolated: got isolated={n['isolated']} engine={n['engine']}")
        print(f"  not-isolated: the page is not cross-origin isolated and slices with the {n['engine']} engine", flush=True)
    # Guard and slice checks on every browser result.
    for name, r in results.items():
        for jid in r.get("jobs", []):
            job = json.load(open(os.path.join(JOBS, jid, "job.json")))
            if not str(job.get("log", "")).startswith("Sliced in the browser"):
                continue
            problems = check_gcode(job["result"].replace("/tmp/jobs", JOBS), "H2S", use_ams=True)
            use = filament.slice_usage(job["result"].replace("/tmp/jobs", JOBS))
            print(f"GUARD {name} {jid} ({job['quality']}): {'PASS' if not problems else 'FAIL ' + str(problems)}; slice_usage={use['seconds']}s {use['grams']}g {use['filaments']}", flush=True)
            if problems or not (use["seconds"] > 0 and use["grams"] > 0):
                fail(f"{name}: {jid} guard/slice checks: {problems}")
    print("SUMMARY (same image, same runner), seconds good/better/best", flush=True)
    for name, r in results.items():
        if "slice_ms" in r:
            print(f"  browser {r['engine']} {name:15s}: " + " / ".join(f"{m / 1000:.1f}" for m in r["slice_ms"]) + f"  (engine start {r['load_ms'] / 1000:.1f})", flush=True)
    sys.exit(1 if failures else 0)


main()
