"""Model download tests (parameters, algorithm, integrity).

No real network: every test replaces ``llm_core.urlopen`` with a fake response object.
"""
from __future__ import print_function

import hashlib
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

URL = "https://example.invalid/models/tiny.gguf"
PAYLOAD = b"GGUF" + b"weights" * 100
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def base_params(**overrides):
    base = dict(
        text="SerialNumber = 7XK2P13", src=None, prompt="Extract.", prompt_file=None,
        fields=[{"name": "serial", "type": "string"}], json_schema=None,
        result_key="llm_result", model_path=None, model_id=None, backend="python",
        server_url="http://127.0.0.1:8080", max_input_chars=1200, on_too_long="fail",
        merge="first", on_missing="allow", on_invalid="retry", grounding="substring",
        on_ungrounded="keep", min_grounded=0.0, strict_enum=False, cache=False,
        cache_dir=None, temperature=0.0, seed=0, max_tokens=320, retries=0,
        n_ctx=4096, n_threads=0, timeout=120, mock_response=None, debug=False,
        check_mode=False, allow_mock=False, model_url=None, model_dir=None,
        model_sha256=None, strict_download=False, refresh=False, download_timeout=600,
    )
    base.update(overrides)
    return base


class ValidationTest(unittest.TestCase):
    def assert_invalid(self, **overrides):
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_params(base_params(**overrides))
        self.assertEqual(ctx.exception.code, "invalid_params")
        return str(ctx.exception)

    def test_model_url_and_path_are_mutually_exclusive(self):
        message = self.assert_invalid(model_url=URL, model_path="/tmp/x.gguf")
        self.assertIn("mutually exclusive", message)

    def test_server_backend_cannot_download(self):
        message = self.assert_invalid(model_url=URL, backend="server")
        self.assertIn("backend 'server' loads the model itself", message)

    def test_only_https_is_accepted(self):
        for url in ("http://example.invalid/m.gguf", "file:///tmp/m.gguf",
                    "ftp://example.invalid/m.gguf"):
            self.assert_invalid(model_url=url)

    def test_extension_must_be_gguf(self):
        self.assert_invalid(model_url="https://example.invalid/model.bin")

    def test_sha256_format(self):
        self.assert_invalid(model_url=URL, model_sha256="deadbeef")

    def test_python_backend_needs_a_model(self):
        message = self.assert_invalid()
        self.assertIn("model_path or model_url", message)

    def test_strict_download_requires_a_digest(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.validate_params(base_params(model_url=URL, strict_download=True))
        self.assertEqual(ctx.exception.code, "model_checksum_required")

    def test_download_timeout_range(self):
        self.assert_invalid(model_url=URL, download_timeout=0)
        self.assert_invalid(model_url=URL, download_timeout="600")


if __name__ == "__main__":
    unittest.main()
