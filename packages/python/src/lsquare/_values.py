"""Python values as JavaScript holds them (Python spec, section 2, "Values").

Python only: TypeScript gets its strings, numbers and objects from JavaScript itself. Each rule here
is applied as a walk visits a value, inside the walk's limits, never as a conversion beforehand:

- strings are read as JavaScript reads them, with each valid surrogate pair one character;
- numbers are JavaScript numbers: an int beyond 2**53 - 1 is the nearest double, and one too large
  for a double is infinite, as JavaScript's parser would make it;
- mappings are read in JavaScript's key order, within a bound on how many keys are enumerated;
- pydantic models and dataclasses are objects, field by field; UUIDs, enums, aware datetimes and
  dates stand for their JSON; bytes, like a Buffer in TypeScript, and everything else aren't JSON.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import itertools
import math
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from typing import Any, Final, Literal, Union


class _Missing(enum.Enum):
    MISSING = 0


class _NotJson(enum.Enum):
    NOT_JSON = 0


#: JavaScript's `undefined`: a field or entry that isn't there, as opposed to one that is None (null).
MISSING: Final = _Missing.MISSING
Missing = Literal[_Missing.MISSING]
#: A value that isn't JSON at all (shape.ts's NOT_JSON).
NOT_JSON: Final = _NotJson.NOT_JSON
NotJson = Literal[_NotJson.NOT_JSON]

MAX_SAFE_INTEGER: Final = 2**53 - 1
#: The largest array index: JavaScript reads the keys "0" to "4294967294" first, ascending.
MAX_ARRAY_INDEX: Final = 2**32 - 2

_SURROGATE = re.compile(r"[\ud800-\udfff]")
_ASTRAL = re.compile(r"[\U00010000-\U0010ffff]")
_CHUNK = 1 << 20


class _Unreadable:
    """A value from the app that can't be read as what it says it is (`readable`)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNREADABLE"


#: What `readable` gives for such a value: an object JSON doesn't have, so its field is
#: `wrong_type`, `not_json` or an invalid ID, as for any other object.
UNREADABLE: Final = _Unreadable()
#: Types this SDK reads through their own methods (`str.__str__`, `int.__int__`, `float.__float__`),
#: which an object that only says it is one, through its `__class__`, doesn't have. `bool` is one.
_READ_BY_TYPE: Final = (str, int, float)


def readable(value: object) -> object:
    """The value, or UNREADABLE when its class can't be read as it says. Every type check reads an
    object's `__class__`, which a lazy proxy (Django's `SimpleLazyObject`, `wrapt`'s proxies) takes
    from what it wraps, running its factory, which can raise; and a proxy passing for a string, a
    number or a boolean isn't one to read. So such a value is one field left out, as any other
    object would be. A proxy of a list or a mapping is still read through it."""
    try:
        claimed = value.__class__
        if claimed is type(value) or not (isinstance(claimed, type) and issubclass(claimed, _READ_BY_TYPE)):
            return value
    except Exception:
        pass
    return UNREADABLE


def text(value: str) -> str:
    """The string itself, as a plain `str`: a subclass (a str enum, say) gives its characters."""
    return value if type(value) is str else str.__str__(value)


def join(value: str) -> str:
    """The string as JavaScript reads it: each valid surrogate pair one character, lone ones kept.

    Python can hold a pair as two code points (`"\\ud83d" + "\\ude00"` has length 2); JavaScript
    can't tell them from the character they make. Once joined, Python's code points are what
    JavaScript counts as code points, so counting, cutting and writing agree.
    """
    value = text(value)
    if value.isascii() or _SURROGATE.search(value) is None:
        return value
    return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


def joined_prefix(value: str, count: int) -> tuple[str, bool]:
    """At least the first `count` characters of `value`, joined, and whether that's all of it.

    Reads at most `2 * count + 2` of it, so a huge string costs only what is read: a character
    JavaScript sees is one or two of Python's code points.
    """
    value = text(value)
    count = max(0, count)
    if len(value) <= 2 * count + 2:
        return join(value), True
    part = join(value[: 2 * count + 2])
    # Its last character may be the first half of a pair whose second half wasn't read.
    if 0xD800 <= ord(part[-1]) <= 0xDBFF:
        part = part[:-1]
    return part, False


def _split(match: re.Match[str]) -> str:
    point = ord(match.group(0)) - 0x10000
    return chr(0xD800 | (point >> 10)) + chr(0xDC00 | (point & 0x3FF))


def to_units(value: str) -> str:
    """The string as UTF-16 code units, one code point each: a regex then reads it as JavaScript's do.

    JavaScript's regexes without the `u` flag match code units, so a bounded repeat such as `{0,256}`
    counts an emoji as two, and match offsets are UTF-16 indexes. `join` turns the result back.
    """
    if value.isascii() or _ASTRAL.search(value) is None:
        return value
    return _ASTRAL.sub(_split, value)


def utf16_length(value: str) -> int:
    """JavaScript's `length`: UTF-16 code units, whether pairs are joined or not."""
    if value.isascii():
        return len(value)
    if _ASTRAL.search(value) is None:
        return len(value)
    total = 0
    # In parts, so a huge string doesn't need a copy of twice its size.
    for start in range(0, len(value), _CHUNK):
        total += len(value[start : start + _CHUNK].encode("utf-16-le", "surrogatepass")) // 2
    return total


def utf8_length(value: str) -> int:
    """UTF-8 bytes as JavaScript's TextEncoder counts them: a lone surrogate is U+FFFD, 3 bytes."""
    return len(join(value).encode("utf-8", "surrogatepass"))


def is_array_index(key: str) -> bool:
    """Whether JavaScript reads this key first: "0" or a digit string without a leading zero, at most 4294967294."""
    length = len(key)
    if length == 0 or length > 10:
        return False
    first = key[0]
    if first == "0":
        return length == 1
    if not ("1" <= first <= "9") or not key.isascii() or not key.isdigit():
        return False
    return int(key) <= MAX_ARRAY_INDEX


def js_order(keys: list[str]) -> list[str]:
    """Keys in JavaScript's order: array indexes ascending, then the others in the order given."""
    indexes: list[str] = []
    names: list[str] = []
    for key in keys:
        if "0" <= key[:1] <= "9" and is_array_index(key):
            indexes.append(key)
        else:
            names.append(key)
    if not indexes:
        return names
    indexes.sort(key=int)
    return indexes + names


class Keys:
    """A mapping's keys in JavaScript's order, read once: string keys as the mapping holds them
    (to read their values by), and how many keys weren't strings, which JSON can't have."""

    __slots__ = ("names", "others", "total", "merged")

    def __init__(self, names: list[Any], others: int, total: int) -> None:
        self.names = names
        self.others = others
        self.total = total
        #: `names`, one per key JavaScript holds, once worked out (`_jsjson.json_length`).
        self.merged: list[Any] | None = None


def object_keys(source: Mapping[Any, Any], limit: int) -> Keys | None:
    """The keys of `source` in JavaScript's order, or None when it has more than `limit`.

    JavaScript reads array-index keys first, wherever they were added, so every key has to be
    enumerated before the first one is known. The bound keeps a huge or endless mapping from
    costing unbounded time or memory. `total` counts every entry enumerated, a model's fields left
    out included. May raise what the mapping's own iteration raises.
    """
    # A model's or a dataclass's fields are enumerated with every entry reached, those it leaves
    # out included: each counts against the bound.
    # By its type, not `isinstance`: an app's mapping whose `__class__` raises isn't asked.
    entries = source.scan() if type(source) is _Fields else iter(source)
    listed = list(itertools.islice(entries, max(0, limit) + 1))
    if len(listed) > limit:
        return None
    indexes: list[Any] = []
    names: list[Any] = []
    others = 0
    for key in listed:
        if key is SKIPPED:
            continue
        # By its type, not its `__class__`: a key that only says it is a string (a proxy) isn't one.
        if issubclass(type(key), str):
            if "0" <= key[:1] <= "9" and is_array_index(text(key)):
                indexes.append(key)
            else:
                names.append(key)
        else:
            others += 1
    if indexes:
        indexes.sort(key=lambda key: int(text(key)))
        names = indexes + names
    return Keys(names, others, len(listed))


def js_number(value: object) -> int | float | None:
    """A Python number as JavaScript holds it, or None for anything else (`bool` included).

    Exact within +-(2**53 - 1); beyond, the nearest double, as JavaScript's parser rounds a long
    integer. One too large for a double is infinite, as JavaScript's parser makes it.
    """
    # By its type, not its `__class__`: a proxy passing for a number isn't one to read.
    kind = type(value)
    if kind is bool:
        return None
    if issubclass(kind, int):
        number = int.__int__(value)  # type: ignore[arg-type]
        if -MAX_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER:
            return number
        try:
            return float(number)
        except OverflowError:
            return math.inf if number > 0 else -math.inf
    if issubclass(kind, float):
        return float.__float__(value)  # type: ignore[arg-type]
    return None


def is_finite(number: int | float) -> bool:
    return isinstance(number, int) or math.isfinite(number)


def is_safe_integer(number: int | float) -> bool:
    """JavaScript's `Number.isSafeInteger`."""
    if isinstance(number, int):
        return -MAX_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER
    return math.isfinite(number) and number.is_integer() and abs(number) <= MAX_SAFE_INTEGER


BINARY_TYPES: Final = (bytes, bytearray, memoryview)


def binary_length(value: bytes | bytearray | memoryview) -> int | None:
    """The size of binary data, or None when it can't be read (a released memoryview)."""
    try:
        return value.nbytes if isinstance(value, memoryview) else len(value)
    except Exception:
        return None


def _pydantic_fields(value: object) -> dict[str, Any] | None:
    """A pydantic v2 model's fields, recognised by their shape: pydantic itself is never imported."""
    if isinstance(value, type):
        return None
    cls = type(value)
    try:
        fields = getattr(cls, "model_fields", None)
        if not isinstance(fields, dict) or not callable(getattr(cls, "model_dump", None)):
            return None
        if not hasattr(value, "__pydantic_fields_set__"):
            return None
    except Exception:
        return None
    return fields


def _is_root_model(value: object) -> bool:
    try:
        return _pydantic_fields(value) is not None and getattr(type(value), "__pydantic_root_model__", False) is True
    except Exception:
        return False


class _Skipped(enum.Enum):
    SKIPPED = 0


#: An entry `_Fields.scan` reached and left out: a field marked `exclude`, a dataclass's ClassVar or
#: InitVar, an MCP SDK type's field holding None, a key written twice. Counted against the walk's
#: bound on keys as any other, so however many there are, they cost no more than it.
SKIPPED: Final = _Skipped.SKIPPED

#: Each field a class declares, as it is reached: its key and its attribute, or SKIPPED.
_Pair = Union[tuple[str, str], _Skipped]


class _Declared:
    """The fields a class declares, as its objects' visits reach them: each worked out once, however
    many visits read the object, and only as far as one of them has needed. Shared by every visit's
    `_Fields` (`_Fields.again`), and in one pass (`Listed`) by every object whose fields are declared
    in the same table, a model class's `model_fields` or a dataclass's `__dataclass_fields__`: what's
    kept is the class's, never an object's."""

    __slots__ = ("_make", "_pending", "reached", "by_key", "complete")

    def __init__(self, make: Callable[[], Iterator[_Pair]]) -> None:
        self._make = make
        self._pending: Iterator[_Pair] | None = None
        #: Each field reached so far, in the class's order.
        self.reached: list[_Pair] = []
        #: Each key reached so far, to the attributes written under it, in the class's order.
        self.by_key: dict[str, list[str]] = {}
        #: Every field has been reached.
        self.complete = False

    def keyed(self) -> dict[str, list[str]]:
        """Each key, to the attributes written under it: every field reached first."""
        while not self.complete and self.reach():
            pass
        return self.by_key

    def reach(self) -> bool:
        """Reaches the next field; False when there are no more. A read of the class's metadata that
        raises raises, and the next starts again from the first field, passing over those already
        reached: a generator that raised is finished, and reading on from it would end the fields
        there. A field is reached once it is indexed by its key too: one whose key can't be (a str
        subclass whose hash raises) isn't, and the next read gives it again."""
        try:
            pending = self._pending
            if pending is None:
                pending = self._make()
                for _ in range(len(self.reached)):
                    next(pending)
                self._pending = pending
            pair = next(pending)
        except StopIteration:
            self.complete = True
            self._pending = None
            return False
        except BaseException:
            self._pending = None
            raise
        if pair is not SKIPPED:
            key, attribute = pair
            try:
                # A failed read or write leaves the index as it was.
                attributes = self.by_key.get(key)
                if attributes is None:
                    self.by_key[key] = [attribute]
                else:
                    attributes.append(attribute)
            except BaseException:
                self._pending = None
                raise
        self.reached.append(pair)
        return True


#: The fields one pass has worked out, by the table they're declared in (`_shared`): the table, held
#: so its id isn't reused, and what it declares.
Declarations = dict[int, tuple[object, _Declared]]


def _shared(table: object, make: Callable[[], Iterator[_Pair]], shared: Declarations | None) -> _Declared:
    """The fields `table` declares, worked out once in a pass for every object they're declared for
    (`shared`, a `Listed`'s), or for this one object only, outside one."""
    if shared is None:
        return _Declared(make)
    known = shared.get(id(table))
    if known is not None and known[0] is table:
        return known[1]
    declared = _Declared(make)
    shared[id(table)] = (table, declared)
    return declared


class _Fields(Mapping[str, Any]):
    """A pydantic model's or a dataclass's fields as a mapping, read lazily: each field's key is
    worked out as the walk enumerates it, within the walk's bound on keys, and a value is read only
    when the walk visits it, so fields past the walk's bounds, their metadata and their getters, are
    never touched. Keys as `model_dump` writes them: the fields', then the extra ones'. A key written
    twice is listed once, in its first place, holding what `model_dump` writes there last: an extra
    field's value over a field's, a later field's over an earlier one's, except that a field left
    out as None (an MCP SDK type's) replaces nothing.

    One visit's: what the class declares is worked out once for every visit (`again`), and in a
    pass for every object of the class (`Listed`), and the values, a model's extra fields and which
    field a key written twice holds are read as they are at the visit."""

    __slots__ = ("_value", "_declared", "_extra", "_skip_none", "_model")

    def __init__(self, value: object, declared: _Declared, extra: dict[Any, Any] | None, skip_none: bool = False, model: bool = False) -> None:
        self._value = value
        self._declared = declared
        #: A model's extra fields, as it held them when the visit began.
        self._extra = extra
        #: Fields holding None are left out, as the MCP SDKs write their own types.
        self._skip_none = skip_none
        self._model = model

    def again(self) -> _Fields | None:
        """The same object's fields for another visit: what its class declares, as far as reached,
        kept, and a model's extra fields read again, as an assignment pydantic validates replaces
        them. None when they can't be read, as `as_object` gives then."""
        if not self._model:
            return self
        try:
            extra = _extras(self._value)
        except Exception:
            return None
        return _Fields(self._value, self._declared, extra, self._skip_none, True)

    def scan(self) -> Iterator[str | _Skipped]:
        """The keys, with SKIPPED for each entry reached and not listed: `object_keys` counts those
        against its bound too."""
        listed: set[str] = set()
        skip_none = self._skip_none
        declared = self._declared
        reached = declared.reached
        index = 0
        # Those already reached, then the rest as they are reached (`_Declared.reach`).
        while index < len(reached) or (not declared.complete and declared.reach()):
            pair = reached[index]
            index += 1
            if pair is SKIPPED:
                yield SKIPPED
                continue
            key, attribute = pair
            # One holding None isn't written, and isn't listed: a later field's value may be.
            if key in listed or (skip_none and getattr(self._value, attribute, None) is None):
                yield SKIPPED
            else:
                listed.add(key)
                yield key
        extra = self._extra
        if extra is not None:
            # The dict's own entries, which are finite, whatever a subclass's `items` would give.
            for key, value in dict.items(extra):
                if key in listed or (skip_none and value is None):
                    # Already listed, in a field's place: its value is the extra's (`__getitem__`).
                    yield SKIPPED
                else:
                    listed.add(key)
                    yield key

    def __getitem__(self, key: str) -> Any:
        extra = self._extra
        if extra is not None and key in extra:
            value = extra[key]
            if value is not None or not self._skip_none:
                return value
        # Every field is reached first, as a later one written under the same key holds it: read by
        # name rather than walked (a result's `content`), that's all of them, once for every visit.
        attributes = self._declared.keyed().get(key)
        if attributes is None:
            raise KeyError(key)
        if not self._skip_none:
            return getattr(self._value, attributes[-1])
        # The last one not holding None, which changes as they do.
        for attribute in reversed(attributes):
            value = getattr(self._value, attribute, None)
            if value is not None:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key in self.scan() if key is not SKIPPED)

    def __len__(self) -> int:
        return sum(1 for _ in self)


def _mcp_type(cls: type) -> bool:
    """One of the MCP SDKs' own protocol types (`mcp.types` in 1.x, `mcp_types` in 2.x), which they
    always write without the fields that hold None."""
    module = getattr(cls, "__module__", None)
    if not isinstance(module, str):
        return False
    return module in ("mcp.types", "mcp_types") or module.startswith(("mcp.types.", "mcp_types."))


def _model_pairs(fields: dict[str, Any]) -> Iterator[_Pair]:
    # The dict's own entries, which are finite, whatever a subclass's `items` would give.
    for name, info in dict.items(fields):
        if getattr(info, "exclude", None) is True:
            yield SKIPPED
            continue
        alias = getattr(info, "serialization_alias", None)
        if not isinstance(alias, str):
            alias = getattr(info, "alias", None)
        yield (alias if isinstance(alias, str) else name), name


def _extras(value: object) -> dict[Any, Any] | None:
    """A model's extra fields: the dict it holds now."""
    extra = getattr(value, "model_extra", None)
    return extra if isinstance(extra, dict) else None


def _model_object(value: object, fields: dict[str, Any], shared: Declarations | None) -> Mapping[Any, Any]:
    """A model's fields as `model_dump(by_alias=True)` names them, then its extra fields. Read one
    by one when visited: no serializer, validator or computed field runs, and `model_dump` is never
    called. An MCP SDK type leaves out the fields holding None, as `model_dump(exclude_none=True)`,
    which the SDKs write them with. Its class's `model_fields` are worked out once for every model
    of the class in `shared`."""
    return _Fields(value, _shared(fields, lambda: _model_pairs(fields), shared), _extras(value), _mcp_type(type(value)), True)


#: What `dataclasses.fields` keeps of a class's `__dataclass_fields__`: not its ClassVar or InitVar ones.
_DATACLASS_FIELD: Final = getattr(dataclasses, "_FIELD", None)


def _dataclass_pairs(found: dict[str, Any]) -> Iterator[_Pair]:
    """`dataclasses.fields` of a dataclass whose `__dataclass_fields__` is `found`, one by one rather
    than as a tuple of them all, with SKIPPED for each ClassVar and InitVar it leaves out."""
    for field in dict.values(found):
        if getattr(field, "_field_type", None) is _DATACLASS_FIELD:
            yield field.name, field.name
        else:
            yield SKIPPED


def _read_dataclass_pairs(value: object) -> Iterator[_Pair]:
    """`dataclasses.fields(value)`, its `__dataclass_fields__` read as it's reached."""
    found = getattr(value, "__dataclass_fields__", None)
    if _DATACLASS_FIELD is None or not isinstance(found, dict):
        for field in dataclasses.fields(value):  # type: ignore[arg-type]
            yield field.name, field.name
        return
    yield from _dataclass_pairs(found)


def _dataclass_object(value: object, shared: Declarations | None) -> Mapping[Any, Any]:
    """A dataclass's fields, its `__dataclass_fields__` worked out once for every dataclass of its
    class in `shared`."""
    found: object = None
    if _DATACLASS_FIELD is not None:
        # Unguarded: a read that raises makes it no object at this visit (`as_object`), never read
        # again here, which would record what its class says later.
        found = getattr(value, "__dataclass_fields__", None)
    # By its type: one that's only said to be a dict is read as it's reached, raising there what it
    # raises, and for this object only.
    if issubclass(type(found), dict):
        table: dict[str, Any] = found  # type: ignore[assignment]
        return _Fields(value, _shared(table, lambda: _dataclass_pairs(table), shared), None)
    return _Fields(value, _Declared(lambda: _read_dataclass_pairs(value)), None)


def as_object(value: object, shared: Declarations | None = None) -> Mapping[Any, Any] | None:
    """What a JSON object is read from: a mapping, a pydantic model's fields or a dataclass's, or
    None. `shared`: the fields one pass has worked out, by the table they're declared in (`Listed`)."""
    try:
        # Inside the try: an object whose class can't be read (a lazy proxy whose factory fails) is no object.
        if isinstance(value, Mapping):
            return value
        fields = _pydantic_fields(value)
        if fields is not None:
            if getattr(type(value), "__pydantic_root_model__", False) is True:
                return None
            return _model_object(value, fields, shared)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return _dataclass_object(value, shared)
    except Exception:
        return None
    return None


class Listed:
    """The objects one pass reads, by id, each held so its id isn't reused: what each is read from
    (`as_object`) and its keys (`object_keys`), worked out at its first visit, however many times
    the event holds it. The fields a class declares are worked out once for all its objects the pass
    reads, by the table they're declared in (`Declarations`), shared with the `Listed` given as
    `sharing`; each object keeps only its own keys. The keys are a snapshot, as TypeScript's are, so
    a key added later in the pass isn't seen there; the values, and a model's extra fields, are read
    again at each visit."""

    __slots__ = ("_known", "_declarations")

    def __init__(self, sharing: Listed | None = None) -> None:
        self._known: dict[int, list[Any]] = {}
        self._declarations: Declarations = sharing._declarations if sharing is not None else {}

    def source(self, item: object) -> Mapping[Any, Any] | None:
        """What `item` is read from at this visit, or None when it isn't an object (`as_object`):
        whether it's one is worked out at its first visit."""
        known = self._known.get(id(item))
        if known is None or known[0] is not item:
            found = as_object(item, self._declarations)
            if found is not None:
                self._known[id(item)] = [item, found, MISSING]
            return found
        held = known[1]
        # By its type, not `isinstance`: an app's mapping whose `__class__` raises isn't asked.
        again = held.again() if type(held) is _Fields else held
        if again is not None:
            known[1] = again
        return again

    def keys(self, item: object, limit: int) -> Keys | None:
        """The keys of `item`, which `source` has given what it's read from, or None when it has
        more than `limit`: enumerated at its first visit, as one pass's limit only goes down, so one
        with more keys than what's left now can't fit either. May raise what the mapping's own
        iteration raises, and is enumerated again at its next visit then."""
        known = self._known[id(item)]
        keys = known[2]
        if keys is MISSING:
            keys = known[2] = object_keys(known[1], limit)
        return None if keys is None or keys.total > limit else keys


def _iso_datetime(value: _dt.datetime) -> str | NotJson:
    """An aware datetime as `Date.prototype.toJSON` writes it: UTC, with milliseconds and `Z`."""
    try:
        offset = value.utcoffset()
        if offset is None:
            return NOT_JSON
        utc = (value.replace(tzinfo=None) - offset)
    except Exception:
        return NOT_JSON
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}"
        f".{utc.microsecond // 1000:03d}Z"
    )


class _Unchanged(enum.Enum):
    UNCHANGED = 0


#: `stand_in` found no rule: the value is read as it is.
UNCHANGED: Final = _Unchanged.UNCHANGED


def stand_in(value: object) -> object:
    """What a value JSON doesn't have stands for, one step, or UNCHANGED.

    UUID: its string. Enum: its value, read as any value. An aware datetime: UTC with milliseconds
    and `Z`; a date: `YYYY-MM-DD`. A pydantic root model: its root; a pydantic URL: its string. A
    naive datetime, a time and a Decimal: NOT_JSON, rather than guessed or rounded without saying so.
    """
    if isinstance(value, uuid.UUID):
        try:
            return str(value)
        except Exception:
            return NOT_JSON
    if isinstance(value, enum.Enum):
        try:
            return value.value
        except Exception:
            return NOT_JSON
    if isinstance(value, _dt.datetime):
        return _iso_datetime(value)
    if isinstance(value, _dt.date):
        try:
            return value.isoformat()
        except Exception:
            return NOT_JSON
    if isinstance(value, (_dt.time, Decimal)):
        return NOT_JSON
    if _is_root_model(value):
        try:
            return getattr(value, "root")
        except Exception:
            return NOT_JSON
    if pydantic_url(value):
        try:
            return str(value)
        except Exception:
            return NOT_JSON
    return UNCHANGED


def pydantic_url(value: object) -> bool:
    """A pydantic URL (`AnyUrl` and the rest, or pydantic-core's `Url`), which pydantic writes as its
    string: the MCP SDKs' resource `uri` fields hold one."""
    try:
        for cls in type(value).__mro__:
            module = getattr(cls, "__module__", "")
            if (module == "pydantic.networks" and cls.__name__ in ("_BaseUrl", "_BaseMultiHostUrl")) or (
                module == "pydantic_core._pydantic_core" and cls.__name__ in ("Url", "MultiHostUrl")
            ):
                return True
    except Exception:
        return False
    return False


__all__ = [
    "BINARY_TYPES",
    "MAX_SAFE_INTEGER",
    "MISSING",
    "Missing",
    "NOT_JSON",
    "NotJson",
    "SKIPPED",
    "UNCHANGED",
    "UNREADABLE",
    "Keys",
    "Listed",
    "as_object",
    "binary_length",
    "is_array_index",
    "is_finite",
    "is_safe_integer",
    "join",
    "joined_prefix",
    "js_number",
    "js_order",
    "object_keys",
    "pydantic_url",
    "readable",
    "stand_in",
    "text",
    "to_units",
    "utf16_length",
    "utf8_length",
]
