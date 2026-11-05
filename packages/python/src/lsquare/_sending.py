"""One HTTP exchange with the ingestion (spec section 3, "Sending").

Standard library only. The whole exchange has one deadline: the host's address comes from one
resolver thread per client, shared and cached, so a resolver that hangs costs one thread; its
addresses are connected to in staggered attempts, so one that hangs costs 250 ms; TLS, sending and
reading each get a socket timeout of the time left; and a watchdog shuts the socket down at the
deadline, which wakes a blocked read. Only a 2xx body is read, at most 64 KB; one that ends before
its Content-Length is a network error, as in TypeScript.
"""

from __future__ import annotations

import errno
import http.client
import ipaddress
import os
import selectors
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ._threads import interrupts

MAX_RESPONSE_BYTES = 64 * 1024
#: How long a resolved address is kept.
ADDRESS_TTL_S = 60.0
#: How long the resolver thread waits for a lookup before it exits.
IDLE_S = 30.0
#: How long a connection attempt runs alone before the next address is tried beside it: RFC 8305's
#: suggested Connection Attempt Delay.
ATTEMPT_DELAY_S = 0.25
#: The longest connecting waits at a time, so an abort closes the pending attempts promptly.
POLL_S = 0.05
#: What a non-blocking connect returns while it is under way (Windows: WSAEWOULDBLOCK).
_CONNECTING = frozenset(
    code
    for code in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN, getattr(errno, "WSAEWOULDBLOCK", None))
    if code is not None
)

Address = tuple[int, int, int, Any]
#: Starts a daemon thread running `target`, in an empty context. Raises when threads can't start.
StartThread = Callable[[Callable[[], None], str], None]
#: Runs `fn` after `delay` seconds on the client's scheduler; returns a function that cancels it.
Schedule = Callable[[float, Callable[[], None]], Callable[[], None]]


class NetworkError(Exception):
    """No usable response: resolution, connection, TLS, timeout, a refused redirect, an abort."""


@dataclass
class Reply:
    status: int
    #: The `Retry-After` header as sent, if any.
    retry_after: str | None
    #: A 2xx body, decoded; `None` when it was larger than 64 KB or unreadable; `""` otherwise.
    body: str | None


class Abort:
    """Aborts an exchange in flight: its socket is shut down. Safe from any thread."""

    def __init__(self) -> None:
        #: Reentrant: shutdown() from a signal handler or finalizer can abort it from inside _attach.
        self._lock = threading.RLock()
        self._aborted = False
        self._sock: socket.socket | None = None

    @property
    def aborted(self) -> bool:
        return self._aborted

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            sock = self._sock
        _shut(sock)

    def _attach(self, sock: socket.socket | None) -> None:
        with self._lock:
            self._sock = sock
            aborted = self._aborted
        if aborted:
            _shut(sock)


def _shut(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class Resolver:
    """One thread per client resolves host names, one at a time, for every request waiting on one.

    A lookup that outlasts a request's deadline fails that request and keeps the thread until it
    returns, so a hung resolver never costs more than this one thread.
    """

    def __init__(self, start_thread: StartThread, clock: Callable[[], float] = time.monotonic) -> None:
        self._start_thread = start_thread
        self._clock = clock
        #: Reentrant: shutdown() from a signal handler can stop it on a thread inside resolve().
        self._lock = threading.RLock()
        self._cache: dict[tuple[str, int], tuple[float, list[Address]]] = {}
        self._waiting: dict[tuple[str, int], _Lookup] = {}
        self._queue: list[tuple[str, int]] = []
        self._wake = threading.Condition(self._lock)
        #: The token of the start or thread that owns the thread, as the scheduler's.
        self._owner: dict[str, object] = {}
        self._stopping = False

    def cached(self, host: str, port: int) -> list[Address] | None:
        with self._lock:
            hit = self._cache.get((host, port))
            return hit[1] if hit is not None and hit[0] > self._clock() else None

    def resolve(self, host: str, port: int, timeout: float) -> list[Address]:
        if _is_ip(host):
            return _getaddrinfo(host, port)
        key = (host, port)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] > self._clock():
                return hit[1]
            lookup = self._waiting.get(key)
            if lookup is None:
                lookup = _Lookup()
                self._waiting[key] = lookup
                self._queue.append(key)
                self._wake.notify()
        token = object()
        try:
            if self._owner.setdefault("thread", token) is token:
                self._start_thread(lambda: self._run(token), "lsquare-resolver")
        except BaseException as error:  # noqa: BLE001 - an interruption is passed on below
            # It raised, or was interrupted after its thread may have begun: the token is taken
            # back unless that thread took over already.
            with self._lock:
                if self._owner.get("thread") is token:
                    del self._owner["thread"]
            if not isinstance(error, Exception):
                raise
            raise NetworkError("The resolver thread could not start.") from None
        if not lookup.done.wait(max(0.0, timeout)):
            raise NetworkError("Resolving the host took longer than the request's deadline.")
        if lookup.error is not None or lookup.addresses is None:
            raise NetworkError("The host could not be resolved.")
        return lookup.addresses

    def reset(self) -> None:
        """After a fork: the child has none of the parent's threads."""
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._cache = {}
        self._waiting = {}
        self._queue = []
        self._owner = {}
        self._stopping = False

    def stop(self) -> None:
        """The thread exits once idle (a lookup in progress still finishes)."""
        with self._lock:
            self._stopping = True
            self._wake.notify_all()

    def _run(self, token: object) -> None:
        own = object()
        with self._lock:
            if self._owner.get("thread") is token:
                self._owner["thread"] = own
            elif self._owner.setdefault("thread", own) is not own:
                # Its start took its token back, and another thread runs.
                return
        while True:
            with self._lock:
                while not self._queue:
                    if self._stopping or (not self._wake.wait(IDLE_S) and not self._queue):
                        self._owner.pop("thread", None)
                        return
                key = self._queue.pop(0)
                lookup = self._waiting[key]
            addresses: list[Address] | None = None
            error: BaseException | None = None
            try:
                addresses = _getaddrinfo(key[0], key[1])
            except BaseException as caught:  # noqa: BLE001 - reported to the waiting request
                error = caught
            with self._lock:
                if addresses:
                    self._cache[key] = (self._clock() + ADDRESS_TTL_S, addresses)
                self._waiting.pop(key, None)
            lookup.addresses = addresses
            lookup.error = error
            lookup.done.set()


class _Lookup:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.addresses: list[Address] | None = None
        self.error: BaseException | None = None


def _green_sockets_beside_a_kernel_selector() -> bool:
    """gevent or eventlet patching sockets but not selectors (gevent's `patch_all(select=False)`)."""
    try:
        base = _DeadlineSocket.__mro__[1]
        return base.__module__ != "socket" and selectors.DefaultSelector.__module__ == "selectors"
    except Exception:
        return False


def _interleave(addresses: list[Address]) -> list[Address]:
    """RFC 8305 section 4: the address families alternate, starting with the first address's."""
    if not addresses:
        return []
    first = [address for address in addresses if address[0] == addresses[0][0]]
    rest = [address for address in addresses if address[0] != addresses[0][0]]
    ordered: list[Address] = []
    for n in range(max(len(first), len(rest))):
        ordered += first[n : n + 1] + rest[n : n + 1]
    return ordered


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def _getaddrinfo(host: str, port: int) -> list[Address]:
    found = socket.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_STREAM)
    return [(family, kind, proto, address) for family, kind, proto, _canon, address in found]


class _DeadlineSocket(socket.socket):
    """A socket whose every send and receive takes only the time left before its deadline, so a
    server trickling its reply a byte at a time can't outlast it, watchdog or not."""

    deadline: float | None = None

    def _arm(self) -> None:
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("The request's deadline passed.")
            self.settimeout(left)

    def recv_into(self, *args: Any, **kwargs: Any) -> int:
        self._arm()
        return super().recv_into(*args, **kwargs)

    def recv(self, *args: Any, **kwargs: Any) -> bytes:
        self._arm()
        return super().recv(*args, **kwargs)

    def send(self, *args: Any, **kwargs: Any) -> int:
        self._arm()
        return super().send(*args, **kwargs)

    def sendall(self, *args: Any, **kwargs: Any) -> None:
        self._arm()
        super().sendall(*args, **kwargs)


class _DeadlineSSLSocket(ssl.SSLSocket):
    """The same, over TLS."""

    deadline: float | None = None

    def _arm(self) -> None:
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("The request's deadline passed.")
            self.settimeout(left)

    def recv_into(self, *args: Any, **kwargs: Any) -> int:
        self._arm()
        return super().recv_into(*args, **kwargs)

    def recv(self, *args: Any, **kwargs: Any) -> bytes:
        self._arm()
        return super().recv(*args, **kwargs)

    def read(self, *args: Any, **kwargs: Any) -> Any:
        self._arm()
        return super().read(*args, **kwargs)

    def send(self, *args: Any, **kwargs: Any) -> int:
        self._arm()
        return super().send(*args, **kwargs)

    def sendall(self, *args: Any, **kwargs: Any) -> None:
        self._arm()
        super().sendall(*args, **kwargs)


_tls_lock = threading.Lock()
_tls_context: ssl.SSLContext | None = None


def _after_fork() -> None:
    # A thread of the parent's may have held the lock at the fork: the child gets a fresh one.
    global _tls_lock
    _tls_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def tls_context() -> ssl.SSLContext:
    """The system's trust store; certifi's bundle too when the system gave none and it's installed."""
    global _tls_context
    with _tls_lock:
        if _tls_context is None:
            context = ssl.create_default_context()
            context.sslsocket_class = _DeadlineSSLSocket
            try:
                if context.cert_store_stats().get("x509_ca", 0) == 0:
                    import certifi  # type: ignore[import-not-found,unused-ignore]

                    context.load_verify_locations(certifi.where())
            except Exception:
                pass
            _tls_context = context
        return _tls_context


class _Connection(http.client.HTTPConnection):
    """Uses the socket the exchange made, and leaves closing it to the exchange: http.client would
    close it on a `Connection: close` reply, before the body is read with its own timeouts."""

    def close(self) -> None:
        self.sock = None


class Exchange:
    """POSTs a body to the ingestion's `/v1/turns` with the client's key."""

    def __init__(
        self,
        url: str,
        api_key: str,
        user_agent: str,
        resolver: Resolver,
        schedule: Schedule,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        parts = urlsplit(url)
        self._https = parts.scheme == "https"
        self._host = parts.hostname or ""
        # Port 0 is the one written, as fetch reads it (which then can't connect), not the default.
        self._port = parts.port if parts.port is not None else (443 if self._https else 80)
        self._path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        self._headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
            "user-agent": user_agent,
        }
        self._resolver = resolver
        self._schedule = schedule
        self._clock = clock

    def post(self, body: bytes, timeout_s: float, abort: Abort) -> Reply:
        deadline = self._clock() + timeout_s

        def left() -> float:
            remaining = deadline - self._clock()
            if remaining <= 0 or abort.aborted:
                raise NetworkError("The request's deadline passed.")
            return remaining

        cancel_watchdog = self._schedule(timeout_s, abort.abort)
        sock: socket.socket | None = None
        try:
            addresses = self._resolver.resolve(self._host, self._port, left())
            sock = self._connect(addresses, left, deadline)
            abort._attach(sock)
            if self._https:
                sock.settimeout(left())
                sock = tls_context().wrap_socket(sock, server_hostname=self._host)
                if isinstance(sock, _DeadlineSSLSocket):
                    sock.deadline = self._deadline_wall(deadline)
                abort._attach(sock)
            connection = _Connection(self._host, self._port, timeout=left())
            connection.sock = sock
            sock.settimeout(left())
            connection.request("POST", self._path, body=body, headers=self._headers)
            sock.settimeout(left())
            response = connection.getresponse()
            status = response.status
            retry_after = response.getheader("retry-after")
            if 300 <= status < 400:
                # Redirects are refused, as TypeScript's `redirect: 'error'`: a network error.
                raise NetworkError("The ingestion answered with a redirect, which is refused.")
            text: str | None = ""
            if 200 <= status < 300:
                text = self._read(response, sock, left)
            return Reply(status=status, retry_after=retry_after, body=text)
        except NetworkError:
            raise
        except Exception as error:
            raise NetworkError(type(error).__name__) from None
        finally:
            cancel_watchdog()
            abort._attach(None)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _deadline_wall(self, deadline: float) -> float:
        """The deadline on `time.monotonic()`, which the sockets read (the exchange's clock may be a test's)."""
        return time.monotonic() + (deadline - self._clock())

    def _connect(self, addresses: list[Address], left: Callable[[], float], deadline: float) -> socket.socket:
        """Staggered concurrent attempts, in the manner of RFC 8305: the addresses alternate by family,
        each attempt runs alone for `ATTEMPT_DELAY_S` (or until it fails) before the next starts beside
        it, and the first to connect wins. An address that hangs, such as a blackholed IPv6 one, costs
        the others 250 ms, not the whole deadline."""
        if _green_sockets_beside_a_kernel_selector():
            return self._connect_in_turn(addresses, left, deadline)
        order = _interleave(addresses)
        selector = selectors.DefaultSelector()
        pending: list[socket.socket] = []
        last: BaseException | None = None
        next_at = self._clock()
        # In real time too: the exchange's clock may be a test's.
        wall = self._deadline_wall(deadline)
        try:
            while True:
                left()  # Raises at the deadline, or once aborted.
                if time.monotonic() >= wall:
                    raise NetworkError("The request's deadline passed.")
                if order and (not pending or self._clock() >= next_at):
                    family, kind, proto, address = order.pop(0)
                    sock: socket.socket | None = None
                    connected: socket.socket | None = None
                    try:
                        sock = _DeadlineSocket(family, kind, proto)
                        sock.setblocking(False)
                        code = sock.connect_ex(address)
                        if code == 0:
                            connected, sock = sock, None
                        elif code not in _CONNECTING:
                            raise OSError(code, os.strerror(code))
                        else:
                            selector.register(sock, selectors.EVENT_WRITE)
                            pending.append(sock)
                            sock = None
                            next_at = self._clock() + ATTEMPT_DELAY_S
                    except Exception as error:  # noqa: BLE001 - this address failed; the next is tried at once
                        last = error
                    finally:
                        if sock is not None:
                            sock.close()
                    if connected is not None:
                        return self._connected(connected, left, deadline)
                    continue
                if not pending:
                    raise NetworkError(type(last).__name__ if last else "No address to connect to.")
                # Never long at a time, so an abort is noticed while attempts are pending.
                wait = min(left(), POLL_S)
                if order:
                    wait = min(wait, max(0.0, next_at - self._clock()))
                for key, _ in selector.select(wait):
                    ready: socket.socket = key.fileobj  # type: ignore[assignment]
                    selector.unregister(ready)
                    pending.remove(ready)
                    try:
                        code = ready.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    except OSError as error:
                        code = error.errno or -1
                    if code == 0:
                        return self._connected(ready, left, deadline)
                    ready.close()
                    last = OSError(code, os.strerror(code))
                    # A failed attempt hands over at once, as one that fails to start does.
                    next_at = self._clock()
        finally:
            # Every attempt that didn't win, on every way out: the selector first, so no socket is
            # closed while still registered.
            selector.close()
            for sock in pending:
                sock.close()

    def _connect_in_turn(self, addresses: list[Address], left: Callable[[], float], deadline: float) -> socket.socket:
        """One address after another, each given the time left: with green sockets, whose own
        connect yields, but a kernel selector, which would block every greenlet."""
        last: BaseException | None = None
        for family, kind, proto, address in addresses:
            sock: socket.socket | None = None
            try:
                # Inside: an IPv6 socket that can't even be created moves on to IPv4.
                sock = _DeadlineSocket(family, kind, proto)
                sock.settimeout(left())
                sock.connect(address)
                return self._connected(sock, left, deadline)
            except BaseException as error:  # noqa: BLE001 - the next address is tried
                last = error
                if sock is not None:
                    sock.close()
                # Inline at exit this runs on the main thread: a signal handler's SystemExit goes on.
                if isinstance(error, NetworkError) or interrupts(error):
                    raise
        raise NetworkError(type(last).__name__ if last else "No address to connect to.")

    def _connected(self, sock: socket.socket, left: Callable[[], float], deadline: float) -> socket.socket:
        try:
            sock.settimeout(left())
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            sock.deadline = self._deadline_wall(deadline)  # type: ignore[attr-defined]
            return sock
        except BaseException:
            sock.close()
            raise

    @staticmethod
    def _read(response: http.client.HTTPResponse, sock: socket.socket, left: Callable[[], float]) -> str | None:
        chunks: list[bytes] = []
        total = 0
        while True:
            sock.settimeout(left())
            chunk = response.read1(16 * 1024) if hasattr(response, "read1") else response.read(16 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                return None
            chunks.append(chunk)
        if response.length:
            # The connection closed before its Content-Length had come, which http.client's read1
            # reports by returning b'' without raising: a network error, retried, as TypeScript's
            # fetch rejects such a body. A chunked body cut short raises IncompleteRead.
            raise NetworkError("The ingestion's reply ended before its Content-Length.")
        # As TypeScript's TextDecoder: one leading byte order mark is dropped, invalid bytes become
        # U+FFFD, and the JSON decides.
        return b"".join(chunks).decode("utf-8-sig", errors="replace")
