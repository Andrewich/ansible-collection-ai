# `andrewich.ai` — unstructured text → JSON with a local LLM

An Ansible collection with a module and a lookup plugin that turn unstructured text into JSON
matching a schema, using a small GGUF model running on the controller. Inference stays on the
host and no data leaves it; the only network contact is an optional one-time HTTPS download of
the model file when `model_url` is set.

```
text + prompt + schema ──► GBNF grammar ──► llama.cpp (350M) ──► JSON ──► checks ──► facts
```

The default model is `LFM2-350M-Extract` (220 MB). Nothing about Redfish, nginx logs or support
tickets is hard-coded: a domain is a prompt file plus a schema.

## Installation

```bash
# from git (default branch)
ansible-galaxy collection install git+https://github.com/Andrewich/ansible-collection-ai.git

# pinned release, recommended for production
ansible-galaxy collection install git+https://github.com/Andrewich/ansible-collection-ai.git,0.4.0
```

`requirements.yml`:

```yaml
collections:
  - name: https://github.com/Andrewich/ansible-collection-ai.git
    type: git
    version: 0.4.0
```

Then `ansible-galaxy collection install -r requirements.yml`.

The collection itself has no Python dependencies beyond `ansible-core >= 2.15`. The `python`
backend needs `llama-cpp-python` in the interpreter that runs the module (usually the controller):

```bash
python3 -m venv .venv
.venv/bin/pip install "llama-cpp-python==0.3.37" --only-binary=:all: \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
.venv/bin/pip install ansible-core
```

Prebuilt wheels are important on older distributions: prebuilt `llama.cpp` binaries need
`GLIBCXX_3.4.30` and do not start on CentOS Stream 9, while the wheel is manylinux-based.

## Quick start

```yaml
- name: Redfish payload -> facts
  andrewich.ai.llm_extract:
    text: "{{ payload.content }}"
    prompt_file: prompts/redfish_extract.txt
    fields: "{{ redfish_fields }}"   # a list of name/type/description items
    model_url: https://huggingface.co/LiquidAI/LFM2-350M-Extract-GGUF/resolve/main/LFM2-350M-Extract-Q4_K_M.gguf
    model_sha256: 687a31c3e7864647aa181e1feb156e4e5da33978c174d7dbf0d289f6014a5621
    strict_download: true
  delegate_to: localhost
  register: extracted

- name: Act on the facts
  debug:
    msg: "{{ extracted.ansible_facts.llm_result.serial }}"
```

The same pipeline as a template, without facts and without `delegate_to`:

```yaml
- name: Parse an nginx error
  set_fact:
    parsed: >-
      {{ lookup('andrewich.ai.llm_extract', payload,
                prompt_file='prompts/nginx_error_classify.txt',
                json_schema=nginx_schema) }}
```

The examples in [`playbooks/`](playbooks/) cover three domains with one module (Redfish,
nginx logs, support ticket) plus a native Redfish compaction filter. The difference is only the
prompt file and the schema.

## What the module guarantees

| Property | How |
|---|---|
| JSON of the requested shape | GBNF grammar generated from the schema: an extra key or a string where a number belongs cannot be produced |
| "I don't know" is expressible | every nullable grammar leaf has a `null` alternative; absent values land in `llm.missing` |
| Inventions do not pass silently | token-bounded grounding in the chunk that produced the value; `on_ungrounded: drop` (default) removes values not found in the source |
| Data does not leave the host | inference is local; the `server` backend accepts loopback only; redirects and proxies are refused |
| The model need not be fetched by hand | `model_url` downloads the GGUF once, atomically, under a file lock |
| Mock cannot leak into production | `backend: mock` requires `allow_mock: true` |
| Enums are normalised | `Dell Inc.` → `Dell`, `Hewlett Packard Enterprise` → `HPE`, by containment, no domain tables |
| Re-runs are cheap | cache keyed by model, prompt, schema, input and policies; the same policies are re-applied to cached answers |
| Secrets stay local | everything runs offline, cache is 0700/0600, `cache: false` for sensitive input |

The `llm` metadata is part of the contract: `missing`, `unverified`, `dropped`, `checked`,
`grounded`, `tokens`, `cache_hit`, `normalized`, `rejected`, prompt/schema/input hashes, and
`model.download` (`fresh` / `cached` / `skipped` / `false`).

## Model download

If `model_url` is set, `prepare()` fetches the model before inference:

- HTTPS only, and `backend: python` only (the `server` backend loads the model itself);
- the local name is `<sha256(url)[:16]>-<basename>.gguf` in `model_dir`
  (default `<cache_dir>/models`), so equal basenames never collide;
- the file is streamed in ~1 MB chunks into `mkstemp` → `fsync` → `os.replace`; the first four
  bytes must be `GGUF` and SHA-256 is computed on the fly;
- concurrent tasks are serialised with a lock file and `fcntl.flock`, so with `forks > 1` one
  process downloads and the rest wait;
- `model_sha256` is strongly recommended; without it only the GGUF magic and `Content-Length`
  protect the download (the module emits a warning), and `strict_download: true` without a
  digest is an error (`model_checksum_required`);
- `check_mode` downloads nothing and returns the plan instead.

> **First run on a large fleet.** The model is ~220 MB. If it is missing on the controller, the
> first play downloads it once, but with `forks: 50` dozens of tasks will queue on the lock and
> wall time grows. Run a separate warm-up play with `throttle: 1` (or a single
> `andrewich.ai.llm_extract` on `localhost`), then run the real play — every task will see the
> finished file.

## Backends

| Backend | How | When |
|---|---|---|
| `python` (default) | `llama-cpp-python` inside the module process | up to a few dozen hosts; no dependencies except the wheel |
| `server` | OpenAI-compatible API, grammar in the request body | many hosts, hot model, shared process |
| `mock` | canned answer | CI and tests, only with `allow_mock: true` |

The server runs without any `llama.cpp` binaries:

```bash
./scripts/run-llama-server.sh --port 8080
# or scripts/llama-server.service for systemd
```

## Repository layout

```
galaxy.yml                 collection metadata (namespace andrewich, name ai)
meta/runtime.yml           requires_ansible
README.md / README.ru.md   this file, English and Russian
docs/contract.md           the full contract (parameters, errors, policies, measurements)
docs/contract.ru.md        same in Russian
plugins/module_utils/      llm_core.py — grammar, validation, cache, backends
plugins/modules/           llm_extract.py — thin AnsibleModule wrapper
plugins/lookup/            llm_extract.py — the same core for templates
prompts/                   model instructions, versioned
schemas/                   JSON Schema for nested structures
fixtures/                  real vendor payloads (Dell, HPE, Inspur, Huawei, Fujitsu)
playbooks/                 end-to-end examples
tests/                     unit tests, no network, no model required
scripts/                   model download, llama-server, build & install
```

## Development

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # 190 tests
./scripts/build-and-install.sh                                   # build + install into ./.collections
./scripts/run-playbook.sh playbooks/introspect_llm_extract.yml   # end-to-end, real model + mock
```

`ansible.cfg` points `collections_paths` at `./.collections`, so playbooks always use the
installed collection exactly as a user would.

## Measurements

2 vCPU, no GPU, LFM2-350M-Extract Q4_K_M:

| Scenario | Result |
|---|---|
| 294 chars of compact text, 10 fields | 10/10 facts, 492 in / 131 out tokens, ~6 s |
| the same text again | `cache_hit: true`, `attempts: 0`, ~0.01 s |
| document missing 8 of 10 fields | 2 facts; 8 invented values caught and removed, `missing` filled |
| `backend: server` (`python -m llama_cpp.server`) | same facts, ~7.4 s |
| `on_too_long: split`, 1.2 KB, 2 chunks | same 10 facts, ~14 s |
| model download, 219 MB | 2.7 s, `download: fresh`, then `cached` |

What the research showed: **350M is weak on this task on its own** (~47% of fields), which is why
grounding, `on_ungrounded` and a strict input budget exist. Input compaction belongs outside the
module and should look like `key = value`: long JSON paths confuse the model
(`Oem.Dell...` → `vendor: "Oem.Dell"`), short keys give 10/10.

## Trust boundary and limitations

- Output is JSON only. YAML/XML/free text are not supported: the grammar cannot guarantee them.
- The module downloads the model and nothing else; it never executes anything. The result is
  data — do not feed it to `shell`/`command` without an `assert` on the schema and values.
- Prompts and schemas are trusted artifacts; the document is untrusted and is never executed.
- There is no deterministic alias/JSON-path layer: `aliases` in `fields` is rejected with
  `schema_invalid`. That step belongs before the module.
- Fact projection differs from the validated envelope: `null` and ungrounded values are removed,
  so facts validate against `projection_schema`, not the original schema (see
  [the contract](docs/contract.md), §5).
- 350M suits 0.5–1.5 KB of clean text; for more use `on_too_long: split` or a 1.2B model
  (93% of fields unaided, three times slower).
- `ansible-test sanity` has not been run yet (it needs `pylint`/`voluptuous`); the collection
  layout is ready for it.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
