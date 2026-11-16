# -*- coding: utf-8 -*-
# Copyright: (c) 2026
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""lookup(local.llm.llm_extract) - text -> JSON, computed inline in a template.

Same engine as the module, without facts and without delegate_to:

    - debug:
        msg: "{{ lookup('local.llm.llm_extract', payload,
                        prompt_file='prompts/redfish_extract.txt',
                        fields=fields) }}"

Returns a one element list containing the extracted object; with the default
``wantlist=False`` a single result is flattened to the object itself.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
name: llm_extract
short_description: Turn unstructured text into JSON with a local GGUF model
description:
  - Runs the same engine as the C(local.llm.llm_extract) module but returns the value to the
    template, which is the natural form for "compute a variable" tasks.
  - The first positional term is the text; every module option can be passed as a keyword
    argument.
options:
  _terms:
    description: Text to process. Alternatively pass I(text) or I(src) as a keyword.
    required: false
  _input:
    description: Not used.
  prompt_file:
    description: Path to the instruction file.
    type: path
  prompt:
    description: Inline instruction.
    type: str
  fields:
    description: Shorthand field list, as in the module.
    type: list
    elements: dict
  json_schema:
    description: Full JSON Schema.
    type: dict
  model_path:
    description: GGUF model path.
    type: path
  model_url:
    description: HTTPS URL of a .gguf file; downloaded once into I(model_dir).
    type: str
  model_dir:
    description: Directory for downloaded models (default C(<cache_dir>/models)).
    type: path
  model_sha256:
    description: Expected SHA-256 of the model file.
    type: str
  strict_download:
    description: Refuse to download without I(model_sha256).
    type: bool
    default: false
  refresh:
    description: Re-download when the local copy does not match.
    type: bool
    default: false
  download_timeout:
    description: Timeout of one download attempt, seconds.
    type: int
    default: 600
  backend:
    description: C(python), C(server) or C(mock).
    type: str
    default: python
  server_url:
    description: Base URL of the inference server.
    type: str
    default: http://127.0.0.1:8080
  max_input_chars:
    description: Budget for the text.
    type: int
    default: 1200
  on_too_long:
    description: C(fail), C(truncate) or C(split).
    type: str
    default: fail
  merge:
    description: Merge mode for chunks.
    type: str
    default: first
  on_missing:
    description: C(allow) or C(fail).
    type: str
    default: allow
  on_invalid:
    description: C(retry), C(fail) or C(return_raw).
    type: str
    default: retry
  grounding:
    description: C(substring) or C(none).
    type: str
    default: substring
  on_ungrounded:
    description: C(drop), C(keep) or C(fail) for values not found in the source.
    type: str
    default: drop
  min_grounded:
    description: Grounding threshold.
    type: float
    default: 0.0
  strict_enum:
    description: Hard constrain enum values.
    type: bool
    default: false
  cache:
    description: Enable the on-disk cache.
    type: bool
    default: true
  cache_dir:
    description: Cache directory.
    type: path
  temperature:
    description: Sampling temperature.
    type: float
    default: 0.0
  seed:
    description: Sampling seed.
    type: int
    default: 0
  max_tokens:
    description: Token budget of the first attempt.
    type: int
    default: 320
  retries:
    description: Extra attempts, each warmer and with a larger token budget.
    type: int
    default: 1
  n_ctx:
    description: Model context size.
    type: int
    default: 4096
  n_threads:
    description: CPU threads for in-process inference.
    type: int
    default: 0
  timeout:
    description: Timeout of a single call, seconds.
    type: int
    default: 120
  mock_response:
    description: Canned answer for C(backend=mock).
    type: str
  allow_mock:
    description: Must be true to use C(backend=mock); refused by default.
    type: bool
    default: false
  debug:
    description: Include the raw output in C(llm).
    type: bool
    default: false
author:
  - Sysadmin toolkit
"""

EXAMPLES = r"""
- name: Extract facts in a template, without touching ansible_facts
  set_fact:
    hardware: >-
      {{ lookup('local.llm.llm_extract', payload,
                prompt_file='prompts/redfish_extract.txt',
                fields=redfish_fields,
                model_path='/opt/llm/models/LFM2-350M-Extract-Q4_K_M.gguf') }}

- name: Use it directly in a message
  debug:
    msg: "serial={{ lookup('local.llm.llm_extract', text, fields=[{'name': 'serial', 'type': 'string'}], prompt='Extract the serial number.', backend='mock', mock_response='{\"serial\": \"ABC\"}')['serial'] }}"
"""

RETURN = r"""
_raw:
  description: A one element list with the extracted JSON object.
  type: list
  elements: dict
"""

import os
import sys

from ansible.errors import AnsibleError
from ansible.plugins.lookup import LookupBase

# Reuse the collection's module_utils even when the lookup runs from a checkout.
try:
    from ansible_collections.local.llm.plugins.module_utils import llm_core as core
except ImportError:  # pragma: no cover - only for ad-hoc imports in tests
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(os.path.dirname(_here), "module_utils"))
    import llm_core as core  # type: ignore


DEFAULTS = {
    "text": None, "src": None, "prompt": None, "prompt_file": None,
    "fields": None, "json_schema": None, "result_key": "llm_result",
    "model_path": None, "model_id": None, "backend": "python",
    "server_url": "http://127.0.0.1:8080", "max_input_chars": 1200,
    "on_too_long": "fail", "merge": "first", "on_missing": "allow",
    "on_invalid": "retry", "grounding": "substring", "on_ungrounded": "drop",
    "min_grounded": 0.0,
    "strict_enum": False, "cache": True, "cache_dir": None, "temperature": 0.0,
    "seed": 0, "max_tokens": 320, "retries": 1, "n_ctx": 4096, "n_threads": 0,
    "timeout": 120, "mock_response": None, "debug": False, "check_mode": False,
    "allow_mock": False, "model_url": None, "model_dir": None, "model_sha256": None,
    "strict_download": False, "refresh": False, "download_timeout": 600,
}


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        params = dict(DEFAULTS)
        params.update(kwargs)

        if terms:
            if len(terms) > 1:
                raise AnsibleError("local.llm.llm_extract: exactly one text term is expected")
            params["text"] = terms[0]
        if not params.get("text") and not params.get("src"):
            raise AnsibleError("local.llm.llm_extract: pass the text as a term, or use text=/src=")

        try:
            core.validate_params(params, strict_unknown=True)
        except core.ContractError as exc:
            raise AnsibleError("local.llm.llm_extract: %s" % exc.message)

        if params.get("on_invalid") == "return_raw":
            raise AnsibleError(
                "local.llm.llm_extract: on_invalid=return_raw is not supported by the lookup; "
                "use the module when you need the raw model output"
            )

        try:
            data, meta = core.extract(params)
        except core.ContractError as exc:
            raise AnsibleError(
                "local.llm.llm_extract: %s (error_code: %s)" % (exc.message, exc.code)
            )
        except core.BackendError as exc:
            raise AnsibleError(
                "local.llm.llm_extract: %s (error_code: %s)" % (exc.message, exc.code)
            )

        if not meta.get("ok"):
            raise AnsibleError(
                "local.llm.llm_extract: the model answer was rejected (%s)"
                % ", ".join(meta.get("errors") or ["invalid output"])
            )
        if meta.get("unverified"):
            self._display.warning(
                "local.llm.llm_extract: %d extracted value(s) not found in the source: %s"
                % (len(meta["unverified"]),
                   ", ".join(item["path"] for item in meta["unverified"]))
            )
        return [data]
