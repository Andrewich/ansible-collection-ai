# -*- coding: utf-8 -*-
# Copyright: (c) 2026
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""Filter: reduce a Redfish payload to identity lines, keeping each value's path.

    - set_fact:
        lines: "{{ payload_text | local.llm.redfish_identity }}"

Why a filter and not pure Jinja: the values that must be told apart (the system
manufacturer "FUJITSU" versus the mainboard vendor "MiTAC") differ only by nesting
depth, so the filter tracks a path stack while walking the document line by line.
Some vendor payloads are not valid JSON at all (Huawei iBMC mixes an object and an
array under "Processors"), which rules out from_json/json_query as well.

Kept lines:
  * top-level Manufacturer / Model - the identity of the whole server
  * Model values under a Processors path - the CPU strings themselves
  * shallow ProductName - an OEM alias of the system model
Everything else (mainboard vendor and model, CPU vendor, firmware, memory) is dropped.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import re

from ansible.errors import AnsibleFilterError

DOCUMENTATION = r"""
  name: redfish_identity
  short_description: Reduce a Redfish payload to identity lines with their paths
  description:
    - Walks a pretty printed Redfish payload line by line, tracks nesting by indentation
      and returns C(path = value) lines for the identity fields only.
    - Works on payloads that are not valid JSON, which rules out C(from_json) and
      C(json_query); the nesting path is what separates the system manufacturer from a
      mainboard or CPU vendor.
  options:
    _input:
      description: The payload as text (str or bytes).
      type: raw
      required: true
"""

EXAMPLES = r"""
- name: Compact the payload inside the playbook, no external tools
  ansible.builtin.set_fact:
    identity: "{{ payload_text | local.llm.redfish_identity }}"
"""

RETURN = r"""
  _value:
    description: Newline separated C(path = value) lines, in document order.
    type: str
"""

_KEY_VALUE = re.compile(r'^"([^"]+)"\s*:\s*(.*)$')
_SCALAR = re.compile(r'^"([^"]*)"$')


def _decode(text):
    if isinstance(text, bytes):
        return text.decode("utf-8")
    return text


def _scalar(value):
    """Return the unquoted scalar, or None when the value is not a plain string."""
    value = value.strip()
    if value.endswith(","):
        value = value[:-1].rstrip()
    match = _SCALAR.match(value)
    if match:
        return match.group(1)
    return None


def redfish_identity(text):
    """Return "path = value" lines for the identity fields of a Redfish payload."""
    text = _decode(text)
    if not isinstance(text, str):
        raise AnsibleFilterError("redfish_identity expects the payload as text")

    stack = []
    seen = set()
    lines = []
    for raw in text.splitlines():
        stripped = raw.lstrip(" ")
        if not stripped:
            continue
        indent = len(raw) - len(stripped)
        match = _KEY_VALUE.match(stripped)
        if not match:
            continue
        key, value = match.group(1), match.group(2)

        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        path = ".".join(item[1] for item in stack)

        is_system = key in ("Manufacturer", "Model") and indent <= 2
        is_cpu = key == "Model" and "Processors" in path
        is_product = key == "ProductName" and indent <= 4
        if not (is_system or is_cpu or is_product):
            continue

        scalar = _scalar(value)
        if scalar is None:
            continue
        line = "%s = %s" % (path, scalar)
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)

    return "\n".join(lines)


class FilterModule(object):
    def filters(self):
        return {"redfish_identity": redfish_identity}
