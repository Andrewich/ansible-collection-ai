"""Egress guard tests (review P0-6)."""
from __future__ import print_function

import os
import socket
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "plugins", "module_utils"))

import llm_core as L  # noqa: E402


def server_params(url):
    return {
        "backend": "server", "server_url": url, "model_id": "m",
        "max_tokens": 16, "temperature": 0.0, "seed": 0, "timeout": 1,
        "model_path": None, "n_ctx": 1024, "n_threads": 0,
    }


class LoopbackTest(unittest.TestCase):
    def test_remote_host_is_rejected_before_connecting(self):
        for url in ("http://remote.example:8080", "http://10.0.0.5:8080",
                    "https://192.168.1.10", "http://[2001:db8::1]:8080"):
            with self.assertRaises(L.BackendError) as ctx:
                L.make_backend(server_params(url))
            self.assertEqual(ctx.exception.code, "invalid_server_url")
            self.assertIn("loopback", str(ctx.exception))

    def test_bad_scheme_and_credentials(self):
        for url in ("file:///etc/passwd", "ftp://127.0.0.1/x",
                    "http://user:pass@127.0.0.1:8080", "127.0.0.1:8080"):
            with self.assertRaises(L.BackendError) as ctx:
                L.make_backend(server_params(url))
            self.assertEqual(ctx.exception.code, "invalid_server_url")

    def test_loopback_hosts_are_accepted(self):
        for url in ("http://127.0.0.1:8080", "http://localhost:8080",
                    "http://[::1]:8080", "https://127.0.0.1"):
            backend = L.make_backend(server_params(url))
            self.assertTrue(backend.url.endswith("/v1/chat/completions"))

    def test_no_socket_is_opened_for_a_remote_url(self):
        opened = []
        original = socket.socket

        class Tracker(original):
            def connect(self, *args, **kwargs):
                opened.append(args)
                raise AssertionError("no socket must be opened")

        socket.socket = Tracker
        try:
            with self.assertRaises(L.BackendError):
                L.make_backend(server_params("http://remote.example:8080"))
        finally:
            socket.socket = original
        self.assertEqual(opened, [])

    def test_loopback_helper(self):
        self.assertTrue(L.is_loopback_host("127.0.0.1"))
        self.assertTrue(L.is_loopback_host("127.10.20.30"))
        self.assertTrue(L.is_loopback_host("::1"))
        self.assertTrue(L.is_loopback_host("localhost"))
        self.assertFalse(L.is_loopback_host("10.0.0.1"))
        self.assertFalse(L.is_loopback_host("example.com"))
        self.assertFalse(L.is_loopback_host(""))

    def test_redirects_are_refused(self):
        handler = L.NoRedirectHandler()
        with self.assertRaises(Exception):
            handler.redirect_request(None, None, 302, "Found", {}, "http://remote.example")


if __name__ == "__main__":
    unittest.main()
