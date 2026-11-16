"""Grounding and enum honesty tests (review P0-1, P1-1 and the zero denominator)."""
from __future__ import print_function

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "plugins", "module_utils"))

import llm_core as L  # noqa: E402


def params(**overrides):
    base = dict(
        text="", src=None, prompt="Extract.", prompt_file=None, fields=None,
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


STATE = [{"name": "state", "type": "string", "enum": ["On", "Off"]}]


class EnumLaunderingTest(unittest.TestCase):
    def test_only_is_not_on(self):
        data, meta = L.extract(params(text="PowerState = Only", fields=STATE,
                                      mock_response='{"state": "Only"}'))
        self.assertEqual(data, {})
        self.assertEqual(meta["missing"], ["state"])
        self.assertEqual(meta["rejected"][0]["value"], "Only")

    def test_only_is_never_grounded(self):
        direct = L.extract(params(text="PowerState = Only", fields=STATE,
                                  mock_response='{"state": "Only"}',
                                  on_ungrounded="keep"))[0]
        self.assertEqual(direct, {})

    def test_enum_invention_never_reaches_facts_even_with_fail(self):
        # A rejected enum member is invalid, not ungrounded: it is removed before grounding,
        # so neither keep nor fail can return it.
        data, meta = L.extract(params(text="PowerState = Only", fields=STATE,
                                      mock_response='{"state": "Only"}', on_ungrounded="fail"))
        self.assertEqual(data, {})
        self.assertEqual(meta["rejected"][0]["value"], "Only")

    def test_fail_policy_catches_a_valid_but_absent_enum_member(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(text="Manufacturer = Dell Inc.",
                             fields=[{"name": "vendor", "type": "string",
                                      "enum": ["Dell", "HPE"]}],
                             mock_response='{"vendor": "HPE"}', on_ungrounded="fail"))
        self.assertEqual(ctx.exception.code, "ungrounded_values")

    def test_not_dell_is_not_dell(self):
        fields = [{"name": "vendor", "type": "string", "enum": ["Dell"]}]
        data, meta = L.extract(params(text="Manufacturer = NotDell", fields=fields,
                                      mock_response='{"vendor": "NotDell"}'))
        self.assertEqual(data, {})
        self.assertEqual(meta["rejected"][0]["value"], "NotDell")

    def test_dell_inc_is_dell(self):
        fields = [{"name": "vendor", "type": "string", "enum": ["Dell"]}]
        data, meta = L.extract(params(text="Manufacturer = Dell Inc.", fields=fields,
                                      mock_response='{"vendor": "dell inc."}'))
        self.assertEqual(data, {"vendor": "Dell"})
        self.assertEqual(meta["normalized"][0]["to"], "Dell")

    def test_array_items_are_normalised_individually(self):
        schema = {
            "type": "object", "additionalProperties": False, "required": ["xs"],
            "properties": {"xs": {"type": ["array", "null"],
                                  "items": {"type": "string", "enum": ["Dell", "HPE", None]}}},
        }
        data, meta = L.extract(params(text="vendors: Dell Inc., HPE",
                                      json_schema=schema,
                                      mock_response='{"xs": ["Dell Inc.", "HPE"]}'))
        self.assertEqual(data, {"xs": ["Dell", "HPE"]})
        self.assertEqual(len(meta["normalized"]), 1)


class MissingUnionTest(unittest.TestCase):
    def test_null_paths_survive_a_later_drop(self):
        fields = [{"name": "vendor", "type": "string"},
                  {"name": "power_state", "type": "string"},
                  {"name": "serial", "type": "string"}]
        _data, meta = L.extract(params(
            text="SerialNumber = 7XK2P13", fields=fields, on_ungrounded="drop",
            mock_response='{"vendor": null, "power_state": null, "serial": "BOGUS"}',
        ))
        self.assertEqual(meta["missing"], ["power_state", "serial", "vendor"])
        self.assertEqual(meta["dropped"], 1)

    def test_missing_is_sorted_and_unique(self):
        fields = [{"name": "a", "type": "string"}, {"name": "b", "type": "string"}]
        _data, meta = L.extract(params(text="x = 1", fields=fields, on_ungrounded="drop",
                                       mock_response='{"a": null, "b": "BOGUS"}'))
        self.assertEqual(meta["missing"], ["a", "b"])


class ZeroDenominatorTest(unittest.TestCase):
    def test_threshold_fails_when_nothing_is_checkable(self):
        fields = [{"name": "powered", "type": "boolean"}]
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(text="powered: true", fields=fields, min_grounded=1.0,
                             mock_response='{"powered": true}'))
        self.assertEqual(ctx.exception.code, "grounding_failed")
        self.assertIn("nothing to verify", str(ctx.exception))

    def test_threshold_is_fine_when_zero_is_allowed(self):
        fields = [{"name": "powered", "type": "boolean"}]
        data, meta = L.extract(params(text="powered: true", fields=fields,
                                      mock_response='{"powered": true}'))
        self.assertEqual(data, {"powered": True})
        self.assertEqual(meta["checked"], 0)

    def test_fully_dropped_answer_fails_the_threshold(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(text="x = 1", fields=STATE, min_grounded=1.0,
                             mock_response='{"state": "Off"}'))
        self.assertEqual(ctx.exception.code, "grounding_failed")


class BoundaryTest(unittest.TestCase):
    def test_short_values_respect_boundaries(self):
        self.assertFalse(L.appears_in("On", "Only"))
        self.assertFalse(L.appears_in(536, "1536"))
        self.assertTrue(L.appears_in("On", "PowerState = On"))
        self.assertTrue(L.appears_in("Dell", "Dell Inc."))


if __name__ == "__main__":
    unittest.main()
