"""Schema authority tests (review P0-4, P1-2 roots, P1-10 projection)."""
from __future__ import print_function

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "plugins", "module_utils"))

import llm_core as L  # noqa: E402

TEXT = "SerialNumber = 7XK2P13"


def params(**overrides):
    base = dict(
        text=TEXT, src=None, prompt="Extract.", prompt_file=None, fields=None,
        json_schema=None, result_key="llm_result", model_path=None, model_id="m",
        backend="mock", allow_mock=True, server_url="http://127.0.0.1:8080", max_input_chars=1200,
        on_too_long="fail", merge="first", on_missing="allow", on_invalid="retry",
        grounding="substring", on_ungrounded="keep", min_grounded=0.0,
        strict_enum=False, cache=False, cache_dir=None, temperature=0.0, seed=0,
        max_tokens=320, retries=0, n_ctx=4096, n_threads=0, timeout=120,
        mock_response=None, debug=False,
    )
    base.update(overrides)
    return base


def object_schema(properties, required=None):
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required if required is not None else properties),
        "properties": properties,
    }


class EnumRulesTest(unittest.TestCase):
    def test_enum_on_integer_is_rejected(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_schema(object_schema({"n": {"type": ["integer", "null"],
                                                   "enum": ["1", "2"]}}))
        self.assertEqual(ctx.exception.code, "schema_unsupported")
        self.assertIn("only supported for string", str(ctx.exception))

    def test_enum_on_array_is_rejected(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_schema(object_schema({"xs": {"type": ["array", "null"],
                                                    "items": {"type": "string"},
                                                    "enum": ["a"]}}))
        self.assertEqual(ctx.exception.code, "schema_unsupported")

    def test_enum_on_items_is_supported(self):
        schema = object_schema({"xs": {"type": ["array", "null"],
                                       "items": {"type": "string", "enum": ["a", "b", "null"]}}})
        L.validate_schema(schema)
        self.assertEqual(L.validate_instance({"xs": ["a", "b"]}, schema), [])
        self.assertTrue(L.validate_instance({"xs": ["c"]}, schema))

    def test_only_null_enum_means_only_null(self):
        schema = object_schema({"x": {"type": ["string", "null"], "enum": [None]}})
        L.validate_schema(schema)
        gbnf = L.build_gbnf(schema)
        self.assertIn('ws ":" ws null', gbnf)  # the member is null itself, not a string rule
        self.assertEqual(L.validate_instance({"x": None}, schema), [])
        self.assertTrue(L.validate_instance({"x": "anything"}, schema))


class LimitRulesTest(unittest.TestCase):
    def test_zero_max_length(self):
        schema = object_schema({"x": {"type": ["string", "null"], "maxLength": 0}})
        L.validate_schema(schema)
        self.assertTrue(L.validate_instance({"x": "a"}, schema))
        self.assertEqual(L.validate_instance({"x": ""}, schema), [])

    def test_zero_max_items(self):
        schema = object_schema({"xs": {"type": ["array", "null"],
                                       "items": {"type": "string"}, "maxItems": 0}})
        L.validate_schema(schema)
        self.assertTrue(L.validate_instance({"xs": ["a"]}, schema))
        self.assertEqual(L.validate_instance({"xs": []}, schema), [])

    def test_negative_and_oversized_limits(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_schema(object_schema({"x": {"type": "string", "maxLength": -1}}))
        self.assertEqual(ctx.exception.code, "schema_invalid")
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_schema(object_schema({"x": {"type": "string", "maxLength": 121}}))
        self.assertEqual(ctx.exception.code, "schema_unsupported")
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_schema(object_schema({"xs": {"type": "array", "items": {"type": "string"},
                                                    "maxItems": 9}}))
        self.assertEqual(ctx.exception.code, "schema_unsupported")

    def test_grammar_uses_declared_limits(self):
        schema = object_schema({
            "x": {"type": ["string", "null"], "maxLength": 3},
            "xs": {"type": ["array", "null"], "items": {"type": "string"}, "maxItems": 2},
        })
        gbnf = L.build_gbnf(schema)
        self.assertIn("char{0,3}", gbnf)
        self.assertIn("{0,1}", gbnf)


class RootTest(unittest.TestCase):
    def test_scalar_root_is_rejected_early(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_schema({"type": "string"})
        self.assertEqual(ctx.exception.code, "schema_unsupported")
        self.assertIn("root of the schema must be an object", str(ctx.exception))

    def test_extraction_reports_root_problem_in_preflight(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(json_schema={"type": "string"}, mock_response='"x"'))
        self.assertEqual(ctx.exception.code, "schema_unsupported")


class EmptyArrayTest(unittest.TestCase):
    def test_empty_array_survives_strip_nulls(self):
        fields = [{"name": "nics", "type": "list"}]
        data, meta = L.extract(params(fields=fields, mock_response='{"nics": []}'))
        self.assertEqual(data, {"nics": []})
        self.assertEqual(meta["missing"], [])

    def test_null_array_is_missing(self):
        fields = [{"name": "nics", "type": "list"}]
        data, meta = L.extract(params(fields=fields, mock_response='{"nics": null}'))
        self.assertEqual(data, {})
        self.assertEqual(meta["missing"], ["nics"])


class ProjectionTest(unittest.TestCase):
    def test_projection_relaxes_required(self):
        fields = [{"name": "a", "type": "string"}, {"name": "b", "type": "string"}]
        schema = L.fields_to_schema(fields)
        data, _meta = L.extract(params(fields=fields, mock_response='{"a": null, "b": "x"}'))
        self.assertEqual(data, {"b": "x"})
        self.assertTrue(L.validate_instance(data, schema))
        projection = L.projection_schema(schema)
        self.assertEqual(L.validate_instance(data, projection), [])

    def test_projection_keeps_types_and_enums(self):
        schema = L.fields_to_schema([{"name": "a", "type": "integer"},
                                     {"name": "b", "type": "string", "enum": ["x", "y"]}])
        projection = L.projection_schema(schema)
        self.assertEqual(projection["properties"]["a"]["type"], ["integer", "null"])
        self.assertEqual(projection["properties"]["b"]["enum"], ["x", "y", None])


class ShorthandEnumTest(unittest.TestCase):
    def test_enum_on_list_field_is_rejected(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.fields_to_schema([{"name": "xs", "type": "list", "enum": ["a"]}])
        self.assertEqual(ctx.exception.code, "schema_invalid")


if __name__ == "__main__":
    unittest.main()
