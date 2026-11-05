"""AWS Lambda through Mangum, on each line (Python spec, section 3, "Serverless"): a stateless
server driven with API Gateway HTTP API events built by hand, invoked warm, one invocation after
another on one thread, as the Python runtime does.

Mangum runs the ASGI lifespan around every invocation. A session manager runs only once, and a
client shut down in a lifespan stays shut down, so the FastAPI recipe can't be reused: the
REFERENCE.md's Lambda recipe (`hosts.lambda_handler`) gives each invocation a new session manager and
never shuts the client down, and `flush_before_reply=True` sends each call's event before its
reply."""

from __future__ import annotations

import time
from typing import Any

import pytest

pytest.importorskip("mangum")
pytest.importorskip("fastapi")

from helpers import Recorder
from lines import FAST, needs_line, tool

from frameworks.hosts import (
    BASE,
    ENDPOINT,
    LambdaRuntime,
    api_gateway,
    calling,
    client,
    counting_flushes,
    fastapi_app,
    issued,
    lambda_handler,
    lambda_result,
    orders,
    properties,
    request,
    texts,
    until,
)
from lsquare.mcp import instrument

pytestmark = [needs_line]
LAMBDA = "This looks like AWS Lambda"


@pytest.fixture(autouse=True)
def on_lambda(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "orders")


def warnings(recorder: Recorder, lsquare: Any, text: str) -> int:
    """How many logged lines contain `text`, once the client's callback thread has caught up."""
    lsquare._lsquare_internal().warn_once("test-marker", "[test] marker")
    assert recorder.wait_for(lambda: any(line == "[test] marker" for _, line in recorder.lines))
    return sum(text in line for _, line in recorder.lines)


def test_the_recipe_survives_warm_invocations_and_sends_each_event_before_its_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    lsquare, ingestion = client(logger=recorder)
    flushes = counting_flushes(lsquare, monkeypatch)
    seen: list[dict[str, Any]] = []

    def search(query: str) -> str:
        seen.append({"query": query})
        return f"found {query}"

    runtime = LambdaRuntime(lambda: lambda_handler(lsquare, fn=search, flush_before_reply=True))
    try:
        listed = lambda_result(runtime(api_gateway(request("tools/list"))))
        first = lambda_result(runtime(api_gateway(calling({"query": "a", "intent": "Find late orders"}))))
        # Each invocation returned only once its call's event was acknowledged.
        assert ingestion.answered() == [True]
        task = issued(texts(first))
        assert task is not None, texts(first)
        second = lambda_result(runtime(api_gateway(calling({"query": "b", "session_id": task}, id_=2))))
        assert ingestion.answered() == [True, True]
    finally:
        runtime.close()
    assert properties(listed, "search") == ["query", "session_id", "intent"]
    assert "get_more_tools" in [each["name"] for each in listed["tools"]]
    assert texts(first)[0] == "found a" and texts(second) == ["found b"]
    assert seen == [{"query": "a"}, {"query": "b"}]
    a, b = ingestion.events()
    assert (a["input"], a["intent"], a["sessionId"], a["sessionSource"]) == ({"query": "a"}, "Find late orders", task, "issued")
    assert (b["input"], b["sessionId"], b["sessionSource"]) == ({"query": "b"}, task, "sent")
    assert a["protocolVersion"] == "2025-03-26" and "client" not in a
    # Nothing flushed or shut the client down: the reply waits did it all.
    assert flushes == []
    assert warnings(recorder, lsquare, LAMBDA) == 0


def test_reusing_the_fastapi_recipe_breaks_the_second_warm_invocation() -> None:
    """The control: the FastAPI recipe under Mangum. Its lifespan runs around each invocation, so
    the second either can't start its session manager again (mcp 1.x and 2.x: "can only be called
    once per instance") or runs with the client its first invocation shut down (fastmcp: the event
    is dropped)."""
    lsquare, ingestion = client()

    def init() -> Any:
        mangum: Any = __import__("mangum")
        server = orders(stateless=True)
        tool(server, lambda query: f"found {query}", name="search")
        instrument(server, lsquare)
        return mangum.Mangum(fastapi_app(server, lsquare, stateless=True))

    runtime = LambdaRuntime(init)
    outcomes: list[str] = []
    try:
        # A Host the recipe's default DNS-rebinding protection accepts, to isolate the lifespan.
        host = BASE.removeprefix("http://")
        for query in ("a", "b"):
            before = len(ingestion.events())
            try:
                out = runtime(api_gateway(calling({"query": query}), path=ENDPOINT, host=host))
            except Exception as error:  # noqa: BLE001 - Mangum's LifespanFailure, by name
                outcomes.append(f"raised {type(error).__name__}")
                continue
            sent = until(lambda: len(ingestion.events()) > before, timeout=1)  # noqa: B023 - called in this iteration
            outcomes.append(f"{out['statusCode']} {'sent' if sent else 'lost'}")
    finally:
        runtime.close()
    # The first works, its event sent by the lifespan's ashutdown.
    assert outcomes[0] == "200 sent", outcomes
    expected = "200 lost" if FAST else "raised LifespanFailure"
    assert outcomes[1] == expected, outcomes


@pytest.mark.parametrize("options", [{}, {"flush_before_reply": True}, {"wait_until": "stub"}], ids=["neither", "flush_before_reply", "wait_until"])
def test_warns_once_on_lambda_without_flush_before_reply_or_wait_until_and_not_with_either(options: dict[str, Any]) -> None:
    recorder = Recorder()
    lsquare, _ = client(logger=recorder)
    handed: list[Any] = []
    if options.get("wait_until") == "stub":
        options = {"wait_until": handed.append}
    runtime = LambdaRuntime(lambda: lambda_handler(lsquare, **options))
    try:
        for query in ("a", "b"):
            assert texts(lambda_result(runtime(api_gateway(calling({"query": query})))))[0] == f"found {query}"
    finally:
        runtime.close()
    for awaitable in handed:
        awaitable.close()
    # Once per client, though mcp 1.x's recipe instruments a server per invocation.
    assert warnings(recorder, lsquare, LAMBDA) == (0 if options else 1)
    if not options:
        [line] = [line for _, line in recorder.lines if LAMBDA in line]
        assert "pass wait_until, or flush_before_reply=True, to instrument()" in line


def test_flush_before_reply_holds_each_reply_until_its_event_is_acknowledged_before_any_teardown() -> None:
    lsquare, ingestion = client()
    runtime = LambdaRuntime(lambda: lambda_handler(lsquare, observe=ingestion, flush_before_reply=True))
    try:
        for n, query in enumerate(("a", "b"), 1):
            ingestion.hold()
            reply = runtime.invoke(api_gateway(calling({"query": query})))
            # The call's event is with the ingestion, its acknowledgement held: no reply yet.
            assert until(lambda: len(ingestion.batches) == n)  # noqa: B023 - called in this iteration
            time.sleep(0.3)
            assert not reply.done()
            assert ingestion.seen().count("replied") == n - 1
            ingestion.release()
            out = reply.result(5)
            assert texts(lambda_result(out))[0] == f"found {query}"
    finally:
        runtime.close()
    timeline = ingestion.seen()
    # Per invocation: the acknowledgement, then the reply, then (fastmcp, whose recipe keeps
    # Mangum's lifespan) the teardown. The teardown never had anything to send.
    per = ["acknowledged", "replied", "teardown"] if FAST else ["acknowledged", "replied"]
    assert timeline == per * 2, timeline


def test_flush_before_reply_replies_at_the_flush_timeout_while_the_acknowledgement_is_still_held() -> None:
    lsquare, ingestion = client(flush_timeout_ms=300)
    runtime = LambdaRuntime(lambda: lambda_handler(lsquare, observe=ingestion, flush_before_reply=True))
    try:
        ingestion.hold()
        started = time.monotonic()
        out = runtime(api_gateway(calling({"query": "slow"})))
        took = time.monotonic() - started
        assert texts(lambda_result(out))[0] == "found slow"
        # The reply went at the deadline, the event still unacknowledged.
        assert 0.25 <= took < 2.0, took
        assert ingestion.answered() == [False]
        assert "acknowledged" not in ingestion.seen()
        # Sending carries on: released, the event is acknowledged after the reply.
        ingestion.release()
        assert until(lambda: ingestion.answered() == [True])
    finally:
        runtime.close()
    timeline = ingestion.seen()
    assert timeline.index("replied") < timeline.index("acknowledged"), timeline
