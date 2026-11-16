"""llama.cpp failures must become contract errors, not tracebacks."""
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


class StubLlama(object):
    def __init__(self, error=None):
        self.error = error

    def create_chat_completion(self, **kwargs):
        raise self.error


class StubGrammar(object):
    @staticmethod
    def from_string(gbnf, verbose=False):
        return object()


class StubLlamaCpp(object):
    __version__ = "0.3.36"

    def __init__(self, load_error=None, run_error=None):
        self.load_error = load_error
        self.run_error = run_error
        StubLlamaCpp.instance = self

    def Llama(self, **kwargs):
        if self.load_error is not None:
            raise self.load_error
        return StubLlama(self.run_error)

    LlamaGrammar = StubGrammar


def backend_params(**overrides):
    base = dict(model_path="/tmp/model.gguf", n_ctx=4096, n_threads=0)
    base.update(overrides)
    return base


class LoadFailureTest(unittest.TestCase):
    def test_unsupported_architecture_is_model_invalid(self):
        backend = L.PythonBackend(backend_params())
        backend._llama_cpp = StubLlamaCpp(
            load_error=ValueError("Failed to load model from file: x.gguf"),
        )
        with self.assertRaises(L.BackendError) as ctx:
            backend._load()
        self.assertEqual(ctx.exception.code, "model_invalid")
        self.assertIn("llama-cpp-python 0.3.36", str(ctx.exception))
        self.assertIn("architecture", str(ctx.exception))

    def test_extract_attaches_the_plan(self):
        class Loader(object):
            def complete(self, *args, **kwargs):
                raise L.BackendError("model_invalid", "cannot load model")

        original = L.make_backend
        L.make_backend = lambda _params: Loader()
        model = tempfile.NamedTemporaryFile(suffix=".gguf", delete=False)
        model.write(b"GGUF")
        model.close()
        try:
            with self.assertRaises(L.BackendError) as ctx:
                L.extract({
                    "text": "SerialNumber = 7XK2P13", "src": None, "prompt": "Extract.",
                    "prompt_file": None, "fields": [{"name": "serial", "type": "string"}],
                    "json_schema": None, "result_key": "llm_result", "model_path": model.name,
                    "model_id": None, "backend": "python", "server_url": "http://127.0.0.1:8080",
                    "max_input_chars": 1200, "on_too_long": "fail", "merge": "first",
                    "on_missing": "allow", "on_invalid": "retry", "grounding": "substring",
                    "on_ungrounded": "keep", "min_grounded": 0.0, "strict_enum": False,
                    "cache": False, "cache_dir": None, "temperature": 0.0, "seed": 0,
                    "max_tokens": 320, "retries": 0, "n_ctx": 4096, "n_threads": 0,
                    "timeout": 120, "mock_response": None, "debug": False, "check_mode": False,
                    "allow_mock": False, "model_url": None, "model_dir": None,
                    "model_sha256": None, "strict_download": False, "refresh": False,
                    "download_timeout": 600,
                })
        finally:
            L.make_backend = original
            os.unlink(model.name)
        self.assertEqual(ctx.exception.code, "model_invalid")
        self.assertIn("schema", ctx.exception.plan)


class ContextOverflowTest(unittest.TestCase):
    def test_context_overflow_is_input_too_long(self):
        backend = L.PythonBackend(backend_params(n_ctx=8192))
        backend._llama_cpp = StubLlamaCpp(
            run_error=ValueError("Requested tokens (9745) exceed context window of 8192"),
        )
        with self.assertRaises(L.BackendError) as ctx:
            backend.complete([], "root", 16, 0.0, 0, 10)
        self.assertEqual(ctx.exception.code, "input_too_long")
        self.assertIn("n_ctx=8192", str(ctx.exception))
        self.assertIn("max_input_chars", str(ctx.exception))

    def test_other_value_errors_are_backend_unavailable(self):
        backend = L.PythonBackend(backend_params())
        backend._llama_cpp = StubLlamaCpp(run_error=ValueError("unexpected graph failure"))
        with self.assertRaises(L.BackendError) as ctx:
            backend.complete([], "root", 16, 0.0, 0, 10)
        self.assertEqual(ctx.exception.code, "backend_unavailable")


class ModuleErrorTest(unittest.TestCase):
    def test_module_reports_model_invalid_as_json(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "broken.gguf")
        with open(path, "wb") as handle:
            handle.write(b"this is not a gguf file")
        args = {"ANSIBLE_MODULE_ARGS": {
            "text": "SerialNumber = 7XK2P13", "prompt": "Extract.",
            "fields": [{"name": "serial", "type": "string"}],
            "backend": "python", "model_path": path, "n_ctx": 512,
        }}
        args_path = os.path.join(directory, "args.json")
        with open(args_path, "w", encoding="utf-8") as handle:
            json.dump(args, handle)
        completed = subprocess.run(
            [sys.executable, MODULE, args_path], capture_output=True, text=True,
            env=dict(os.environ, PYTHONPATH=ROOT),
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result.get("error_code"), "model_invalid")
        self.assertIn("cannot load model", result.get("msg", ""))
        self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
