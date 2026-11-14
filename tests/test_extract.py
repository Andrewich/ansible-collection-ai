"""Pipeline tests for llm_extract: grammar, validation, grounding, cache, chunking.

Everything runs against the mock backend, so no model file is needed:

    .venv/bin/python -m unittest discover -s tests -p 'test_extract.py' -v
"""
from __future__ import print_function

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "ansible_collections", "local", "llm",
                                "plugins", "module_utils"))

import llm_core as L  # noqa: E402

FIELDS = [
    {"name": "vendor", "type": "string", "enum": ["Dell", "HPE"]},
    {"name": "serial", "type": "string"},
    {"name": "memory_gib", "type": "integer"},
    {"name": "power_state", "type": "string", "enum": ["On", "Off"]},
    {"name": "nics", "type": "list"},
]

TEXT = (
    "Manufacturer = Dell Inc.\n"
    "SerialNumber = 7XK2P13\n"
    "MemorySummary.TotalSystemMemoryGiB = 512 GB\n"
    "PowerState = On\n"
    "Nics = AA:BB:CC:DD:EE:FF, 11:22:33:44:55:66\n"
)


def params(**overrides):
    base = dict(
        text=TEXT,
        src=None,
        prompt="Extract the fields.",
        prompt_file=None,
        fields=FIELDS,
        json_schema=None,
        result_key="llm_result",
        model_path=None,
        model_id="mock-model",
        backend="mock",
        server_url="http://127.0.0.1:8080",
        max_input_chars=1200,
        on_too_long="fail",
        merge="first",
        on_missing="allow",
        on_invalid="retry",
        grounding="substring",
        on_ungrounded="keep",
        min_grounded=0.0,
        strict_enum=False,
        cache=False,
        cache_dir=None,
        temperature=0.0,
        seed=0,
        max_tokens=256,
        retries=0,
        n_ctx=4096,
        n_threads=0,
        timeout=120,
        mock_response=None,
        debug=False,
    )
    base.update(overrides)
    return base


def full(**overrides):
    """A complete answer: the grammar always emits every key, absent data becomes null."""
    payload = dict((field["name"], None) for field in FIELDS)
    payload.update(overrides)
    return json.dumps(payload)


GOOD = full(vendor="Dell", serial="7XK2P13", memory_gib=512, power_state="On",
            nics=["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"])


class GrammarTest(unittest.TestCase):
    def test_root_and_key_order(self):
        schema = L.fields_to_schema(FIELDS)
        gbnf = L.build_gbnf(schema)
        root = gbnf.splitlines()[0]
        self.assertTrue(root.startswith("root ::="))
        rule = [line for line in gbnf.splitlines() if line.startswith("o1 ::=")][0]
        positions = [
            rule.index('\\"%s\\"' % name) for name in ("vendor", "serial", "memory_gib")
        ]
        self.assertEqual(positions, sorted(positions))

    def test_string_is_capped(self):
        self.assertIn("char{0,%d}" % L.MAX_STRING_CHARS, L.build_gbnf(L.fields_to_schema(FIELDS)))

    def test_enum_is_permissive_by_default_and_strict_on_request(self):
        schema = L.fields_to_schema(FIELDS)
        self.assertNotIn('\\"Dell\\"', L.build_gbnf(schema))
        self.assertIn('\\"Dell\\"', L.build_gbnf(schema, strict_enum=True))

    def test_nested_object_and_array_grammar_parses(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["system", "disks"],
            "properties": {
                "system": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "required": ["serial"],
                    "properties": {"serial": {"type": ["string", "null"]}},
                },
                "disks": {"type": ["array", "null"], "items": {"type": "string"}, "maxItems": 4},
            },
        }
        L.validate_schema(schema)
        gbnf = L.build_gbnf(schema)
        self.assertIn("root ::=", gbnf)
        try:
            import llama_cpp
        except ImportError:
            self.skipTest("llama-cpp-python is not installed")
        llama_cpp.LlamaGrammar.from_string(gbnf, verbose=False)  # raises on a broken grammar

    def test_deeply_nested_grammar_parses(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["a"],
            "properties": {
                "a": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["b"],
                    "properties": {
                        "b": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["c"],
                            "properties": {"c": {"type": ["integer", "null"]}},
                        }
                    },
                }
            },
        }
        L.validate_schema(schema)
        gbnf = L.build_gbnf(schema)
        try:
            import llama_cpp
        except ImportError:
            self.skipTest("llama-cpp-python is not installed")
        llama_cpp.LlamaGrammar.from_string(gbnf, verbose=False)


class ExtractionTest(unittest.TestCase):
    def test_happy_path(self):
        data, meta = L.extract(params(mock_response=GOOD))
        self.assertTrue(meta["ok"])
        self.assertEqual(data["serial"], "7XK2P13")
        self.assertEqual(data["memory_gib"], 512)
        self.assertEqual(data["nics"], ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"])
        self.assertEqual(meta["missing"], [])
        self.assertEqual(meta["unverified"], [])

    def test_invalid_json_fails_with_documented_code(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(mock_response="{not json"))
        self.assertEqual(ctx.exception.code, "invalid_json")

    def test_schema_violation_is_not_silently_accepted(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(mock_response=full(serial=42, memory_gib="lots", vendor="Acme")))
        self.assertEqual(ctx.exception.code, "schema_violation")
        self.assertIn("memory_gib", str(ctx.exception))

    def test_unexpected_key_is_rejected(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(mock_response=full(serial="7XK2P13", surprise=1)))
        self.assertEqual(ctx.exception.code, "schema_violation")

    def test_null_fields_are_reported_as_missing(self):
        data, meta = L.extract(params(mock_response=full(serial="7XK2P13", memory_gib=None)))
        self.assertEqual(data, {"serial": "7XK2P13"})
        self.assertIn("memory_gib", meta["missing"])

    def test_on_missing_fail(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(mock_response=full(serial="7XK2P13"), on_missing="fail"))
        self.assertEqual(ctx.exception.code, "missing_fields")

    def test_enum_is_normalised(self):
        data, meta = L.extract(params(mock_response=full(vendor="Dell Inc.", power_state="On")))
        self.assertEqual(data["vendor"], "Dell")
        self.assertEqual(meta["normalized"][0]["from"], "Dell Inc.")

    def test_string_number_is_coerced(self):
        data, meta = L.extract(params(mock_response=full(memory_gib="512")))
        self.assertEqual(data["memory_gib"], 512)
        self.assertEqual(meta["coerced"][0]["to"], 512)

    def test_hallucination_is_reported_as_ungrounded(self):
        data, meta = L.extract(params(mock_response=full(serial="BOGUS-9999")))
        self.assertEqual(data["serial"], "BOGUS-9999")
        self.assertEqual(meta["unverified"][0]["value"], "BOGUS-9999")

    def test_min_grounded_rejects_hallucination(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(mock_response=full(serial="BOGUS-9999"), min_grounded=1.0))
        self.assertEqual(ctx.exception.code, "grounding_failed")

    def test_grounding_can_be_disabled(self):
        data, meta = L.extract(params(mock_response=full(serial="BOGUS"), grounding="none"))
        self.assertEqual(meta["unverified"], [])

    def test_return_raw_does_not_fail(self):
        data, meta = L.extract(params(mock_response="{broken", on_invalid="return_raw"))
        self.assertFalse(meta["ok"])
        self.assertEqual(data, {})
        self.assertEqual(meta["raw_output"], "{broken")

    def test_debug_exposes_raw_output(self):
        _data, meta = L.extract(params(mock_response=GOOD, debug=True))
        self.assertEqual(json.loads(meta["raw_output"])["serial"], "7XK2P13")


class CacheTest(unittest.TestCase):
    def test_second_call_hits_cache(self):
        cache_dir = tempfile.mkdtemp()
        try:
            first, meta1 = L.extract(params(mock_response=GOOD, cache=True, cache_dir=cache_dir))
            self.assertFalse(meta1["cache_hit"])
            second, meta2 = L.extract(
                params(mock_response=full(serial="CHANGED"), cache=True, cache_dir=cache_dir)
            )
            self.assertTrue(meta2["cache_hit"])
            self.assertEqual(second, first)
            self.assertEqual(meta2["attempts"], 0)
        finally:
            shutil.rmtree(cache_dir)

    def test_cache_file_is_not_world_readable(self):
        cache_dir = tempfile.mkdtemp()
        try:
            L.extract(params(mock_response=GOOD, cache=True, cache_dir=cache_dir))
            name = os.listdir(cache_dir)[0]
            mode = os.stat(os.path.join(cache_dir, name)).st_mode & 0o777
            self.assertEqual(mode, 0o600)
        finally:
            shutil.rmtree(cache_dir)

    def test_different_input_misses_cache(self):
        cache_dir = tempfile.mkdtemp()
        try:
            L.extract(params(mock_response=GOOD, cache=True, cache_dir=cache_dir))
            _data, meta = L.extract(
                params(mock_response=GOOD, cache=True, cache_dir=cache_dir, text="other text")
            )
            self.assertFalse(meta["cache_hit"])
        finally:
            shutil.rmtree(cache_dir)


class ChunkingTest(unittest.TestCase):
    def test_fail_on_too_long(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(text="x" * 50, max_input_chars=10))
        self.assertEqual(ctx.exception.code, "input_too_long")

    def test_truncate_keeps_first_part(self):
        _data, meta = L.extract(
            params(text="x" * 50, max_input_chars=10, on_too_long="truncate", mock_response=GOOD)
        )
        self.assertEqual(meta["input_chars"], 50)
        self.assertEqual(meta["chunks"], 1)

    def test_split_runs_every_chunk_and_merges(self):
        text = "\n".join("line %d of the document" % index for index in range(40))
        calls = {"count": 0}

        backend = L.MockBackend(params(mock_response=full(nics=["n1"])))
        original = backend.complete

        def counting(*args, **kwargs):
            calls["count"] += 1
            return original(*args, **kwargs)

        backend.complete = counting
        original_make = L.make_backend
        L.make_backend = lambda _params: backend
        try:
            data, meta = L.extract(
                params(text=text, max_input_chars=120, on_too_long="split",
                       merge="concat", mock_response=full(nics=["n1"]))
            )
        finally:
            L.make_backend = original_make
        self.assertGreater(meta["chunks"], 1)
        self.assertEqual(calls["count"], meta["chunks"])
        self.assertEqual(data["nics"], ["n1"])

    def test_merge_first_and_concat(self):
        self.assertEqual(
            L.merge_results([{"a": 1}, {"a": 2}], "first")["a"], 1
        )
        self.assertEqual(
            L.merge_results([{"n": ["x"]}, {"n": ["x", "y"]}], "concat")["n"], ["x", "y"]
        )
        self.assertEqual(
            L.merge_results([{"o": {"a": 1}}, {"o": {"b": 2}}], "deep_merge")["o"],
            {"a": 1, "b": 2},
        )


class ValidationTest(unittest.TestCase):
    def test_type_errors(self):
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["a"], "properties": {"a": {"type": ["integer", "null"]}}}
        self.assertEqual(L.validate_instance({"a": 1}, schema), [])
        self.assertTrue(L.validate_instance({"a": "1"}, schema))
        self.assertTrue(L.validate_instance({"a": True}, schema))
        self.assertEqual(L.validate_instance({"a": None}, schema), [])

    def test_required_and_additional(self):
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["a"], "properties": {"a": {"type": ["string", "null"]}}}
        self.assertTrue(L.validate_instance({}, schema))
        self.assertTrue(L.validate_instance({"a": "x", "b": 1}, schema))

    def test_enum_and_bounds(self):
        schema = {"type": "string", "enum": ["On", "Off", "null"]}
        self.assertEqual(L.validate_instance("On", schema), [])
        self.assertTrue(L.validate_instance("Maybe", schema))

    def test_null_where_not_allowed(self):
        schema = {"type": "string"}
        self.assertTrue(L.validate_instance(None, schema))


if __name__ == "__main__":
    unittest.main()
