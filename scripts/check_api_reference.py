#!/usr/bin/env python3
"""Compare the hand-written Cowork API reference with server OpenAPI."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from html.parser import HTMLParser
import importlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable
from urllib.parse import urlsplit


HTTP_METHODS = frozenset(
    {"delete", "get", "head", "options", "patch", "post", "put"}
)
JSON_TYPES = frozenset(
    {"array", "boolean", "integer", "number", "object", "string"}
)
RESPONSE_KINDS = frozenset({"binary", "empty", "json", "sse", "text"})
_PLACEHOLDER = re.compile(r"\{[^{}]+\}")


class ContractParseError(ValueError):
    """The reference annotations or visible response example are invalid."""

    def __init__(self, errors: str | Iterable[str]):
        if isinstance(errors, str):
            messages = [errors]
        else:
            messages = list(errors)
        self.errors = messages
        super().__init__("\n".join(messages))


@dataclass(frozen=True)
class FieldContract:
    types: frozenset[str]
    nullable: bool


@dataclass(frozen=True)
class EndpointContract:
    method: str
    path: str
    normalized_path: str
    success_status: int
    response_kinds: frozenset[str]
    root_types: frozenset[str]
    fields: dict[str, FieldContract]


@dataclass
class _RawField:
    parts: list[str] = field(default_factory=list)
    nullable: str | None = None
    declared_type: str | None = None


@dataclass
class _ResponseBlock:
    kind: str
    parts: list[str] = field(default_factory=list)
    fields: list[_RawField] = field(default_factory=list)


@dataclass
class _EndpointBuilder:
    line: int
    success_status: str | None
    response_kind: str | None
    method: str | None = None
    path: str | None = None
    responses: list[_ResponseBlock] = field(default_factory=list)


class _ReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.builders: list[_EndpointBuilder] = []
        self.errors: list[str] = []
        self._endpoint: _EndpointBuilder | None = None
        self._endpoint_div_depth = 0
        self._method_parts: list[str] | None = None
        self._response: _ResponseBlock | None = None
        self._field: _RawField | None = None

    def handle_starttag(
        self, tag: str, attrs_list: list[tuple[str, str | None]]
    ) -> None:
        attrs = dict(attrs_list)
        classes = set((attrs.get("class") or "").split())
        if self._endpoint is None:
            if tag == "div" and "endpoint" in classes:
                self._endpoint = _EndpointBuilder(
                    line=self.getpos()[0],
                    success_status=attrs.get("data-success-status"),
                    response_kind=attrs.get("data-response-kind"),
                )
                self._endpoint_div_depth = 1
            return

        if tag == "div":
            self._endpoint_div_depth += 1
        if tag == "span" and "method" in classes:
            self._method_parts = []
        if tag == "span" and "endpoint-path" in classes:
            self._endpoint.path = attrs.get("data-path")
        if tag == "pre" and "data-response-body" in attrs:
            if self._response is not None:
                self.errors.append(
                    f"line {self.getpos()[0]}: nested response examples are not "
                    "supported"
                )
                return
            self._response = _ResponseBlock(
                kind=(attrs.get("data-response-body") or "json").strip().lower()
            )
        if tag == "span" and self._response is not None and "hl" in classes:
            if self._field is not None:
                self.errors.append(
                    f"line {self.getpos()[0]}: nested response field annotations "
                    "are not supported"
                )
                return
            self._field = _RawField(
                nullable=attrs.get("data-nullable"),
                declared_type=attrs.get("data-type"),
            )

    def handle_data(self, data: str) -> None:
        if self._method_parts is not None:
            self._method_parts.append(data)
        if self._response is not None:
            self._response.parts.append(data)
        if self._field is not None:
            self._field.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._endpoint is None:
            return
        if tag == "span" and self._field is not None:
            assert self._response is not None
            self._response.fields.append(self._field)
            self._field = None
        elif tag == "span" and self._method_parts is not None:
            self._endpoint.method = "".join(self._method_parts).strip().lower()
            self._method_parts = None
        elif tag == "pre" and self._response is not None:
            self._endpoint.responses.append(self._response)
            self._response = None
        if tag == "div":
            self._endpoint_div_depth -= 1
            if self._endpoint_div_depth == 0:
                self.builders.append(self._endpoint)
                self._endpoint = None

    def close(self) -> None:
        super().close()
        if self._endpoint is not None:
            self.errors.append(
                f"line {self._endpoint.line}: endpoint block is not closed"
            )


def normalize_path(path: str) -> str:
    parsed = urlsplit(path)
    normalized = parsed.path or "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if normalized == "/api/v1":
        normalized = "/"
    elif normalized.startswith("/api/v1/"):
        normalized = normalized[len("/api/v1") :]
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return _PLACEHOLDER.sub("{}", normalized)


def _strip_json_comments(text: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and following == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and following == "*":
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                if text[index] in "\r\n":
                    output.append(text[index])
                index += 1
            index += 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _strip_trailing_commas(text: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "]}":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def parse_json_example(text: str) -> Any:
    return json.loads(_strip_trailing_commas(_strip_json_comments(text)))


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    raise TypeError(f"unsupported JSON value {value!r}")


def _object_fields(value: Any, prefix: str = "response") -> list[tuple[str, str, Any]]:
    found: list[tuple[str, str, Any]] = []
    if isinstance(value, dict):
        for name, child in value.items():
            path = f"{prefix}.{name}"
            found.append((path, name, child))
            if isinstance(child, dict):
                found.extend(_object_fields(child, path))
            elif isinstance(child, list):
                for item in child:
                    found.extend(_object_fields(item, f"{path}[]"))
    elif isinstance(value, list):
        for item in value:
            found.extend(_object_fields(item, f"{prefix}[]"))
    return found


def _declared_types(value: str, label: str) -> frozenset[str]:
    names = {part.strip().lower() for part in value.split("|") if part.strip()}
    if "json" in names:
        names.remove("json")
        names.update(JSON_TYPES)
    invalid = names - JSON_TYPES
    if invalid or not names:
        rendered = ", ".join(sorted(invalid)) or "an empty type"
        raise ContractParseError(f"{label}: invalid data-type {rendered}")
    return frozenset(names)


def _field_name(raw: _RawField, label: str) -> str:
    text = "".join(raw.parts).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractParseError(
            f"{label}: response field marker must contain one JSON string"
        ) from exc
    if not isinstance(value, str):
        raise ContractParseError(
            f"{label}: response field marker must contain one JSON string"
        )
    return value


def _response_contract(
    block: _ResponseBlock, operation: str
) -> tuple[frozenset[str], dict[str, FieldContract]]:
    try:
        body = parse_json_example("".join(block.parts))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ContractParseError(
            f"{operation}: malformed JSON response example: {exc}"
        ) from exc

    occurrences = _object_fields(body)
    if len(occurrences) != len(block.fields):
        raise ContractParseError(
            f"{operation}: response example has {len(occurrences)} object keys but "
            f"{len(block.fields)} annotated field markers"
        )

    fields: dict[str, FieldContract] = {}
    for (path, name, value), raw in zip(occurrences, block.fields, strict=True):
        marked_name = _field_name(raw, f"{operation} {path}")
        if marked_name != name:
            raise ContractParseError(
                f"{operation} {path}: field marker names {marked_name!r}, "
                f"expected {name!r}"
            )
        if raw.nullable not in {"true", "false"}:
            raise ContractParseError(f"{operation} {path}: missing data-nullable")
        inferred = _json_type(value)
        if value is None or value == [] or value == {}:
            if not raw.declared_type:
                raise ContractParseError(
                    f"{operation} {path}: null or empty values require data-type"
                )
            types = _declared_types(raw.declared_type, f"{operation} {path}")
        elif raw.declared_type:
            types = _declared_types(raw.declared_type, f"{operation} {path}")
            if inferred not in types:
                raise ContractParseError(
                    f"{operation} {path}: example is {inferred}, outside data-type "
                    f"{_format_types(types)}"
                )
        else:
            types = frozenset({inferred})
        current = FieldContract(types=types, nullable=raw.nullable == "true")
        previous = fields.get(path)
        if previous is not None and previous != current:
            raise ContractParseError(
                f"{operation} {path}: repeated examples have inconsistent annotations"
            )
        fields[path] = current
    return frozenset({_json_type(body)}), fields


def _parse_response_kinds(raw: str | None, label: str) -> frozenset[str]:
    if not raw:
        raise ContractParseError(f"{label}: missing data-response-kind")
    kinds = {part for part in re.split(r"[\s,]+", raw.strip().lower()) if part}
    invalid = kinds - RESPONSE_KINDS
    if invalid:
        raise ContractParseError(
            f"{label}: invalid response kind {', '.join(sorted(invalid))}"
        )
    if "empty" in kinds and len(kinds) != 1:
        raise ContractParseError(
            f"{label}: empty cannot be combined with another response kind"
        )
    return frozenset(kinds)


def parse_reference(html: str) -> list[EndpointContract]:
    parser = _ReferenceParser()
    parser.feed(html)
    parser.close()
    errors = list(parser.errors)
    endpoints: list[EndpointContract] = []
    seen: dict[tuple[str, str], str] = {}

    for builder in parser.builders:
        line_label = f"line {builder.line}"
        method = (builder.method or "").lower()
        path = builder.path or ""
        if method not in HTTP_METHODS:
            errors.append(f"{line_label}: missing or invalid endpoint method")
            continue
        if not path:
            errors.append(f"{line_label}: missing endpoint data-path")
            continue
        operation = f"{method.upper()} {path}"
        try:
            status = int(builder.success_status or "")
        except ValueError:
            errors.append(f"{operation}: missing or invalid data-success-status")
            continue
        if not 200 <= status < 300:
            errors.append(f"{operation}: success status must be in the 2xx range")
            continue
        try:
            kinds = _parse_response_kinds(builder.response_kind, operation)
        except ContractParseError as exc:
            errors.extend(exc.errors)
            continue

        normalized = normalize_path(path)
        key = (method, normalized)
        if key in seen:
            errors.append(
                f"duplicate documented operation {operation}; first declared as "
                f"{seen[key]}"
            )
            continue
        seen[key] = operation

        root_types: frozenset[str] = frozenset()
        fields: dict[str, FieldContract] = {}
        json_blocks = [
            response for response in builder.responses if response.kind == "json"
        ]
        if "json" in kinds:
            if len(json_blocks) != 1:
                errors.append(
                    f"{operation}: JSON operations require exactly one "
                    'pre[data-response-body="json"]'
                )
                continue
            try:
                root_types, fields = _response_contract(json_blocks[0], operation)
            except ContractParseError as exc:
                errors.extend(exc.errors)
                continue
        elif json_blocks:
            errors.append(
                f"{operation}: JSON response example is not declared as a response kind"
            )
            continue

        endpoints.append(
            EndpointContract(
                method=method,
                path=path,
                normalized_path=normalized,
                success_status=status,
                response_kinds=kinds,
                root_types=root_types,
                fields=fields,
            )
        )

    if errors:
        raise ContractParseError(errors)
    return endpoints


@dataclass
class _SchemaNode:
    types: frozenset[str] = frozenset()
    nullable: bool = False
    properties: dict[str, "_SchemaNode"] = field(default_factory=dict)
    items: "_SchemaNode | None" = None


class _SchemaResolver:
    def __init__(self, document: dict[str, Any]):
        self.document = document

    def resolve(
        self, schema: Any, seen_refs: frozenset[str] = frozenset()
    ) -> _SchemaNode:
        if not isinstance(schema, dict):
            return _SchemaNode()

        nodes: list[_SchemaNode] = []
        reference = schema.get("$ref")
        if isinstance(reference, str):
            if reference in seen_refs:
                nodes.append(_SchemaNode(types=JSON_TYPES, nullable=True))
            else:
                nodes.append(
                    self.resolve(
                        self._dereference(reference), seen_refs | {reference}
                    )
                )

        own = {key: value for key, value in schema.items() if key != "$ref"}
        branches = own.pop("anyOf", None) or own.pop("oneOf", None)
        if isinstance(branches, list):
            nodes.append(
                self._union([self.resolve(branch, seen_refs) for branch in branches])
            )

        combined = own.pop("allOf", None)
        if isinstance(combined, list):
            nodes.append(
                self._intersection(
                    [self.resolve(branch, seen_refs) for branch in combined]
                )
            )

        if own:
            nodes.append(self._direct(own, seen_refs))
        if not nodes:
            return _SchemaNode()
        return self._intersection(nodes)

    def _direct(
        self, schema: dict[str, Any], seen_refs: frozenset[str]
    ) -> _SchemaNode:
        raw_type = schema.get("type")
        if isinstance(raw_type, str):
            types = {raw_type}
        elif isinstance(raw_type, list):
            types = {value for value in raw_type if isinstance(value, str)}
        else:
            types = set()
        nullable = bool(schema.get("nullable")) or "null" in types
        types.discard("null")
        properties = {
            name: self.resolve(child, seen_refs)
            for name, child in (schema.get("properties") or {}).items()
        }
        items = (
            self.resolve(schema["items"], seen_refs)
            if isinstance(schema.get("items"), dict)
            else None
        )
        if properties and not types:
            types.add("object")
        if items is not None and not types:
            types.add("array")
        return _SchemaNode(
            types=frozenset(types & JSON_TYPES),
            nullable=nullable,
            properties=properties,
            items=items,
        )

    def _union(self, nodes: list[_SchemaNode]) -> _SchemaNode:
        types: set[str] = set()
        properties: dict[str, _SchemaNode] = {}
        items: list[_SchemaNode] = []
        nullable = False
        for node in nodes:
            types.update(node.types)
            nullable = nullable or node.nullable
            for name, child in node.properties.items():
                if name in properties:
                    properties[name] = self._union([properties[name], child])
                else:
                    properties[name] = child
            if node.items is not None:
                items.append(node.items)
        return _SchemaNode(
            types=frozenset(types),
            nullable=nullable,
            properties=properties,
            items=self._union(items) if items else None,
        )

    def _intersection(self, nodes: list[_SchemaNode]) -> _SchemaNode:
        meaningful = [
            node
            for node in nodes
            if node.types or node.nullable or node.properties or node.items is not None
        ]
        if not meaningful:
            return _SchemaNode()
        types: set[str] = set()
        properties: dict[str, _SchemaNode] = {}
        items: list[_SchemaNode] = []
        for node in meaningful:
            if not types:
                types.update(node.types)
            elif node.types:
                overlap = types & set(node.types)
                types = overlap or (types | set(node.types))
            for name, child in node.properties.items():
                if name in properties:
                    properties[name] = self._intersection([properties[name], child])
                else:
                    properties[name] = child
            if node.items is not None:
                items.append(node.items)
        return _SchemaNode(
            types=frozenset(types),
            nullable=all(node.nullable for node in meaningful),
            properties=properties,
            items=self._intersection(items) if items else None,
        )

    def _dereference(self, reference: str) -> Any:
        if not reference.startswith("#/"):
            raise ValueError(
                f"external OpenAPI reference is not supported: {reference}"
            )
        value: Any = self.document
        for raw_part in reference[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            try:
                value = value[part]
            except (KeyError, TypeError) as exc:
                raise ValueError(f"unresolved OpenAPI reference: {reference}") from exc
        return value


def _schema_fields(
    node: _SchemaNode, prefix: str = "response"
) -> dict[str, FieldContract]:
    fields: dict[str, FieldContract] = {}
    for name, child in node.properties.items():
        path = f"{prefix}.{name}"
        fields[path] = FieldContract(types=child.types, nullable=child.nullable)
        fields.update(_schema_fields(child, path))
    if node.items is not None:
        fields.update(_schema_fields(node.items, f"{prefix}[]"))
    return fields


def _response_kinds(response: dict[str, Any]) -> frozenset[str]:
    content = response.get("content")
    if not isinstance(content, dict) or not content:
        return frozenset({"empty"})
    kinds: set[str] = set()
    for media_type, media in content.items():
        lowered = media_type.lower()
        schema = media.get("schema", {}) if isinstance(media, dict) else {}
        if lowered == "application/json" or lowered.endswith("+json"):
            kinds.add("json")
        elif lowered == "text/event-stream":
            kinds.add("sse")
        elif isinstance(schema, dict) and schema.get("format") == "binary":
            kinds.add("binary")
        elif lowered.startswith("text/"):
            kinds.add("text")
        else:
            kinds.add("binary")
    return frozenset(kinds)


def _openapi_operations(
    document: dict[str, Any],
) -> dict[tuple[str, str], tuple[str, dict[str, Any]]]:
    operations: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    for path, path_item in document.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            key = (method, normalize_path(path))
            if key in operations:
                previous = operations[key][0]
                raise ValueError(
                    f"OpenAPI has duplicate normalized operations {method.upper()} "
                    f"{previous} and {path}"
                )
            operations[key] = (path, operation)
    return operations


def _success_statuses(operation: dict[str, Any]) -> list[int]:
    statuses: list[int] = []
    for raw_status in operation.get("responses", {}):
        try:
            status = int(raw_status)
        except (TypeError, ValueError):
            continue
        if 200 <= status < 300:
            statuses.append(status)
    return sorted(statuses)


def _format_types(types: Iterable[str]) -> str:
    return " | ".join(sorted(types)) or "unknown"


def _format_kinds(kinds: Iterable[str]) -> str:
    return " + ".join(sorted(kinds)) or "none"


def _compare_endpoints(
    endpoints: list[EndpointContract], openapi: dict[str, Any]
) -> list[str]:
    operations = _openapi_operations(openapi)
    resolver = _SchemaResolver(openapi)
    errors: list[str] = []

    for endpoint in endpoints:
        operation_label = f"{endpoint.method.upper()} {endpoint.path}"
        indexed = operations.get((endpoint.method, endpoint.normalized_path))
        if indexed is None:
            errors.append(
                f"{operation_label}: documented operation is absent from OpenAPI"
            )
            continue
        _, operation = indexed
        statuses = _success_statuses(operation)
        response = operation.get("responses", {}).get(str(endpoint.success_status))
        if not isinstance(response, dict):
            errors.append(
                f"{operation_label}: docs say success status "
                f"{endpoint.success_status}, "
                f"OpenAPI has {', '.join(map(str, statuses)) or 'no 2xx response'}"
            )
            continue

        openapi_kinds = _response_kinds(response)
        if endpoint.response_kinds != openapi_kinds:
            errors.append(
                f"{operation_label}: docs say response kind "
                f"{_format_kinds(endpoint.response_kinds)}, OpenAPI says "
                f"{_format_kinds(openapi_kinds)}"
            )
            continue
        if "json" not in endpoint.response_kinds:
            continue

        media = response.get("content", {}).get("application/json", {})
        raw_schema = media.get("schema", {}) if isinstance(media, dict) else {}
        node = resolver.resolve(raw_schema)
        if not node.types and not node.properties and node.items is None:
            errors.append(
                f"{operation_label}: OpenAPI success response has an empty JSON schema"
            )
            continue
        if endpoint.root_types != node.types:
            errors.append(
                f"{operation_label} response: docs say "
                f"{_format_types(endpoint.root_types)}, "
                f"OpenAPI says {_format_types(node.types)}"
            )

        openapi_fields = _schema_fields(node)
        for path in sorted(set(endpoint.fields) | set(openapi_fields)):
            documented = endpoint.fields.get(path)
            advertised = openapi_fields.get(path)
            if documented is None:
                errors.append(
                    f"{operation_label} {path}: OpenAPI field is absent from docs"
                )
                continue
            if advertised is None:
                errors.append(
                    f"{operation_label} {path}: documented field is absent from OpenAPI"
                )
                continue
            if documented.types != advertised.types:
                errors.append(
                    f"{operation_label} {path}: docs say "
                    f"{_format_types(documented.types)}, "
                    f"OpenAPI says {_format_types(advertised.types)}"
                )
            if documented.nullable != advertised.nullable:
                docs_value = "nullable" if documented.nullable else "non-nullable"
                openapi_value = "nullable" if advertised.nullable else "non-nullable"
                errors.append(
                    f"{operation_label} {path}: docs say {docs_value}, "
                    f"OpenAPI says {openapi_value}"
                )
    return errors


def compare_api_reference(html: str, openapi: dict[str, Any]) -> list[str]:
    return _compare_endpoints(parse_reference(html), openapi)


def load_openapi(core_api: Path) -> dict[str, Any]:
    if not core_api.is_dir():
        raise FileNotFoundError(
            f"Cowork server submodule is unavailable at {core_api}; "
            "initialize backend/core_api"
        )
    sys.path.insert(0, str(core_api))
    try:
        server = importlib.import_module("cowork.server")
        return server.create_app().openapi()
    finally:
        sys.path.remove(str(core_api))


def main(argv: list[str] | None = None) -> int:
    repository = Path(__file__).resolve().parents[1]
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument(
        "--html", type=Path, default=repository / "docs" / "api.html"
    )
    argument_parser.add_argument(
        "--core-api", type=Path, default=repository / "backend" / "core_api"
    )
    args = argument_parser.parse_args(argv)

    try:
        html = args.html.read_text(encoding="utf-8")
        openapi = load_openapi(args.core_api.resolve())
        endpoints = parse_reference(html)
        errors = _compare_endpoints(endpoints, openapi)
    except (ContractParseError, OSError, ImportError, ValueError) as exc:
        print(f"api-reference: {exc}", file=sys.stderr)
        return 1

    if errors:
        for error in errors:
            print(f"api-reference: {error}", file=sys.stderr)
        return 1
    print(f"API reference matches OpenAPI ({len(endpoints)} operations checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
