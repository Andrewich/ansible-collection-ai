"""Parameter validation tests (review P1-6).

    .venv/bin/python -m unittest discover -s tests -p 'test_params.py' -v
"""
from __future__ import print_function

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "plugins", "module_utils"))
sys.path.insert(0, ROOT)

import llm_core as L  # noqa: E402

FIELDS = [{"name": "serial", "type": "string"}]
TEXT = "SerialNumber = 7XK2P13"


def params(**overrides):
    base = dict(
        text=TEXT, src=None, prompt="Extract.", prompt_file=None, fields=FIELDS,
        json_schema=None, result_key="llm_result", model_path=None, model_id="m",
        backend="mock", allow_mock=True, server_url="http://127.0.0.1:8080", max_input_chars=1200,
        on_too_long="fail", merge="first", on_missing="allow", on_invalid="retry",
        grounding="substring", on_ungrounded="keep", min_grounded=0.0,
        strict_enum=False, cache=False, cache_dir=None, temperature=0.0, seed=0,
        max_tokens=320, retries=0, n_ctx=4096, n_threads=0, timeout=120,
        mock_response='{"serial": "7XK2P13"}', debug=False,
    )
    base.update(overrides)
    return base


class RangeTest(unittest.TestCase):
    def assert_rejected(self, **overrides):
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_params(params(**overrides))
        self.assertEqual(ctx.exception.code, "invalid_params")
        return str(ctx.exception)

    def test_positive_ranges(self):
        for name in ("max_input_chars", "max_tokens", "n_ctx", "timeout"):
            self.assert_rejected(**{name: 0})
        self.assert_rejected(retries=-1)
        self.assert_rejected(n_threads=-1)

    def test_ratio_and_temperature(self):
        self.assert_rejected(min_grounded=1.5)
        self.assert_rejected(min_grounded=-0.01)
        self.assert_rejected(temperature=-0.1)

    def test_parameter_types(self):
        message = self.assert_rejected(cache="false")
        self.assertIn("cache must be a boolean", message)
        self.assert_rejected(retries="1")
        self.assert_rejected(max_input_chars="1200")
        self.assert_rejected(strict_enum=1)

    def test_zero_budget_is_rejected_before_split(self):
        message = self.assert_rejected(max_input_chars=0)
        self.assertIn("max_input_chars must be >= 1", message)
        with self.assertRaises(L.ContractError):
            L.split_text("abc", 0, "split")


class ChoiceTest(unittest.TestCase):
    def test_unknown_choice(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_params(params(on_ungrounded="drpo"))
        self.assertEqual(ctx.exception.code, "invalid_params")
        self.assertIn("on_ungrounded must be one of", str(ctx.exception))

    def test_unknown_backend(self):
        with self.assertRaises(L.ContractError):
            L.validate_params(params(backend="ollama"))


class ExclusivityTest(unittest.TestCase):
    def test_text_and_src(self):
        with self.assertRaises(L.ContractError):
            L.validate_params(params(src="/tmp/x"))

    def test_required_text(self):
        with self.assertRaises(L.ContractError):
            L.validate_params(params(text=None))

    def test_prompt_sources(self):
        with self.assertRaises(L.ContractError):
            L.validate_params(params(prompt_file="/tmp/p.txt"))

    def test_schema_sources(self):
        with self.assertRaises(L.ContractError):
            L.validate_params(params(json_schema={"type": "object"}))

    def test_required_schema(self):
        with self.assertRaises(L.ContractError):
            L.validate_params(params(fields=None))

    def test_unknown_parameter_only_in_strict_mode(self):
        L.validate_params(params())
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_params(params(typo=1), strict_unknown=True)
        self.assertIn("unknown parameter(s): typo", str(ctx.exception))


class LookupValidationTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "plugins", "lookup"))
        from llm_extract import LookupModule

        self.lookup = LookupModule()

    def test_string_boolean_is_rejected(self):
        from ansible.errors import AnsibleError

        with self.assertRaises(AnsibleError) as ctx:
            self.lookup.run([TEXT], prompt="Extract.", fields=FIELDS,
                            backend="mock", allow_mock=True, cache="false")
        self.assertIn("cache must be a boolean", str(ctx.exception))

    def test_unknown_keyword_is_rejected(self):
        from ansible.errors import AnsibleError

        with self.assertRaises(AnsibleError) as ctx:
            self.lookup.run([TEXT], prompt="Extract.", fields=FIELDS,
                            backend="mock", allow_mock=True, mutst_response="{}")
        self.assertIn("unknown parameter(s): mutst_response", str(ctx.exception))

    def test_term_with_src_is_rejected(self):
        from ansible.errors import AnsibleError

        with self.assertRaises(AnsibleError) as ctx:
            self.lookup.run([TEXT], src="/tmp/x", prompt="Extract.", fields=FIELDS,
                            backend="mock")
        self.assertIn("mutually exclusive", str(ctx.exception))

    def test_return_raw_is_not_supported(self):
        from ansible.errors import AnsibleError

        with self.assertRaises(AnsibleError) as ctx:
            self.lookup.run([TEXT], prompt="Extract.", fields=FIELDS, backend="mock", allow_mock=True,
                            on_invalid="return_raw")
        self.assertIn("return_raw", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
