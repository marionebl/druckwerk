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
import traceback
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX = "/api/hassio_ingress/MOCKTOKEN/"
sys.path.insert(0, HERE)
import ingress_proxy  # noqa: E402
MODEL = {"name": "demo_part.stl", "mimeType": "model/stl", "buffer": b"solid x\nendsolid x\n"}
SLICE_TIMEOUT = 15000
SLICE_ON = None
MOCKED = True  # False against a real hub (--base-url): no mock hooks, other spool names
ONLY = None
ALLOW_502 = []  # path fragments whose 502 answer is the point of the step
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
    if MODEL["name"] != "demo_part.stl":  # a real slice (image run): the checks below need the mock's two-object plate
        m = re.fullmatch(r"Layers 1 to (\d+) of (\d+) · Z .* mm", text())
        assert m and m[1] == m[2], text()
        assert full["lit"] > 50, f"the canvas draws no toolpaths: {full}"
        setrange("To layer", int(m[2]) // 2)
        assert read()["sum"] != full["sum"], "layer range changes nothing"
        return
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


def check_cloud_down(page, go, shot):
    """The mock's cloud library stops answering: the tab says so instead of showing old spools."""
    ALLOW_502.append("api/spools")
    page.evaluate("fetch('api/_mock/spools-down?on=1')")
    go("#/queue", ".job")
    go("#/filament", ".empty")
    assert "filament library" in page.locator(".empty").text_content() and page.locator(".spool").count() == 0, "the error replaces the list"
    shot("filament-error")
    page.evaluate("fetch('api/_mock/spools-down?on=0')")
    ALLOW_502.clear()


def check_load_request(page, base, go, shot):
    """The user asks for a spool, the admin sees the pill, the user cancels."""
    card = lambda: page.locator(".spool", has_text="PLA Matte Charcoal")
    card().get_by_role("button", name="Ask to load").click()
    page.wait_for_selector("dialog[open]")
    page.locator("dialog label.field", has_text="Printer").locator("select").select_option(label="Office P1S")
    page.get_by_label("Note").fill("for the grey bracket")
    shot("filament-load-modal")
    page.get_by_role("button", name="Send", exact=True).click()
    page.wait_for_selector("dialog[open]", state="detached")
    pill = "Requested for Office P1S by Lena Tester"
    card().get_by_text(pill).wait_for()
    assert card().get_by_role("button", name="Cancel request").count() == 1, "the requester may cancel"
    shot("filament-requested")
    page.goto(f"{base}/?as=admin")  # the owner's view of the same request
    go("#/filament", ".spool")
    card().get_by_text(pill).wait_for()
    assert card().get_by_role("button", name="Cancel request").count() == 1, "an admin may cancel"
    page.goto(f"{base}/?as=user")
    go("#/filament", ".spool")
    card().get_by_role("button", name="Cancel request").click()
    page.wait_for_selector(".spool:has-text('PLA Matte Charcoal') >> text=Requested for", state="detached")
    assert card().get_by_role("button", name="Ask to load").count() == 1, "the spool can be requested again"


JOB_CLIPS, TASK_BENCHY, TASK_CLIPS, TASK_CLOUD, TASK_OWNED_CLOUD, JOB_ONLY = "a00000000002", "5", "8", "7", "10", "job-b00000000004"
HOLDS = {"printers": ".pcard", "queue": ".job", "history": ".tl-card", "new": ".drop", "filament": ".spool", "access": "table"}


def fresh(page, url):
    """A full reload of this URL, hash included (a same-URL goto would only change the hash)."""
    page.goto("about:blank")
    page.goto(url)


def hash_is(page, want):
    page.wait_for_function("w => location.hash === w", arg=want, timeout=5000)


def check_deep_links(page, base, ident, go):
    """Every tab and every detail view opens straight from its URL after a full reload; a tab click changes the hash."""
    for tab, sel in HOLDS.items():
        if tab == "access" and ident != "admin":
            continue
        fresh(page, f"{base}/?as={ident}#/{tab}")
        page.wait_for_selector(sel, timeout=8000)
        assert page.locator(f"nav.tabs a[data-id={tab}][aria-current=page]").count() == 1, f"deep link #/{tab} marks its tab"
    for h, tab, text in [(f"#/history/{TASK_BENCHY}", "history", "Benchy for the shelf"), (f"#/queue/{JOB_CLIPS}", "queue", "Cable clips for the desk"),
                         ("#/filament/5000", "filament", "Acme PLA")]:
        fresh(page, f"{base}/?as={ident}{h}")
        page.wait_for_selector(".detail", timeout=8000)
        assert text in page.locator("h1").text_content(), f"{h} opens {text}: " + page.locator("h1").text_content()
        assert page.locator(f"nav.tabs a[data-id={tab}][aria-current=page]").count() == 1
        assert page.url.endswith(h), "the hash stays " + h
    page.locator("nav.tabs a[data-id=history]").click()
    hash_is(page, "#/history")
    page.locator("nav.tabs a[data-id=filament]").click()
    hash_is(page, "#/filament")
    fresh(page, f"{base}/?as={ident}#/history/nothing-here")
    page.get_by_text("not in the history").wait_for()


def check_inside_ha_panel(page, base, ident):
    """HA shows the app in an iframe whose own URL never carries our hash: the frame reads the panel URL's hash and writes its route back."""
    page.route(f"{base}/?host=1", lambda r: r.fulfill(content_type="text/html", body=f'<iframe id="f" src="{base}/?as={ident}" style="width:100%;height:700px"></iframe>'))
    page.goto(f"{base}/?host=1#/history/{TASK_BENCHY}")
    fr = page.frame_locator("#f")
    fr.locator(".detail h1").wait_for(timeout=8000)
    assert "Benchy for the shelf" in fr.locator("h1").text_content(), "the iframe opens the panel URL's hash"
    fr.locator("nav.tabs a[data-id=queue]").click()
    page.wait_for_function("() => location.hash === '#/queue'", timeout=5000)
    page.unroute(f"{base}/?host=1")
    fresh(page, f"{base}/?as={ident}")  # back to the app itself for the rest of the walk


def check_history_list(page, base, ident, go, shot):
    go("#/history", ".tl-card")
    assert page.locator(".seg").count() == 0
    merged = page.locator(".tl-card", has_text="Benchy for the shelf")
    assert merged.count() == 1 and "benchy_test" not in merged.text_content(), "a merged item shows the Druckwerk name"
    only = page.locator(".tl-card", has_text="wall_hook_test")
    assert only.count() == 1 and "Druckwerk only" in only.text_content(), "a job without a cloud task is listed and marked"
    if ident == "admin":
        cloud = page.locator(".tl-card", has_text="Slicer plate 1")
        assert cloud.count() == 1 and "Not started here" in cloud.text_content(), "a cloud-only item with no owner grant keeps the old text"
        assert "Printer owner: Jonas Example" in page.locator(".tl-card", has_text="Slicer plate 3").text_content(), "no record: the printer owner is named"
        # a thumbnail that fails keeps its box: same size, placeholder inside
        box = cloud.locator(".thumb")
        page.wait_for_function("() => document.querySelectorAll('.tl-card .thumb.ph').length > 1")
        ref = page.locator(".tl-card", has_text="Benchy for the shelf").locator(".thumb")
        b1, b2 = box.bounding_box(), ref.bounding_box()
        assert (b1["width"], b1["height"]) == (b2["width"], b2["height"]) == (56, 56), f"placeholder box keeps its size: {b1} {b2}"
        assert cloud.locator(".thumb img").count() == 0 and "ph" in box.get_attribute("class")
    page.locator("nav.tabs a[data-id=queue]").click()
    page.wait_for_selector(".page-head h1:has-text('Queue')")
    assert page.get_by_role("button", name="History").count() == 0 and page.locator(".seg").count() == 0, "the queue has no History segment"


def check_reslice_lands(page):
    # New print opens with what the old print had: model, name, target, filament (AMS 1·1), build plate
    page.wait_for_selector(".drop b:has-text('cable_clip_set.stl')", timeout=8000)
    assert page.locator(".chip[aria-pressed=true]", has_text="AMS 1·1").count() == 1, "the old filament is preselected"
    assert page.locator(".chip[aria-pressed=true]", has_text="Workshop H2S").count() == 1, "the old printer is preselected"
    assert page.locator("select option:checked", has_text="Textured PEI Plate").count() >= 1
    page.get_by_role("button", name="Slice good, better and best").click()
    page.wait_for_selector(".variant[aria-pressed=true]", timeout=SLICE_TIMEOUT)
    assert page.locator(".variant[aria-pressed=true]", has_text="Best").count() == 1, "the old quality is the pick"
    assert page.locator("input[type=text][maxlength='80']").input_value() == "Cable clip set (desk)", "the old name is filled in"


def check_reslice(page, base, ident):
    fresh(page, f"{base}/?as={ident}#/history/{TASK_CLIPS}")
    page.wait_for_selector(".detail")
    page.get_by_role("button", name="Reslice").click()
    hash_is(page, "#/new")
    check_reslice_lands(page)


GROUPS = ["Quality", "Strength", "Support and adhesion", "Speed", "Filament", "Plate and bed", "Printer"]
SAME = "New print job with the same settings"


def check_settings(page, base, ident, shot):
    """The history detail shows all slicing settings; the one primary action queues the same file with the same options."""
    page.wait_for_selector(".sgroup", timeout=8000)
    assert page.locator(".sgroup h3").all_text_contents() == GROUPS, "the settings are grouped"
    body = page.locator(".detail").text_content()
    for want in ("Layer height", "0.2 mm", "Infill density", "15%", "Nozzle temperature", "Flow ratio", "Build plate", "Read from the sliced file."):
        assert want in body, f"settings show {want!r}"
    all_ = page.locator("details.allsettings")
    assert all_.locator("summary").text_content().startswith("All settings (") and all_.get_attribute("open") is None
    all_.locator("summary").click()
    all_.locator("dt").first.wait_for()
    assert all_.locator("dt").count() == 8, "all settings list every key"
    all_.get_by_label("Find a setting").fill("infill")
    page.wait_for_function("() => document.querySelectorAll('details.allsettings dt').length === 1")
    assert all_.locator("dt").all_text_contents() == ["sparse_infill_density"], "the list can be searched"
    all_.get_by_label("Find a setting").fill("")
    shot("history-detail")
    assert page.get_by_role("button", name=SAME).count() == 1 and page.get_by_role("button", name="Print again").count() == 0, "one action, not two"
    assert page.locator(".btn.primary", has_text=SAME).count() == 1, "it is the primary button"
    page.get_by_role("button", name=SAME).click()
    page.wait_for_function("() => /^#\\/queue\\/[0-9a-f]{12}$/.test(location.hash)", timeout=5000)
    page.wait_for_selector("h2:has-text('Change this job')")
    assert page.locator("h1").text_content() == "Cable clip set (desk)", "the name is kept"
    assert page.locator("input[data-opt=timelapse]").is_checked() and page.locator("select[data-opt=flow_cali]").input_value() == "auto" \
        and page.locator("select[data-opt=bed_leveling]").input_value() == "false", "the new job has the options of the print"
    page.go_back()
    page.wait_for_selector(".sgroup")


def check_history_details(page, base, ident, go, shot):
    go("#/history", ".tl-card")
    page.locator(".tl-card", has_text="Cable clip set (desk)").click()
    hash_is(page, f"#/history/{TASK_CLIPS}")
    page.wait_for_selector(".detail")
    body = page.locator(".detail").text_content()
    for want in ("Print options", "Timelapse", "Slice", "Best", "0.20mm Standard", "Bambu PLA Basic", "Spools used", "Cloud title: cable_clip_set"):
        assert want in body, f"history detail shows {want!r}"
    assert page.locator("a.spoolprint[href='#/filament/5000']").count() == 1, "the spool links to the filament detail"
    page.wait_for_selector(".preview3d", timeout=8000)  # the slice is still stored: the 3D preview
    check_settings(page, base, ident, shot)
    page.get_by_role("button", name="Reslice").click()
    hash_is(page, "#/new")
    check_reslice_lands(page)
    fresh(page, f"{base}/?as={ident}#/history/{TASK_BENCHY}")  # settings kept, file gone: the one action opens New print with the options
    page.wait_for_selector(".sgroup", timeout=8000)
    assert "Saved when the job was queued." in page.locator(".detail").text_content() and "The sliced file is gone" in page.locator(".detail").text_content()
    page.get_by_role("button", name=SAME).click()
    hash_is(page, "#/new")
    page.wait_for_selector(".toast:has-text('model is no longer stored')")
    fresh(page, f"{base}/?as={ident}#/history/{JOB_ONLY}")
    page.wait_for_selector(".detail")
    body = page.locator(".detail").text_content()
    assert "Druckwerk only" in body and "The printer is busy" in body and "Print options" in body, "a job-only item shows its error and settings"
    page.wait_for_selector("h2:has-text('Slicing settings')")
    assert "No slicing settings for this print" in page.locator(".detail").text_content(), "a job without a file says so"
    if ident == "admin":
        fresh(page, f"{base}/?as={ident}#/history/{TASK_CLOUD}")
        page.wait_for_selector(".detail")
        body = page.locator(".detail").text_content()
        assert "Not started here" in body and "Print options" not in body, "a cloud-only print shows what the cloud knows"
        page.wait_for_selector("h2:has-text('Slicing settings')")
        body = page.locator(".detail").text_content()
        assert "No slicing settings for this print" in body and "What the Bambu cloud record has" in body and "profileId" in body and "4711" in body, "cloud-only without a file: the record's fields and the reason"
        assert page.get_by_role("button", name="New print job with the same settings").count() == 0, "no file and no job: no new print job"
        assert page.locator(".detail-img.ph").count() == 1 and page.locator(".detail-img img").count() == 0, "a broken cover leaves the placeholder box"
        shot("history-cloud-only")
        fresh(page, f"{base}/?as={ident}#/history/{TASK_OWNED_CLOUD}")  # the cloud gave a file: settings and the same action as for a Druckwerk print
        page.wait_for_selector(".sgroup", timeout=8000)
        assert "Read from the file of the Bambu cloud task." in page.locator(".detail").text_content()
        page.get_by_role("button", name=SAME).click()
        page.wait_for_function("() => /^#\\/queue\\/[0-9a-f]{12}$/.test(location.hash)", timeout=5000)
        page.wait_for_selector("h1:has-text('Slicer plate 3')")


def check_queue_details(page, base, ident, go, shot):
    fresh(page, f"{base}/?as={ident}#/queue/{JOB_CLIPS}")
    page.wait_for_selector(".detail")
    body = page.locator(".detail").text_content()
    for want in ("Change this job", "Flow dynamics calibration", "Slice", "Better", "0.20mm Standard"):
        assert want in body, f"queue detail shows {want!r}"
    page.wait_for_selector(".preview3d", timeout=8000)
    shot("queue-detail")
    # a fresh job to change and cancel: print the finished clip set again
    go(f"#/history/{TASK_CLIPS}", ".detail")
    page.get_by_role("button", name=SAME).click()
    page.wait_for_function("() => /^#\\/queue\\/[0-9a-f]{12}$/.test(location.hash)", timeout=5000)
    url = page.url
    page.wait_for_selector(".detail")
    page.get_by_label("Name", exact=True).fill("Clips, second run")
    page.locator("select[data-opt=flow_cali]").select_option("false")
    page.locator("input[data-opt=timelapse]").uncheck()
    page.get_by_role("button", name="Save changes").click()
    page.wait_for_selector("h1:has-text('Clips, second run')")
    fresh(page, url)  # reload keeps the view and the saved values
    page.wait_for_selector("h1:has-text('Clips, second run')")
    assert page.locator("select[data-opt=flow_cali]").input_value() == "false" and not page.locator("input[data-opt=timelapse]").is_checked()
    page.get_by_role("button", name="Cancel job").click()
    page.wait_for_selector("h2:has-text('Change this job')", state="detached")  # a cancelled job can no longer be changed
    assert page.locator(".detail .pill", has_text="Cancelled").count() == 1


def check_spool_detail(page, base, ident, go, shot):
    fresh(page, f"{base}/?as={ident}#/filament/5000")
    page.wait_for_selector("#spool-prints a.spoolprint")
    links = page.locator("#spool-prints a.spoolprint")
    assert links.count() >= 2 and page.locator("#spool-prints a[href='#/history/%s']" % TASK_BENCHY).count() == 1, "prints with the spool link to history"
    assert "Cable clip set (desk)" in page.locator("#spool-prints").text_content()
    shot("filament-detail")
    page.locator("#spool-prints a[href='#/history/%s']" % TASK_BENCHY).click()
    hash_is(page, f"#/history/{TASK_BENCHY}")
    go("#/filament", ".spool")
    page.locator(".spool a", has_text="Acme PLA").first.click()
    page.wait_for_selector(".detail")
    assert page.url.endswith("#/filament/5000")


def check_cache(page, base, ident):
    """Stale-while-revalidate: after one visit the printer and filament lists show at once from the cache while the API is slow or
    failing; the loading text is only for a first visit; a change drops the cached list; a throwing localStorage changes nothing."""
    user = "u-alex" if ident == "admin" else "u-lena"
    for tab, sel in (("printers", ".pcard"), ("filament", ".spool")):
        fresh(page, f"{base}/?as={ident}#/{tab}")
        page.wait_for_selector(sel, timeout=8000)
    page.evaluate("fetch('api/_mock/slow')")  # a failed earlier run may have left the delay on
    slow = lambda s: page.evaluate("s => fetch('api/_mock/slow?path=api/printers&path=api/spools&path=api/load-requests&s=' + s)", s)
    slow(3)
    for tab, sel, text in (("printers", ".pcard", "Workshop H2S"), ("filament", ".spool", "PLA Basic White")):
        t0 = time.time()
        fresh(page, f"{base}/?as={ident}#/{tab}")
        page.wait_for_selector(sel, timeout=1500)  # the API needs 3 s: this can only be the cache
        assert time.time() - t0 < 2.5 and page.locator("#view .loading").count() == 0, f"{tab}: cached data without a spinner ({time.time() - t0:.1f} s, {page.locator('#view .loading').count()} spinners)"
        assert text in page.locator("#view").text_content()
    page.wait_for_timeout(3500)  # the fresh answer arrives and replaces it without trouble
    assert page.locator(".spool").count() == 5, f"{page.locator('.spool').count()} spools, page {page.url}"
    page.evaluate("fetch('api/_mock/slow')")
    # the cloud library fails: the cached spools still show at once
    ALLOW_502.append("api/spools")
    page.evaluate("fetch('api/_mock/spools-down?on=1')")
    fresh(page, f"{base}/?as={ident}#/filament")
    page.wait_for_selector(".spool", timeout=3000)
    assert page.locator(".empty").count() == 0 and page.locator("#view .loading").count() == 0, "failing API: the cached list stays"
    page.evaluate("fetch('api/_mock/spools-down?on=0')")
    ALLOW_502.clear()
    if ident == "admin":  # a change drops the cached list of that resource
        gone = page.evaluate("""async (u) => {
          const { api } = await import(new URL('api.js', location.href));
          const key = 'dw1:' + u + ':api/spools';
          await api.spools();
          const had = localStorage.getItem(key) !== null;
          await api.saveSpool('5003', { grams_left: 730 });
          return [had, localStorage.getItem(key)];
        }""", user)
        assert gone == [True, None], f"a spool change drops the cached list: {gone}"
    # no usable storage at all
    p2 = page.context.new_page()
    errs = []
    p2.on("pageerror", lambda e: errs.append(str(e)))
    p2.add_init_script("Object.defineProperty(window, 'localStorage', { get() { throw new Error('denied'); } })")
    p2.goto(f"{base}/?as={ident}#/filament")
    p2.wait_for_selector(".spool", timeout=8000)
    p2.goto(f"{base}/?as={ident}#/printers")
    p2.wait_for_selector(".pcard", timeout=8000)
    assert not errs, f"no storage: {errs}"
    p2.close()


def check_loading(page, go):
    """While the first answer is on its way a list says Loading, never "No ... yet"."""
    for tab, path, sel in [("history", "api/history", ".tl-card"), ("queue", "api/queue", ".job"), ("filament", "api/spools", ".spool")]:
        go("#/printers", ".pcard")
        page.evaluate("p => fetch('api/_mock/slow?path=' + p + '&s=1.5')", path)
        page.evaluate("t => { location.hash = '#/' + t }", tab)
        page.wait_for_selector("#view .loading", timeout=2000)
        txt = page.locator("#view").text_content()
        assert "Loading" in txt and "No " not in txt and "empty" not in txt and page.locator(".empty").count() == 0, f"{tab} while loading: {txt!r}"
        page.wait_for_selector(sel, timeout=8000)
        page.evaluate("fetch('api/_mock/slow')")
P1S = "FAKE-P1S-0002"
# Images that are on screen but show nothing: a broken image, or alt text where a picture should be.
BROKEN = """() => [...document.images].filter((i) => i.complete && i.naturalWidth === 0 && i.getClientRects().length
  && getComputedStyle(i).display !== 'none' && getComputedStyle(i).visibility !== 'hidden').map((i) => i.alt || i.src)"""


def hook(page, what, serial, on):
    page.evaluate("([w, s, o]) => fetch(`api/_mock/hook?set=${w}&serial=${s}&on=${o}`).then((r) => r.text())", [what, serial, on])


def must(page, selector, msg, timeout=6000):
    try:
        page.wait_for_selector(selector, timeout=timeout)
    except Exception:
        raise AssertionError(msg + " (broken images: %s)" % page.evaluate(BROKEN))


def check_printer_card(page, base, ident, go, shot):
    """A printer whose camera fails and whose AMS slots are all empty: a placeholder, no broken image, no tray row;
    the chamber light switch sends the command and shows the state."""
    ALLOW_502.append("/camera")
    hook(page, "camera_down", P1S, 1)
    hook(page, "empty_ams", P1S, 1)
    page.goto(f"{base}/?as={ident}")  # a fresh page: no earlier camera frame is kept
    go("#/printers", ".pcard")
    must(page, ".pcard:has-text('Office P1S') .off:has-text('No camera image')", "a failing camera shows a placeholder")
    assert page.evaluate(BROKEN) == [], "broken images on the printer grid: %s" % page.evaluate(BROKEN)
    p1s = page.locator(".pcard", has_text="Office P1S")
    assert p1s.locator(".tray").count() == 0, "an all-empty AMS has no tray row"
    assert "AMS empty" in p1s.text_content(), "the card says the AMS is empty"
    assert page.locator(".pcard", has_text="Workshop H2S").locator(".tray").count() > 0, "a loaded AMS keeps its trays"
    shot("printer-card-empty-ams")
    go("#/printers/" + P1S, ".cam.big")
    must(page, ".cam.big .off:has-text('No camera image')", "a failing camera shows a placeholder on the printer page")
    assert page.evaluate(BROKEN) == [], "broken images on the printer page"
    assert page.locator(".tray").count() == 0 and page.locator("section.card", has_text="Filament").get_by_text("AMS empty").count() == 1
    hook(page, "camera_down", P1S, 0)
    hook(page, "empty_ams", P1S, 0)
    # the light: admins switch it, a plain user sees the state only
    go("#/printers", ".pcard")
    sw = page.locator(".pcard", has_text="Workshop H2S").get_by_role("switch", name="Chamber light")
    sw.wait_for()
    if ident == "admin":
        sw.click()
        page.wait_for_selector(".pcard:has-text('Workshop H2S') [role=switch][aria-checked=true]")
        assert page.evaluate("fetch('api/_mock/light').then((r) => r.json())")[-1] == {"serial": H2S, "on": True}, "the light command was sent"
        go("#/printers/" + H2S, ".cam.big")
        detail = page.locator("section.card", has_text="Temperatures").get_by_role("switch", name="Chamber light")
        assert detail.get_attribute("aria-checked") == "true", "the page shows the new state"
        detail.click()
        page.wait_for_function("document.querySelector(\"section.card [role=switch]\").getAttribute('aria-checked') === 'false'")
        assert page.evaluate("fetch('api/_mock/light').then((r) => r.json())")[-1]["on"] is False
    else:
        assert sw.is_disabled(), "a user without admin rights cannot switch the light"
    ALLOW_502.clear()
    page.goto(f"{base}/?as={ident}")


def check_library_pick(page, go, shot):
    """New print for a printer with an empty AMS: the library is the list, and the chosen spool goes to the slicer."""
    hook(page, "empty_ams", P1S, 1)
    go("#/new", ".drop")
    page.set_input_files("input[type=file]", files=[MODEL])
    page.get_by_role("button", name="Office P1S").click()
    page.wait_for_selector("#fil-note")
    assert page.get_by_text("No AMS trays reported").count() == 0
    assert "AMS is empty" in page.locator("#fil-note").text_content()
    assert page.locator("#fil-ams").count() == 0 and page.locator("#fil-library .chip").count() == 1, "only the library spool that is in no printer"
    slice_btn = page.get_by_role("button", name="Slice good, better and best")
    assert slice_btn.is_disabled(), "nothing is chosen yet"
    page.locator("#fil-library .chip").click()
    assert "not in the AMS" in page.locator("#load-note").text_content()
    assert page.locator("#load-note").get_by_role("button", name="Ask to load").count() == 1
    assert not slice_btn.is_disabled()
    shot("newprint-filament-pick")
    if SLICE_ON:
        page.locator("#where").get_by_role("button", name=SLICE_ON).click()
    slice_btn.click()
    page.wait_for_selector(".variant[aria-pressed=true]", timeout=SLICE_TIMEOUT)
    sent = page.evaluate("fetch('api/_mock/slices').then((r) => r.json())")[-3:]
    want = {"type": "PLA", "brand": "Bambu Lab", "name": "PLA Matte Charcoal", "colour": "2E2E2E", "spool": "5004"}
    assert len(sent) == 3 and all({k: b.get(k) for k in want} == want and "tray" not in b for b in sent), "the slice request names the spool: %s" % sent
    hook(page, "empty_ams", P1S, 0)


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
                card.locator("select[data-field=owner]").select_option(label="Lena Tester")
                card.get_by_role("button", name="Save printer").click()
                page.wait_for_selector("text=Printer saved")
                page.wait_for_function("[...document.querySelectorAll('input')].some(i => i.value === 'Workshop H2S (renamed)')", timeout=5000)
                # the owner is stored; the user id differs between the mock and the real-image fakes, so match by label
                page.wait_for_function("[...document.querySelectorAll('select[data-field=owner]')].some(s => s.selectedOptions[0]?.textContent.includes('Lena Tester'))", timeout=5000)
        return
    if ONLY == "reslice":
        check_reslice(page, base, ident)
        return
    if ONLY == "cache":
        check_cache(page, base, ident)
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
    if MOCKED:
        check_printer_card(page, base, ident, go, shot)
    go("#/queue", ".job")
    shot("queue")
    if MOCKED:
        check_deep_links(page, base, ident, go)
        check_history_list(page, base, ident, go, shot)
        shot("history-list")
        check_history_details(page, base, ident, go, shot)
        check_queue_details(page, base, ident, go, shot)
        check_spool_detail(page, base, ident, go, shot)
        check_loading(page, go)
        if ident == "admin":
            check_inside_ha_panel(page, base, ident)
        go("#/queue", ".job")
    # new print: upload, pick printer + tray, slice 3 variants, pick
    go("#/new", ".drop")
    page.set_input_files("input[type=file]", files=[MODEL])
    page.get_by_role("button", name="Workshop H2S").click()
    page.locator(".chip", has_text="AMS 1·1").click()
    if SLICE_ON:  # the page defaults to "This device" where it is isolated; the real image run covers the Pi path here
        page.locator("#where").get_by_role("button", name=SLICE_ON).click()
    shot("new-print-ready")
    page.get_by_role("button", name="Slice good, better and best").click()
    page.wait_for_selector(".variant[aria-pressed=true]", timeout=SLICE_TIMEOUT)
    check_viewer(page, shot)
    shot("new-print-compare")
    page.get_by_role("button", name="Add Better to queue").click()
    page.wait_for_selector(".job")
    shot("queue-after-add")
    if MOCKED:
        check_library_pick(page, go, shot)
    go("#/filament", ".spool")
    shot("filament")
    assert page.locator(".spool").count() == 5, "the cloud library lists five spools: %d" % page.locator(".spool").count()
    assert page.locator("h3", has_text="Not loaded").count() == 1 and page.get_by_text("Bambu Cloud filament library").count() == 1, "not-loaded group and the subtitle"
    if MOCKED:
        assert "unknown" in page.locator(".spool", has_text="Matte Charcoal").text_content(), "a spool without grams says unknown"
        assert "of 1000 g" in page.locator(".spool", has_text="PLA Basic White").text_content()
    if ident == "admin":
        page.get_by_role("button", name="Edit").first.click()
        page.wait_for_selector("dialog[open]")
        shot("filament-edit")
        assert page.get_by_label("Loaded in").count() == 0, "the AMS location is read-only"
        page.get_by_label("Grams left").fill("700")
        page.get_by_label("Price per kg (EUR)").fill("")  # "no price" is allowed
        page.get_by_role("button", name="Save", exact=True).click()
        page.wait_for_selector("text=Saved")
        page.wait_for_selector("dialog[open]", state="detached")
        page.wait_for_selector(".spool:has-text('700 g')")
        assert "no price" in page.locator(".spool", has_text="PLA").first.text_content()
    if MOCKED and ident == "user":
        check_load_request(page, base, go, shot)
    # the cloud library does not answer: the tab says so instead of showing old spools
    if MOCKED:
        check_cloud_down(page, go, shot)
    if ident == "admin":
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
    ap.add_argument("--only", choices=["cache", "reslice"], help="run one check only")
    ap.add_argument("--port", type=int, default=18099, help="port of the mock server")
    ap.add_argument("--inject-absolute", action="store_true")
    ap.add_argument("--base-url")
    ap.add_argument("--idents", default="admin,user,admin-new")
    ap.add_argument("--model")
    ap.add_argument("--slice-timeout", type=int, default=15)
    ap.add_argument("--expect-403", default="")
    ap.add_argument("--slice-on", help="chip to press under Slice on, e.g. \"Pi (Bambu Studio)\"")
    a = ap.parse_args()
    global PREFIX, MODEL, SLICE_TIMEOUT, SLICE_ON, MOCKED, ONLY
    ONLY = a.only
    MOCKED = not a.base_url
    SLICE_ON = a.slice_on
    SLICE_TIMEOUT = a.slice_timeout * 1000
    if a.model:
        MODEL = {"name": os.path.basename(a.model), "mimeType": "model/stl", "buffer": open(a.model, "rb").read()}
    if a.base_url:
        u = urlparse(a.base_url)
        srv = ingress_proxy.start((u.hostname, u.port))
        PREFIX = ingress_proxy.PREFIX
        port = srv.server_address[1]
    else:
        port = a.port
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
                    page.on("console", lambda m, wh=where: m.type == "error" and not (ALLOW_502 and "status of 502" in m.text) and problems.append(f"console error [{wh}]: {m.text}"))
                    page.on("pageerror", lambda e, wh=where: problems.append(f"page error [{wh}]: {e}"))
                    page.on("requestfailed", lambda r, wh=where: r.failure != "net::ERR_ABORTED" and problems.append(f"request failed [{wh}]: {r.url} ({r.failure})"))  # a reload aborts what is in flight

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
                        if r.status >= 400 and not (r.status == 403 and any(x in u.path for x in expected_403)) and not (r.status == 502 and any(x in u.path for x in ALLOW_502)):
                            problems.append(f"HTTP {r.status} [{wh}]: {r.request.method} {u.path[len(PREFIX):] or u.path}")
                    page.on("response", on_resp)
                    shot = lambda name, s=sz, i=ident, pg=page: a.shots and pg.screenshot(path=f"{a.shots}/{i}-{s}-{name}.png", full_page=True)
                    try:
                        walk(page, ident, base, shot, a.inject_absolute and ident == "admin" and sz == "390x844")
                    except Exception as e:
                        problems.append(f"walk failed [{where}] at check_ui.py line {[f.lineno for f in traceback.extract_tb(e.__traceback__) if f.filename.endswith('check_ui.py')]}: {type(e).__name__}: {(str(e).splitlines() or [''])[0]}")
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
