"""How often the wrapper reads a high-level server's registry (Python spec, section 5): once for a
listing, once for a call and once for dry run, however many tools it holds, and a call keeps none
of what it read while its tool runs. Work is counted, never timed: fastmcp's components are swapped
for a dict that counts every pass over them, whichever helper makes it."""

from __future__ import annotations

import asyncio
import weakref
from types import SimpleNamespace
from typing import Any

import pytest

from helpers import Recorder, acollected
from lines import client, connect, high, only, schema, tool
from lsquare import LSquare
from lsquare.mcp import instrument

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Counting(dict[str, Any]):
    """fastmcp's components, counting the passes over them."""

    passes = 0

    def items(self) -> Any:
        self.passes += 1
        return super().items()

    def values(self) -> Any:
        self.passes += 1
        return super().values()

    def keys(self) -> Any:
        self.passes += 1
        return super().keys()

    def __iter__(self) -> Any:
        self.passes += 1
        return super().__iter__()


def server(count: int) -> tuple[Any, Counting]:
    """A fastmcp server of `count` tools, and its counted components."""
    app = high()
    for index in range(count):

        def numbered(x: int) -> int:
            return x

        tool(app, numbered, name=f"tool_{index}")
    components = Counting(app._local_provider._components)
    app._local_provider._components = components
    return app, components


@only("fastmcp3", "fastmcp4")
async def test_a_listing_and_a_call_each_read_the_registry_once_whatever_its_size() -> None:
    async def passes(count: int, instrumented: bool) -> tuple[int, int]:
        lsquare, _ = client()
        app, components = server(count)
        if instrumented:
            instrument(app, lsquare)
        async with connect(app) as connection:
            components.passes = 0
            listed = await connection.tools()
            listing = components.passes
            components.passes = 0
            await connection.call("tool_0", {"x": 1})
        if instrumented:
            # Every tool still gets the extras, and get_more_tools is added.
            assert len(listed) == count + 1 and all("session_id" in (schema(item).get("properties") or {}) for item in listed)
        return listing, components.passes

    for count in (3, 300):
        plain, wrapped = await passes(count, False), await passes(count, True)
        # fastmcp's own passes, and one of the wrapper's for each.
        assert (wrapped[0] - plain[0], wrapped[1] - plain[1]) == (1, 1), count


@only("fastmcp3", "fastmcp4")
async def test_a_call_keeps_nothing_it_read_of_the_registry_while_its_tool_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from lsquare.mcp import _registry

    reads: list[weakref.ref[_registry.Read]] = []
    read = _registry.Registry.read

    def watched(self: _registry.Registry) -> _registry.Read:
        taken = read(self)
        reads.append(weakref.ref(taken))
        return taken

    monkeypatch.setattr(_registry.Registry, "read", watched)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(x: int) -> int:
        entered.set()
        await release.wait()
        return x

    lsquare, _ = client()
    app, _ = server(300)
    tool(app, held)
    instrument(app, lsquare)
    async with connect(app) as connection:
        reads.clear()
        calling = asyncio.create_task(connection.call("held", {"x": 1}))
        await asyncio.wait_for(entered.wait(), 5)
        # While the tool is suspended: a pending call must not hold every registration.
        taken = len(reads)
        gone = await acollected(*reads)
        release.set()
        await calling
    assert taken > 0 and gone


@only("fastmcp3", "fastmcp4")
async def test_dry_run_reads_the_registry_once_whatever_its_size() -> None:
    for count in (3, 300):
        recorder = Recorder()
        app, components = server(count)
        instrument(app, LSquare(dry_run=True, logger=recorder))
        assert components.passes == 1, count
        said = f"{count} of the {count} tools registered so far"
        assert recorder.wait_for(lambda: any(said in line for _, line in recorder.lines))


def fake_fastmcp(components: dict[str, Any]) -> Any:
    """A stand-in for a fastmcp server whose tool set is known: its local provider alone."""
    local = SimpleNamespace(_components=components, _transforms=[])
    return SimpleNamespace(_local_provider=local, providers=[local], middleware=[], _transforms=[])


def test_a_fastmcp_registry_past_the_bound_is_refused_before_it_is_copied() -> None:
    from lsquare.mcp import _registry

    class Huge(dict[str, Any]):
        def __len__(self) -> int:
            return _registry.MAX_REGISTRATIONS + 1

        def items(self) -> Any:
            raise AssertionError("copied")

    registry = _registry.Registry(fake_fastmcp(Huge()), "fastmcp")
    assert _registry._registrations(registry.high) is None
    assert registry.known() is False and registry.local("search") == []


def test_names_keeps_each_name_once_in_registration_order() -> None:
    from lsquare.mcp._registry import Registry

    components = {f"tool:{name}@{version}": SimpleNamespace(name=name) for name, version in (("b", "1"), ("a", ""), ("b", "2"), ("c", ""))}
    registry = Registry(fake_fastmcp(components), "fastmcp")
    assert registry.names() == ["b", "a", "c"]
    assert [len(registry.tools(name) or []) for name in ("a", "b", "c", "d")] == [1, 2, 1, 0]
