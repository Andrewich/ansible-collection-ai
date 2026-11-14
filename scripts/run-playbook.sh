#!/usr/bin/env bash
# Run ansible-playbook from the project venv with the paths this sandbox allows.
#
#   ./scripts/run-playbook.sh playbooks/introspect_llm_extract.yml
#
# ANSIBLE_HOME keeps Ansible away from ~/.ansible, TMPDIR from the system temp area;
# both are redirected into the workspace so the run works without extra privileges.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ANSIBLE_HOME="${ANSIBLE_HOME:-$ROOT/.ansible}"
export TMPDIR="${TMPDIR:-$ROOT/.tmp}"
mkdir -p "$ANSIBLE_HOME" "$TMPDIR"

exec "$ROOT/.venv/bin/python" -m ansible.cli.playbook "$@"
