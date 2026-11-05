"""The runtime: its name for `sdk.runtime`, Pyodide, and the default logger (spec section 3)."""

from __future__ import annotations

import os
import sys
from typing import Any


def runtime_name() -> str:
    """`python/3.12` on CPython, `<implementation>/<major.minor>` elsewhere (`pypy/3.11`)."""
    implementation = getattr(sys, "implementation", None)
    name = getattr(implementation, "name", "python")
    version = f"{sys.version_info[0]}.{sys.version_info[1]}"
    return f"python/{version}" if name == "cpython" else f"{name}/{version}"


def is_pyodide() -> bool:
    """Cloudflare's Python Workers and other Pyodide hosts: no threads or sockets."""
    return sys.platform == "emscripten"


class StderrLogger:
    """Writes `info` and `warn` lines to standard error, never stdout, so a stdio server's MCP
    messages stay clean (shared spec, "logs on stdio servers"). Never calls back into the SDK."""

    def info(self, message: str) -> None:
        _write(message)

    def warn(self, message: str) -> None:
        _write(message)


def _write(message: str) -> None:
    """Straight to stderr's file descriptor, past Python's buffered stream and its lock: a line
    still being written as the interpreter finalizes can't abort it. A replaced `sys.stderr`
    without a descriptor (a test's capture, say) gets an ordinary write."""
    try:
        stream = sys.stderr
        if stream is None:
            return
        line = f"{message}\n"
        try:
            fd = stream.fileno()
        except Exception:
            stream.write(line)
            stream.flush()
            return
        data = line.encode(getattr(stream, "encoding", None) or "utf-8", getattr(stream, "errors", None) or "backslashreplace")
        while data:
            data = data[os.write(fd, data):]
    except Exception:
        pass


def logger_method(logger: Any, level: str) -> Any:
    """The logger's method for a level: `info`, and `warn` or `warning` (a `logging.Logger`)."""
    if level == "warn":
        method = getattr(logger, "warn", None) if not _is_logging_logger(logger) else None
        return method if callable(method) else getattr(logger, "warning", None)
    return getattr(logger, "info", None)


def _is_logging_logger(logger: Any) -> bool:
    # `logging.Logger.warn` is deprecated and warns when called: use `warning` there.
    try:
        import logging

        return isinstance(logger, (logging.Logger, logging.LoggerAdapter))
    except Exception:
        return False
