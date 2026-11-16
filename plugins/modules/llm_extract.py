#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: llm_extract
short_description: Turn unstructured text into JSON with a small local GGUF model
description:
  - Sends a caller supplied prompt and a piece of unstructured text to a local GGUF model
    and returns JSON validated against a caller supplied schema.
  - The model runs where the module executes, normally the controller
    (C(delegate_to=localhost)); inference itself never leaves the host.
  - The only network contact the module can make is a one-time HTTPS download of the GGUF
    file, and only when I(model_url) is passed explicitly.
  - Output is constrained by a GBNF grammar generated from the schema. Nullable fields
    really can come back as C(null), so "the document does not say" is expressible and is
    reported in C(llm.missing) instead of being filled with a nearby value.
  - The module never fetches data and never executes anything. Text comes from C(uri),
    C(command), C(slurp) or a lookup.
version_added: "2.15"
options:
  text:
    description:
      - Text to process.
      - Accepts a string, or a dict/list because Ansible converts JSON-looking template
        results into data structures; those are serialised back to compact JSON. Use
        I(src) when byte-exact input matters.
      - Mutually exclusive with I(src).
    type: raw
  src:
    description: Path of a file with the text, read on the machine running the module.
    type: path
  prompt:
    description: Inline instruction, for tests and one-off tasks.
    type: str
  prompt_file:
    description:
      - Path to the instruction file. Preferred, because the prompt is a versioned
        artifact and not a string buried in a task.
      - The instruction must not embed the document; text is passed separately so trusted
        instructions and untrusted data stay in different chat messages.
      - The module appends a compact field list built from the schema to the system
        message; a full JSON Schema would cost about 68% of the prompt tokens.
    type: path
  fields:
    description:
      - Shorthand schema for flat JSON.
      - Supported keys are I(name), I(type), I(description) and I(enum). Anything else,
        including C(aliases), fails with C(schema_invalid) instead of being ignored.
      - Mutually exclusive with I(json_schema).
    type: list
    elements: dict
    suboptions:
      name:
        description: Result key.
        type: str
        required: true
      type:
        description: Value type.
        type: str
        choices: [string, integer, number, boolean, list]
        default: string
      description:
        description: What to extract, in the language the model understands best.
        type: str
      enum:
        description: Allowed values, also used for post-hoc normalisation.
        type: list
        elements: str
  json_schema:
    description:
      - Full JSON Schema, for nested structures.
      - Supported subset is object with I(properties), I(required) and
        C(additionalProperties=false); string with I(enum) and I(maxLength); integer;
        number; boolean; array of scalars with I(maxItems). Anything else fails with
        C(schema_unsupported) instead of silently degrading.
      - Properties outside I(required) are optional and may be null or absent.
      - Mutually exclusive with I(fields).
    type: dict
  result_key:
    description: Key under C(ansible_facts) that receives the extracted JSON.
    type: str
    default: llm_result
  model_path:
    description:
      - Path to the GGUF model file. Required for the C(python) backend unless I(model_url)
        is set.
      - Mutually exclusive with I(model_url).
    type: path
  model_url:
    description:
      - Direct HTTPS URL of a C(.gguf) file. The module downloads it once into I(model_dir)
        and reuses the local copy afterwards.
      - Using this option is itself the consent to make one network request; there is no
        separate switch. Only the C(python) backend can download, because C(server) loads
        the model itself.
      - Mutually exclusive with I(model_path).
    type: str
  model_dir:
    description: Directory for downloaded models. Defaults to C(<cache_dir>/models).
    type: path
  model_sha256:
    description:
      - Expected SHA-256 of the model file. Strongly recommended; without it a truncated
        download can only be detected by the GGUF magic and the Content-Length.
    type: str
  strict_download:
    description: Refuse to download without I(model_sha256) (C(model_checksum_required)).
    type: bool
    default: false
  refresh:
    description:
      - Re-download the model when the local copy does not match the recorded size or hash.
      - Without it a mismatch is an error (C(model_stale)).
    type: bool
    default: false
  download_timeout:
    description: Timeout of one download attempt, in seconds.
    type: int
    default: 600
  model_id:
    description: Identifier used in the cache key and in the result metadata.
    type: str
  backend:
    description:
      - C(python) runs the model in-process through C(llama-cpp-python).
      - C(server) talks to a running C(llama-server) or C(python -m llama_cpp.server),
        with the grammar passed in the request body.
      - C(mock) returns I(mock_response) verbatim; for CI.
    type: str
    choices: [python, server, mock]
    default: python
  server_url:
    description: Base URL of the inference server.
    type: str
    default: http://127.0.0.1:8080
  max_input_chars:
    description:
      - Budget for the text handed to the model, in characters.
      - The 350M default of 1200 is measured; exceeding it is an error by default.
    type: int
    default: 1200
  on_too_long:
    description: C(fail), C(truncate) or C(split) (chunks merged per I(merge)).
    type: str
    choices: [fail, truncate, split]
    default: fail
  merge:
    description: How to merge chunk results.
    type: str
    choices: [first, concat, deep_merge]
    default: first
  on_missing:
    description: C(fail) turns fields the model reported as C(null) into an error.
    type: str
    choices: [allow, fail]
    default: allow
  on_invalid:
    description:
      - C(retry) makes additional attempts (see I(retries)) and keeps the best answer.
      - C(fail) stops with C(invalid_json) or C(schema_violation).
      - C(return_raw) returns C(ok=false), C(raw_output) and no facts, without failing.
    type: str
    choices: [retry, fail, return_raw]
    default: retry
  grounding:
    description:
      - C(substring) checks each extracted scalar against the source text using token
        boundaries, so C(On) is not confirmed by C(Only) and 536 is not confirmed by 1536.
      - Enum values are checked too, using the value before normalisation, so a vendor of
        C(HPE) is reported when the document says C(Dell).
      - Booleans are not checkable and are excluded from C(min_grounded) denominator.
      - C(none) disables the check; correct for classification and summarisation.
    type: str
    choices: [substring, none]
    default: substring
  on_ungrounded:
    description:
      - C(drop) (default) removes values that cannot be found in the source and reports
        them in C(missing) and C(llm.dropped) - a 350M model does invent plausible values
        when the document is silent, so the safe default is to keep the fact out.
      - C(keep) returns them and only flags them in C(llm.unverified).
      - C(fail) stops with C(ungrounded_values).
    type: str
    choices: [drop, keep, fail]
    default: drop
  min_grounded:
    description: Fail when the share of checked values found in the source is below this ratio.
    type: float
    default: 0.0
  strict_enum:
    description:
      - Hard constrain I(enum) values in the grammar.
      - Off by default; a 350M model forced to pick from a list it cannot copy emits null.
    type: bool
    default: false
  cache:
    description:
      - Cache results on disk.
      - The key covers the model, backend, server URL, prompt, schema, input, sampling and
        the quality policies; grounding and on_missing are re-applied to cached results.
    type: bool
    default: true
  cache_dir:
    description: Cache directory, created with mode 0700, files 0600.
    type: path
  temperature:
    description: Sampling temperature. Greedy decoding is recommended by the model authors.
    type: float
    default: 0.0
  seed:
    description: Sampling seed for the first attempt.
    type: int
    default: 0
  max_tokens:
    description: Token budget of the first attempt; retries double it.
    type: int
    default: 320
  retries:
    description:
      - Extra attempts, only with I(on_invalid=retry).
      - Each attempt raises the temperature by 0.1 and doubles I(max_tokens); without that
        a retry at temperature 0 with a fixed seed would return byte-identical output.
    type: int
    default: 1
  n_ctx:
    description: Model context size.
    type: int
    default: 4096
  n_threads:
    description: CPU threads for in-process inference. 0 lets the backend decide.
    type: int
    default: 0
  timeout:
    description: Timeout of a single inference call, in seconds.
    type: int
    default: 120
  mock_response:
    description: Canned answer for I(backend=mock), a JSON string.
    type: str
  allow_mock:
    description:
      - Must be set to C(true) for I(backend=mock).
      - The mock backend returns I(mock_response) verbatim instead of inference, so it is
        refused by default, because a playbook that accidentally ships with C(mock) must
        fail loudly rather than return invented facts.
    type: bool
    default: false
  debug:
    description: Include the raw model output in C(llm.raw_output).
    type: bool
    default: false
notes:
  - The model file is not shipped inside the module. Provide I(model_path) to an existing
    file, or I(model_url) to let the module download it once.
  - Downloads are HTTPS only, streamed to a private temporary file and moved into place
    atomically; concurrent tasks are serialised with a file lock.
  - Model files and the cache are stored under I(model_dir), default C(<cache_dir>/models).
  - The result is data. Do not pipe it into C(shell) or C(command) without validating it
    against the schema and asserting on the values you rely on.
seealso:
  - description: The same engine as a lookup plugin.
    module: andrewich.ai.llm_extract
author:
  - Sysadmin toolkit
"""

EXAMPLES = r"""
- name: Extract structured facts from a Redfish payload
  uri:
    url: "https://{{ bmc }}/redfish/v1/Systems/1"
    url_username: "{{ bmc_user }}"
    url_password: "{{ bmc_password }}"
    return_content: true
  register: payload

- name: Normalise the vendor payload
  andrewich.ai.llm_extract:
    text: "{{ payload.content }}"
    prompt_file: prompts/redfish_extract.txt
    fields: "{{ redfish_fields }}"   # list of name/type/description items
    model_path: /opt/llm/models/LFM2-350M-Extract-Q4_K_M.gguf
  delegate_to: localhost
  register: extracted

- name: Same, but let the module fetch the model once into its cache
  andrewich.ai.llm_extract:
    text: "{{ payload.content }}"
    prompt_file: prompts/redfish_extract.txt
    fields: "{{ redfish_fields }}"   # list of name/type/description items
    model_url: https://huggingface.co/LiquidAI/LFM2-350M-Extract-GGUF/resolve/main/LFM2-350M-Extract-Q4_K_M.gguf
    model_sha256: 687a31c3e7864647aa181e1feb156e4e5da33978c174d7dbf0d289f6014a5621
    strict_download: true
  delegate_to: localhost

- name: Fail loudly when the document does not contain the facts
  assert:
    that:
      - extracted.llm.missing | length == 0
      - extracted.ansible_facts.llm_result.serial is defined
"""

RETURN = r"""
ansible_facts:
  description: Extracted JSON under I(result_key).
  returned: on success
  type: dict
llm:
  description: Metadata about the call, the model, the prompt hash and the checks.
  returned: always
  type: dict
  sample:
    ok: true
    backend: python
    cache_hit: false
    attempts: 1
    input_chars: 294
    tokens: {in: 658, out: 123}
    missing: [bios_version]
    unverified: [{path: bios_version, value: "1.2"}]
    checked: 9
    grounded: 8
    dropped: 1
plan:
  description: Present on failures; describes what the module would have done.
  returned: on failure
  type: dict
"""

import os
import sys

from ansible.module_utils.basic import AnsibleModule

try:
    from ansible_collections.andrewich.ai.plugins.module_utils import llm_core as core
except ImportError:  # source checkout, e.g. module run directly by the tests
    sys.path.insert(
        0,
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "module_utils"),
    )
    import llm_core as core


def main():
    module = AnsibleModule(
        argument_spec=dict(
            text=dict(type="raw"),
            src=dict(type="path"),
            prompt=dict(type="str"),
            prompt_file=dict(type="path"),
            fields=dict(
                type="list",
                elements="dict",
                options=dict(
                    name=dict(type="str", required=True),
                    type=dict(type="str", default="string",
                              choices=["string", "integer", "number", "boolean", "list"]),
                    description=dict(type="str"),
                    enum=dict(type="list", elements="str"),
                ),
            ),
            json_schema=dict(type="dict"),
            result_key=dict(type="str", default="llm_result"),
            model_path=dict(type="path"),
            model_url=dict(type="str"),
            model_dir=dict(type="path"),
            model_sha256=dict(type="str"),
            strict_download=dict(type="bool", default=False),
            refresh=dict(type="bool", default=False),
            download_timeout=dict(type="int", default=600),
            model_id=dict(type="str"),
            backend=dict(type="str", default="python", choices=["python", "server", "mock"]),
            server_url=dict(type="str", default="http://127.0.0.1:8080"),
            max_input_chars=dict(type="int", default=1200),
            on_too_long=dict(type="str", default="fail", choices=["fail", "truncate", "split"]),
            merge=dict(type="str", default="first", choices=["first", "concat", "deep_merge"]),
            on_missing=dict(type="str", default="allow", choices=["allow", "fail"]),
            on_invalid=dict(type="str", default="retry",
                            choices=["retry", "fail", "return_raw"]),
            grounding=dict(type="str", default="substring", choices=["substring", "none"]),
            on_ungrounded=dict(type="str", default="drop", choices=["drop", "keep", "fail"]),
            min_grounded=dict(type="float", default=0.0),
            strict_enum=dict(type="bool", default=False),
            cache=dict(type="bool", default=True),
            cache_dir=dict(type="path"),
            temperature=dict(type="float", default=0.0),
            seed=dict(type="int", default=0),
            max_tokens=dict(type="int", default=320),
            retries=dict(type="int", default=1),
            n_ctx=dict(type="int", default=4096),
            n_threads=dict(type="int", default=0),
            timeout=dict(type="int", default=120),
            mock_response=dict(type="str"),
            allow_mock=dict(type="bool", default=False),
            debug=dict(type="bool", default=False),
        ),
        mutually_exclusive=[
            ["text", "src"], ["prompt", "prompt_file"], ["fields", "json_schema"],
            ["model_path", "model_url"],
        ],
        required_one_of=[["text", "src"], ["prompt", "prompt_file"], ["fields", "json_schema"]],
        supports_check_mode=True,
    )

    params = dict(module.params)
    params["check_mode"] = bool(module.check_mode)
    try:
        data, meta = core.extract(params)
    except core.ContractError as exc:
        module.fail_json(
            msg=exc.message, error_code=exc.code, contract=core.CORE_VERSION,
            plan=exc.plan or None,
        )
        return
    except core.BackendError as exc:
        module.fail_json(
            msg=exc.message, error_code=exc.code, contract=core.CORE_VERSION,
            plan=exc.plan or None,
        )
        return

    if meta.get("plan_only"):
        module.exit_json(changed=False, llm=meta, plan=meta.get("plan"))
        return

    if not meta.get("ok"):
        # No facts at all: a failed extraction must not overwrite an existing value.
        module.exit_json(changed=False, llm=meta)
        return

    module.exit_json(
        changed=False,
        ansible_facts={module.params["result_key"]: data},
        llm=meta,
    )


if __name__ == "__main__":
    main()
