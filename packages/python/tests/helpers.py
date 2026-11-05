"""Shared test helpers: a fake transport, a logger that records, and a local ingestion server."""

from __future__ import annotations

import asyncio
import gc
import http.server
import json
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

from lsquare._sending import Abort, NetworkError, Reply


def ack(*accepted: str, rejected: list[dict[str, Any]] | None = None, duplicates: list[str] | None = None) -> str:
    return json.dumps({"accepted": list(accepted), "duplicates": duplicates or [], "rejected": rejected or []})


class FakeTransport:
    """Records each request's events; answers with `respond(events) -> Reply | Exception`."""

    def __init__(self, respond: Callable[[list[dict[str, Any]]], Any] | None = None) -> None:
        self.requests: list[list[dict[str, Any]]] = []
        self.lock = threading.Lock()
        self.respond = respond or (lambda events: Reply(200, None, ack(*[event["eventId"] for event in events])))

    def __call__(self, body: bytes, timeout: float, abort: Abort) -> Reply:
        events = json.loads(body.decode("utf-8"))["events"]
        with self.lock:
            self.requests.append(events)
        out = self.respond(events)
        if isinstance(out, BaseException):
            raise out
        return out

    @property
    def events(self) -> list[dict[str, Any]]:
        with self.lock:
            return [event for request in self.requests for event in request]


class Held:
    """A fake transport that holds each batch's answer until a test releases it: that batch, or
    every batch from then on. Whether a reply or a hand-off waited for the sending is then exact.
    A batch still held after 10 s fails as a network error."""

    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.answered: list[bool] = []
        #: Set once a batch has reached it.
        self.entered = threading.Event()
        self.lock = threading.Lock()
        self._gates: list[threading.Event] = []
        self._open = threading.Event()

    def __call__(self, body: bytes, timeout: float, abort: Abort) -> Reply:
        events = json.loads(body.decode("utf-8"))["events"]
        gate = threading.Event()
        with self.lock:
            index = len(self.batches)
            self.batches.append(events)
            self.answered.append(False)
            self._gates.append(gate)
        self.entered.set()
        deadline = time.monotonic() + 10
        while not (gate.is_set() or self._open.is_set()):
            if abort.aborted or time.monotonic() > deadline:
                raise NetworkError("held, then given up")
            gate.wait(0.01)
        with self.lock:
            self.answered[index] = True
        return Reply(200, None, ack(*[event["eventId"] for event in events]))

    def release(self, index: int | None = None) -> None:
        """Answers batch `index`, or, without one, every batch, held or to come."""
        if index is None:
            self._open.set()
        else:
            with self.lock:
                gate = self._gates[index]
            gate.set()

    def kinds(self) -> list[list[str]]:
        with self.lock:
            return [[event["kind"] for event in batch] for batch in self.batches]

    def acknowledged(self, kind: str) -> list[dict[str, Any]]:
        """The events of this kind in the batches answered so far."""
        with self.lock:
            return [event for batch, answered in zip(self.batches, self.answered) if answered for event in batch if event["kind"] == kind]


def network_error(_: Any) -> NetworkError:
    return NetworkError("down")


class Recorder:
    """A logger and an `on_error` that record what they get, and wait for it."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.errors: list[Any] = []
        self.events: list[Any] = []
        self.lock = threading.Condition()

    def info(self, message: str) -> None:
        with self.lock:
            self.lines.append(("info", message))
            self.lock.notify_all()

    def warn(self, message: str) -> None:
        with self.lock:
            self.lines.append(("warn", message))
            self.lock.notify_all()

    def on_error(self, error: Any, event: Any = None) -> None:
        with self.lock:
            self.errors.append(error)
            self.events.append(event)
            self.lock.notify_all()

    def codes(self) -> list[str]:
        with self.lock:
            return [error.code for error in self.errors]

    def wait_for(self, check: Callable[[], bool], timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self.lock:
            while not check():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.lock.wait(left)
            return True


def eventually(check: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return check()


def collected(*refs: weakref.ref[Any], timeout: float = 5.0) -> bool:
    """Collects garbage until each of `refs` is dead, at most `timeout`: whether they all died.
    What lets go of them may do so a moment after what the test waited for, on another thread."""
    deadline = time.monotonic() + timeout
    while True:
        gc.collect()
        if all(ref() is None for ref in refs):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)


async def acollected(*refs: weakref.ref[Any], timeout: float = 5.0) -> bool:
    """`collected` on a loop, which it leaves free between attempts: what lets go of them may be
    one of its callbacks."""
    deadline = time.monotonic() + timeout
    while True:
        gc.collect()
        if all(ref() is None for ref in refs):
            return True
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.01)


def run_until(loop: asyncio.AbstractEventLoop, step: Callable[[], None], check: Callable[[], bool], timeout: float = 5.0) -> None:
    """Runs `step` on an app's `loop`, then the loop on until `check()`, at most `timeout`, so the
    async handlers of what `step` reported start there; then the loop stops, as after
    `run_until_complete`, with its tasks still pending."""

    async def main() -> None:
        step()
        deadline = time.monotonic() + timeout
        while not check() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

    loop.run_until_complete(main())


class Ingestion:
    """A local HTTP server standing in for the ingestion: records requests, answers as told."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        #: (status, headers, body) for the next requests, in order; then a normal acknowledgement.
        self.script: list[tuple[int, dict[str, str], bytes] | Callable[[], object]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
                with outer.lock:
                    outer.requests.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
                    step = outer.script.pop(0) if outer.script else None
                if callable(step):
                    step()
                    step = None
                if step is None:
                    payload = ack(*[event["eventId"] for event in body["events"]]).encode()
                    step = (200, {"content-type": "application/json"}, payload)
                status, headers, payload = step
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def events(self) -> list[dict[str, Any]]:
        with self.lock:
            return [event for request in self.requests for event in request["body"]["events"]]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
