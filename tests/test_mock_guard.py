"""Mock backend must be explicit (finding from the review discussion)."""
from __future__ import print_function

import json
import os
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
        result_key="llm_result", model_path=None, model_id="m", backend="mock",
        server_url="http://127.0.0.1:8080", max_input_chars=1200, on_too_long="fail",
        merge="first", on_missing="allow", on_invalid="retry", grounding="substring",
        on_ungrounded="keep", min_grounded=0.0, strict_enum=False, cache=False,
        cache_dir=None, temperature=0.0, seed=0, max_tokens=320, retries=0,
        n_ctx=4096, n_threads=0, timeout=120, mock_response='{"serial": "7XK2P13"}',
        debug=False, check_mode=False,
    )
    base.update(overrides)
    return base


class MockGuardTest(unittest.TestCase):
    def test_mock_requires_explicit_opt_in(self):
        with self.assertRaises(L.BackendError) as ctx:
            L.make_backend(params())
        self.assertEqual(ctx.exception.code, "mock_not_allowed")
        self.assertIn("allow_mock=true", str(ctx.exception))

    def test_mock_works_with_opt_in(self):
        backend = L.make_backend(params(allow_mock=True))
        self.assertEqual(backend.complete(None, None, 1, 0.0, 0, 1)[0], '{"serial": "7XK2P13"}')

    def test_module_refuses_mock_without_the_flag(self):
        path = os.path.join(tempfile.mkdtemp(), "args.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"ANSIBLE_MODULE_ARGS": {
                "text": "SerialNumber = 7XK2P13", "prompt": "Extract.",
                "fields": [{"name": "serial", "type": "string"}],
                "backend": "mock", "mock_response": '{"serial": "7XK2P13"}',
            }}, handle)
        env = dict(os.environ, PYTHONPATH=ROOT)
        result = json.loads(subprocess.run(
            [sys.executable, MODULE, path], capture_output=True, text=True, env=env,
        ).stdout)
        self.assertEqual(result["error_code"], "mock_not_allowed")

    def test_lookup_refuses_mock_without_the_flag(self):
        from ansible.errors import AnsibleError
        sys.path.insert(0, os.path.join(ROOT, "plugins", "lookup"))
        from llm_extract import LookupModule

        with self.assertRaises(AnsibleError) as ctx:
            LookupModule().run(["SerialNumber = 7XK2P13"], prompt="Extract.",
                               fields=[{"name": "serial", "type": "string"}],
                               backend="mock", mock_response='{"serial": "7XK2P13"}')
        self.assertIn("allow_mock", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
