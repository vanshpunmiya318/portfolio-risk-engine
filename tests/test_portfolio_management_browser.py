"""Optional Windows Chrome integration, using the installed browser and CDP."""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

import pytest

from riskengine.api import main
from riskengine.db.connection import get_connection
from riskengine.db.lifecycle import create_database
from test_local_access import BROWSER_HOST, CODE, serving
from test_portfolio_management import managed


CHROME = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe"
pytestmark = pytest.mark.skipif(os.name != "nt" or not CHROME.is_file() or not shutil.which("node"),
                               reason="Installed Windows Chrome and Node are required")


@contextmanager
def process(arguments, **options):
    with subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=subprocess.CREATE_NO_WINDOW, **options) as child:
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=20)


def wait_until(test, child=None, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = test()
        if result:
            return result
        if child and child.poll() is not None:
            break
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for browser test condition")


@contextmanager
def browser(tmp_path, backend_port):
    websockets = pytest.importorskip("websockets.sync.client")
    host, browser_port = BROWSER_HOST.split(":")
    env = dict(os.environ)
    env.pop("VITE_PORTFOLIO_ID", None)
    frontend = main.ROOT / "frontend"
    vite = f"""
        import {{ createServer }} from 'vite';
        await (await createServer({{server: {{host: '{host}', port: {browser_port}, strictPort: true, hmr: false,
          proxy: {{'/api': {{target: 'http://127.0.0.1:{backend_port}', changeOrigin: false}}}}}}}})).listen();
    """
    profile = tmp_path / "chrome"
    with process(["node", "--input-type=module", "-e", vite], cwd=frontend, env=env) as server:
        def ready():
            try:
                with socket.create_connection((host, int(browser_port)), timeout=0.25):
                    return True
            except OSError:
                return False

        wait_until(ready, server)
        with process([str(CHROME), "--headless=new", "--remote-debugging-port=0",
                      f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                      "--disable-background-networking", "--disable-background-timer-throttling",
                      "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows", "about:blank"]) as chrome:
            port_file = profile / "DevToolsActivePort"

            def devtools_address():
                try:
                    lines = port_file.read_text().splitlines()
                except (FileNotFoundError, PermissionError):
                    return None
                return lines[:2] if len(lines) >= 2 else None

            port, endpoint = wait_until(devtools_address, chrome)
            with websockets.connect(f"ws://127.0.0.1:{port}{endpoint}", max_size=5_000_000) as ws:
                count = 0
                session = None

                def call(method, params=None, *, target_session=None):
                    nonlocal count
                    count += 1
                    payload = {"id": count, "method": method, "params": params or {}}
                    selected_session = session if target_session is None else target_session
                    if selected_session:
                        payload["sessionId"] = selected_session
                    ws.send(json.dumps(payload))
                    while True:
                        message = json.loads(ws.recv(timeout=20))
                        if message.get("id") == count:
                            assert "error" not in message, message
                            return message.get("result", {})

                target = call("Target.createTarget", {"url": "about:blank"})["targetId"]
                session = call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
                call("Page.enable")

                def js(expression, *, target_session=None):
                    result = call("Runtime.evaluate", {
                        "expression": expression, "awaitPromise": True, "returnByValue": True,
                    }, target_session=target_session)
                    assert "exceptionDetails" not in result, result
                    return result.get("result", {}).get("value")

                yield call, js


BROWSER_HOOKS = """
window.calls = [];
window.downloadCount = 0;
const originalCreateObjectURL = URL.createObjectURL.bind(URL);
URL.createObjectURL = blob => { window.downloadCount++; return originalCreateObjectURL(blob); };
const realFetch = window.fetch.bind(window);
window.fetch = async (path, init) => {
  const url = String(path);
  const method = init?.method || 'GET';
  calls.push([url, method]);
  if (window.failRefresh && url === '/api/portfolios' && method === 'GET') {
    return Response.json({detail: 'Simulated directory error'}, {status: 503});
  }
  if (method === 'GET' && sessionStorage.getItem('holdDirectoryUntilStatusForTest')) {
    if (url.startsWith('/api/portfolios/mutations/')) {
      sessionStorage.removeItem('holdDirectoryUntilStatusForTest');
    } else if (url === '/api/portfolios') {
      const held = window.initialDirectoryRequests ??= [];
      return new Promise(resolve => held.push(async () => resolve(await realFetch(path, {...init, signal: undefined}))));
    }
  }
  if (window.delayMarkdown && url.includes('format=markdown')) {
    window.markdownRequested = true;
    window.markdownSignal = init.signal;
    return new Promise(resolve => { window.releaseMarkdown = async () => { const response = await realFetch(path, {...init, signal: undefined}); resolve(response); }; });
  }
  const response = await realFetch(path, init);
  if (window.loseAcknowledgement && (url === '/api/portfolios' && method === 'POST' || method === 'PATCH')) {
    throw new TypeError('Simulated lost acknowledgement');
  }
  return response;
};
"""


def test_browser_create_switch_rename_reload_and_reconcile(managed, monkeypatch, tmp_path):
    with serving(monkeypatch) as backend_port, browser(tmp_path, backend_port) as (cdp, js):
        cdp("Page.addScriptToEvaluateOnNewDocument", {"source": BROWSER_HOOKS})
        cdp("Page.navigate", {"url": f"http://{BROWSER_HOST}/reports#launch={CODE}"})

        def wait(expression):
            return wait_until(lambda: js(expression))

        def text(value):
            wait("document.body?.innerText.includes(" + json.dumps(value) + ")")

        def reload():
            previous = js("performance.timeOrigin")
            cdp("Page.reload")
            wait(f"performance.timeOrigin !== {previous} && document.readyState === 'complete'")

        def submit(value, *, rename=False):
            form = 'Rename portfolio' if rename else 'Create portfolio'
            js(f"""(() => {{
                const form = document.querySelector('form[aria-label="{form}"]');
                const input = form.querySelector('input');
                const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                set.call(input, {json.dumps(value)});
                input.dispatchEvent(new Event('input', {{bubbles:true}}));
                if (!{str(rename).lower()}) {{
                  const select = form.querySelector('select');
                  select.value = 'USD';
                  select.dispatchEvent(new Event('change', {{bubbles:true}}));
                }}
            }})()""")
            js(f"""(() => {{
                const form = document.querySelector('form[aria-label="{form}"]');
                form.querySelector('button').focus();
                form.requestSubmit();
            }})()""")

        text("Create your first portfolio")
        submit("First browser portfolio")
        text("Portfolio created.")
        wait("document.querySelector('.active-portfolio select')?.value === '1'")
        js("document.querySelector('.portfolio-management summary').click()")
        submit("Second browser portfolio")
        wait("document.querySelector('.active-portfolio select')?.value === '2'")

        js("""(async () => {
            for (const id of ['1','2','1']) {
                const select = document.querySelector('.active-portfolio select');
                select.value = id;
                select.dispatchEvent(new Event('change', {bubbles:true}));
                await new Promise(resolve => setTimeout(resolve, 15));
            }
        })()""")
        wait("document.querySelector('.active-portfolio select')?.value === '1'")
        text("First browser portfolio")
        def draft_date(day):
            js(f"""(() => {{
                const input = document.querySelector('form[aria-label="Report controls"] input[type=date]');
                Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, '{day}');
                input.dispatchEvent(new Event('input', {{bubbles:true}}));
            }})()""")
        draft_date("2026-01-05")
        js("document.querySelector('form[aria-label=\"Report controls\"]').requestSubmit()")
        wait("calls.some(([url]) => url.includes('/1/report?start_date=2026-01-05'))")
        draft_date("2026-01-06")

        wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='Generate fresh Markdown'&&!b.disabled)")
        js("window.delayMarkdown = true; [...document.querySelectorAll('button')].find(b=>b.textContent==='Generate fresh Markdown').click()")
        wait("window.markdownRequested")
        js("window.failRefresh = true; [...document.querySelectorAll('button')].find(b=>b.textContent==='Refresh portfolios').click()")
        text("The directory is stale")
        assert js("window.markdownSignal.aborted")
        assert js("[...document.querySelectorAll('.report-actions button')].every(b=>b.disabled)")
        js("(async () => { await window.releaseMarkdown(); await new Promise(resolve => setTimeout(resolve, 200)); })()")
        assert js("window.downloadCount") == 0
        assert js("document.querySelector('form[aria-label=\"Report controls\"] input[type=date]').value") == "2026-01-06"
        js("window.delayMarkdown = false; window.markdownRequested = false; window.failRefresh = false; [...document.querySelectorAll('button')].find(b=>b.textContent==='Refresh portfolios').click()")
        wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='Generate fresh Markdown'&&!b.disabled)")

        js("window.failRefresh = true")
        submit("Renamed in browser", rename=True)
        text("Portfolio name saved.")
        text("The directory is stale")
        assert js("document.querySelector('form[aria-label=\"Report controls\"] input[type=date]').value") == "2026-01-06"
        assert js("document.activeElement.textContent") == "Save name"
        assert js("!calls.some(([url]) => url.includes('/1/report?start_date=2026-01-06'))")
        js("window.failRefresh = false; [...document.querySelectorAll('button')].find(b=>b.textContent==='Refresh portfolios').click()")
        wait("document.querySelector('form[aria-label=\"Create portfolio\"] button').getAttribute('aria-disabled') === 'false'")

        wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='Generate fresh Markdown'&&!b.disabled)")
        js("window.delayMarkdown = true; [...document.querySelectorAll('button')].find(b=>b.textContent==='Generate fresh Markdown').click()")
        wait("window.markdownRequested")
        js("""(async () => {
            for (const id of ['2','1']) {
                const select = document.querySelector('.active-portfolio select');
                select.value = id;
                select.dispatchEvent(new Event('change', {bubbles:true}));
                await new Promise(resolve => setTimeout(resolve, 25));
            }
            await window.releaseMarkdown();
            await new Promise(resolve => setTimeout(resolve, 200));
        })()""")
        assert js("window.downloadCount") == 0
        assert js("document.querySelector('form[aria-label=\"Report controls\"] input[type=date]').value") == ""
        js("window.delayMarkdown = false")
        js("window.loseAcknowledgement = true")
        submit("Lost acknowledgement")
        text("The previous change may have saved")
        js("sessionStorage.setItem('holdDirectoryUntilStatusForTest', '1')")
        reload()
        text("A previous change needs review.")
        assert js("!!sessionStorage.getItem('riskengine.pendingPortfolioMutation')")
        wait("window.initialDirectoryRequests?.length > 0")
        assert js("!document.querySelector('.active-portfolio select')")
        wait("(() => { const button = document.querySelector('.portfolio-reconciliation button'); if (!button || button.disabled) return false; button.click(); return true; })()")
        text("Portfolio created.")
        wait("document.querySelector('.active-portfolio select')?.value === '3'")
        js("Promise.all(window.initialDirectoryRequests.map(release => release()))")
        assert js("sessionStorage.getItem('riskengine.pendingPortfolioMutation')") is None
        assert js("calls.filter(([url, method])=>url==='/api/portfolios' && method==='POST').length") == 0
        assert js("document.querySelectorAll('.active-portfolio option').length") == 4
        wait("[...document.querySelectorAll('button')].some(b=>b.textContent==='Generate fresh Markdown'&&!b.disabled)")

        replacement = create_database(tmp_path / "browser-replacement")
        with closing(get_connection(replacement)) as db, db:
            db.execute("INSERT INTO portfolios VALUES (3, 'Other database', 'EUR')")
        replacement.replace(managed)
        js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Refresh portfolios').click()")
        wait("document.querySelector('.active-portfolio select')?.value === ''")
        text("Select a portfolio to continue")

        cdp("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})
        for theme in ("light", "dark"):
            js(f"document.querySelector('.theme-toggle input[value={theme}]').click()")
            wait("document.documentElement.scrollWidth <= innerWidth")


        js("""(() => {
            document.querySelector('.portfolio-management summary').click();
            Storage.prototype.setItem = () => { throw new Error('Storage blocked by test'); };
        })()""")
        submit("Storage blocked")
        text("Tab storage is unavailable")
        assert js("calls.filter(([url, method])=>url==='/api/portfolios' && method==='POST').length") == 0

        reload()
        wait("document.querySelector('.active-portfolio select')?.value === '3'")
        js("document.querySelector('.portfolio-management summary').click(); window.loseAcknowledgement = true")
        submit("Session loss")
        text("The previous change may have saved")
        main.app.state.local_access.session_until = 0
        js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Check change status').click()")
        text("Local session unavailable")
        assert js("!!sessionStorage.getItem('riskengine.pendingPortfolioMutation')")


def test_browser_empty_read_only_database(managed, monkeypatch, tmp_path):
    monkeypatch.delenv("RISKENGINE_WRITE_DATA_ROOT")
    with serving(monkeypatch) as backend_port, browser(tmp_path, backend_port) as (cdp, js):
        cdp("Page.navigate", {"url": f"http://{BROWSER_HOST}/overview#launch={CODE}"})
        wait_until(lambda: js("document.body?.innerText.includes('Read-only database.')"))
        assert js("document.querySelector('form[aria-label=\"Create portfolio\"]') === null")


def rename_in_browser(js, name):
    js(f"""(() => {{
        const details = document.querySelector('.portfolio-management');
        details.open = true;
        const input = details.querySelector('form[aria-label="Rename portfolio"] input');
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, {json.dumps(name)});
        input.dispatchEvent(new Event('input', {{bubbles:true}}));
    }})()""")
    js("document.querySelector('form[aria-label=\"Rename portfolio\"]').requestSubmit()")


def test_browser_recovered_rename_receipt_does_not_restore_old_name(managed, monkeypatch, tmp_path):
    with closing(get_connection(managed)) as db, db:
        db.execute("INSERT INTO portfolios VALUES (1, 'Initial name', 'USD')")
    with serving(monkeypatch) as backend_port, browser(tmp_path, backend_port) as (cdp, js):
        cdp("Page.addScriptToEvaluateOnNewDocument", {"source": BROWSER_HOOKS})
        url = f"http://{BROWSER_HOST}/overview"
        cdp("Page.navigate", {"url": f"{url}#launch={CODE}"})
        wait_until(lambda: js("document.querySelector('.active-portfolio select')?.value === '1'"))
        js("window.loseAcknowledgement = true")
        rename_in_browser(js, "Older receipt name")
        wait_until(lambda: js("document.body?.innerText.includes('The previous change may have saved')"))

        target = cdp("Target.createTarget", {"url": "about:blank"}, target_session="")["targetId"]
        tab = cdp("Target.attachToTarget", {"targetId": target, "flatten": True}, target_session="")["sessionId"]
        cdp("Page.navigate", {"url": url}, target_session=tab)

        def second(expression):
            return js(expression, target_session=tab)

        wait_until(lambda: second("document.querySelector('.active-portfolio select')?.value === '1'"))
        rename_in_browser(second, "Current portfolio name")
        wait_until(lambda: second("document.querySelector('.active-portfolio select')?.textContent.includes('Current portfolio name')"))

        cdp("Target.closeTarget", {"targetId": target}, target_session="")

        js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Refresh portfolios').click()")
        wait_until(lambda: js("document.querySelector('.active-portfolio select')?.textContent.includes('Current portfolio name')"))
        js("window.failRefresh = true; [...document.querySelectorAll('button')].find(b=>b.textContent==='Check change status').click()")
        wait_until(lambda: js("document.body.innerText.includes('Portfolio name saved.') && document.body.innerText.includes('The directory is stale')"))
        assert js("document.querySelector('.active-portfolio select').textContent.includes('Current portfolio name')")
        assert js("!sessionStorage.getItem('riskengine.pendingPortfolioMutation')")
        js("window.failRefresh = false; [...document.querySelectorAll('button')].find(b=>b.textContent==='Refresh portfolios').click()")
        wait_until(lambda: js("!document.body.innerText.includes('The directory is stale') && document.querySelector('.active-portfolio select')?.textContent.includes('Current portfolio name')"))


def test_browser_rename_preserves_performance_risk_and_allocation_controls(managed, monkeypatch, tmp_path):
    with closing(get_connection(managed)) as db, db:
        db.execute("INSERT INTO portfolios VALUES (1, 'Original name', 'USD')")
    with serving(monkeypatch) as backend_port, browser(tmp_path, backend_port) as (cdp, js):
        cdp("Page.addScriptToEvaluateOnNewDocument", {"source": BROWSER_HOOKS})
        cdp("Page.navigate", {"url": f"http://{BROWSER_HOST}/performance#launch={CODE}"})
        for index, (route, label, endpoint, query_key) in enumerate([
            ("performance", "Performance date range", "performance", "start_date"),
            ("risk-analytics", "Risk analysis controls", "risk", "start_date"),
            ("portfolio-allocation", "Allocation valuation date", "allocation", "as_of_date"),
        ]):
            if index:
                js(f"document.querySelector('.page-nav a[href=\"/{route}\"]').click()")
            form = f'form[aria-label="{label}"]'
            input_selector = json.dumps(form + ' input[type=date]')
            wait_until(lambda: js(f"!!document.querySelector({input_selector})"))

            def draft(day):
                js(f"""(() => {{
                    const input = document.querySelector({input_selector});
                    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, '{day}');
                    input.dispatchEvent(new Event('input', {{bubbles:true}}));
                }})()""")

            applied_path = json.dumps(f"/api/portfolios/1/{endpoint}?{query_key}=2026-01-05")
            draft_path = json.dumps(f"/api/portfolios/1/{endpoint}?{query_key}=2026-01-06")
            draft("2026-01-05")
            js(f"document.querySelector({json.dumps(form)}).requestSubmit()")
            wait_until(lambda: js(f"calls.some(([url]) => url.startsWith({applied_path}))"))
            draft("2026-01-06")
            count = js(f"calls.filter(([url]) => url.startsWith({applied_path})).length")
            rename_in_browser(js, f"Renamed on {route}")
            wait_until(lambda: js(f"calls.filter(([url]) => url.startsWith({applied_path})).length > {count}"))
            assert js(f"document.querySelector({input_selector}).value") == "2026-01-06"
            assert js(f"!calls.some(([url]) => url.startsWith({draft_path}))")
