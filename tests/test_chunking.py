"""Chunking and merge tests (review P1-5, P2-2)."""
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
        grounding="substring", on_ungrounded="drop", min_grounded=0.0,
        strict_enum=False, cache=False, cache_dir=None, temperature=0.0, seed=0,
        max_tokens=320, retries=0, n_ctx=4096, n_threads=0, timeout=120,
        mock_response=None, debug=False, check_mode=False,
    )
    base.update(overrides)
    return base


class SequenceBackend(object):
    """Returns a different answer for each call, in order."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def complete(self, messages, gbnf, max_tokens, temperature, seed, timeout):
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        return json.dumps(answer), "stop", {}


class ChunkPreferenceTest(unittest.TestCase):
    def setUp(self):
        self._original = L.make_backend

    def tearDown(self):
        L.make_backend = self._original

    def _run(self, answers, text, **overrides):
        backend = SequenceBackend(answers)
        L.make_backend = lambda _params: backend
        fields = [{"name": "serial", "type": "string"}]
        return L.extract(params(text=text, fields=fields, max_input_chars=40,
                                on_too_long="split", **overrides))

    def test_valid_value_from_the_second_chunk_wins(self):
        text = ("filler line one\nfiller line two\n"
                "serial marker: 7XK2P13\n")
        data, meta = self._run([{"serial": "BOGUS"}, {"serial": "7XK2P13"}], text)
        self.assertEqual(data, {"serial": "7XK2P13"})
        self.assertEqual(meta["chunks"], 2)
        # BOGUS was never grounded, so it is not reported as an extracted value at all.
        self.assertEqual(meta["unverified"], [])
        self.assertEqual(meta["missing"], [])

    def test_truncated_tail_cannot_ground_a_value(self):
        text = "filler line\n" * 8 + "SerialNumber = 7XK2P13\n"
        backend = SequenceBackend([{"serial": "7XK2P13"}])
        L.make_backend = lambda _params: backend
        data, meta = L.extract(params(
            text=text, fields=[{"name": "serial", "type": "string"}],
            max_input_chars=60, on_too_long="truncate", mock_response=None,
        ))
        self.assertEqual(data, {})
        self.assertEqual(meta["missing"], ["serial"])
        self.assertEqual(meta["dropped"], 1)

    def test_missing_is_unioned_across_chunks(self):
        text = "serial 7XK2P13\nmemory 512\n"
        backend = SequenceBackend([
            {"serial": "7XK2P13", "memory_gib": None},
            {"serial": None, "memory_gib": 512},
        ])
        L.make_backend = lambda _params: backend
        data, meta = L.extract(params(
            text=text, text_extra=None,
            fields=[{"name": "serial", "type": "string"},
                    {"name": "memory_gib", "type": "integer"}],
            max_input_chars=20, on_too_long="split", merge="first",
            mock_response=None,
        ))
        self.assertEqual(data, {"serial": "7XK2P13", "memory_gib": 512})
        self.assertEqual(meta["missing"], [])


class MergeTableTest(unittest.TestCase):
    def test_first_wins(self):
        merged = L.merge_results([{"a": 1, "xs": ["a"]}, {"a": 2, "xs": ["b"]}], "first")
        self.assertEqual(merged, {"a": 1, "xs": ["a"]})

    def test_concat_keeps_duplicates(self):
        merged = L.merge_results([{"xs": ["a"]}, {"xs": ["a", "b"]}], "concat")
        self.assertEqual(merged["xs"], ["a", "a", "b"])

    def test_deep_merge_recurses_and_deduplicates(self):
        merged = L.merge_results(
            [{"o": {"a": 1}, "xs": ["a"]}, {"o": {"b": 2}, "xs": ["a", "b"]}],
            "deep_merge",
        )
        self.assertEqual(merged["o"], {"a": 1, "b": 2})
        self.assertEqual(merged["xs"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
