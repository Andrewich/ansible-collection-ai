#!/usr/bin/env bash
# Download the extraction model and the llama.cpp runtime.
#
#   ./scripts/fetch_model.sh            # model only (~220 MB)
#   ./scripts/fetch_model.sh --runtime  # model + llama-server binary (~23 MB)
#
# The model file is what the module needs. The runtime is only required when you
# prefer a separate llama-server daemon over llama-cpp-python inside the module.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODELS="${ROOT}/.models"
REPO="LiquidAI/LFM2-350M-Extract-GGUF"
FILE="LFM2-350M-Extract-Q4_K_M.gguf"
LLAMA_BUILD="b12318"

mkdir -p "${MODELS}"

if [[ -f "${MODELS}/${FILE}" ]]; then
    echo "model already present: ${MODELS}/${FILE}"
else
    echo "downloading ${FILE} ..."
    curl -fL --retry 3 -o "${MODELS}/${FILE}" \
        "https://huggingface.co/${REPO}/resolve/main/${FILE}"
fi

if [[ "${1:-}" == "--runtime" ]]; then
    if [[ -x "${ROOT}/vendor/llama/llama-server" ]]; then
        echo "runtime already present: ${ROOT}/vendor/llama"
    else
        echo "downloading llama.cpp ${LLAMA_BUILD} (CPU build) ..."
        mkdir -p "${ROOT}/vendor/llama"
        curl -fL --retry 3 -o /tmp/llama.tar.gz \
            "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_BUILD}/llama-${LLAMA_BUILD}-bin-ubuntu-x64.tar.gz"
        tar xzf /tmp/llama.tar.gz -C "${ROOT}/vendor/llama" --strip-components=1
        rm -f /tmp/llama.tar.gz
        echo "note: this build needs GLIBCXX 3.4.30+ (glibc 2.39 era)."
        echo "      On older distributions use the llama-cpp-python wheel instead."
    fi
fi

ls -lh "${MODELS}"
