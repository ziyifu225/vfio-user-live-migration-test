#!/usr/bin/env bash
set -euo pipefail
export EXPERIMENT_DIRECTORY
EXPERIMENT_DIRECTORY=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd -- "$EXPERIMENT_DIRECTORY/../.." && pwd)
exec nix run "$repo#qemu-physical-device-test" -- "$@"
