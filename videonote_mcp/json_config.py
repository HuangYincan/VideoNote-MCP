"""Read JSONC and edit object fields while preserving unrelated text/comments."""
import json
import re
from dataclasses import dataclass, field

_TOKEN = re.compile(r'\s+|//[^\r\n]*|/\*[\s\S]*?\*/|"(?:[^"\\]|\\.)*"|'
                    r'true|false|null|[-0-9.eE+]+|[{}\[\]:,]')


@dataclass
class _Node:
    start: int
    end: int
    fields: dict = field(default_factory=dict)
    suffix: int = 0
    close: int = 0


@dataclass
class _Field:
    start: int
    prefix: int
    value: _Node
    tail: str


def _tokens(text):
    result, position = [], 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None:
            raise ValueError("Invalid JSONC token")
        token = match.group()
        if not token.isspace() and not token.startswith(("//", "/*")):
            result.append((token, match.start(), match.end()))
        position = match.end()
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def loads(text: str):
    tokens = _tokens(text)
    normalized = " ".join(token for index, (token, _, _) in enumerate(tokens)
                         if not (token == "," and index + 1 < len(tokens)
                                 and tokens[index + 1][0] in ("}", "]")))
    return json.loads(normalized, object_pairs_hook=_unique_object)


def strict_loads(text: str):
    return json.loads(text, object_pairs_hook=_unique_object)


def _tree(text: str, tokens: list, index: int = 0) -> tuple[_Node, int]:
    token, start, end = tokens[index]
    node = _Node(start, end)
    index += 1
    if token == "{":
        prefix = end
        while tokens[index][0] != "}":
            key, key_start, _ = tokens[index]
            child, index = _tree(text, tokens, index + 2)  # key, colon, value
            tail = ""
            suffix = child.end
            if tokens[index][0] == ",":
                _, comma_start, comma_end = tokens[index]
                tail = text[child.end:comma_start]
                suffix = comma_end
                index += 1
            node.fields[json.loads(key)] = _Field(key_start, prefix, child, tail)
            prefix = suffix
        node.suffix, node.close = prefix, tokens[index][1]
        node.end = tokens[index][2]
        return node, index + 1
    if token == "[":
        while tokens[index][0] != "]":
            _, index = _tree(text, tokens, index)
            if tokens[index][0] == ",":
                index += 1
        node.end = tokens[index][2]
        return node, index + 1
    return node, index


def _indent(text, position):
    prefix = text[:position].rsplit("\n", 1)[-1]
    return prefix if not prefix.strip() else "  "


def _render(text: str, node: _Node, old, new) -> str:
    if old == new:
        return text[node.start:node.end]
    if isinstance(old, dict) and isinstance(new, dict):
        chunks = []
        for key, item in node.fields.items():
            if key in new:
                chunks.append(text[item.prefix:item.start] + text[item.start:item.value.start]
                              + _render(text, item.value, old[key], new[key]) + item.tail)
        indent = _indent(text, next(iter(node.fields.values())).start) if node.fields else _indent(text, node.start) + "  "
        for key, value in new.items():
            if key not in old:
                encoded = json.dumps(value, ensure_ascii=False, indent=2).replace("\n", "\n" + indent)
                chunks.append("\n" + indent + json.dumps(key, ensure_ascii=False) + ": " + encoded)
        return "{" + ",".join(chunks) + text[node.suffix:node.close] + "}"
    indent = _indent(text, node.start)
    return json.dumps(new, ensure_ascii=False, indent=2).replace("\n", "\n" + indent)


def dumps_preserving(text: str, document: dict) -> str:
    text = text if text.strip() else "{}"
    old = loads(text)
    root, _ = _tree(text, _tokens(text))
    result = text[:root.start] + _render(text, root, old, document) + text[root.end:]
    if loads(result) != document:
        raise ValueError("JSONC edit did not preserve the intended document")
    return result if result.endswith("\n") else result + "\n"
