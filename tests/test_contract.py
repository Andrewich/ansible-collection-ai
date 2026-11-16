"""Contract tests for the llm_extract skeleton. No model, no Ansible runtime needed.

    .venv/bin/python -m unittest discover -s tests -p 'test_contract.py' -v
"""
from __future__ import print_function

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODULE_PATH = os.path.join(ROOT, "plugins", "modules", "llm_extract.py")
sys.path.insert(0, os.path.join(ROOT, "plugins", "module_utils"))

import llm_core as L  # noqa: E402

FIELDS = [
    {"name": "serial", "type": "string", "description": "serial number"},
    {"name": "memory_gib", "type": "integer", "description": "memory in GiB"},
    {"name": "health", "type": "string", "enum": ["OK", "Warning"], "description": "health"},
    {"name": "nics", "type": "list", "description": "MAC addresses"},
]


def params(**overrides):
    base = dict(
        text="SerialNumber: 7XK2P13\nMemory: 512 GiB\nHealth OK\n",
        src=None,
        prompt="Extract fields.",
        prompt_file=None,
        fields=FIELDS,
        json_schema=None,
        result_key="llm_result",
        model_path=None,
        model_id=None,
        backend="mock", allow_mock=True,
        server_url="http://127.0.0.1:8080",
        max_input_chars=1200,
        on_too_long="fail",
        merge="first",
        on_missing="allow",
        on_invalid="retry",
        grounding="substring",
        on_ungrounded="drop",
        min_grounded=0.0,
        strict_enum=False,
        cache=True,
        cache_dir=None,
        temperature=0.0,
        seed=0,
        max_tokens=256,
        retries=1,
        n_ctx=4096,
        n_threads=0,
        timeout=120,
        mock_response=None,
        debug=False,
    )
    base.update(overrides)
    return base


class FieldsSchemaTest(unittest.TestCase):
    def test_shorthand_produces_closed_nullable_object(self):
        schema = L.fields_to_schema(FIELDS)
        self.assertEqual(schema["type"], "object")
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(sorted(schema["required"]), ["health", "memory_gib", "nics", "serial"])
        self.assertIn(None, schema["properties"]["health"]["enum"])
        self.assertEqual(schema["properties"]["nics"]["type"], ["array", "null"])
        self.assertEqual(schema["properties"]["nics"]["maxItems"], 8)

    def test_bad_field_is_rejected(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.fields_to_schema([{"name": "x", "type": "date"}])
        self.assertEqual(ctx.exception.code, "schema_invalid")

    def test_empty_fields_is_rejected(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.fields_to_schema([])
        self.assertEqual(ctx.exception.code, "schema_invalid")


class SchemaSubsetTest(unittest.TestCase):
    def test_generated_schema_is_supported(self):
        summary = L.validate_schema(L.fields_to_schema(FIELDS))
        self.assertEqual(summary["kind"], "object")
        self.assertEqual(sorted(summary["properties"]), ["health", "memory_gib", "nics", "serial"])

    def test_additional_properties_must_be_false(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_schema(schema)
        self.assertEqual(ctx.exception.code, "schema_unsupported")

    def test_unsupported_keywords_are_rejected_loudly(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["a"],
            "properties": {"a": {"type": "string", "patternProperties": {}}},
        }
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_schema(schema)
        self.assertEqual(ctx.exception.code, "schema_unsupported")
        self.assertIn("patternProperties", str(ctx.exception))

    def test_arrays_of_objects_are_not_silently_accepted(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["rows"],
            "properties": {"rows": {"type": ["array", "null"],
                                    "items": {"type": "object", "additionalProperties": False,
                                              "properties": {"a": {"type": "string"}}}}},
        }
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_schema(schema)
        self.assertEqual(ctx.exception.code, "schema_unsupported")

    def test_required_must_exist_in_properties(self):
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["b"], "properties": {"a": {"type": "string"}}}
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_schema(schema)
        self.assertEqual(ctx.exception.code, "schema_invalid")


class PreflightTest(unittest.TestCase):
    def test_happy_path_returns_plan(self):
        plan, _schema = L.preflight(params())
        self.assertEqual(plan["text"]["source"], "text")
        self.assertTrue(plan["budget"]["fits"])
        self.assertEqual(plan["budget"]["max_input_chars"], 1200)
        self.assertEqual(plan["schema"]["kind"], "object")
        self.assertTrue(plan["cache"]["key"])
        self.assertEqual(plan["inference"]["attempts"], 2)
        self.assertEqual(plan["budget"]["chunks"], 1)

    def test_prompt_file_is_read_and_hashed(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        handle.write("Extract these fields.\n")
        handle.close()
        try:
            plan, _schema = L.preflight(params(prompt=None, prompt_file=handle.name))
            self.assertEqual(plan["prompt"]["source"], "file")
            self.assertEqual(plan["prompt"]["sha256"], L.sha256_text("Extract these fields.\n"))
        finally:
            os.unlink(handle.name)

    def test_missing_prompt_file(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.preflight(params(prompt=None, prompt_file="/nonexistent/prompt.txt"))
        self.assertEqual(ctx.exception.code, "prompt_not_found")

    def test_empty_prompt(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.preflight(params(prompt="   \n"))
        self.assertEqual(ctx.exception.code, "prompt_not_found")

    def test_structured_text_is_serialised_back_to_json(self):
        plan, _schema = L.preflight(params(text=[{"SerialNumber": "7XK2P13"}]))
        self.assertEqual(plan["text"]["structured_input"], "json")
        self.assertEqual(plan["text"]["chars"], len('[{"SerialNumber":"7XK2P13"}]'))
        self.assertEqual(plan["text"]["source"], "text")

    def test_src_is_read_byte_exact(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        handle.write("SerialNumber = 7XK2P13\n")
        handle.close()
        try:
            plan, _schema = L.preflight(params(text=None, src=handle.name))
            self.assertEqual(plan["text"]["source"], "src")
            self.assertIsNone(plan["text"]["structured_input"])
            self.assertEqual(plan["text"]["chars"], 23)
        finally:
            os.unlink(handle.name)

    def test_empty_text(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.preflight(params(text="  \n\t "))
        self.assertEqual(ctx.exception.code, "input_empty")

    def test_input_too_long_fails_by_default(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.preflight(params(text="x" * 1300))
        self.assertEqual(ctx.exception.code, "input_too_long")
        self.assertEqual(ctx.exception.plan["budget"]["action"], "fail")
        self.assertIn("1200", str(ctx.exception))

    def test_input_too_long_allowed_when_policy_is_explicit(self):
        plan, _schema = L.preflight(params(text="x" * 1300, on_too_long="split"))
        self.assertFalse(plan["budget"]["fits"])
        self.assertEqual(plan["budget"]["action"], "split")

    def test_model_must_exist_for_python_backend(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.preflight(params(backend="python", model_path="/nonexistent/model.gguf"))
        self.assertEqual(ctx.exception.code, "model_not_found")

    def test_mock_backend_needs_no_model(self):
        plan, _schema = L.preflight(params(backend="mock", allow_mock=True, model_path=None))
        self.assertFalse(plan["model"]["exists"])

    def test_json_schema_as_string_is_accepted(self):
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["a"], "properties": {"a": {"type": ["string", "null"]}}}
        plan, _schema = L.preflight(params(fields=None, json_schema=json.dumps(schema)))
        self.assertEqual(plan["schema"]["properties"], ["a"])


class CacheKeyTest(unittest.TestCase):
    def test_key_is_stable(self):
        first = L.preflight(params())[0]["cache"]["key"]
        second = L.preflight(params())[0]["cache"]["key"]
        self.assertEqual(first, second)

    def test_key_depends_on_prompt_text_and_sampling(self):
        base = L.preflight(params())[0]["cache"]["key"]
        self.assertNotEqual(base, L.preflight(params(prompt="Other."))[0]["cache"]["key"])
        self.assertNotEqual(base, L.preflight(params(temperature=0.7))[0]["cache"]["key"])
        self.assertNotEqual(base, L.preflight(params(text="different"))[0]["cache"]["key"])


class ContractSurfaceTest(unittest.TestCase):
    def test_module_version_is_recorded(self):
        self.assertEqual(L.preflight(params())[0]["module"], L.CORE_VERSION)

    def test_documented_error_codes_are_complete(self):
        for code in ("input_empty", "input_too_long", "prompt_not_found", "schema_invalid",
                     "schema_unsupported", "model_not_found", "invalid_json"):
            self.assertIn(code, L.ERROR_CODES)

    def test_documentation_is_valid_yaml(self):
        import ast

        import yaml

        source = open(MODULE_PATH, encoding="utf-8").read()
        constants = {}
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in ("DOCUMENTATION", "EXAMPLES", "RETURN"):
                    constants[name] = ast.literal_eval(node.value)
        doc = yaml.safe_load(constants["DOCUMENTATION"])
        self.assertEqual(doc["module"], "llm_extract")
        for name in ("text", "src", "prompt_file", "fields", "json_schema", "result_key",
                     "max_input_chars", "on_too_long", "backend", "retries", "grounding"):
            self.assertIn(name, doc["options"])
        self.assertEqual(doc["options"]["result_key"]["default"], "llm_result")
        self.assertEqual(doc["options"]["max_input_chars"]["default"], 1200)
        self.assertEqual(doc["options"]["max_tokens"]["default"], 320)
        retries_doc = " ".join(doc["options"]["retries"]["description"])
        self.assertIn("0.1", retries_doc)
        yaml.safe_load(constants["EXAMPLES"])
        yaml.safe_load(constants["RETURN"])


if __name__ == "__main__":
    unittest.main()
