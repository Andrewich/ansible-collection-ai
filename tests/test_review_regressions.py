"""Regression tests for every finding of the review.

Named after the review items so a future regression is traceable:

    .venv/bin/python -m unittest discover -s tests -p 'test_review*.py' -v
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
sys.path.insert(0, os.path.join(ROOT, "plugins", "module_utils"))

import llm_core as L  # noqa: E402

TEXT = "Manufacturer = Dell Inc.\nSerialNumber = 7XK2P13\nMemory = 512 GB\nPowerState = On\n"


def params(**overrides):
    base = dict(
        text=TEXT, src=None, prompt="Extract the fields.", prompt_file=None,
        fields=None, json_schema=None, result_key="llm_result",
        model_path=None, model_id="mock-model", backend="mock", allow_mock=True,
        server_url="http://127.0.0.1:8080", max_input_chars=1200,
        on_too_long="fail", merge="first", on_missing="allow", on_invalid="retry",
        grounding="substring", on_ungrounded="keep", min_grounded=0.0, strict_enum=False,
        cache=False, cache_dir=None, temperature=0.0, seed=0, max_tokens=320,
        retries=0, n_ctx=4096, n_threads=0, timeout=120, mock_response=None, debug=False,
    )
    base.update(overrides)
    return base


FIELDS = [
    {"name": "serial", "type": "string"},
    {"name": "vendor", "type": "string", "enum": ["Dell", "HPE"]},
    {"name": "memory_gib", "type": "integer"},
    {"name": "power_state", "type": "string", "enum": ["On", "Off"]},
]


class P0NullIsReachableTest(unittest.TestCase):
    def test_nullable_leaf_gets_null_alternative(self):
        gbnf = L.build_gbnf(L.fields_to_schema(FIELDS))
        self.assertIn("::= null | string", gbnf)
        self.assertIn("::= null | integer", gbnf)

    def test_non_nullable_leaf_has_no_null_alternative(self):
        schema = {"type": "object", "additionalProperties": False, "required": ["a"],
                  "properties": {"a": {"type": "string"}}}
        gbnf = L.build_gbnf(schema)
        self.assertNotIn("null | string", gbnf)

    def test_model_may_answer_null_and_it_becomes_missing(self):
        data, meta = L.extract(params(
            text="SerialNumber = 7XK2P13\nMemory = 512 GB\n",
            fields=FIELDS,
            mock_response=json.dumps({"serial": "7XK2P13", "vendor": None,
                                      "memory_gib": 512, "power_state": None}),
        ))
        self.assertEqual(data, {"serial": "7XK2P13", "memory_gib": 512})
        self.assertEqual(sorted(meta["missing"]), ["power_state", "vendor"])

    def test_on_missing_fail_now_actually_triggers(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(
                fields=FIELDS, on_missing="fail",
                mock_response=json.dumps({"serial": "7XK2P13", "vendor": None,
                                          "memory_gib": None, "power_state": None}),
            ))
        self.assertEqual(ctx.exception.code, "missing_fields")


class P0GroundingTest(unittest.TestCase):
    def test_substring_is_not_grounding(self):
        # The old substring check grounded "On" inside "Only"; boundaries must reject it.
        self.assertFalse(L.appears_in("On", "Device = only"))
        self.assertFalse(L.appears_in("On", "Device = Only"))
        self.assertFalse(L.appears_in("On", "Device = Only"))
        self.assertFalse(L.appears_in(536, "Memory = 1536 GB"))

    def test_short_value_boundaries(self):
        self.assertTrue(L.appears_in("On", "PowerState = On"))
        self.assertTrue(L.appears_in("2.9.4", "BiosVersion = 2.9.4, 2024"))

    def test_enum_is_checked_and_wrong_vendor_is_caught(self):
        data, meta = L.extract(params(
            fields=FIELDS,
            mock_response=json.dumps({"serial": "7XK2P13", "vendor": "HPE",
                                      "memory_gib": 512, "power_state": "On"}),
        ))
        self.assertEqual(data["vendor"], "HPE")
        self.assertEqual([item["path"] for item in meta["unverified"]], ["vendor"])

    def test_enum_normalised_from_source_counts_as_grounded(self):
        _data, meta = L.extract(params(
            fields=FIELDS,
            mock_response=json.dumps({"vendor": "Dell Inc.", "serial": "7XK2P13",
                                      "memory_gib": 512, "power_state": "On"}),
        ))
        self.assertEqual(meta["unverified"], [])
        self.assertEqual(meta["normalized"][0]["from"], "Dell Inc.")

    def test_drop_policy_removes_invented_values(self):
        data, meta = L.extract(params(
            fields=FIELDS, on_ungrounded="drop",
            mock_response=json.dumps({"serial": "BOGUS", "vendor": None,
                                      "memory_gib": 512, "power_state": None}),
        ))
        self.assertNotIn("serial", data)
        self.assertIn("serial", meta["missing"])
        self.assertEqual(meta["dropped"], 1)
        self.assertEqual(meta["unverified"][0]["path"], "serial")

    def test_fail_policy_stops_on_invention(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(
                fields=FIELDS, on_ungrounded="fail",
                mock_response=json.dumps({"serial": "BOGUS", "vendor": None,
                                          "memory_gib": 512, "power_state": None}),
            ))
        self.assertEqual(ctx.exception.code, "ungrounded_values")

    def test_denominator_excludes_booleans(self):
        fields = [{"name": "serial", "type": "string"},
                  {"name": "powered", "type": "boolean"}]
        _data, meta = L.extract(params(
            text="SerialNumber = 7XK2P13\n", fields=fields,
            mock_response=json.dumps({"serial": "7XK2P13", "powered": True}),
        ))
        self.assertEqual(meta["checked"], 1)
        self.assertEqual(meta["grounded"], 1)

    def test_min_grounded_ratio_is_honest(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(
                fields=FIELDS, min_grounded=1.0,
                mock_response=json.dumps({"serial": "BOGUS", "vendor": None,
                                          "memory_gib": None, "power_state": None}),
            ))
        self.assertEqual(ctx.exception.code, "grounding_failed")


class P0EnumRejectionTest(unittest.TestCase):
    def test_invented_enum_value_becomes_null_and_is_reported(self):
        data, meta = L.extract(params(
            fields=FIELDS,
            mock_response=json.dumps({"serial": "7XK2P13", "vendor": "Server manufacturer",
                                      "memory_gib": None, "power_state": None}),
        ))
        self.assertNotIn("vendor", data)
        self.assertEqual(meta["rejected"][0]["path"], "vendor")
        self.assertIn("vendor", meta["missing"])

    def test_invented_enum_does_not_kill_the_whole_answer(self):
        data, _meta = L.extract(params(
            fields=FIELDS,
            mock_response=json.dumps({"serial": "7XK2P13", "vendor": "Nope",
                                      "memory_gib": 512, "power_state": None}),
        ))
        self.assertEqual(data["serial"], "7XK2P13")


class P1CacheTest(unittest.TestCase):
    def setUp(self):
        self.cache_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache_dir)

    def test_quality_gate_runs_on_cache_hit(self):
        response = json.dumps({"serial": "BOGUS", "vendor": None,
                               "memory_gib": None, "power_state": None})
        first, meta = L.extract(params(fields=FIELDS, cache=True, cache_dir=self.cache_dir,
                                       mock_response=response))
        self.assertFalse(meta["cache_hit"])
        self.assertEqual([item["path"] for item in meta["unverified"]], ["serial"])
        # Same key, stricter threshold: the cached answer must not bypass the check.
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(fields=FIELDS, cache=True, cache_dir=self.cache_dir,
                             min_grounded=1.0, mock_response=response))
        self.assertEqual(ctx.exception.code, "grounding_failed")

    def test_key_covers_backend_server_and_model(self):
        base = params(fields=FIELDS, backend="mock")
        key = lambda **kw: L.prepare(dict(base, **kw))["plan"]["cache"]["key"]
        self.assertNotEqual(key(backend="server", server_url="http://a:8080"),
                            key(backend="server", server_url="http://b:8080"))
        self.assertNotEqual(key(backend="server"), key(backend="mock"))
        self.assertNotEqual(key(model_id="model-a"), key(model_id="model-b"))
        self.assertNotEqual(key(grounding="none"), key(grounding="substring"))
        self.assertNotEqual(key(on_missing="fail"), key(on_missing="allow"))

    def test_key_stable_for_identical_calls(self):
        first = L.prepare(params(fields=FIELDS))["plan"]["cache"]["key"]
        second = L.prepare(params(fields=FIELDS))["plan"]["cache"]["key"]
        self.assertEqual(first, second)


class P1RetriesTest(unittest.TestCase):
    def test_retry_changes_temperature_and_budget(self):
        calls = []

        class Recorder(object):
            def __init__(self):
                self.n = 0

            def complete(self, messages, gbnf, max_tokens, temperature, seed, timeout):
                calls.append((temperature, max_tokens, seed))
                self.n += 1
                if self.n == 1:
                    return "{broken", "stop", {}
                return json.dumps({"serial": "7XK2P13", "vendor": None,
                                   "memory_gib": None, "power_state": None}), "stop", {}

        original = L.make_backend
        L.make_backend = lambda _params: Recorder()
        try:
            _data, meta = L.extract(params(fields=FIELDS, retries=1, on_invalid="retry"))
        finally:
            L.make_backend = original
        self.assertEqual(meta["attempts"], 2)
        self.assertEqual(calls[0][0], 0.0)
        self.assertEqual(calls[1][0], 0.1)
        self.assertEqual(calls[1][1], calls[0][1] * 2)
        self.assertNotEqual(calls[0][2], calls[1][2])


class P1AliasesTest(unittest.TestCase):
    def test_aliases_are_rejected_loudly(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.fields_to_schema([{"name": "serial", "aliases": ["SerialNumber"]}])
        self.assertEqual(ctx.exception.code, "schema_invalid")
        self.assertIn("aliases", str(ctx.exception))

    def test_shipped_example_fields_have_no_aliases(self):
        # The example playbook carries the Redfish schema now; it must not rely on aliases.
        with open(os.path.join(ROOT, "playbooks", "introspect_llm_extract.yml"),
                  encoding="utf-8") as handle:
            self.assertNotIn("aliases:", handle.read())


class P1RequiredTest(unittest.TestCase):
    schema = {
        "type": "object", "additionalProperties": False, "required": ["a"],
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    }

    def test_optional_property_is_nullable_in_the_grammar(self):
        gbnf = L.build_gbnf(self.schema)
        optional_rule = [line for line in gbnf.splitlines()
                         if line.startswith("n") and "null | string" in line]
        self.assertTrue(optional_rule, gbnf)

    def test_optional_property_may_be_null(self):
        self.assertEqual(L.validate_instance({"a": "x", "b": None}, self.schema), [])
        self.assertEqual(L.validate_instance({"a": "x"}, self.schema), [])

    def test_required_property_still_required(self):
        self.assertTrue(L.validate_instance({"b": "y"}, self.schema))

    def test_empty_required_list_makes_everything_optional(self):
        schema = dict(self.schema, required=[])
        self.assertEqual(L.validate_instance({"a": None, "b": None}, schema), [])
        self.assertEqual(L.validate_instance({}, schema), [])


class P2PromptTest(unittest.TestCase):
    def test_brief_schema_is_smaller_than_json_schema(self):
        schema = L.fields_to_schema(FIELDS)
        brief = "\n".join(L.brief_schema(schema))
        self.assertLess(len(brief), len(json.dumps(schema)))
        self.assertIn("serial (string)", brief)
        self.assertIn("one of: Dell, HPE", brief)

    def test_system_message_contains_fields_and_null_instruction(self):
        messages = L._messages("Extract.", L.fields_to_schema(FIELDS), TEXT)
        system = messages[0]["content"]
        self.assertIn("never invent", system)
        self.assertIn("serial (string)", system)
        self.assertNotIn('"additionalProperties"', system)
        self.assertEqual(messages[1]["content"], TEXT)


if __name__ == "__main__":
    unittest.main()
