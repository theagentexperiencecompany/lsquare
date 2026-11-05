"""LSquare for Python: record every tool call of an MCP server, and the agent turns behind them.

    from lsquare import LSquare
    lsquare = LSquare(api_key=os.environ.get("LSQUARE_KEY"))

Specification: sdk-0.2-python-spec.md.
"""

from ._client import FlushResult, LSquare, LSquareError, Signal
from ._version import __version__

__all__ = ["FlushResult", "LSquare", "LSquareError", "Signal", "__version__"]
