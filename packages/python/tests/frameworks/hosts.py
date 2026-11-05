"""The hosts and frameworks the framework checks serve each line through, as REFERENCE.md writes
them: FastAPI mounting, Mangum on AWS Lambda and Vercel's Python runtime. Also a fake ingestion
that can hold its acknowledgement (tests/frameworks/shared/ingestion.ts at the repo root), so a
test can tell sending a reply waited for, or handed to `wait_until`, from sending that merely
finished, and the raw JSON-RPC each test sends.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import importlib
import json
import queue
import re
import socket
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from lines import FAST, LINE, high, tool

from lsquare import LSquare
from lsquare._sending import Abort, NetworkError, Reply
from lsquare.mcp import instrument

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

#: The host the in-process clients name: one mcp's DNS-rebinding protection accepts by default.
BASE = "http://127.0.0.1:8000"
#: API Gateway's Host header for an HTTP API's default endpoint.
API_HOST = "abc123.execute-api.eu-west-1.amazonaws.com"
#: Sent by hand: a version every line supports on its legacy (handshake) path.
VERSION = "2025-03-26"
ID_LINE = re.compile(r"^Task ID: (ses_[A-Za-z0-9_-]{22})\. Send it as session_id")


# -- The ingestion ----------------------------------------------------------------------------------


@dataclass
class Batch:
    events: list[dict[str, Any]]
    answered: bool = False


@dataclass
class Ingestion:
    """A fake ingestion, given to the client as its transport: it keeps each batch and acknowledges
    it at once, or holds the answer until a test releases it. Each acknowledgement, and anything a
    test marks, goes on one timeline, in order."""

    batches: list[Batch] = field(default_factory=list)
    timeline: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _open: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self._open.set()

    def __call__(self, body: bytes, timeout: float, abort: Abort) -> Reply:
        events: list[dict[str, Any]] = json.loads(body.decode("utf-8"))["events"]
        batch = Batch(events)
        with self.lock:
            self.batches.append(batch)
        deadline = time.monotonic() + timeout
        while not self._open.wait(0.01):
            if abort.aborted or time.monotonic() > deadline:
                raise NetworkError("held, then given up")
        with self.lock:
            batch.answered = True
            self.timeline.append("acknowledged")
        ack = {"accepted": [event["eventId"] for event in events], "duplicates": [], "rejected": []}
        return Reply(200, None, json.dumps(ack))

    def hold(self) -> None:
        self._open.clear()

    def release(self) -> None:
        self._open.set()

    def mark(self, what: str) -> None:
        with self.lock:
            self.timeline.append(what)

    def seen(self) -> list[str]:
        with self.lock:
            return list(self.timeline)

    def answered(self) -> list[bool]:
        with self.lock:
            return [batch.answered for batch in self.batches]

    def events(self, kind: str | None = "tool_call") -> list[dict[str, Any]]:
        with self.lock:
            return [event for batch in self.batches for event in batch.events if kind is None or event.get("kind") == kind]


def client(**options: Any) -> tuple[LSquare, Ingestion]:
    ingestion = Ingestion()
    options.setdefault("api_key", "lsq_test_key")
    options.setdefault("endpoint", "http://localhost:1")
    return LSquare(_transport=ingestion, **options), ingestion


def until(check: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


async def until_async(check: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def counting_flushes(lsquare: LSquare, monkeypatch: Any) -> list[str]:
    """Every flush or shutdown of the client from here on, by name."""
    seen: list[str] = []

    def counted(name: str) -> Callable[..., Any]:
        original = getattr(lsquare, name)
        if name.startswith("a"):

            async def waiting(*args: Any, **kwargs: Any) -> Any:
                seen.append(name)
                return await original(*args, **kwargs)

            return waiting

        def blocking(*args: Any, **kwargs: Any) -> Any:
            seen.append(name)
            return original(*args, **kwargs)

        return blocking

    for name in ("flush", "aflush", "shutdown", "ashutdown"):
        monkeypatch.setattr(lsquare, name, counted(name))
    return seen


# -- JSON-RPC by hand -------------------------------------------------------------------------------


def request(method: str, params: dict[str, Any] | None = None, id_: int | None = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if id_ is not None:
        message["id"] = id_
    return message


def calling(arguments: dict[str, Any], name: str = "search", id_: int = 1) -> dict[str, Any]:
    return request("tools/call", {"name": name, "arguments": arguments}, id_)


def initialize(id_: int = 1) -> dict[str, Any]:
    return request("initialize", {"protocolVersion": VERSION, "capabilities": {}, "clientInfo": {"name": "claude-code", "version": "2.1"}}, id_)


def answer(body: str) -> dict[str, Any]:
    """The JSON-RPC response in a JSON or an SSE reply."""
    if body.lstrip().startswith("{"):
        out: dict[str, Any] = json.loads(body)
        return out
    for line in body.splitlines():
        if line.startswith("data:"):
            message: dict[str, Any] = json.loads(line[len("data:") :])
            if "id" in message:
                return message
    raise AssertionError(f"no JSON-RPC response in {body!r}")


def texts(result: dict[str, Any]) -> list[str]:
    return [block["text"] for block in result.get("content", []) if block.get("type") == "text"]


def issued(lines: list[str]) -> str | None:
    match = ID_LINE.match(lines[-1]) if lines else None
    return match.group(1) if match else None


def properties(result: dict[str, Any], name: str) -> list[str]:
    [listed] = [each for each in result["tools"] if each["name"] == name]
    return list(listed["inputSchema"].get("properties", {}))


HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}


@dataclass
class Exchange:
    status: int
    body: str
    headers: dict[str, str]

    def result(self) -> dict[str, Any]:
        out = answer(self.body)
        assert "result" in out, out
        result: dict[str, Any] = out["result"]
        return result


class Http:
    """Raw JSON-RPC over HTTP, through an httpx client (in process or to a served URL)."""

    def __init__(self, http: Any, path: str) -> None:
        self.http = http
        self.path = path
        self.session: str | None = None

    async def post(self, message: dict[str, Any]) -> Exchange:
        headers = {**HEADERS, "mcp-protocol-version": VERSION}
        if self.session:
            headers["mcp-session-id"] = self.session
        response = await self.http.post(self.path, content=json.dumps(message).encode("utf-8"), headers=headers)
        return Exchange(response.status_code, response.text, {key.lower(): value for key, value in response.headers.items()})

    async def handshake(self) -> None:
        """A stateful session's initialize, by hand: the session ID it was issued is kept."""
        hello = await self.post(initialize())
        assert hello.status == 200, hello
        self.session = hello.headers.get("mcp-session-id")
        assert self.session, hello.headers
        done = await self.post(request("notifications/initialized", id_=None))
        assert done.status == 202, done

    async def close(self) -> None:
        if self.session:
            await self.http.delete(self.path, headers={"mcp-session-id": self.session, "mcp-protocol-version": VERSION})


@asynccontextmanager
async def in_process(app: App, path: str) -> AsyncIterator[Http]:
    """httpx's ASGI transport: each response once the app's call returns. No lifespan."""
    httpx: Any = importlib.import_module("httpx")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, timeout=10) as http:
        yield Http(http, path)


@asynccontextmanager
async def served(app: App, path: str) -> AsyncIterator[Http]:
    """`app` served by uvicorn with its lifespan, on a thread with its own loop, as Vercel's
    Python runtime serves it; an httpx client to it."""
    uvicorn: Any = importlib.import_module("uvicorn")
    httpx: Any = importlib.import_module("httpx")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="auto", timeout_graceful_shutdown=2))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("uvicorn didn't start")
            await asyncio.sleep(0.02)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
            yield Http(http, path)
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        sock.close()


class Lifespan:
    """Runs an ASGI app's lifespan, as a server does: startup on entry, and `shutdown()` sends
    the shutdown and waits for the app to complete it, marking the timeline then."""

    def __init__(self, app: App, ingestion: Ingestion) -> None:
        self.app = app
        self.ingestion = ingestion
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.started: asyncio.Future[None] | None = None
        self.ended: asyncio.Future[None] | None = None
        self.task: asyncio.Future[None] | None = None

    async def _send(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        assert self.started is not None and self.ended is not None
        if kind == "lifespan.startup.complete":
            self.started.set_result(None)
        elif kind == "lifespan.startup.failed":
            self.started.set_exception(RuntimeError(message.get("message", "startup failed")))
        elif kind == "lifespan.shutdown.complete":
            self.ingestion.mark("lifespan ended")
            self.ended.set_result(None)
        elif kind == "lifespan.shutdown.failed":
            self.ended.set_exception(RuntimeError(message.get("message", "shutdown failed")))

    async def __aenter__(self) -> Lifespan:
        loop = asyncio.get_running_loop()
        self.started, self.ended = loop.create_future(), loop.create_future()
        scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}, "state": {}}
        self.task = asyncio.ensure_future(self.app(scope, self.inbox.get, self._send))
        await self.inbox.put({"type": "lifespan.startup"})
        await asyncio.wait_for(self.started, 10)
        return self

    async def shutdown(self) -> None:
        assert self.ended is not None and self.task is not None
        if self.ended.done():
            return
        await self.inbox.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(self.ended, 10)
        await asyncio.wait_for(self.task, 10)

    async def __aexit__(self, *exc: object) -> None:
        await self.shutdown()


def observed(app: App, ingestion: Ingestion) -> App:
    """`app` as it is, marking on the ingestion's timeline when a reply's last body is sent and when
    a lifespan shutdown arrives: a reply that waits for its event is marked after the event's
    acknowledgement, and before any teardown."""

    async def observing(scope: Scope, receive: Receive, send: Send) -> None:
        async def receiving() -> dict[str, Any]:
            message = await receive()
            if message.get("type") == "lifespan.shutdown":
                ingestion.mark("teardown")
            return message

        async def sending(message: dict[str, Any]) -> None:
            await send(message)
            if message.get("type") == "http.response.body" and not message.get("more_body", False):
                ingestion.mark("replied")

        await app(scope, receiving, sending)

    return observing


# -- The tools --------------------------------------------------------------------------------------


def search(query: str) -> str:
    return f"found {query}"


def mcp_security(*hosts: str) -> Any:
    """mcp's own DNS-rebinding protection, on by default on both official lines for their
    127.0.0.1 host, refuses any other Host header with 421: API Gateway's must be named."""
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(allowed_hosts=list(hosts))


# -- FastAPI mounting (REFERENCE.md's recipe) -----------------------------------------------------

#: Where the recipe mounts the server, and so its MCP endpoint.
MOUNT = "/orders"
ENDPOINT = "/orders/mcp"


def orders(stateless: bool) -> Any:
    """The line's high-level server, "orders". On mcp 1.x a stateless one is asked for from
    `FastMCP()`, as REFERENCE.md says; the other lines ask for it in `fastapi_app()`."""
    if LINE == "mcp1":
        from mcp.server.fastmcp import FastMCP

        return FastMCP("orders", stateless_http=stateless)
    return high("orders")


def fastapi_app(server: Any, lsquare: LSquare, stateless: bool) -> Any:
    """REFERENCE.md's FastAPI recipe: the line's streamable-HTTP app mounted under /orders, its
    session manager run from the app's lifespan (FastAPI doesn't run a mounted app's own), and
    `await lsquare.ashutdown()` once the session manager has stopped. `server` from `orders()`."""
    fastapi: Any = importlib.import_module("fastapi")
    if LINE == "mcp1":
        assert server.settings.stateless_http is stateless
        mcp_app = server.streamable_http_app()
    elif LINE == "mcp2":
        mcp_app = server.streamable_http_app(stateless_http=stateless)
    else:
        mcp_app = server.http_app(stateless_http=stateless)

    @asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[None]:
        if FAST:
            async with mcp_app.lifespan(app):
                yield
        else:
            async with server.session_manager.run():
                yield
        await lsquare.ashutdown()

    api = fastapi.FastAPI(lifespan=lifespan)
    api.mount(MOUNT, mcp_app)
    return api


# -- AWS Lambda, through Mangum (REFERENCE.md's recipe) -------------------------------------------


def lambda_handler(
    lsquare: LSquare, *, fn: Callable[..., Any] = search, observe: Ingestion | None = None, **options: Any
) -> Any:
    """REFERENCE.md's Lambda recipe for the line: a stateless server with JSON responses, a new
    session manager for each invocation (Mangum runs the ASGI lifespan around every invocation,
    and a session manager runs only once), and the client never shut down in a lifespan. Built at
    the function's cold start; the MCP endpoint is /mcp."""
    mangum: Any = importlib.import_module("mangum")

    def watch(app: App) -> App:
        return observed(app, observe) if observe is not None else app

    if LINE == "mcp1":
        # mcp 1.x's FastMCP keeps one session manager per server: build the server per invocation.
        from mcp.server.fastmcp import FastMCP

        security = mcp_security(API_HOST)

        def build() -> Any:
            mcp = FastMCP("orders", stateless_http=True, json_response=True, transport_security=security)
            mcp.tool()(fn)
            return instrument(mcp, lsquare, **options)

        async def per_server(scope: Scope, receive: Receive, send: Send) -> None:
            mcp = build()
            asgi = mcp.streamable_http_app()
            async with mcp.session_manager.run():
                await asgi(scope, receive, send)

        return mangum.Mangum(watch(per_server), lifespan="off")
    if LINE == "mcp2":
        # Each streamable_http_app() makes a new session manager, mcp.session_manager the latest.
        security = mcp_security(API_HOST)
        server = high("orders")
        tool(server, fn)
        instrument(server, lsquare, **options)

        async def per_invocation(scope: Scope, receive: Receive, send: Send) -> None:
            asgi = server.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)
            async with server.session_manager.run():
                await asgi(scope, receive, send)

        return mangum.Mangum(watch(per_invocation), lifespan="off")
    # fastmcp: http_app()'s lifespan makes a new session manager each time it runs.
    server = high("orders")
    tool(server, fn)
    instrument(server, lsquare, **options)
    return mangum.Mangum(watch(server.http_app(stateless_http=True, json_response=True)))


def api_gateway(message: dict[str, Any], path: str = "/mcp", host: str = API_HOST) -> dict[str, Any]:
    """An API Gateway HTTP API (payload format 2.0) event for one JSON-RPC POST."""
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {**HEADERS, "host": host, "mcp-protocol-version": VERSION, "x-forwarded-proto": "https", "user-agent": "claude-code/2.1"},
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "abc123",
            "domainName": host,
            "domainPrefix": host.split(".")[0],
            "http": {"method": "POST", "path": path, "protocol": "HTTP/1.1", "sourceIp": "203.0.113.7", "userAgent": "claude-code/2.1"},
            "requestId": "c6af9ac6-7b61-11e6-9a41-93e8deadbeef",
            "routeKey": "$default",
            "stage": "$default",
            "time": "29/Oct/2026:10:00:00 +0000",
            "timeEpoch": 1793268000000,
        },
        "body": json.dumps(message),
        "isBase64Encoded": False,
    }


def lambda_result(out: dict[str, Any]) -> dict[str, Any]:
    assert out["statusCode"] == 200, out
    result: dict[str, Any] = answer(out["body"])["result"]
    return result


@dataclass
class LambdaContext:
    function_name: str = "orders"
    aws_request_id: str = "c6af9ac6-7b61-11e6-9a41-93e8deadbeef"
    memory_limit_in_mb: int = 512

    def get_remaining_time_in_millis(self) -> int:
        return 30_000


class LambdaRuntime:
    """One Lambda execution environment: a thread that runs the function's module code once (its
    cold start, `init`, which builds the handler) and then its invocations one at a time, each a
    plain call of the handler, as the Python runtime makes them. Mangum keeps one event loop for
    the thread."""

    def __init__(self, init: Callable[[], Callable[[dict[str, Any], Any], dict[str, Any]]]) -> None:
        self._jobs: queue.Queue[tuple[dict[str, Any], concurrent.futures.Future[dict[str, Any]]] | None] = queue.Queue()
        ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._thread = threading.Thread(target=self._run, args=(init, ready), daemon=True)
        self._thread.start()
        ready.result(30)

    def _run(self, init: Callable[[], Callable[[dict[str, Any], Any], dict[str, Any]]], ready: concurrent.futures.Future[None]) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            try:
                handler = init()
            except BaseException as error:  # noqa: BLE001 - handed to the test
                ready.set_exception(error)
                return
            ready.set_result(None)
            while True:
                job = self._jobs.get()
                if job is None:
                    return
                event, reply = job
                try:
                    reply.set_result(handler(event, LambdaContext()))
                except BaseException as error:  # noqa: BLE001 - handed to the test
                    reply.set_exception(error)
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            asyncio.set_event_loop(None)
            loop.close()

    def invoke(self, event: dict[str, Any]) -> concurrent.futures.Future[dict[str, Any]]:
        reply: concurrent.futures.Future[dict[str, Any]] = concurrent.futures.Future()
        self._jobs.put((event, reply))
        return reply

    def __call__(self, event: dict[str, Any], timeout: float = 15) -> dict[str, Any]:
        return self.invoke(event).result(timeout)

    def close(self) -> None:
        self._jobs.put(None)
        self._thread.join(10)


# -- Vercel's Python runtime ------------------------------------------------------------------------

#: The request's collector, as `vercel.cache.context` keeps the runtime's wait_until callback.
_COLLECTOR: contextvars.ContextVar[Collector | None] = contextvars.ContextVar("vercel_wait_until", default=None)


class Collector:
    """Vercel's per-invocation `WaitUntilCollector` (vercel_runtime/wait_until.py): it keeps each
    awaitable it's given, and after the app has answered, the runtime awaits them all before the
    invocation ends. Started late, so the awaitable must not need starting to make progress."""

    def __init__(self, ingestion: Ingestion) -> None:
        self.ingestion = ingestion
        self.handed: list[Awaitable[object]] = []
        self.pending: list[Awaitable[object]] = []
        self.closed = False
        self.drained = threading.Event()
        self.lock = threading.Lock()

    def wait_until(self, awaitable: Awaitable[object]) -> None:
        with self.lock:
            if self.closed:
                close = getattr(awaitable, "close", None)
                if callable(close):
                    close()
                raise RuntimeError("wait_until cannot be called after the invocation ended")
            self.handed.append(awaitable)
            self.pending.append(awaitable)

    async def drain(self) -> None:
        while True:
            with self.lock:
                batch, self.pending = self.pending, []
                if not batch:
                    self.closed = True
                    break
            await asyncio.gather(*(asyncio.ensure_future(each) for each in batch), return_exceptions=True)
        if self.handed:
            self.ingestion.mark("invocation ended")
        self.drained.set()


def wait_until(awaitable: Awaitable[object]) -> None:
    """`vercel.functions.wait_until`: hands the awaitable to the current request's collector, found
    through a context variable; outside a request it closes a coroutine and does nothing."""
    collector = _COLLECTOR.get()
    if collector is not None:
        collector.wait_until(awaitable)
        return
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()


def vercel(app: App, ingestion: Ingestion) -> tuple[App, list[Collector]]:
    """`app` as Vercel's Python runtime runs it (vc_init.py's ASGIMiddleware, served by uvicorn
    with its lifespan): each HTTP request gets a collector, set in its context before the app is
    called and drained once the app's call returns, after its reply was sent. The collectors, in
    request order."""
    collectors: list[Collector] = []

    async def runtime(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        collector = Collector(ingestion)
        collectors.append(collector)
        token = _COLLECTOR.set(collector)

        async def sending(message: dict[str, Any]) -> None:
            await send(message)
            if message.get("type") == "http.response.body" and not message.get("more_body", False):
                ingestion.mark("replied")

        try:
            await app(scope, receive, sending)
        finally:
            try:
                await collector.drain()
            finally:
                _COLLECTOR.reset(token)

    return runtime, collectors
