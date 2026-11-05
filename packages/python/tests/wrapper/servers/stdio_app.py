"""A high-level server of the installed line, instrumented and served over stdio by its own run
method: transport_test.py runs it as a subprocess.

Its client appends each event it sends, as a JSON line, to the file named by `LSQUARE_TEST_EVENTS`,
and the line `{"marker": "run returned"}` once the run has returned. `LSQUARE_TEST_ENTRY` picks the
run: `run` (the server's own sync `run()`, the default: `anyio.run(self.run_stdio_async)` on mcp,
`anyio.run(self.run_async)` on fastmcp), `run_stdio_async` (awaited under `asyncio.run()`), or
`unwrapped` (the class's `run_stdio_async`, which `instrument()` never wraps). With
`LSQUARE_TEST_HOOK_DELAY` set, an async `before_send` sleeps that many seconds before returning the
event. Nothing here calls `shutdown()`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

# The test harness (`lines.py`) lives one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lines import FAST, high, tool  # noqa: E402
from lsquare import LSquare  # noqa: E402
from lsquare._sending import Abort, Reply  # noqa: E402
from lsquare.mcp import instrument  # noqa: E402

EVENTS = Path(os.environ["LSQUARE_TEST_EVENTS"])
_lock = threading.Lock()


def write(line: dict[str, Any]) -> None:
    with _lock, EVENTS.open("a", encoding="utf-8") as file:
        file.write(json.dumps(line) + "\n")


def transport(body: bytes, timeout: float, abort: Abort) -> Reply:
    """A fake ingestion: writes every event down, acknowledging each."""
    batch = json.loads(body.decode("utf-8"))["events"]
    for event in batch:
        write(event)
    ack = {"accepted": [event["eventId"] for event in batch], "duplicates": [], "rejected": []}
    return Reply(200, None, json.dumps(ack))


def search(query: str) -> str:
    return f"found {query}"


def main() -> None:
    options: dict[str, Any] = {}
    delay = os.environ.get("LSQUARE_TEST_HOOK_DELAY")
    if delay:
        seconds = float(delay)

        async def before_send(event: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(seconds)
            return event

        options["before_send"] = before_send
    lsquare = LSquare(api_key="lsq_test_key", endpoint="http://localhost:1", _transport=transport, **options)
    app = high("stdio-app")
    tool(app, search)
    instrument(app, lsquare)
    entry = os.environ.get("LSQUARE_TEST_ENTRY", "run")
    quiet: dict[str, Any] = {"show_banner": False} if FAST else {}
    if entry == "run":
        app.run(**quiet)
    elif entry == "run_stdio_async":
        asyncio.run(app.run_stdio_async(**quiet))
    elif entry == "unwrapped":
        asyncio.run(type(app).run_stdio_async(app, **quiet))
    else:
        raise SystemExit(f"unknown LSQUARE_TEST_ENTRY {entry!r}")
    write({"marker": "run returned"})


if __name__ == "__main__":
    main()
