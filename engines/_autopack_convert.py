"""Convert a ComfyUI template (the UI format) to the API format with the LANE'S OWN page.

`graphToPrompt` belongs to the lane's frontend and changes between releases, which is why a
Python port fails on most templates. This loads the lane's page in headless Chromium (through
Playwright, an OPTIONAL dependency: requirements-converter.txt), hands it the template, and asks
that page for the prompt it would queue. Nothing is queued and nothing is saved on the lane.

Isolation: every request whose origin is not the lane's is aborted, so the page can reach
nothing else; the browser sandbox stays ON unless the user turned it off in config.json
(never silently); one conversion at a time per process; one browser per call, closed before
returning. Playwright is imported inside the functions, so nothing else needs it.
"""

import os
import threading
from urllib.parse import urlsplit

INSTALL = ("Run: pip install -r requirements-converter.txt, then: python -m playwright install chromium. "
           "Or use Workflow > Export (API) in ComfyUI and pass the file with --api-json.")
CHANGED = "This ComfyUI's page changed in a way the converter does not understand. Use Export (API) in ComfyUI instead."

_LOCK = threading.Lock()
_JS = """async (wf) => {
  const app = window.app;
  await app.loadGraphData(wf, true, true, 'bwf-pack');
  const r = await app.graphToPrompt();
  return {output: r && r.output, v: window.__COMFYUI_FRONTEND_VERSION__ || null};
}"""


class ConverterUnavailable(Exception):
    """The conversion cannot run here. `.sentence` is plain English, shown as it stands."""

    def __init__(self, sentence):
        super().__init__(sentence)
        self.sentence = sentence


def _origin(url):
    p = urlsplit(url)
    try:
        port = p.port
    except ValueError:
        port = -1
    return (p.scheme, (p.hostname or "").lower(), port or {"http": 80, "https": 443}.get(p.scheme))


def _same_origin(url, lane_url):
    """True when `url` is on the lane's scheme://host:port."""
    return _origin(url) == _origin(lane_url)


def converter_status(sandbox=True):
    """(ok, sentence): whether Playwright and a Chromium that starts are here. Starts the
    browser once and closes it (a few seconds); never raises."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False, "The browser converter is not installed. " + INSTALL
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
            if not path or not os.path.exists(path):
                return False, "Playwright is installed but its Chromium is not downloaded. Run: python -m playwright install chromium"
            p.chromium.launch(headless=True, chromium_sandbox=sandbox).close()
    except Exception as e:
        why = str(e).splitlines()[0][:120] if str(e) else type(e).__name__
        if sandbox:
            return False, ('The browser could not start with its sandbox on (%s). If this machine cannot run it, set '
                           '"pack_builder": {"browser_sandbox": false} in config.json.' % why)
        return False, "The browser converter could not start (%s). %s" % (why, INSTALL)
    return True, "Playwright and its Chromium are present and start."


def convert(lane_url, template_ui, timeout_s=90.0, sandbox=True):
    """-> {"prompt": <API graph>, "frontend_version": str|None, "blocked": [urls refused]}.
    Raises ConverterUnavailable (with a sentence) when it cannot run or the page is not understood."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise ConverterUnavailable("The browser converter is not installed. " + INSTALL)
    lane_url = lane_url.rstrip("/")
    blocked = []

    def gate(route):
        url = route.request.url
        try:
            if url.startswith(("data:", "blob:", "about:")) or _same_origin(url, lane_url):
                route.continue_()
            else:
                blocked.append(url[:200])
                route.abort()
        except Exception:
            pass

    with _LOCK:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, chromium_sandbox=sandbox)
            except Exception as e:
                why = str(e).splitlines()[0][:160] if str(e) else type(e).__name__
                if sandbox:
                    raise ConverterUnavailable('The browser could not start with its sandbox on (%s). If this machine cannot run it, set '
                                               '"pack_builder": {"browser_sandbox": false} in config.json, or use Export (API) in ComfyUI.' % why)
                raise ConverterUnavailable("The browser could not start (%s). %s" % (why, INSTALL))
            try:
                page = browser.new_context().new_page()
                page.set_default_timeout(int(timeout_s * 1000))
                page.route("**/*", gate)
                page.goto(lane_url + "/", wait_until="domcontentloaded")
                page.wait_for_function("window.app && window.app.graphToPrompt && window.app.loadGraphData", timeout=60000)
                page.wait_for_timeout(2000)        # let extensions register their widgets
                result = page.evaluate(_JS, template_ui)
            except Exception:
                raise ConverterUnavailable(CHANGED)
            finally:
                browser.close()
    out = result.get("output") if isinstance(result, dict) else None
    if not out or not isinstance(out, dict):
        raise ConverterUnavailable(CHANGED)
    return {"prompt": out, "frontend_version": result.get("v"), "blocked": blocked}
