"""Minimal operator console.

Scope, stated plainly: this is NOT a co-browsing product. It is the smallest
surface that makes the control transfer real — it shows the operator the run's
context and a live view of the session, and it owns the take/release/abort
signals. The operator's actual clicking happens in the real browser window the
automation is driving (run with ``--headed``), which is why the takeover is on
the same session rather than a copy of it.

Threading note: Playwright's sync API is single-threaded. The HTTP handlers
therefore never touch the surface. They only enqueue a command and read a
screenshot the run loop published. All browser work stays on the run thread.
"""

from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .control import SessionControl

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>CUA Operator Console</title>
<style>
 body{font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#11151c;color:#e6eaf2}
 header{background:#1b2330;padding:10px 16px;border-bottom:1px solid #2b3648}
 h1{font-size:15px;margin:0}
 main{display:flex;gap:16px;padding:16px;align-items:flex-start;flex-wrap:wrap}
 .panel{background:#1b2330;border:1px solid #2b3648;border-radius:6px;padding:14px;min-width:330px;max-width:460px}
 .k{color:#8fa3bf;display:inline-block;min-width:92px}
 img{border:1px solid #2b3648;border-radius:6px;max-width:760px;width:100%}
 button{font:600 13px system-ui;padding:8px 14px;border-radius:5px;border:1px solid #3a4a63;
   background:#27354a;color:#e6eaf2;cursor:pointer;margin-right:8px}
 button.primary{background:#2f6f4f;border-color:#3d8a63}
 button.danger{background:#6f2f2f;border-color:#8a3d3d}
 .badge{display:inline-block;padding:2px 8px;border-radius:10px;background:#27354a;font-weight:600}
 .human{background:#7a5c17}.auto{background:#1f4a6f}
 pre{white-space:pre-wrap;color:#b9c6da;background:#151b25;padding:10px;border-radius:5px;
   max-height:190px;overflow:auto;font-size:12px}
 li{margin-bottom:5px}
 .steps{background:#151b25;border:1px solid #2b3648;border-radius:5px;padding:8px 12px;margin:12px 0}
 .steps ol{margin:6px 0 2px;padding-left:20px}
 .steps li.now{color:#ffd479;font-weight:600}
 .steps li.done{color:#6f7f96;text-decoration:line-through}
</style></head><body>
<header><h1>Computer-Use Automation &mdash; Operator Console</h1></header>
<main>
  <div class="panel" id="ctx">loading&hellip;</div>
  <div><img id="shot" src="/screen.png" alt="live session"></div>
</main>
<script>
async function tick(){
  const r = await fetch('/state.json'); const s = await r.json();
  const o = s.control.owner;
  document.getElementById('ctx').innerHTML = `
    <div><span class="k">Control</span> <span class="badge ${o==='human'?'human':'auto'}">${o}</span>
         <span class="badge">${s.control.state}</span></div>
    <div><span class="k">Run</span> ${s.control.run_id}</div>
    <div><span class="k">Capability</span> ${s.request.capability_id||'-'}</div>
    <div><span class="k">Goal</span> ${s.request.goal||'-'}</div>
    <div><span class="k">Step</span> ${s.request.step_index ?? '-'} ${s.request.step_id||''} — ${s.request.step_intent||''}</div>
    <div><span class="k">Reason</span> ${s.request.reason||'-'}</div>
    <div><span class="k">Act in</span> the browser window automation opened (not this page)</div>
    <div><span class="k">At</span> ${s.request.observed_url||'-'}</div>
    <div style="margin:10px 0 6px"><b>Suggested manual steps</b></div>
    <ul>${(s.request.suggested_actions||[]).map(a=>`<li>${a}</li>`).join('')}</ul>
    <div class="steps">
      <b>What to do</b>
      <ol>
        <li class="${o==='human'?'done':'now'}">Click <b>Take control</b> below.</li>
        <li class="${o==='human'?'now':''}">Do the steps above <b>in the browser window the
            automation already has open</b> &mdash; not a new tab.</li>
        <li>Click <b>Release &amp; resume</b>. Automation carries on from where it paused.</li>
      </ol>
    </div>
    <div style="margin:12px 0">
      <button class="primary" onclick="cmd('take')" ${o==='human'?'disabled':''}
        title="${o==='human'?'You already have control':'Take control of the live session'}">
        Take control</button>
      <button class="primary" onclick="cmd('release')"
        title="${o==='human'?'Hand the session back to automation':'Do this after you have finished in the browser window'}">
        Release &amp; resume</button>
      <button class="danger" onclick="cmd('abort')">Abort run</button>
    </div>
    <div><b>Recorded human actions (${s.control.human_actions.length})</b></div>
    <pre>${s.control.human_actions.map(a=>`${a.at}  ${a.description}`).join('\\n')||'(none yet)'}</pre>`;
  document.getElementById('shot').src = '/screen.png?t=' + Date.now();
}
async function cmd(c){ await fetch('/'+c, {method:'POST'}); tick(); }
tick(); setInterval(tick, 1200);
</script></body></html>"""


class OperatorConsole:
    def __init__(self, control: SessionControl, *, host: str = "127.0.0.1", port: int = 8811) -> None:
        self.control = control
        self.host = host
        self.port = port
        self.commands: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._png: bytes = b""
        self._request: dict[str, Any] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- run-thread API ---------------------------------------------------

    def publish(self, png: bytes | None, request: dict[str, Any] | None = None) -> None:
        with self._lock:
            if png:
                self._png = png
            if request is not None:
                self._request = request

    def poll_command(self) -> str | None:
        try:
            return self.commands.get_nowait()
        except queue.Empty:
            return None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    # -- lifecycle --------------------------------------------------------

    def start(self) -> str:
        console = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence default logging
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                if path == "/":
                    self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
                elif path == "/screen.png":
                    with console._lock:
                        png = console._png
                    self._send(200, png, "image/png")
                elif path == "/state.json":
                    with console._lock:
                        payload = {
                            "control": console.control.as_dict(),
                            "request": console._request,
                        }
                    self._send(200, json.dumps(payload, default=str).encode(), "application/json")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self) -> None:
                cmd = self.path.strip("/").split("?")[0]
                if cmd in {"take", "release", "abort"}:
                    console.commands.put(cmd)
                    self._send(200, b'{"ok":true}', "application/json")
                else:
                    self._send(404, b'{"ok":false}', "application/json")

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
