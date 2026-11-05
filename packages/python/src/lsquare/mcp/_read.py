"""Reading the app's and the MCP SDKs' objects without ever raising, as read.ts does.

The MCP SDKs are never imported: their classes are recognised by name and module, and anything
needed from them is read from the modules the app has already loaded (`sys.modules`).
"""

from __future__ import annotations

import inspect
import math
import re
import sys
from collections.abc import Mapping
from types import ModuleType
from typing import Any


def attr(value: Any, name: str, default: Any = None) -> Any:
    """An attribute, or `default` when it's missing or reading it raises."""
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def item(value: Any, key: str, default: Any = None) -> Any:
    """A mapping's entry, or `default`."""
    try:
        if isinstance(value, Mapping):
            return value.get(key, default)
    except Exception:
        pass
    return default


def field(value: Any, key: str, name: str | None = None) -> Any:
    """A wire field: a mapping's `key` (mcp 2.x hands the wire dict), else a model's attribute
    `name` (mcp 1.x's types name their fields as the wire does, except `_meta`, which is `meta`)."""
    if isinstance(value, Mapping):
        return item(value, key)
    return attr(value, name or key)


def is_class(value: Any, module: str, name: str) -> bool:
    """`value`'s own class is exactly this one."""
    try:
        cls = type(value)
        return cls.__name__ == name and cls.__module__ == module
    except Exception:
        return False


def in_mro(value: Any, module: str, name: str) -> bool:
    """`value` is an instance of this class or a subclass of it."""
    try:
        return any(cls.__name__ == name and cls.__module__ == module for cls in type(value).__mro__)
    except Exception:
        return False


def module_starts(value: Any, *prefixes: str) -> bool:
    """`value`'s class comes from one of these packages."""
    try:
        module = type(value).__module__
        return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)
    except Exception:
        return False


def loaded(name: str) -> ModuleType | None:
    """A module the app has already imported, never importing it."""
    module = sys.modules.get(name)
    return module if isinstance(module, ModuleType) else None


def close(value: Any) -> None:
    """Closes a coroutine the app returned where nothing will await it, so Python doesn't warn."""
    try:
        if inspect.iscoroutine(value):
            value.close()
    except Exception:
        pass


def cancelled(error: BaseException) -> bool:
    """The loop's cancellation: asyncio's (which anyio raises on asyncio) or trio's."""
    try:
        import asyncio

        if isinstance(error, asyncio.CancelledError):
            return True
        trio = loaded("trio")
        cancelled_class = attr(trio, "Cancelled") if trio is not None else None
        return isinstance(cancelled_class, type) and isinstance(error, cancelled_class)
    except Exception:
        return False


def scope_cancelled() -> bool:
    """The running task is in a cancel scope that was cancelled, its cancellation still pending:
    the request's (mcp 1.x's responder and mcp 2.x's dispatch cancel it for the client's
    notifications/cancelled), or the server's as it shuts down. AnyIO's documented `-inf` effective
    deadline, read from AnyIO, which the MCP SDKs run on, never imported here. Never raises."""
    deadline = attr(loaded("anyio"), "current_effective_deadline")
    try:
        return callable(deadline) and deadline() == -math.inf
    except Exception:
        return False


def wire(value: Any, key: str) -> Any:
    """A wire field of a dict or of an object whose attribute may be named otherwise: mcp 2.x's
    types are snake_case with camelCase aliases, and fastmcp's `ToolResult` is snake_case alone."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        return item(value, key)
    snake = _SNAKE.sub("_", key).lower()
    try:
        fields = getattr(type(value), "model_fields", None)
        if isinstance(fields, dict):
            for name, info in fields.items():
                if name == key or getattr(info, "alias", None) == key or getattr(info, "serialization_alias", None) == key:
                    return attr(value, name)
            return attr(value, snake) if snake in fields else None
    except Exception:
        return None
    found = attr(value, key, _ABSENT)
    return found if found is not _ABSENT else attr(value, snake)


_SNAKE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ABSENT = object()
