# `andrewich.ai.llm_extract` contract

Status: core **2.1**, collection **0.4.0**. All P0/P1 review findings and the issues the review
missed are closed (§16). A few remarks are deliberately out of scope (§17).

Goal: unstructured text + prompt + schema → JSON. Everything domain-specific (Redfish, logs,
tickets) lives in prompts, schemas and playbooks, never in the code.

## 1. Delivery

```
andrewich.ai (the repository root is the collection root)
  galaxy.yml, meta/runtime.yml
  plugins/module_utils/llm_core.py    # the whole core: grammar, checks, cache, backends
  plugins/modules/llm_extract.py      # thin wrapper: DOCUMENTATION + AnsibleModule
  plugins/lookup/llm_extract.py       # the same core for templates
  plugins/filter/redfish_identity.py  # Redfish "path = value" compaction (example, no external tools)
  README.md / README.ru.md
  docs/contract.md, docs/contract.ru.md
```

```bash
ansible-galaxy collection install git+https://github.com/Andrewich/ansible-collection-ai.git,0.4.0
```

```yaml
- andrewich.ai.llm_extract:            # host facts
- set_fact:
    parsed: "{{ lookup('andrewich.ai.llm_extract', payload, prompt_file=..., fields=...) }}"
```

## 2. Pipeline

```
text|src ─► prepare ─► [per chunk: GBNF ─► backend ─► envelope]
                                   │
                    finalize_chunks: grounding against its own chunk
                                   ▼
                          merge ─► on_ungrounded ─► projection ─► facts
```

- **envelope** — validated model output (with `null`), before any policy; this is what gets cached.
- **projection** — what becomes facts: no `null`, no ungrounded values.

## 3. Parameters

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `text` | raw | — | str or dict/list (Ansible parses JSON) → serialised back |
| `src` | path | — | read **as bytes**, strict UTF-8, no newline translation |
| `prompt_file` / `prompt` | path / str | — | a file is preferred |
| `fields` | list[dict] | — | only `name/type/description/enum`; `aliases` → `schema_invalid` |
| `json_schema` | dict | — | nested structures, see §4 |
| `result_key` | str | `llm_result` | |
| `model_path` / `model_id` | path / str | — / basename | |
| `model_url` | str | — | HTTPS to a `.gguf`; mutually exclusive with `model_path`; passing it is consent to download |
| `model_dir` | path | `<cache_dir>/models` | where downloaded models are stored |
| `model_sha256` | str | — | expected SHA-256, strongly recommended |
| `strict_download` | bool | `false` | requires `model_sha256` (`model_checksum_required`) |
| `refresh` | bool | `false` | re-download on size/hash mismatch |
| `download_timeout` | int ≥ 1 | `600` | timeout of one attempt and of the lock wait |
| `backend` | str | `python` | `python` / `server` / `mock` |
| `server_url` | str | `http://127.0.0.1:8080` | **loopback only**, no redirects, no proxies |
| `max_input_chars` | int ≥ 1 | `1200` | |
| `on_too_long` | str | `fail` | `fail` / `truncate` / `split` |
| `merge` | str | `first` | see the table in §7 |
| `on_missing` | str | `allow` | `allow` / `fail` |
| `on_invalid` | str | `retry` | `retry` / `fail` / `return_raw` |
| `grounding` | str | `substring` | `substring` / `none` |
| `on_ungrounded` | str | **`drop`** | `drop` / `keep` / `fail` |
| `min_grounded` | float 0..1 | `0.0` | with `checked == 0` and a positive threshold — error |
| `strict_enum` | bool | `false` | |
| `cache` / `cache_dir` | bool / path | `true` / `~/.cache/ansible-llm` | |
| `temperature` ≥ 0 / `seed` | float / int | `0.0` / `0` | |
| `max_tokens` ≥ 1 | int | `320` | a retry doubles it |
| `retries` ≥ 0 | int | `1` | attempt N: `temperature+0.1`, `max_tokens×2`, `seed+1` |
| `n_ctx` ≥ 1 / `n_threads` ≥ 0 / `timeout` ≥ 1 | int | `4096` / `0` / `120` | |
| `mock_response` | str | — | only together with `allow_mock` |
| `allow_mock` | bool | `false` | without it `backend: mock` → `mock_not_allowed` |
| `debug` | bool | `false` | `raw_output`, `projection_schema` |

Invalid values (types, ranges, `choices`, mutually exclusive pairs, unknown lookup keys) produce
`invalid_params` before any inference.

## 4. Schema: what is allowed

| Rule | Behaviour |
|---|---|
| `enum` | only on `string` and on `items` of a string array; otherwise `schema_unsupported` |
| `enum: [null]` | means "the value must be null" |
| `maxLength` / `maxItems` | integers ≥ 0; `0` is a valid limit; > 120 / > 8 is `schema_unsupported` |
| schema root | must be an object, otherwise `schema_unsupported` in preflight |
| `required` | missing keys are not allowed; non-`required` properties are nullable |
| empty array | preserved as `[]`; only `null` lands in `missing` |

GBNF is generated from the schema: string and array limits come from `maxLength`/`maxItems`
(capped at 120/8), keys and enum literals are escaped as JSON strings. `strict_enum` enables
hard literals; it is off by default.

## 5. Three result contracts

| Object | Guarantee |
|---|---|
| envelope | passes `validate_instance(envelope, schema)`, including `required` and `null` |
| projection (`ansible_facts.llm_result`) | no `null`, no ungrounded values; `required` relaxed, schema is `projection_schema()` |
| `llm` metadata | `missing`, `unverified`, `dropped`, `checked`, `grounded`, `rejected`, `normalized`, `coerced` |

So "the facts are valid against the original schema" is no longer claimed: the original schema
validates the envelope, while the projection is described by `projection_schema` (available with
`debug`).

## 6. Grounding: honest semantics

The check answers "does the value occur in **the chunk that produced it**", with token
boundaries (`[0-9A-Za-zА-Яа-яЁё_]`), using the value before normalisation.

| Before | Now |
|---|---|
| `On` was confirmed by the word `Only` | `On` ≠ `Only` |
| `536` was confirmed by `1536` | `536` ≠ `1536` |
| enums were not checked | checked; `HPE` in a Dell document is ungrounded |
| `Dell Inc.` in arrays was not normalised | normalised by item rules |
| the tail under `truncate` "confirmed" values | the tail never reached the model, so it proves nothing |
| chunk 2 could not rescue a value from chunk 1 | a grounded candidate wins during the merge |

Grounding does **not** attribute a value to a particular record field (out of scope, §17): it
checks presence in the text that was sent. `checked`/`grounded` form an honest denominator;
booleans and `null` are not counted.

## 7. Chunk merge

| Mode | Scalars | Lists |
|---|---|---|
| `first` | first grounded non-null | first list |
| `concat` | first grounded non-null | plain concatenation, duplicates kept |
| `deep_merge` | first grounded non-null | concatenation with de-duplication, dictionaries recursively |

## 8. `on_ungrounded` policy

| Mode | Behaviour |
|---|---|
| `drop` (default) | values not found are removed and appear in `missing` and `llm.dropped` |
| `keep` | returned and flagged in `llm.unverified` |
| `fail` | error `ungrounded_values` |

## 9. Cache

Envelope format v2: `{v: 2, chunks: [envelope, ...]}` — one per chunk, with `data` (validated
data including `null`), `missing`, `normalized`, `coerced`, `rejected`. On a hit the **same**
`finalize_chunks` runs as for a fresh answer, so `min_grounded`, `on_missing` and `on_ungrounded`
are applied again.

- Key: core version, model (id + size + mtime), backend, `server_url`, prompt/schema/input
  hashes, `temperature`, `seed`, `max_tokens`, `strict_enum`, `on_too_long`, `merge`,
  `on_missing`, `grounding`, `on_ungrounded`, `max_input_chars`, `min_grounded`, cache format.
- Write: `mkstemp` inside the cache directory (0600), `fsync`, atomic `os.replace`; the directory
  must be owned by the euid and must not be group/world-writable, otherwise the cache is disabled.
- Read: wrong version, wrong shape or data that fails the schema is a miss.
- `check_mode`: inference runs, the cache is not written.

## 10. Backends

| Backend | How | Constraint |
|---|---|---|
| `python` | `llama-cpp-python` in-process | loads the model per task |
| `server` | OpenAI-compatible API, grammar in the body | **loopback only**, no proxies, no redirects |
| `mock` | canned answer | only with `allow_mock: true` |

## 11. Model download

If `model_url` is set, `prepare()` downloads the model before inference. This is the **only**
network contact of the module; inference is always local.

| Step | Behaviour |
|---|---|
| Eligibility | `https` and `.gguf` only; `backend: python`; `model_url` plus `model_path` — `invalid_params` |
| File name | `<sha256(url)[:16]>-<basename>.gguf` in `model_dir` — different URLs never overwrite each other |
| Already present | `model_sha256` matches or the recorded size matches — `model.download: "cached"`, no network |
| Mismatch | `refresh=false` → `model_stale`; `refresh=true` → download again |
| `check_mode` | nothing is downloaded or created; the module returns `plan_only` with the plan |
| Concurrency | lock file + `fcntl.flock`; the file is re-checked after the lock, so one process downloads |
| Space | `os.statvfs` with a ×1.1 margin → `insufficient_space` |
| Write | ~1 MB chunks into `mkstemp` (0600) → `fsync` → `os.replace`; the file is never read into memory |
| Integrity | first 4 bytes `GGUF` (`model_invalid`), `Content-Length` check (`download_failed`), streaming SHA-256 (`model_checksum_mismatch`); the broken file is removed |
| Retries | 3 attempts with backoff on network errors; timeout → `download_timeout`, exhaustion → `download_failed` |
| Metadata | `llm.model`: `download` (`fresh` / `cached` / `skipped` / `false`), `url`, `sha256`, `bytes`, `seconds`; a `<name>.meta.json` (0600) sits next to the file |

Without `model_sha256`, `llm.warnings` gets a warning: only the GGUF magic and `Content-Length`
protect against substitution or truncation. `strict_download: true` turns that into
`model_checksum_required`.

**URL trust.** The link must point to a file in a pinned revision (tag, commit, explicit
version), not to `main`/`latest`: content behind a floating address can change, and then either
`model_stale` fires or (without `model_sha256`) a different model is silently used. The module
never talks to the HF API, does not support `hf:org/repo`, tokens or gated models — only a direct
HTTPS URL.

## 12. Result

```yaml
ansible_facts:
  llm_result: { ... projection ... }
llm:
  ok: true
  model: {id, path, size, download, url, sha256, bytes, seconds}
  prompt_sha256 / schema_sha256 / input_sha256
  input_chars: 294
  backend: python
  cache_hit: false
  check_mode: false
  attempts: 1
  chunks: 1
  tokens: {in: 492, out: 131}
  elapsed_ms: 5600
  missing: []
  unverified: []
  dropped: 0
  checked: 9
  grounded: 9
```

With `ok: false` the module **does not return** `ansible_facts` (it must not overwrite a previous
value), and the lookup always raises.

## 13. Errors

`invalid_params`, `input_empty`, `input_not_found`, `input_unreadable`, `input_encoding`,
`input_too_long`, `prompt_not_found`, `schema_invalid`, `schema_unsupported`,
`model_not_found`, `backend_unavailable`, `invalid_server_url`, `mock_not_allowed`,
`invalid_json`, `schema_violation`, `missing_fields`, `grounding_failed`,
`ungrounded_values`, `timeout`, `download_failed`, `download_timeout`, `download_busy`,
`insufficient_space`, `model_checksum_mismatch`, `model_checksum_required`, `model_invalid`,
`model_stale`.

llama.cpp failures never surface as tracebacks: an incompatible or corrupt model file yields
`model_invalid` with the llama-cpp-python version in the message, and an `n_ctx` overflow yields
`input_too_long` with a hint to raise `n_ctx` or lower `max_input_chars`.

## 14. Verification

```
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
→ Ran 197 tests ... OK

./scripts/build-and-install.sh
./scripts/run-playbook.sh playbooks/introspect_llm_extract.yml
→ PLAY RECAP: ok=20 failed=0 ignored=2
```

Measurements (2 vCPU, LFM2-350M-Extract Q4_K_M):

| Scenario | Result |
|---|---|
| 294 chars of compact text, 10 fields | 10/10, 492 in / 131 out, ~5.6 s |
| document missing 8 fields | 2 facts, 8 invented values caught and removed |
| second run | `cache_hit: true`, `attempts: 0` |
| `on_too_long: split`, 2 chunks | values are grounded against their own chunks |
| model download, 219 MB via `model_url` | 2.7 s, `download: fresh`, same sha256, 10/10 facts |
| second run with the same URL | no network, `download: cached` |

## 15. Trust boundary

- The prompt and the schema are **trusted artifacts**: they go into the system message.
- The document is **untrusted**: it goes into the user message and its output is never executed.
  The grammar constrains the shape, not the meaning: do not feed the result to
  `shell`/`command`/`uri` without an `assert` on the schema and values.
- The defaults `on_ungrounded: drop` and `allow_mock: false` exist so that an accidental
  configuration cannot produce invented facts.
- `model_url` is trusted configuration: it points at a specific file in a pinned revision, not at
  a floating branch; `model_sha256` pins the content.

## 16. What was fixed

| # | Finding | Fix |
|---|---|---|
| P0-1 | enum laundering (`Only → On`) | token-boundary normalisation, single match only |
| P0-2 | cache bypassed `min_grounded` | envelope v2 + shared `finalize_chunks`; key extended |
| P0-4 | enums on non-strings, zero limits | "keyword × type" table, `is not None`, limits from the schema |
| P0-5 | unsafe cache writes | mkstemp/0600, directory check, payload validation |
| P0-6 | egress via `server_url` | loopback only, no credentials, no redirects |
| P1-1 | null paths lost from `missing` | set union |
| P1-2 | empty arrays vanished; schema root | `[]` preserved, root must be an object |
| P1-5 | merge before grounding | per-chunk grounding, grounded candidate wins |
| P1-6 | weak validation, hang | single `validate_params`, `max_input_chars ≥ 1` |
| P1-8 | `src` not byte-exact | byte reads, new error codes |
| P1-9 | `return_raw`/facts | `raw_output` always, no facts on `ok=false` |
| P1-10 | projection vs schema | two contracts, `projection_schema()` |
| P0-3 | "field-attributable" | **out of scope**: the wording means presence in the chunk |
| — | `backend: mock` in production | `allow_mock` defaults to `false` |
| — | enums in arrays unchecked | checked and normalised by item rules |
| — | zero denominator = 1.0 | error when `min_grounded > 0` and `checked == 0` |

## 17. Out of scope (deliberate)

P0-3 (attributing a value to a record field), P1-3 (GGUF content hash), P1-4 (path collisions like
`a.b`), P1-7 (hard in-process timeout), P2-1 (best-of-N), P2-4 (full Unicode), P2-5 (schema
injection — the schema is trusted), P3 (Galaxy publishing).
