#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-2.1.222}"
IMAGE_NAME="${IMAGE_NAME:-cybergym/claudecode:latest}"

docker build \
    --build-arg "CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}" \
    --tag "${IMAGE_NAME}" \
    "${SCRIPT_DIR}"
