#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="${IMAGE_NAME:-cybergym/dognavy:latest}"
BASE_IMAGE="${BASE_IMAGE:-registry.h.pjlab.org.cn/ailab-safevlagent-safevlagent_cpu/yangzhonghao:cybergym-dognavy}"

docker build \
    --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
    --tag "${IMAGE_NAME}" \
    "${SCRIPT_DIR}"
