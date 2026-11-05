"""The high-level servers' tool registries (Python spec, section 5): which tool a name runs, and
what its definition says of the extras' inputs, read as the registry is now, never cached.

FastMCP 1.x and MCPServer keep one registry, `_tool_manager`. fastmcp resolves a name through
providers, transforms, versions and middleware, so its registrations count only while its tool set
is known: its own local provider alone, no transform but fastmcp's own `Visibility` (which only
hides), and no injected tool of that name.

Each list, and each step of a call, reads the registry once (`Read`), as it is then: a listing asks
of every tool it lists from that one read, so it costs one pass over the registry, not one per
tool. A read is never kept: the next list or call reads the registry again.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from ._extras import INTENT, MISSING_TOOLS, SESSION_ID, Ownership
from ._ownership import fastmcp_tool_ownership, mcp_tool_ownership
from ._read import attr, in_mro, is_class

#: Which high-level class: mcp 1.x's FastMCP, mcp 2.x's MCPServer, or fastmcp 3.x / 4.x.
Kind = Literal["fastmcp1", "mcpserver", "fastmcp"]

#: How many of a fastmcp server's registrations are read, at most: past it, the set isn't known.
MAX_REGISTRATIONS = 10_000

UNKNOWN = Ownership(readable=False, why="the server's tools can't all be read")
UNREADABLE = Ownership(readable=False, why="it could not be read")


def _tool_manager_tools(high: Any) -> Mapping[str, Any] | None:
    tools = attr(attr(high, "_tool_manager"), "_tools")
    return tools if isinstance(tools, Mapping) else None


def _known(high: Any) -> bool:
    """A fastmcp server whose tool set is known: its local provider alone, no transform other
    than fastmcp's exact `Visibility`, on the server or on that provider."""
    try:
        local = attr(high, "_local_provider")
        providers = attr(high, "providers")
        if local is None or not isinstance(providers, list) or len(providers) != 1 or providers[0] is not local:
            return False
        for holder in (high, local):
            transforms = attr(holder, "_transforms")
            if not isinstance(transforms, list):
                return False
            for transform in transforms:
                if not is_class(transform, "fastmcp.server.transforms.visibility", "Visibility"):
                    return False
        return True
    except Exception:
        return False


INJECTION = "fastmcp.server.middleware.tool_injection"


def _injected(high: Any) -> set[str] | None:
    """The names fastmcp's tool-injection middleware adds, or None when they can't be read: a
    subclass of the app's own may resolve names its own way (`get_injected_tool`)."""
    names: set[str] = set()
    middleware = attr(high, "middleware")
    if not isinstance(middleware, list):
        return None
    for each in middleware:
        if not in_mro(each, INJECTION, "ToolInjectionMiddleware"):
            continue
        # fastmcp's own classes (the base, its prompt and resource tools) read their dict.
        if type(each).__module__ != INJECTION:
            return None
        by_name = attr(each, "_tools_to_inject_by_name")
        if not isinstance(by_name, Mapping):
            return None
        names.update(name for name in by_name if isinstance(name, str))
    return names


def _registrations(high: Any) -> list[Any] | None:
    """Every local tool registration of a fastmcp server, every version, or None when they can't
    all be read. A registry past the bound is refused before anything in it is copied."""
    components = attr(attr(high, "_local_provider"), "_components")
    if not isinstance(components, Mapping):
        return None
    try:
        if len(components) > MAX_REGISTRATIONS:
            return None
    except Exception:
        return None
    tools: list[Any] = []
    for count, (key, component) in enumerate(list(components.items())):
        if count >= MAX_REGISTRATIONS:
            return None
        if isinstance(key, str) and key.startswith("tool:"):
            tools.append(component)
    return tools


def _plain(name: Any) -> str | None:
    """`name` as a plain string, when it compares and hashes as one: a str subclass that keeps
    str's own `__eq__` and `__hash__` is read as its text, by str's own `__str__`. None for any
    other name, since which names it equals can't be told."""
    kind = type(name)
    if kind is str or (issubclass(kind, str) and kind.__eq__ is str.__eq__ and kind.__hash__ is str.__hash__):
        return str.__str__(name)
    return None


def _by_name(found: list[Any]) -> dict[str, list[Any]] | None:
    """The registrations by name, from one pass, each name's in registry order; None when a name
    isn't a plain string (fastmcp's own make it one), so which registrations a name runs can't be
    told."""
    by_name: dict[str, list[Any]] = {}
    for tool in found:
        name = _plain(attr(tool, "name"))
        if name is None:
            return None
        by_name.setdefault(name, []).append(tool)
    return by_name


class Read:
    """One high-level server's registry, read once, as it is now: for one listing, or for one step
    of a call (its extras' inputs taken out and its functions tapped). Never kept past it."""

    def __init__(self, high: Any, kind: Kind) -> None:
        self.kind = kind
        #: FastMCP 1.x and MCPServer: the registry itself, by name.
        self._tools: Mapping[str, Any] | None = None
        #: fastmcp: every local registration, every version; None when they can't all be read.
        self._found: list[Any] | None = None
        #: fastmcp: those registrations by name; None when a name's can't be told.
        self._by_name: dict[str, list[Any]] | None = None
        #: fastmcp: the names injected, while the tool set is known; None while it isn't.
        self._injected: set[str] | None = None
        if kind != "fastmcp":
            self._tools = _tool_manager_tools(high)
            return
        self._found = _registrations(high)
        self._by_name = _by_name(self._found) if self._found is not None else None
        if self._by_name is not None and _known(high):
            self._injected = _injected(high)

    def known(self) -> bool:
        """Whether the names registered decide what a call runs."""
        if self.kind != "fastmcp":
            return self._tools is not None
        return self._injected is not None

    def tools(self, name: str) -> list[Any] | None:
        """The registrations a call of `name` can run: none for a name not registered, None when
        that can't be told."""
        if self.kind != "fastmcp":
            if self._tools is None:
                return None
            tool = self._tools.get(name)
            return [tool] if tool is not None else []
        key = _plain(name)
        if self._by_name is None or self._injected is None or key is None or key in self._injected:
            return None
        return list(self._by_name.get(key, ()))

    def local(self, name: str) -> list[Any]:
        """The server's own registrations of `name`, known tool set or not: the functions tapped."""
        if self.kind != "fastmcp":
            return self.tools(name) or []
        key = _plain(name)
        if self._by_name is not None and key is not None:
            return list(self._by_name.get(key, ()))
        return [tool for tool in self._found or () if attr(tool, "name") == name]

    def names(self) -> list[str] | None:
        """The names registered so far, each once, in registry order: for dry run and
        get_more_tools."""
        if self.kind != "fastmcp":
            return [name for name in self._tools if isinstance(name, str)] if self._tools is not None else None
        if self._by_name is None or not self.known():
            return None
        return list(self._by_name)

    def has(self, name: str) -> bool | None:
        """`name` is a tool of the server's own (or injected): None when that can't be told."""
        if self.kind == "fastmcp":
            if self._injected is None:
                return None
            if name in self._injected:
                return True
        tools = self.tools(name)
        return None if tools is None else len(tools) > 0

    def ownership(self, name: str) -> Ownership | None:
        """What the definitions of `name` say of the extras' inputs; None for a name the server
        hasn't registered. Every registration must be readable and agree."""
        tools = self.tools(name)
        if tools is None:
            return UNKNOWN
        if not tools:
            return None
        read = [self._one(tool) for tool in tools]
        for one in read:
            if not one.readable:
                return one
        first = read[0]
        for other in read[1:]:
            if any((extra in first.declared) != (extra in other.declared) for extra in (SESSION_ID, INTENT)):
                return Ownership(readable=False, why="its registered versions don't agree on their inputs")
        return first

    def _one(self, tool: Any) -> Ownership:
        try:
            if self.kind == "fastmcp":
                return fastmcp_tool_ownership(tool)
            return mcp_tool_ownership(tool)
        except Exception:
            return UNREADABLE

    def offers_missing_tools(self) -> bool:
        """get_more_tools may be answered and listed: the tool set is known, has a tool of its
        own, and none of that name. Whether one of them is enabled is the caller's to check
        (fastmcp's `visible()`)."""
        names = self.names()
        if not names or not self.known():
            return False
        return self.has(MISSING_TOOLS) is False

    def owns_missing_tools(self) -> bool:
        return self.has(MISSING_TOOLS) is True


class Registry:
    """One high-level server's registry, read as it is now at every list and call: each of its
    questions reads it again, and `read()` reads it once for many."""

    def __init__(self, high: Any, kind: Kind) -> None:
        self.high = high
        self.kind = kind

    def read(self) -> Read:
        """The registry as it is now, read once: a listing asks of all its tools from one read."""
        return Read(self.high, self.kind)

    def known(self) -> bool:
        return self.read().known()

    def tools(self, name: str) -> list[Any] | None:
        return self.read().tools(name)

    def local(self, name: str) -> list[Any]:
        return self.read().local(name)

    def names(self) -> list[str] | None:
        return self.read().names()

    def has(self, name: str) -> bool | None:
        return self.read().has(name)

    def ownership(self, name: str) -> Ownership | None:
        return self.read().ownership(name)

    def offers_missing_tools(self) -> bool:
        return self.read().offers_missing_tools()

    def owns_missing_tools(self) -> bool:
        return self.read().owns_missing_tools()

    async def visible(self) -> bool:
        """The server shows a tool of its own now: on fastmcp, one its transforms (enable,
        disable) and filters leave visible, read as fastmcp lists them, without its middleware.
        FastMCP 1.x and MCPServer can't disable a tool."""
        if self.kind != "fastmcp":
            return True
        try:
            tools = await self.high.list_tools(run_middleware=False)
            return any(attr(tool, "name") != MISSING_TOOLS for tool in tools)
        except Exception:
            return False
