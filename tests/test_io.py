"""I/O and result-contract tests (review P1-8, P1-9)."""
from __future__ import print_function

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "plugins", "module_utils"))

import llm_core as L  # noqa: E402

MODULE = os.path.join(ROOT, "plugins", "modules", "llm_extract.py")


def params(**overrides):
    base = dict(
        text="SerialNumber = 7XK2P13", src=None, prompt="Extract.", prompt_file=None,
        fields=[{"name": "serial", "type": "string"}], json_schema=None,
        result_key="llm_result", model_path=None, model_id="m", backend="mock", allow_mock=True,
        server_url="http://127.0.0.1:8080", max_input_chars=1200, on_too_long="fail",
        merge="first", on_missing="allow", on_invalid="retry", grounding="substring",
        on_ungrounded="keep", min_grounded=0.0, strict_enum=False, cache=False,
        cache_dir=None, temperature=0.0, seed=0, max_tokens=320, retries=0,
        n_ctx=4096, n_threads=0, timeout=120, mock_response='{"serial": "7XK2P13"}',
        debug=False, check_mode=False,
    )
    base.update(overrides)
    return base


class SourceFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, payload):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    def test_crlf_is_kept_byte_exact(self):
        path = self.write("crlf.txt", b"line1\r\nline2\r\n")
        plan, _schema = L.preflight(params(text=None, src=path))
        self.assertEqual(plan["text"]["chars"], 14)
        self.assertEqual(plan["text"]["sha256"],
                         L.sha256_bytes(b"line1\r\nline2\r\n"))
        self.assertEqual(plan["text"]["bytes"], 14)

    def test_invalid_utf8_is_reported(self):
        path = self.write("bad.txt", b"\xff\xfehello")
        with self.assertRaises(L.ContractError) as ctx:
            L.preflight(params(text=None, src=path))
        self.assertEqual(ctx.exception.code, "input_encoding")

    def test_missing_file_is_not_empty_input(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.preflight(params(text=None, src=os.path.join(self.dir, "nope.txt")))
        self.assertEqual(ctx.exception.code, "input_not_found")

    def test_unreadable_file(self):
        path = self.write("locked.txt", b"data")
        os.chmod(path, 0o000)
        try:
            with self.assertRaises(L.ContractError) as ctx:
                L.preflight(params(text=None, src=path))
            self.assertEqual(ctx.exception.code, "input_unreadable")
        finally:
            os.chmod(path, 0o600)

    def test_structured_text_hash_matches_json(self):
        payload = [{"a": 1}]
        plan, _schema = L.preflight(params(text=payload))
        compact = '[{"a":1}]'
        self.assertEqual(plan["text"]["sha256"], L.sha256_text(compact))
        self.assertEqual(plan["text"]["structured_input"], "json")


class ReturnRawTest(unittest.TestCase):
    def test_schema_invalid_output_keeps_raw_output(self):
        raw_answer = '{"serial": "7XK2P13", "surprise": 1}'
        data, meta = L.extract(params(mock_response=raw_answer, on_invalid="return_raw"))
        self.assertEqual(data, {})
        self.assertFalse(meta["ok"])
        self.assertEqual(meta["raw_output"], raw_answer)

    def test_unparseable_output_keeps_raw_output(self):
        data, meta = L.extract(params(mock_response="{broken", on_invalid="return_raw"))
        self.assertEqual(data, {})
        self.assertIn("raw_output", meta)

    def test_backend_error_carries_the_plan(self):
        class Boom(object):
            def complete(self, *args, **kwargs):
                raise L.BackendError("backend_unavailable", "boom")

        original = L.make_backend
        L.make_backend = lambda _params: Boom()
        try:
            with self.assertRaises(L.BackendError) as ctx:
                L.extract(params())
        finally:
            L.make_backend = original
        self.assertTrue(ctx.exception.plan)
        self.assertIn("schema", ctx.exception.plan)


class ModuleResultTest(unittest.TestCase):
    def run_module(self, args):
        path = os.path.join(tempfile.mkdtemp(), "args.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"ANSIBLE_MODULE_ARGS": args}, handle)
        env = dict(os.environ, PYTHONPATH=ROOT)
        completed = subprocess.run(
            [sys.executable, MODULE, path], capture_output=True, text=True, env=env,
        )
        return json.loads(completed.stdout)

    def test_failed_extraction_returns_no_facts(self):
        result = self.run_module({
            "text": "SerialNumber = 7XK2P13",
            "prompt": "Extract.",
            "fields": [{"name": "serial", "type": "string"}],
            "backend": "mock",
            "allow_mock": True,
            "mock_response": "{broken",
            "on_invalid": "return_raw",
        })
        self.assertNotIn("ansible_facts", result)
        self.assertEqual(result["llm"]["ok"], False)
        self.assertIn("raw_output", result["llm"])

    def test_successful_extraction_returns_facts(self):
        result = self.run_module({
            "text": "SerialNumber = 7XK2P13",
            "prompt": "Extract.",
            "fields": [{"name": "serial", "type": "string"}],
            "backend": "mock",
            "allow_mock": True,
            "mock_response": '{"serial": "7XK2P13"}',
        })
        self.assertEqual(result["ansible_facts"]["llm_result"], {"serial": "7XK2P13"})


if __name__ == "__main__":
    unittest.main()
