"""Remote load generators: ``redline load agent`` on other hosts, ``--agents`` on the controller.

Running the generator on the machine under test distorts both sides, and one host caps the
offered rate. An **agent** is a small HTTP service that runs worker processes for a controller:

* the controller computes the step plan once and sends each agent its workers' config
  (``POST /v1/prepare``); workers' phase offsets use the *global* worker numbering, so the
  union of all hosts' arrivals is exactly the planned rate;
* one shared start time (``POST /v1/start``) is converted to each agent's clock from a measured
  offset, so step boundaries line up to the second;
* workers' per-second deltas (counters and latency histograms) stream back
  (``GET /v1/events``) and the controller merges them exactly as it merges local workers.

Security: an agent needs a bearer token (it refuses to start without one), listens on loopback
unless told otherwise, can be restricted to target prefixes, never accepts a token command or a
Python callable (``auth.provider`` ``command`` / ``python``), and the controller sends identities
and data rows inline so the agent reads no files. Use a private network or TLS between them: the
config carries credentials.
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import multiprocessing as mp
import os
import queue as queue_mod
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

__all__ = ["AgentError", "AgentServer", "RemoteAgent", "decode_event", "serve"]

MAX_BODY = 64 * 1024 * 1024
MAX_AGENT_WORKERS = 64
READY_TIMEOUT_S = 60.0
LONG_POLL_S = 5.0
_BLOCKED_NAMES = {"metadata.google.internal", "metadata"}


class AgentError(RuntimeError):
    """An agent refused a request or could not be reached."""


# ── wire format: histogram bytes travel as {"__b": base64} ───────────────────────────────
def _encode_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return {"__b": base64.b64encode(bytes(value)).decode("ascii")}
    raise TypeError(f"cannot serialise {type(value).__name__}")


def encode_event(event: Any) -> str:
    return json.dumps(event, default=_encode_default)


def _decode_hook(obj: dict[str, Any]) -> Any:
    if len(obj) == 1 and "__b" in obj:
        return base64.b64decode(obj["__b"])
    return obj


def decode_event(event: dict[str, Any]) -> dict[str, Any]:
    """An event as the engine expects it: ``steps`` keyed by integer step number."""
    if isinstance(event.get("steps"), dict):
        event["steps"] = {int(k): v for k, v in event["steps"].items()}
    return event


def _blocked_target(base_url: str) -> str | None:
    host = (urlsplit(base_url).hostname or "").lower()
    if host in _BLOCKED_NAMES:
        return f"{host} is a cloud metadata name"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_link_local:
        return f"{host} is a link-local address (cloud metadata)"
    return None


def validate_worker_cfg(cfg: Any, allow_targets: tuple[str, ...]) -> str | None:
    """Why a worker config from a controller is refused, or ``None``."""
    from .scenario import METHODS

    if not isinstance(cfg, dict):
        return "a worker config must be an object"
    for key in ("worker", "workers", "connections", "max_inflight"):
        if isinstance(cfg.get(key), bool) or not isinstance(cfg.get(key), int) or cfg[key] < 0:
            return f"{key} must be a non-negative integer"
    if cfg["workers"] < 1 or cfg["workers"] > 100_000 or cfg["worker"] >= cfg["workers"]:
        return "worker/workers are out of range"
    base = cfg.get("base_url")
    if not isinstance(base, str) or not base.startswith(("http://", "https://")):
        return "base_url must be an http(s) URL"
    if (why := _blocked_target(base)) is not None:
        return f"target refused: {why}"
    if allow_targets and not any(base.startswith(prefix) for prefix in allow_targets):
        return "target is not in this agent's allowed targets"
    auth = cfg.get("auth")
    if auth and auth.get("provider") != "request":
        return "an agent only supports auth.provider = 'request' (no command or callable)"
    if cfg.get("identities_file"):
        return "identities must be sent inline, not as a file"
    for r in cfg.get("requests") or ():
        if not isinstance(r, dict) or str(r.get("method", "GET")).upper() not in METHODS:
            return "a request has an invalid method"
        path = r.get("path")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            return "request paths must start with a single '/'"
    for f in cfg.get("flows") or ():
        for r in f.get("steps") or ():
            path = r.get("path")
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
                return "flow step paths must start with a single '/'"
    if "_crash_after_s" in cfg:
        return "unknown key _crash_after_s"
    return None


class _Run:
    def __init__(self, run_id: str, n: int) -> None:
        self.id = run_id
        self.n = n
        self.cond = threading.Condition()
        self.events: list[dict[str, Any]] = []
        self.dropped = 0  # events already handed out (the list is trimmed)
        self.ready: set[int] = set()
        self.exited: set[int] = set()
        self.error: str | None = None
        self.finished = False
        self.procs: list[Any] = []
        self.q: Any = None
        self.go: Any = None
        self.stop: Any = None
        self.start_at: Any = None
        self.started = False


class AgentServer:
    """The agent's HTTP service (stdlib only)."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        max_workers: int | None = None,
        allow_targets: tuple[str, ...] = (),
        certfile: str | None = None,
        keyfile: str | None = None,
    ) -> None:
        if not token or len(token) < 8:
            raise AgentError("an agent needs a token of at least 8 characters")
        self.token = token
        self.max_workers = max_workers or min(os.cpu_count() or 1, 8)
        self.allow_targets = tuple(allow_targets)
        self.run: _Run | None = None
        self._lock = threading.Lock()
        handler = type("Handler", (_Handler,), {"agent": self})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        if certfile:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile, keyfile)
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.httpd.server_address[:2]
        return str(host), int(port)

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.2)

    def close(self) -> None:
        self.stop_run()
        self.httpd.shutdown()
        self.httpd.server_close()

    # ── run lifecycle ───────────────────────────────────────────────────────────────────
    def prepare(self, run_id: str, cfgs: list[dict[str, Any]]) -> dict[str, Any]:
        from .worker import worker_main

        if not cfgs or len(cfgs) > min(self.max_workers, MAX_AGENT_WORKERS):
            raise AgentError(f"this agent runs 1-{self.max_workers} workers")
        for cfg in cfgs:
            problem = validate_worker_cfg(cfg, self.allow_targets)
            if problem:
                raise AgentError(problem)
        with self._lock:
            if self.run is not None and not self.run.finished:
                raise _Busy("a run is already in progress on this agent")
            ctx = mp.get_context("spawn")
            run = _Run(run_id, len(cfgs))
            run.q, run.go, run.stop = ctx.Queue(), ctx.Event(), ctx.Event()
            run.start_at = ctx.Value("d", 0.0)
            run.procs = [
                ctx.Process(
                    target=worker_main,
                    args=({**cfg, "parent_pid": None}, run.q, run.go, run.start_at, run.stop),
                    name=f"redline-agent-{cfg['worker']}",
                    daemon=True,
                )
                for cfg in cfgs
            ]
            self.run = run
            for p in run.procs:
                p.start()
            threading.Thread(target=self._pump, args=(run,), daemon=True).start()
        deadline = time.monotonic() + READY_TIMEOUT_S
        with run.cond:
            while len(run.ready) < run.n and run.error is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    run.error = "workers did not start in time"
                    break
                run.cond.wait(min(left, 0.5))
        if run.error:
            self.stop_run()
            raise AgentError(run.error)
        return {"ready": len(run.ready), "time": time.time()}

    def _pump(self, run: _Run) -> None:
        """Move worker messages into the run's event list."""
        while True:
            try:
                msg = run.q.get(timeout=0.2)
            except queue_mod.Empty:
                msg = None
            with run.cond:
                if msg is not None:
                    kind = msg.get("type")
                    if kind == "ready":
                        run.ready.add(msg["worker"])
                    else:
                        if kind == "exit":
                            run.exited.add(msg["worker"])
                        elif kind == "error":
                            run.error = run.error or str(msg.get("message"))
                        run.events.append(msg)
                dead = [p for p in run.procs if not p.is_alive() and p.exitcode not in (0, None)]
                if dead and not run.error and len(run.exited) < run.n:
                    run.error = f"a worker exited unexpectedly (exit code {dead[0].exitcode})"
                    run.events.append(
                        {"type": "error", "worker": -1, "message": run.error, "traceback": ""}
                    )
                if len(run.exited) >= run.n or (run.error and not run.started):
                    run.finished = True
                run.cond.notify_all()
                if run.finished:
                    break
        for p in run.procs:
            p.join(timeout=2.0)

    def start(self, start_epoch: float) -> None:
        run = self._current()
        run.start_at.value = float(start_epoch)
        run.started = True
        run.go.set()

    def events(self, after: int, wait: float) -> dict[str, Any]:
        run = self._current()
        deadline = time.monotonic() + max(0.0, min(wait, LONG_POLL_S))
        with run.cond:
            while True:
                have = run.dropped + len(run.events)
                if have > after or run.finished:
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                run.cond.wait(left)
            # events before ``after`` were delivered: drop them
            drop = max(0, after - run.dropped)
            if drop:
                del run.events[:drop]
                run.dropped += drop
            batch = list(run.events)
            return {
                "events": batch,
                "next": run.dropped + len(batch),
                "finished": run.finished and not batch,
            }

    def stop_run(self) -> None:
        run = self.run
        if run is None:
            return
        for event in (run.stop, run.go):  # release workers still waiting to start
            if event is not None:
                event.set()
        for p in run.procs:
            p.join(timeout=3.0)
            if p.is_alive():
                p.kill()
        with run.cond:
            run.finished = True
            run.cond.notify_all()

    def _current(self) -> _Run:
        run = self.run
        if run is None:
            raise AgentError("no run is prepared")
        return run


class _Busy(AgentError):
    pass


class _Handler(BaseHTTPRequestHandler):
    agent: AgentServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        pass

    def _reply(self, status: int, payload: Any) -> None:
        body = encode_event(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        header = self.headers.get("Authorization", "")
        scheme, _, given = header.partition(" ")
        ok = scheme.lower() == "bearer" and hmac.compare_digest(
            given.strip().encode(), self.agent.token.encode()
        )
        if not ok:
            self._reply(401, {"error": "unauthorised"})
        return ok

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise AgentError("request body too large")
        return json.loads(self.rfile.read(length) or b"{}", object_hook=_decode_hook)

    def do_GET(self) -> None:
        if not self._authorised():
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/v1/info":
                self._reply(
                    200,
                    {
                        "version": "1",
                        "cpus": self.agent.max_workers,
                        "time": time.time(),
                        "busy": bool(self.agent.run and not self.agent.run.finished),
                    },
                )
            elif url.path == "/v1/events":
                after = int(query.get("after", ["0"])[0])
                wait = float(query.get("wait", ["1"])[0])
                self._reply(200, self.agent.events(after, wait))
            else:
                self._reply(404, {"error": "not found"})
        except AgentError as exc:
            self._reply(409, {"error": str(exc)})
        except (ValueError, TypeError):
            self._reply(400, {"error": "bad request"})

    def do_POST(self) -> None:
        if not self._authorised():
            return
        try:
            body = self._body()
            if self.path == "/v1/prepare":
                if not isinstance(body, dict) or not isinstance(body.get("workers"), list):
                    raise AgentError("expected {run_id, workers: [...]}")
                self._reply(200, self.agent.prepare(str(body.get("run_id", "")), body["workers"]))
            elif self.path == "/v1/start":
                self.agent.start(float(body["start_epoch"]))
                self._reply(200, {"started": True})
            elif self.path == "/v1/stop":
                self.agent.stop_run()
                self._reply(200, {"stopped": True})
            else:
                self._reply(404, {"error": "not found"})
        except _Busy as exc:
            self._reply(409, {"error": str(exc)})
        except AgentError as exc:
            self._reply(400, {"error": str(exc)})
        except (ValueError, KeyError, TypeError):
            self._reply(400, {"error": "bad request"})


def serve(
    listen: str,
    token: str,
    *,
    max_workers: int | None = None,
    allow_targets: tuple[str, ...] = (),
    certfile: str | None = None,
    keyfile: str | None = None,
) -> AgentServer:
    host, _, port = listen.rpartition(":")
    return AgentServer(
        host or "127.0.0.1",
        int(port),
        token,
        max_workers=max_workers,
        allow_targets=allow_targets,
        certfile=certfile,
        keyfile=keyfile,
    )


# ── controller side ─────────────────────────────────────────────────────────────────────
class RemoteAgent:
    """The controller's handle on one agent."""

    def __init__(self, url: str, token: str, *, verify: bool | str = True) -> None:
        import httpx

        self.url = url.rstrip("/")
        self._client = httpx.Client(timeout=15.0, verify=verify)
        self._token = token
        self.cpus = 1
        self.clock_offset = 0.0  # agent clock minus controller clock, seconds

    def _call(self, method: str, path: str, body: Any = None, *, timeout: float = 15.0) -> Any:
        import httpx

        try:
            response = self._client.request(
                method,
                f"{self.url}{path}",
                content=encode_event(body) if body is not None else None,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise AgentError(f"agent {self.url} unreachable: {type(exc).__name__}") from None
        if response.status_code == 401:
            raise AgentError(f"agent {self.url} refused the token")
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", "")
            except Exception:
                detail = ""
            raise AgentError(
                f"agent {self.url} answered HTTP {response.status_code} {detail}".strip()
            )
        return json.loads(response.content, object_hook=_decode_hook)

    def info(self) -> dict[str, Any]:
        t0 = time.time()
        data = self._call("GET", "/v1/info")
        t1 = time.time()
        self.cpus = int(data.get("cpus") or 1)
        self.clock_offset = float(data["time"]) - (t0 + t1) / 2.0
        return data  # type: ignore[no-any-return]

    def prepare(self, run_id: str, cfgs: list[dict[str, Any]]) -> None:
        self._call(
            "POST", "/v1/prepare", {"run_id": run_id, "workers": cfgs}, timeout=READY_TIMEOUT_S + 15
        )

    def start(self, controller_epoch: float) -> None:
        self._call("POST", "/v1/start", {"start_epoch": controller_epoch + self.clock_offset})

    def events(self, after: int, wait: float = 2.0) -> dict[str, Any]:
        return self._call("GET", f"/v1/events?after={after}&wait={wait}", timeout=wait + 15)  # type: ignore[no-any-return]

    def stop(self) -> None:
        try:
            self._call("POST", "/v1/stop", {}, timeout=5.0)
        except AgentError:
            pass

    def close(self) -> None:
        self._client.close()
