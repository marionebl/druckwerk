#!/usr/bin/env python3
"""Checks for the pollers of the web UI against the mock server: a hidden tab sends no request, one call at a time.

  python3 hub/ui/check_poll.py [--port 18099]      exit code 1 when a check fails"""
import argparse
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
HIDDEN = """(() => {
  window.__hidden = false;
  Object.defineProperty(document, 'hidden', { get: () => window.__hidden });
  window.__show = (v) => { window.__hidden = !v; document.dispatchEvent(new Event('visibilitychange')); };
})()"""

SLOW = """(() => {
  const f = window.fetch; let now = 0; window.__peak = 0; window.__slow = false;
  window.fetch = async (u, o) => {
    if (!window.__slow || !String(u).includes('api/printers')) return f(u, o);
    window.__peak = Math.max(window.__peak, ++now);
    try { await new Promise((r) => setTimeout(r, 9000)); return await f(u, o); } finally { now--; }
  };
})()"""


def polls(page, base, seconds):
    """Requests to api/printers during `seconds`."""
    seen = []
    on = lambda r: "api/printers" in r.url and r.method == "GET" and seen.append(time.time())
    page.on("request", on)
    page.wait_for_timeout(int(seconds * 1000))
    page.remove_listener("request", on)
    return len(seen)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18099)
    a = ap.parse_args()
    srv = subprocess.Popen([sys.executable, os.path.join(HERE, "mock_server.py"), str(a.port)], stdout=subprocess.DEVNULL)
    time.sleep(0.8)
    base, problems = f"http://127.0.0.1:{a.port}/?as=admin", []
    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            page = br.new_context(viewport={"width": 1280, "height": 800}).new_page()
            page.add_init_script(HIDDEN)
            page.goto(base + "#/printers")
            page.wait_for_selector(".card, .pcard, h1, main", timeout=8000)
            page.wait_for_timeout(500)
            page.evaluate("window.__show(false)")
            n = polls(page, base, 12)
            print("requests of a hidden tab in 12 s:", n)
            if n:
                problems.append(f"hidden tab sent {n} requests")
            before = []
            page.on("request", lambda r: "api/printers" in r.url and before.append(1))
            page.evaluate("window.__show(true)")
            page.wait_for_timeout(500)
            print("requests when the tab shows again:", len(before))
            if not before:
                problems.append("no request when the tab became visible")

            # one slow answer: the ticks of the next 14 s must not pile up behind it
            fresh = br.new_context(viewport={"width": 1280, "height": 800}).new_page()
            fresh.add_init_script(SLOW)
            fresh.goto(base + "#/printers")
            fresh.wait_for_timeout(1000)
            fresh.evaluate("window.__slow = true")
            fresh.wait_for_timeout(14000)
            peak = fresh.evaluate("window.__peak")
            print("peak of printer list calls in flight with a 9 s answer:", peak)
            if peak > 1:
                problems.append(f"peak in flight {peak}")
            br.close()
    finally:
        srv.terminate()
    for x in problems:
        print("FAIL", x)
    print("poll checks:", "FAILED" if problems else "ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
