"""JSON Schema translation for the Messages protocol."""

import copy
import json


def strip_encrypted(schema, name_bag=False):
    if isinstance(schema, list):
        return [strip_encrypted(value) for value in schema]
    if not isinstance(schema, dict):
        return schema
    bags = {
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "dependencies",
        "dependentSchemas",
        "dependentRequired",
    }
    literals = {"const", "default", "enum", "examples"}
    return {
        key: copy.deepcopy(value)
        if not name_bag and key in literals
        else strip_encrypted(value, not name_bag and key in bags)
        for key, value in schema.items()
        if name_bag or key != "encrypted"
    }


def input_schema(schema):
    value = strip_encrypted(schema)
    value = value if isinstance(value, dict) else {}
    keys = ("oneOf", "anyOf", "allOf")
    if not any(isinstance(value.get(k), list) for k in keys):
        value["type"] = "object"
        if value.get("properties") is None:
            value["properties"] = {}
        return value
    properties = dict(value.get("properties") or {})
    required = [r for r in value.get("required", []) if isinstance(r, str)]
    for key in keys:
        for branch in value.get(key, []) if isinstance(value.get(key), list) else []:
            if not isinstance(branch, dict):
                continue
            if isinstance(branch.get("properties"), dict):
                properties.update(branch["properties"])
            if key == "allOf":
                required.extend(r for r in branch.get("required", []) if isinstance(r, str))
    result = {k: v for k, v in value.items() if k not in {*keys, "type", "properties", "required"}}
    return {
        **result,
        "type": "object",
        "properties": properties,
        **({"required": list(dict.fromkeys(required))} if required else {}),
    }


def output_schema(schema):
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)
    value, result = copy.deepcopy(schema), {}
    defs = value.pop("$defs", None)
    if isinstance(defs, dict):
        result["$defs"] = {k: output_schema(v) for k, v in defs.items()}
    if "$ref" in value:
        return {**result, "$ref": value["$ref"]}
    kind = value.pop("type", None)
    composition = next(
        (k for k in ("anyOf", "oneOf", "allOf") if isinstance(value.get(k), list)), None
    )
    if composition:
        result["anyOf" if composition == "oneOf" else composition] = [
            output_schema(v) for v in value.pop(composition)
        ]
    elif kind is None:
        raise ValueError("JSON schema requires a type or anyOf/oneOf/allOf.")
    else:
        result["type"] = kind
    for key in ("description", "title"):
        if key in value:
            result[key] = value.pop(key)
    if kind == "object":
        props = value.pop("properties", {})
        result["properties"] = (
            {k: output_schema(v) for k, v in props.items()} if isinstance(props, dict) else {}
        )
        value.pop("additionalProperties", None)
        result["additionalProperties"] = False
        if "required" in value:
            result["required"] = value.pop("required")
    elif kind == "string" and value.get("format") in {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "uri",
        "ipv4",
        "ipv6",
        "uuid",
    }:
        result["format"] = value.pop("format")
    elif kind == "array":
        if "items" in value:
            result["items"] = output_schema(value.pop("items"))
        if value.get("minItems") in {0, 1}:
            result["minItems"] = value.pop("minItems")
    if value:
        prefix = result.get("description", "")
        result["description"] = (
            (prefix + "\n\n" if isinstance(prefix, str) and prefix else "")
            + "{"
            + ", ".join(
                f"{k}: {json.dumps(v, ensure_ascii=False, separators=(',', ':'))}"
                for k, v in value.items()
            )
            + "}"
        )
    return result
