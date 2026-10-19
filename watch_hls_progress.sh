#!/usr/bin/env bash
set -euo pipefail
HLS_PROGRAM_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${HLS_PROGRAM_ROOT}/.venv/bin/python" -u "${HLS_PROGRAM_ROOT}/watch_hls_progress.py" "$@"
