"""What the wrapper keeps for an object of the app's or of an MCP SDK's (a read stream, a session, a
connection, a client's bound method) for as long as it lives, through weak references with no
callback.

The standard library's weak containers (`WeakSet`, `WeakKeyDictionary`, `WeakMethod`) run a
callback of their own as what they hold is collected. A signal handler's `SystemExit` or
`KeyboardInterrupt` that arrives then is raised inside that callback, where the interpreter only
reports it, and the process goes on. Nothing here runs as anything is collected: a reference whose
object is gone stays until a later addition prunes it.
"""

from __future__ import annotations

import _weakref
import types
import weakref
from collections.abc import Callable, MutableMapping
from typing import Any, Generic, TypeVar

V = TypeVar("V")

#: Deletes a dict's entry only if it is a dead weak reference, in one step: another thread or a
#: signal handler that stored a live one under that key meanwhile keeps it, and no reader sees it
#: gone. The standard library's own `weakref` imports it, so every Python whose `weakref` works has it.
_remove_dead: Callable[[MutableMapping[int, Any], int], None] = getattr(_weakref, "_remove_dead_weakref")

#: A table prunes its dead references once it holds more than twice the entries alive at its last
#: pruning, and at least this many: adding costs constant time on average, and what was collected
#: leaves a few entries at most until then.
PRUNED_AT = 16
#: How many times keeping a value for an object retries, where each try found a collected
#: object's reference under the key it reuses, and dropped it.
_TRIES = 4


class _Kept(weakref.ref[Any]):
    """A weak reference with no callback, and the value kept for its object."""

    __slots__ = ("value",)

    def __new__(cls, held: Any, value: Any) -> _Kept:
        return super().__new__(cls, held)

    def __init__(self, held: Any, value: Any) -> None:
        # weakref.ref's own only checks its arguments: those its __new__ was given are another's.
        self.value = value


def _drop_dead(table: dict[int, _Kept], key: int) -> None:
    _remove_dead(table, key)


class ByIdentity(Generic[V]):
    """A value kept for each object, by its identity, for as long as it lives. Never by the
    object's `__eq__` or `__hash__`, which are its own code: keyed by `id()`, which a collected
    object's successor may reuse, so a reference counts only for the object it still refers to.
    Each step is one operation on a dict keyed by ints, which runs no Python code: another thread,
    or a signal handler on this one, changing the table meanwhile breaks nothing. An object that
    can't be weakly referenced raises TypeError when a value is kept for it."""

    __slots__ = ("_table", "_limit")

    def __init__(self) -> None:
        self._table: dict[int, _Kept] = {}
        self._limit = PRUNED_AT

    def get(self, held: object) -> V | None:
        kept = self._table.get(id(held))
        if kept is not None and kept() is held:
            value: V = kept.value
            return value
        return None

    def setdefault(self, held: object, value: V) -> V:
        """What is kept for `held`: `value`, unless a value already was."""
        key = id(held)
        new = _Kept(held, value)
        for _ in range(_TRIES):
            kept = self._table.setdefault(key, new)
            if kept is new:
                self._prune()
                return value
            if kept() is held:
                known: V = kept.value
                return known
            # A collected object's, under the id `held` now has.
            _drop_dead(self._table, key)
        return value

    def __len__(self) -> int:
        """The references held, dead ones not yet pruned included."""
        return len(self._table)

    def _prune(self) -> None:
        table = self._table
        if len(table) <= self._limit:
            return
        # The keys in one copy, each dropped only if its reference is dead.
        for key in list(table):
            _drop_dead(table, key)
        self._limit = max(PRUNED_AT, 2 * len(table))


class IdentitySet:
    """The objects added, by identity, for as long as each lives (see ByIdentity)."""

    __slots__ = ("_kept",)

    def __init__(self) -> None:
        self._kept: ByIdentity[bool] = ByIdentity()

    def add(self, held: object) -> None:
        self._kept.setdefault(held, True)

    def __contains__(self, held: object) -> bool:
        return self._kept.get(held) is True

    def __len__(self) -> int:
        return len(self._kept)


def method(calls: Callable[..., Any]) -> Callable[[], Callable[..., Any] | None]:
    """`calls` for as long as what it's made of lives: a bound method (a client's own), through
    weak references with no callback to its instance and its function, bound again from them while
    both live (`weakref.WeakMethod`'s callback runs as either is collected). Anything else is held
    as it is."""
    try:
        instance = weakref.ref(getattr(calls, "__self__"))
        function = weakref.ref(getattr(calls, "__func__"))
    except (AttributeError, TypeError):
        return lambda: calls

    def bound() -> Callable[..., Any] | None:
        one, two = instance(), function()
        if one is None or two is None:
            return None
        return types.MethodType(two, one)

    return bound
