#!/usr/bin/env python3
"""Walk every screen against the mock server as admin and user. Fails on any console error, page error,
failed request, or request outside the fake ingress prefix (absolute paths break behind HA ingress).

  python3 hub/ui/check_ui.py [--shots DIR] [--inject-absolute]
  python3 hub/ui/check_ui.py --base-url http://127.0.0.1:8099 --idents admin,user --model cube.stl --slice-timeout 180
--inject-absolute fires one deliberate fetch('/api/me') to show the check catches it.
--base-url tests a real hub (the image) instead of the mock: ingress_proxy.py puts it under the ingress prefix and adds
the identity headers. --model is uploaded in the new-print step (a real slicer needs a real model).
Any HTTP status >= 400 fails the run, except the 403 paths given with --expect-403."""
import argparse
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX = "/api/hassio_ingress/MOCKTOKEN/"
sys.path.insert(0, HERE)
import ingress_proxy  # noqa: E402
MODEL = {"name": "demo_part.stl", "mimeType": "model/stl", "buffer": b"solid x\nendsolid x\n"}
SLICE_TIMEOUT = 15000
SIZES = {"390x844": (390, 844), "1440x900": (1440, 900)}
H2S = "FAKE-H2S-0001"


# What is on the 3D canvas: lit pixels (feature colours, not the plate or grid), a checksum, the lit width, and purple (infill) pixels.
PIXELS = """() => {
  const c = document.querySelector('canvas.view3d'), t = document.createElement('canvas');
  t.width = c.width; t.height = c.height;
  const g = t.getContext('2d'); g.drawImage(c, 0, 0);
  const d = g.getImageData(0, 0, t.width, t.height).data;
  let lit = 0, sum = 0, purple = 0, x0 = 1e9, x1 = -1;
  for (let i = 0, p = 0; i < d.length; i += 4, p++) {
    const r = d[i], gg = d[i + 1], b = d[i + 2];
    sum = (sum * 31 + r + gg * 3 + b * 7) % 2147483647;
    if (Math.max(r, gg, b) > 140) {
      lit++; const x = p % t.width;
      if (x < x0) x0 = x; if (x > x1) x1 = x;
      if (b > 150 && gg < 130 && r > 90) purple++;
    }
  }
  return { lit, sum, purple, w: x1 - x0 };
}"""


def check_viewer(page, shot):
    """The 3D preview must really draw, and every control must change what is drawn."""
    page.wait_for_selector('.preview3d[data-ready="1"]', timeout=15000)
    page.wait_for_timeout(200)
    read = lambda: (page.wait_for_timeout(200), page.evaluate(PIXELS))[1]
    text = lambda: page.locator(".preview3d small[aria-live]").inner_text()
    setrange = lambda name, v: page.evaluate("([n, v]) => { const e = document.querySelector(`.preview3d input[aria-label='${n}']`); e.value = v; e.dispatchEvent(new Event('input', { bubbles: true })); }", [name, v])

    full = read()
    assert full["lit"] > 800, f"the canvas draws no toolpaths: {full}"
    assert re.fullmatch(r"Layers 1 to 60 of 60 · Z 0\.20 to 12\.00 mm", text()), text()
    assert page.locator(".preview3d .legend .key").count() >= 4, "legend lists the features"

    # layer range: the to-handle by script, then the real handle by mouse
    setrange("To layer", 20)
    part = read()
    assert text().startswith("Layers 1 to 21 of 60"), text()
    assert part["sum"] != full["sum"] and part["lit"] < full["lit"] * 0.8, f"layer range changes nothing: {part} vs {full}"
    page.locator(".preview3d .range2").scroll_into_view_if_needed()  # mouse coordinates are viewport coordinates
    r = page.locator(".preview3d .range2").bounding_box()
    y = r["y"] + r["height"] / 2
    page.mouse.move(r["x"] + 11 + (r["width"] - 22) * 20 / 59, y)  # on the to-handle (22 px thumb)
    page.mouse.down(); page.mouse.move(r["x"] + 11 + (r["width"] - 22) * 0.5, y, steps=6); page.mouse.up()
    assert re.match(r"Layers 1 to (2[6-9]|3\d) of 60", text()), "dragging the to-handle: " + text()
    setrange("From layer", 40)  # past the to-handle: it pushes it along
    assert text().startswith("Layer 41 of 60"), text()
    page.get_by_role("button", name="One layer").click()
    setrange("From layer", 10)
    one = read()
    assert text() == "Layer 11 of 60 · Z 2.20 mm", text()
    assert one["lit"] < full["lit"] * 0.4, f"one layer should draw much less: {one} vs {full}"
    if page.viewport_size["width"] < 500:
        shot("viewer-one-layer")
    page.get_by_role("button", name="One layer").click()
    setrange("From layer", 0); setrange("To layer", 59)
    assert read()["sum"] == full["sum"], "all layers draw the same picture again"

    # feature toggle
    key = page.locator(".preview3d .legend .key", has_text="Sparse infill")
    assert full["purple"] > 50, f"infill colour is on screen: {full}"
    key.click()
    off = read()
    assert key.get_attribute("aria-pressed") == "false" and off["sum"] != full["sum"] and off["purple"] < full["purple"] * 0.1, f"infill toggle: {off} vs {full}"
    key.click()
    assert read()["sum"] == full["sum"]

    # travel moves are hidden by default
    page.get_by_role("button", name="Travel moves").click()
    assert read()["sum"] != full["sum"], "travel toggle changes nothing"
    page.get_by_role("button", name="Travel moves").click()

    # camera: orbit, wheel, reset
    page.locator("canvas.view3d").scroll_into_view_if_needed()
    box = page.locator("canvas.view3d").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy); page.mouse.down(); page.mouse.move(cx + 80, cy - 30, steps=5); page.mouse.up()
    orbit = read()
    assert orbit["sum"] != full["sum"], "orbit changes nothing"
    page.mouse.wheel(0, -400)
    assert read()["sum"] != orbit["sum"], "wheel zoom changes nothing"
    page.get_by_role("button", name="Reset view").click()
    assert read()["sum"] == full["sum"], "reset view returns to the first picture"

    # explode (the mock plate has two objects)
    ex = page.locator(".preview3d input[aria-label='Explode']")
    assert ex.is_enabled(), "explode needs a two-object file"
    setrange("Explode", 100)
    boom = read()
    assert boom["sum"] != full["sum"] and boom["w"] > full["w"] * 1.1, f"explode should spread the objects: {boom} vs {full}"
    if page.viewport_size["width"] < 500:
        shot("viewer-exploded")
    setrange("Explode", 0)
    assert read()["sum"] == full["sum"]

    # one object: nothing to explode
    note = page.evaluate("""async () => {
      const { gcodePreview } = await import(new URL('views/preview.js', location.href));
      const lines = ['M83'];
      for (let n = 1; n <= 3; n++) lines.push('; CHANGE_LAYER', '; Z_HEIGHT: ' + n * 0.2, 'G1 Z' + n * 0.2, '; start printing object, unique label id: 7',
        '; FEATURE: Outer wall', 'G1 X10 Y10', 'G1 X30 Y10 E1', 'G1 X30 Y30 E1', '; stop printing object, unique label id: 7');
      const p = gcodePreview(); document.body.append(p); await p.load(lines.join('\\n'));
      const out = { disabled: p.querySelector("input[aria-label='Explode']").disabled, note: p.querySelector('.explode small:last-child').textContent };
      p.remove(); return out;
    }""")
    assert note["disabled"] and note["note"] == "One object on this plate", note


def walk(page, ident, base, shot, inject):
    def go(h, wait):
        page.evaluate("h => { location.hash = h }", h)
        page.wait_for_selector(wait, timeout=8000)
        page.wait_for_timeout(250)

    page.goto(f"{base}/?as={ident}")
    if ident == "admin-new":
        page.wait_for_selector("#view")
        page.evaluate("location.hash = '#/setup'")  # a second run finds the hub already set up by the first
        page.wait_for_selector(".wizard h1")
        for i, name in enumerate(["setup-1-account", "setup-2-printers", "setup-3-done"], 1):
            if i > 1:
                page.get_by_role("button", name="Next").first.click()
                page.wait_for_timeout(400)
            page.wait_for_selector(".wizard .card")
            shot(name)
            if i == 2:  # saving a printer must come back as saved: the form shows the stored values again
                card = page.locator("section.card", has_text="Workshop H2S").first
                card.get_by_label("Name", exact=True).fill("Workshop H2S (renamed)")
                card.get_by_role("button", name="Save printer").click()
                page.wait_for_selector("text=Printer saved")
                page.wait_for_function("[...document.querySelectorAll('input')].some(i => i.value === 'Workshop H2S (renamed)')", timeout=5000)
        return
    page.wait_for_selector(".pcard")
    if inject:
        page.evaluate("fetch('/api/me')")
        page.wait_for_timeout(300)
    shot("printers")
    go("#/printers/" + H2S, ".cam.big")
    shot("printer-detail")
    if ident == "admin":
        go("#/printers/FAKE-P1S-0002", ".cam.big")
        btn = page.get_by_role("button", name="Start “")
        assert btn.is_disabled(), "start must be disabled without the checkbox"
        page.locator("#plate-clear").check()
        assert not btn.is_disabled()
        shot("printer-idle-start-armed")
        assert page.locator("section.card h2", has_text="Start next").count() == 1, "queued job: start card is shown"
    if ident == "admin":
        go("#/printers/FAKE-X1C-0003", ".hms")
        assert page.locator("section.card h2", has_text="Start next").count() == 0, "empty queue: no start card"
        shot("printer-offline-hms")
    go("#/queue", ".job")
    shot("queue")
    page.get_by_role("button", name="History").click()
    page.wait_for_selector(".job")
    shot("queue-history")
    # new print: upload, pick printer + tray, slice 3 variants, pick
    go("#/new", ".drop")
    page.set_input_files("input[type=file]", files=[MODEL])
    page.get_by_role("button", name="Workshop H2S").click()
    page.locator(".chip", has_text="AMS 1·1").click()
    shot("new-print-ready")
    page.get_by_role("button", name="Slice good, better and best").click()
    page.wait_for_selector(".variant[aria-pressed=true]", timeout=SLICE_TIMEOUT)
    check_viewer(page, shot)
    shot("new-print-compare")
    page.get_by_role("button", name="Add Better to queue").click()
    page.wait_for_selector(".job")
    shot("queue-after-add")
    go("#/filament", ".spool")
    shot("filament")
    if ident == "admin":
        page.get_by_role("button", name="Edit").first.click()
        page.wait_for_selector("dialog[open]")
        shot("filament-edit")
        page.get_by_label("Price per kg (EUR)").fill("")  # "no price" is allowed
        page.get_by_role("button", name="Save", exact=True).click()
        page.wait_for_selector("text=Saved")
        page.wait_for_selector("dialog[open]", state="detached")
        go("#/access", "table")
        shot("access")
        page.locator("label.field", has_text="Home Assistant user").locator("select").select_option(label="Jonas Example")
        page.locator("label.field", has_text="Printer").locator("select").first.select_option(label="Office P1S")
        page.get_by_role("button", name="Add grant").click()
        page.wait_for_selector("text=Grant added")
        row = page.locator("tr", has_text="Jonas Example").filter(has_text="Office P1S").first
        row.wait_for()
        assert "Active" in row.text_content() and "Office P1S" in row.text_content(), "new grant row shows: " + row.text_content()
        go("#/setup", ".wizard .card")
        shot("setup-menu-reopen")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots")
    ap.add_argument("--inject-absolute", action="store_true")
    ap.add_argument("--base-url")
    ap.add_argument("--idents", default="admin,user,admin-new")
    ap.add_argument("--model")
    ap.add_argument("--slice-timeout", type=int, default=15)
    ap.add_argument("--expect-403", default="")
    a = ap.parse_args()
    global PREFIX, MODEL, SLICE_TIMEOUT
    SLICE_TIMEOUT = a.slice_timeout * 1000
    if a.model:
        MODEL = {"name": os.path.basename(a.model), "mimeType": "model/stl", "buffer": open(a.model, "rb").read()}
    if a.base_url:
        u = urlparse(a.base_url)
        srv = ingress_proxy.start((u.hostname, u.port))
        PREFIX = ingress_proxy.PREFIX
        port = srv.server_address[1]
    else:
        port = 18099
        srv = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_server.py"), str(port)], stdout=subprocess.DEVNULL)
        time.sleep(0.8)
    expected_403 = [x for x in a.expect_403.split(",") if x]
    base, problems, count = f"http://127.0.0.1:{port}", [], 0
    if a.shots:
        os.makedirs(a.shots, exist_ok=True)
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])  # software WebGL for the 3D preview
            for ident in a.idents.split(","):
                for sz, (w, h) in SIZES.items():
                    ctx = br.new_context(viewport={"width": w, "height": h}, is_mobile=w < 500, has_touch=w < 500)
                    page = ctx.new_page()
                    where = f"{ident}@{sz}"
                    page.on("console", lambda m, wh=where: m.type == "error" and problems.append(f"console error [{wh}]: {m.text}"))
                    page.on("pageerror", lambda e, wh=where: problems.append(f"page error [{wh}]: {e}"))
                    page.on("requestfailed", lambda r, wh=where: problems.append(f"request failed [{wh}]: {r.url}"))

                    def on_req(r, wh=where):
                        nonlocal count
                        u = urlparse(r.url)
                        if u.scheme not in ("http", "https"):
                            return
                        count += 1
                        if u.path != "/" and not u.path.startswith(PREFIX):
                            problems.append(f"absolute path [{wh}]: {r.method} {u.path}")
                    page.on("request", on_req)

                    def on_resp(r, wh=where):
                        u = urlparse(r.url)
                        if r.status >= 400 and not (r.status == 403 and any(x in u.path for x in expected_403)):
                            problems.append(f"HTTP {r.status} [{wh}]: {r.request.method} {u.path[len(PREFIX):] or u.path}")
                    page.on("response", on_resp)
                    shot = lambda name, s=sz, i=ident, pg=page: a.shots and pg.screenshot(path=f"{a.shots}/{i}-{s}-{name}.png", full_page=True)
                    try:
                        walk(page, ident, base, shot, a.inject_absolute and ident == "admin" and sz == "390x844")
                    except Exception as e:
                        problems.append(f"walk failed [{where}]: {type(e).__name__}: {(str(e).splitlines() or [''])[0]}")
                    ctx.close()
            br.close()
    finally:
        srv.terminate() if hasattr(srv, "terminate") else srv.shutdown()
    if problems:
        print(f"FAIL: {len(problems)} problem(s) in {count} requests")
        for x in dict.fromkeys(problems):
            print("  -", x)
        sys.exit(1)
    print(f"PASS: no console errors, no page errors, all {count} requests stayed under the ingress prefix")


main()
