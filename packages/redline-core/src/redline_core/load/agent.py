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


_ALLOWED_CFG_KEYS = frozenset(
    {
        "worker", "workers", "run_id", "parent_pid", "base_url", "headers", "timeout_s",
        "connections", "max_inflight", "poisson", "seed", "requests", "flows", "steps",
        "identities", "identities_file", "identity_mode", "data", "data_rows", "auth", "base_dir",
    }
)  # fmt: skip
MAX_REQUESTS_PER_CFG = 2_000
MAX_CONNECTIONS = 10_000
MAX_INFLIGHT = 100_000
MAX_TOKEN_MIN = 16
#: IPv6 ranges of cloud metadata services that are not "link-local" (AWS Nitro IPv6).
_METADATA_V6 = ipaddress.ip_network("fd00:ec2::/32")


def _addresses(host: str) -> list[ipaddress._BaseAddress]:
    """Every IP ``host`` stands for: a literal in any spelling (decimal, hex, trailing dot,
    IPv4-mapped IPv6) or what DNS resolves it to. Unresolvable names give ``[]``."""
    import socket

    host = host.strip("[]").rstrip(".")
    found: list[ipaddress._BaseAddress] = []
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return found
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        found.append(ip)
    return found


def _blocked_target(base_url: str) -> str | None:
    """Why a target is refused (cloud metadata and link-local addresses), or ``None``.

    Names are resolved and every address is checked, so decimal/hex IPs, trailing dots and
    DNS names that point at a metadata service are caught. A name that changes its answer
    between this check and the connection (DNS rebinding) is not: use ``--allow-target``."""
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower().rstrip(".")
    if host in _BLOCKED_NAMES or (host.endswith(".internal") and host.startswith("metadata")):
        return f"{host} is a cloud metadata name"
    for ip in _addresses(host):
        if ip.is_link_local or (isinstance(ip, ipaddress.IPv6Address) and ip in _METADATA_V6):
            return f"{host} resolves to {ip}, a link-local or metadata address"
    return None


def _target_allowed(base_url: str, allowed: tuple[str, ...]) -> bool:
    """Exact scheme, host and port match against an allow-list entry, on a path boundary.

    ``http://app.internal`` allows ``http://app.internal:80/x`` but neither
    ``http://app.internal.evil.com`` nor ``http://app.internal@evil.com``."""

    def identity(parts: Any) -> tuple[str, str, int | None]:
        default = {"http": 80, "https": 443}.get(parts.scheme)
        return parts.scheme, (parts.hostname or "").lower().rstrip("."), parts.port or default

    target = urlsplit(base_url)
    if target.username is not None or target.password is not None:
        return False
    for entry in allowed:
        want = urlsplit(entry)
        if identity(target) != identity(want):
            continue
        prefix = want.path.rstrip("/")
        if not prefix or target.path == prefix or target.path.startswith(prefix + "/"):
            return True
    return False


def _check_target(url: str, allow_targets: tuple[str, ...], what: str) -> str | None:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return f"{what} must be an http(s) URL"
    if urlsplit(url).username is not None:
        return f"{what} must not carry credentials"
    if (why := _blocked_target(url)) is not None:
        return f"target refused: {why}"
    if allow_targets and not _target_allowed(url, allow_targets):
        return f"{what} is not in this agent's allowed targets"
    return None


def validate_worker_cfg(cfg: Any, allow_targets: tuple[str, ...]) -> str | None:
    """Why a worker config from a controller is refused, or ``None``.

    Only a fixed set of keys is accepted, numbers are bounded, every URL the worker may call
    (the target and the token endpoint) is checked, and nothing may name a file or an
    environment variable: identities and data rows must arrive inline."""
    from .scenario import METHODS

    if not isinstance(cfg, dict):
        return "a worker config must be an object"
    extra = sorted(set(cfg) - _ALLOWED_CFG_KEYS)
    if extra:
        return f"unknown key {extra[0]}"
    for key in ("worker", "workers", "connections", "max_inflight"):
        if isinstance(cfg.get(key), bool) or not isinstance(cfg.get(key), int) or cfg[key] < 0:
            return f"{key} must be a non-negative integer"
    if cfg["workers"] < 1 or cfg["workers"] > 100_000 or cfg["worker"] >= cfg["workers"]:
        return "worker/workers are out of range"
    if cfg["connections"] > MAX_CONNECTIONS or cfg["max_inflight"] > MAX_INFLIGHT:
        return "connections or max_inflight is too large"
    if (problem := _check_target(cfg.get("base_url"), allow_targets, "base_url")) is not None:
        return problem
    auth = cfg.get("auth")
    if auth:
        if not isinstance(auth, dict) or auth.get("provider") != "request":
            return "an agent only supports auth.provider = 'request' (no command or callable)"
        request = auth.get("request") or {}
        token_base = request.get("base_url") or cfg["base_url"]
        if problem := _check_target(token_base, allow_targets, "auth.request.base_url"):
            return problem
    if cfg.get("identities_file"):
        return "identities must be sent inline, not as a file"
    for ident in cfg.get("identities") or ():
        if not isinstance(ident, dict) or "${" in json.dumps(ident):
            return "identities must be sent already resolved (no ${VAR})"
    sources = cfg.get("data") or ()
    rows = cfg.get("data_rows")
    if sources and (not isinstance(rows, dict) or any(d.get("name") not in rows for d in sources)):
        return "data must be sent inline as data_rows; an agent reads no files"
    requests = cfg.get("requests", [])
    flows = cfg.get("flows", [])
    if (
        not isinstance(requests, list)
        or not isinstance(flows, list)
        or len(requests) > MAX_REQUESTS_PER_CFG
        or len(flows) > MAX_REQUESTS_PER_CFG
    ):
        return "too many requests or flows"
    for r in requests:
        if not isinstance(r, dict) or str(r.get("method", "GET")).upper() not in METHODS:
            return "a request has an invalid method"
        path = r.get("path")
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            return "request paths must start with a single '/'"
    for f in flows:
        if not isinstance(f, dict) or not isinstance(f.get("steps"), list):
            return "a flow needs steps"
        if len(f["steps"]) > MAX_REQUESTS_PER_CFG:
            return "too many flow steps"
        for r in f["steps"]:
            path = r.get("path") if isinstance(r, dict) else None
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
                return "flow step paths must start with a single '/'"
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
        if not token or len(token) < MAX_TOKEN_MIN:
            raise AgentError(f"an agent needs a token of at least {MAX_TOKEN_MIN} characters")
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
    #: a connection that sends nothing for this long is dropped (slow-connection DoS)
    timeout = 30

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
        if length < 0 or length > MAX_BODY:
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
        except Exception:
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
        except Exception:  # a hostile body must never take the handler down
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
