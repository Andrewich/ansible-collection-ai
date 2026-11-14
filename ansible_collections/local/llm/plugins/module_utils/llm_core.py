"""Core of the ``local.llm`` collection: unstructured text -> JSON via a local GGUF model.

Deliberately free of Ansible imports so that the module, the lookup plugin and the unit
tests share exactly one implementation.

Design rules that were paid for with measurements and review findings:

* a nullable field MUST be able to come back as ``null`` - otherwise the model is forced
  to invent a value, and no downstream check can tell the difference;
* grounding answers "is this value in this field", not "does this token exist somewhere",
  so it uses token boundaries and the pre-normalisation form of the value;
* every quality gate is applied after a cache hit too, never only before a cache write;
* a retry changes something (temperature and token budget), otherwise it is a no-op at
  temperature 0 and a fixed seed.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import hashlib
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

CORE_VERSION = "llm_extract/2.1"
FIELD_TYPES = ("string", "integer", "number", "boolean", "list")
SCALAR_TYPES = ("string", "integer", "number", "boolean")
FIELD_KEYS = frozenset(["name", "type", "description", "enum"])
MAX_STRING_CHARS = 120
MAX_ARRAY_ITEMS = 8

ERROR_CODES = (
    "input_empty",
    "input_too_long",
    "prompt_not_found",
    "schema_invalid",
    "schema_unsupported",
    "model_not_found",
    "backend_unavailable",
    "invalid_json",
    "schema_violation",
    "missing_fields",
    "grounding_failed",
    "ungrounded_values",
    "input_not_found",
    "input_unreadable",
    "input_encoding",
    "invalid_server_url",
    "mock_not_allowed",
    "invalid_params",
    "timeout",
)

ALLOWED_KEYWORDS = frozenset(
    [
        "type",
        "properties",
        "required",
        "additionalProperties",
        "description",
        "title",
        "enum",
        "maxLength",
        "items",
        "maxItems",
        "minimum",
        "maximum",
        "default",
    ]
)

_WORD = "0-9A-Za-z\u0410-\u044f\u0401\u0451_"

CHOICES = {
    "backend": ("python", "server", "mock"),
    "on_too_long": ("fail", "truncate", "split"),
    "merge": ("first", "concat", "deep_merge"),
    "on_missing": ("allow", "fail"),
    "on_invalid": ("retry", "fail", "return_raw"),
    "grounding": ("substring", "none"),
    "on_ungrounded": ("drop", "keep", "fail"),
}

INT_PARAMS = {
    "max_input_chars": 1,
    "max_tokens": 1,
    "n_ctx": 1,
    "timeout": 1,
    "retries": 0,
    "n_threads": 0,
}

BOOL_PARAMS = ("strict_enum", "cache", "debug", "allow_mock")

KNOWN_PARAMS = frozenset(
    [
        "text", "src", "prompt", "prompt_file", "fields", "json_schema", "result_key",
        "model_path", "model_id", "backend", "server_url", "max_input_chars",
        "on_too_long", "merge", "on_missing", "on_invalid", "grounding", "on_ungrounded",
        "min_grounded", "strict_enum", "cache", "cache_dir", "temperature", "seed",
        "max_tokens", "retries", "n_ctx", "n_threads", "timeout", "mock_response", "debug",
        "check_mode", "allow_mock",
    ]
)


def validate_params(params, strict_unknown=False):
    """One validator for both entry points: the module's argument spec and the lookup."""
    errors = []
    if strict_unknown:
        unknown = sorted(set(params) - KNOWN_PARAMS)
        if unknown:
            errors.append("unknown parameter(s): %s" % ", ".join(unknown))

    for name, allowed in CHOICES.items():
        value = params.get(name)
        if value is not None and value not in allowed:
            errors.append(
                "%s must be one of %s, got %r" % (name, ", ".join(allowed), value)
            )

    for name, minimum in INT_PARAMS.items():
        value = params.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append("%s must be an integer, got %s" % (name, type(value).__name__))
        elif value < minimum:
            errors.append("%s must be >= %d, got %d" % (name, minimum, value))

    for name in BOOL_PARAMS:
        value = params.get(name)
        if value is not None and not isinstance(value, bool):
            errors.append("%s must be a boolean, got %s" % (name, type(value).__name__))

    for name in ("temperature", "min_grounded"):
        value = params.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append("%s must be a number, got %s" % (name, type(value).__name__))
        elif name == "temperature" and value < 0:
            errors.append("temperature must be >= 0, got %r" % value)
        elif name == "min_grounded" and not 0.0 <= value <= 1.0:
            errors.append("min_grounded must be within 0..1, got %r" % value)

    for left, right in (("text", "src"), ("prompt", "prompt_file"), ("fields", "json_schema")):
        if params.get(left) is not None and params.get(right) is not None:
            errors.append("%s and %s are mutually exclusive" % (left, right))
    for group in (("text", "src"), ("prompt", "prompt_file"), ("fields", "json_schema")):
        if all(params.get(name) is None for name in group):
            errors.append("one of %s is required" % ", ".join(group))

    if params.get("fields") is not None and not isinstance(params["fields"], list):
        errors.append("fields must be a list")
    if params.get("json_schema") is not None and not isinstance(params["json_schema"], (dict, str)):
        errors.append("json_schema must be a dict or a JSON string")
    if params.get("seed") is not None and (
        isinstance(params["seed"], bool) or not isinstance(params["seed"], int)
    ):
        errors.append("seed must be an integer")

    if errors:
        raise ContractError("invalid_params", "; ".join(errors))


class ContractError(Exception):
    """A failure with a stable, documented error code."""

    def __init__(self, code, message, plan=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.plan = plan or {}


class BackendError(Exception):
    def __init__(self, code, message, plan=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.plan = plan or {}


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _collapse(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def read_text_file(path):
    """Read bytes, then decode explicitly: no newline translation, no silent replacement."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        raise ContractError("input_not_found", "input file not found: %s" % path)
    except (IOError, OSError) as exc:
        raise ContractError("input_unreadable", "cannot read input file %s: %s" % (path, exc))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(
            "input_encoding",
            "input file %s is not valid UTF-8 (byte %d): %s" % (path, exc.start, exc.reason),
        )
    return text, sha256_bytes(raw), len(raw)


def load_text(params):
    """Return (text, source, path, structured_input).

    Ansible templates convert a string that looks like JSON into a data structure before
    the module sees it, so ``uri.content`` of a Redfish endpoint arrives as a dict.
    Structured input is serialised back to compact JSON instead of being turned into a
    Python repr, which would silently corrupt the text the model reads.
    """
    if params.get("src"):
        path = params["src"]
        text, digest, size = read_text_file(path)
        return {
            "text": text, "source": "src", "path": path, "structured": None,
            "sha256": digest, "bytes": size,
        }
    value = params.get("text")
    if value is None:
        raise ContractError("input_empty", "either 'text' or 'src' must be provided")
    if isinstance(value, str):
        text, structured = value, None
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        structured = "json"
    else:
        text, structured = str(value), type(value).__name__
    raw = text.encode("utf-8")
    return {
        "text": text, "source": "text", "path": None, "structured": structured,
        "sha256": sha256_bytes(raw), "bytes": len(raw),
    }


def load_prompt(params):
    """Return (prompt, source, path, sha256)."""
    if params.get("prompt_file"):
        path = params["prompt_file"]
        try:
            prompt, _digest, _size = read_text_file(path)
        except ContractError as exc:
            raise ContractError("prompt_not_found", exc.message)
        return prompt, "file", path, sha256_text(prompt)
    prompt = params.get("prompt") or ""
    return prompt, "inline", None, sha256_text(prompt)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def fields_to_schema(fields):
    """Turn the shorthand field list into a JSON Schema object.

    Unknown field keys are rejected: ``aliases`` used to be silently ignored here, which
    promised a deterministic layer that did not exist.
    """
    if not isinstance(fields, list) or not fields:
        raise ContractError("schema_invalid", "'fields' must be a non-empty list")
    properties, required = {}, []
    for field in fields:
        if not isinstance(field, dict) or not field.get("name"):
            raise ContractError("schema_invalid", "each field needs a 'name'")
        unknown = set(field) - FIELD_KEYS
        if unknown:
            raise ContractError(
                "schema_invalid",
                "field %r: unsupported keys %s (supported: %s); a deterministic alias layer "
                "is not implemented, resolve such values before calling the module"
                % (field["name"], ", ".join(sorted(unknown)), ", ".join(sorted(FIELD_KEYS))),
            )
        name = str(field["name"])
        ftype = field.get("type") or "string"
        if ftype not in FIELD_TYPES:
            raise ContractError(
                "schema_invalid",
                "field %r: unsupported type %r (allowed: %s)"
                % (name, ftype, ", ".join(FIELD_TYPES)),
            )
        if ftype == "list":
            spec = {"type": ["array", "null"], "items": {"type": "string"},
                    "maxItems": MAX_ARRAY_ITEMS}
        else:
            spec = {"type": [ftype, "null"]}
        if field.get("description"):
            spec["description"] = str(field["description"])
        if field.get("enum"):
            if ftype != "string":
                raise ContractError(
                    "schema_invalid",
                    "field %r: enum is only supported for string fields; for a list put the "
                    "constraint on the items, for numbers use minimum/maximum" % name,
                )
            spec["enum"] = list(field["enum"]) + [None]
        properties[name] = spec
        required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _types_of(schema):
    declared = schema.get("type")
    declared = declared if isinstance(declared, list) else [declared]
    return [item for item in declared if item is not None]


def _is_nullable(schema):
    return "null" in _types_of(schema)


def _kind_of(schema):
    kinds = [kind for kind in _types_of(schema) if kind != "null"]
    if len(kinds) != 1:
        raise ContractError("schema_unsupported", "exactly one non-null type is required")
    return kinds[0]


def enum_entries(schema):
    """Return (values, only_null). None in an enum list means 'may be null'."""
    raw = schema.get("enum")
    if raw is None:
        return None, False
    values = [value for value in raw if value is not None]
    return values, (not values and None in raw)


def _as_nullable(schema):
    """Return a copy of the schema that also accepts null (for optional properties)."""
    if _is_nullable(schema):
        return schema
    copy = dict(schema)
    copy["type"] = _types_of(schema) + ["null"]
    return copy


def validate_schema(schema, path="root", root=True):
    """Enforce the grammar-friendly subset and return a summary for the plan."""
    if not isinstance(schema, dict):
        raise ContractError("schema_invalid", "%s: schema must be an object" % path)
    unknown = set(schema) - ALLOWED_KEYWORDS
    if unknown:
        raise ContractError(
            "schema_unsupported",
            "%s: keywords not supported by the grammar generator: %s"
            % (path, ", ".join(sorted(unknown))),
        )
    non_null = [kind for kind in _types_of(schema) if kind != "null"]
    if len(non_null) != 1:
        raise ContractError(
            "schema_unsupported", "%s: exactly one non-null type is required" % path
        )
    unknown_types = set(non_null) - set(SCALAR_TYPES + ("object", "array"))
    if unknown_types:
        raise ContractError(
            "schema_unsupported",
            "%s: unsupported type %s" % (path, ", ".join(sorted(unknown_types))),
        )
    kind = _kind_of(schema)
    if root and kind != "object":
        raise ContractError(
            "schema_unsupported",
            "%s: the root of the schema must be an object, got %s" % (path, kind),
        )

    if kind == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            raise ContractError("schema_invalid", "%s: object without properties" % path)
        if schema.get("additionalProperties") is not False:
            raise ContractError(
                "schema_unsupported",
                "%s: additionalProperties must be false, the grammar needs a fixed set of keys"
                % path,
            )
        required = schema.get("required") or []
        missing = [name for name in required if name not in properties]
        if missing:
            raise ContractError(
                "schema_invalid", "%s: required keys not in properties: %s" % (path, missing)
            )
        children = {}
        for name, subschema in properties.items():
            children[name] = validate_schema(subschema, "%s.%s" % (path, name), root=False)
        return {"kind": "object", "properties": sorted(properties),
                "required": list(required), "children": children}

    if kind in SCALAR_TYPES:
        if schema.get("enum") is not None:
            raw = schema["enum"]
            if not isinstance(raw, list) or not all(
                isinstance(value, str) for value in raw if value is not None
            ):
                raise ContractError(
                    "schema_invalid", "%s: enum must be a list of strings" % path
                )
            if kind != "string":
                raise ContractError(
                    "schema_unsupported",
                    "%s: enum is only supported for string fields, not %s" % (path, kind),
                )
        if schema.get("maxLength") is not None:
            limit = schema["maxLength"]
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ContractError(
                    "schema_invalid", "%s: maxLength must be a non-negative integer" % path
                )
            if limit > MAX_STRING_CHARS:
                raise ContractError(
                    "schema_unsupported",
                    "%s: maxLength above %d is not supported by the grammar"
                    % (path, MAX_STRING_CHARS),
                )
        return {"kind": kind, "nullable": _is_nullable(schema)}

    if kind == "array":
        if schema.get("enum") is not None:
            raise ContractError(
                "schema_unsupported",
                "%s: enum is not supported on arrays, put it on items instead" % path,
            )
        if schema.get("maxItems") is not None:
            limit = schema["maxItems"]
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ContractError(
                    "schema_invalid", "%s: maxItems must be a non-negative integer" % path
                )
            if limit > MAX_ARRAY_ITEMS:
                raise ContractError(
                    "schema_unsupported",
                    "%s: maxItems above %d is not supported by the grammar"
                    % (path, MAX_ARRAY_ITEMS),
                )
        items = schema.get("items") or {}
        item_summary = validate_schema(items, "%s[]" % path, root=False)
        if item_summary["kind"] not in SCALAR_TYPES:
            raise ContractError(
                "schema_unsupported",
                "%s: arrays of objects are not supported yet, restructure the prompt" % path,
            )
        return {"kind": "array", "items": item_summary["kind"]}

    raise ContractError("schema_unsupported", "%s: unsupported type %r" % (path, kind))


def brief_schema(schema, prefix=""):
    """Compact, human readable field list for the system message.

    The full JSON Schema cost 68% of the prompt tokens on the reference task; this form
    says the same thing in about 40% less, which matters for a 350M model.
    """
    lines = []
    kind = _kind_of(schema)
    if kind == "object":
        required = set(schema.get("required") or [])
        for name, subschema in (schema.get("properties") or {}).items():
            path = "%s.%s" % (prefix, name) if prefix else name
            for line in brief_schema(subschema, path):
                if name not in required:
                    line += " [optional]"
                lines.append(line)
        return lines
    if kind == "array":
        items = schema.get("items") or {"type": "string"}
        kind = "array of %s" % _kind_of(items)
        schema = dict(schema)
        schema["enum"] = items.get("enum") or schema.get("enum")
        schema["description"] = schema.get("description") or items.get("description")
    extra = []
    enum = [value for value in (schema.get("enum") or []) if value is not None]
    if enum:
        extra.append("one of: %s" % ", ".join(str(value) for value in enum))
    line = "- %s (%s)" % (prefix or "value", kind)
    if extra:
        line += "; " + "; ".join(extra)
    if schema.get("description"):
        line += "  // " + schema["description"]
    return [line]


# ---------------------------------------------------------------------------
# GBNF
# ---------------------------------------------------------------------------

_GBNF_PRIMITIVES = r"""
string ::= "\"" char{0,%d} "\""
char ::= [^"\\\x00-\x1f\x7f] | escape
escape ::= "\\" (["\\/bfnrt] | "u" hex hex hex hex)
hex ::= [0-9a-fA-F]
integer ::= "-"? ("0" | [1-9] [0-9]*)
number ::= "-"? ("0" | [1-9] [0-9]*) ("." [0-9]+)? ([eE] [-+]? [0-9]+)?
boolean ::= "true" | "false"
null ::= "null"
ws ::= [ \t\n]*
""" % MAX_STRING_CHARS


def _quoted_literal(text):
    """A GBNF literal that matches a JSON string *including* its double quotes."""
    body = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"\\"' + body + '\\""'


def build_gbnf(schema, strict_enum=False):
    """Generate a GBNF grammar from a validated schema.

    Every nullable leaf gets an explicit ``null`` alternative, otherwise the grammar makes
    "I do not know" unsayable and the model is forced to fill the field with something
    that happens to be nearby. Optional (non-required) properties are nullable by
    construction, so the grammar and ``validate_instance`` agree on what may be missing.
    """
    rules = []
    counter = [0]

    def next_name(prefix):
        counter[0] += 1
        return "%s%d" % (prefix, counter[0])

    def wrap_null(rule):
        name = next_name("n")
        rules.append("%s ::= null | %s" % (name, rule))
        return name

    def value_rule(node, path, optional=False):
        kind = _kind_of(node)
        nullable = _is_nullable(node) or optional
        if kind == "object":
            name = next_name("o")
            required = set(node.get("required") or [])
            members = []
            for key, subschema in (node.get("properties") or {}).items():
                child = value_rule(subschema, "%s.%s" % (path, key), optional=key not in required)
                members.append('%s ws ":" ws %s' % (_quoted_literal(key), child))
            rules.append('%s ::= "{" ws %s ws "}"' % (name, (' ws "," ws ').join(members)))
            return wrap_null(name) if nullable else name
        if kind == "array":
            name = next_name("a")
            child = value_rule(node.get("items") or {"type": "string"}, path + "[]")
            limit = node.get("maxItems")
            max_items = MAX_ARRAY_ITEMS if limit is None else min(limit, MAX_ARRAY_ITEMS)
            if max_items == 0:
                rules.append('%s ::= "[" ws "]"' % name)
            else:
                rules.append(
                    '%s ::= "[" ws (%s (ws "," ws %s){0,%d})? ws "]"'
                    % (name, child, child, max_items - 1)
                )
            return wrap_null(name) if nullable else name
        enum_values, only_null = enum_entries(node)
        if only_null:
            return "null"
        if enum_values and strict_enum:
            name = next_name("e")
            alternatives = " | ".join(_quoted_literal(value) for value in enum_values)
            rules.append("%s ::= %s" % (name, alternatives))
            return wrap_null(name) if nullable else name
        if kind == "string" and node.get("maxLength") is not None:
            limit = node["maxLength"]
            name = next_name("s")
            if limit == 0:
                rules.append('%s ::= "\\"\\""' % name)
            else:
                rules.append('%s ::= "\\"" char{0,%d} "\\""' % (name, limit))
            return wrap_null(name) if nullable else name
        return wrap_null(kind) if nullable else kind

    body = value_rule(schema, "$")
    return "\n".join(["root ::= %s" % body] + rules + [_GBNF_PRIMITIVES])


# ---------------------------------------------------------------------------
# post-processing
# ---------------------------------------------------------------------------


def _path_join(path, key):
    return "%s.%s" % (path, key) if path else str(key)


def coerce_leaves(data, schema, path="", out=None):
    """JSON from the model is already typed by the grammar; this fixes mock and edge cases."""
    if out is None:
        out = []
    if data is None:
        return data
    if isinstance(data, dict):
        properties = schema.get("properties") or {}
        return {
            key: coerce_leaves(value, properties.get(key) or {}, _path_join(path, key), out)
            for key, value in data.items()
        }
    if isinstance(data, list):
        items = schema.get("items") or {}
        return [coerce_leaves(value, items, "%s[]" % path, out) for value in data]

    try:
        kind = _kind_of(schema)
    except ContractError:
        return data
    original = data
    if kind == "integer" and isinstance(data, str):
        match = re.fullmatch(r"\s*([-+]?\d+)\s*", data)
        if match:
            data = int(match.group(1))
    elif kind == "number" and isinstance(data, str):
        try:
            data = float(data.strip())
        except ValueError:
            pass
    elif kind == "boolean" and isinstance(data, str):
        folded = data.strip().casefold()
        if folded in ("true", "yes", "on", "1"):
            data = True
        elif folded in ("false", "no", "off", "0"):
            data = False
    elif kind == "string" and isinstance(data, (int, float)) and not isinstance(data, bool):
        data = str(data)
    if data is not original:
        out.append({"path": path or "$", "from": original, "to": data})
    return data


def normalize_enums(data, schema, path="", out=None):
    """Map 'Dell Inc.' onto the enum value 'Dell' when exactly one enum member matches."""
    if out is None:
        out = []
    if data is None:
        return data
    if isinstance(data, dict):
        properties = schema.get("properties") or {}
        return {
            key: normalize_enums(value, properties.get(key) or {}, _path_join(path, key), out)
            for key, value in data.items()
        }
    if isinstance(data, list):
        items = schema.get("items") or {}
        return [normalize_enums(value, items, "%s[]" % path, out) for value in data]

    enum, only_null = enum_entries(schema)
    if not enum or not isinstance(data, str):
        return data
    folded = data.casefold().strip()
    for allowed in enum:
        if allowed.casefold() == folded:
            if allowed != data:
                out.append({"path": path or "$", "from": data, "to": allowed})
            return allowed
    matches = [allowed for allowed in enum if appears_in(allowed, data)]
    if len(matches) == 1:
        out.append({"path": path or "$", "from": data, "to": matches[0]})
        return matches[0]
    return data


def reject_enum_mismatches(data, schema, path="", out=None):
    """An enum value the model made up is dropped to null and reported.

    A 350M model asked for a vendor sometimes copies a neighbouring word ("Server
    manufacturer") instead of answering null. Failing the whole extraction for that is
    worse than reporting the field as missing, which is what the caller can act on.
    """
    if out is None:
        out = []
    if data is None:
        return data
    if isinstance(data, dict):
        properties = schema.get("properties") or {}
        return {
            key: reject_enum_mismatches(value, properties.get(key) or {},
                                        _path_join(path, key), out)
            for key, value in data.items()
        }
    if isinstance(data, list):
        items = schema.get("items") or {}
        return [reject_enum_mismatches(value, items, "%s[]" % path, out) for value in data]
    enum, only_null = enum_entries(schema)
    if enum and isinstance(data, str) and data not in enum:
        out.append({"path": path or "$", "value": data, "reason": "not in enum"})
        return None
    return data


def validate_instance(instance, schema, path="$", errors=None):
    """Small JSON Schema validator for the supported subset."""
    if errors is None:
        errors = []
    if instance is None:
        if not _is_nullable(schema):
            errors.append("%s: null is not allowed" % path)
        return errors
    try:
        kind = _kind_of(schema)
    except ContractError:
        return errors

    if kind == "object":
        if not isinstance(instance, dict):
            errors.append("%s: expected object, got %s" % (path, type(instance).__name__))
            return errors
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        for name in required:
            if name not in instance:
                errors.append("%s: missing required key %r" % (path, name))
        for name, value in instance.items():
            if name not in properties:
                if schema.get("additionalProperties") is False:
                    errors.append("%s: unexpected key %r" % (path, name))
                continue
            subschema = properties[name]
            if name not in required:
                subschema = _as_nullable(subschema)
            validate_instance(value, subschema, _path_join(path, name), errors)
        return errors

    if kind == "array":
        if not isinstance(instance, list):
            errors.append("%s: expected array, got %s" % (path, type(instance).__name__))
            return errors
        if schema.get("maxItems") is not None and len(instance) > int(schema["maxItems"]):
            errors.append("%s: more than %s items" % (path, schema["maxItems"]))
        for index, value in enumerate(instance):
            validate_instance(value, schema.get("items") or {}, "%s[%d]" % (path, index), errors)
        return errors

    expected = {"string": str, "integer": int, "number": (int, float), "boolean": bool}[kind]
    if isinstance(instance, bool) and kind != "boolean":
        errors.append("%s: expected %s, got boolean" % (path, kind))
        return errors
    if not isinstance(instance, expected):
        errors.append("%s: expected %s, got %s" % (path, kind, type(instance).__name__))
        return errors
    if kind == "string":
        if schema.get("maxLength") is not None and len(instance) > int(schema["maxLength"]):
            errors.append("%s: longer than %s" % (path, schema["maxLength"]))
        enum, only_null = enum_entries(schema)
        if only_null:
            errors.append("%s: the schema allows only null, got %r" % (path, instance))
        elif enum and instance not in enum:
            errors.append("%s: %r is not one of %s" % (path, instance, enum))
    if kind in ("integer", "number"):
        if schema.get("minimum") is not None and instance < schema["minimum"]:
            errors.append("%s: below minimum %s" % (path, schema["minimum"]))
        if schema.get("maximum") is not None and instance > schema["maximum"]:
            errors.append("%s: above maximum %s" % (path, schema["maximum"]))
    return errors


def strip_nulls(data, schema, path="", missing=None):
    """Drop null values (the grammar allows them for absent data) and collect their paths."""
    if missing is None:
        missing = []

    def walk(value, node, current):
        if value is None:
            missing.append(current or "$")
            return None
        if isinstance(value, dict):
            properties = (node or {}).get("properties") or {}
            result = {}
            for key, item in value.items():
                cleaned = walk(item, properties.get(key) or {}, _path_join(current, key))
                if cleaned is not None:
                    result[key] = cleaned
            return result
        if isinstance(value, list):
            items = (node or {}).get("items") or {}
            cleaned = [walk(item, items, "%s[]" % current) for item in value]
            cleaned = [item for item in cleaned if item is not None]
            return cleaned
        return value

    return walk(data, schema, path), missing


_BOUNDED = r"(?<![" + _WORD + r"])%s(?![" + _WORD + r"])"


def appears_in(value, haystack):
    """Token-bounded, case-insensitive search.

    Substring matching said "On" is grounded by "Device = Only" and 536 by 1536, which is
    exactly the kind of false confidence grounding exists to prevent.
    """
    needle = _collapse(value).casefold()
    if not needle:
        return False
    return re.search(_BOUNDED % re.escape(needle), _collapse(haystack).casefold()) is not None


def ground(data, schema, text, originals=None):
    """Return (unverified, checked, grounded).

    ``originals`` maps a path to the value before enum normalisation, so "Dell" can be
    confirmed by "Dell Inc." in the document. Booleans are not checkable and are excluded
    from the denominator instead of being counted as verified.
    """
    haystack = _collapse(text).casefold()
    originals = originals or {}
    unverified = []
    checked = [0]

    def walk(value, node, path):
        if value is None:
            return
        if isinstance(value, dict):
            properties = (node or {}).get("properties") or {}
            for key, item in value.items():
                walk(item, properties.get(key) or {}, _path_join(path, key))
            return
        if isinstance(value, list):
            items = (node or {}).get("items") or {}
            for index, item in enumerate(value):
                walk(item, items, "%s[%d]" % (path, index))
            return
        if isinstance(value, bool):
            return
        checked[0] += 1
        candidates = [value]
        if path in originals:
            candidates.append(originals[path])
        if any(appears_in(candidate, haystack) for candidate in candidates):
            return
        unverified.append({"path": path or "$", "value": value})

    walk(data, schema, "")
    return unverified, checked[0], checked[0] - len(unverified)


def drop_unverified(data, schema, unverified):
    """Remove the reported leaves and re-strip the nulls that removal creates."""
    doomed = set(item["path"] for item in unverified)
    cleaned, extra_missing = strip_nulls(drop_paths(data, schema, doomed), schema)
    return cleaned, sorted(set(extra_missing) | doomed)


def projection_schema(schema):
    """The schema of the returned facts: nulls are stripped, so nothing stays required."""
    kind = _kind_of(schema)
    result = {"type": _types_of(schema)}
    if kind == "object":
        result["properties"] = dict(
            (name, projection_schema(subschema))
            for name, subschema in (schema.get("properties") or {}).items()
        )
        result["additionalProperties"] = False
    elif kind == "array":
        result["items"] = projection_schema(schema.get("items") or {"type": "string"})
    else:
        if schema.get("enum") is not None:
            result["enum"] = schema["enum"]
        for key in ("maxLength", "minimum", "maximum"):
            if schema.get(key) is not None:
                result[key] = schema[key]
    return result


def original_values(normalized):
    return dict((item["path"], item["from"]) for item in normalized or [])


def drop_paths(data, schema, doomed, path=""):
    """Remove the leaves at ``doomed`` paths (strings as produced by ground())."""
    if data is None:
        return None
    if isinstance(data, dict):
        properties = (schema or {}).get("properties") or {}
        return dict(
            (key, drop_paths(value, properties.get(key) or {}, doomed, _path_join(path, key)))
            for key, value in data.items()
        )
    if isinstance(data, list):
        items = (schema or {}).get("items") or {}
        cleaned = [drop_paths(value, items, doomed, "%s[%d]" % (path, index))
                   for index, value in enumerate(data)]
        cleaned = [value for value in cleaned if value is not None]
        return cleaned or None
    return None if path in doomed else data


# ---------------------------------------------------------------------------
# chunking and merging
# ---------------------------------------------------------------------------


def split_text(text, max_chars, on_too_long):
    if max_chars < 1:
        raise ContractError("invalid_params", "max_input_chars must be >= 1, got %r" % max_chars)
    if len(text) <= max_chars:
        return [text]
    if on_too_long == "truncate":
        return [text[:max_chars]]
    if on_too_long != "split":
        raise ContractError(
            "input_too_long",
            "input is %d characters, the budget is %d; raise 'max_input_chars', split the "
            "input yourself or set 'on_too_long' explicitly" % (len(text), max_chars),
        )
    chunks, current, size = [], [], 0
    for line in text.splitlines(True):
        while len(line) > max_chars:  # a single huge line: hard cut
            if current:
                chunks.append("".join(current))
                current, size = [], 0
            chunks.append(line[:max_chars])
            line = line[max_chars:]
        if size + len(line) > max_chars and current:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks or [text[:max_chars]]


def _dedupe(items):
    seen, result = [], []
    for item in items:
        marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if marker not in seen:
            seen.append(marker)
            result.append(item)
    return result


def _merge_values(first, second, mode):
    """first  - first non-null value wins;
    concat - plain list concatenation, duplicates kept;
    deep_merge - recursive dictionaries and de-duplicated lists."""
    if first is None:
        return second
    if second is None:
        return first
    if isinstance(first, dict) and isinstance(second, dict):
        merged = dict(first)
        for key, value in second.items():
            merged[key] = _merge_values(merged.get(key), value, mode)
        return merged
    if mode == "deep_merge" and isinstance(first, list) and isinstance(second, list):
        return _dedupe(first + second)
    if mode == "concat" and isinstance(first, list) and isinstance(second, list):
        return first + second
    return first


def merge_results(results, mode):
    merged = {}
    for result in results:
        merged = _merge_values(merged, result, mode)
    return merged


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def default_cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "ansible-llm")


def build_cache_key(params, model_id, model_path, prompt_sha, schema_sha, input_sha):
    """Everything semantic goes into the key.

    ``backend``, ``server_url`` and ``model_id`` matter because two different servers can
    serve different models under the same file name, and ``grounding``/``on_missing``
    change what the module is allowed to return.
    """
    digest = hashlib.sha256()
    parts = [
        CORE_VERSION,
        model_id or "",
        params.get("backend") or "",
        params.get("server_url") if params.get("backend") == "server" else "",
        prompt_sha,
        schema_sha,
        input_sha,
        str(params.get("temperature")),
        str(params.get("seed")),
        str(params.get("max_tokens")),
        str(params.get("strict_enum")),
        str(params.get("on_too_long")),
        str(params.get("merge")),
        str(params.get("on_missing")),
        str(params.get("grounding")),
        str(params.get("on_ungrounded")),
        str(params.get("max_input_chars")),
        str(params.get("min_grounded")),
        str(CACHE_FORMAT),
    ]
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    if model_path and os.path.exists(model_path):
        stat = os.stat(model_path)
        digest.update(("%d:%d" % (stat.st_size, int(stat.st_mtime))).encode("utf-8"))
    return digest.hexdigest()


CACHE_FORMAT = 2


def cache_dir_usable(cache_dir):
    """The cache directory must be ours and not group/world writable."""
    try:
        if not os.path.isdir(cache_dir):
            os.makedirs(cache_dir, mode=0o700, exist_ok=True)
        stat = os.stat(cache_dir)
    except (IOError, OSError):
        return False
    if stat.st_uid != os.geteuid():
        return False
    return not stat.st_mode & 0o022


def cache_read(cache_dir, key):
    try:
        with open(os.path.join(cache_dir, key + ".json"), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (IOError, OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != CACHE_FORMAT:
        return None
    if not isinstance(payload.get("chunks"), list):
        return None
    return payload


def cache_write(cache_dir, key, payload):
    """Write through a unique 0600 file and rename it into place."""
    if not cache_dir_usable(cache_dir):
        return False
    path = os.path.join(cache_dir, key + ".json")
    handle = None
    temporary = None
    try:
        handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=cache_dir)
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            handle = None
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return True
    except (IOError, OSError):
        if handle is not None:
            os.close(handle)
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass
        return False


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------


class PythonBackend(object):
    """In-process inference through llama-cpp-python: no binary, no service."""

    def __init__(self, params):
        try:
            import llama_cpp
        except Exception as exc:
            raise BackendError(
                "backend_unavailable",
                "llama-cpp-python is not importable: %s (pip install -r scripts/requirements.txt)"
                % exc,
            )
        self._llama_cpp = llama_cpp
        self.params = params
        self._llm = None
        self._grammars = {}

    def _load(self):
        if self._llm is None:
            kwargs = dict(
                model_path=self.params["model_path"],
                n_ctx=self.params["n_ctx"],
                verbose=False,
            )
            if self.params.get("n_threads"):
                kwargs["n_threads"] = self.params["n_threads"]
            self._llm = self._llama_cpp.Llama(**kwargs)
        return self._llm

    def _grammar(self, gbnf):
        if gbnf not in self._grammars:
            self._grammars[gbnf] = self._llama_cpp.LlamaGrammar.from_string(gbnf, verbose=False)
        return self._grammars[gbnf]

    def complete(self, messages, gbnf, max_tokens, temperature, seed, timeout):
        llm = self._load()
        started = time.time()
        result = llm.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            seed=seed,
            grammar=self._grammar(gbnf),
        )
        if time.time() - started > timeout:
            raise BackendError("timeout", "inference exceeded the %ss timeout" % timeout)
        choice = result["choices"][0]
        return choice["message"]["content"], choice.get("finish_reason"), result.get("usage") or {}


def is_loopback_host(host):
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, so a loopback endpoint cannot forward the document elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect refused: %s" % newurl, headers, fp
        )


class ServerBackend(object):
    """OpenAI compatible inference against llama-server or `python -m llama_cpp.server`.

    The endpoint must be loopback: the module promises that inference is local, and a
    remote URL would silently ship the document (and the prompt) over the network.
    """

    def __init__(self, params):
        self.params = params
        raw_url = params["server_url"] or ""
        parsed = urllib.parse.urlsplit(raw_url)
        if parsed.scheme not in ("http", "https"):
            raise BackendError(
                "invalid_server_url",
                "server_url must use http or https, got %r" % raw_url,
            )
        if parsed.username or parsed.password:
            raise BackendError(
                "invalid_server_url", "server_url must not contain credentials"
            )
        if not is_loopback_host(parsed.hostname):
            raise BackendError(
                "invalid_server_url",
                "server_url must point to a loopback address (127.0.0.0/8, ::1 or "
                "localhost); %r would send the document off the host" % raw_url,
            )
        self.url = raw_url.rstrip("/") + "/v1/chat/completions"
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirectHandler()
        )

    def complete(self, messages, gbnf, max_tokens, temperature, seed, timeout):
        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "seed": seed,
            "grammar": gbnf,
            "stream": False,
        }
        if self.params.get("model_id"):
            payload["model"] = self.params["model_id"]
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        context = ssl.create_default_context() if self.url.startswith("https") else None
        try:
            with self._opener.open(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except socket.timeout:
            raise BackendError("timeout", "inference exceeded the %ss timeout" % timeout)
        except urllib.error.URLError as exc:
            raise BackendError("backend_unavailable", "cannot reach %s: %s" % (self.url, exc))
        except ValueError as exc:
            raise BackendError("backend_unavailable", "malformed server response: %s" % exc)
        try:
            choice = body["choices"][0]
            return choice["message"]["content"], choice.get("finish_reason"), body.get("usage") or {}
        except (KeyError, IndexError):
            raise BackendError(
                "backend_unavailable", "unexpected server response: %s" % str(body)[:300]
            )


class MockBackend(object):
    """Canned answer for tests and CI."""

    def __init__(self, params):
        self.response = params.get("mock_response") or "{}"

    def complete(self, messages, gbnf, max_tokens, temperature, seed, timeout):
        return self.response, "stop", {}


def make_backend(params):
    backend = params["backend"]
    if backend == "mock":
        if not params.get("allow_mock"):
            raise BackendError(
                "mock_not_allowed",
                "backend 'mock' returns a canned answer, not extracted facts; set "
                "allow_mock=true explicitly for tests and demos",
            )
        return MockBackend(params)
    if backend == "python":
        return PythonBackend(params)
    if backend == "server":
        return ServerBackend(params)
    raise BackendError("backend_unavailable", "unknown backend %r" % backend)


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


def prepare(params):
    """Validate everything that can be validated without a model; return the plan."""
    validate_params(params)
    plan = {"module": CORE_VERSION, "backend": params["backend"]}

    loaded = load_text(params)
    text = loaded["text"]
    if not text.strip():
        raise ContractError("input_empty", "the input text is empty")
    plan["text"] = {
        "source": loaded["source"],
        "path": loaded["path"],
        "chars": len(text),
        "bytes": loaded["bytes"],
        "sha256": loaded["sha256"],
        "structured_input": loaded["structured"],
    }

    prompt, prompt_source, prompt_path, prompt_sha = load_prompt(params)
    if not prompt.strip():
        raise ContractError(
            "prompt_not_found",
            "the prompt is empty; pass a non-empty 'prompt_file' or 'prompt'",
        )
    plan["prompt"] = {
        "source": prompt_source,
        "path": prompt_path,
        "chars": len(prompt),
        "sha256": prompt_sha,
    }

    if params.get("json_schema") is not None:
        schema = params["json_schema"]
        if isinstance(schema, str):
            try:
                schema = json.loads(schema)
            except ValueError as exc:
                raise ContractError("schema_invalid", "json_schema is not valid JSON: %s" % exc)
    else:
        schema = fields_to_schema(params.get("fields"))
    plan["schema"] = validate_schema(schema)

    model_path = params.get("model_path")
    model_id = params.get("model_id") or (os.path.basename(model_path) if model_path else None)
    model_exists = bool(model_path) and os.path.exists(model_path)
    plan["model"] = {
        "path": model_path,
        "id": model_id,
        "exists": model_exists,
        "size": os.path.getsize(model_path) if model_exists else None,
    }

    try:
        chunks = split_text(text, int(params["max_input_chars"]), params["on_too_long"])
    except ContractError as exc:
        plan["budget"] = {
            "max_input_chars": int(params["max_input_chars"]),
            "fits": False,
            "on_too_long": params["on_too_long"],
            "action": "fail",
            "chunks": 0,
        }
        exc.plan = plan
        raise

    plan["budget"] = {
        "max_input_chars": int(params["max_input_chars"]),
        "fits": len(text) <= int(params["max_input_chars"]),
        "on_too_long": params["on_too_long"],
        "action": "proceed" if len(text) <= int(params["max_input_chars"])
        else params["on_too_long"],
        "chunks": len(chunks),
    }
    plan["inference"] = {
        "temperature": params["temperature"],
        "seed": params["seed"],
        "max_tokens": params["max_tokens"],
        "retries": params["retries"],
        "attempts": 1 + (params["retries"] if params["on_invalid"] == "retry" else 0),
        "n_ctx": params["n_ctx"],
        "n_threads": params["n_threads"],
        "timeout": params["timeout"],
        "strict_enum": params["strict_enum"],
        "grounding": params["grounding"],
        "merge": params["merge"],
        "on_invalid": params["on_invalid"],
        "on_missing": params["on_missing"],
    }
    plan["cache"] = {
        "enabled": bool(params.get("cache")),
        "dir": params.get("cache_dir") or default_cache_dir(),
        "key": build_cache_key(
            params, model_id, model_path, prompt_sha,
            json.dumps(schema, sort_keys=True), plan["text"]["sha256"],
        ),
    }

    if params["backend"] == "python":
        if not model_path:
            raise ContractError("model_not_found", "model_path is required for backend 'python'", plan)
        if not model_exists:
            raise ContractError("model_not_found", "model file not found: %s" % model_path, plan)

    return {
        "plan": plan,
        "schema": schema,
        "text": text,
        "chunks": chunks,
        "prompt": prompt,
        "prompt_sha": prompt_sha,
    }


def preflight(params):
    """Validate and return (plan, schema); used by tests and by the lookup for early errors."""
    prepared = prepare(params)
    return prepared["plan"], prepared["schema"]


def _messages(prompt, schema, text):
    """Trusted instruction and field list first, untrusted document second."""
    fields = "\n".join(brief_schema(schema))
    system = (
        "%s\n\nReturn a JSON object with exactly these fields. "
        "Use null when the text does not contain a value; never invent one. "
        "Copy values from the document; the notes after // describe a field and are "
        "never values.\n%s" % (prompt, fields)
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


def _count_leaves(data):
    if isinstance(data, dict):
        return sum(_count_leaves(value) for value in data.values())
    if isinstance(data, list):
        return sum(_count_leaves(value) for value in data)
    return 0 if data is None else 1


def _quality_gate(data, missing, schema, text, normalized, params):
    """Apply on_missing/on_ungrounded/grounding to a result, from the model or the cache."""
    if params["on_missing"] == "fail" and missing:
        raise ContractError("missing_fields", "no value for: %s" % ", ".join(missing), None)
    if params["grounding"] == "none":
        return [], 0, 0
    unverified, checked, grounded = ground(data, schema, text, original_values(normalized))
    policy = params.get("on_ungrounded") or "drop"
    if unverified and policy == "fail":
        raise ContractError(
            "ungrounded_values",
            "values not found in the source: %s"
            % ", ".join("%s=%r" % (item["path"], item["value"]) for item in unverified),
            None,
        )
    if params["min_grounded"]:
        if checked == 0:
            raise ContractError(
                "grounding_failed",
                "min_grounded=%s was requested but there is nothing to verify: every value "
                "was missing, dropped or not checkable" % params["min_grounded"],
                None,
            )
        ratio = grounded / float(checked)
        if ratio < params["min_grounded"]:
            raise ContractError(
                "grounding_failed",
                "only %.0f%% of the %d checked values were found in the source (threshold %.0f%%)"
                % (ratio * 100, checked, params["min_grounded"] * 100),
                None,
            )
    return unverified, checked, grounded


def build_envelope(parsed, schema):
    """Validate model output and turn it into the cacheable, policy-free envelope."""
    coerced, normalized, rejected = [], [], []
    data = coerce_leaves(parsed, schema, out=coerced)
    data = normalize_enums(data, schema, out=normalized)
    data = reject_enum_mismatches(data, schema, out=rejected)
    errors = validate_instance(data, schema)
    if errors:
        return None, errors
    _stripped, missing = strip_nulls(data, schema)
    return {
        "v": CACHE_FORMAT,
        "data": data,
        "missing": missing,
        "normalized": normalized,
        "coerced": coerced,
        "rejected": rejected,
    }, []


def checkable_paths(data, schema, path="", out=None):
    """Paths of leaves that grounding can actually verify (no nulls, no booleans)."""
    if out is None:
        out = []
    if data is None:
        return out
    if isinstance(data, dict):
        properties = (schema or {}).get("properties") or {}
        for key, value in data.items():
            checkable_paths(value, properties.get(key) or {}, _path_join(path, key), out)
        return out
    if isinstance(data, list):
        items = (schema or {}).get("items") or {}
        for index, value in enumerate(data):
            checkable_paths(value, items, "%s[%d]" % (path, index), out)
        return out
    if isinstance(data, bool):
        return out
    out.append(path)
    return out


def set_path(data, path, value):
    parts = path.split(".") if path else []
    current = data
    for index, part in enumerate(parts):
        name, _, bracket = part.partition("[")
        last = index == len(parts) - 1
        if name:
            if not isinstance(current, dict):
                return
            if last and not bracket:
                current[name] = value
                return
            current = current[name]
        if bracket:
            position = int(bracket.rstrip("]"))
            if last:
                current[position] = value
                return
            current = current[position]


def get_path(data, path):
    current = data
    for part in path.split(".") if path else []:
        name, _, index = part.partition("[")
        if name:
            if not isinstance(current, dict) or name not in current:
                return None
            current = current[name]
        if index:
            try:
                current = current[int(index.rstrip("]"))]
            except (IndexError, TypeError, ValueError):
                return None
    return current


def finalize_chunks(envelopes, chunks, schema, params):
    """Ground each chunk against its own text, then merge the surviving values.

    A value is accepted when the chunk that produced it also contained it. Evidence from a
    discarded tail or from another chunk cannot vouch for it, and a bad answer in chunk one
    does not evict the good answer from chunk two: grounded candidates are preferred during
    the merge, not after it.
    """
    per_chunk, grounded_parts, evidence = [], [], {}
    provenance = {"normalized": [], "coerced": [], "rejected": []}

    for envelope in envelopes:
        for key in provenance:
            provenance[key].extend(envelope.get(key) or [])

    for envelope, chunk in zip(envelopes, chunks):
        per_chunk.append(envelope.get("data"))
        chunk_data, _missing = strip_nulls(envelope.get("data"), schema)
        if params["grounding"] == "none":
            grounded_parts.append(chunk_data)
            continue
        unverified, _checked, _grounded = ground(
            chunk_data, schema, chunk, original_values(envelope.get("normalized"))
        )
        bad = set(item["path"] for item in unverified)
        for path in checkable_paths(chunk_data, schema):
            evidence[path] = evidence.get(path, False) or (path not in bad)
        cleaned, _missing_after = strip_nulls(drop_paths(chunk_data, schema, bad), schema)
        grounded_parts.append(cleaned)

    merged = merge_results(per_chunk, params["merge"])
    if params["grounding"] != "none":
        grounded_merged = merge_results(grounded_parts, params["merge"])
        for path in checkable_paths(merged, schema):
            if not evidence.get(path):
                continue
            grounded_value = get_path(grounded_merged, path)
            if grounded_value is not None:
                set_path(merged, path, grounded_value)

    paths = checkable_paths(merged, schema)
    unverified = [
        {"path": path, "value": get_path(merged, path)}
        for path in paths
        if evidence.get(path) is False
    ]
    checked = len(paths)
    grounded = checked - len(unverified)

    policy = params.get("on_ungrounded") or "drop"
    if unverified and policy == "fail":
        raise ContractError(
            "ungrounded_values",
            "values not found in the source: %s"
            % ", ".join("%s=%r" % (item["path"], item["value"]) for item in unverified),
            None,
        )

    dropped = 0
    if unverified and policy == "drop":
        doomed = set(item["path"] for item in unverified)
        data, missing = strip_nulls(drop_paths(merged, schema, doomed), schema)
        missing = sorted(set(missing) | doomed)
        dropped = len(unverified)
    else:
        data, missing = strip_nulls(merged, schema)
        missing = sorted(set(missing))

    if params["on_missing"] == "fail" and missing:
        raise ContractError("missing_fields", "no value for: %s" % ", ".join(missing), None)
    if params["min_grounded"]:
        if checked == 0:
            raise ContractError(
                "grounding_failed",
                "min_grounded=%s was requested but there is nothing to verify: every value "
                "was missing, dropped or not checkable" % params["min_grounded"],
                None,
            )
        ratio = grounded / float(checked)
        if ratio < params["min_grounded"]:
            raise ContractError(
                "grounding_failed",
                "only %.0f%% of the %d checked values were found in the source (threshold %.0f%%)"
                % (ratio * 100, checked, params["min_grounded"] * 100),
                None,
            )
    return data, {
        "missing": missing,
        "unverified": unverified,
        "checked": checked,
        "grounded": grounded,
        "dropped": dropped,
        "normalized": provenance["normalized"],
        "coerced": provenance["coerced"],
        "rejected": provenance["rejected"],
    }


def _cache_hit(cache_dir, key, schema, chunks):
    payload = cache_read(cache_dir, key)
    if payload is None:
        return None
    envelopes = payload.get("chunks")
    if not isinstance(envelopes, list) or len(envelopes) != len(chunks):
        return None
    for envelope in envelopes:
        if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), (dict, list)):
            return None
        if validate_instance(envelope["data"], schema):
            return None  # a tampered or stale entry is a miss, not an answer
    return envelopes


def extract(params):
    """Run the whole pipeline; returns (facts, metadata)."""
    started = time.time()
    prepared = prepare(params)
    plan = prepared["plan"]
    schema = prepared["schema"]
    text = prepared["text"]
    chunks = prepared["chunks"]

    meta = {
        "ok": False,
        "model": {"id": plan["model"]["id"], "path": plan["model"]["path"],
                  "size": plan["model"]["size"]},
        "prompt_sha256": prepared["prompt_sha"],
        "schema_sha256": sha256_text(json.dumps(schema, sort_keys=True)),
        "input_sha256": plan["text"]["sha256"],
        "input_chars": len(text),
        "backend": params["backend"],
        "cache_hit": False,
        "check_mode": bool(params.get("check_mode")),
        "attempts": 0,
        "chunks": len(chunks),
        "tokens": {"in": 0, "out": 0},
        "elapsed_ms": 0,
        "missing": [],
        "unverified": [],
        "coerced": [],
        "normalized": [],
        "rejected": [],
        "checked": 0,
        "grounded": 0,
        "dropped": 0,
    }

    cache_dir = plan["cache"]["dir"]
    cache_key = plan["cache"]["key"]
    if params.get("cache"):
        cached = _cache_hit(cache_dir, cache_key, schema, chunks)
        if cached is not None:
            try:
                data, info = finalize_chunks(cached, chunks, schema, params)
            except ContractError as exc:
                exc.plan = plan
                raise
            meta.update(info)
            meta.update(ok=True, cache_hit=True,
                        elapsed_ms=int((time.time() - started) * 1000))
            return data, meta

    try:
        backend = make_backend(params)
    except BackendError as exc:
        exc.plan = plan
        raise
    gbnf = build_gbnf(schema, strict_enum=params["strict_enum"])
    attempts_allowed = plan["inference"]["attempts"]

    envelopes = []
    last_raw = None
    last_errors = []
    for chunk in chunks:
        best_envelope = None
        best_parsed = None
        best_score = -1
        for attempt in range(attempts_allowed):
            # A retry has to change the sampling, otherwise it is byte-identical at
            # temperature 0 with a fixed seed and "best of N" is a lie.
            attempt_temperature = params["temperature"]
            if attempt:
                attempt_temperature = min(1.0, round(params["temperature"] + 0.1 * attempt, 2))
            try:
                raw, finish_reason, usage = backend.complete(
                    _messages(prepared["prompt"], schema, chunk),
                    gbnf,
                    min(1024, params["max_tokens"] * (2 ** attempt)),
                    attempt_temperature,
                    params["seed"] + attempt,
                    params["timeout"],
                )
            except BackendError as exc:
                exc.plan = plan
                raise
            last_raw = raw
            meta["attempts"] += 1
            meta["tokens"]["in"] += usage.get("prompt_tokens") or 0
            meta["tokens"]["out"] += usage.get("completion_tokens") or 0
            try:
                parsed = json.loads(raw)
            except ValueError:
                last_errors = ["model output is not valid JSON"]
                if finish_reason == "length":
                    last_errors.append("output was truncated by max_tokens")
                continue
            envelope, last_errors = build_envelope(parsed, schema)
            if envelope is not None:
                best_envelope = envelope
                break
            score = _count_leaves(parsed)
            if score > best_score:
                best_parsed, best_score = parsed, score
        if best_envelope is None:
            if params["on_invalid"] == "return_raw":
                meta.update(ok=False, raw_output=last_raw, errors=last_errors,
                            elapsed_ms=int((time.time() - started) * 1000))
                return {}, meta
            code = "invalid_json" if last_errors and last_errors[0].startswith("model output") \
                else "schema_violation"
            raise ContractError(code, "; ".join(last_errors) or "no parseable answer", plan)
        envelopes.append(best_envelope)

    try:
        data, info = finalize_chunks(envelopes, chunks, schema, params)
    except ContractError as exc:
        exc.plan = plan
        raise
    meta.update(info, ok=True, elapsed_ms=int((time.time() - started) * 1000))

    if params.get("debug"):
        meta["raw_output"] = last_raw
        meta["projection_schema"] = projection_schema(schema)
    if params.get("cache") and not params.get("check_mode"):
        if not cache_write(cache_dir, cache_key, {"v": CACHE_FORMAT, "chunks": envelopes}):
            meta["cache_written"] = False
    elif params.get("check_mode"):
        meta["cache_written"] = False
    return data, meta
