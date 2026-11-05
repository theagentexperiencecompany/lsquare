"""Which of the extras' inputs a high-level server's tool owns, read from pydantic (Python spec,
section 5, "Which tools get the extras"), where TypeScript reads zod.

The shared rule: a tool gets the extras only when its validator drops or refuses keys it doesn't
declare, and nothing in it can read them. The validator read is the one the SDK runs on the call's
arguments: the argument model mcp's FastMCP and MCPServer made from the function's signature, or
the TypeAdapter fastmcp's `FunctionTool.run()` builds. Its core schema must be a plain model over
its fields, which drops or refuses other keys, or a call over the function's arguments, which
refuses them; anything else could read any key, so both inputs are the tool's and its calls are
left as they are. Read as the definitions are now, at every list and call: nothing is cached but
the check that fastmcp's `run()` is code this module knows.

Nothing is imported: pydantic and the SDKs are read from the modules the app has loaded, and every
entry point returns an unreadable `Ownership` instead of raising.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
import textwrap
import types
from typing import Any, Final

from ._extras import COULD_NOT_READ, Ownership

#: The modules of mcp's high-level `Tool` classes, each with the module of its `FuncMetadata`.
_MCP_LINES: Final = {
    "mcp.server.fastmcp.tools.base": "mcp.server.fastmcp.utilities.func_metadata",
    "mcp.server.mcpserver.tools.base": "mcp.server.mcpserver.utilities.func_metadata",
}
_FASTMCP_TOOLS: Final = "fastmcp.tools.function_tool"
_FASTMCP_DEPENDENCIES: Final = "fastmcp.server.dependencies"

#: Any of these, on the model's class or one it inherits from below `BaseModel`, can read a key the
#: fields never declare. (Every model has its own `__pydantic_core_schema__`: that alone means nothing.)
_OVERRIDES: Final = ("__init__", "model_validate", "model_validate_json", "model_validate_strings", "__get_pydantic_core_schema__")
_DROPPED_OR_REFUSED: Final = ("ignore", "forbid")
_KIND: Final = re.compile(r"[a-z][a-z0-9-]{0,39}\Z", re.ASCII)
_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z", re.ASCII)

#: The lines of `FunctionTool.run()` that build its validator, as fastmcp 3.x and 4.x write them.
#: The validator is built the same way only when `run()` starts with one of them, after its docstring.
_CHAINS: Final = {
    3: """
from fastmcp.server.dependencies import without_injected_parameters
wrapper_fn = without_injected_parameters(self.fn, run_in_thread=self.run_in_thread)
exec_fn = _wrap_body_errors(wrapper_fn)
type_adapter = get_cached_typeadapter(exec_fn)
""",
    4: """
from fastmcp.server.dependencies import without_injected_parameters
body_fn = _wrap_body_errors(self.fn, materialize_generators=True)
wrapper_fn = without_injected_parameters(body_fn, run_in_thread=self.run_in_thread)
exec_fn = _wrap_body_errors(wrapper_fn)
type_adapter = get_cached_typeadapter(exec_fn)
""",
}
_UNKNOWN_CHAIN: Final = Ownership.unreadable("fastmcp builds its validator in a way this version of LSquare doesn't know")
_KEEPS_KEYS: Final = Ownership.unreadable("it keeps keys it does not declare")
_MODEL_VALIDATOR: Final = Ownership.unreadable("it has a model validator")
_ROOT_MODEL: Final = Ownership.unreadable("it is a root model, not a model of fields")

#: The last `run()` read, by its code object, and which chain it starts with (None: neither).
_known_run: tuple[types.CodeType, int | None] | None = None


def mcp_tool_ownership(tool: Any) -> Ownership:
    """A registered tool of mcp's FastMCP (1.x) or MCPServer (2.x): its argument model,
    `fn_metadata.arg_model`, which `Tool.run()` validates the arguments with. Only the SDK's own
    `Tool` and `FuncMetadata` are read: a subclass could read the arguments itself."""
    try:
        tool_class = type(tool)
        metadata_module = _MCP_LINES.get(tool_class.__module__)
        if metadata_module is None or not _is_loaded_class(tool_class, tool_class.__module__, "Tool"):
            return Ownership.unreadable(_not_plain(tool_class, "Tool"))
        metadata = tool.fn_metadata
        if not _is_loaded_class(type(metadata), metadata_module, "FuncMetadata"):
            return Ownership.unreadable("its arguments are read by something other than the SDK's own FuncMetadata")
        return _model_ownership(metadata.arg_model)
    except Exception:
        return COULD_NOT_READ


def fastmcp_tool_ownership(tool: Any) -> Ownership:
    """A registered tool of fastmcp (3.x or 4.x): the validator `FunctionTool.run()` builds, built
    here with the same helpers, read from the loaded fastmcp. Only a `FunctionTool` itself is read:
    proxies, transformed tools and other subclasses receive raw arguments."""
    try:
        tool_class = type(tool)
        if not _is_loaded_class(tool_class, _FASTMCP_TOOLS, "FunctionTool"):
            return Ownership.unreadable(_not_plain(tool_class, "FunctionTool"))
        module = sys.modules[_FASTMCP_TOOLS]
        run = tool_class.__dict__.get("run")
        chain = _chain(run) if isinstance(run, types.FunctionType) and run.__globals__ is vars(module) else None
        dependencies = sys.modules.get(_FASTMCP_DEPENDENCIES)
        without_injected_parameters = getattr(dependencies, "without_injected_parameters", None)
        wrap_body_errors = vars(module).get("_wrap_body_errors")
        get_cached_typeadapter = vars(module).get("get_cached_typeadapter")
        if chain is None or not (
            callable(without_injected_parameters) and callable(wrap_body_errors) and callable(get_cached_typeadapter)
        ):
            return _UNKNOWN_CHAIN
        # As run() does it, line by line. Each helper caches by its arguments, so this is the very
        # validator the next call runs, unless the tool's function changes in between.
        if chain == 4:
            body_fn = wrap_body_errors(tool.fn, materialize_generators=True)
            wrapper_fn = without_injected_parameters(body_fn, run_in_thread=tool.run_in_thread)
        else:
            wrapper_fn = without_injected_parameters(tool.fn, run_in_thread=tool.run_in_thread)
        exec_fn = wrap_body_errors(wrapper_fn)
        type_adapter = get_cached_typeadapter(exec_fn)
        return adapter_ownership(type_adapter)
    except Exception:
        return COULD_NOT_READ


def model_ownership(model: Any) -> Ownership:
    """A pydantic model class whose `model_validate()` takes the arguments, as the mcp lines' argument models."""
    try:
        return _model_ownership(model)
    except Exception:
        return COULD_NOT_READ


def adapter_ownership(adapter: Any) -> Ownership:
    """A pydantic TypeAdapter that validates the arguments, as fastmcp's."""
    try:
        return _schema_ownership(adapter.core_schema, None)
    except Exception:
        return COULD_NOT_READ


def _model_ownership(model: Any) -> Ownership:
    if not isinstance(model, type):
        return Ownership.unreadable("its argument model is not a class")
    # Its class first: a model validator or an override says more than the schema it makes.
    checked = _class_ownership(model)
    if checked is not None:
        return checked
    return _schema_ownership(getattr(model, "__pydantic_core_schema__"), model)


def _schema_ownership(schema: Any, model: type[Any] | None) -> Ownership:
    """A validator's core schema: a model over its fields, or a call over a function's arguments."""
    if not isinstance(schema, dict):
        # A model not fully defined has a stand-in, which builds the schema when read: not here.
        return Ownership.unreadable("its schema is not built")
    if schema.get("type") == "definitions":
        schema = _definition(schema)
        if schema is None:
            return Ownership.unreadable("its schema refers to a definition it does not hold")
    kind = schema.get("type")
    if kind == "model":
        return _fields_ownership(schema, model)
    if kind == "call" and model is None:
        return _arguments_ownership(schema)
    if isinstance(kind, str) and kind.startswith("function-"):
        return Ownership.unreadable(f"it is a pydantic {_kind(kind)} schema, not a plain model")
    return Ownership.unreadable(f"it is a pydantic {_kind(kind)} schema, not a plain model or call")


def _definition(schema: dict[Any, Any]) -> dict[Any, Any] | None:
    """A top-level `definitions` schema's own: the definition its reference names, or its schema
    itself when that isn't a reference (an argument model whose fields refer to a recursive one)."""
    inner = schema.get("schema")
    if not isinstance(inner, dict):
        return None
    if inner.get("type") != "definition-ref":
        return inner
    ref = inner.get("schema_ref")
    definitions = schema.get("definitions")
    if not isinstance(ref, str) or not isinstance(definitions, list):
        return None
    found = [entry for entry in definitions if isinstance(entry, dict) and entry.get("ref") == ref]
    return found[0] if len(found) == 1 else None


def _fields_ownership(schema: dict[Any, Any], model: type[Any] | None) -> Ownership:
    """`model` over `model-fields`, without `custom_init`, dropping or refusing extra keys."""
    if schema.get("root_model", False) is not False:
        return _ROOT_MODEL
    if schema.get("custom_init", False) is not False:
        return Ownership.unreadable("it defines __init__")
    cls = schema.get("cls")
    if cls is not model:
        checked = _class_ownership(cls)
        if checked is not None:
            return checked
    fields_schema = schema.get("schema")
    if not isinstance(fields_schema, dict) or fields_schema.get("type") != "model-fields":
        kind = fields_schema.get("type") if isinstance(fields_schema, dict) else None
        return Ownership.unreadable(f"its model is a pydantic {_kind(kind)} schema, not its fields")
    # pydantic-core reads the fields' own setting first, then the model's config; none means ignore.
    config = schema.get("config", {})
    if not isinstance(config, dict):
        return COULD_NOT_READ
    for behavior in (schema.get("extra_behavior"), fields_schema.get("extra_behavior"), config.get("extra_fields_behavior")):
        if behavior is not None and behavior not in _DROPPED_OR_REFUSED:
            return _KEEPS_KEYS
    if "extras_schema" in fields_schema or "extras_keys_schema" in fields_schema:
        return _KEEPS_KEYS
    fields = fields_schema.get("fields")
    if not isinstance(fields, dict):
        return COULD_NOT_READ
    names: set[str] = set()
    for name, field in fields.items():
        if not isinstance(name, str) or not isinstance(field, dict):
            return COULD_NOT_READ
        names.add(name)
        names.update(_alias_names(field.get("validation_alias")))
    # The class's own record of its fields, in case it says more than the schema.
    for owner in {model, cls} - {None}:
        names.update(_declared_fields(owner))
    return Ownership.of(names)


def _arguments_ownership(schema: dict[Any, Any]) -> Ownership:
    """`call` over `arguments` without `**kwargs`: it refuses any other key."""
    arguments = schema.get("arguments_schema")
    kind = arguments.get("type") if isinstance(arguments, dict) else None
    if not isinstance(arguments, dict) or kind != "arguments":
        return Ownership.unreadable(f"its arguments are a pydantic {_kind(kind)} schema")
    if "var_kwargs_schema" in arguments:
        return _KEEPS_KEYS
    behavior = arguments.get("extra_behavior")
    if behavior is not None and behavior not in _DROPPED_OR_REFUSED:
        return _KEEPS_KEYS
    parameters = arguments.get("arguments_schema")
    if not isinstance(parameters, list):
        return COULD_NOT_READ
    names: set[str] = set()
    for parameter in parameters:
        name = parameter.get("name") if isinstance(parameter, dict) else None
        if not isinstance(name, str):
            return COULD_NOT_READ
        names.add(name)
        names.update(_alias_names(parameter.get("alias")))
    return Ownership.of(names)


def _alias_names(alias: Any) -> list[str]:
    """The keys an alias in a core schema reads at the top level: the alias itself, the first
    element of its path, or that of each of its choices. Raises on any other form."""
    if alias is None:
        return []
    if isinstance(alias, str):
        return [alias]
    if not isinstance(alias, list):
        raise TypeError("an alias of an unknown form")
    paths = [entry for entry in alias if isinstance(entry, list)]
    if not paths:
        paths = [alias]
    elif len(paths) != len(alias):
        raise TypeError("an alias of an unknown form")
    return [path[0] for path in paths if path and isinstance(path[0], str)]


def _declared_fields(model: Any) -> list[str]:
    """A model's field names and the keys their aliases read: a string, any choice of an
    `AliasChoices`, the first element of an `AliasPath`. Raises on any other form."""
    aliases = sys.modules.get("pydantic.aliases")
    choices_class = getattr(aliases, "AliasChoices", None)
    path_class = getattr(aliases, "AliasPath", None)

    def keys(alias: Any) -> list[str]:
        if alias is None:
            return []
        if isinstance(alias, str):
            return [alias]
        if path_class is not None and type(alias) is path_class:
            return [alias.path[0]] if alias.path and isinstance(alias.path[0], str) else []
        if choices_class is not None and type(alias) is choices_class:
            return [key for choice in alias.choices for key in keys(choice)]
        raise TypeError("an alias of an unknown form")

    names: list[str] = []
    for name, info in model.model_fields.items():
        names.append(name)
        names.extend(keys(info.alias))
        names.extend(keys(info.validation_alias))
    return names


def _class_ownership(cls: Any) -> Ownership | None:
    """None when neither the model's class nor one it inherits from, up to pydantic's `BaseModel`,
    has a model validator or defines what could read keys its fields don't declare."""
    pydantic_main = sys.modules.get("pydantic.main")
    base = getattr(pydantic_main, "BaseModel", None)
    if not isinstance(cls, type) or not isinstance(base, type) or base not in cls.__mro__:
        return Ownership.unreadable("its argument model is not a pydantic model")
    root_model = getattr(sys.modules.get("pydantic.root_model"), "RootModel", None)
    if root_model is not None and root_model in cls.__mro__:
        return _ROOT_MODEL
    for owner in cls.__mro__:
        if owner is base:
            break
        own = vars(owner)
        for name in _OVERRIDES:
            if name in own:
                return Ownership.unreadable(f"it defines {name}")
    # pydantic gathers the validators of every class the model inherits from, models or not, here.
    decorators = getattr(cls, "__pydantic_decorators__")
    if decorators.model_validators or decorators.root_validators:
        return _MODEL_VALIDATOR
    return None


def _chain(run: types.FunctionType) -> int | None:
    """Which of fastmcp's chains `run()` starts with, read from its source, or None."""
    global _known_run
    code = run.__code__
    known = _known_run
    if known is not None and known[0] is code:
        return known[1]
    chain = _read_chain(run)
    _known_run = (code, chain)
    return chain


def _read_chain(run: types.FunctionType) -> int | None:
    try:
        if hasattr(run, "__wrapped__"):
            return None
        lines, _ = inspect.getsourcelines(run)
        tree = ast.parse(textwrap.dedent("".join(lines)))
        function = tree.body[0] if len(tree.body) == 1 else None
        if not isinstance(function, ast.AsyncFunctionDef) or function.name != "run" or function.decorator_list:
            return None
        if not function.args.args or function.args.args[0].arg != "self":
            return None
        body = function.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        found = [ast.dump(statement) for statement in body]
        for chain, source in _CHAINS.items():
            expected = [ast.dump(statement) for statement in ast.parse(source).body]
            if found[: len(expected)] == expected:
                return chain
        return None
    except Exception:
        return None


def _is_loaded_class(cls: Any, module: str, name: str) -> bool:
    """`cls` is exactly the class `name` of the loaded `module`."""
    return (
        isinstance(cls, type)
        and cls.__name__ == name
        and cls.__module__ == module
        and getattr(sys.modules.get(module), name, None) is cls
    )


def _not_plain(cls: Any, expected: str) -> str:
    name = getattr(cls, "__name__", None)
    if isinstance(name, str) and _NAME.match(name) and name != expected:
        return f"it is a {name}, not a plain {expected}"
    return f"it is not a plain {expected} of the SDK"


def _kind(kind: Any) -> str:
    """A core schema's type, as said in a reason: a custom schema could hold anything there."""
    return kind if isinstance(kind, str) and _KIND.match(kind) else "custom"
