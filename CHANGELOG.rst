=======================
andrewich.ai changelog
=======================

0.4.0
=====

* Renamed the collection to ``andrewich.ai`` and moved ``galaxy.yml`` to the repository
  root, so the repository can be installed with
  ``ansible-galaxy collection install git+https://github.com/Andrewich/ansible-collection-ai.git``.
* Added ``model_url``: the module downloads a GGUF model over HTTPS once, with a file lock,
  atomic writes, GGUF/SHA-256 integrity checks and three retries. ``model_dir``,
  ``model_sha256``, ``strict_download``, ``refresh`` and ``download_timeout`` control it.
  ``model_sha256`` is optional but recommended; without it a warning is emitted.
* Added an English ``README.md`` and a Russian ``README.ru.md``; the contract now exists in
  both languages. Added the GPL-3.0-or-later licence text.
* Added ``scripts/build-and-install.sh`` and pointed ``ansible.cfg`` at ``./.collections``,
  so playbooks always run against the installed collection.
* Tightened llama.cpp error reporting: load and context failures become contract errors
  instead of tracebacks (``model_invalid``, ``input_too_long``).

0.3.0
=====

* Model downloads, byte-exact ``src`` reads, safe caching and loopback-only servers.
* Review fixes: token-bounded enum normalisation, per-chunk grounding, projection schema,
  ``allow_mock`` opt-in, new error codes and parameter validation.
