import pytest
from google.genai import types

from dockai.llm.gemini_schema import to_gemini_parameters
from dockai.server.app import mcp


def test_no_arguments_gives_no_parameters():
    assert to_gemini_parameters({"type": "object", "properties": {}}) is None
    assert to_gemini_parameters(None) is None


def test_basic_types_required_and_titles_dropped():
    out = to_gemini_parameters({
        "type": "object",
        "title": "fooArguments",
        "properties": {
            "name": {"type": "string", "title": "Name", "description": "Container name"},
            "tail": {"type": "integer", "default": 50},
            "force": {"type": "boolean"},
        },
        "required": ["name"],
        "additionalProperties": False,
    })
    assert out == {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING", "description": "Container name"},
            "tail": {"type": "INTEGER", "description": "(default: 50)"},
            "force": {"type": "BOOLEAN"},
        },
        "required": ["name"],
    }


def test_optional_anyof_null_becomes_nullable():
    out = to_gemini_parameters({
        "type": "object",
        "properties": {"port": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None}},
    })
    assert out["properties"]["port"] == {"type": "INTEGER", "nullable": True}


def test_type_list_with_null():
    out = to_gemini_parameters({"type": "object", "properties": {"x": {"type": ["string", "null"]}}})
    assert out["properties"]["x"] == {"type": "STRING", "nullable": True}


def test_arrays_and_optional_arrays():
    out = to_gemini_parameters({
        "type": "object",
        "properties": {
            "ports": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
            "bare": {"type": "array"},
        },
    })
    assert out["properties"]["ports"] == {"type": "ARRAY", "items": {"type": "STRING"}, "nullable": True}
    assert out["properties"]["bare"]["items"] == {"type": "STRING"}


def test_refs_are_inlined():
    out = to_gemini_parameters({
        "type": "object",
        "$defs": {"Mode": {"type": "string", "enum": ["fast", "safe"]}},
        "properties": {"mode": {"$ref": "#/$defs/Mode", "description": "How to run"}},
    })
    assert out["properties"]["mode"] == {"type": "STRING", "description": "How to run", "enum": ["fast", "safe"]}


def test_non_string_enum_moves_to_description():
    out = to_gemini_parameters({"type": "object", "properties": {"n": {"type": "integer", "enum": [1, 2]}}})
    assert "enum" not in out["properties"]["n"]
    assert "Allowed values: [1, 2]" in out["properties"]["n"]["description"]


def test_nested_objects_keep_only_known_required():
    out = to_gemini_parameters({
        "type": "object",
        "properties": {"opts": {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a", "ghost"]}},
    })
    assert out["properties"]["opts"]["required"] == ["a"]


def test_unresolvable_ref_raises():
    with pytest.raises(ValueError):
        to_gemini_parameters({"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}}})


async def test_every_server_tool_converts_to_a_valid_gemini_declaration():
    tools = await mcp.list_tools()
    assert {t.name for t in tools} >= {"compose_up", "compose_down", "get_logs", "list_containers"}
    for tool in tools:
        params = to_gemini_parameters(tool.input_schema)
        # Pydantic validation in google-genai rejects anything outside Gemini's schema subset.
        types.FunctionDeclaration(name=tool.name, description=tool.description, parameters=params)


async def test_compose_up_schema_shape():
    tool = next(t for t in await mcp.list_tools() if t.name == "compose_up")
    params = to_gemini_parameters(tool.input_schema)
    assert params["required"] == ["stack_name"]
    assert params["properties"]["with_adminer"] == {"type": "BOOLEAN", "nullable": True}
