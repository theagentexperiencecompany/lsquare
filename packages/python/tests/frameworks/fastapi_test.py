"""FastAPI mounting, on each line (Python spec, sections 3 and 4): the line's streamable-HTTP app
mounted under a path, its session manager run from the FastAPI app's lifespan, and
`await lsquare.ashutdown()` in that lifespan's shutdown, as REFERENCE.md's recipe has it. Driven
in process through httpx's ASGI transport, the lifespan run as a server runs it."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from lines import needs_line, tool

from frameworks.hosts import (
    ENDPOINT,
    Lifespan,
    calling,
    client,
    counting_flushes,
    fastapi_app,
    in_process,
    issued,
    orders,
    properties,
    request,
    texts,
    until_async,
)
from lsquare.mcp import instrument

pytestmark = [needs_line, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize("stateless", [False, True], ids=["stateful", "stateless"])
async def test_a_mounted_server_records_each_call_and_the_lifespans_ashutdown_sends_them_before_it_ends(
    stateless: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    lsquare, ingestion = client()
    flushes = counting_flushes(lsquare, monkeypatch)
    seen: list[dict[str, Any]] = []

    def search(query: str) -> str:
        seen.append({"query": query})
        return f"found {query}"

    server = orders(stateless)
    tool(server, search)
    instrument(server, lsquare)
    api = fastapi_app(server, lsquare, stateless)
    async with Lifespan(api, ingestion) as lifespan, in_process(api, ENDPOINT) as http:
        if not stateless:
            await http.handshake()
        listed = await http.post(request("tools/list"))
        assert listed.status == 200, listed
        # The extras: two optional inputs on the tool, and the wrapper's own tool.
        result = listed.result()
        assert properties(result, "search") == ["query", "session_id", "intent"]
        assert "get_more_tools" in [each["name"] for each in result["tools"]]
        # Sending is held from here: nothing acknowledges an event until the test says so.
        ingestion.hold()
        first = await http.post(calling({"query": "a", "intent": "Find late orders"}))
        task = issued(texts(first.result()))
        second = await http.post(calling({"query": "b", **({"session_id": task} if task else {})}, id_=2))
        await http.close()
        # The replies didn't wait: both events are with the ingestion, unacknowledged.
        assert await until_async(lambda: len(ingestion.events()) == 2)
        assert ingestion.answered() and not any(ingestion.answered())
        # The lifespan's shutdown waits for them (ashutdown), and ends only once they're acknowledged.
        ending = asyncio.ensure_future(lifespan.shutdown())
        await asyncio.sleep(0.3)
        assert not ending.done()
        assert "lifespan ended" not in ingestion.seen()
        ingestion.release()
        await asyncio.wait_for(ending, 5)
    # Acknowledged before the lifespan ended, and nothing but the lifespan's ashutdown flushed.
    timeline = ingestion.seen()
    assert timeline.count("acknowledged") >= 1 and timeline[-1] == "lifespan ended", timeline
    assert all(ingestion.answered())
    assert flushes == ["ashutdown"]
    # The tool got its own arguments: the extras' inputs were taken out before it ran.
    assert seen == [{"query": "a"}, {"query": "b"}]
    assert texts(first.result())[0] == "found a" and texts(second.result())[0] == "found b"
    events = {event["input"]["query"]: event for event in ingestion.events()}
    assert sorted(events) == ["a", "b"]
    assert events["a"]["input"] == {"query": "a"} and events["a"]["intent"] == "Find late orders"
    assert all(event["toolName"] == "search" and event["status"] == "completed" for event in events.values())
    if stateless:
        # No protocol session: the first result carried a task ID, which the second call sent back.
        assert task is not None
        assert (events["a"]["sessionId"], events["a"]["sessionSource"]) == (task, "issued")
        assert (events["b"]["sessionId"], events["b"]["sessionSource"]) == (task, "sent")
        assert issued(texts(second.result())) is None
    else:
        # Grouped by the session the server issued: no ID line.
        assert task is None and http.session is not None
        assert {(event["sessionId"], event["sessionSource"]) for event in events.values()} == {(hashlib.sha256(http.session.encode()).hexdigest(), "protocol")}
        assert all(event["client"] == {"name": "claude-code", "version": "2.1"} for event in events.values())
