"""Which of the extras' inputs a tool owns, read from pydantic (Python spec, section 5, "Which tools
get the extras"): hand-built models and adapters in the default environment, then the real SDKs'
tools, each in its own group (`uv run --group mcp1 pytest tests/wrapper`, and mcp2, fastmcp3, fastmcp4).

A readable tool is one whose validator drops or refuses the keys it doesn't declare; the SDK cases
also check that against the SDK itself, so a reading can't be right on paper and wrong in practice."""

from __future__ import annotations

import asyncio
import importlib.metadata
import inspect
import linecache
import sys
import textwrap
import warnings
from typing import Annotated, Any

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
    TypeAdapter,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema

from lsquare.mcp import _ownership
from lsquare.mcp._extras import COULD_NOT_READ, INTENT, SESSION_ID, Ownership
from lsquare.mcp._ownership import adapter_ownership, fastmcp_tool_ownership, mcp_tool_ownership, model_ownership


def declared(owner: Ownership) -> list[str] | str:
    return sorted(owner.declared) if owner.readable else "unreadable"


# Models, as mcp's FastMCP and MCPServer make them from a function's signature


class ArgModelBase(BaseModel):
    """As mcp's: a base with a method of its own and no `extra` setting."""

    def model_dump_one_level(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in type(self).model_fields}

    model_config = ConfigDict(arbitrary_types_allowed=True)


def generated(**fields: Any) -> type[BaseModel]:
    return create_model("searchArguments", __base__=ArgModelBase, **fields)


def test_a_generated_model_with_no_extra_setting_drops_other_keys() -> None:
    model = generated(q=(str, ...), session_id=(str, ""))
    assert declared(model_ownership(model)) == ["session_id"]
    assert model.model_validate({"q": "a", "intent": "why"}).model_dump() == {"q": "a", "session_id": ""}
    assert declared(model_ownership(generated(q=(str, ...)))) == []
    assert declared(model_ownership(generated())) == []


def test_ignore_and_forbid_are_readable_and_allow_is_not() -> None:
    class Ignored(BaseModel):
        model_config = ConfigDict(extra="ignore")
        q: str

    class Forbidden(BaseModel, extra="forbid"):
        intent: str = ""

    class Allowed(BaseModel):
        model_config = ConfigDict(extra="allow")
        q: str

    class Typed(BaseModel, extra="allow"):
        __pydantic_extra__: dict[str, int]
        q: str

    assert declared(model_ownership(Ignored)) == []
    assert declared(model_ownership(Forbidden)) == ["intent"]
    with pytest.raises(ValidationError):
        Forbidden.model_validate({"session_id": "x"})
    for model in (Allowed, Typed):
        assert model_ownership(model) == Ownership.unreadable("it keeps keys it does not declare")
    assert Allowed.model_validate({"q": "a", "session_id": "x"}).model_extra == {"session_id": "x"}


def test_a_model_that_could_read_any_key_is_unreadable() -> None:
    class Init(BaseModel):
        q: str

        def __init__(self, **data: Any) -> None:
            super().__init__(**data)

    class Before(BaseModel):
        q: str

        @model_validator(mode="before")
        @classmethod
        def peek(cls, data: Any) -> Any:
            return data

    class After(BaseModel):
        q: str

        @model_validator(mode="after")
        def check(self) -> After:
            return self

    class Wrap(BaseModel):
        q: str

        @model_validator(mode="wrap")
        @classmethod
        def around(cls, data: Any, handler: Any) -> Any:
            return handler(data)

    class Mixin:
        @model_validator(mode="before")
        @classmethod
        def peek(cls, data: Any) -> Any:
            return data

    class Mixed(Mixin, BaseModel):
        q: str

    assert model_ownership(Init) == Ownership.unreadable("it defines __init__")
    for model in (Before, After, Wrap, Mixed):
        assert model_ownership(model) == Ownership.unreadable("it has a model validator"), model


def test_a_deprecated_root_validator_is_a_model_validator() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from pydantic import root_validator

        class Rooted(BaseModel):
            q: str

            @root_validator(pre=True)
            @classmethod
            def peek(cls, values: Any) -> Any:
                return values

    assert model_ownership(Rooted) == Ownership.unreadable("it has a model validator")


def test_a_field_validator_sees_only_its_field() -> None:
    class Checked(BaseModel):
        session_id: str = ""

        @field_validator("session_id", mode="before")
        @classmethod
        def strip(cls, value: Any) -> Any:
            return value

    assert declared(model_ownership(Checked)) == ["session_id"]


@pytest.mark.parametrize("name", ["model_validate", "model_validate_json", "model_validate_strings", "__get_pydantic_core_schema__", "__init__"])
def test_an_override_on_any_class_it_inherits_from_is_unreadable(name: str) -> None:
    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("never called")

    def schema(cls: Any, source: Any, handler: Any) -> Any:
        return handler(source)

    override: Any = never if name == "__init__" else classmethod(schema if name == "__get_pydantic_core_schema__" else never)
    # On a plain mixin, which comes before BaseModel, and on a model the tool's model inherits from.
    mixin: Any = type("Mixin", (), {name: override})
    base: Any = type("Base", (BaseModel,), {name: override})
    for bases in ((mixin, BaseModel), (base,)):
        model = create_model("Inheriting", __base__=bases, q=(str, ...))
        assert model_ownership(model) == Ownership.unreadable(f"it defines {name}"), bases


def test_its_own_core_schema_alone_means_nothing() -> None:
    class Plain(BaseModel):
        q: str

    assert "__pydantic_core_schema__" in vars(Plain)
    assert declared(model_ownership(Plain)) == []


class Aliased(BaseModel):
    a: str = Field("", alias="session_id")
    b: str = Field("", validation_alias=AliasChoices("x", AliasPath("intent", 0)))


@pytest.mark.parametrize(
    ("field", "owns"),
    [
        (Field("", alias="session_id"), ["session_id"]),
        (Field("", validation_alias="intent"), ["intent"]),
        (Field("", validation_alias=AliasChoices("x", "session_id")), ["session_id"]),
        (Field("", validation_alias=AliasChoices("x", AliasPath("intent", 0))), ["intent"]),
        (Field("", validation_alias=AliasPath("session_id", "inner")), ["session_id"]),
        (Field("", validation_alias=AliasPath("other", "session_id")), []),
        (Field("", alias="other", validation_alias="intent"), ["intent"]),
        (Field("", serialization_alias="session_id"), []),
    ],
)
def test_a_field_owns_its_name_alias_and_the_keys_its_validation_alias_reads(field: Any, owns: list[str]) -> None:
    model = create_model("Aliased", q=(str, field))
    assert declared(model_ownership(model)) == owns


def test_an_alias_generator_and_both_inputs_on_one_model() -> None:
    class Generated(BaseModel):
        model_config = ConfigDict(alias_generator=lambda name: "session_id" if name == "sid" else name)
        sid: str = ""

    assert declared(model_ownership(Generated)) == ["session_id"]
    assert declared(model_ownership(Aliased)) == ["intent", "session_id"]


def test_a_root_model_is_unreadable() -> None:
    class Root(RootModel[dict[str, str]]):
        pass

    assert model_ownership(Root) == Ownership.unreadable("it is a root model, not a model of fields")


def test_a_recursive_model_is_read_through_its_definitions() -> None:
    class Node(BaseModel):
        name: str
        kids: list[Node] = []

    class Holder(BaseModel):
        tree: Node
        intent: str = ""

    assert Node.__pydantic_core_schema__["type"] == "definitions"
    assert Holder.__pydantic_core_schema__["type"] == "definitions"
    assert declared(model_ownership(Node)) == []
    assert declared(model_ownership(Holder)) == ["intent"]


def test_a_custom_schema_or_a_wrapped_model_is_unreadable() -> None:
    class Plain(BaseModel):
        q: str

    def peek(value: Any) -> Any:
        return value

    wrapped = adapter_ownership(TypeAdapter(Annotated[Plain, BeforeValidator(peek)]))
    assert wrapped == Ownership.unreadable("it is a pydantic function-before schema, not a plain model")
    assert declared(adapter_ownership(TypeAdapter(Plain))) == []
    assert adapter_ownership(TypeAdapter(dict[str, str])) == Ownership.unreadable("it is a pydantic dict schema, not a plain model or call")

    class Custom(BaseModel):
        q: str

        @classmethod
        def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> CoreSchema:
            return core_schema.no_info_before_validator_function(peek, handler(source))

    assert model_ownership(Custom) == Ownership.unreadable("it defines __get_pydantic_core_schema__")


def test_a_hand_made_schema_is_read_by_its_rules() -> None:
    class Plain(BaseModel):
        q: str

    built = Plain.__pydantic_core_schema__
    assert built["type"] == "model"

    def with_schema(schema: Any) -> Ownership:
        fake: Any = type("Fake", (BaseModel,), {"__annotations__": {"q": str}})
        fake.__pydantic_core_schema__ = schema
        return model_ownership(fake)

    fields = built["schema"]
    assert declared(with_schema({**built, "cls": None})) == "unreadable"
    assert with_schema({**built, "custom_init": True}) == Ownership.unreadable("it defines __init__")
    assert with_schema({**built, "config": {"extra_fields_behavior": "allow"}}).why == "it keeps keys it does not declare"
    assert with_schema({**built, "schema": {**fields, "extra_behavior": "allow"}}).why == "it keeps keys it does not declare"
    assert with_schema({**built, "extra_behavior": "allow"}).why == "it keeps keys it does not declare"
    assert with_schema({**built, "schema": {"type": "any"}}).why == "its model is a pydantic any schema, not its fields"
    assert with_schema({**built, "schema": {**fields, "fields": {"q": {"type": "model-field", "validation_alias": 3}}}}) == COULD_NOT_READ
    assert with_schema({**built, "schema": {**fields, "fields": {"q": {"type": "model-field", "validation_alias": ["a", ["b"]]}}}}) == COULD_NOT_READ
    assert with_schema({"type": "definitions", "schema": {"type": "definition-ref", "schema_ref": "x"}, "definitions": []}).why == (
        "its schema refers to a definition it does not hold"
    )
    assert with_schema({"type": "call", "arguments_schema": {}}).why == "it is a pydantic call schema, not a plain model or call"
    assert with_schema({"type": "<script>"}).why == "it is a pydantic custom schema, not a plain model or call"


def test_a_model_not_yet_defined_is_unreadable_and_not_built_by_reading_it() -> None:
    class Later(BaseModel):
        q: Undefined  # type: ignore[name-defined]  # noqa: F821

    assert Later.__pydantic_complete__ is False
    assert model_ownership(Later) == Ownership.unreadable("its schema is not built")
    assert Later.__pydantic_complete__ is False


def test_anything_else_is_unreadable_and_nothing_raises() -> None:
    class Exploding:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError(name)

    assert model_ownership(object()) == Ownership.unreadable("its argument model is not a class")
    assert model_ownership(dict) == Ownership.unreadable("its argument model is not a pydantic model")
    assert adapter_ownership(object()) == COULD_NOT_READ
    for tool in (None, object(), Exploding(), 1):
        assert not mcp_tool_ownership(tool).readable
        assert not fastmcp_tool_ownership(tool).readable


# Calls over a function's arguments, as fastmcp validates them


def adapted(function: Any) -> TypeAdapter[Any]:
    return TypeAdapter(function)


def test_a_call_over_arguments_refuses_other_keys_and_owns_its_parameters() -> None:
    def search(q: str, session_id: str = "", limit: Annotated[int, Field(alias="intent")] = 1) -> str:
        return q

    adapter = adapted(search)
    assert declared(adapter_ownership(adapter)) == ["intent", "session_id"]
    with pytest.raises(ValidationError, match="unexpected_keyword_argument"):
        adapter.validate_python({"q": "a", "other": 1})

    def plain(q: str) -> str:
        return q

    assert declared(adapter_ownership(adapted(plain))) == []

    def chosen(q: Annotated[str, Field(validation_alias=AliasChoices("query", AliasPath("session_id", 0)))]) -> str:
        return q

    assert declared(adapter_ownership(adapted(chosen))) == ["session_id"]


def test_a_call_that_takes_any_keyword_is_unreadable() -> None:
    def anything(q: str, **rest: Any) -> str:
        return q

    def positional(*values: Any) -> None:
        return None

    assert adapter_ownership(adapted(anything)) == Ownership.unreadable("it keeps keys it does not declare")
    assert declared(adapter_ownership(adapted(positional))) == []


# The real SDKs' tools


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def major(distribution: str) -> int:
    """The installed major version, read from the distribution: with the tests on the path and no
    SDK installed, `import mcp` would find this test directory, as a namespace package."""
    try:
        return int(importlib.metadata.version(distribution).split(".")[0])
    except importlib.metadata.PackageNotFoundError:
        pytest.skip(f"{distribution} is not installed")


def search(q: str, session_id: str = "") -> str:
    return f"{q}|{session_id}"


def plain(q: str) -> str:
    return q


def test_mcp1_fastmcp_tools() -> None:
    if major("mcp") != 1:
        pytest.skip("mcp 1.x")
    fastmcp = pytest.importorskip("mcp.server.fastmcp")
    server = fastmcp.FastMCP("t")

    def with_context(q: str, ctx: Any, json: int = 0, intent: str = "") -> str:
        return q

    with_context.__annotations__["ctx"] = fastmcp.Context
    server.tool()(search)
    server.tool()(plain)
    server.tool()(with_context)
    manager = server._tool_manager
    assert declared(mcp_tool_ownership(manager.get_tool("search"))) == ["session_id"]
    assert declared(mcp_tool_ownership(manager.get_tool("plain"))) == []
    # The context isn't an argument, and `json` is a field `field_json` whose alias is `json`.
    assert declared(mcp_tool_ownership(manager.get_tool("with_context"))) == ["intent"]
    # What makes it readable: the SDK drops the keys it doesn't declare.
    assert run(manager.get_tool("plain").run({"q": "a", "session_id": "x", "intent": "why"})) == "a"
    assert run(manager.get_tool("search").run({"q": "a", "session_id": "x"})) == "a|x"
    check_mcp_reads(fastmcp.tools.base.Tool, manager.get_tool("plain"), "mcp.server.fastmcp.utilities.func_metadata")


def test_mcp2_mcpserver_tools() -> None:
    if major("mcp") != 2:
        pytest.skip("mcp 2.x")
    mcpserver = pytest.importorskip("mcp.server.mcpserver")
    server = mcpserver.MCPServer("t")

    def with_context(q: str, ctx: Any, intent: str = "") -> str:
        return q

    with_context.__annotations__["ctx"] = mcpserver.Context
    server.tool()(search)
    server.tool()(plain)
    server.tool()(with_context)
    manager = server._tool_manager
    assert declared(mcp_tool_ownership(manager.get_tool("search"))) == ["session_id"]
    assert declared(mcp_tool_ownership(manager.get_tool("plain"))) == []
    assert declared(mcp_tool_ownership(manager.get_tool("with_context"))) == ["intent"]
    metadata = manager.get_tool("plain").fn_metadata
    assert metadata.validate_arguments({"q": "a", "session_id": "x", "intent": "why"}) == {"q": "a"}
    check_mcp_reads(sys.modules["mcp.server.mcpserver.tools.base"].Tool, manager.get_tool("plain"), "mcp.server.mcpserver.utilities.func_metadata")


def check_mcp_reads(tool_class: Any, tool: Any, metadata_module: str) -> None:
    """The definitions as they are now, and only the SDK's own classes."""
    func_metadata = sys.modules[metadata_module]
    metadata = tool.fn_metadata
    original = metadata.arg_model
    metadata.arg_model = func_metadata.func_metadata(search).arg_model
    try:
        assert declared(mcp_tool_ownership(tool)) == ["session_id"]
    finally:
        metadata.arg_model = original
    assert declared(mcp_tool_ownership(tool)) == []

    class Mine(tool_class):  # type: ignore[misc]
        pass

    assert mcp_tool_ownership(Mine.from_function(plain)) == Ownership.unreadable("it is a Mine, not a plain Tool")

    class Metadata(func_metadata.FuncMetadata):  # type: ignore[misc, name-defined]
        pass

    replaced = tool.model_copy(update={"fn_metadata": Metadata(arg_model=original)})
    assert mcp_tool_ownership(replaced) == Ownership.unreadable("its arguments are read by something other than the SDK's own FuncMetadata")


@pytest.mark.parametrize("line", [3, 4])
def test_fastmcp_function_tools(line: int) -> None:
    if major("fastmcp") != line:
        pytest.skip(f"fastmcp {line}.x")
    fastmcp = pytest.importorskip("fastmcp")
    dependencies = pytest.importorskip("fastmcp.dependencies")
    server = fastmcp.FastMCP("t")

    def database() -> str:
        return "db"

    async def injected(q: str, ctx: Any, db: str = dependencies.Depends(database), limit: Annotated[int, Field(alias="intent")] = 1) -> str:
        return f"{q}|{db}|{limit}"

    injected.__annotations__["ctx"] = fastmcp.Context

    def generator(q: str) -> Any:
        yield q

    server.tool(search)
    server.tool(plain)
    server.tool(injected)
    server.tool(generator)
    tools = {name: run(server.get_tool(name)) for name in ("search", "plain", "injected", "generator")}
    assert declared(fastmcp_tool_ownership(tools["search"])) == ["session_id"]
    assert declared(fastmcp_tool_ownership(tools["plain"])) == []
    # Neither the context nor the injected dependency is an argument.
    assert declared(fastmcp_tool_ownership(tools["injected"])) == ["intent"]
    assert declared(fastmcp_tool_ownership(tools["generator"])) == []

    # What makes it readable: fastmcp refuses any other key, so removing the extras lets the call through.
    async def calls() -> list[Any]:
        async with fastmcp.Client(server) as client:
            replies = [await client.call_tool("plain", {"q": "a"}), await client.call_tool("injected", {"q": "a", "intent": 2})]
            refused = await client.call_tool("plain", {"q": "a", "session_id": "x"}, raise_on_error=False)
            return [*replies, refused]

    first, second, refused = run(calls())
    assert first.data == "a" and second.data == "a|db|2"
    assert refused.is_error and "session_id" in refused.content[0].text

    # The definitions as they are now: another function, read at the next call.
    tool = tools["plain"]
    original = tool.fn
    tool.fn = search
    try:
        assert declared(fastmcp_tool_ownership(tool)) == ["session_id"]
    finally:
        tool.fn = original
    assert declared(fastmcp_tool_ownership(tool)) == []


@pytest.mark.parametrize("line", [3, 4])
def test_fastmcp_tools_that_are_not_a_plain_function_tool(line: int) -> None:
    if major("fastmcp") != line:
        pytest.skip(f"fastmcp {line}.x")
    fastmcp = pytest.importorskip("fastmcp")
    tools = pytest.importorskip("fastmcp.tools")
    function_tool = sys.modules["fastmcp.tools.function_tool"]
    server = fastmcp.FastMCP("t")
    server.tool(plain)
    registered = run(server.get_tool("plain"))
    transformed = tools.Tool.from_tool(registered, name="renamed")
    assert fastmcp_tool_ownership(transformed) == Ownership.unreadable("it is a TransformedTool, not a plain FunctionTool")

    class Mine(function_tool.FunctionTool):  # type: ignore[misc, name-defined]
        pass

    assert fastmcp_tool_ownership(Mine.from_function(plain)) == Ownership.unreadable("it is a Mine, not a plain FunctionTool")
    # The mcp lines' reading doesn't take fastmcp's tools, nor fastmcp's theirs.
    assert mcp_tool_ownership(registered) == Ownership.unreadable("it is a FunctionTool, not a plain Tool")


@pytest.mark.parametrize("line", [3, 4])
def test_fastmcp_validator_built_in_a_way_not_known(line: int, monkeypatch: pytest.MonkeyPatch) -> None:
    if major("fastmcp") != line:
        pytest.skip(f"fastmcp {line}.x")
    fastmcp = pytest.importorskip("fastmcp")
    function_tool = sys.modules["fastmcp.tools.function_tool"]
    server = fastmcp.FastMCP("t")
    server.tool(search)
    tool = run(server.get_tool("search"))
    unknown = Ownership.unreadable("fastmcp builds its validator in a way this version of LSquare doesn't know")
    assert _ownership._read_chain(function_tool.FunctionTool.run) == line

    original = function_tool.FunctionTool.run
    source = inspect.getsource(original)

    async def changed(self: Any, arguments: dict[str, Any]) -> Any:
        return await original(self, {**arguments, "session_id": "theirs"})

    monkeypatch.setattr(function_tool.FunctionTool, "run", changed)
    assert fastmcp_tool_ownership(tool) == unknown
    monkeypatch.undo()
    assert declared(fastmcp_tool_ownership(tool)) == ["session_id"]

    # A run() in fastmcp's own module that builds its validator otherwise, or as written.
    for edit in ("exec_fn = wrapper_fn", "exec_fn = _wrap_body_errors(wrapper_fn)"):
        edited = textwrap.dedent(source.replace("exec_fn = _wrap_body_errors(wrapper_fn)", edit))
        filename = f"<edited run {edit}>"
        linecache.cache[filename] = (len(edited), None, edited.splitlines(True), filename)
        namespace: dict[str, Any] = {}
        exec(compile(edited, filename, "exec"), vars(function_tool), namespace)
        monkeypatch.setattr(function_tool.FunctionTool, "run", namespace["run"])
        expected = unknown if edit == "exec_fn = wrapper_fn" else Ownership.of([SESSION_ID])
        assert fastmcp_tool_ownership(tool) == expected, edit
        monkeypatch.undo()

    for helper in ("_wrap_body_errors", "get_cached_typeadapter"):
        monkeypatch.delattr(function_tool, helper)
        assert fastmcp_tool_ownership(tool) == unknown, helper
        monkeypatch.undo()
    monkeypatch.delattr(sys.modules["fastmcp.server.dependencies"], "without_injected_parameters")
    assert fastmcp_tool_ownership(tool) == unknown
    monkeypatch.undo()
    assert declared(fastmcp_tool_ownership(tool)) == ["session_id"]
    assert SESSION_ID in fastmcp_tool_ownership(tool).declared and INTENT not in fastmcp_tool_ownership(tool).declared
