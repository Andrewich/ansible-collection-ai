#!/usr/bin/env bash
# Build the collection and install it into ./.collections, which ansible.cfg uses.
#
#   ./scripts/build-and-install.sh
#
# Playbooks use the fully qualified name andrewich.ai.llm_extract, so the collection
# must be installed before they can run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="${ROOT}/dist"
TARGET="${ROOT}/.collections"

export ANSIBLE_HOME="${ANSIBLE_HOME:-$ROOT/.ansible}"
mkdir -p "$DIST" "$TARGET" "$ANSIBLE_HOME"
cd "$ROOT"

"${ROOT}/.venv/bin/python" -m ansible.cli.galaxy collection build . \
    --output-path "$DIST" --force "$@"
"${ROOT}/.venv/bin/python" -m ansible.cli.galaxy collection install \
    "$(ls -t "$DIST"/andrewich-ai-*.tar.gz | head -1)" \
    --collections-path "$TARGET" --force

echo "installed into ${TARGET}/ansible_collections/andrewich/ai"
