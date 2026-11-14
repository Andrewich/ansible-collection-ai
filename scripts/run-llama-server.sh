#!/usr/bin/env bash
# Keep the extraction model hot and serve it over the OpenAI-compatible API, using
# the llama-cpp-python wheel only - no llama.cpp binary, no glibc requirements.
#
#   ./scripts/run-llama-server.sh [--port 8080] [--model path.gguf]
#
# The module talks to it with:  backend: server, server_url: http://127.0.0.1:<port>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL="${ROOT}/.models/LFM2-350M-Extract-Q4_K_M.gguf"
PORT=8080
HOST=127.0.0.1
THREADS="$(nproc)"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model) MODEL="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --threads) THREADS="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[[ -f "$MODEL" ]] || { echo "model not found: $MODEL" >&2; exit 1; }

exec "${ROOT}/.venv/bin/python" -m llama_cpp.server \
    --model "$MODEL" \
    --host "$HOST" \
    --port "$PORT" \
    --n_ctx 4096 \
    --n_threads "$THREADS"
