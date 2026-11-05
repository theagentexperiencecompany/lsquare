"""The real HTTP exchange against a local server: headers, the path, the body cap, the deadline,
redirects, a hung resolver costing one thread, and connecting past an address that hangs."""

from __future__ import annotations

import errno
import selectors
import socket
import threading
import time
import types
from collections.abc import Callable
from typing import Any

import pytest

from lsquare import LSquare, __version__
from lsquare import _sending
from lsquare._sending import Abort, Exchange, NetworkError, Resolver
from lsquare._threads import Scheduler, start_thread

from helpers import Ingestion, Recorder, eventually

KEY = "lsq_test_key"


@pytest.fixture
def ingestion() -> Any:
    server = Ingestion()
    yield server
    server.close()


def test_events_reach_the_ingestion_with_our_key_user_agent_and_path(ingestion: Ingestion) -> None:
    lsquare = LSquare(api_key=KEY, endpoint=ingestion.url, logger=Recorder())
    lsquare.capture({"conversationId": "c", "turnId": 1, "question": "q"})
    lsquare.flush(timeout_ms=5_000)
    assert eventually(lambda: len(ingestion.requests) == 1)
    request = ingestion.requests[0]
    headers = {key.lower(): value for key, value in request["headers"].items()}
    assert request["path"] == "/v1/turns"
    assert headers["authorization"] == f"Bearer {KEY}"
    assert headers["user-agent"].startswith(f"lsquare-python/{__version__} (python/")
    assert headers["content-type"] == "application/json"


def test_an_endpoint_path_and_trailing_slash_are_kept(ingestion: Ingestion) -> None:
    lsquare = LSquare(api_key=KEY, endpoint=ingestion.url + "/base/", logger=Recorder())
    lsquare.capture({"conversationId": "c", "turnId": 1})
    lsquare.flush(timeout_ms=5_000)
    assert ingestion.requests[0]["path"] == "/base/v1/turns"


def test_an_acknowledgement_over_64_kb_is_a_failure(ingestion: Ingestion) -> None:
    recorder = Recorder()
    ingestion.script.append((200, {"content-type": "application/json"}, b'{"accepted":[' + b'"x",' * 20_000 + b'"y"]}'))
    lsquare = LSquare(api_key=KEY, endpoint=ingestion.url, logger=recorder, on_error=recorder.on_error)
    lsquare.capture({"conversationId": "c", "turnId": 1})
    lsquare.flush(timeout_ms=5_000)
    assert recorder.wait_for(lambda: "send_failed" in recorder.codes())


def test_a_redirect_is_refused_and_retried_as_a_network_error(ingestion: Ingestion) -> None:
    for _ in range(3):
        ingestion.script.append((307, {"location": "https://elsewhere.example.com/v1/turns"}, b""))
    recorder = Recorder()
    lsquare = LSquare(api_key=KEY, endpoint=ingestion.url, logger=recorder, on_error=recorder.on_error, _random=lambda: 0.0)
    lsquare.capture({"conversationId": "c", "turnId": 1})
    lsquare.flush(timeout_ms=5_000)
    assert len(ingestion.requests) == 3
    assert recorder.wait_for(lambda: any("Network error" in error.message for error in recorder.errors))


def test_a_stalled_body_is_cut_at_the_request_deadline(ingestion: Ingestion) -> None:
    stall = threading.Event()
    ingestion.script.append(lambda: stall.wait(5))
    recorder = Recorder()
    lsquare = LSquare(
        api_key=KEY, endpoint=ingestion.url, logger=recorder, on_error=recorder.on_error,
        _request_timeout_ms=300, _max_attempts=1,
    )
    lsquare.capture({"conversationId": "c", "turnId": 1})
    started = time.monotonic()
    lsquare.flush(timeout_ms=5_000)
    assert recorder.wait_for(lambda: "send_failed" in recorder.codes())
    assert time.monotonic() - started < 2
    stall.set()


def test_a_hung_resolver_costs_one_thread_and_fails_each_request_at_its_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    hang = threading.Event()
    calls: list[str] = []

    def stuck(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
        calls.append(host)
        hang.wait(10)
        raise socket.gaierror("gave up")

    monkeypatch.setattr(socket, "getaddrinfo", stuck)
    # The resolver's own starts, counted where it makes them: the process's thread count also moves
    # with what earlier tests left running.
    started: list[str] = []

    def counted(target: Callable[[], None], name: str) -> None:
        started.append(name)
        start_thread(target, name)

    resolver = Resolver(counted)
    scheduler = Scheduler()
    exchange = Exchange("https://ingest.example.invalid/v1/turns", KEY, "ua", resolver, scheduler.call_later)
    for _ in range(5):
        with pytest.raises(NetworkError):
            exchange.post(b"{}", 0.1, Abort())
    assert len(started) == 1
    assert calls == ["ingest.example.invalid"]
    hang.set()


def test_an_aborted_exchange_stops_at_once(ingestion: Ingestion) -> None:
    stall = threading.Event()
    ingestion.script.append(lambda: stall.wait(5))
    scheduler = Scheduler()
    exchange = Exchange(ingestion.url + "/v1/turns", KEY, "ua", Resolver(start_thread), scheduler.call_later)
    abort = Abort()
    threading.Timer(0.2, abort.abort).start()
    started = time.monotonic()
    with pytest.raises(NetworkError):
        exchange.post(b'{"events":[]}', 5, abort)
    assert time.monotonic() - started < 2
    stall.set()


# Connecting: staggered attempts, so an address whose connect hangs (a blackholed IPv6 one, say)
# costs the next one 250 ms, not the request's deadline. First against fakes, deterministically:
# sockets whose connect is scripted per address, a selector that moves the exchange's clock.

V6 = socket.AF_INET6
V4 = socket.AF_INET


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Network:
    """What each address does: ("ok", after_s), ("fail", after_s), "hang", "refused"; and the
    families whose sockets can't even be created."""

    def __init__(self, clock: _Clock, plans: dict[Any, Any], unmakeable: frozenset[int] = frozenset()) -> None:
        self.clock = clock
        self.plans = plans
        self.unmakeable = unmakeable
        self.sockets: list[Any] = []
        self.selectors: list[Any] = []
        self.dialled: list[Any] = []
        network = self

        class Socket:
            def __init__(self, family: int, kind: int, proto: int) -> None:
                if family in network.unmakeable:
                    raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")
                self.address: Any = None
                self.closed = False
                network.sockets.append(self)

            def setblocking(self, flag: bool) -> None:
                pass

            def connect_ex(self, address: Any) -> int:
                self.address = address
                network.dialled.append(address)
                plan = network.plans[address]
                return errno.ECONNREFUSED if plan == "refused" else errno.EINPROGRESS

            def getsockopt(self, level: int, option: int) -> int:
                return 0 if network.plans[self.address][0] == "ok" else errno.ECONNREFUSED

            def settimeout(self, timeout: float) -> None:
                pass

            def setsockopt(self, *args: Any) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        class Selector:
            def __init__(self) -> None:
                self.ready_at: dict[Any, float | None] = {}
                self.closed = False
                network.selectors.append(self)

            def register(self, sock: Any, events: int) -> None:
                plan = network.plans[sock.address]
                self.ready_at[sock] = None if plan == "hang" else network.clock.now + plan[1]

            def unregister(self, sock: Any) -> None:
                del self.ready_at[sock]

            def select(self, timeout: float) -> list[Any]:
                due = [at for at in self.ready_at.values() if at is not None]
                soonest = min(due, default=float("inf"))
                if soonest <= network.clock.now + timeout:
                    network.clock.now = max(network.clock.now, soonest)
                    return [(selectors.SelectorKey(sock, 0, selectors.EVENT_WRITE, None), selectors.EVENT_WRITE) for sock, at in self.ready_at.items() if at is not None and at <= network.clock.now]
                network.clock.now += timeout
                return []

            def close(self) -> None:
                self.closed = True

        self.Socket = Socket
        self.Selector = Selector


def _connect(monkeypatch: pytest.MonkeyPatch, addresses: list[Any], plans: dict[Any, Any], deadline: float = 10.0, unmakeable: frozenset[int] = frozenset(), abort_at: float | None = None) -> tuple[Any, _Network]:
    clock = _Clock()
    network = _Network(clock, plans, unmakeable)
    monkeypatch.setattr(_sending, "_DeadlineSocket", network.Socket)
    monkeypatch.setattr(_sending, "_green_sockets_beside_a_kernel_selector", lambda: False)
    monkeypatch.setattr(_sending, "selectors", types.SimpleNamespace(DefaultSelector=network.Selector, EVENT_WRITE=selectors.EVENT_WRITE))
    exchange = Exchange("http://ingest.test/v1/turns", KEY, "ua", Resolver(start_thread), lambda delay, fn: (lambda: None), clock)

    def left() -> float:
        if abort_at is not None and clock.now >= abort_at:
            raise NetworkError("Aborted.")
        if clock.now >= deadline:
            raise NetworkError("The request's deadline passed.")
        return deadline - clock.now

    try:
        return exchange._connect(addresses, left, deadline), network
    except NetworkError as error:
        return error, network


def _address(family: int, host: str) -> Any:
    return (family, socket.SOCK_STREAM, 6, (host, 443))


def _closed_but(network: _Network, winner: Any) -> bool:
    return all(sock.closed for sock in network.sockets if sock is not winner) and not getattr(winner, "closed", False) and all(selector.closed for selector in network.selectors)


def test_an_address_that_hangs_costs_the_next_250_ms_and_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    v6, v4 = _address(V6, "2001:db8::1"), _address(V4, "192.0.2.1")
    sock, network = _connect(monkeypatch, [v6, v4], {v6[3]: "hang", v4[3]: ("ok", 0.01)})
    assert sock.address == v4[3]
    assert network.clock.now == pytest.approx(0.26)
    assert _closed_but(network, sock)


def test_a_refused_address_or_one_whose_socket_cant_be_made_moves_on_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    v6, v4 = _address(V6, "2001:db8::1"), _address(V4, "192.0.2.1")
    sock, network = _connect(monkeypatch, [v6, v4], {v6[3]: "refused", v4[3]: ("ok", 0.01)})
    assert sock.address == v4[3] and network.clock.now == pytest.approx(0.01)
    sock, network = _connect(monkeypatch, [v6, v4], {v6[3]: ("ok", 0.01), v4[3]: ("ok", 0.01)}, unmakeable=frozenset({V6}))
    assert sock.address == v4[3] and network.clock.now == pytest.approx(0.01)


def test_a_slow_but_working_attempt_stays_open_and_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    v6, v4 = _address(V6, "2001:db8::1"), _address(V4, "192.0.2.1")
    sock, network = _connect(monkeypatch, [v6, v4], {v6[3]: ("ok", 0.4), v4[3]: "hang"})
    assert sock.address == v6[3]
    assert network.clock.now == pytest.approx(0.4)
    assert _closed_but(network, sock)


def test_an_attempt_that_fails_while_another_hangs_hands_over_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    hung, failing, working = _address(V4, "192.0.2.1"), _address(V4, "192.0.2.2"), _address(V4, "192.0.2.3")
    plans = {hung[3]: "hang", failing[3]: ("fail", 0.01), working[3]: ("ok", 0.01)}
    sock, network = _connect(monkeypatch, [hung, failing, working], plans, deadline=0.4)
    assert sock.address == working[3]
    assert network.clock.now == pytest.approx(0.27)
    assert _closed_but(network, sock)


def test_the_families_alternate(monkeypatch: pytest.MonkeyPatch) -> None:
    a6, b6, a4, b4 = _address(V6, "2001:db8::1"), _address(V6, "2001:db8::2"), _address(V4, "192.0.2.1"), _address(V4, "192.0.2.2")
    plans = {a6[3]: ("fail", 0.01), b6[3]: ("fail", 0.01), a4[3]: ("fail", 0.01), b4[3]: ("ok", 0.01)}
    sock, network = _connect(monkeypatch, [a6, b6, a4, b4], plans)
    assert network.dialled == [a6[3], a4[3], b6[3], b4[3]]
    assert sock.address == b4[3]


def test_every_attempt_ends_at_the_deadline_or_an_abort_and_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    v6, v4 = _address(V6, "2001:db8::1"), _address(V4, "192.0.2.1")
    error, network = _connect(monkeypatch, [v6, v4], {v6[3]: "hang", v4[3]: "hang"}, deadline=2.0)
    assert isinstance(error, NetworkError) and network.clock.now == pytest.approx(2.0)
    assert _closed_but(network, None)
    error, network = _connect(monkeypatch, [v6, v4], {v6[3]: "hang", v4[3]: "hang"}, abort_at=0.3)
    assert isinstance(error, NetworkError) and network.clock.now < 0.4
    assert _closed_but(network, None)


def test_every_address_failing_is_a_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    v6, v4 = _address(V6, "2001:db8::1"), _address(V4, "192.0.2.1")
    error, network = _connect(monkeypatch, [v6, v4], {v6[3]: "refused", v4[3]: ("fail", 0.02)})
    assert isinstance(error, NetworkError) and network.clock.now == pytest.approx(0.02)
    assert _closed_but(network, None)


def _hanging_listener() -> tuple[socket.socket, list[socket.socket]] | None:
    """A local listener whose accept queue is full, so a connect to it hangs (its SYNs are
    dropped); None where this platform completes or refuses such a connect instead, or runs out
    of descriptors first."""
    server = socket.socket()
    fillers: list[socket.socket] = []
    try:
        server.bind(("127.0.0.1", 0))
        # 1, not 0: macOS reads a backlog of 0 as its maximum, 128.
        server.listen(1)
        for _ in range(256):
            probe = socket.socket()
            fillers.append(probe)
            probe.setblocking(False)
            probe.connect_ex(server.getsockname())
            with selectors.DefaultSelector() as selector:
                selector.register(probe, selectors.EVENT_WRITE)
                if not selector.select(0.3):
                    return server, fillers
    except OSError:
        pass
    for sock in [server, *fillers]:
        sock.close()
    return None


class _Fixed(Resolver):
    def __init__(self, addresses: list[Any]) -> None:
        super().__init__(start_thread)
        self.addresses = addresses

    def resolve(self, host: str, port: int, timeout: float) -> list[Any]:
        return self.addresses


def test_a_real_connect_that_hangs_costs_the_next_address_250_ms(ingestion: Ingestion) -> None:
    hung = _hanging_listener()
    if hung is None:
        pytest.skip("this platform doesn't leave a connect to a full listener hanging")
    server, fillers = hung
    try:
        addresses = [(V4, socket.SOCK_STREAM, 6, server.getsockname()), (V4, socket.SOCK_STREAM, 6, ("127.0.0.1", ingestion.port))]
        exchange = Exchange(ingestion.url + "/v1/turns", KEY, "ua", _Fixed(addresses), lambda delay, fn: (lambda: None))
        started = time.monotonic()
        reply = exchange.post(b'{"events": []}', 5.0, Abort())
        assert reply.status == 200
        assert 0.2 <= time.monotonic() - started < 2.0
    finally:
        for sock in [server, *fillers]:
            sock.close()


def test_green_sockets_beside_a_kernel_selector_connect_one_address_after_another(monkeypatch: pytest.MonkeyPatch, ingestion: Ingestion) -> None:
    # gevent's patch_all(select=False): a kernel selector would block every greenlet, while a green
    # socket's own connect yields. Unpatched, the staggered attempts run.
    assert not _sending._green_sockets_beside_a_kernel_selector()

    class Green(socket.socket):
        pass

    Green.__module__ = "gevent._socket3"

    class Deadline(Green):
        pass

    monkeypatch.setattr(_sending, "_DeadlineSocket", Deadline)
    assert _sending._green_sockets_beside_a_kernel_selector()
    tried: list[Any] = []
    real = Exchange._connect_in_turn

    def in_turn(self: Exchange, *args: Any) -> socket.socket:
        tried.append(args[0])
        return real(self, *args)

    monkeypatch.setattr(Exchange, "_connect_in_turn", in_turn)
    exchange = Exchange(ingestion.url + "/v1/turns", KEY, "ua", Resolver(start_thread), lambda delay, fn: (lambda: None))
    assert exchange.post(b'{"events": []}', 5.0, Abort()).status == 200
    assert len(tried) == 1

    # An IPv6 socket that can't be created moves on to the IPv4 address.
    class NoV6(Green):
        def __init__(self, family: int = -1, *args: Any) -> None:
            if family == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")
            super().__init__(family, *args)

    monkeypatch.setattr(_sending, "_DeadlineSocket", NoV6)
    addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, ("::1", ingestion.port, 0, 0)), (socket.AF_INET, socket.SOCK_STREAM, 6, ("127.0.0.1", ingestion.port))]
    exchange = Exchange(ingestion.url + "/v1/turns", KEY, "ua", _Fixed(addresses), lambda delay, fn: (lambda: None))
    assert exchange.post(b'{"events": []}', 5.0, Abort()).status == 200
