"""Model download tests (parameters, algorithm, integrity).

No real network: every test replaces ``llm_core.urlopen`` with a fake response object.
"""
from __future__ import print_function

import hashlib
import http.client
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


class FakeResponse(object):
    def __init__(self, chunks, content_length=None, fail_after=None):
        self._chunks = list(chunks)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self._fail_after = fail_after
        self._sent = 0

    def read(self, size=-1):
        if self._fail_after is not None and self._sent >= self._fail_after:
            raise http.client.IncompleteRead(b"", len(self._chunks[0]))
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        self._sent += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class DownloadCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.model_dir = os.path.join(self.dir, "models")
        self._original = L.urlopen
        self._original_backend = L.make_backend
        self.calls = []

        class FakeInference(object):
            def __init__(self, answer):
                self.answer = answer

            def complete(self, *args, **kwargs):
                return self.answer, "stop", {}

        # The downloader requires backend python; inference itself is faked so no real
        # GGUF file is ever loaded.
        L.make_backend = lambda _params: FakeInference('{"serial": "7XK2P13"}')

    def tearDown(self):
        L.urlopen = self._original
        L.make_backend = self._original_backend
        shutil.rmtree(self.dir, ignore_errors=True)

    def serve(self, payload=PAYLOAD, content_length=None, fail_after=None):
        def fake(url, timeout=None):
            self.calls.append(url)
            length = len(payload) if content_length is None else content_length
            return FakeResponse([payload], content_length=length, fail_after=fail_after)
        L.urlopen = fake

    def refuse_network(self):
        def fake(url, timeout=None):
            raise AssertionError("network must not be used here")
        L.urlopen = fake

    def params(self, **overrides):
        base = base_params(model_url=URL, model_dir=self.model_dir,
                           fields=[{"name": "serial", "type": "string"}],
                           mock_response='{"serial": "7XK2P13"}', allow_mock=True,
                           cache=False)
        base.update(overrides)
        return base

    def local_path(self, url=URL):
        return os.path.join(self.model_dir, L.local_model_name(url))


class DownloadSuccessTest(DownloadCase):
    def test_successful_download(self):
        self.serve()
        data, meta = L.extract(self.params())
        path = self.local_path()
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(meta["model"]["download"], "fresh")
        self.assertEqual(meta["model"]["bytes"], len(PAYLOAD))
        self.assertEqual(meta["model"]["sha256"], DIGEST)
        self.assertEqual(data, {"serial": "7XK2P13"})
        leftovers = [name for name in os.listdir(self.model_dir) if name.startswith(".dl-")]
        self.assertEqual(leftovers, [])

    def test_second_call_uses_the_local_copy(self):
        self.serve()
        L.extract(self.params())
        self.refuse_network()
        _data, meta = L.extract(self.params())
        self.assertEqual(meta["model"]["download"], "cached")
        self.assertEqual(len(self.calls), 1)

    def test_skipped_download_reports_cached(self):
        self.serve()
        first = L.extract(self.params())[1]
        self.assertEqual(first["model"]["download"], "fresh")
        self.refuse_network()
        second = L.extract(self.params())[1]
        self.assertEqual(second["model"]["download"], "cached")

    def test_two_urls_with_the_same_basename_do_not_collide(self):
        self.serve()
        other = "https://mirror.invalid/other/tiny.gguf"
        first = self.local_path(URL)
        second = self.local_path(other)
        self.assertNotEqual(os.path.basename(first), os.path.basename(second))
        L.extract(self.params())
        L.extract(self.params(model_url=other))
        self.assertTrue(os.path.exists(first))
        self.assertTrue(os.path.exists(second))


class DownloadIntegrityTest(DownloadCase):
    def test_bad_magic_is_rejected_and_removed(self):
        self.serve(payload=b"NOPE" + b"x" * 64)
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(self.params())
        self.assertEqual(ctx.exception.code, "model_invalid")
        self.assertFalse(os.path.exists(self.local_path()))

    def test_checksum_mismatch_is_rejected_and_removed(self):
        self.serve()
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(self.params(model_sha256="0" * 64))
        self.assertEqual(ctx.exception.code, "model_checksum_mismatch")
        self.assertFalse(os.path.exists(self.local_path()))
        self.assertEqual([n for n in os.listdir(self.model_dir) if n.startswith(".dl-")], [])

    def test_truncated_download_fails(self):
        self.serve(content_length=len(PAYLOAD) + 10)
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(self.params())
        self.assertEqual(ctx.exception.code, "download_failed")
        self.assertFalse(os.path.exists(self.local_path()))

    def test_connection_abort_is_retried_then_reported(self):
        self.serve(fail_after=0)
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(self.params())
        self.assertIn(ctx.exception.code, ("download_failed", "download_timeout"))
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(os.path.exists(self.local_path()))

    def test_missing_checksum_warns(self):
        self.serve()
        _data, meta = L.extract(self.params())
        self.assertTrue(meta.get("warnings"))
        self.assertIn("model_sha256", meta["warnings"][0])

    def test_strict_without_digest_is_refused(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(self.params(strict_download=True))
        self.assertEqual(ctx.exception.code, "model_checksum_required")


class DownloadRefreshTest(DownloadCase):
    def test_changed_size_without_refresh_is_stale(self):
        self.serve()
        L.extract(self.params())
        path = self.local_path()
        with open(path, "r+b") as handle:
            handle.truncate(10)
        self.refuse_network()
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(self.params())
        self.assertEqual(ctx.exception.code, "model_stale")

    def test_refresh_downloads_again(self):
        self.serve()
        L.extract(self.params())
        path = self.local_path()
        with open(path, "r+b") as handle:
            handle.truncate(10)
        self.serve()
        _data, meta = L.extract(self.params(refresh=True))
        self.assertEqual(meta["model"]["download"], "fresh")
        self.assertEqual(os.path.getsize(path), len(PAYLOAD))

    def test_matching_digest_means_cached(self):
        self.serve()
        L.extract(self.params())
        self.refuse_network()
        _data, meta = L.extract(self.params(model_sha256=DIGEST))
        self.assertEqual(meta["model"]["download"], "cached")

    def test_bad_digest_after_download_is_stale(self):
        self.serve()
        L.extract(self.params())
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(self.params(model_sha256="1" * 64))
        self.assertEqual(ctx.exception.code, "model_stale")


class DownloadModeTest(DownloadCase):
    def test_check_mode_does_not_touch_the_filesystem(self):
        self.refuse_network()
        _data, meta = L.extract(self.params(check_mode=True))
        self.assertTrue(meta["plan_only"])
        self.assertEqual(meta["model"]["download"], "skipped")
        self.assertFalse(os.path.exists(self.model_dir))
        self.assertEqual(self.calls, [])

    def test_backend_server_cannot_download(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(self.params(backend="server"))
        self.assertEqual(ctx.exception.code, "invalid_params")

    def test_http_is_refused(self):
        with self.assertRaises(L.ContractError) as ctx:
            L.extract(self.params(model_url="http://example.invalid/tiny.gguf"))
        self.assertEqual(ctx.exception.code, "invalid_params")

    def test_lock_is_released_after_download(self):
        self.serve()
        L.extract(self.params())
        lock = os.path.join(self.model_dir, ".%s.lock" % os.path.basename(self.local_path()))
        handle = os.open(lock, os.O_RDWR)
        try:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(handle)


if __name__ == "__main__":
    unittest.main()
