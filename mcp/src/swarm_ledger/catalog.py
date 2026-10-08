"""The ledger's MCP tool catalog and argument checks for the lean front. Stdlib only.

`catalog.json` holds the `tools/list` result exactly as the fastmcp registrations in
`server.py` list it, the argument schema pydantic validates for each tool where it differs
from the listed one, and the tools that take the stamped `agent_id`. `python -m
swarm_ledger.catalog` writes it from `server.py`; a test fails when the two differ.

`validate` checks arguments the way pydantic's lax mode does in fastmcp: an integral float or
a numeric string is an integer, `"true"`/`"off"`/`1`/`0` are booleans, an unknown argument is
refused, and a missing one takes its default.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

PATH = Path(__file__).with_name("catalog.json")

_INT = re.compile(r"[+-]?[0-9]+(?:_[0-9]+)*(?:\.0*)?")
_TRUE = frozenset({"1", "on", "t", "true", "y", "yes"})
_FALSE = frozenset({"0", "off", "f", "false", "n", "no"})
_TYPES: dict[str, type] = {"string": str, "object": dict, "array": list}


def load(path: Path = PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


CATALOG: dict[str, Any] = load()
TOOLS: dict[str, dict[str, Any]] = {t["name"]: t for t in CATALOG["tools"]}
STAMPED: frozenset[str] = frozenset(CATALOG["stamped"])
WRAPPED: frozenset[str] = frozenset(
    name
    for name, tool in TOOLS.items()
    if (tool.get("outputSchema") or {}).get("x-fastmcp-wrap-result")
)


def _checks(name: str) -> dict[str, Any]:
    schema = copy.deepcopy(TOOLS[name]["inputSchema"])
    for key, sub in (CATALOG["checks"].get(name) or {}).items():
        listed = schema["properties"][key]
        schema["properties"][key] = {
            **sub,
            **({"default": listed["default"]} if "default" in listed else {}),
        }
    return schema


_CHECKS: dict[str, dict[str, Any]] = {name: _checks(name) for name in TOOLS}


def _join(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _integer(value: Any, where: str) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and _INT.fullmatch(value.strip()):
        return int(value.strip().partition(".")[0].replace("_", ""))
    raise ValueError(f"{where}\n  Input should be a valid integer")


def _boolean(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in _TRUE | _FALSE:
        return value.lower() in _TRUE
    raise ValueError(f"{where}\n  Input should be a valid boolean")


def _check(schema: dict[str, Any], value: Any, where: str) -> Any:
    options = schema.get("anyOf") or schema.get("oneOf")
    if options:
        errors = []
        for option in options:
            try:
                return _check(option, value, where)
            except ValueError as exc:
                errors.append(str(exc))
        raise ValueError(errors[0])
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{where}\n  Input should be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(v) for v in schema["enum"])
        raise ValueError(f"{where}\n  Input should be one of {allowed}")
    kind = schema.get("type")
    if kind == "null":
        if value is not None:
            raise ValueError(f"{where}\n  Input should be None")
    elif kind == "integer":
        value = _integer(value, where)
    elif kind == "boolean":
        value = _boolean(value, where)
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where}\n  Input should be a valid number")
    elif kind in _TYPES and not isinstance(value, _TYPES[kind]):
        raise ValueError(f"{where}\n  Input should be a valid {_noun(kind)}")
    if "minimum" in schema and value < schema["minimum"]:
        raise ValueError(f"{where}\n  Input should be greater than or equal to {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        raise ValueError(f"{where}\n  Input should be less than or equal to {schema['maximum']}")
    if kind == "array" and "items" in schema:
        items = cast("list[Any]", value)
        return [_check(schema["items"], v, f"{where}.{i}") for i, v in enumerate(items)]
    if kind == "object" and "properties" in schema:
        return _object(schema, cast("dict[str, Any]", value), where)
    return value


def _noun(kind: str) -> str:
    return {"object": "dictionary", "array": "list"}.get(kind, kind)


def _object(schema: dict[str, Any], value: dict[str, Any], where: str) -> dict[str, Any]:
    props: dict[str, Any] = schema.get("properties", {})
    missing = [k for k in schema.get("required", []) if k not in value]
    if missing:
        raise ValueError(f"{_join(where, missing[0])}\n  Missing required argument")
    if schema.get("additionalProperties") is False:
        extra = [k for k in value if k not in props]
        if extra:
            raise ValueError(f"{_join(where, extra[0])}\n  Unexpected keyword argument")
    out: dict[str, Any] = {}
    for key, sub in props.items():
        if key in value:
            out[key] = _check(sub, value[key], _join(where, key))
        elif "default" in sub:
            out[key] = copy.deepcopy(sub["default"])
    for key in value:
        out.setdefault(key, value[key])
    return out


def validate(name: str, args: Any) -> dict[str, Any]:
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments\n  Input should be a valid dictionary")
    return _object(_CHECKS[name], args, "")


def generate() -> dict[str, Any]:
    import asyncio
    import inspect
    import types
    import typing

    from fastmcp import Client
    from pydantic import TypeAdapter, WithJsonSchema

    from . import server

    async def listed() -> list[Any]:
        async with Client(server.mcp) as client:
            return list(await client.list_tools())

    tools = [
        t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in asyncio.run(listed())
    ]
    checks: dict[str, dict[str, Any]] = {}
    stamped: list[str] = []
    for tool in tools:
        fn = getattr(server, tool["name"])
        fn = getattr(fn, "fn", fn)
        if "agent_id" in inspect.signature(fn).parameters:
            stamped.append(tool["name"])
        for key, hint in typing.get_type_hints(fn, include_extras=True).items():
            if key in ("return", "agent_id"):
                continue
            validated = _strip(hint, WithJsonSchema, types, typing)
            if validated is not hint:
                checks.setdefault(tool["name"], {})[key] = TypeAdapter(validated).json_schema()
    return {
        "instructions": server.mcp.instructions,
        "tools": tools,
        "checks": checks,
        "stamped": sorted(stamped),
    }


def _strip(hint: Any, display: type, types: Any, typing: Any) -> Any:
    origin = typing.get_origin(hint)
    if origin is typing.Annotated:
        base, *metadata = typing.get_args(hint)
        kept = [m for m in metadata if not isinstance(m, display)]
        inner = _strip(base, display, types, typing)
        if len(kept) == len(metadata) and inner is base:
            return hint
        return typing.Annotated[(inner, *kept)] if kept else inner
    if origin in (typing.Union, types.UnionType):
        members = typing.get_args(hint)
        stripped = tuple(_strip(m, display, types, typing) for m in members)
        if all(a is b for a, b in zip(stripped, members)):
            return hint
        return typing.Union[stripped]
    return hint


def dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1, ensure_ascii=False) + "\n"


def main() -> int:
    with PATH.open("w", encoding="utf-8", newline="\n") as out:
        out.write(dumps(generate()))
    print(f"wrote {PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
