"""Convert MCP tool input schemas (JSON Schema) into Gemini function-declaration parameters.

Gemini accepts an OpenAPI 3.0 subset: no $ref/$defs, no additionalProperties, no type arrays,
`nullable` instead of `anyOf [X, null]`, and enums of strings only.
"""

from __future__ import annotations

from typing import Any

_TYPE_MAP = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}
_PASSTHROUGH = ("minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength", "pattern")


def _resolve_ref(ref: str, defs: dict[str, Any]) -> dict[str, Any]:
    name = ref.rsplit("/", 1)[-1]
    if name not in defs:
        raise ValueError(f"unresolvable $ref {ref!r}")
    return defs[name]


def _convert(node: dict[str, Any], defs: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    if depth > 20:
        raise ValueError("schema nesting too deep (recursive $ref?)")
    if "$ref" in node:
        merged = {**_resolve_ref(node["$ref"], defs), **{k: v for k, v in node.items() if k != "$ref"}}
        return _convert(merged, defs, depth + 1)

    nullable = False
    for key in ("anyOf", "oneOf"):
        if key in node:
            variants = [v for v in node[key] if v.get("type") != "null"]
            nullable = len(variants) < len(node[key])
            # Gemini has no union types: keep the first concrete variant.
            base = {k: v for k, v in node.items() if k != key}
            node = {**variants[0], **base} if variants else {**base, "type": "string"}

    json_type = node.get("type", "string")
    if isinstance(json_type, list):
        nullable = nullable or "null" in json_type
        json_type = next((t for t in json_type if t != "null"), "string")

    out: dict[str, Any] = {"type": _TYPE_MAP.get(json_type, "STRING")}

    description = node.get("description") or ""
    if "default" in node and node["default"] is not None:
        description = f"{description} (default: {node['default']})".strip()
    if description:
        out["description"] = description
    if nullable:
        out["nullable"] = True
    if "enum" in node:
        values = [v for v in node["enum"] if v is not None]
        if json_type == "string":
            out["enum"] = [str(v) for v in values]
        else:
            out["description"] = f"{out.get('description', '')} Allowed values: {values}".strip()
    for key in _PASSTHROUGH:
        if key in node:
            out[key] = node[key]

    if json_type == "array":
        out["items"] = _convert(node.get("items") or {"type": "string"}, defs, depth + 1)
    elif json_type == "object":
        props = node.get("properties") or {}
        if props:
            out["properties"] = {name: _convert(sub, defs, depth + 1) for name, sub in props.items()}
            required = [r for r in node.get("required", []) if r in props]
            if required:
                out["required"] = required
    return out


def to_gemini_parameters(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return Gemini `parameters` for a tool, or None when the tool takes no arguments."""
    if not schema or not schema.get("properties"):
        return None
    return _convert(schema, schema.get("$defs") or schema.get("definitions") or {})
