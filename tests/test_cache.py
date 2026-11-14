"""Cache equivalence and safety tests (review P0-2, P0-5, P2-3)."""
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

TEXT = "SerialNumber = 7XK2P13\nMemory = 512 GB"
FIELDS = [{"name": "serial", "type": "string"}, {"name": "memory_gib", "type": "integer"}]
GOOD = json.dumps({"serial": "7XK2P13", "memory_gib": 512})


def params(**overrides):
    base = dict(
        text=TEXT, src=None, prompt="Extract.", prompt_file=None, fields=FIELDS,
        json_schema=None, result_key="llm_result", model_path=None, model_id="m",
        backend="mock", allow_mock=True, server_url="http://127.0.0.1:8080", max_input_chars=1200,
        on_too_long="fail", merge="first", on_missing="allow", on_invalid="retry",
        grounding="substring", on_ungrounded="keep", min_grounded=0.0,
        strict_enum=False, cache=False, cache_dir=None, temperature=0.0, seed=0,
        max_tokens=320, retries=0, n_ctx=4096, n_threads=0, timeout=120,
        mock_response=GOOD, debug=False, check_mode=False,
    )
    base.update(overrides)
    return base


class CacheEquivalenceTest(unittest.TestCase):
    def setUp(self):
        self.cache_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def test_cached_result_matches_fresh_result(self):
        fresh, fresh_meta = L.extract(params(cache=True, cache_dir=self.cache_dir))
        cached, cached_meta = L.extract(params(cache=True, cache_dir=self.cache_dir))
        self.assertFalse(fresh_meta["cache_hit"])
        self.assertTrue(cached_meta["cache_hit"])
        self.assertEqual(fresh, cached)
        for key in ("missing", "unverified", "coerced", "normalized", "rejected",
                    "checked", "grounded", "dropped"):
            self.assertEqual(fresh_meta[key], cached_meta[key], key)

    def test_stricter_threshold_cannot_be_bypassed_by_cache(self):
        fields = [{"name": "serial", "type": "string"}]
        # First run drops the invented value, but the envelope keeps it for later policy.
        data, meta = L.extract(params(fields=fields, cache=True, cache_dir=self.cache_dir,
                                      on_ungrounded="drop", min_grounded=0.0,
                                      mock_response='{"serial": "BOGUS"}'))
        self.assertEqual(data, {})
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(params(fields=fields, cache=True, cache_dir=self.cache_dir,
                             on_ungrounded="drop", min_grounded=1.0,
                             mock_response='{"serial": "BOGUS"}'))
        self.assertEqual(ctx.exception.code, "grounding_failed")

    def test_cache_key_covers_min_grounded_and_format(self):
        first = L.prepare(params(cache=True))["plan"]["cache"]["key"]
        second = L.prepare(params(cache=True, min_grounded=1.0))["plan"]["cache"]["key"]
        self.assertNotEqual(first, second)


class CacheSafetyTest(unittest.TestCase):
    def setUp(self):
        self.cache_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def test_tampered_entry_is_a_miss(self):
        L.extract(params(cache=True, cache_dir=self.cache_dir))
        name = [item for item in os.listdir(self.cache_dir)][0]
        path = os.path.join(self.cache_dir, name)
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["chunks"][0]["data"]["surprise"] = "value"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        data, meta = L.extract(params(cache=True, cache_dir=self.cache_dir,
                                      mock_response='{"serial": "7XK2P13", "memory_gib": 512}'))
        self.assertFalse(meta["cache_hit"])
        self.assertNotIn("surprise", data)

    def test_old_format_is_a_miss(self):
        key = L.prepare(params(cache=True, cache_dir=self.cache_dir))["plan"]["cache"]["key"]
        with open(os.path.join(self.cache_dir, key + ".json"), "w", encoding="utf-8") as handle:
            json.dump({"v": 1, "data": {"serial": "old"}, "missing": []}, handle)
        _data, meta = L.extract(params(cache=True, cache_dir=self.cache_dir))
        self.assertFalse(meta["cache_hit"])

    def test_shared_writable_directory_disables_the_cache(self):
        os.chmod(self.cache_dir, 0o777)
        _data, meta = L.extract(params(cache=True, cache_dir=self.cache_dir))
        self.assertEqual(meta.get("cache_written"), False)
        self.assertEqual(os.listdir(self.cache_dir), [])

    def test_write_does_not_follow_a_planted_symlink(self):
        key = L.prepare(params(cache=True, cache_dir=self.cache_dir))["plan"]["cache"]["key"]
        victim = os.path.join(self.cache_dir, "victim.txt")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("untouched")
        os.symlink(victim, os.path.join(self.cache_dir, key + ".json.tmp"))
        L.extract(params(cache=True, cache_dir=self.cache_dir))
        with open(victim, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "untouched")

    def test_cache_file_mode_is_private(self):
        L.extract(params(cache=True, cache_dir=self.cache_dir))
        for name in os.listdir(self.cache_dir):
            mode = os.stat(os.path.join(self.cache_dir, name)).st_mode & 0o777
            self.assertEqual(mode, 0o600, name)

    def test_check_mode_writes_nothing(self):
        _data, meta = L.extract(params(cache=True, cache_dir=self.cache_dir, check_mode=True))
        self.assertEqual(os.listdir(self.cache_dir), [])
        self.assertEqual(meta.get("cache_written"), False)
        self.assertFalse(meta["cache_hit"])


if __name__ == "__main__":
    unittest.main()
