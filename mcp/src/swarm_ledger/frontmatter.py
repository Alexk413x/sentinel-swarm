# Standard library only: the ledger runs on the base interpreter with -I -S, so it parses
# the YAML subset that settings and role files use instead of importing PyYAML.

from __future__ import annotations

import re
from typing import Any

_INT = re.compile(r"[-+]?(0|[1-9][0-9_]*)")
_FLOAT = re.compile(r"[-+]?([0-9][0-9_]*)?\.[0-9_]*([eE][-+][0-9]+)?")
_TRUE = frozenset({"true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"})
_FALSE = frozenset({"false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"})
_NULL = frozenset({"", "~", "null", "Null", "NULL"})
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/", " ": " "}

Line = tuple[int, str, int]


class FrontmatterError(ValueError):
    pass


def split(text: str) -> tuple[str, str] | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    return "\n".join(lines[1:end]), "\n".join(lines[end + 1 :])


def parse(text: str) -> Any:
    raw = text.splitlines()
    lines = [
        (_indent(line), _strip_comment(line).strip(), number) for number, line in enumerate(raw)
    ]
    lines = [line for line in lines if line[1]]
    if not lines:
        return None
    value, index = _block(lines, 0, lines[0][0], raw)
    if index < len(lines):
        raise FrontmatterError(f"line {lines[index][2] + 1}: unexpected indentation")
    return value


def _indent(line: str) -> int:
    if "\t" in line[: len(line) - len(line.lstrip())]:
        raise FrontmatterError("tabs are not allowed in indentation")
    return len(line) - len(line.lstrip(" "))


def _strip_comment(line: str) -> str:
    quote = None
    for i, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'" and (i == 0 or line[i - 1] in " \t[{,:-"):
            quote = char
        elif char == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _block(lines: list[Line], index: int, indent: int, raw: list[str]) -> tuple[Any, int]:
    if _is_item(lines[index][1]):
        return _sequence(lines, index, indent, raw)
    return _mapping(lines, index, indent, raw)


def _is_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _sequence(lines: list[Line], index: int, indent: int, raw: list[str]) -> tuple[list, int]:
    items: list = []
    while index < len(lines) and lines[index][0] == indent and _is_item(lines[index][1]):
        _, content, number = lines[index]
        rest = content[1:].lstrip()
        if not rest:
            value, index = _nested(lines, index + 1, indent, raw, allow_same=False)
        elif _key_split(rest) is not None and rest[0] not in "[{\"'":
            column = indent + (len(content) - len(rest))
            lines = [*lines[:index], (column, rest, number), *lines[index + 1 :]]
            value, index = _mapping(lines, index, column, raw)
        else:
            value, index = _inline(lines, index, rest, raw)
        items.append(value)
    return items, index


def _mapping(lines: list[Line], index: int, indent: int, raw: list[str]) -> tuple[dict, int]:
    result: dict = {}
    while index < len(lines) and lines[index][0] == indent and not _is_item(lines[index][1]):
        _, content, number = lines[index]
        parts = _key_split(content)
        if parts is None:
            raise FrontmatterError(f"line {number + 1}: expected 'key: value'")
        key, rest = parts
        if not rest:
            value, index = _nested(lines, index + 1, indent, raw, allow_same=True)
        elif rest[0] in "|>":
            value, index = _block_scalar(lines, index, indent, rest, raw)
        else:
            value, index = _inline(lines, index, rest, raw)
        result[key] = value
    return result, index


def _nested(
    lines: list[Line], index: int, indent: int, raw: list[str], allow_same: bool
) -> tuple[Any, int]:
    if index >= len(lines):
        return None, index
    child = lines[index][0]
    if child > indent or (allow_same and child == indent and _is_item(lines[index][1])):
        return _block(lines, index, child, raw)
    return None, index


def _key_split(content: str) -> tuple[str, str] | None:
    if content[0] in "\"'":
        end = _closing_quote(content, 0)
        if end is None or not content[end + 1 :].startswith(":"):
            return None
        key = _scalar(content[: end + 1])
        rest = content[end + 2 :]
        return (str(key), rest.strip()) if not rest or rest[0] == " " else None
    match = re.search(r":(\s|$)", content)
    if match is None or content[0] in "[{":
        return None
    return content[: match.start()].strip(), content[match.end() :].strip()


def _closing_quote(text: str, start: int) -> int | None:
    quote = text[start]
    i = start + 1
    while i < len(text):
        if quote == '"' and text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            if quote == "'" and text[i + 1 : i + 2] == "'":
                i += 2
                continue
            return i
        i += 1
    return None


def _inline(lines: list[Line], index: int, rest: str, raw: list[str]) -> tuple[Any, int]:
    if rest[0] in "[{":
        text = rest
        index += 1
        while _depth(text) > 0:
            if index >= len(lines):
                raise FrontmatterError(f"an unclosed flow collection: {rest}")
            text += " " + lines[index][1]
            index += 1
        value, end = _flow(text, 0)
        if text[end:].strip():
            raise FrontmatterError(f"unexpected text after a flow collection: {text[end:]}")
        return value, index
    return _scalar(rest), index + 1


def _depth(text: str) -> int:
    depth = 0
    quote = None
    for char in text:
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return depth


def _flow(text: str, i: int) -> tuple[Any, int]:
    i = _skip_space(text, i)
    if text[i] in "[{":
        closing = "]" if text[i] == "[" else "}"
        is_map = text[i] == "{"
        items: list = []
        mapping: dict = {}
        i = _skip_space(text, i + 1)
        while i < len(text) and text[i] != closing:
            if is_map:
                key, i = _flow_scalar(text, i, ":")
                if i >= len(text) or text[i] != ":":
                    raise FrontmatterError(f"expected ':' in a flow mapping: {text}")
                value, i = _flow(text, i + 1)
                mapping[str(key)] = value
            else:
                value, i = _flow(text, i)
                items.append(value)
            i = _skip_space(text, i)
            if i < len(text) and text[i] == ",":
                i = _skip_space(text, i + 1)
        if i >= len(text):
            raise FrontmatterError(f"an unclosed flow collection: {text}")
        return (mapping if is_map else items), i + 1
    return _flow_scalar(text, i, "")


def _flow_scalar(text: str, i: int, extra_stops: str) -> tuple[Any, int]:
    i = _skip_space(text, i)
    if i < len(text) and text[i] in "\"'":
        end = _closing_quote(text, i)
        if end is None:
            raise FrontmatterError(f"an unclosed quote: {text[i:]}")
        return _scalar(text[i : end + 1]), end + 1
    start = i
    while i < len(text) and text[i] not in ",]}" + extra_stops:
        i += 1
    return _scalar(text[start:i].strip()), i


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def _block_scalar(
    lines: list[Line], index: int, indent: int, header: str, raw: list[str]
) -> tuple[str, int]:
    first = lines[index][2] + 1
    index += 1
    while index < len(lines) and lines[index][0] > indent:
        index += 1
    last = lines[index][2] if index < len(lines) else len(raw)
    body = raw[first:last]
    while body and not body[-1].strip():
        body.pop()
    width = min((len(line) - len(line.lstrip(" ")) for line in body if line.strip()), default=0)
    text_lines = [line[width:] for line in body]
    if header[0] == "|":
        text = "\n".join(text_lines)
    else:
        text = re.sub(r"(?<!\n)\n(?!\n)", " ", "\n".join(text_lines))
        text = re.sub(r"\n\n", "\n", text)
    keep = header[1:].strip()
    if keep.startswith("-"):
        return text, index
    return text + "\n" if text else text, index


def _scalar(text: str) -> Any:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return _unescape(text[1:-1])
    if text in _NULL:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    if _INT.fullmatch(text):
        return int(text.replace("_", ""))
    if _FLOAT.fullmatch(text) and any(c.isdigit() for c in text):
        return float(text.replace("_", ""))
    if text in (".inf", "+.inf", ".Inf"):
        return float("inf")
    if text in ("-.inf", "-.Inf"):
        return float("-inf")
    if text in (".nan", ".NaN"):
        return float("nan")
    return text


def _unescape(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        char = text[i]
        if char != "\\" or i + 1 >= len(text):
            out.append(char)
            i += 1
            continue
        code = text[i + 1]
        if code in _ESCAPES:
            out.append(_ESCAPES[code])
            i += 2
        elif code in "xuU":
            size = {"x": 2, "u": 4, "U": 8}[code]
            out.append(chr(int(text[i + 2 : i + 2 + size], 16)))
            i += 2 + size
        else:
            raise FrontmatterError(f"an unknown escape \\{code}")
    return "".join(out)
